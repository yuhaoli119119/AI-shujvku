from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.schemas.dft_review_bundle import OfflineDFTReviewResult
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.utils.artifact_paths import resolve_persisted_artifact_path


OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION = "offline_dft_review_bundle_v1"
MAX_TEXT_SNIPPETS = 100
MAX_TEXT_SNIPPET_CHARS = 6000
MAX_FIGURE_FILES = 24
MAX_TOTAL_FIGURE_BYTES = 24 * 1024 * 1024

DFT_SIGNAL_RE = re.compile(
    r"(?:\bDFT\b|density\s+functional|first[-\s]?principles?|computational\s+details?|"
    r"calculation\s+method|adsorption\s+energ|binding\s+energ|free\s+energ|gibbs|"
    r"delta\s*g|Δ\s*G|reaction\s+barrier|activation\s+energ|dissociation\s+energ|"
    r"bader|charge\s+transfer|electronic\s+structure|density\s+of\s+states|\bDOS\b|"
    r"\bVASP\b|quantum\s+espresso|\bCASTEP\b|\bDMol|pseudopotential|k[-\s]?point|"
    r"plane[-\s]?wave|cutoff\s+energ|\bPBE\b|\bHSE\d*\b|van\s+der\s+waals|\bvdW\b)",
    re.IGNORECASE,
)

ALLOWED_DFT_REVIEW_FIELDS = {
    "dft_results",
    "property_type",
    "energy_type",
    "value",
    "unit",
    "adsorbate",
    "reaction_step",
    "catalyst",
    "catalyst_sample_id",
    "source_section",
    "source_figure",
    "evidence_text",
    "confidence",
}


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized or fallback


class DFTReviewBundleService:
    """Build and validate small, temporary, offline DFT review packages."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def build_zip(self, paper_id: UUID, *, include_figure_files: bool = True) -> dict[str, Any]:
        materials = self._build_materials(paper_id)
        files: dict[str, bytes] = {
            "parsed/paper_metadata.json": _json_bytes(materials["paper_metadata"]),
            "parsed/initial_dft_candidates.json": _json_bytes(materials["initial_dft_candidates"]),
            "parsed/extracted_tables.json": _json_bytes(materials["extracted_tables"]),
            "parsed/extracted_figures.json": _json_bytes(self._public_figures(materials["extracted_figures"])),
            "evidence/text_snippets.jsonl": self._jsonl_bytes(materials["text_snippets"]),
            "return_schema.json": _json_bytes(OfflineDFTReviewResult.model_json_schema()),
        }

        for table in materials["extracted_tables"]:
            filename = _safe_name(table["evidence_id"].replace(":", "_"), "table") + ".md"
            files[f"evidence/tables/{filename}"] = self._table_markdown(table).encode("utf-8")

        figure_warnings: list[str] = []
        figure_bytes_total = 0
        figure_file_count = 0
        if include_figure_files:
            for figure in materials["extracted_figures"]:
                artifact = self._resolve_figure(figure.get("_artifact_reference"))
                if artifact is None:
                    continue
                if figure_file_count >= MAX_FIGURE_FILES:
                    figure_warnings.append("figure_file_limit_reached")
                    break
                size = artifact.stat().st_size
                if figure_bytes_total + size > MAX_TOTAL_FIGURE_BYTES:
                    figure_warnings.append("figure_byte_limit_reached")
                    continue
                suffix = artifact.suffix.lower() or ".png"
                filename = _safe_name(figure["evidence_id"].replace(":", "_"), "figure") + suffix
                data = artifact.read_bytes()
                files[f"evidence/figures/{filename}"] = data
                figure["bundle_file"] = f"evidence/figures/{filename}"
                figure_file_count += 1
                figure_bytes_total += len(data)

        # Re-serialize figures after assigning bundle-local file names.
        files["parsed/extracted_figures.json"] = _json_bytes(self._public_figures(materials["extracted_figures"]))

        template = self._return_template(materials)
        files["return_template.json"] = _json_bytes(template)
        files["instructions_for_web_ai.md"] = self._instructions(materials).encode("utf-8")

        inventory = [
            {"path": path, "size_bytes": len(data), "sha256": _sha256(data)}
            for path, data in sorted(files.items())
        ]
        manifest = {
            "schema_version": OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper": {
                "paper_id": materials["paper_metadata"]["paper_id"],
                "paper_code": materials["paper_metadata"]["paper_code"],
                "title": materials["paper_metadata"]["title"],
            },
            "review_scope": "single_paper_main_plus_relevant_supplementary_dft_evidence",
            "writeback_paper_id": materials["paper_metadata"]["paper_id"],
            "evidence_ids": sorted(materials["evidence_map"]),
            "target_dft_result_ids": sorted(materials["target_dft_result_ids"]),
            "counts": {
                "main_dft_candidates": len(materials["initial_dft_candidates"]["existing_candidates"]),
                "supporting_si_dft_candidates": len(
                    materials["initial_dft_candidates"]["supporting_si_candidates"]
                ),
                "text_snippets": len(materials["text_snippets"]),
                "tables": len(materials["extracted_tables"]),
                "figures": len(materials["extracted_figures"]),
                "included_figure_files": figure_file_count,
            },
            "warnings": sorted(set(materials["warnings"] + figure_warnings)),
            "retention_policy": "generated_in_memory_not_persisted_on_server",
            "files": inventory,
        }
        files["manifest.json"] = _json_bytes(manifest)

        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for path, data in sorted(files.items()):
                archive.writestr(path, data)

        paper_code = _safe_name(materials["paper_metadata"]["paper_code"], "paper")
        return {
            "filename": f"{paper_code}_dft_review_bundle.zip",
            "content": buffer.getvalue(),
            "manifest": manifest,
        }

    def validate_result(self, paper_id: UUID, raw_payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = OfflineDFTReviewResult.model_validate(raw_payload)
        except ValidationError as exc:
            return {
                "valid": False,
                "errors": [
                    {
                        "code": "schema_validation_error",
                        "path": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ],
                "warnings": [],
                "import_analysis_request": None,
            }

        materials = self._build_materials(paper_id)
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        metadata = materials["paper_metadata"]

        def add_error(code: str, message: str, *, audit_index: int | None = None) -> None:
            item: dict[str, Any] = {"code": code, "message": message}
            if audit_index is not None:
                item["audit_index"] = audit_index
            errors.append(item)

        if result.paper_id != metadata["paper_id"]:
            add_error("paper_id_mismatch", "review_result paper_id does not match the selected paper")
        if result.paper_code != metadata["paper_code"]:
            add_error("paper_code_mismatch", "review_result paper_code does not match the selected paper")
        if result.bundle_fingerprint != materials["bundle_fingerprint"]:
            add_error(
                "stale_or_mismatched_bundle",
                "bundle_fingerprint differs from the current evidence snapshot; export a new package and review again",
            )

        normalized_audits: list[dict[str, Any]] = []
        reviewed_target_ids: set[str] = set()
        for index, audit in enumerate(result.object_review_audits):
            if audit.field_name not in ALLOWED_DFT_REVIEW_FIELDS:
                add_error(
                    "field_out_of_scope",
                    f"field_name '{audit.field_name}' is outside the DFT review scope",
                    audit_index=index,
                )
            if audit.target_id.lower() != "new" and audit.target_id not in materials["target_dft_result_ids"]:
                add_error(
                    "unknown_target_id",
                    f"target_id '{audit.target_id}' is not a current main-paper DFT candidate",
                    audit_index=index,
                )
            elif audit.target_id.lower() != "new":
                reviewed_target_ids.add(audit.target_id)
            missing_evidence = [item for item in audit.evidence_ids if item not in materials["evidence_map"]]
            if missing_evidence:
                add_error(
                    "unknown_evidence_id",
                    f"Evidence ids are not present in the package: {', '.join(missing_evidence)}",
                    audit_index=index,
                )
            if audit.decision != "NEEDS_HUMAN" and not audit.evidence_checked:
                add_error(
                    "evidence_not_checked",
                    f"{audit.decision} requires evidence_checked=true",
                    audit_index=index,
                )
            if missing_evidence:
                continue

            evidence_items = [materials["evidence_map"][item] for item in audit.evidence_ids]
            primary = evidence_items[0]
            primary_table = (
                primary.get("table") or primary.get("caption")
                if primary.get("evidence_kind") == "table"
                else primary.get("table")
            )
            evidence_location = {
                "source_document_type": primary.get("source_document_type") or "main_text",
                "page": primary.get("page") or primary.get("page_start"),
                "section": primary.get("section") or primary.get("section_title"),
                "table": primary_table,
                "figure": primary.get("figure") or primary.get("figure_label"),
                "quoted_text": self._evidence_quote(primary),
                "evidence_ids": audit.evidence_ids,
                "bundle_fingerprint": result.bundle_fingerprint,
            }
            normalized_audits.append(
                {
                    "paper_id": result.paper_id,
                    "target_type": audit.target_type,
                    "target_id": audit.target_id,
                    "field_name": audit.field_name,
                    "decision": audit.decision,
                    "evidence_checked": audit.evidence_checked,
                    "evidence_location": evidence_location,
                    "supporting_evidence": [self._compact_evidence(item) for item in evidence_items[1:]],
                    "blocking_errors": audit.blocking_errors,
                    "recommended_action": audit.recommended_action,
                    "corrected_value": audit.corrected_value,
                    "confidence": audit.confidence,
                    "source": result.review_source.review_source_type,
                    "source_label": result.review_source.reviewer_label,
                    "model_name": result.review_source.reviewer_model,
                    "agent_role": "review_proposal_source",
                    "reason": audit.reason,
                    "writes_final_truth": False,
                    "human_confirmation_required": True,
                }
            )

        if not result.object_review_audits:
            warnings.append(
                {
                    "code": "no_object_review_audits",
                    "message": "The result contains no DFT row review or new-candidate proposal.",
                }
            )
        if result.overall_status == "completed":
            missing_targets = sorted(materials["target_dft_result_ids"] - reviewed_target_ids)
            if missing_targets:
                add_error(
                    "incomplete_candidate_coverage",
                    "overall_status=completed requires a review for every current main-paper DFT candidate; "
                    f"missing target ids: {', '.join(missing_targets)}",
                )
        if result.overall_status == "completed" and result.uncertainties:
            warnings.append(
                {
                    "code": "completed_with_uncertainties",
                    "message": "The proposal is marked completed but still contains uncertainties; local AI should inspect them.",
                }
            )

        import_request = None
        if not errors:
            review_notes = [
                {
                    "content": note,
                    "confidence": None,
                    "mapping_reason": "Imported from validated offline DFT review package",
                }
                for note in [*result.uncertainties, *result.notes]
            ]
            import_request = {
                "paper_id": result.paper_id,
                "source": result.review_source.review_source_type,
                "source_label": result.review_source.reviewer_label,
                "reviewer": result.review_source.reviewer_label,
                "auto_apply_review_rules": True,
                "raw_payload": {
                    "review_metadata": {
                        "schema_version": result.schema_version,
                        "bundle_fingerprint": result.bundle_fingerprint,
                        "paper_code": result.paper_code,
                        "overall_status": result.overall_status,
                        "review_source": result.review_source.model_dump(mode="json"),
                    },
                    "object_review_audits": normalized_audits,
                    "review_notes": review_notes,
                },
            }

        return {
            "valid": not errors,
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "errors": errors,
            "warnings": warnings,
            "validated_audit_count": len(normalized_audits) if not errors else 0,
            "import_analysis_request": import_request,
            "safety": {
                "writes_database": False,
                "writes_final_truth": False,
                "next_step": "Local AI reviews this response, then calls authenticated import_analysis with the returned request.",
            },
        }

    def _build_materials(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        if not str(paper.paper_code or "").strip():
            raise ValueError("paper_code_required_before_offline_review_export")

        source_papers = self._source_papers(paper)
        source_ids = [item["paper"].id for item in source_papers]
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id.in_(source_ids))).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(source_ids))).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(source_ids))).all()
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(source_ids))).all()
        dft_settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id.in_(source_ids))).all()
        samples = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id.in_(source_ids))).all()

        source_by_id = {item["paper"].id: item for item in source_papers}
        sample_by_id = {row.id: row for row in samples}
        dft_rows = sorted(dft_rows, key=lambda row: (row.paper_id != paper.id, str(row.id)))
        anchors = self._collect_anchors(dft_rows)

        text_snippets, text_map = self._text_snippets(
            source_by_id=source_by_id,
            dft_rows=dft_rows,
            sections=sections,
        )
        extracted_tables, table_map = self._tables(
            source_by_id=source_by_id,
            tables=tables,
            anchors=anchors,
        )
        extracted_figures, figure_map = self._figures(
            source_by_id=source_by_id,
            figures=figures,
            anchors=anchors,
        )
        evidence_map = {**text_map, **table_map, **figure_map}

        paper_metadata = {
            "paper_id": str(paper.id),
            "paper_code": str(paper.paper_code),
            "title": paper.title,
            "doi": paper.doi,
            "authors": paper.authors or [],
            "year": paper.year,
            "journal": paper.journal,
            "abstract": paper.abstract,
            "paper_type": paper.paper_type,
            "source_documents": [
                {
                    "source_document_type": item["source_document_type"],
                    "paper_id": str(item["paper"].id),
                    "paper_code": item["paper"].paper_code,
                    "title": item["paper"].title,
                    "relationship_id": item.get("relationship_id"),
                    "role": item["prefix"],
                }
                for item in source_papers
            ],
        }
        initial_dft_candidates = {
            "schema_version": "offline_initial_dft_candidates_v1",
            "writeback_paper_id": str(paper.id),
            "existing_candidates": [
                self._dft_row_payload(row, sample_by_id=sample_by_id, source_by_id=source_by_id)
                for row in dft_rows
                if row.paper_id == paper.id
            ],
            "supporting_si_candidates": [
                {
                    **self._dft_row_payload(row, sample_by_id=sample_by_id, source_by_id=source_by_id),
                    "review_instruction": (
                        "Use as supplementary evidence. If it is missing from the main writeback record, submit "
                        "decision=new_candidate with target_id=new; do not use this SI record id as a main target."
                    ),
                }
                for row in dft_rows
                if row.paper_id != paper.id
            ],
            "dft_settings": [
                self._dft_setting_payload(row, source_by_id=source_by_id)
                for row in sorted(dft_settings, key=lambda item: (item.paper_id != paper.id, str(item.id)))
            ],
        }

        fingerprint_payload = {
            "schema_version": OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "paper_metadata": paper_metadata,
            "initial_dft_candidates": initial_dft_candidates,
            "text_snippets": text_snippets,
            "extracted_tables": extracted_tables,
            "extracted_figures": [
                {key: value for key, value in item.items() if key != "bundle_file" and not key.startswith("_")}
                for item in extracted_figures
            ],
        }
        bundle_fingerprint = _sha256(_canonical_json_bytes(fingerprint_payload))
        warnings = []
        if not evidence_map:
            warnings.append("no_dft_relevant_evidence_found")
        if not initial_dft_candidates["existing_candidates"]:
            warnings.append("no_main_paper_dft_candidates")

        return {
            "paper_metadata": paper_metadata,
            "initial_dft_candidates": initial_dft_candidates,
            "text_snippets": text_snippets,
            "extracted_tables": extracted_tables,
            "extracted_figures": extracted_figures,
            "evidence_map": evidence_map,
            "target_dft_result_ids": {
                str(row.id) for row in dft_rows if row.paper_id == paper.id
            },
            "bundle_fingerprint": bundle_fingerprint,
            "warnings": warnings,
        }

    def _source_papers(self, paper: Paper) -> list[dict[str, Any]]:
        relationships = self.session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == paper.id,
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
            )
        ).all()
        sources = [
            {
                "paper": paper,
                "prefix": "main",
                "source_document_type": "main_text",
                "relationship_id": None,
            }
        ]
        seen = {paper.id}
        for relationship in sorted(relationships, key=lambda item: str(item.target_paper_id)):
            related = self.session.get(Paper, relationship.target_paper_id)
            if related is None or related.id in seen:
                continue
            seen.add(related.id)
            sources.append(
                {
                    "paper": related,
                    "prefix": "si",
                    "source_document_type": "supplementary_information",
                    "relationship_id": str(relationship.id),
                }
            )
        return sources

    def _text_snippets(
        self,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        dft_rows: list[DFTResult],
        sections: list[PaperSection],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        counters = {"main": 0, "si": 0}
        seen: set[tuple[str, str]] = set()

        def append_item(*, source: dict[str, Any], text: str, payload: dict[str, Any]) -> None:
            normalized = " ".join(str(text or "").split())
            if not normalized or len(items) >= MAX_TEXT_SNIPPETS:
                return
            digest = _sha256(normalized.encode("utf-8"))
            key = (source["prefix"], digest)
            if key in seen:
                return
            seen.add(key)
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:text:{counters[source['prefix']]:03d}"
            items.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_kind": "text",
                    "source_document_type": source["source_document_type"],
                    "source_paper_id": str(source["paper"].id),
                    "source_paper_code": source["paper"].paper_code,
                    "content_sha256": digest,
                    "text": normalized,
                    **payload,
                }
            )

        for row in dft_rows:
            source = source_by_id[row.paper_id]
            evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            location = evidence.get("source_location") if isinstance(evidence.get("source_location"), dict) else {}
            append_item(
                source=source,
                text=row.evidence_text or evidence.get("quoted_text") or "",
                payload={
                    "source_record_id": str(row.id),
                    "source_record_type": "dft_result_candidate_evidence",
                    "page": evidence.get("page") or location.get("page"),
                    "section": row.source_section or evidence.get("section") or location.get("section"),
                    "figure": row.source_figure or evidence.get("figure") or location.get("figure"),
                    "table": evidence.get("table") or location.get("table"),
                },
            )

        ordered_sections = sorted(
            sections,
            key=lambda row: (
                source_by_id[row.paper_id]["prefix"] != "main",
                row.page_start is None,
                row.page_start or 0,
                str(row.id),
            ),
        )
        for section in ordered_sections:
            title = section.section_title or section.section_type or ""
            combined = f"{title}\n{section.text or ''}"
            if not DFT_SIGNAL_RE.search(combined):
                continue
            excerpt = self._relevant_excerpt(section.text or combined)
            append_item(
                source=source_by_id[section.paper_id],
                text=excerpt,
                payload={
                    "source_record_id": str(section.id),
                    "source_record_type": "paper_section",
                    "section_title": title or None,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                },
            )

        return items, {item["evidence_id"]: item for item in items}

    def _tables(
        self,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        tables: list[PaperTable],
        anchors: dict[str, set[str]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        counters = {"main": 0, "si": 0}
        ordered = sorted(
            tables,
            key=lambda row: (
                source_by_id[row.paper_id]["prefix"] != "main",
                row.page is None,
                row.page or 0,
                str(row.id),
            ),
        )
        for row in ordered:
            haystack = f"{row.caption or ''}\n{row.markdown_content or ''}"
            if not self._is_relevant(haystack, anchors["table"]):
                continue
            source = source_by_id[row.paper_id]
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:table:{counters[source['prefix']]:03d}"
            item = {
                "evidence_id": evidence_id,
                "evidence_kind": "table",
                "source_document_type": source["source_document_type"],
                "source_paper_id": str(row.paper_id),
                "source_paper_code": source["paper"].paper_code,
                "source_record_id": str(row.id),
                "caption": row.caption,
                "page": row.page,
                "markdown_content": row.markdown_content,
                "content_sha256": _sha256(haystack.encode("utf-8")),
            }
            items.append(item)
        return items, {item["evidence_id"]: item for item in items}

    def _figures(
        self,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        figures: list[PaperFigure],
        anchors: dict[str, set[str]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        items: list[dict[str, Any]] = []
        counters = {"main": 0, "si": 0}
        ordered = sorted(
            figures,
            key=lambda row: (
                source_by_id[row.paper_id]["prefix"] != "main",
                row.page is None,
                row.page or 0,
                str(row.id),
            ),
        )
        for row in ordered:
            haystack = f"{row.figure_label or ''}\n{row.caption or ''}\n{row.content_summary or ''}"
            if not self._is_relevant(haystack, anchors["figure"]):
                continue
            source = source_by_id[row.paper_id]
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:fig:{counters[source['prefix']]:03d}"
            artifact = self._resolve_figure(row.image_path)
            artifact_hash = None
            artifact_size = None
            if artifact is not None:
                data = artifact.read_bytes()
                artifact_hash = _sha256(data)
                artifact_size = len(data)
            item = {
                "evidence_id": evidence_id,
                "evidence_kind": "figure",
                "source_document_type": source["source_document_type"],
                "source_paper_id": str(row.paper_id),
                "source_paper_code": source["paper"].paper_code,
                "source_record_id": str(row.id),
                "figure_label": row.figure_label,
                "caption": row.caption,
                "content_summary": row.content_summary,
                "page": row.page,
                "_artifact_reference": row.image_path,
                "image_available": artifact is not None,
                "image_sha256": artifact_hash,
                "image_size_bytes": artifact_size,
                "crop_status": row.crop_status,
                "content_sha256": _sha256(haystack.encode("utf-8")),
            }
            items.append(item)
        return items, {item["evidence_id"]: item for item in items}

    def _collect_anchors(self, rows: list[DFTResult]) -> dict[str, set[str]]:
        anchors = {"table": set(), "figure": set()}
        for row in rows:
            if row.source_figure:
                anchors["figure"].add(str(row.source_figure))
            evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            locations = [evidence]
            for key in ("source_location", "evidence_location"):
                if isinstance(evidence.get(key), dict):
                    locations.append(evidence[key])
            for location in locations:
                for key in ("table", "table_label"):
                    if location.get(key):
                        anchors["table"].add(str(location[key]))
                for key in ("figure", "figure_label"):
                    if location.get(key):
                        anchors["figure"].add(str(location[key]))
        return anchors

    @staticmethod
    def _is_relevant(text: str, anchors: set[str]) -> bool:
        if DFT_SIGNAL_RE.search(text or ""):
            return True
        normalized = " ".join((text or "").lower().split())
        return any(
            len(anchor.strip()) >= 3 and " ".join(anchor.lower().split()) in normalized
            for anchor in anchors
        )

    @staticmethod
    def _relevant_excerpt(text: str) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= MAX_TEXT_SNIPPET_CHARS:
            return normalized
        match = DFT_SIGNAL_RE.search(normalized)
        start = max(0, (match.start() if match else 0) - 800)
        end = min(len(normalized), start + MAX_TEXT_SNIPPET_CHARS)
        return normalized[start:end]

    def _resolve_figure(self, image_path: Any) -> Path | None:
        if not image_path:
            return None
        return resolve_persisted_artifact_path(
            str(image_path),
            category="figures",
            settings=self.settings,
            trusted_persisted_reference=True,
            must_exist=True,
        )

    @staticmethod
    def _dft_row_payload(
        row: DFTResult,
        *,
        sample_by_id: dict[UUID, CatalystSample],
        source_by_id: dict[UUID, dict[str, Any]],
    ) -> dict[str, Any]:
        sample = sample_by_id.get(row.catalyst_sample_id) if row.catalyst_sample_id else None
        source = source_by_id[row.paper_id]
        return {
            "target_id": str(row.id),
            "target_type": "dft_results",
            "source_document_type": source["source_document_type"],
            "source_paper_id": str(row.paper_id),
            "source_paper_code": source["paper"].paper_code,
            "catalyst_sample_id": str(row.catalyst_sample_id) if row.catalyst_sample_id else None,
            "material_identity": sample.name if sample else None,
            "adsorbate": row.adsorbate,
            "property_type": row.property_type,
            "value": row.value,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "reaction_type": row.reaction_type,
            "source_section": row.source_section,
            "source_figure": row.source_figure,
            "evidence_text": row.evidence_text,
            "confidence": row.confidence,
            "candidate_status": row.candidate_status,
            "evidence_payload": DFTReviewBundleService._sanitize_for_bundle(row.evidence_payload),
        }

    @staticmethod
    def _dft_setting_payload(row: DFTSetting, *, source_by_id: dict[UUID, dict[str, Any]]) -> dict[str, Any]:
        source = source_by_id[row.paper_id]
        return {
            "id": str(row.id),
            "source_document_type": source["source_document_type"],
            "source_paper_id": str(row.paper_id),
            "source_paper_code": source["paper"].paper_code,
            "software": row.software,
            "functional": row.functional,
            "dispersion_correction": row.dispersion_correction,
            "pseudopotential": row.pseudopotential,
            "cutoff_energy_ev": row.cutoff_energy_ev,
            "k_points": row.k_points,
            "convergence_settings": row.convergence_settings,
            "vacuum_thickness_a": row.vacuum_thickness_a,
            "raw_json": DFTReviewBundleService._sanitize_for_bundle(row.raw_json),
        }

    @staticmethod
    def _jsonl_bytes(items: list[dict[str, Any]]) -> bytes:
        if not items:
            return b""
        return ("\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in items) + "\n").encode(
            "utf-8"
        )

    @staticmethod
    def _public_figures(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in items
        ]

    @classmethod
    def _sanitize_for_bundle(cls, value: Any) -> Any:
        blocked_keys = {
            "path",
            "pdf_path",
            "image_path",
            "workspace_path",
            "markdown_path",
            "docling_json_path",
            "tei_path",
            "local_path",
            "server_path",
        }
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize_for_bundle(item)
                for key, item in value.items()
                if str(key).strip().lower() not in blocked_keys
            }
        if isinstance(value, list):
            return [cls._sanitize_for_bundle(item) for item in value]
        return value

    @staticmethod
    def _table_markdown(table: dict[str, Any]) -> str:
        return (
            f"# {table['evidence_id']}\n\n"
            f"- source_document_type: {table['source_document_type']}\n"
            f"- source_paper_code: {table.get('source_paper_code') or '-'}\n"
            f"- page: {table.get('page') or '-'}\n"
            f"- caption: {table.get('caption') or '-'}\n\n"
            f"{table.get('markdown_content') or ''}\n"
        )

    @staticmethod
    def _return_template(materials: dict[str, Any]) -> dict[str, Any]:
        metadata = materials["paper_metadata"]
        return {
            "schema_version": "offline_dft_review_result_v1",
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "review_source": {
                "review_source_type": "web_ai",
                "reviewer_label": "user-provided AI",
                "reviewer_model": None,
                "tool_capabilities": ["none"],
            },
            "overall_status": "uncertain",
            "object_review_audits": [],
            "uncertainties": [],
            "notes": [],
        }

    @staticmethod
    def _instructions(materials: dict[str, Any]) -> str:
        metadata = materials["paper_metadata"]
        return f"""# Literature AI 离线 DFT 核验任务

目标文献：`{metadata['paper_code']}`（paper_id=`{metadata['paper_id']}`）

你是审核建议来源，不是数据库执行者。你没有 MCP、数据库、服务器或外部检索工具，只能使用本压缩包中的材料。

## 必须遵守

1. 只核验当前这一篇主文献及包内相关支撑信息（SI）的 DFT 数据、计算参数、图表和文字证据。
2. 不得猜测。证据不足、材料身份不清或来源冲突时，使用 `NEEDS_HUMAN`，并写入 `uncertainties`。
3. 每条 `object_review_audits` 都必须引用一个或多个真实 `evidence_ids`。证据编号来自 `manifest.json` 和 `evidence/`。
4. 已有主文献 DFT candidate 使用 `PASS`、`REVISE`、`REJECT` 或 `NEEDS_HUMAN`。
5. 只有 `manifest.json` 的 `target_dft_result_ids` 中每个已有候选都提交了审核结果时，才能使用 `overall_status="completed"`；未覆盖全部已有候选时，使用 `uncertain` 或 `needs_human`。
6. 发现漏项时使用 `decision="new_candidate"`、`target_type="dft_results"`、`target_id="new"`、`field_name="dft_results"`；`corrected_value` 至少包含 `material_identity`、`property_type`、`value`、`unit`。
7. SI 是主文献的证据来源，不是独立审核任务。SI 中发现的漏项仍写回当前主文献，使用 `new_candidate`。
8. 不得声称已写数据库、已入库、已人工确认、已 verified 或已成为 ML_Ready。
9. 严格按 `return_schema.json` 输出一个 JSON 对象；不要修改 schema，不要用自由散文替代 JSON，也不要包 Markdown 代码块。
10. 保留 `return_template.json` 中的 `bundle_fingerprint`、`paper_id`、`paper_code` 原值。

## 材料顺序

先读 `manifest.json`、`parsed/initial_dft_candidates.json`，再读 `evidence/text_snippets.jsonl`、相关表格和图片。`parsed/extracted_*.json` 提供证据编号与来源映射。

最终只输出符合 `return_schema.json` 的 JSON。
"""

    @staticmethod
    def _evidence_quote(item: dict[str, Any]) -> str | None:
        value = item.get("text") or item.get("caption") or item.get("markdown_content") or item.get("content_summary")
        if value is None:
            return None
        return str(value)[:2000]

    @classmethod
    def _compact_evidence(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": item.get("evidence_id"),
            "source_document_type": item.get("source_document_type"),
            "page": item.get("page") or item.get("page_start"),
            "section": item.get("section") or item.get("section_title"),
            "table": item.get("caption") if item.get("evidence_kind") == "table" else item.get("table"),
            "figure": item.get("figure_label") or item.get("figure"),
            "quoted_text": cls._evidence_quote(item),
        }
