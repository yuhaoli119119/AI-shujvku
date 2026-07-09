from __future__ import annotations

from copy import deepcopy
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
    AuditLog,
    CatalystSample,
    DFTResult,
    DFTSetting,
    EvidenceLocator,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.schemas.dft_review_bundle import OfflineDFTReviewResult
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService, pending_needs_human_actions_from_review_payload
from app.services.figure_table_snapshot_service import compute_figure_table_snapshot
from app.services.figure_rag_quality import build_figure_rag_quality_summary
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.evidence_anchors import first_pdf_evidence_anchor, has_pdf_evidence_anchor
from app.utils.review_safety import bulk_export_gate_results


OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION = "offline_dft_review_bundle_v1"
MAX_TEXT_SNIPPETS = 100
MAX_TEXT_SNIPPET_CHARS = 6000
MAX_FIGURE_FILES = 24
MAX_TOTAL_FIGURE_BYTES = 24 * 1024 * 1024
SINGLE_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")

DFT_SIGNAL_RE = re.compile(
    r"(?:\bDFT\b|density\s+functional|first[-\s]?principles?|computational\s+details?|"
    r"calculation\s+method|adsorption\s+energ|binding\s+energ|free\s+energ|gibbs|"
    r"delta\s*g|Δ\s*G|reaction\s+barrier|activation\s+energ|dissociation\s+energ|"
    r"binding\s+strength|Li2S\w*|Li2S\s*n|polysulfide|"
    r"bader|charge\s+transfer|electronic\s+structure|density\s+of\s+states|\bDOS\b|"
    r"orbital\s+occupanc|d[-\s]?orbital|"
    r"\bVASP\b|quantum\s+espresso|\bCASTEP\b|\bDMol|pseudopotential|k[-\s]?point|"
    r"plane[-\s]?wave|cutoff\s+energ|\bPBE\b|\bHSE\d*\b|van\s+der\s+waals|\bvdW\b)",
    re.IGNORECASE,
)

ALLOWED_DFT_REVIEW_FIELDS = {
    "dft_results",
    "property_type",
    "energy_type",
    "value",
    "value_upper",
    "value_kind",
    "value_type",
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

FIGURE_TABLE_REVIEW_READY_STATUSES = {"completed", "not_required"}
LOCAL_AI_REQUIRED_TOOLS = ("get_codex_item", "read_paper_page")


class FigureTableReviewNotCompletedError(ValueError):
    """Raised when DFT review is attempted before the figure/table stage is stable."""

    code = "figure_table_review_not_completed"

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        super().__init__(self.code)

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": "Figure/table review must be completed or not_required, and figure RAG quality must be ready, before DFT review.",
            "figure_table_review": self.state,
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

    def get_figure_table_review_state(self, paper_id: UUID) -> dict[str, Any]:
        materials = self._build_materials(paper_id, enforce_figure_table_gate=False)
        return dict(materials["curated_evidence_snapshot"]["review_gate"])

    @classmethod
    def ensure_figure_table_review_ready(
        cls,
        state: dict[str, Any],
        *,
        expected_completed_snapshot_fingerprint: str | None = None,
    ) -> None:
        status = str(state.get("stage_status") or "").strip()
        current_fingerprint = str(state.get("current_snapshot_fingerprint") or "").strip()
        completed_fingerprint = str(state.get("completed_snapshot_fingerprint") or "").strip()
        if status not in FIGURE_TABLE_REVIEW_READY_STATUSES:
            raise FigureTableReviewNotCompletedError(state)
        rag_quality = state.get("rag_quality") if isinstance(state.get("rag_quality"), dict) else {}
        figure_quality = rag_quality.get("figures") if isinstance(rag_quality.get("figures"), dict) else {}
        rag_quality_status = str(state.get("rag_quality_status") or figure_quality.get("status") or "ready").strip()
        if rag_quality_status and rag_quality_status != "ready":
            raise FigureTableReviewNotCompletedError(state)
        if int(figure_quality.get("blocked") or 0) > 0:
            raise FigureTableReviewNotCompletedError(state)
        if not current_fingerprint or not completed_fingerprint:
            raise FigureTableReviewNotCompletedError({**state, "stage_status": "stale"})
        if completed_fingerprint != current_fingerprint:
            raise FigureTableReviewNotCompletedError({**state, "stage_status": "stale"})
        if (
            expected_completed_snapshot_fingerprint
            and expected_completed_snapshot_fingerprint != completed_fingerprint
        ):
            raise FigureTableReviewNotCompletedError(
                {
                    **state,
                    "stage_status": "stale",
                    "expected_completed_snapshot_fingerprint": expected_completed_snapshot_fingerprint,
                }
            )

    def build_zip(self, paper_id: UUID, *, include_figure_files: bool = True) -> dict[str, Any]:
        materials = self._build_materials(paper_id)
        files: dict[str, bytes] = {
            "parsed/paper_metadata.json": _json_bytes(materials["paper_metadata"]),
            "parsed/initial_dft_candidates.json": _json_bytes(materials["initial_dft_candidates"]),
            "parsed/dft_review_checklist.json": _json_bytes(self._dft_review_checklist(materials)),
            "parsed/extracted_tables.json": _json_bytes(materials["extracted_tables"]),
            "parsed/extracted_figures.json": _json_bytes(self._public_figures(materials["extracted_figures"])),
            "parsed/curated_figure_table_evidence_snapshot.json": _json_bytes(materials["curated_evidence_snapshot"]),
            "evidence/text_snippets.jsonl": self._jsonl_bytes(materials["text_snippets"]),
            "format_examples.json": _json_bytes(self._format_examples()),
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
            "figure_table_evidence_review_status": materials["curated_evidence_snapshot"]["evidence_review_status"],
            "figure_table_review_status": materials["curated_evidence_snapshot"]["stage_status"],
            "figure_table_completed_snapshot_fingerprint": materials["curated_evidence_snapshot"][
                "completed_snapshot_fingerprint"
            ],
            "figure_table_evidence_snapshot_fingerprint": materials["curated_evidence_snapshot"]["snapshot_fingerprint"],
            "writeback_paper_id": materials["paper_metadata"]["paper_id"],
            "evidence_ids": sorted(materials["evidence_map"]),
            "target_dft_result_ids": sorted(materials["target_dft_result_ids"]),
            "expected_dft_review_coverage": {
                "target_ids": sorted(materials["target_dft_result_ids"]),
                "required_decisions_for_existing_targets": ["PASS", "REVISE", "REJECT", "NEEDS_HUMAN"],
                "completion_rule": (
                    "Every target_dft_result_id must appear exactly once in object_review_audits before "
                    "the server will generate an import_analysis_request."
                ),
            },
            "counts": {
                "main_dft_candidates": len(materials["initial_dft_candidates"]["existing_candidates"]),
                "supporting_si_dft_candidates": len(
                    materials["initial_dft_candidates"]["supporting_si_candidates"]
                ),
                "excluded_terminal_main_dft_candidates": materials["excluded_terminal_main_dft_candidates"],
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
        raw_payload, normalization_warnings = self._normalize_common_web_ai_result_json(raw_payload)
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
                "warnings": normalization_warnings,
                "import_analysis_request": None,
            }

        materials = self._build_materials(paper_id)
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = list(normalization_warnings)
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
        figure_table_gate = materials["curated_evidence_snapshot"]["review_gate"]
        expected_completed_snapshot = figure_table_gate.get("completed_snapshot_fingerprint")
        if result.figure_table_completed_snapshot_fingerprint != expected_completed_snapshot:
            add_error(
                "stale_figure_table_review_snapshot",
                "figure_table_completed_snapshot_fingerprint differs from the current completed figure/table snapshot",
            )

        normalized_audits: list[dict[str, Any]] = []
        reviewed_target_ids: set[str] = set()
        new_candidate_count = 0
        seen_target_fields: dict[tuple[str, str], str] = {}
        for index, audit in enumerate(result.object_review_audits):
            target_field_key = (audit.target_id, audit.field_name)
            previous_decision = seen_target_fields.get(target_field_key)
            if previous_decision is not None:
                error_code = (
                    "conflicting_target_field_review"
                    if previous_decision != audit.decision
                    else "duplicate_target_field_review"
                )
                add_error(
                    error_code,
                    "Each target_id + field_name may appear only once in a DFT review result",
                    audit_index=index,
                )
            else:
                seen_target_fields[target_field_key] = audit.decision
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
            else:
                new_candidate_count += 1
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
            for error in self._validate_dft_audit_payload(
                audit=audit.model_dump(mode="json"),
                evidence_items=evidence_items,
                materials=materials,
            ):
                add_error(error["code"], error["message"], audit_index=index)
            primary = self._best_pdf_evidence_item(evidence_items) or evidence_items[0]
            quote_item = self._best_quote_evidence_item(evidence_items) or primary
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
                "bbox": primary.get("bbox"),
                "locator_id": primary.get("locator_id"),
                "locator_status": primary.get("locator_status"),
                "locator_confidence": primary.get("locator_confidence"),
                "quoted_text": self._evidence_quote(quote_item),
                "evidence_ids": audit.evidence_ids,
                "bundle_fingerprint": result.bundle_fingerprint,
                "figure_table_completed_snapshot_fingerprint": result.figure_table_completed_snapshot_fingerprint,
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
                    "agent_role": "web_ai_review_suggestion",
                    "requires_local_ai_verification": True,
                    "local_ai_verification": {
                        "verified_against_pdf": False,
                        "required_tools": list(LOCAL_AI_REQUIRED_TOOLS),
                        "instruction": (
                            "Local AI must call get_codex_item for this target and read_paper_page for each cited "
                            "page/evidence before changing verified_against_pdf to true."
                        ),
                    },
                    "reason": audit.reason,
                    "writes_final_truth": False,
                    "confirmation_required": True,
                }
            )

        if not result.object_review_audits:
            warnings.append(
                {
                    "code": "no_object_review_audits",
                    "message": "The result contains no DFT row review or new-candidate proposal.",
                }
            )
        missing_targets = sorted(materials["target_dft_result_ids"] - reviewed_target_ids)
        coverage = {
            "expected_target_ids": sorted(materials["target_dft_result_ids"]),
            "reviewed_target_ids": sorted(reviewed_target_ids),
            "missing_target_ids": missing_targets,
            "expected_count": len(materials["target_dft_result_ids"]),
            "reviewed_existing_count": len(reviewed_target_ids),
            "new_candidate_count": new_candidate_count,
            "coverage_complete": not missing_targets,
        }
        if missing_targets:
            add_error(
                "incomplete_candidate_coverage",
                "DFT review requires PASS, REVISE, REJECT, or NEEDS_HUMAN for every current main-paper "
                "DFT candidate before an import request can be generated; missing target ids: "
                + ", ".join(missing_targets),
            )
        coverage_ack = result.coverage_acknowledgement
        if coverage_ack is not None:
            ack_expected = set(coverage_ack.expected_target_ids)
            expected = set(materials["target_dft_result_ids"])
            if ack_expected and ack_expected != expected:
                add_error(
                    "coverage_acknowledgement_mismatch",
                    "coverage_acknowledgement.expected_target_ids differs from the current DFT review target set",
                )
            ack_reviewed = set(coverage_ack.reviewed_target_ids)
            if ack_reviewed and ack_reviewed != reviewed_target_ids:
                warnings.append(
                    {
                        "code": "coverage_acknowledgement_ignored",
                        "message": (
                            "coverage_acknowledgement.reviewed_target_ids differs from object_review_audits; "
                            "server-derived coverage is used."
                        ),
                    }
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
                "source": "local_ai",
                "source_label": "local_ai_after_pdf_evidence_check",
                "reviewer": "local_ai",
                "auto_apply_review_rules": True,
                "raw_payload": {
                    "review_metadata": {
                        "schema_version": result.schema_version,
                        "bundle_fingerprint": result.bundle_fingerprint,
                        "figure_table_completed_snapshot_fingerprint": (
                            result.figure_table_completed_snapshot_fingerprint
                        ),
                        "paper_code": result.paper_code,
                        "overall_status": result.overall_status,
                        "web_ai_review_source": result.review_source.model_dump(mode="json"),
                        "local_ai_verification_required": True,
                        "required_local_ai_tools": list(LOCAL_AI_REQUIRED_TOOLS),
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
            "coverage": coverage,
            "validated_audit_count": len(normalized_audits) if not errors else 0,
            "import_analysis_request": import_request,
            "safety": {
                "writes_database": False,
                "writes_final_truth": False,
                "next_step": (
                    "Local AI must verify each object_review_audit with get_codex_item and read_paper_page, "
                    "set local_ai_verification.verified_against_pdf=true with the used tools, then call authenticated "
                    "import_analysis with the returned request."
                ),
            },
            "local_ai_writeback_contract": self._local_ai_writeback_contract(),
        }

    def _build_materials(self, paper_id: UUID, *, enforce_figure_table_gate: bool = True) -> dict[str, Any]:
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
        review_dft_rows, excluded_terminal_main_count = self._dft_rows_for_review_bundle(dft_rows, main_paper_id=paper.id)
        locator_by_target = self._dft_locator_payloads_by_target(review_dft_rows)
        anchors = self._collect_anchors(review_dft_rows)

        text_snippets, text_map = self._text_snippets(
            source_by_id=source_by_id,
            dft_rows=review_dft_rows,
            sections=sections,
            locator_by_target=locator_by_target,
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
        figure_by_id = {str(row.id): row for row in figures}
        evidence_figure_ids = {
            str(item.get("source_record_id"))
            for item in extracted_figures
            if item.get("source_record_id")
        }
        figure_rag_quality = build_figure_rag_quality_summary(
            self.session,
            [row for figure_id, row in figure_by_id.items() if figure_id in evidence_figure_ids],
        )
        evidence_map = {**text_map, **table_map, **figure_map}

        curated_evidence_snapshot = self._curated_evidence_snapshot(
            paper=paper,
            extracted_tables=extracted_tables,
            extracted_figures=extracted_figures,
            figure_rag_quality=figure_rag_quality,
        )
        if enforce_figure_table_gate:
            self.ensure_figure_table_review_ready(curated_evidence_snapshot["review_gate"])

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
                self._dft_row_payload(
                    row,
                    sample_by_id=sample_by_id,
                    source_by_id=source_by_id,
                    locator_by_target=locator_by_target,
                )
                for row in review_dft_rows
                if row.paper_id == paper.id
            ],
            "supporting_si_candidates": [
                {
                    **self._dft_row_payload(
                        row,
                        sample_by_id=sample_by_id,
                        source_by_id=source_by_id,
                        locator_by_target=locator_by_target,
                    ),
                    "review_instruction": (
                        "Use as supplementary evidence. If it is missing from the main writeback record, submit "
                        "decision=new_candidate with target_id=new; do not use this SI record id as a main target."
                    ),
                }
                for row in dft_rows
                if row.paper_id != paper.id and row in review_dft_rows
            ],
            "dft_settings": [
                self._dft_setting_payload(row, source_by_id=source_by_id)
                for row in sorted(dft_settings, key=lambda item: (item.paper_id != paper.id, str(item.id)))
            ],
        }
        dft_row_payloads_by_id = {
            item["target_id"]: item
            for item in initial_dft_candidates["existing_candidates"]
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
            "curated_evidence_snapshot": curated_evidence_snapshot,
        }
        bundle_fingerprint = _sha256(_canonical_json_bytes(fingerprint_payload))
        warnings = []
        if not evidence_map:
            warnings.append("no_dft_relevant_evidence_found")
        if not initial_dft_candidates["existing_candidates"]:
            warnings.append("no_main_paper_dft_candidates")
        if curated_evidence_snapshot["evidence_review_status"] != "applied":
            warnings.append("figure_table_evidence_not_yet_reviewed")

        return {
            "paper_metadata": paper_metadata,
            "initial_dft_candidates": initial_dft_candidates,
            "text_snippets": text_snippets,
            "extracted_tables": extracted_tables,
            "extracted_figures": extracted_figures,
            "curated_evidence_snapshot": curated_evidence_snapshot,
            "evidence_map": evidence_map,
            "target_dft_result_ids": {
                str(row.id) for row in review_dft_rows if row.paper_id == paper.id
            },
            "dft_row_payloads_by_id": dft_row_payloads_by_id,
            "bundle_fingerprint": bundle_fingerprint,
            "warnings": warnings,
            "excluded_terminal_main_dft_candidates": excluded_terminal_main_count,
        }

    def _dft_rows_for_review_bundle(
        self,
        rows: list[DFTResult],
        *,
        main_paper_id: UUID,
    ) -> tuple[list[DFTResult], int]:
        if not rows:
            return [], 0
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        selected: list[DFTResult] = []
        excluded_terminal_main = 0
        for row in rows:
            status = str(row.candidate_status or "").strip().lower()
            gate = gate_by_id.get(str(row.id))
            review_status = str(getattr(gate, "review_status", "") or "").strip().lower()
            is_rejected = status == "rejected" or "rejected" in review_status
            is_currently_exportable_ml_ready = status == "ml_ready" and bool(getattr(gate, "eligible", False))
            should_skip = is_rejected or is_currently_exportable_ml_ready
            if row.paper_id == main_paper_id and should_skip:
                excluded_terminal_main += 1
                continue
            if row.paper_id != main_paper_id and should_skip:
                continue
            selected.append(row)
        return selected, excluded_terminal_main

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
        locator_by_target: dict[str, list[dict[str, Any]]],
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
            primary_locator = self._primary_locator(locator_by_target.get(str(row.id), []))
            append_item(
                source=source,
                text=row.evidence_text or evidence.get("quoted_text") or (primary_locator or {}).get("evidence_text") or "",
                payload={
                    "source_record_id": str(row.id),
                    "source_record_type": "dft_result_candidate_evidence",
                    "page": evidence.get("page") or location.get("page") or (primary_locator or {}).get("page"),
                    "section": row.source_section or evidence.get("section") or location.get("section") or (primary_locator or {}).get("section"),
                    "figure": row.source_figure or evidence.get("figure") or location.get("figure"),
                    "table": evidence.get("table") or location.get("table"),
                    "bbox": (primary_locator or {}).get("bbox"),
                    "locator_id": (primary_locator or {}).get("id"),
                    "locator_status": (primary_locator or {}).get("locator_status"),
                    "locator_confidence": (primary_locator or {}).get("locator_confidence"),
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
        locator_by_target: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        sample = sample_by_id.get(row.catalyst_sample_id) if row.catalyst_sample_id else None
        source = source_by_id[row.paper_id]
        locators = list((locator_by_target or {}).get(str(row.id), []))
        primary_locator = DFTReviewBundleService._primary_locator(locators)
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
            "value_upper": row.value_upper,
            "value_kind": row.value_kind,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "reaction_type": row.reaction_type,
            "source_section": row.source_section,
            "source_figure": row.source_figure,
            "evidence_text": row.evidence_text,
            "confidence": row.confidence,
            "candidate_status": row.candidate_status,
            "evidence_payload": DFTReviewBundleService._sanitize_for_bundle(row.evidence_payload),
            "primary_evidence_locator": primary_locator,
            "evidence_locators": locators,
        }

    def _dft_locator_payloads_by_target(self, rows: list[DFTResult]) -> dict[str, list[dict[str, Any]]]:
        if not rows:
            return {}
        target_ids = {str(row.id) for row in rows}
        locators_by_target: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in target_ids}
        locators = self.session.scalars(
            select(EvidenceLocator)
            .where(
                EvidenceLocator.paper_id.in_({row.paper_id for row in rows}),
                EvidenceLocator.target_id.in_(target_ids),
                EvidenceLocator.target_type.in_(("dft_results", "dft_result", "DFTResult")),
            )
            .order_by(
                EvidenceLocator.target_id.asc(),
                EvidenceLocator.page.asc().nulls_last(),
                EvidenceLocator.created_at.asc(),
            )
        ).all()
        for locator in locators:
            target_id = str(locator.target_id)
            if len(locators_by_target.setdefault(target_id, [])) >= 5:
                continue
            locators_by_target[target_id].append(self._locator_payload(locator))
        return locators_by_target

    @staticmethod
    def _primary_locator(locators: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not locators:
            return None
        exact = [
            item
            for item in locators
            if str(item.get("locator_status") or "").strip() == "exact_page" and item.get("page") is not None
        ]
        if exact:
            return exact[0]
        with_page = [item for item in locators if item.get("page") is not None]
        if with_page:
            return with_page[0]
        return locators[0]

    @staticmethod
    def _locator_payload(locator: EvidenceLocator) -> dict[str, Any]:
        return {
            "id": str(locator.id),
            "page": locator.page,
            "source_type": locator.source_type,
            "section": locator.section,
            "figure_id": str(locator.figure_id) if locator.figure_id else None,
            "table_id": str(locator.table_id) if locator.table_id else None,
            "field_name": locator.field_name,
            "locator_status": locator.locator_status,
            "locator_confidence": locator.locator_confidence,
            "parser_source": locator.parser_source,
            "evidence_text": locator.evidence_text,
            "bbox": locator.bbox,
            "warning_reason": locator.warning_reason,
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



    def _curated_evidence_snapshot(
        self,
        *,
        paper: Paper,
        extracted_tables: list[dict[str, Any]],
        extracted_figures: list[dict[str, Any]],
        figure_rag_quality: dict[str, Any],
    ) -> dict[str, Any]:
        public_figures = self._public_figures(extracted_figures)
        content_snapshot = compute_figure_table_snapshot(self.session, paper.id)
        chart_task = EvidenceReviewBundleService(self.session, self.settings).get_review_task(paper.id)
        current_snapshot_fingerprint = str(
            chart_task.get("current_snapshot_fingerprint") or content_snapshot["fingerprint"]
        )
        latest = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == paper.id)
            .where(AuditLog.action == "offline_evidence_review_applied")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        ).first()
        payload = latest.payload if latest is not None and isinstance(latest.payload, dict) else {}
        completed_snapshot_fingerprint = (
            str(chart_task.get("completed_snapshot_fingerprint") or payload.get("completed_snapshot_fingerprint") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if latest is None and not content_snapshot["tables"] and not content_snapshot["figures"]:
            stage_status = "not_required"
            completed_snapshot_fingerprint = current_snapshot_fingerprint
        elif latest is None:
            stage_status = str(chart_task.get("stage_status") or "not_started")
        else:
            stage_status = str(chart_task.get("stage_status") or payload.get("stage_status") or "").strip() or (
                "completed" if completed_snapshot_fingerprint else "incomplete"
            )
            if (
                stage_status == "completed"
                and completed_snapshot_fingerprint != current_snapshot_fingerprint
            ):
                stage_status = "stale"
        chart_rag_quality = (
            chart_task.get("rag_quality")
            if isinstance(chart_task.get("rag_quality"), dict)
            else {"figures": figure_rag_quality}
        )
        chart_figure_quality = (
            chart_rag_quality.get("figures")
            if isinstance(chart_rag_quality.get("figures"), dict)
            else figure_rag_quality
        )
        rag_quality_status = str(chart_task.get("rag_quality_status") or chart_figure_quality.get("status") or "ready")
        quality_blocking_errors: list[dict[str, Any]] = list(chart_task.get("blocking_errors") or [])
        needs_human_pending = pending_needs_human_actions_from_review_payload(payload)
        chart_scope_completion = chart_task.get("scope_completion") if isinstance(chart_task.get("scope_completion"), dict) else {}
        effective_needs_human_pending = [] if chart_scope_completion.get("complete") else needs_human_pending
        if stage_status == "completed" and effective_needs_human_pending:
            stage_status = "needs_local_ai"
            quality_blocking_errors.append(
                {
                    "code": "needs_human_pending",
                    "message": "Figure/table review still contains NEEDS_HUMAN actions that must be resolved before DFT export.",
                    "blocked_count": len(effective_needs_human_pending),
                    "blocked_items": effective_needs_human_pending[:50],
                }
            )
        if stage_status == "completed" and rag_quality_status != "ready":
            stage_status = "needs_local_ai"
            quality_blocking_errors.append(
                {
                    "code": "figure_rag_quality_incomplete",
                    "message": "Figure review action coverage is completed, but one or more current figures are not RAG-ready.",
                    "blocked_count": chart_figure_quality.get("blocked"),
                    "blocked_reasons": chart_figure_quality.get("blocked_reasons") or {},
                    "blocked_items": (chart_figure_quality.get("blocked_items") or [])[:50],
                }
            )
        snapshot = {
            "schema_version": "curated_figure_table_evidence_snapshot_v1",
            "evidence_review_status": (
                "applied" if stage_status in FIGURE_TABLE_REVIEW_READY_STATUSES else "not_recorded"
            ),
            "stage_status": stage_status,
            "rag_quality_status": rag_quality_status,
            "rag_quality": {"figures": chart_figure_quality},
            "blocking_errors": quality_blocking_errors,
            "current_snapshot_fingerprint": current_snapshot_fingerprint,
            "completed_snapshot_fingerprint": completed_snapshot_fingerprint or None,
            "review_run_id": str(latest.id) if latest is not None else None,
            "reviewed_at": latest.created_at.isoformat() if latest is not None and latest.created_at else None,
            "review_source": payload.get("review_source") if isinstance(payload, dict) else None,
            "stage1_bundle_fingerprint": payload.get("bundle_fingerprint") if isinstance(payload, dict) else None,
            "post_apply_bundle_fingerprint": payload.get("post_apply_bundle_fingerprint") if isinstance(payload, dict) else None,
            "applied_count": len(payload.get("applied") or []) if isinstance(payload, dict) else 0,
            "skipped_count": len(payload.get("skipped") or []) if isinstance(payload, dict) else 0,
            "unresolved_count": max(
                int(chart_task.get("unresolved_count") or 0),
                (len(payload.get("unresolved_actions") or []) + len(effective_needs_human_pending)) if isinstance(payload, dict) else 0,
            ),
            "dft_evidence_candidates": payload.get("dft_evidence_candidates") if isinstance(payload, dict) else [],
            "tables": extracted_tables,
            "figures": public_figures,
            "content_snapshot_schema_version": content_snapshot["schema_version"],
        }
        snapshot["review_gate"] = {
            "stage_status": stage_status,
            "allowed_statuses": sorted(FIGURE_TABLE_REVIEW_READY_STATUSES),
            "rag_quality_status": rag_quality_status,
            "rag_quality": {"figures": chart_figure_quality},
            "blocking_errors": quality_blocking_errors,
            "current_snapshot_fingerprint": current_snapshot_fingerprint,
            "completed_snapshot_fingerprint": completed_snapshot_fingerprint or None,
            "review_run_id": snapshot["review_run_id"],
            "reviewed_at": snapshot["reviewed_at"],
        }
        snapshot["snapshot_fingerprint"] = current_snapshot_fingerprint
        return snapshot

    @staticmethod
    def _normalize_common_web_ai_result_json(raw_payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(raw_payload, dict):
            return raw_payload, []
        payload = deepcopy(raw_payload)
        audits = payload.get("object_review_audits")
        if not isinstance(audits, list):
            return payload, []

        warnings: list[dict[str, Any]] = []
        normalized_audits: list[Any] = []
        seen: dict[tuple[str, str, str], int] = {}
        converted_numeric_values = 0
        deduped_audits = 0
        for audit in audits:
            if not isinstance(audit, dict):
                normalized_audits.append(audit)
                continue
            audit = deepcopy(audit)
            converted_numeric_values += DFTReviewBundleService._normalize_corrected_numeric_values(audit)
            key = (
                str(audit.get("target_type") or "dft_results").strip(),
                str(audit.get("target_id") or "").strip(),
                str(audit.get("field_name") or "dft_results").strip(),
            )
            if not key[1]:
                normalized_audits.append(audit)
                continue
            previous_index = seen.get(key)
            if previous_index is None:
                seen[key] = len(normalized_audits)
                normalized_audits.append(audit)
                continue
            merged = DFTReviewBundleService._merge_duplicate_web_ai_audit(normalized_audits[previous_index], audit)
            if merged is None:
                normalized_audits.append(audit)
                continue
            normalized_audits[previous_index] = merged
            deduped_audits += 1

        payload["object_review_audits"] = normalized_audits
        if converted_numeric_values:
            warnings.append(
                {
                    "code": "normalized_dft_numeric_string",
                    "message": "Converted unambiguous corrected_value numeric strings before validation.",
                    "count": converted_numeric_values,
                }
            )
        if deduped_audits:
            warnings.append(
                {
                    "code": "normalized_duplicate_object_review_audit",
                    "message": "Merged duplicate web-AI DFT review entries with the same decision and corrected value.",
                    "count": deduped_audits,
                }
            )
        return payload, warnings

    @staticmethod
    def _normalize_corrected_numeric_values(audit: dict[str, Any]) -> int:
        corrected = audit.get("corrected_value")
        if not isinstance(corrected, dict):
            return 0
        converted = 0
        for key in ("value", "value_upper"):
            parsed = DFTReviewBundleService._parse_unambiguous_number(corrected.get(key))
            if parsed is None:
                continue
            corrected[key] = parsed
            converted += 1
        return converted

    @staticmethod
    def _parse_unambiguous_number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            return float(value)
        if not isinstance(value, str):
            return None
        text = (
            value.strip()
            .replace("\u2212", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\uff0b", "+")
        )
        if not text:
            return None
        matches = list(SINGLE_NUMBER_RE.finditer(text))
        if len(matches) != 1:
            return None
        match = matches[0]
        prefix = text[: match.start()].strip()
        suffix = text[match.end() :].strip()
        if prefix and not re.fullmatch(r"(?:[~≈≃∼]|ca\.?|about|around|\(|=)+", prefix, flags=re.IGNORECASE):
            return None
        if suffix and not re.fullmatch(r"[A-Za-zµμΩÅ°/%·^_{}().\s-]+", suffix):
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def _merge_duplicate_web_ai_audit(existing: Any, duplicate: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(existing, dict):
            return None
        if str(existing.get("decision") or "") != str(duplicate.get("decision") or ""):
            return None
        if bool(existing.get("evidence_checked")) != bool(duplicate.get("evidence_checked")):
            return None
        if DFTReviewBundleService._canonical_review_value(existing.get("corrected_value")) != (
            DFTReviewBundleService._canonical_review_value(duplicate.get("corrected_value"))
        ):
            return None
        existing_action = str(existing.get("recommended_action") or "").strip()
        duplicate_action = str(duplicate.get("recommended_action") or "").strip()
        if existing_action and duplicate_action and existing_action != duplicate_action:
            return None

        merged = deepcopy(existing)
        merged["evidence_ids"] = DFTReviewBundleService._merge_string_lists(
            existing.get("evidence_ids"),
            duplicate.get("evidence_ids"),
        )
        merged["blocking_errors"] = DFTReviewBundleService._merge_string_lists(
            existing.get("blocking_errors"),
            duplicate.get("blocking_errors"),
        )
        if not merged.get("recommended_action") and duplicate_action:
            merged["recommended_action"] = duplicate_action
        merged["reason"] = DFTReviewBundleService._merge_reason(existing.get("reason"), duplicate.get("reason"))
        merged["confidence"] = DFTReviewBundleService._merge_confidence(
            existing.get("confidence"),
            duplicate.get("confidence"),
        )
        return merged

    @staticmethod
    def _canonical_review_value(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _merge_string_lists(*values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            if not isinstance(value, list):
                continue
            for item in value:
                text = str(item).strip()
                if text and text not in merged:
                    merged.append(text)
        return merged

    @staticmethod
    def _merge_reason(first: Any, second: Any) -> str:
        first_text = str(first or "").strip()
        second_text = str(second or "").strip()
        if not first_text:
            return second_text
        if not second_text or second_text == first_text:
            return first_text
        return f"{first_text} / {second_text}"

    @staticmethod
    def _merge_confidence(first: Any, second: Any) -> float | None:
        values = [value for value in (first, second) if isinstance(value, int | float) and not isinstance(value, bool)]
        if not values:
            return None
        return float(min(values))

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

    def _validate_dft_audit_payload(
        self,
        *,
        audit: dict[str, Any],
        evidence_items: list[dict[str, Any]],
        materials: dict[str, Any],
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        target_id = str(audit.get("target_id") or "").strip()
        decision = str(audit.get("decision") or "").strip()
        if not evidence_items:
            errors.append({"code": "evidence_ids_required", "message": "Each DFT review audit requires evidence_ids."})
            return errors
        if target_id.lower() != "new" and not any(
            self._evidence_relevant_to_dft_target(item, target_id, materials)
            for item in evidence_items
        ):
            errors.append(
                {
                    "code": "unrelated_evidence_id",
                    "message": "At least one evidence_id must directly support the reviewed DFT target.",
                }
            )
        if decision in {"REVISE", "new_candidate"}:
            errors.extend(self._validate_structured_dft_value(audit))
        if decision == "new_candidate" and not has_pdf_evidence_anchor(evidence_items):
            errors.append(
                {
                    "code": "missing_pdf_evidence_anchor",
                    "message": "new_candidate requires at least one cited evidence_id with a PDF page anchor.",
                }
            )
        return errors

    @staticmethod
    def _evidence_relevant_to_dft_target(
        evidence: dict[str, Any],
        target_id: str,
        materials: dict[str, Any],
    ) -> bool:
        if str(evidence.get("source_record_id") or "") == target_id:
            return True
        row = materials.get("dft_row_payloads_by_id", {}).get(target_id)
        if not isinstance(row, dict):
            return False
        if str(evidence.get("source_paper_id") or "") != str(row.get("source_paper_id") or ""):
            return False
        figure = str(evidence.get("figure") or evidence.get("figure_label") or "").strip().lower()
        table = str(evidence.get("table") or evidence.get("caption") or "").strip().lower()
        section = str(evidence.get("section") or evidence.get("section_title") or "").strip().lower()
        row_figure = str(row.get("source_figure") or "").strip().lower()
        row_section = str(row.get("source_section") or "").strip().lower()
        if row_figure and figure and row_figure in figure:
            return True
        if row_section and section and (row_section in section or section in row_section):
            return True
        evidence_text = " ".join(
            str(evidence.get(key) or "")
            for key in ("text", "quoted_text", "markdown_content", "content_summary", "caption")
        ).lower()
        row_value = row.get("value")
        row_unit = str(row.get("unit") or "").strip().lower()
        if row_value is not None and str(row_value).lower() in evidence_text and row_unit and row_unit in evidence_text:
            return True
        if table and row.get("evidence_payload"):
            payload = row.get("evidence_payload") if isinstance(row.get("evidence_payload"), dict) else {}
            row_table = str(payload.get("table") or "").strip().lower()
            if row_table and row_table in table:
                return True
        return False

    @classmethod
    def _validate_structured_dft_value(cls, audit: dict[str, Any]) -> list[dict[str, str]]:
        corrected = audit.get("corrected_value")
        if not isinstance(corrected, dict):
            return [
                {
                    "code": "missing_structured_corrected_value",
                    "message": "REVISE and new_candidate require corrected_value as an object.",
                }
            ]
        errors: list[dict[str, str]] = []
        material = cls._first_nonblank(
            corrected.get("material_identity"),
            corrected.get("material"),
            corrected.get("catalyst"),
            corrected.get("structure_name"),
        )
        property_type = cls._first_nonblank(
            corrected.get("property_type"),
            corrected.get("property"),
            corrected.get("energy_type"),
        )
        value = corrected.get("value")
        unit = cls._first_nonblank(corrected.get("unit"))
        if not material:
            errors.append({"code": "missing_material_identity", "message": "corrected_value must include material_identity."})
        if not property_type:
            errors.append({"code": "missing_property_type", "message": "corrected_value must include property_type."})
        if not cls._is_number(value):
            errors.append({"code": "invalid_dft_value", "message": "corrected_value.value must be numeric."})
        if not unit:
            errors.append({"code": "missing_unit", "message": "corrected_value must include unit."})
        property_text = str(property_type or "").lower()
        needs_reaction_context = any(
            marker in property_text
            for marker in ("reaction", "barrier", "free_energy", "adsorption", "binding", "gibbs")
        )
        if needs_reaction_context and not cls._first_nonblank(
            corrected.get("adsorbate"),
            corrected.get("reaction_step"),
            corrected.get("reaction_type"),
        ):
            errors.append(
                {
                    "code": "missing_reaction_context",
                    "message": "Reaction or adsorption DFT values require adsorbate, reaction_step, or reaction_type.",
                }
            )
        return errors

    @staticmethod
    def _first_nonblank(*values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _is_number(value: Any) -> bool:
        try:
            float(value)
        except (TypeError, ValueError):
            return False
        return True

    @classmethod
    def _best_pdf_evidence_item(cls, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        anchored = [item for item in items if first_pdf_evidence_anchor(item)]
        if not anchored:
            return None
        return sorted(
            anchored,
            key=lambda item: (
                0 if (item.get("figure") or item.get("figure_label") or item.get("table") or item.get("caption")) else 1,
                0 if cls._evidence_quote(item) else 1,
            ),
        )[0]

    @classmethod
    def _best_quote_evidence_item(cls, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next((item for item in items if cls._evidence_quote(item)), None)

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
    def _local_ai_writeback_contract() -> dict[str, Any]:
        return {
            "dft_web_ai_is_suggestion_only": True,
            "required_per_audit_tools": list(LOCAL_AI_REQUIRED_TOOLS),
            "required_local_ai_verification": {
                "verified_against_pdf": True,
                "used_tools_include": list(LOCAL_AI_REQUIRED_TOOLS),
                "must_read_each_target": "get_codex_item",
                "must_read_each_cited_page": "read_paper_page",
            },
            "writeback": {
                "tool": "import_analysis",
                "auto_apply_review_rules": True,
                "source": "local_ai",
                "after_write_readback": [
                    "object_versions",
                    "candidate_status",
                    "conflicts",
                    "export_safety",
                    "unfinished_items",
                ],
            },
        }

    @staticmethod
    def _return_template(materials: dict[str, Any]) -> dict[str, Any]:
        metadata = materials["paper_metadata"]
        return {
            "schema_version": "offline_dft_review_result_v1",
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "figure_table_completed_snapshot_fingerprint": materials["curated_evidence_snapshot"][
                "completed_snapshot_fingerprint"
            ],
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "review_source": {
                "review_source_type": "web_ai",
                "reviewer_label": "user-provided AI",
                "reviewer_model": None,
                "tool_capabilities": ["none"],
            },
            "overall_status": "uncertain",
            "coverage_acknowledgement": {
                "expected_target_ids": sorted(materials["target_dft_result_ids"]),
                "reviewed_target_ids": [],
                "coverage_complete": False,
            },
            "object_review_audits": [],
            "uncertainties": [],
            "notes": [],
        }

    @staticmethod
    def _dft_review_checklist(materials: dict[str, Any]) -> dict[str, Any]:
        existing_candidates = materials["initial_dft_candidates"]["existing_candidates"]
        candidate_by_id = {str(item.get("target_id")): item for item in existing_candidates}
        evidence_by_target: dict[str, list[str]] = {target_id: [] for target_id in materials["target_dft_result_ids"]}
        for evidence_id, evidence in sorted(materials["evidence_map"].items()):
            target_id = str(evidence.get("source_record_id") or "")
            if target_id in evidence_by_target and len(evidence_by_target[target_id]) < 8:
                evidence_by_target[target_id].append(evidence_id)
        targets = []
        for target_id in sorted(materials["target_dft_result_ids"]):
            candidate = candidate_by_id.get(target_id, {})
            targets.append(
                {
                    "target_id": target_id,
                    "target_type": "dft_results",
                    "field_name": "dft_results",
                    "required_once": True,
                    "allowed_decisions": ["PASS", "REVISE", "REJECT", "NEEDS_HUMAN"],
                    "preferred_evidence_ids": evidence_by_target.get(target_id, []),
                    "candidate_summary": {
                        "material_identity": candidate.get("material_identity"),
                        "property_type": candidate.get("property_type"),
                        "value": candidate.get("value"),
                        "value_upper": candidate.get("value_upper"),
                        "value_kind": candidate.get("value_kind"),
                        "unit": candidate.get("unit"),
                        "adsorbate": candidate.get("adsorbate"),
                        "reaction_step": candidate.get("reaction_step"),
                        "source_section": candidate.get("source_section"),
                        "source_figure": candidate.get("source_figure"),
                        "candidate_status": candidate.get("candidate_status"),
                    },
                }
            )
        return {
            "schema_version": "offline_dft_review_checklist_v1",
            "target_ids": sorted(materials["target_dft_result_ids"]),
            "expected_count": len(materials["target_dft_result_ids"]),
            "completion_rule": (
                "Return exactly one object_review_audits item for every target_id in this checklist. "
                "Use NEEDS_HUMAN when evidence is insufficient."
            ),
            "targets": targets,
            "new_candidate_rule": {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "when_to_use": "Only when the package evidence contains a DFT result missing from existing candidates.",
                "corrected_value_required_fields": ["material_identity", "property_type", "value", "unit"],
            },
        }

    @staticmethod
    def _format_examples() -> dict[str, Any]:
        target_id = "<target_id from parsed/dft_review_checklist.json>"
        evidence_id = "<supporting evidence_id from manifest.json or evidence files>"
        base = {
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "dft_results",
            "evidence_checked": True,
            "evidence_ids": [evidence_id],
            "confidence": 0.9,
            "blocking_errors": [],
            "recommended_action": "ready_for_local_ai_pdf_verification",
        }
        return {
            "schema_version": "offline_dft_review_format_examples_v1",
            "usage": [
                "These examples are format only; do not copy placeholder IDs into the final answer.",
                "Use real target_id values from parsed/dft_review_checklist.json and real evidence_ids from this package.",
                "The final answer must be the return_template.json object shape, not this examples wrapper.",
            ],
            "examples": {
                "pass_existing_candidate": {
                    "object_review_audits": [
                        {
                            **base,
                            "decision": "PASS",
                            "corrected_value": None,
                            "reason": "The cited package evidence directly supports the existing DFT candidate.",
                        }
                    ]
                },
                "revise_existing_candidate": {
                    "object_review_audits": [
                        {
                            **base,
                            "decision": "REVISE",
                            "corrected_value": {
                                "material_identity": "Fe-N-C",
                                "property_type": "adsorption_energy",
                                "value": -1.2,
                                "unit": "eV",
                                "adsorbate": "Li2S",
                            },
                            "reason": "The cited package evidence reports the same property with a corrected numeric value.",
                        }
                    ]
                },
                "needs_human_existing_candidate": {
                    "object_review_audits": [
                        {
                            **base,
                            "decision": "NEEDS_HUMAN",
                            "corrected_value": None,
                            "confidence": 0.2,
                            "blocking_errors": ["evidence_ambiguous"],
                            "recommended_action": "local_ai_pdf_check_required",
                            "reason": "The package evidence is ambiguous or insufficient for a safe PASS/REVISE/REJECT.",
                        }
                    ]
                },
                "new_candidate": {
                    "object_review_audits": [
                        {
                            **base,
                            "target_id": "new",
                            "decision": "new_candidate",
                            "corrected_value": {
                                "material_identity": "Co-N4/G",
                                "property_type": "free_energy",
                                "value": 0.42,
                                "unit": "eV",
                                "reaction_step": "Li2S2 to Li2S",
                            },
                            "reason": "The cited package evidence contains a DFT result missing from existing candidates.",
                        }
                    ]
                },
            },
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
3. 每条 `object_review_audits` 都必须引用一个或多个真实 `evidence_ids`。证据编号来自 `manifest.json` 和 `evidence/`，且必须和该 DFT 目标直接相关。
4. 已有主文献 DFT candidate 使用 `PASS`、`REVISE`、`REJECT` 或 `NEEDS_HUMAN`。
5. 必须为 `manifest.json` 的 `target_dft_result_ids` 中每个已有候选都提交 1 条审核结果；同一个 `target_id + field_name` 不要复制多条，多个证据合并到同一条的 `evidence_ids`。证据不足也要提交 `NEEDS_HUMAN`，不能漏掉该 target_id。只有全部覆盖时才能使用 `overall_status="completed"`；未覆盖全部已有候选时服务器不会生成导入请求。
6. 发现漏项时使用 `decision="new_candidate"`、`target_type="dft_results"`、`target_id="new"`、`field_name="dft_results"`；`corrected_value` 至少包含 `material_identity`、`property_type`、`value`、`unit`；`value` 必须是 JSON number，不要写成 `"−1.20 eV"`，单位只写入 `unit`。
7. SI 是主文献的证据来源，不是独立审核任务。SI 中发现的漏项仍写回当前主文献，使用 `new_candidate`。
8. 不得声称已写数据库、已入库、已确认、已 verified 或已成为 ML_Ready。
9. 严格按 `return_schema.json` 输出一个 JSON 对象；不要修改 schema，不要用自由散文替代 JSON，也不要包 Markdown 代码块。
10. 保留 `return_template.json` 中的 `bundle_fingerprint`、`figure_table_completed_snapshot_fingerprint`、`paper_id`、`paper_code` 原值。
11. 更新 `coverage_acknowledgement.reviewed_target_ids` 和 `coverage_acknowledgement.coverage_complete`；服务器最终仍会按 `object_review_audits` 重新计算覆盖率，缺一个 target_id 就不会生成导入请求。
12. `format_examples.json` 只是格式示例；不要照抄示例 ID，也不要输出示例文件外层 wrapper。最终只输出 `return_template.json` 的对象结构。

## 材料顺序

先读 `manifest.json`、`parsed/initial_dft_candidates.json`、`parsed/dft_review_checklist.json`、`format_examples.json`、`parsed/curated_figure_table_evidence_snapshot.json`，再读 `evidence/text_snippets.jsonl`、相关表格和图片。`parsed/extracted_*.json` 提供证据编号与来源映射。

如果 `curated_figure_table_evidence_snapshot.json` 的 `stage_status` 不是 `completed` 或 `not_required`，或 `rag_quality_status=blocked`，或 `completed_snapshot_fingerprint` 与当前快照不一致，说明图表证据还没有完成第一阶段闭环；此时不要继续产出 DFT 终审 JSON，应先完成图表证据整理。

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
