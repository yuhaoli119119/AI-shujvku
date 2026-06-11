from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DFTResult, EvidenceLocator, ExternalAnalysisCandidate, PaperNote
from app.schemas.api import CodexContextResponse, CodexItemContextResponse, PaperDetailResponse, PaperFigureResponse
from app.services.paper_knowledge_service import PaperKnowledgeService
from app.services.paper_query import PaperQueryService
from app.utils.evidence_anchors import first_evidence_anchor, has_evidence_anchor
from app.utils.figure_reliability import build_figure_image_review
from app.utils.review_safety import bulk_export_gate_results, is_export_eligible_extraction, summarize_gate_results


class CodexContextService:
    """Builds a compact, candidate-aware paper bundle for Codex."""

    schema_version = "codex_context_v1"
    item_schema_version = "codex_item_context_v1"
    dft_export_safety_gate = "safe_verified_with_required_evidence"
    item_type_aliases = {
        "section": "section",
        "sections": "section",
        "figure": "figure",
        "figures": "figure",
        "table": "table",
        "tables": "table",
        "dft_setting": "dft_setting",
        "dft_settings": "dft_setting",
        "dft_result": "dft_result",
        "dft_results": "dft_result",
        "catalyst_sample": "catalyst_sample",
        "catalyst_samples": "catalyst_sample",
        "electrochemical_performance": "electrochemical_performance",
        "mechanism_claim": "mechanism_claim",
        "mechanism_claims": "mechanism_claim",
        "writing_card": "writing_card",
        "writing_cards": "writing_card",
        "figure_data_point": "figure_data_point",
        "figure_data_points": "figure_data_point",
    }
    item_detail_attributes = {
        "section": "sections",
        "figure": "figures",
        "table": "tables",
        "dft_setting": "dft_settings_items",
        "dft_result": "dft_results_items",
        "catalyst_sample": "catalyst_samples_items",
        "electrochemical_performance": "electrochemical_performance_items",
        "mechanism_claim": "mechanism_claims_items",
        "writing_card": "writing_cards_items",
        "figure_data_point": "figure_data_points_items",
    }

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def build_context(
        self,
        paper_id: UUID,
        *,
        max_sections: int = 8,
        max_chars_per_section: int = 1800,
        max_figures: int = 12,
        max_tables: int = 8,
        max_candidates: int = 20,
    ) -> CodexContextResponse | None:
        detail = PaperQueryService(self.session).get_paper_detail(paper_id)
        if detail is None:
            return None

        notes = self._load_notes(paper_id, limit=max_candidates)
        imported_candidates = self._load_imported_candidates(paper_id, limit=max_candidates)
        locators = self._load_locators(paper_id, limit=max_candidates * 2)
        context = self._build_json_context(
            detail,
            notes=notes,
            imported_candidates=imported_candidates,
            locators=locators,
            max_sections=max_sections,
            max_chars_per_section=max_chars_per_section,
            max_figures=max_figures,
            max_tables=max_tables,
            max_candidates=max_candidates,
        )
        markdown = self._build_markdown(context)
        return CodexContextResponse(
            paper_id=paper_id,
            title=detail.title,
            schema_version=self.schema_version,
            context=context,
            markdown=markdown,
            token_budget_hint={
                "sections_included": len(context["content"]["sections"]),
                "section_chars_each_max": max_chars_per_section,
                "figures_included": len(context["content"]["figures"]),
                "tables_included": len(context["content"]["tables"]),
                "structured_candidates_included": sum(
                    len(items)
                    for items in context["structured_candidates"].values()
                    if isinstance(items, list)
                ),
            },
        )

    def build_item_context(
        self,
        paper_id: UUID,
        item_type: str,
        item_id: UUID,
        *,
        max_chars_per_section: int = 1600,
        max_related_sections: int = 3,
        max_locators: int = 12,
    ) -> CodexItemContextResponse | None:
        normalized_type = self.item_type_aliases.get(str(item_type or "").strip().lower())
        if normalized_type is None:
            supported = ", ".join(sorted(set(self.item_type_aliases.values())))
            raise ValueError(f"Unsupported item type. Supported values: {supported}")

        detail = PaperQueryService(self.session).get_paper_detail(paper_id)
        if detail is None:
            return None
        item = self._find_detail_item(detail, normalized_type, item_id)
        if item is None:
            return None

        item_payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        raw_candidate_status = item_payload.get("candidate_status")
        if raw_candidate_status:
            item_payload["workbench_candidate_status"] = raw_candidate_status
            item_payload["candidate_status"] = self._legacy_candidate_status(raw_candidate_status)
        else:
            item_payload["candidate_status"] = self._candidate_status_for_item(normalized_type)
        export_safety = None
        if normalized_type == "figure":
            item_payload["asset_url"] = (
                f"/api/papers/assets/{item_payload.get('image_path')}"
                if item_payload.get("image_path")
                else None
            )
            item_payload["image_review"] = self._build_figure_image_review(item)
        elif normalized_type == "dft_result":
            row = self.session.get(DFTResult, item_id)
            if row is not None and row.paper_id == paper_id:
                export_safety = self._dft_export_gate_payload(row)
                item_payload["export_safety"] = export_safety
                item_payload.update(self._dft_binding_item_payload(detail, row))

        locators = self._load_item_locators(
            paper_id,
            normalized_type,
            item_id,
            limit=max_locators,
        )
        related_sections = self._related_sections(
            detail,
            normalized_type,
            item_payload,
            max_chars_per_section=max_chars_per_section,
            limit=max_related_sections,
        )
        context = {
            "schema_version": self.item_schema_version,
            "purpose": "Low-token, evidence-aware context for one paper item.",
            "reliability_policy": {
                "automatic_outputs_are_candidates": True,
                "requires_human_or_codex_review": True,
                "do_not_treat_as_verified": True,
            },
            "paper": {
                "id": str(detail.id),
                "title": detail.title,
                "doi": detail.doi,
                "year": detail.year,
                "journal": detail.journal,
                "pdf_path": detail.pdf_path,
            },
            "source_assets": {
                "pdf_url": f"/api/papers/{paper_id}/pdf",
                "pdf_path": detail.pdf_path,
                "workspace_path": detail.workspace_path,
                "has_pdf": bool(detail.pdf_path),
            },
            "item_type": normalized_type,
            "item": item_payload,
            "export_safety": export_safety,
            "evidence_locators": {
                "status_counts": dict(Counter(item.get("locator_status") or "unknown" for item in locators)),
                "items": locators,
            },
            "nearby_context": {
                "abstract": self._clip(detail.abstract, 900),
                "related_sections": related_sections,
            },
            "recommended_next_actions": self._item_next_actions(
                normalized_type,
                item_payload,
                export_safety,
                locators,
            ),
        }
        markdown = self._build_item_markdown(context)
        return CodexItemContextResponse(
            paper_id=paper_id,
            title=detail.title,
            item_type=normalized_type,
            item_id=item_id,
            schema_version=self.item_schema_version,
            context=context,
            markdown=markdown,
            token_budget_hint={
                "item_count": 1,
                "related_sections_included": len(related_sections),
                "section_chars_each_max": max_chars_per_section,
                "evidence_locators_included": len(locators),
            },
        )

    def _build_json_context(
        self,
        detail: PaperDetailResponse,
        *,
        notes: list[dict[str, Any]],
        imported_candidates: list[dict[str, Any]],
        locators: list[dict[str, Any]],
        max_sections: int,
        max_chars_per_section: int,
        max_figures: int,
        max_tables: int,
        max_candidates: int,
    ) -> dict[str, Any]:
        counts = detail.counts.model_dump(mode="json") if detail.counts else {}
        dft_export_readiness = self._build_dft_export_readiness(detail, limit=max_candidates)
        artifact_status = (
            detail.artifact_status.model_dump(mode="json")
            if hasattr(detail.artifact_status, "model_dump")
            else dict(detail.artifact_status or {})
        )
        warnings = self._build_warnings(detail, locators, dft_export_readiness)
        locator_status_counts = dict(Counter(item.get("locator_status") or "unknown" for item in locators))
        dft_results = self._dump_items(detail.dft_results_items, max_candidates)
        knowledge_candidates = PaperKnowledgeService(self.session).build_candidates(
            detail,
            max_candidates=max_candidates,
            max_chars_per_candidate=900,
        )
        safety_by_id = {
            str(item["record_id"]): item
            for item in dft_export_readiness["items"]
        }
        for item in dft_results:
            safety = safety_by_id.get(str(item.get("id")))
            if safety is not None:
                item["export_safety"] = safety

        context: dict[str, Any] = {
            "schema_version": self.schema_version,
            "purpose": "Compact paper context for Codex reading, curation, DFT review, and writing support.",
            "reliability_policy": {
                "automatic_outputs_are_candidates": True,
                "figure_crops_are_candidates": True,
                "requires_human_or_codex_review": True,
                "do_not_treat_as_verified": True,
            },
            "paper": {
                "id": str(detail.id),
                "library_name": detail.library_name,
                "serial_number": detail.serial_number,
                "title": detail.title,
                "title_zh": detail.title_zh,
                "doi": detail.doi,
                "year": detail.year,
                "journal": detail.journal,
                "authors": detail.authors,
                "paper_type": detail.paper_type,
                "type_confidence": detail.type_confidence,
                "classification_source": detail.classification_source,
                "workflow_status": detail.workflow_status,
                "pdf_quality_status": detail.pdf_quality_status,
                "pdf_quality_score": detail.pdf_quality_score,
                "oa_status": detail.oa_status,
                "license": detail.license,
                "created_at": detail.created_at.isoformat() if detail.created_at else None,
            },
            "source_assets": {
                "has_pdf": bool(artifact_status.get("pdf_exists")),
                "pdf_path": detail.pdf_path,
                "tei_path": detail.tei_path,
                "docling_json_path": detail.docling_json_path,
                "markdown_path": detail.markdown_path,
                "workspace_path": detail.workspace_path,
                "artifact_status": artifact_status,
                "markdown_trust": (
                    detail.pdf_quality_report or {}
                ).get("markdown_trust") if isinstance(detail.pdf_quality_report, dict) else None,
                "full_translation_available": bool(detail.full_translation_zh),
            },
            "artifact_status": artifact_status,
            "external_audit_precondition": {
                "status": "ready"
                if artifact_status.get("artifact_ready_for_external_audit")
                else "artifact_precondition_failed",
                "blocking_errors": artifact_status.get("blocking_errors") or [],
            },
            "counts": counts,
            "warnings": warnings,
            "content": {
                "abstract": self._clip(detail.abstract, max_chars_per_section),
                "abstract_zh": self._clip(detail.abstract_zh, max_chars_per_section),
                "sections": [
                    {
                        "id": str(section.id),
                        "title": section.section_title or section.section_type or "Untitled section",
                        "section_type": section.section_type,
                        "page_start": section.page_start,
                        "page_end": section.page_end,
                        "text": self._clip(section.text, max_chars_per_section),
                        "truncated": len(section.text or "") > max_chars_per_section,
                    }
                    for section in (detail.sections or [])[:max_sections]
                ],
                "figures": [
                    {
                        "id": str(figure.id),
                        "caption": self._clip(figure.caption, 1200),
                        "page": figure.page,
                        "image_path": figure.image_path,
                        "asset_url": f"/api/papers/assets/{figure.image_path}" if figure.image_path else None,
                        "prov": figure.prov,
                        "image_review": self._build_figure_image_review(figure),
                        "figure_role": figure.figure_role,
                        "role_confidence": figure.role_confidence,
                        "content_summary": figure.content_summary,
                        "key_elements": figure.key_elements,
                        "candidate_status": "candidate_unverified",
                    }
                    for figure in (detail.figures or [])[:max_figures]
                ],
                "tables": [
                    {
                        "id": str(table.id),
                        "caption": self._clip(table.caption, 800),
                        "page": table.page,
                        "markdown_content": self._clip(table.markdown_content, 2200),
                        "candidate_status": "candidate_unverified",
                    }
                    for table in (detail.tables or [])[:max_tables]
                ],
            },
            "structured_candidates": {
                "dft_settings": self._dump_items(detail.dft_settings_items, max_candidates),
                "catalyst_samples": self._dump_items(detail.catalyst_samples_items, max_candidates),
                "dft_results": dft_results,
                "electrochemical_performance": self._dump_items(detail.electrochemical_performance_items, max_candidates),
                "mechanism_claims": self._dump_items(detail.mechanism_claims_items, max_candidates),
                "writing_cards": self._dump_items(detail.writing_cards_items, max_candidates),
                "figure_data_points": self._dump_items(detail.figure_data_points_items, max_candidates),
                "knowledge_candidates": knowledge_candidates,
                "candidate_status": "automatic_unverified_candidates",
            },
            "knowledge_candidates": {
                "schema_version": PaperKnowledgeService.schema_version,
                "items": knowledge_candidates,
            },
            "dft_export_readiness": dft_export_readiness,
            "evidence_locators": {
                "status_counts": locator_status_counts,
                "items": locators,
            },
            "external_analysis_candidates": imported_candidates,
            "notes": notes,
            "relationships": {
                "outgoing": self._dump_items(detail.outgoing_relationships, max_candidates),
                "incoming": self._dump_items(detail.incoming_relationships, max_candidates),
                "relationship_summary": detail.relationship_summary,
            },
            "references_preview": self._dump_items(detail.references, max_candidates),
            "recommended_next_actions": self._next_actions(detail, warnings, locator_status_counts),
        }
        return context

    def _build_warnings(
        self,
        detail: PaperDetailResponse,
        locators: list[dict[str, Any]],
        dft_export_readiness: dict[str, Any],
    ) -> list[dict[str, str]]:
        warnings: list[dict[str, str]] = []
        artifact_status = (
            detail.artifact_status.model_dump(mode="json")
            if hasattr(detail.artifact_status, "model_dump")
            else dict(detail.artifact_status or {})
        )
        artifact_errors = set(artifact_status.get("blocking_errors") or [])
        if "missing_pdf" in artifact_errors or not detail.pdf_path or detail.oa_status in {"metadata_only", "needs_upload"}:
            warnings.append({"code": "missing_pdf", "message": "PDF is missing or this record is metadata-only."})
        if "missing_markdown_and_docling_json" in artifact_errors:
            warnings.append(
                {
                    "code": "missing_markdown_and_docling_json",
                    "message": "Neither Markdown nor Docling JSON contains readable parsed content.",
                }
            )
        if "missing_ai_reading_package" in artifact_errors:
            warnings.append(
                {
                    "code": "missing_ai_reading_package",
                    "message": "The AI reading package is missing; external audit must stop at the artifact precondition.",
                }
            )
        if "invalid_pdf_content" in artifact_errors:
            warnings.append(
                {
                    "code": "invalid_pdf_content",
                    "message": "The stored PDF failed the quality/openability check and cannot be used as audit evidence.",
                }
            )
        if "workflow_blocked_for_external_audit" in artifact_errors:
            warnings.append(
                {
                    "code": "workflow_blocked_for_external_audit",
                    "message": "The paper workflow is still blocked and must not be treated as externally auditable.",
                }
            )
        if not detail.sections:
            warnings.append({"code": "missing_sections", "message": "No parsed body sections are available."})
        if not detail.figures:
            warnings.append({"code": "missing_figures", "message": "No paper figures are available from the parser."})
        elif any(self._build_figure_image_review(figure).get("review_required") for figure in detail.figures):
            warnings.append({"code": "figure_crop_review", "message": "One or more extracted figure crops need review before reuse."})
        if detail.dft_results_items and not detail.dft_settings_items:
            warnings.append({"code": "missing_dft_settings", "message": "DFT result candidates exist but DFT settings are missing."})
        if detail.dft_results_items:
            warnings.append({"code": "dft_unverified", "message": "DFT rows are extraction candidates and require evidence review before ML export."})
        if dft_export_readiness.get("blocked_count"):
            warnings.append(
                {
                    "code": "dft_export_blocked",
                    "message": (
                        f"{dft_export_readiness['blocked_count']} DFT candidate(s) are blocked by the "
                        "review/evidence/locator export gate."
                    ),
                }
            )
        if locators:
            exact = sum(1 for item in locators if item.get("locator_status") in {"exact_page", "exact_bbox"})
            if exact == 0:
                warnings.append({"code": "no_exact_pdf_locators", "message": "Evidence locator rows exist, but none have exact PDF page/bbox status."})
        return warnings

    def _next_actions(
        self,
        detail: PaperDetailResponse,
        warnings: list[dict[str, str]],
        locator_status_counts: dict[str, int],
    ) -> list[str]:
        codes = {item["code"] for item in warnings}
        actions: list[str] = []
        if {
            "missing_pdf",
            "missing_markdown_and_docling_json",
            "missing_ai_reading_package",
            "invalid_pdf_content",
            "workflow_blocked_for_external_audit",
        } & codes:
            actions.append("Return artifact_precondition_failed before auditing; the artifact_status blocking_errors list explains why.")
        if "missing_pdf" in codes:
            actions.append("Attach or download the PDF before treating this paper as readable evidence.")
        if "missing_sections" in codes:
            actions.append("Re-run PDF parsing or inspect parser logs; Codex has no body text to read.")
        if "missing_figures" in codes:
            actions.append("Use PDF pages/captions directly; figure extraction is currently absent.")
        if "dft_unverified" in codes or detail.dft_results_items:
            actions.append("Review DFT candidates against evidence text and PDF locators before database export.")
        if "dft_export_blocked" in codes:
            actions.append("Use each DFT candidate's export_safety.blocked_reasons to repair review, evidence, or locator gaps.")
        if locator_status_counts and not any(key in locator_status_counts for key in ("exact_page", "exact_bbox")):
            actions.append("Repair evidence locators; current evidence is text-only or missing exact pages.")
        if not actions:
            actions.append("Read sections, compare candidates against evidence, then append notes or correction proposals as needed.")
        return actions

    def _build_markdown(self, context: dict[str, Any]) -> str:
        paper = context["paper"]
        lines = [
            f"# {paper.get('title') or 'Untitled paper'}",
            "",
            "## Codex Use Policy",
            "- Automatic parser, extraction, and external analysis outputs are candidates, not verified facts.",
            "- Use evidence text, PDF locators, and notes before writing conclusions or exporting data.",
            "",
            "## Metadata",
            f"- Paper ID: `{paper.get('id')}`",
            f"- DOI: {paper.get('doi') or '-'}",
            f"- Year / Journal: {paper.get('year') or '-'} / {paper.get('journal') or '-'}",
            f"- Type: {paper.get('paper_type') or '-'} (confidence: {paper.get('type_confidence') or '-'})",
            f"- PDF: {'available' if context['source_assets']['has_pdf'] else 'missing'}",
            "",
        ]
        artifact_status = context.get("artifact_status") or {}
        lines.extend(
            [
                "## Artifact Status",
                f"- External audit precondition: `{context.get('external_audit_precondition', {}).get('status')}`",
                f"- PDF exists / size / path kind: {artifact_status.get('pdf_exists')} / {artifact_status.get('pdf_file_size') or '-'} / {artifact_status.get('pdf_path_kind')}",
                f"- Markdown / Docling / GROBID content: {artifact_status.get('markdown_has_content')} / {artifact_status.get('docling_json_has_content')} / {artifact_status.get('grobid_tei_has_content')}",
                f"- AI reading package / workspace: {artifact_status.get('ai_reading_package_exists')} / {artifact_status.get('workspace_exists')}",
                f"- Blocking errors: {artifact_status.get('blocking_errors') or 'none'}",
                "",
            ]
        )
        if context["warnings"]:
            lines.extend(["## Warnings"])
            lines.extend(f"- `{item['code']}`: {item['message']}" for item in context["warnings"])
            lines.append("")

        abstract = context["content"].get("abstract") or context["content"].get("abstract_zh")
        if abstract:
            lines.extend(["## Abstract", abstract, ""])

        lines.append("## Sections")
        for idx, section in enumerate(context["content"]["sections"], start=1):
            page = self._page_label(section.get("page_start"), section.get("page_end"))
            lines.extend([
                f"### {idx}. {section.get('title') or 'Untitled section'} {page}",
                section.get("text") or "",
                "",
            ])
        if not context["content"]["sections"]:
            lines.extend(["No parsed sections available.", ""])

        lines.append("## Figures")
        for figure in context["content"]["figures"]:
            review = figure.get("image_review") or {}
            flags = review.get("flags") or []
            flag_text = f" | flags={', '.join(flags)}" if flags else ""
            size = review.get("pixel_size") or {}
            size_text = (
                f" | size={size.get('width')}x{size.get('height')}"
                if size.get("width") and size.get("height")
                else ""
            )
            lines.append(
                f"- Page {figure.get('page') or '-'} | {figure.get('figure_role') or 'unknown'}"
                f" | crop={review.get('crop_status') or 'unknown'}{size_text}{flag_text}"
                f" | image={figure.get('image_path') or '-'} | {figure.get('caption') or 'No caption'}"
            )
        if not context["content"]["figures"]:
            lines.append("- No parsed figures.")
        lines.append("")

        lines.append("## Tables")
        for table in context["content"]["tables"]:
            lines.append(f"### Table page {table.get('page') or '-'}")
            if table.get("caption"):
                lines.append(table["caption"])
            if table.get("markdown_content"):
                lines.append(table["markdown_content"])
            lines.append("")
        if not context["content"]["tables"]:
            lines.extend(["No parsed tables.", ""])

        structured = context["structured_candidates"]
        lines.append("## Structured Candidates")
        for key in [
            "dft_settings",
            "catalyst_samples",
            "dft_results",
            "electrochemical_performance",
            "mechanism_claims",
            "writing_cards",
            "knowledge_candidates",
        ]:
            items = structured.get(key) or []
            lines.append(f"### {key} ({len(items)})")
            for item in items[:10]:
                lines.append("- " + self._one_line_candidate(item))
            if not items:
                lines.append("- none")
        lines.append("")

        readiness = context["dft_export_readiness"]
        lines.extend(
            [
                "## DFT Export Readiness",
                f"- Safety gate: `{readiness['safety_gate']}`",
                f"- Total / eligible / blocked: {readiness['total_candidates']} / {readiness['eligible_count']} / {readiness['blocked_count']}",
                f"- Blocked reasons: {readiness['blocked_reasons'] or 'none'}",
            ]
        )
        for item in readiness["items"][:10]:
            lines.append(
                f"- `{item['record_id']}` | exportable={item['is_exportable']} | "
                f"review={item['review_status']} | locator={item['locator_status']} | "
                f"blocked={item['blocked_reasons'] or 'none'}"
            )
        lines.append("")

        lines.append("## Evidence Locator Summary")
        counts = context["evidence_locators"]["status_counts"]
        lines.append(", ".join(f"{key}: {value}" for key, value in counts.items()) if counts else "No evidence locators.")
        lines.append("")

        lines.append("## External Analysis Candidates")
        for candidate in context["external_analysis_candidates"][:10]:
            lines.append(
                f"- {candidate.get('candidate_type')} | status={candidate.get('status')} | confidence={candidate.get('confidence')} | {candidate.get('summary') or ''}"
            )
        if not context["external_analysis_candidates"]:
            lines.append("- none")
        lines.append("")

        lines.append("## Recommended Next Actions")
        lines.extend(f"- {action}" for action in context["recommended_next_actions"])
        lines.append("")
        return "\n".join(lines)

    def _build_item_markdown(self, context: dict[str, Any]) -> str:
        paper = context["paper"]
        item = context["item"]
        lines = [
            f"# Codex Item: {context['item_type']}",
            "",
            f"- Paper: {paper.get('title') or 'Untitled paper'}",
            f"- Paper ID: `{paper.get('id')}`",
            f"- Item ID: `{item.get('id')}`",
            "- Reliability: automatic/parser outputs are candidates until reviewed against evidence.",
            "",
            "## Item",
            "```json",
            self._compact_json(item),
            "```",
            "",
        ]
        if context.get("export_safety"):
            lines.extend(
                [
                    "## Export Safety",
                    "```json",
                    self._compact_json(context["export_safety"]),
                    "```",
                    "",
                ]
            )
        if context["item_type"] == "dft_result":
            lines.extend(
                [
                    "## AI Review Protocol",
                    "You are a materials-computation data reviewer. Do not invent or repair values from memory.",
                    "Open the original PDF evidence first. Do not bind or correct this row from parsed markdown alone.",
                    "Check whether this DFT candidate is directly supported by the PDF evidence package.",
                    "Required checks: material/catalyst binding, adsorbate, property type, numeric value, unit, method/condition, evidence excerpt, page/section/table/figure locator, duplicates, and suspected missing data.",
                    "If catalyst_sample_id is blank, choose one explicit candidate catalyst sample and cite the exact source anchor used for the binding.",
                    "Never auto-bind because the paper has only one sample or because one sample looks similar; the choice must come from the PDF evidence.",
                    "If a figure only shows a trend/path and no readable value, mark pending/needs_fix instead of estimating from the image.",
                    "Output exactly one decision: accept / reject / needs_fix / suspected_duplicate / suspected_missing, followed by a concise reason and evidence location.",
                    "",
                ]
            )
        lines.append("## Evidence Locators")
        for locator in context["evidence_locators"]["items"]:
            lines.append(
                f"- page={locator.get('page') or '-'} | status={locator.get('locator_status') or 'unknown'} | "
                f"field={locator.get('field_name') or '-'} | {locator.get('evidence_text') or ''}"
            )
        if not context["evidence_locators"]["items"]:
            lines.append("- none")
        lines.append("")
        lines.append("## Related Sections")
        for section in context["nearby_context"]["related_sections"]:
            lines.extend(
                [
                    f"### {section.get('title') or 'Untitled section'} {self._page_label(section.get('page_start'), section.get('page_end'))}",
                    section.get("text") or "",
                    "",
                ]
            )
        if not context["nearby_context"]["related_sections"]:
            lines.extend(["No related parsed sections found.", ""])
        lines.append("## Recommended Next Actions")
        lines.extend(f"- {action}" for action in context["recommended_next_actions"])
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _clip(value: Any, max_chars: int) -> str:
        text = "" if value is None else str(value)
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + " [truncated]"

    @staticmethod
    def _page_label(start: int | None, end: int | None) -> str:
        if start and end and start != end:
            return f"(pages {start}-{end})"
        if start:
            return f"(page {start})"
        return ""

    def _build_figure_image_review(self, figure: PaperFigureResponse) -> dict[str, Any]:
        return build_figure_image_review(figure, settings=self.settings, check_asset_exists=True)

    def _dump_items(self, items: list[Any], limit: int) -> list[dict[str, Any]]:
        dumped: list[dict[str, Any]] = []
        for item in (items or [])[:limit]:
            if hasattr(item, "model_dump"):
                payload = item.model_dump(mode="json")
            else:
                payload = dict(item)
            raw_candidate_status = payload.get("candidate_status")
            if raw_candidate_status:
                payload["workbench_candidate_status"] = raw_candidate_status
            payload["candidate_status"] = self._legacy_candidate_status(raw_candidate_status)
            dumped.append(payload)
        return dumped

    def _build_dft_export_readiness(self, detail: PaperDetailResponse, *, limit: int) -> dict[str, Any]:
        rows = self.session.scalars(
            select(DFTResult).where(DFTResult.paper_id == detail.id)
        ).all()
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        gates = []
        items = []
        for row in rows:
            gate = gate_by_id.get(str(row.id))
            if gate is None:
                continue
            gates.append(gate)
            if len(items) < limit:
                items.append(self._dft_export_gate_payload(row, gate=gate))
        summary = summarize_gate_results(gates)
        return {
            "safety_gate": self.dft_export_safety_gate,
            "total_candidates": summary["total_candidates"],
            "eligible_count": summary["eligible"],
            "blocked_count": summary["blocked"],
            "blocked_reasons": summary["blocked_reasons"],
            "items": items,
        }

    def _dft_export_gate_payload(self, row: DFTResult, *, gate: Any = None) -> dict[str, Any]:
        gate = gate or is_export_eligible_extraction(self.session, row, target_type="dft_results")
        return {
            "record_id": str(row.id),
            "candidate_status": row.candidate_status or "system_candidate",
            "is_exportable": gate.eligible,
            "eligible": gate.eligible,
            "blocked_reasons": list(gate.reasons),
            "review_status": gate.review_status,
            "review_gate_status": gate.review_gate_status,
            "provenance_level": gate.provenance_level,
            "locator_status": gate.locator_status,
        }

    def _find_detail_item(self, detail: PaperDetailResponse, item_type: str, item_id: UUID) -> Any | None:
        attribute = self.item_detail_attributes[item_type]
        return next(
            (item for item in (getattr(detail, attribute, None) or []) if str(item.id) == str(item_id)),
            None,
        )

    @staticmethod
    def _candidate_status_for_item(item_type: str) -> str:
        if item_type == "section":
            return "parsed_source_text"
        if item_type in {"figure", "table"}:
            return "parser_candidate_unverified"
        return "candidate_unverified"

    @staticmethod
    def _legacy_candidate_status(status: Any) -> str:
        normalized = str(status or "").strip()
        if normalized in {"Codex_Candidate", "system_candidate"}:
            return "candidate_unverified"
        if not normalized or normalized in {"Imported", "Quality_Checked", "Parsed_Material_Ready"}:
            return "candidate_unverified"
        if normalized == "Gemini_Verified":
            return "gemini_reviewed_candidate"
        if normalized == "Human_Confirmed":
            return "human_confirmed"
        if normalized in {"ML_Ready", "Citation_Ready"}:
            return "human_confirmed"
        return normalized

    def _load_item_locators(
        self,
        paper_id: UUID,
        item_type: str,
        item_id: UUID,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        conditions = [EvidenceLocator.target_id == str(item_id)]
        if item_type == "figure":
            conditions.append(EvidenceLocator.figure_id == item_id)
        elif item_type == "table":
            conditions.append(EvidenceLocator.table_id == item_id)
        rows = self.session.scalars(
            select(EvidenceLocator)
            .where(
                EvidenceLocator.paper_id == paper_id,
                or_(*conditions),
            )
            .order_by(EvidenceLocator.created_at.desc())
            .limit(limit)
        ).all()
        return [self._locator_payload(row) for row in rows]

    def _related_sections(
        self,
        detail: PaperDetailResponse,
        item_type: str,
        item_payload: dict[str, Any],
        *,
        max_chars_per_section: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        page = item_payload.get("page")
        source_section = str(item_payload.get("source_section") or "").strip().lower()
        evidence_text = str(item_payload.get("evidence_text") or "").strip()
        evidence_probe = " ".join(evidence_text.split())[:120].lower()
        ranked: list[tuple[int, int, Any]] = []
        for index, section in enumerate(detail.sections or []):
            score = 0
            if item_type == "section" and str(section.id) == str(item_payload.get("id")):
                score += 10
            if page and section.page_start and section.page_end and section.page_start <= page <= section.page_end:
                score += 6
            title = str(section.section_title or section.section_type or "").lower()
            if source_section and (source_section in title or title in source_section):
                score += 4
            section_text = " ".join((section.text or "").split()).lower()
            if evidence_probe and evidence_probe in section_text:
                score += 3
            if score:
                ranked.append((-score, index, section))
        ranked.sort()
        return [
            {
                "id": str(section.id),
                "title": section.section_title or section.section_type or "Untitled section",
                "section_type": section.section_type,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "text": self._clip(section.text, max_chars_per_section),
                "truncated": len(section.text or "") > max_chars_per_section,
            }
            for _, _, section in ranked[:limit]
        ]

    def _item_next_actions(
        self,
        item_type: str,
        item_payload: dict[str, Any],
        export_safety: dict[str, Any] | None,
        locators: list[dict[str, Any]],
    ) -> list[str]:
        actions = []
        if item_type == "dft_result":
            actions.append("Open the original PDF page/table/figure before trusting parsed fields or proposing a catalyst binding.")
            if not has_evidence_anchor(item_payload.get("binding_evidence_anchor")) and not any(
                item.get("locator_status") in {"exact_page", "exact_bbox"} for item in locators
            ):
                actions.append("Do not bind this DFT row until you can cite a page, section, table, figure, or quoted-text anchor from the PDF.")
            if not item_payload.get("catalyst_sample_id"):
                candidate_count = len(item_payload.get("candidate_catalyst_samples") or [])
                if candidate_count > 1:
                    actions.append("Explicitly choose one catalyst_sample_id from the candidate list; do not silently fall back to the first sample.")
                elif candidate_count == 1:
                    actions.append("Even with one candidate catalyst sample, confirm the binding against the PDF before proposing catalyst_sample_id.")
        if item_type == "figure":
            review = item_payload.get("image_review") or {}
            if review.get("review_required"):
                actions.append("Compare the crop with the original PDF page before interpreting or reusing the figure.")
        if export_safety and not export_safety.get("is_exportable"):
            actions.append(
                "Resolve the DFT export blockers: "
                + ", ".join(export_safety.get("blocked_reasons") or ["unknown"])
                + "."
            )
        if not locators:
            actions.append("Locate this item in the PDF or attach an evidence locator before treating it as verified.")
        elif not any(item.get("locator_status") in {"exact_page", "exact_bbox"} for item in locators):
            actions.append("Repair or confirm the PDF locator; current evidence is not exact.")
        if not actions:
            actions.append("Review the item against its evidence, then append a note or propose a correction if needed.")
        return actions

    @staticmethod
    def _dft_candidate_catalyst_payload(detail: PaperDetailResponse) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for catalyst in detail.catalyst_samples_items or []:
            items.append(
                {
                    "id": str(catalyst.id),
                    "name": catalyst.name,
                    "catalyst_type": catalyst.catalyst_type,
                    "metal_centers": catalyst.metal_centers or [],
                    "coordination": catalyst.coordination,
                    "support": catalyst.support,
                    "synthesis_method": catalyst.synthesis_method,
                    "evidence_strength": catalyst.evidence_strength,
                    "has_material_identity": any(
                        (
                            bool((catalyst.name or "").strip()),
                            bool((catalyst.catalyst_type or "").strip()),
                            bool(catalyst.metal_centers),
                            bool((catalyst.coordination or "").strip()),
                            bool((catalyst.support or "").strip()),
                        )
                    ),
                }
            )
        return items

    def _dft_binding_item_payload(self, detail: PaperDetailResponse, row: DFTResult) -> dict[str, Any]:
        candidate_samples = self._dft_candidate_catalyst_payload(detail)
        current_sample = next(
            (item for item in candidate_samples if item["id"] == str(row.catalyst_sample_id)),
            None,
        )
        binding_payload = (
            (row.evidence_payload or {}).get("material_binding")
            if isinstance(row.evidence_payload, dict)
            else None
        )
        return {
            "binding_status": "bound" if row.catalyst_sample_id else "unbound",
            "current_catalyst_sample": current_sample,
            "candidate_catalyst_samples": candidate_samples,
            "binding_evidence_anchor": first_evidence_anchor(binding_payload),
            "requires_explicit_material_choice": not bool(row.catalyst_sample_id) and len(candidate_samples) > 1,
        }

    def _locator_payload(self, row: EvidenceLocator) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "target_type": row.target_type,
            "target_id": row.target_id,
            "field_name": row.field_name,
            "page": row.page,
            "bbox": row.bbox,
            "section": row.section,
            "figure_id": str(row.figure_id) if row.figure_id else None,
            "table_id": str(row.table_id) if row.table_id else None,
            "locator_status": row.locator_status,
            "locator_confidence": row.locator_confidence,
            "parser_source": row.parser_source,
            "warning_reason": row.warning_reason,
            "evidence_text": self._clip(row.evidence_text, 900),
        }

    @staticmethod
    def _compact_json(value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    def _load_notes(self, paper_id: UUID, *, limit: int) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(PaperNote)
            .where(PaperNote.paper_id == paper_id)
            .order_by(PaperNote.created_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": str(row.id),
                "source": row.source,
                "field_name": row.field_name,
                "page": row.page,
                "section_title": row.section_title,
                "quoted_text": self._clip(row.quoted_text, 500),
                "content": self._clip(row.content, 1200),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    def _load_imported_candidates(self, paper_id: UUID, *, limit: int) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .order_by(ExternalAnalysisCandidate.created_at.desc())
            .limit(limit)
        ).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = row.normalized_payload if isinstance(row.normalized_payload, dict) else {}
            items.append(
                {
                    "id": str(row.id),
                    "candidate_type": row.candidate_type,
                    "status": row.status,
                    "confidence": row.confidence,
                    "summary": self._clip(
                        payload.get("content")
                        or payload.get("reason")
                        or payload.get("summary")
                        or row.mapping_reason
                        or "",
                        700,
                    ),
                    "mapping_reason": self._clip(row.mapping_reason, 700),
                    "materialized_target_type": row.materialized_target_type,
                    "materialized_target_id": row.materialized_target_id,
                    "candidate_status": "imported_candidate_unverified",
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
        return items

    def _load_locators(self, paper_id: UUID, *, limit: int) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(EvidenceLocator)
            .where(EvidenceLocator.paper_id == paper_id)
            .order_by(EvidenceLocator.created_at.desc())
            .limit(limit)
        ).all()
        return [self._locator_payload(row) for row in rows]

    def _one_line_candidate(self, item: dict[str, Any]) -> str:
        preferred_keys = [
            "category",
            "title",
            "content",
            "adsorbate",
            "property_type",
            "value",
            "unit",
            "software",
            "functional",
            "name",
            "claim_type",
            "claim_text",
            "research_gap",
            "proposed_solution",
            "confidence",
            "evidence_text",
        ]
        parts = []
        for key in preferred_keys:
            value = item.get(key)
            if value not in (None, "", [], {}):
                parts.append(f"{key}={self._clip(value, 160)}")
            if len(parts) >= 5:
                break
        return "; ".join(parts) if parts else self._clip(item, 300)
