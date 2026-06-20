from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import EvidenceSpan, Paper, PaperFigure, PaperSection, PaperTable, WritingCard
from app.schemas.documents import UnifiedFigure, UnifiedPaperDocument, UnifiedSection, UnifiedTable
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.workbench_status import HUMAN_FINAL_WORKFLOW_STATUSES
from app.utils.review_safety import writing_card_content_gate


class PaperReprocessingService:
    """Rebuilds AI-readable paper materials without requiring backend-owned LLM parsing."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.pipeline = ExtractionPipelineService(session, settings)
        self.workbench = PaperWorkbenchService(session, settings)

    def rerun_stage2(self, paper_id: UUID) -> dict[str, Any]:
        return self._run_exclusive_rebuild(paper_id, "rerun_stage2", self._rerun_stage2)

    def _rerun_stage2(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")
        raw_pdf_path = Path(paper.pdf_path) if paper.pdf_path else None
        pdf_path = (
            None
            if raw_pdf_path is not None and raw_pdf_path.is_absolute() and not raw_pdf_path.exists()
            else resolve_persisted_artifact_path(
                paper.pdf_path,
                category="pdf",
                settings=self.settings,
                trusted_persisted_reference=True,
            )
        )
        if pdf_path is not None and pdf_path.exists():
            quality = PaperWorkbenchService.assess_pdf_path(pdf_path, self.settings)
            self.workbench.apply_quality_report(paper, quality)
        document = self._rebuild_document(paper)
        if (
            paper.workflow_status not in HUMAN_FINAL_WORKFLOW_STATUSES
            and paper.workflow_status != "Rejected"
            and not bool((paper.pdf_quality_report or {}).get("needs_human_confirmation"))
        ):
            paper.workflow_status = "Parsed_Material_Ready"
        self.session.add(paper)
        self.session.commit()

        # External AI reads context through codex-context/codex-item/read_paper_page.
        # Full page preview rendering is optional and noticeably slows this path down.
        workspace_summary = self.workbench._prepare_paper_workspace_unlocked(paper.id, render_pages=False)
        self.session.refresh(paper)
        artifact_status = build_paper_artifact_status(paper, settings=self.settings)

        refreshed_materials = [
            "workspace",
            "metadata",
            "quality_report",
            "markdown_copy",
            "docling_copy",
            "evidence_sections",
            "evidence_tables",
            "evidence_figures",
            "evidence_locators",
            "ai_reading_package",
            "audit_exports",
        ]
        notes = [
            "Backend LLM deep extraction is not required for this action.",
            "Structured DFT/mechanism/writing outputs were not regenerated automatically.",
            "Use MCP paper detail, codex context, codex item, workspace artifacts, and import_analysis for the next AI step.",
        ]
        if not artifact_status.get("artifact_ready_for_external_audit"):
            notes.append(
                "External AI handoff is partially blocked until artifact prerequisites are fixed: "
                + ", ".join(artifact_status.get("blocking_errors") or ["unknown"])
            )

        return {
            "action": "prepare_external_ai_reparse_context",
            "status": "completed",
            "llm_required": False,
            "material_rebuild_completed": True,
            "external_ai_ready": bool(artifact_status.get("artifact_ready_for_external_audit")),
            "workflow_status": paper.workflow_status,
            "workspace_path": workspace_summary.get("workspace_path"),
            "workspace_abs_path": workspace_summary.get("workspace_abs_path"),
            "artifact_status": artifact_status,
            "document_snapshot": {
                "sections": len(document.sections or []),
                "tables": len(document.tables or []),
                "figures": len(document.figures or []),
                "has_markdown": bool((document.markdown or "").strip()),
                "has_docling_json": bool(document.docling_json),
                "has_pdf_reference": bool(paper.pdf_path),
            },
            "refreshed_materials": refreshed_materials,
            "deferred_capabilities": [
                "dft_deep_parse",
                "mechanism_claim_intelligence",
                "writing_card_generation",
                "complex_object_understanding",
            ],
            "next_actions": [
                "Open /api/papers/{paper_id}/codex-context or MCP get_codex_context.",
                "Read item-level evidence with /api/papers/{paper_id}/codex-item/... or MCP get_codex_item.",
                "Import external AI review results through MCP import_analysis or the external analysis workbench.",
            ],
            "notes": notes,
            "dft_settings": 0,
            "catalyst_samples": 0,
            "dft_results": 0,
            "electrochemical_performance": 0,
            "mechanism_claims": 0,
            "writing_cards": 0,
            "comprehensive_analysis": 0,
            "skipped_quality_blocked": 1 if workspace_summary.get("workflow_status") == "Needs_Human_Confirmation" else 0,
        }

    def rebuild_writing_card(self, paper_id: UUID) -> dict[str, Any]:
        """Atomically replace only WritingCard/EvidenceSpan data for one paper."""
        return self._run_exclusive_rebuild(paper_id, "rebuild_writing_card", self._rebuild_writing_card)

    def _rebuild_writing_card(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")
        document = self._rebuild_document(paper)
        payload = self.pipeline._refine_writing_card(self.pipeline.writing_card_extractor.extract(document))
        if not payload:
            raise ValueError("WritingCard extraction returned no payload")

        with self.session.begin_nested():
            old_ids = [
                str(value) for value in self.session.scalars(
                    select(WritingCard.id).where(WritingCard.paper_id == paper_id)
                ).all()
            ]
            if old_ids:
                self.session.execute(
                    delete(EvidenceSpan).where(
                        EvidenceSpan.paper_id == paper_id,
                        EvidenceSpan.object_type.in_(["writing_card", "writing_cards"]),
                        EvidenceSpan.object_id.in_(old_ids),
                    )
                )
            self.session.execute(delete(WritingCard).where(WritingCard.paper_id == paper_id))
            created = self.pipeline._persist_writing_card(paper_id, payload)
            if created != 1:
                raise RuntimeError("WritingCard replacement did not create exactly one card")

        self.session.commit()
        card = self.session.scalar(select(WritingCard).where(WritingCard.paper_id == paper_id))
        gate = writing_card_content_gate(card) if card is not None else None
        return {
            "paper_id": str(paper_id),
            "status": "completed",
            "writing_cards": created,
            "rag_eligible": bool(gate and gate.can_use_for_writing),
            "blocked_reasons": list(gate.blocked_reasons) if gate else ["missing_rebuilt_card"],
        }

    def _run_exclusive_rebuild(self, paper_id: UUID, operation: str, callback) -> dict[str, Any]:
        owner = f"paper_operation:{operation}:{uuid4().hex}"
        locks = ModuleWriteLockService(self.session)
        try:
            lock = locks.acquire(
                paper_id=paper_id,
                module_name="all_non_dft",
                locked_by=owner,
                ttl_minutes=60,
                meta={"operation": operation, "internal_operation_lock": True},
            )
            self.session.commit()
        except ValueError as exc:
            self.session.rollback()
            raise ValueError(f"paper_operation_conflict:{operation}:{paper_id}:{exc}") from exc
        try:
            return callback(paper_id)
        except Exception:
            self.session.rollback()
            raise
        finally:
            try:
                locks.release(lock_token=lock.lock_token, released_by=owner)
                self.session.commit()
            except Exception:
                self.session.rollback()

    def classify_single_paper(self, paper_id: UUID, overwrite: bool = False) -> dict[str, Any]:
        """Classify a single paper using LLM or rule fallback, and commit to DB."""
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")
        
        # If already classified and we don't want to overwrite, return existing
        if paper.paper_type and paper.paper_type != "Unknown" and not overwrite:
            return {
                "paper_type": paper.paper_type,
                "type_confidence": paper.type_confidence or 0.0,
                "classification_source": paper.classification_source or "existing",
            }
            
        # 1. Check if metadata_only paper
        is_metadata_only = not paper.pdf_path or paper.oa_status == "metadata_only"
        
        # 2. Metadata-only papers still get an AI-assisted attempt when title/abstract is available.
        if is_metadata_only:
            res = self._classify_with_ai_or_rules(paper)
            paper.paper_type = res["paper_type"]
            paper.type_confidence = res["type_confidence"]
            paper.classification_source = res["classification_source"]
            self.session.add(paper)
            self.session.commit()
            return res
            
        # 3. Rebuild document safely
        try:
            document = self._rebuild_document(paper)
        except Exception:
            # Rebuild document failed, fallback to AI-assisted metadata classification and then rules.
            res = self._classify_with_ai_or_rules(paper)
            paper.paper_type = res["paper_type"]
            paper.type_confidence = res["type_confidence"]
            paper.classification_source = res["classification_source"]
            self.session.add(paper)
            self.session.commit()
            return res
            
        # 4. Fallback if document has no valid text content
        if not document.sections and not document.abstract:
            res = self._classify_with_ai_or_rules(paper)
        else:
            try:
                quick_class = self.pipeline.comprehensive_extractor.extract_quick_classification(document)
                if quick_class and quick_class.get("paper_type") != "Unknown":
                    res = {
                        "paper_type": quick_class.get("paper_type"),
                        "type_confidence": quick_class.get("type_confidence", 0.0),
                        "classification_source": "quick"
                    }
                else:
                    sections_text = "\n".join(section.text or "" for section in (document.sections or [])[:12])
                    res = self.pipeline._rule_based_classify(paper.title, paper.journal, document.abstract, sections_text)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("LLM classification failed for paper %s, falling back to rules: %s", paper_id, e)
                sections_text = "\n".join(section.text or "" for section in (document.sections or [])[:12])
                res = self.pipeline._rule_based_classify(paper.title, paper.journal, document.abstract, sections_text)
                
        paper.paper_type = res["paper_type"]
        paper.type_confidence = res["type_confidence"]
        paper.classification_source = res["classification_source"]
        self.session.add(paper)
        self.session.commit()
        return res

    def _classify_with_ai_or_rules(self, paper: Paper) -> dict[str, Any]:
        synthetic_doc = self._build_classification_stub(paper)
        try:
            quick_class = self.pipeline.comprehensive_extractor.extract_quick_classification(synthetic_doc)
        except Exception:
            quick_class = None
        if quick_class and quick_class.get("paper_type") and quick_class.get("paper_type") != "Unknown":
            return {
                "paper_type": quick_class.get("paper_type"),
                "type_confidence": quick_class.get("type_confidence", 0.0),
                "classification_source": quick_class.get("classification_source", "quick"),
            }
        result = self.pipeline._rule_based_classify(paper.title, paper.journal, paper.abstract)
        if (not paper.pdf_path or paper.oa_status == "metadata_only") and result.get("classification_source") == "rule_heuristic":
            result["type_confidence"] = min(float(result.get("type_confidence") or 0.0), 0.5)
        return result

    def _build_classification_stub(self, paper: Paper) -> UnifiedPaperDocument:
        summary_lines = [
            f"Title: {paper.title or ''}",
            f"Journal: {paper.journal or ''}",
            f"Year: {paper.year or ''}",
        ]
        if paper.abstract:
            summary_lines.append(f"Abstract: {paper.abstract}")
        pseudo_summary = "\n".join(line for line in summary_lines if line.strip())
        sections = [
            UnifiedSection(
                section_title="Metadata Summary",
                section_type="metadata",
                text=pseudo_summary,
                page_start=None,
                page_end=None,
            )
        ] if pseudo_summary else []
        return UnifiedPaperDocument(
            metadata={
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "authors": paper.authors or [],
            },
            abstract=paper.abstract or pseudo_summary,
            sections=sections,
            tables=[],
            figures=[],
            references=[],
            markdown=pseudo_summary,
            tei_xml="",
            docling_json={},
            source_pdf_path=Path(paper.pdf_path or ""),
            tei_path=None,
            markdown_path=None,
            docling_json_path=None,
        )

    def _rebuild_document(self, paper: Paper) -> UnifiedPaperDocument:
        section_rows = self.session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == paper.id)
            .where(PaperSection.section_type.not_in(["table", "figure_caption"]))
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
        ).all()
        table_rows = self.session.scalars(
            select(PaperTable)
            .where(PaperTable.paper_id == paper.id)
            .order_by(PaperTable.page.asc().nulls_last())
        ).all()
        figure_rows = self.session.scalars(
            select(PaperFigure)
            .where(PaperFigure.paper_id == paper.id)
            .order_by(PaperFigure.page.asc().nulls_last())
        ).all()

        docling_json = self._load_json(paper.docling_json_path, category="docling_json")
        markdown = self._load_text(paper.markdown_path, category="markdown")
        tei_xml = self._load_text(paper.tei_path, category="tei")
        source_pdf_path = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=self.settings,
            must_exist=False,
            trusted_persisted_reference=True,
        ) or Path(paper.pdf_path or "")
        tei_path = resolve_persisted_artifact_path(
            paper.tei_path,
            category="tei",
            settings=self.settings,
            must_exist=False,
            trusted_persisted_reference=True,
        )
        markdown_path = resolve_persisted_artifact_path(
            paper.markdown_path,
            category="markdown",
            settings=self.settings,
            must_exist=False,
            trusted_persisted_reference=True,
        )
        docling_json_path = resolve_persisted_artifact_path(
            paper.docling_json_path,
            category="docling_json",
            settings=self.settings,
            must_exist=False,
            trusted_persisted_reference=True,
        )

        return UnifiedPaperDocument(
            metadata={
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "authors": paper.authors or [],
                "oa_status": paper.oa_status,
                "license": paper.license,
            },
            abstract=paper.abstract or "",
            sections=[
                UnifiedSection(
                    section_title=row.section_title,
                    section_type=row.section_type,
                    text=row.text,
                    page_start=row.page_start,
                    page_end=row.page_end,
                )
                for row in section_rows
            ],
            tables=[
                UnifiedTable(
                    caption=row.caption,
                    markdown_content=row.markdown_content,
                    page=row.page,
                    extraction_source=row.extraction_source,
                )
                for row in table_rows
            ],
            figures=[
                UnifiedFigure(
                    caption=row.caption,
                    image_path=row.image_path,
                    page=row.page,
                    figure_role=row.figure_role,
                )
                for row in figure_rows
            ],
            references=[],
            markdown=markdown,
            tei_xml=tei_xml,
            docling_json=docling_json,
            source_pdf_path=source_pdf_path,
            tei_path=tei_path,
            markdown_path=markdown_path,
            docling_json_path=docling_json_path,
        )

    @staticmethod
    def _load_text(path_str: str | None, category: str | None = None) -> str:
        path = resolve_persisted_artifact_path(
            path_str,
            category=category,
            trusted_persisted_reference=True,
        )
        if path is None:
            return ""
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _load_json(path_str: str | None, category: str | None = None) -> dict:
        raw = PaperReprocessingService._load_text(path_str, category=category)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
