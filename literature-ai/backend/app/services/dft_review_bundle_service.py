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
    PaperSection,
    PaperTable,
)
from app.schemas.dft_review_bundle import OfflineDFTReviewResult
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService, pending_needs_human_actions_from_review_payload
from app.services.figure_table_snapshot_service import compute_figure_table_snapshot
from app.services.figure_rag_quality import build_figure_rag_quality_summary
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.review_bundle_shared import compact_figure_artifact, linked_source_papers
from app.services.dft_rescan_policy import normalize_source_document_type
from app.services.source_pdf_inventory import build_source_pdf_inventory, public_source_pdf_inventory
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.evidence_anchors import first_pdf_evidence_anchor, has_pdf_evidence_anchor
from app.utils.review_safety import bulk_export_gate_results


OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION = "offline_dft_review_bundle_v1"
DFT_LIVE_REVIEW_TASK_SCHEMA_VERSION = "dft_live_review_task_v1"
MAX_TEXT_SNIPPETS = 100
MAX_TEXT_SNIPPET_CHARS = 6000
MAX_FIGURE_FILES = 24
MAX_TOTAL_FIGURE_BYTES = 24 * 1024 * 1024
MAX_SOURCE_PDF_COUNT = 8
MAX_TOTAL_SOURCE_PDF_BYTES = 160 * 1024 * 1024
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
_LIVE_TASK_OMIT = object()


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


class ChartReviewScopeSelectionRequiredError(ValueError):
    code = "chart_review_scope_selection_required"

    def __init__(self, paper_id: UUID, runs: list[dict[str, Any]]) -> None:
        self.state = {
            "code": self.code,
            "paper_id": str(paper_id),
            "message": "请选择图表审核批次；当前论文存在多个图表审核批次，系统不会自动选择或退回整篇论文。",
            "chart_runs": runs,
        }
        super().__init__(self.code)

    @property
    def detail(self) -> dict[str, Any]:
        return self.state


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


def _optional_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid_chart_run_id")


class DFTReviewBundleService:
    _bundle_figure_artifact = staticmethod(compact_figure_artifact)

    """Build and validate small, temporary, offline DFT review packages."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        return _optional_uuid(value)

    def get_figure_table_review_state(self, paper_id: UUID, *, chart_run_id: UUID | None = None) -> dict[str, Any]:
        task = EvidenceReviewBundleService(self.session, self.settings).get_review_task(
            paper_id,
            run_id=chart_run_id,
        )
        return {
            "stage_status": task.get("stage_status"),
            "allowed_statuses": sorted(FIGURE_TABLE_REVIEW_READY_STATUSES),
            "rag_quality_status": task.get("rag_quality_status"),
            "rag_quality": task.get("rag_quality") or {},
            "blocking_errors": task.get("blocking_errors") or [],
            "current_snapshot_fingerprint": task.get("current_snapshot_fingerprint"),
            "completed_snapshot_fingerprint": task.get("completed_snapshot_fingerprint"),
            "review_run_id": task.get("latest_review_run_id"),
            "reviewed_at": task.get("reviewed_at"),
        }

    def get_review_state(self, paper_id: UUID) -> dict[str, Any]:
        materials = self._build_materials(paper_id, enforce_figure_table_gate=False)
        return {
            "paper_id": materials["paper_metadata"]["paper_id"],
            "paper_code": materials["paper_metadata"]["paper_code"],
            "review_mode": materials["review_mode"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "summary": materials["evidence_summary"],
            "review_gate": materials["curated_evidence_snapshot"]["review_gate"],
            "existing_terminal_context_count": len(
                materials["initial_dft_candidates"]["existing_terminal_context"]
            ),
            "target_count": len(materials["target_dft_result_ids"]),
            "review_runs": materials["curated_evidence_snapshot"].get("review_runs") or [],
        }

    def get_review_task(
        self,
        paper_id: UUID,
        *,
        catalyst_sample_id: UUID | None = None,
        dft_result_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        """Return the current local-AI DFT task without creating an export or writing state."""

        materials = self._build_materials(
            paper_id,
            enforce_figure_table_gate=False,
            catalyst_sample_id=catalyst_sample_id,
            dft_result_ids=dft_result_ids,
        )
        metadata = materials["paper_metadata"]
        evidence_items = [
            self._live_evidence_item(item)
            for item in sorted(
                materials["evidence_map"].values(),
                key=lambda item: str(item.get("evidence_id") or ""),
            )
        ]
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in materials["evidence_map"].values()
            if str(item.get("evidence_id") or "").strip()
        }
        target_ids = sorted(str(item) for item in materials["target_dft_result_ids"])
        targets_by_id = {
            str(item.get("target_id")): item
            for item in materials["initial_dft_candidates"].get("existing_candidates", [])
            if str(item.get("target_id") or "").strip()
        }
        target_evidence_map = {
            target_id: [
                evidence_id
                for evidence_id, evidence in sorted(evidence_by_id.items())
                if self._evidence_relevant_to_dft_target(evidence, target_id, materials)
            ]
            for target_id in target_ids
        }
        targets = []
        for target_id in target_ids:
            target = self._live_target_payload(targets_by_id.get(target_id) or {})
            target["evidence_ids"] = target_evidence_map[target_id]
            targets.append(target)

        terminal_context = [
            self._live_target_payload(item)
            for item in materials["initial_dft_candidates"].get("existing_terminal_context", [])
        ]
        review_source = {
            "review_source_type": "local_ai",
            "reviewer_label": "local_ai",
            "reviewer_model": None,
            "tool_capabilities": list(LOCAL_AI_REQUIRED_TOOLS),
        }
        review_result_template = self._return_template(materials)
        review_result_template["review_source"] = deepcopy(review_source)
        import_analysis_template = self._live_import_analysis_template(
            materials=materials,
            review_source=review_source,
            target_evidence_map=target_evidence_map,
            evidence_items=evidence_items,
        )
        eligible_evidence_ids = [
            str(item.get("evidence_id"))
            for item in evidence_items
            if item.get("eligible_for_auto_apply") is True
        ]
        missing_data_scan = {
            "required": True,
            "scope": "all packaged evidence_items with eligible_for_auto_apply=true",
            "eligible_evidence_ids": eligible_evidence_ids,
            "existing_terminal_context_target_ids": [
                str(item.get("target_id"))
                for item in terminal_context
                if str(item.get("target_id") or "").strip()
            ],
            "completion_field": "coverage_acknowledgement.missing_data_search_complete",
            "completion_value": False,
            "rule": (
                "Inspect every eligible evidence item for genuinely missing DFT results and compare each candidate "
                "with existing_terminal_context before proposing target_id='new'."
            ),
        }
        public_paper = {
            key: metadata.get(key)
            for key in (
                "paper_id",
                "paper_code",
                "title",
                "doi",
                "authors",
                "year",
                "journal",
                "abstract",
                "paper_type",
            )
        }
        source_documents = [
            self._live_public_payload(item)
            for item in materials["paper_metadata"].get("source_documents", [])
        ]
        source_pdf_inventory = self._live_public_payload(
            materials["paper_metadata"].get("source_pdf_inventory", [])
        )
        review_gate = self._live_public_payload(
            materials["curated_evidence_snapshot"].get("review_gate") or {}
        )
        local_ai = {
            "review_result_template": review_result_template,
            "import_analysis_template": import_analysis_template,
        }
        return {
            "schema_version": DFT_LIVE_REVIEW_TASK_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "title": metadata.get("title"),
            "paper": public_paper,
            "writeback": {
                "paper_id": metadata["paper_id"],
                "paper_code": metadata["paper_code"],
                "target_type": "dft_results",
                "target_field": "dft_results",
                "tool": "import_analysis",
            },
            "writeback_paper_id": metadata["paper_id"],
            "source_documents": source_documents,
            "source_pdf_inventory": source_pdf_inventory,
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "catalyst_sample_id": materials["review_selection"]["catalyst_sample_id"],
            "dft_result_ids": materials["review_selection"]["dft_result_ids"],
            "explicit_review": materials["review_selection"]["explicit"],
            "figure_table_completed_snapshot_fingerprint": materials["curated_evidence_snapshot"].get(
                "completed_snapshot_fingerprint"
            ),
            "figure_table_review": review_gate,
            "review_mode": materials["review_mode"],
            "target_count": len(target_ids),
            "target_ids": target_ids,
            "targets": targets,
            "existing_terminal_context_count": len(terminal_context),
            "existing_terminal_context": terminal_context,
            "evidence_count": len(evidence_items),
            "evidence_items": evidence_items,
            "target_evidence_map": target_evidence_map,
            "missing_data_scan": missing_data_scan,
            "missing_data_scan_requirements": missing_data_scan,
            "missing_data_search": missing_data_scan,
            "local_ai": local_ai,
            "review_result_template": review_result_template,
            "import_analysis_template": import_analysis_template,
            "local_ai_writeback_contract": self._local_ai_writeback_contract(),
            "offline_zip_policy": {
                "available": True,
                "purpose": "web_ai_third_party_or_offline_review",
                "local_ai_workflow": "Use this live task with get_codex_item/read_paper_page and import_analysis.",
                "compatibility": "The existing in-memory build_zip workflow remains unchanged.",
                "retention_policy": "generated_in_memory_not_persisted_on_server",
            },
        }

    def get_completeness_snapshot(self, paper_id: UUID) -> dict[str, Any]:
        """Expose the authoritative, read-only inputs for DFT completeness.

        This keeps source-PDF inventory, chart scope, and bundle fingerprint
        computation in the existing whole-paper review implementation.
        """

        materials = self._build_materials(paper_id, enforce_figure_table_gate=False)
        # Do not reuse the full offline-package fingerprint here: it also
        # contains transient DFT candidate lifecycle state.  Completeness must
        # compare only the versioned scientific source/review-scope manifest.
        from app.services.source_snapshot_reconciliation_service import build_source_snapshot_manifest

        source_snapshot = build_source_snapshot_manifest(
            source_documents=materials["source_documents"],
            source_pdf_inventory=materials["source_pdf_inventory"],
            extracted_figures=materials["extracted_figures"],
            extracted_tables=materials["extracted_tables"],
            text_snippets=materials["text_snippets"],
            review_gate=materials["curated_evidence_snapshot"]["review_gate"],
        )
        return {
            "paper_id": materials["paper_metadata"]["paper_id"],
            "paper_code": materials["paper_metadata"]["paper_code"],
            "review_mode": materials["review_mode"],
            "source_pdf_inventory": materials["paper_metadata"]["source_pdf_inventory"],
            "review_gate": materials["curated_evidence_snapshot"]["review_gate"],
            "source_snapshot_fingerprint": source_snapshot["fingerprint"],
            "source_snapshot_manifest": source_snapshot["manifest"],
            "source_snapshot_algorithm_version": source_snapshot["manifest"]["schema_version"],
        }

    def _resolve_chart_run_id(
        self,
        paper_id: UUID,
        chart_run_id: UUID | None,
        *,
        explicit_paper_scope: bool = False,
    ) -> UUID | None:
        if chart_run_id is not None:
            EvidenceReviewBundleService(self.session, self.settings).get_review_task(paper_id, run_id=chart_run_id)
            return chart_run_id
        if explicit_paper_scope:
            return None
        options = EvidenceReviewBundleService(self.session, self.settings).get_review_scope_options(paper_id)
        primary_completed = options.get("primary_completed_run")
        if isinstance(primary_completed, dict) and primary_completed.get("chart_run_id"):
            # A stable completed main-paper review is the safe default.  It must
            # win over unrelated unfinished or duplicated historical attempts;
            # callers can still pass chart_run_id to select another scope.
            return UUID(str(primary_completed["chart_run_id"]))
        runs = options.get("chart_runs") if isinstance(options.get("chart_runs"), list) else []
        representatives = [
            run for run in runs
            if run.get("is_duplicate_representative", True)
        ]
        if len(representatives) > 1:
            raise ChartReviewScopeSelectionRequiredError(paper_id, representatives)
        return UUID(str(representatives[0]["chart_run_id"])) if representatives else None

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

    def build_zip(
        self,
        paper_id: UUID,
        *,
        chart_run_id: UUID | None = None,
        explicit_paper_scope: bool = False,
        include_figure_files: bool = True,
    ) -> dict[str, Any]:
        materials = self._build_materials(
            paper_id,
            chart_run_id=chart_run_id,
        )
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

        source_pdf_inventory = materials["source_pdf_inventory"]
        pdf_warnings = [
            f"{item['omitted_reason']}:{item.get('paper_code') or item.get('paper_id')}"
            for item in source_pdf_inventory
            if item.get("omitted_reason")
        ]
        for item in source_pdf_inventory:
            if item.get("included_in_bundle"):
                files[str(item["bundle_file"])] = Path(str(item["_pdf_abs_path"])).read_bytes()
        pdf_count = sum(1 for item in source_pdf_inventory if item.get("included_in_bundle"))
        pdf_bytes_total = sum(int(item.get("size_bytes") or 0) for item in source_pdf_inventory if item.get("included_in_bundle"))
        materials["paper_metadata"]["source_pdf_inventory"] = public_source_pdf_inventory(source_pdf_inventory)
        files["parsed/paper_metadata.json"] = _json_bytes(materials["paper_metadata"])

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
                data, suffix, compacted = compact_figure_artifact(artifact)
                if figure_bytes_total + len(data) > MAX_TOTAL_FIGURE_BYTES:
                    figure_warnings.append("figure_byte_limit_reached")
                    continue
                filename = _safe_name(figure["evidence_id"].replace(":", "_"), "figure") + suffix
                files[f"evidence/figures/{filename}"] = data
                figure["bundle_file"] = f"evidence/figures/{filename}"
                figure["bundle_image_format"] = suffix.lstrip(".")
                figure["bundle_image_compacted"] = compacted
                figure["bundle_image_size_bytes"] = len(data)
                figure_file_count += 1
                figure_bytes_total += len(data)

        # Re-serialize figures after assigning bundle-local file names.
        files["parsed/extracted_figures.json"] = _json_bytes(self._public_figures(materials["extracted_figures"]))

        template = self._return_template(materials)
        files["return_template.json"] = _json_bytes(template)
        files["WEB_AI_FILL_THIS.json"] = _json_bytes(template)
        files["OUTPUT_RULES.json"] = _json_bytes(self._output_rules(materials))
        files["START_HERE.md"] = self._start_here(materials).encode("utf-8")
        files["instructions_for_web_ai.md"] = self._instructions(materials).encode("utf-8")

        inventory = [
            {"path": path, "size_bytes": len(data), "sha256": _sha256(data)}
            for path, data in sorted(files.items())
        ]
        manifest = {
            "schema_version": OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "review_mode": materials["review_mode"],
            "paper": {
                "paper_id": materials["paper_metadata"]["paper_id"],
                "paper_code": materials["paper_metadata"]["paper_code"],
                "title": materials["paper_metadata"]["title"],
            },
            "review_scope": "single_paper_main_plus_relevant_supplementary_dft_evidence",
            "source_documents": materials["paper_metadata"]["source_documents"],
            "source_pdf_inventory": materials["paper_metadata"]["source_pdf_inventory"],
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
                "mode": materials["review_mode"],
                "target_ids": sorted(materials["target_dft_result_ids"]),
                "required_decisions_for_existing_targets": ["PASS", "REVISE", "REJECT", "NEEDS_HUMAN"],
                "completion_rule": (
                    "In this single comprehensive pass, every target_dft_result_id must appear exactly once and "
                    "coverage_acknowledgement.missing_data_search_complete must be true after all eligible packaged "
                    "evidence has been scanned for genuinely missing DFT results."
                ),
            },
            "counts": {
                "main_dft_candidates": len(materials["initial_dft_candidates"]["existing_candidates"]),
                "supporting_si_dft_candidates": len(
                    materials["initial_dft_candidates"]["supporting_si_candidates"]
                ),
                "excluded_terminal_main_dft_candidates": materials["excluded_terminal_main_dft_candidates"],
                "existing_terminal_context": len(materials["initial_dft_candidates"]["existing_terminal_context"]),
                "reviewed_figures": materials["evidence_summary"]["reviewed_figures"],
                "reviewed_tables": materials["evidence_summary"]["reviewed_tables"],
                "reviewed_main_figures": materials["evidence_summary"]["reviewed_main_figures"],
                "reviewed_main_tables": materials["evidence_summary"]["reviewed_main_tables"],
                "pending_main_figures": materials["evidence_summary"]["pending_main_figures"],
                "pending_main_tables": materials["evidence_summary"]["pending_main_tables"],
                "pending_supporting_figures": materials["evidence_summary"].get("pending_supporting_figures", 0),
                "pending_supporting_tables": materials["evidence_summary"].get("pending_supporting_tables", 0),
                "unreviewed_supporting_context": materials["evidence_summary"]["unreviewed_supporting_context"],
                "text_snippets": len(materials["text_snippets"]),
                "tables": len(materials["extracted_tables"]),
                "figures": len(materials["extracted_figures"]),
                "included_figure_files": figure_file_count,
                "included_figure_bytes": figure_bytes_total,
                "source_documents": len(materials["source_documents"]),
                "included_source_pdfs": pdf_count,
                "source_pdf_bytes": pdf_bytes_total,
            },
            "warnings": sorted(set(materials["warnings"] + figure_warnings + pdf_warnings)),
            "pdf_files": {"count": pdf_count, "bytes": pdf_bytes_total},
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

    def validate_result(
        self,
        paper_id: UUID,
        raw_payload: dict[str, Any],
        *,
        chart_run_id: UUID | None = None,
        explicit_paper_scope: bool = False,
    ) -> dict[str, Any]:
        raw_payload, normalization_warnings = self._normalize_common_web_ai_result_json(raw_payload)
        payload_chart_run_id = self._optional_uuid(raw_payload.get("chart_run_id"))
        if chart_run_id is not None and payload_chart_run_id != chart_run_id:
            return {
                "valid": False,
                "errors": [{"code": "chart_run_mismatch", "message": "chart_run_id does not match the selected DFT review scope"}],
                "warnings": normalization_warnings,
                "import_analysis_request": None,
            }
        if payload_chart_run_id is not None:
            return {
                "valid": False,
                "errors": [{"code": "chart_run_mismatch", "message": "DFT review now uses the paper-level reviewed-evidence aggregate; chart_run_id must be null"}],
                "warnings": normalization_warnings,
                "import_analysis_request": None,
            }
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

        try:
            catalyst_sample_id = UUID(result.catalyst_sample_id) if result.catalyst_sample_id else None
            dft_result_ids = [UUID(item) for item in result.dft_result_ids]
        except (TypeError, ValueError, AttributeError):
            return {
                "valid": False,
                "errors": [
                    {
                        "code": "invalid_explicit_target_id",
                        "message": "catalyst_sample_id and dft_result_ids must contain valid UUID values",
                    }
                ],
                "warnings": normalization_warnings,
                "import_analysis_request": None,
            }
        materials = self._build_materials(
            paper_id,
            enforce_figure_table_gate=False,
            catalyst_sample_id=catalyst_sample_id,
            dft_result_ids=dft_result_ids,
        )
        source_pdf_inventory_complete = all(
            item.get("pdf_available") and item.get("included_in_bundle")
            for item in materials["source_pdf_inventory"]
        )
        if source_pdf_inventory_complete:
            self.ensure_figure_table_review_ready(materials["curated_evidence_snapshot"]["review_gate"])
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
        expected_chart_scope = "paper_reviewed_aggregate"
        expected_chart_run_id = None
        if result.chart_scope_type != expected_chart_scope or (result.chart_run_id or None) != expected_chart_run_id:
            add_error("chart_run_mismatch", "chart_run_id/chart_scope_type does not match the selected DFT review scope")
        if result.review_mode != materials["review_mode"]:
            add_error("review_mode_mismatch", "review_mode does not match the current DFT target set")
        if result.bundle_fingerprint != materials["bundle_fingerprint"]:
            add_error(
                "stale_or_mismatched_bundle",
                "bundle_fingerprint differs from the current evidence snapshot; export a new package and review again",
            )
        figure_table_gate = materials["curated_evidence_snapshot"]["review_gate"]
        if figure_table_gate.get("stage_status") not in FIGURE_TABLE_REVIEW_READY_STATUSES:
            add_error(
                "figure_table_review_not_completed",
                "Figure/table review must be completed or not_required before DFT review.",
            )
        expected_completed_snapshot = figure_table_gate.get("completed_snapshot_fingerprint")
        if result.figure_table_completed_snapshot_fingerprint != expected_completed_snapshot:
            add_error(
                "stale_figure_table_review_snapshot",
                "figure_table_completed_snapshot_fingerprint differs from the current completed figure/table snapshot",
            )

        normalized_audits: list[dict[str, Any]] = []
        reviewed_target_ids: set[str] = set()
        new_candidate_count = 0
        seen_target_fields: dict[tuple[str, str, str | None], str] = {}
        for index, audit in enumerate(result.object_review_audits):
            target_field_key = (
                audit.target_id,
                audit.field_name,
                audit.temporary_id if audit.target_id.lower() == "new" else None,
            )
            previous_decision = seen_target_fields.get(target_field_key)
            if previous_decision is not None:
                error_code = (
                    "conflicting_target_field_review"
                    if previous_decision != audit.decision
                    else "duplicate_target_field_review"
                )
                add_error(
                    error_code,
                    "Each target_id + field_name + temporary_id may appear only once in a DFT review result",
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
            required_evidence_checks: list[dict[str, Any]] = []
            for evidence_item in evidence_items:
                try:
                    required_evidence_checks.append(self._evidence_verification_requirement(evidence_item))
                except ValueError as exc:
                    add_error(
                        "unexecutable_evidence_verification_requirement",
                        str(exc),
                        audit_index=index,
                    )
            required_page_checks = self._unique_page_checks(required_evidence_checks)
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
                    "temporary_id": audit.temporary_id,
                    "field_name": audit.field_name,
                    "decision": audit.decision,
                    "evidence_checked": audit.evidence_checked,
                    "evidence_ids": audit.evidence_ids,
                    "evidence_location": evidence_location,
                    "supporting_evidence": [self._compact_evidence(item) for item in evidence_items[1:]],
                    "required_evidence_checks": required_evidence_checks,
                    "required_page_checks": required_page_checks,
                    "blocking_errors": audit.blocking_errors,
                    "recommended_action": audit.recommended_action,
                    "dedupe_analysis": audit.dedupe_analysis,
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
                        "checked_evidence_ids": [],
                        "checked_pages": [],
                        "verification_note": None,
                        "instruction": (
                            "Cover every required_evidence_checks item with get_codex_item using item_paper_id "
                            "and source_record_id (ordinary same-source evidence may use source_paper_id), and "
                            "cover every unique (source_paper_id, page) with read_paper_page. A successful "
                            "evidence-object or page "
                            "read may be reused across audits that require the same key. read_paper_page returns "
                            "stored database page layout; the PDF judgment must also use the source PDF/page "
                            "evidence included in the review bundle."
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
        if result.review_mode == "comprehensive_review" and result.overall_status == "completed":
            if coverage_ack is None or not coverage_ack.missing_data_search_complete:
                add_error(
                    "incomplete_missing_data_search",
                    "Comprehensive DFT review requires coverage_acknowledgement."
                    "missing_data_search_complete=true after scanning all packaged eligible evidence for missing DFT data.",
                )
            missing_source_pdfs = [item for item in materials["source_pdf_inventory"] if not item.get("pdf_available")]
            omitted_source_pdfs = [
                item for item in materials["source_pdf_inventory"]
                if item.get("pdf_available") and not item.get("included_in_bundle")
            ]
            if missing_source_pdfs:
                add_error(
                    "source_pdf_missing_for_comprehensive_review",
                    "Comprehensive DFT review cannot claim full-text gap discovery while a main/SI source PDF is missing: "
                    + ", ".join(str(item.get("paper_code") or item.get("paper_id")) for item in missing_source_pdfs),
                )
            if omitted_source_pdfs:
                add_error(
                    "source_pdf_not_in_bundle",
                    "Comprehensive DFT review cannot claim full-text gap discovery while a main/SI source PDF is absent from this bundle: "
                    + ", ".join(
                        f"{item.get('paper_code') or item.get('paper_id')} ({item.get('omitted_reason') or 'not_included'})"
                        for item in omitted_source_pdfs
                    ),
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
            unique_evidence_checks = self._unique_evidence_checks(
                check
                for audit in normalized_audits
                for check in audit.get("required_evidence_checks", [])
            )
            unique_page_checks = self._unique_page_checks(unique_evidence_checks)
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
                        "chart_scope_type": result.chart_scope_type,
                        "chart_run_id": result.chart_run_id,
                        "review_mode": result.review_mode,
                        "catalyst_sample_id": result.catalyst_sample_id,
                        "dft_result_ids": result.dft_result_ids,
                        "figure_table_completed_snapshot_fingerprint": (
                            result.figure_table_completed_snapshot_fingerprint
                        ),
                        "paper_code": result.paper_code,
                        "overall_status": result.overall_status,
                        "web_ai_review_source": result.review_source.model_dump(mode="json"),
                        "local_ai_verification_required": True,
                        "required_local_ai_tools": list(LOCAL_AI_REQUIRED_TOOLS),
                        "local_ai_verification_reuse_policy": (
                            "Each audit must record complete coverage. One successful get_codex_item result may "
                            "be reused for the same evidence_id, and one successful read_paper_page result may "
                            "be reused for the same (source_paper_id, page)."
                        ),
                        "review_source": result.review_source.model_dump(mode="json"),
                    },
                    "local_ai_verification_plan": {
                        "unique_evidence_checks": unique_evidence_checks,
                        "unique_page_checks": unique_page_checks,
                        "evidence_check_count": len(unique_evidence_checks),
                        "page_check_count": len(unique_page_checks),
                    },
                    "coverage_acknowledgement": (
                        result.coverage_acknowledgement.model_dump(mode="json")
                        if result.coverage_acknowledgement is not None
                        else None
                    ),
                    "object_review_audits": normalized_audits,
                    "review_notes": review_notes,
                },
            }

        return {
            "valid": not errors,
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "review_mode": materials["review_mode"],
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
                    "Local AI must cover every audit's required evidence objects and source-paper pages. Identical "
                    "evidence_id and identical (source_paper_id, page) checks may reuse one successful tool result. "
                    "Each audit must still record checked_evidence_ids, checked_pages, used_tools, a verification_note, "
                    "and verified_against_pdf=true before authenticated import_analysis."
                ),
            },
            "local_ai_writeback_contract": self._local_ai_writeback_contract(),
        }

    def _build_materials(
        self,
        paper_id: UUID,
        *,
        chart_run_id: UUID | None = None,
        explicit_paper_scope: bool = False,
        enforce_figure_table_gate: bool = True,
        catalyst_sample_id: UUID | None = None,
        dft_result_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        if not str(paper.paper_code or "").strip():
            raise ValueError("paper_code_required_before_offline_review_export")
        if chart_run_id is not None:
            # Compatibility-only validation: a supplied legacy run must belong to
            # this paper, but it never narrows the new paper-level DFT aggregate.
            EvidenceReviewBundleService(self.session, self.settings).get_review_task(
                paper_id,
                run_id=chart_run_id,
            )
        source_papers = linked_source_papers(
            self.session,
            paper,
            relationship_types=SUPPLEMENTARY_RELATIONSHIP_TYPES,
        )
        source_documents = []
        for item in source_papers:
            source = item["paper"]
            pdf_path = self._resolve_pdf(source.pdf_path)
            source_documents.append({
                "source_document_type": item["source_document_type"],
                "paper_id": str(source.id),
                "paper_code": source.paper_code,
                "title": source.title,
                "relationship_id": item.get("relationship_id"),
                "role": item["prefix"],
                "pdf_available": pdf_path is not None,
                "pdf_size_bytes": pdf_path.stat().st_size if pdf_path is not None else None,
                "_pdf_abs_path": str(pdf_path) if pdf_path is not None else None,
            })
        source_pdf_inventory = build_source_pdf_inventory(
            source_documents,
            max_count=MAX_SOURCE_PDF_COUNT,
            max_total_bytes=MAX_TOTAL_SOURCE_PDF_BYTES,
        )
        source_ids = [item["paper"].id for item in source_papers]
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id.in_(source_ids))).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(source_ids))).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(source_ids))).all()
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(source_ids))).all()
        dft_settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id.in_(source_ids))).all()
        samples = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id.in_(source_ids))).all()

        review_selection, explicit_target_ids = self._resolve_explicit_review_targets(
            paper_id=paper.id,
            catalyst_sample_id=catalyst_sample_id,
            dft_result_ids=dft_result_ids,
        )

        source_by_id = {item["paper"].id: item for item in source_papers}
        # Keep the DFT gate on exactly the same normalized whole-paper figure
        # scope as chart review.  In particular, an SI extraction candidate
        # explicitly excluded as a duplicate must not invalidate a completed
        # chart snapshot or be exported as unreviewed evidence.
        figures, _excluded_duplicate_figures = EvidenceReviewBundleService._deduplicate_scope_figures(
            figures,
            source_by_id=source_by_id,
        )
        sample_by_id = {row.id: row for row in samples}
        dft_rows = sorted(dft_rows, key=lambda row: (row.paper_id != paper.id, str(row.id)))
        review_dft_rows, terminal_main_rows = self._dft_rows_for_review_bundle(
            dft_rows,
            main_paper_id=paper.id,
            explicit_target_ids=explicit_target_ids,
        )
        locator_by_target = self._dft_locator_payloads_by_target(dft_rows)
        anchors = self._collect_anchors(review_dft_rows)
        reviewed_aggregate = self._reviewed_evidence_aggregate(paper, source_by_id=source_by_id)

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
            reviewed_objects=reviewed_aggregate["objects"],
        )
        extracted_figures, figure_map = self._figures(
            source_by_id=source_by_id,
            figures=figures,
            anchors=anchors,
            reviewed_objects=reviewed_aggregate["objects"],
        )
        reviewed_aggregate["summary"]["unreviewed_supporting_context"] = sum(
            1
            for item in [*extracted_tables, *extracted_figures]
            if item.get("evidence_tier") == "unreviewed_supporting_context"
        )
        figure_by_id = {str(row.id): row for row in figures}
        evidence_figure_ids = {
            str(item.get("source_record_id"))
            for item in extracted_figures
            if item.get("source_record_id") and item.get("eligible_for_auto_apply")
        }
        figure_rag_quality = build_figure_rag_quality_summary(
            self.session,
            [row for figure_id, row in figure_by_id.items() if figure_id in evidence_figure_ids],
        )
        for item in text_snippets:
            is_support = item.get("source_document_type") == "supplementary_information"
            item["review_status"] = "unreviewed_context"
            item["eligible_for_auto_apply"] = False
            item["evidence_tier"] = "unreviewed_supporting_context" if is_support else "text_context"
        evidence_map = {**text_map, **table_map, **figure_map}

        curated_evidence_snapshot = self._aggregated_curated_evidence_snapshot(
            paper=paper,
            extracted_tables=extracted_tables,
            extracted_figures=extracted_figures,
            figure_rag_quality=figure_rag_quality,
            reviewed_aggregate=reviewed_aggregate,
        )
        unreviewed_figure_ids = sorted(
            str(item.get("source_record_id"))
            for item in extracted_figures
            if item.get("source_record_id") and not item.get("eligible_for_auto_apply")
        )
        if unreviewed_figure_ids:
            review_gate = dict(curated_evidence_snapshot.get("review_gate") or {})
            review_gate["stage_status"] = "needs_local_ai"
            review_gate["completed_snapshot_fingerprint"] = None
            review_gate["blocking_errors"] = [
                *(review_gate.get("blocking_errors") or []),
                {
                    "code": "dft_bundle_contains_unreviewed_figures",
                    "message": "Every figure exported in a DFT review bundle must first complete web-AI review and authenticated local-AI PDF verification.",
                    "figure_ids": unreviewed_figure_ids,
                },
            ]
            curated_evidence_snapshot["review_gate"] = review_gate
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
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in source_documents
            ],
            "source_pdf_inventory": public_source_pdf_inventory(source_pdf_inventory),
        }
        terminal_gate_by_id = bulk_export_gate_results(
            self.session,
            terminal_main_rows,
            target_type="dft_results",
        ) if terminal_main_rows else {}
        initial_dft_candidates = {
            "schema_version": "offline_initial_dft_candidates_v2",
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
            "existing_terminal_context": [
                {
                    **self._dft_row_payload(
                        row,
                        sample_by_id=sample_by_id,
                        source_by_id=source_by_id,
                        locator_by_target=locator_by_target,
                    ),
                    "readonly": True,
                    "eligible_as_write_target": False,
                    "is_ml_ready": str(row.candidate_status or "").strip().lower() in {
                        "ml_ready",
                        "ai_verified_ml_ready",
                    },
                    "is_rejected": (
                        str(row.candidate_status or "").strip().lower() == "rejected"
                        or "rejected" in str(getattr(terminal_gate_by_id.get(str(row.id)), "review_status", "") or "").lower()
                    ),
                    "review_status": str(
                        getattr(terminal_gate_by_id.get(str(row.id)), "review_status", "") or ""
                    ) or None,
                }
                for row in terminal_main_rows
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
        # One package now covers both existing-target review and missing-data discovery.
        # Keeping a single mode prevents users from exporting a second large package after
        # existing candidates reach a terminal state.
        review_mode = "comprehensive_review"

        fingerprint_payload = {
            "schema_version": OFFLINE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "paper_metadata": paper_metadata,
            "source_pdf_inventory": public_source_pdf_inventory(source_pdf_inventory),
            "source_documents": source_documents,
            "initial_dft_candidates": initial_dft_candidates,
            "text_snippets": text_snippets,
            "extracted_tables": extracted_tables,
            "extracted_figures": [
                {key: value for key, value in item.items() if key != "bundle_file" and not key.startswith("_")}
                for item in extracted_figures
            ],
            "curated_evidence_snapshot": curated_evidence_snapshot,
            "review_mode": review_mode,
            "review_selection": review_selection,
        }
        bundle_fingerprint = _sha256(_canonical_json_bytes(fingerprint_payload))
        warnings = []
        if not evidence_map:
            warnings.append("no_dft_relevant_evidence_found")
        if not initial_dft_candidates["existing_candidates"]:
            warnings.append("comprehensive_review_no_writable_main_dft_targets")
        if curated_evidence_snapshot["evidence_review_status"] != "applied":
            warnings.append("figure_table_evidence_not_yet_reviewed")

        return {
            "paper_metadata": paper_metadata,
            "source_documents": source_documents,
            "source_pdf_inventory": source_pdf_inventory,
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
            "excluded_terminal_main_dft_candidates": len(terminal_main_rows),
            "review_mode": review_mode,
            "evidence_summary": reviewed_aggregate["summary"],
            "review_selection": review_selection,
        }

    def _resolve_explicit_review_targets(
        self,
        *,
        paper_id: UUID,
        catalyst_sample_id: UUID | None,
        dft_result_ids: list[UUID] | None,
    ) -> tuple[dict[str, Any], set[UUID] | None]:
        requested_result_ids = list(dict.fromkeys(dft_result_ids or []))
        if catalyst_sample_id is not None and requested_result_ids:
            raise ValueError("catalyst_sample_id and dft_result_ids are mutually exclusive")

        if catalyst_sample_id is not None:
            sample = self.session.scalar(
                select(CatalystSample).where(
                    CatalystSample.paper_id == paper_id,
                    CatalystSample.id == catalyst_sample_id,
                )
            )
            if sample is None:
                raise LookupError("Catalyst sample not found for this paper")
            resolved_ids = set(
                self.session.scalars(
                    select(DFTResult.id).where(
                        DFTResult.paper_id == paper_id,
                        DFTResult.catalyst_sample_id == catalyst_sample_id,
                    )
                ).all()
            )
            return (
                {
                    "explicit": True,
                    "catalyst_sample_id": str(catalyst_sample_id),
                    "dft_result_ids": [],
                    "resolved_target_ids": sorted(str(item) for item in resolved_ids),
                },
                resolved_ids,
            )

        if requested_result_ids:
            resolved_ids = set(
                self.session.scalars(
                    select(DFTResult.id).where(
                        DFTResult.paper_id == paper_id,
                        DFTResult.id.in_(requested_result_ids),
                    )
                ).all()
            )
            missing_ids = sorted(str(item) for item in set(requested_result_ids) - resolved_ids)
            if missing_ids:
                raise LookupError(
                    "DFT results not found for this paper: " + ", ".join(missing_ids)
                )
            return (
                {
                    "explicit": True,
                    "catalyst_sample_id": None,
                    "dft_result_ids": [str(item) for item in requested_result_ids],
                    "resolved_target_ids": sorted(str(item) for item in resolved_ids),
                },
                resolved_ids,
            )

        return (
            {
                "explicit": False,
                "catalyst_sample_id": None,
                "dft_result_ids": [],
                "resolved_target_ids": [],
            },
            None,
        )

    def _dft_rows_for_review_bundle(
        self,
        rows: list[DFTResult],
        *,
        main_paper_id: UUID,
        explicit_target_ids: set[UUID] | None = None,
    ) -> tuple[list[DFTResult], list[DFTResult]]:
        if not rows:
            return [], []
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        selected: list[DFTResult] = []
        terminal_main: list[DFTResult] = []
        for row in rows:
            if explicit_target_ids is not None and row.paper_id == main_paper_id:
                if row.id in explicit_target_ids:
                    selected.append(row)
                else:
                    terminal_main.append(row)
                continue
            status = str(row.candidate_status or "").strip().lower()
            gate = gate_by_id.get(str(row.id))
            review_status = str(getattr(gate, "review_status", "") or "").strip().lower()
            is_rejected = status == "rejected" or "rejected" in review_status
            is_currently_exportable_ml_ready = status in {"ml_ready", "ai_verified_ml_ready"} and bool(
                getattr(gate, "eligible", False)
            )
            should_skip = is_rejected or is_currently_exportable_ml_ready
            if row.paper_id == main_paper_id and should_skip:
                terminal_main.append(row)
                continue
            if row.paper_id != main_paper_id and should_skip:
                continue
            selected.append(row)
        return selected, terminal_main

    def _reviewed_evidence_aggregate(
        self,
        paper: Paper,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
    ) -> dict[str, Any]:
        """Collect every current completed chart-review scope for one main paper."""

        service = EvidenceReviewBundleService(self.session, self.settings)
        completed_scopes: list[tuple[str | None, dict[str, Any]]] = []
        blocked_completed_scopes: list[dict[str, Any]] = []
        pending: dict[str, dict[str, Any]] = {}

        def note_pending_main_scope(task: dict[str, Any], chart_run_id: str | None) -> None:
            for kind, items in (("figure", task.get("figures")), ("table", task.get("tables"))):
                for item in items if isinstance(items, list) else []:
                    record_id = str(item.get("source_record_id") or item.get("id") or "").strip()
                    if not record_id:
                        continue
                    pending[f"{kind}:{record_id}"] = {
                        "object_type": kind,
                        "source_record_id": record_id,
                        "source_paper_id": str(item.get("source_paper_id") or task.get("paper_id") or ""),
                        "source_paper_code": item.get("source_paper_code"),
                        "chart_run_id": chart_run_id,
                        "review_status": task.get("stage_status"),
                    }

        # A DFT package is selected by the main paper, but its evidence can
        # legitimately be in a linked SI record.  Aggregate every completed,
        # current scope for every source document; never let the main paper's
        # completed run certify an unreviewed SI object.
        source_entries = sorted(
            source_by_id.values(),
            key=lambda item: (item["paper"].id != paper.id, str(item["paper"].id)),
        )
        for source in source_entries:
            source_paper = source["paper"]
            is_main = source_paper.id == paper.id
            options = service.get_review_scope_options(source_paper.id)
            run_options = options.get("chart_runs") if isinstance(options.get("chart_runs"), list) else []
            paper_audit = service._latest_review_audit(
                source_paper.id,
                run_id=None,
                actions={"offline_evidence_review_applied"},
            )
            paper_task = options.get("paper_scope") if isinstance(options.get("paper_scope"), dict) else {}
            if paper_task:
                if paper_task.get("stage_status") in FIGURE_TABLE_REVIEW_READY_STATUSES:
                    if paper_audit is not None:
                        completed_scopes.append((None, paper_task))
                elif paper_task.get("completed_snapshot_fingerprint"):
                    blocked_completed_scopes.append(paper_task)
                if (
                    is_main
                    and not run_options
                    and paper_task.get("stage_status") not in FIGURE_TABLE_REVIEW_READY_STATUSES
                ):
                    # The paper scope is the ordinary user flow.  It must be
                    # visible as pending even when there is no historical AI
                    # run, otherwise the DFT summary can misleadingly show 0.
                    note_pending_main_scope(paper_task, None)

            for option in run_options:
                run_id = self._optional_uuid(option.get("chart_run_id"))
                if run_id is None:
                    continue
                if option.get("stage_status") not in FIGURE_TABLE_REVIEW_READY_STATUSES:
                    if option.get("completed_snapshot_fingerprint"):
                        blocked_completed_scopes.append(service.get_review_task(source_paper.id, run_id=run_id))
                    # Pending main-paper coverage is user-visible.  Unreviewed
                    # SI is context only and must not block main-paper DFT gap
                    # discovery or get promoted to reviewed evidence.
                    if is_main and option.get("is_duplicate_representative", True):
                        task = service.get_review_task(source_paper.id, run_id=run_id)
                        note_pending_main_scope(task, str(run_id))
                    continue
                task = service.get_review_task(source_paper.id, run_id=run_id)
                if (
                    task.get("stage_status") in FIGURE_TABLE_REVIEW_READY_STATUSES
                    and task.get("completed_snapshot_fingerprint") == task.get("current_snapshot_fingerprint")
                ):
                    completed_scopes.append((str(run_id), task))

        objects: dict[str, dict[str, Any]] = {}
        review_runs: list[dict[str, Any]] = []
        for run_id, task in completed_scopes:
            run_entry = {
                "chart_run_id": run_id,
                "scope_type": task.get("scope_type") or ("external_analysis_run" if run_id else "paper"),
                "review_status": task.get("stage_status"),
                "completed_snapshot_fingerprint": task.get("completed_snapshot_fingerprint"),
                "reviewed_at": task.get("reviewed_at"),
                "counts": task.get("counts") or {},
                "source_paper_id": task.get("paper_id"),
                "source_paper_code": task.get("paper_code"),
            }
            review_runs.append(run_entry)
            for kind, items in (("figure", task.get("figures")), ("table", task.get("tables"))):
                for item in items if isinstance(items, list) else []:
                    record_id = str(item.get("source_record_id") or item.get("id") or "").strip()
                    if not record_id:
                        continue
                    source_paper_id = str(item.get("source_paper_id") or "").strip()
                    key = f"{kind}:{record_id}"
                    entry = objects.setdefault(
                        key,
                        {
                            "object_type": kind,
                            "source_record_id": record_id,
                            "source_paper_id": source_paper_id,
                            "source_paper_code": item.get("source_paper_code"),
                            "review_runs": [],
                        },
                    )
                    if run_entry not in entry["review_runs"]:
                        entry["review_runs"].append(run_entry)

        for key in set(objects):
            pending.pop(key, None)

        reviewed_payload = sorted(objects.values(), key=lambda item: (item["object_type"], item["source_record_id"]))
        completed_fingerprint = _sha256(_canonical_json_bytes({
            "schema_version": "paper_reviewed_evidence_aggregate_v1",
            "paper_id": str(paper.id),
            "objects": reviewed_payload,
        })) if reviewed_payload else None
        reviewed_main_figures = sum(
            1 for item in reviewed_payload
            if item["object_type"] == "figure" and item["source_paper_id"] == str(paper.id)
        )
        reviewed_main_tables = sum(
            1 for item in reviewed_payload
            if item["object_type"] == "table" and item["source_paper_id"] == str(paper.id)
        )
        return {
            "schema_version": "paper_reviewed_evidence_aggregate_v1",
            "objects": objects,
            "review_runs": review_runs,
            "pending_objects": sorted(pending.values(), key=lambda item: (item["object_type"], item["source_record_id"])),
            "completed_snapshot_fingerprint": completed_fingerprint,
            "blocked_completed_scopes": blocked_completed_scopes,
            "summary": {
                "reviewed_figures": sum(1 for item in reviewed_payload if item["object_type"] == "figure"),
                "reviewed_tables": sum(1 for item in reviewed_payload if item["object_type"] == "table"),
                "reviewed_main_figures": reviewed_main_figures,
                "reviewed_main_tables": reviewed_main_tables,
                "pending_main_figures": sum(
                    1 for item in pending.values()
                    if item["object_type"] == "figure" and item.get("source_paper_id") == str(paper.id)
                ),
                "pending_main_tables": sum(
                    1 for item in pending.values()
                    if item["object_type"] == "table" and item.get("source_paper_id") == str(paper.id)
                ),
                "pending_supporting_figures": sum(
                    1 for item in pending.values()
                    if item["object_type"] == "figure" and item.get("source_paper_id") != str(paper.id)
                ),
                "pending_supporting_tables": sum(
                    1 for item in pending.values()
                    if item["object_type"] == "table" and item.get("source_paper_id") != str(paper.id)
                ),
                "reviewed_supporting_evidence": sum(
                    1 for item in reviewed_payload if item["source_paper_id"] != str(paper.id)
                ),
                "unreviewed_supporting_context": 0,
            },
        }

    @staticmethod
    def _dft_evidence_payload_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
        material_binding = payload.get("material_binding")
        material_binding = material_binding if isinstance(material_binding, dict) else {}
        candidates: list[dict[str, Any]] = [payload]
        for key in ("evidence_anchor", "corrected_value", "source_location", "evidence_location"):
            value = material_binding.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        for key in ("evidence_anchor", "corrected_value", "source_location", "evidence_location"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        candidates.append(material_binding)
        return candidates

    @classmethod
    def _dft_review_evidence_details(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Expose only review-relevant evidence fields from the stored payload."""

        detail_keys = (
            "bond",
            "bond_pair",
            "configuration_index",
            "environment",
            "solvent_complex",
            "method",
            "catalyst_scope",
            "coordination_environment",
            "li_s_bond_length",
            "metal_centers",
            "metal_metal_distance",
            "metal_pairing_type",
            "support_material",
            "srr_lis_intermediate",
        )
        details: dict[str, Any] = {}
        for candidate in cls._dft_evidence_payload_candidates(payload):
            for key in detail_keys:
                value = candidate.get(key)
                if key not in details and value not in (None, "", []):
                    details[key] = cls._sanitize_for_bundle(value)
        return details

    @classmethod
    def _resolve_dft_evidence_source(
        cls,
        row: DFTResult,
        source_by_id: dict[UUID, dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve PDF provenance without changing the DFT writeback owner."""

        payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        candidates = cls._dft_evidence_payload_candidates(payload)

        def first_value(*keys: str) -> Any:
            for candidate in candidates:
                for key in keys:
                    value = candidate.get(key)
                    if value not in (None, "", []):
                        return value
            return None

        explicit_source_paper_id = first_value("source_paper_id")
        explicit_source_paper_code = first_value("source_paper_code")
        source_types = [
            normalize_source_document_type(candidate.get("source_document_type") or candidate.get("source_type"))
            for candidate in candidates
        ]
        source_types = [value for value in source_types if value != "unknown"]
        explicit_source_type = (
            "supplementary_information"
            if "supplementary_information" in source_types
            else source_types[0] if source_types else None
        )

        def source_id_match(value: Any) -> UUID | None:
            value_text = str(value or "").strip()
            if not value_text:
                return None
            for source_id in source_by_id:
                if str(source_id) == value_text:
                    return source_id
            return None

        resolved_id = source_id_match(explicit_source_paper_id)
        if resolved_id is None and explicit_source_paper_code:
            for source_id, source in source_by_id.items():
                if str(source["paper"].paper_code or "").strip() == str(explicit_source_paper_code).strip():
                    resolved_id = source_id
                    break

        row_source = source_by_id.get(row.paper_id)
        if row_source is None:
            raise LookupError(f"DFT row source paper is not in review scope: {row.paper_id}")

        if resolved_id is None and explicit_source_type == "supplementary_information":
            supplementary_sources = [
                source_id
                for source_id, source in source_by_id.items()
                if source.get("source_document_type") == "supplementary_information"
            ]
            if len(supplementary_sources) == 1:
                resolved_id = supplementary_sources[0]

        source = source_by_id.get(resolved_id, row_source)
        item_paper = source_by_id.get(row.paper_id, row_source)

        def first_anchor_value(*keys: str) -> Any:
            for candidate in candidates:
                for key in keys:
                    value = candidate.get(key)
                    if value not in (None, "", []):
                        return value
            return None

        source_location = {
            "page": first_anchor_value("page", "page_start"),
            "section": first_anchor_value("section", "section_title"),
            "figure": first_anchor_value("figure", "figure_label"),
            "table": first_anchor_value("table", "table_label"),
        }
        return {
            "source": source,
            "item_paper_id": str(row.paper_id),
            "item_paper_code": str(item_paper["paper"].paper_code),
            "source_paper_id": str(source["paper"].id),
            "source_paper_code": str(source["paper"].paper_code),
            "source_document_type": source["source_document_type"],
            "evidence_payload": payload,
            "source_location": source_location,
            "explicit_source_document_type": explicit_source_type,
        }

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
            source_resolution = self._resolve_dft_evidence_source(row, source_by_id)
            source = source_resolution["source"]
            evidence = source_resolution["evidence_payload"]
            location = source_resolution["source_location"]
            primary_locator = self._primary_locator(locator_by_target.get(str(row.id), []))
            append_item(
                source=source,
                text=row.evidence_text or evidence.get("quoted_text") or (primary_locator or {}).get("evidence_text") or "",
                payload={
                    "source_record_id": str(row.id),
                    "source_record_type": "dft_result_candidate_evidence",
                    "item_paper_id": source_resolution["item_paper_id"],
                    "item_paper_code": source_resolution["item_paper_code"],
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
                    "item_paper_id": str(section.paper_id),
                    "item_paper_code": source_by_id[section.paper_id]["paper"].paper_code,
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
        reviewed_objects: dict[str, dict[str, Any]],
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
            reviewed = reviewed_objects.get(f"table:{row.id}")
            source = source_by_id[row.paper_id]
            if source["prefix"] == "main" and reviewed is None:
                continue
            if reviewed is None and not self._is_relevant(haystack, anchors["table"]):
                continue
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:table:{counters[source['prefix']]:03d}"
            item = {
                "evidence_id": evidence_id,
                "evidence_kind": "table",
                "source_document_type": source["source_document_type"],
                "source_paper_id": str(row.paper_id),
                "source_paper_code": source["paper"].paper_code,
                "item_paper_id": str(row.paper_id),
                "item_paper_code": source["paper"].paper_code,
                "source_record_id": str(row.id),
                "caption": row.caption,
                "page": row.page,
                "markdown_content": row.markdown_content,
                "content_sha256": _sha256(haystack.encode("utf-8")),
                "review_status": "completed" if reviewed else "unreviewed_context",
                "completed_snapshot_fingerprint": (
                    reviewed["review_runs"][0].get("completed_snapshot_fingerprint") if reviewed else None
                ),
                "review_runs": reviewed.get("review_runs", []) if reviewed else [],
                "eligible_for_auto_apply": bool(reviewed),
                "evidence_tier": (
                    "reviewed_supporting_evidence"
                    if reviewed and source["prefix"] == "si"
                    else "reviewed_main_evidence"
                    if reviewed
                    else "unreviewed_supporting_context"
                ),
            }
            items.append(item)
        return items, {item["evidence_id"]: item for item in items}

    def _figures(
        self,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        figures: list[PaperFigure],
        anchors: dict[str, set[str]],
        reviewed_objects: dict[str, dict[str, Any]],
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
            reviewed = reviewed_objects.get(f"figure:{row.id}")
            source = source_by_id[row.paper_id]
            if source["prefix"] == "main" and reviewed is None:
                continue
            if reviewed is None and not self._is_relevant(haystack, anchors["figure"]):
                continue
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
                "item_paper_id": str(row.paper_id),
                "item_paper_code": source["paper"].paper_code,
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
                "review_status": "completed" if reviewed else "unreviewed_context",
                "completed_snapshot_fingerprint": (
                    reviewed["review_runs"][0].get("completed_snapshot_fingerprint") if reviewed else None
                ),
                "review_runs": reviewed.get("review_runs", []) if reviewed else [],
                "eligible_for_auto_apply": bool(reviewed),
                "evidence_tier": (
                    "reviewed_supporting_evidence"
                    if reviewed and source["prefix"] == "si"
                    else "reviewed_main_evidence"
                    if reviewed
                    else "unreviewed_supporting_context"
                ),
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

    def _resolve_pdf(self, pdf_path: Any) -> Path | None:
        if not pdf_path:
            return None
        return resolve_persisted_artifact_path(
            str(pdf_path),
            category="pdf",
            settings=self.settings,
            trusted_persisted_reference=True,
            must_exist=True,
        )

    @classmethod
    def _dft_row_payload(
        cls,
        row: DFTResult,
        *,
        sample_by_id: dict[UUID, CatalystSample],
        source_by_id: dict[UUID, dict[str, Any]],
        locator_by_target: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        sample = sample_by_id.get(row.catalyst_sample_id) if row.catalyst_sample_id else None
        source_resolution = cls._resolve_dft_evidence_source(row, source_by_id)
        source = source_resolution["source"]
        item_source = source_by_id[row.paper_id]
        locators = list((locator_by_target or {}).get(str(row.id), []))
        primary_locator = cls._primary_locator(locators)
        evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        evidence_details = cls._dft_review_evidence_details(evidence_payload)
        return {
            "target_id": str(row.id),
            "target_type": "dft_results",
            "source_document_type": source["source_document_type"],
            "source_paper_id": source_resolution["source_paper_id"],
            "source_paper_code": source_resolution["source_paper_code"],
            "item_paper_id": str(row.paper_id),
            "item_paper_code": item_source["paper"].paper_code,
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
            **evidence_details,
            "evidence_details": evidence_details,
            "evidence_payload": cls._sanitize_for_bundle(row.evidence_payload),
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



    def _aggregated_curated_evidence_snapshot(
        self,
        *,
        paper: Paper,
        extracted_tables: list[dict[str, Any]],
        extracted_figures: list[dict[str, Any]],
        figure_rag_quality: dict[str, Any],
        reviewed_aggregate: dict[str, Any],
    ) -> dict[str, Any]:
        reviewed_tables = [item for item in extracted_tables if item.get("eligible_for_auto_apply")]
        reviewed_figures = [item for item in extracted_figures if item.get("eligible_for_auto_apply")]
        unreviewed_context = [
            item for item in [*extracted_tables, *self._public_figures(extracted_figures)]
            if item.get("evidence_tier") == "unreviewed_supporting_context"
        ]
        aggregate_fingerprint = reviewed_aggregate.get("completed_snapshot_fingerprint")
        blocked_completed_scopes = reviewed_aggregate.get("blocked_completed_scopes") or []
        has_any_chart_objects = bool(
            self.session.scalar(select(PaperFigure.id).where(PaperFigure.paper_id == paper.id).limit(1))
            or self.session.scalar(select(PaperTable.id).where(PaperTable.paper_id == paper.id).limit(1))
        )
        if blocked_completed_scopes:
            stage_status = (
                "stale"
                if any(task.get("stage_status") == "stale" for task in blocked_completed_scopes)
                else "needs_local_ai"
            )
        elif reviewed_tables or reviewed_figures:
            stage_status = "completed"
        elif not has_any_chart_objects:
            stage_status = "not_required"
            aggregate_fingerprint = aggregate_fingerprint or _sha256(
                _canonical_json_bytes({"paper_id": str(paper.id), "reviewed_objects": []})
            )
        else:
            stage_status = "not_started"
        rag_quality_status = str(figure_rag_quality.get("status") or "ready")
        blocked_rag = next(
            (
                task.get("rag_quality")
                for task in blocked_completed_scopes
                if task.get("rag_quality_status") == "blocked" and isinstance(task.get("rag_quality"), dict)
            ),
            None,
        )
        if isinstance(blocked_rag, dict):
            figure_rag_quality = blocked_rag.get("figures") if isinstance(blocked_rag.get("figures"), dict) else figure_rag_quality
            rag_quality_status = "blocked"
        blocking_errors: list[dict[str, Any]] = []
        if stage_status in {"completed", "needs_local_ai"} and rag_quality_status != "ready":
            stage_status = "needs_local_ai"
            blocking_errors.append({
                "code": "figure_rag_quality_incomplete",
                "message": "One or more reviewed figures are no longer RAG-ready.",
                "blocked_count": figure_rag_quality.get("blocked"),
            })
        snapshot = {
            "schema_version": "curated_figure_table_evidence_snapshot_v2",
            "scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "evidence_review_status": "applied" if stage_status in FIGURE_TABLE_REVIEW_READY_STATUSES else "not_recorded",
            "stage_status": stage_status,
            "rag_quality_status": rag_quality_status,
            "rag_quality": {"figures": figure_rag_quality},
            "blocking_errors": blocking_errors,
            "current_snapshot_fingerprint": aggregate_fingerprint,
            "completed_snapshot_fingerprint": aggregate_fingerprint if stage_status in FIGURE_TABLE_REVIEW_READY_STATUSES else None,
            "snapshot_fingerprint": aggregate_fingerprint,
            "review_runs": reviewed_aggregate.get("review_runs") or [],
            "reviewed_main_evidence": {
                "tables": [item for item in reviewed_tables if item.get("source_paper_id") == str(paper.id)],
                "figures": [item for item in self._public_figures(reviewed_figures) if item.get("source_paper_id") == str(paper.id)],
            },
            "reviewed_supporting_evidence": [
                item for item in [*reviewed_tables, *self._public_figures(reviewed_figures)]
                if item.get("source_paper_id") != str(paper.id)
            ],
            "unreviewed_supporting_context": unreviewed_context,
            "pending_main_evidence": reviewed_aggregate.get("pending_objects") or [],
            "summary": reviewed_aggregate.get("summary") or {},
            # Compatibility aliases contain only reviewed evidence, never unreviewed SI context.
            "tables": reviewed_tables,
            "figures": self._public_figures(reviewed_figures),
        }
        snapshot["review_gate"] = {
            "stage_status": stage_status,
            "allowed_statuses": sorted(FIGURE_TABLE_REVIEW_READY_STATUSES),
            "rag_quality_status": rag_quality_status,
            "rag_quality": {"figures": figure_rag_quality},
            "blocking_errors": blocking_errors,
            "current_snapshot_fingerprint": snapshot["current_snapshot_fingerprint"],
            "completed_snapshot_fingerprint": snapshot["completed_snapshot_fingerprint"],
            "review_run_id": None,
            "reviewed_at": max(
                (str(item.get("reviewed_at") or "") for item in snapshot["review_runs"]),
                default="",
            ) or None,
        }
        return snapshot

    def _curated_evidence_snapshot(
        self,
        *,
        paper: Paper,
        extracted_tables: list[dict[str, Any]],
        extracted_figures: list[dict[str, Any]],
        figure_rag_quality: dict[str, Any],
        chart_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        public_figures = self._public_figures(extracted_figures)
        content_snapshot = compute_figure_table_snapshot(self.session, paper.id)
        evidence_service = EvidenceReviewBundleService(self.session, self.settings)
        chart_task = evidence_service.get_review_task(paper.id, run_id=chart_run_id)
        current_snapshot_fingerprint = str(
            chart_task.get("current_snapshot_fingerprint") or content_snapshot["fingerprint"]
        )
        latest = evidence_service._latest_review_audit(
            paper.id,
            run_id=chart_run_id,
            actions={"offline_evidence_review_applied"},
        )
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
            "scope_type": "external_analysis_run" if chart_run_id else "paper",
            "chart_run_id": str(chart_run_id) if chart_run_id else None,
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
        seen: dict[tuple[str, str, str, str], int] = {}
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
                str(audit.get("temporary_id") or "").strip()
                if str(audit.get("target_id") or "").strip().lower() == "new"
                else "",
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
            matching_ids = [
                str(item.get("evidence_id") or "").strip()
                for item in materials.get("evidence_map", {}).values()
                if self._evidence_relevant_to_dft_target(item, target_id, materials)
            ]
            matching_summary = ", ".join(dict.fromkeys(item for item in matching_ids if item))
            if not matching_summary:
                matching_summary = "none in this package"
            errors.append(
                {
                    "code": "unrelated_evidence_id",
                    "message": (
                        f"target_id '{target_id}' is not supported by the submitted evidence_ids. "
                        "Use a directly matching package evidence_id "
                        f"({matching_summary}); candidate-text evidence still requires local PDF verification before writeback. "
                        "Otherwise do not invent a replacement or request automatic writeback."
                    ),
                }
            )
        if decision in {"REVISE", "new_candidate"}:
            errors.extend(self._validate_structured_dft_value(audit))
        unreviewed_si = [
            item for item in evidence_items
            if item.get("evidence_tier") == "unreviewed_supporting_context"
        ]
        if unreviewed_si and decision != "NEEDS_HUMAN":
            errors.append({
                "code": "unreviewed_supporting_evidence_requires_human",
                "message": "Unreviewed SI context may only support NEEDS_HUMAN and is never eligible for automatic writeback.",
            })
        if decision == "new_candidate":
            if not evidence_items or any(not item.get("eligible_for_auto_apply") for item in evidence_items):
                errors.append({
                    "code": "new_candidate_requires_reviewed_evidence",
                    "message": "new_candidate may cite only completed, non-stale reviewed figure/table evidence.",
                })
            if not has_pdf_evidence_anchor(evidence_items):
                errors.append(
                    {
                        "code": "missing_pdf_evidence_anchor",
                        "message": "new_candidate requires at least one cited evidence_id with a PDF page anchor.",
                    }
                )
            duplicate = self._matching_terminal_candidate(
                audit.get("corrected_value"),
                materials["initial_dft_candidates"].get("existing_terminal_context") or [],
            )
            if duplicate is not None:
                errors.append({
                    "code": "duplicate_existing_terminal_candidate",
                    "message": (
                        "new_candidate duplicates readonly terminal DFT context target_id="
                        + str(duplicate.get("target_id"))
                        + "; return an explicit human dedupe analysis instead of creating another row."
                    ),
                })
            terminal_context = materials["initial_dft_candidates"].get("existing_terminal_context") or []
            dedupe_analysis = audit.get("dedupe_analysis") if isinstance(audit.get("dedupe_analysis"), dict) else {}
            compared_ids = {
                str(item).strip()
                for item in dedupe_analysis.get("compared_target_ids", [])
                if str(item).strip()
            }
            if terminal_context and (
                str(dedupe_analysis.get("conclusion") or "").strip().lower() != "distinct"
                or not str(dedupe_analysis.get("reason") or "").strip()
                or not compared_ids
            ):
                errors.append({
                    "code": "terminal_context_dedupe_analysis_required",
                    "message": "new_candidate must explicitly compare existing_terminal_context and provide conclusion='distinct', reason, and compared_target_ids.",
                })
        return errors

    @classmethod
    def _matching_terminal_candidate(
        cls,
        corrected: Any,
        terminal_rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not isinstance(corrected, dict):
            return None

        def norm(value: Any) -> str:
            return " ".join(str(value or "").strip().lower().split())

        def number(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        corrected_property = norm(corrected.get("property_type") or corrected.get("property") or corrected.get("energy_type"))
        corrected_value = number(corrected.get("value"))
        corrected_unit = norm(corrected.get("unit"))
        if not corrected_property or corrected_value is None or not corrected_unit:
            return None
        for row in terminal_rows:
            if norm(row.get("property_type")) != corrected_property:
                continue
            row_value = number(row.get("value"))
            if row_value is None or abs(row_value - corrected_value) > 1e-9:
                continue
            if norm(row.get("unit")) != corrected_unit:
                continue
            conflicting_identity = False
            for corrected_key, row_key in (
                ("material_identity", "material_identity"),
                ("adsorbate", "adsorbate"),
                ("reaction_step", "reaction_step"),
                ("reaction_type", "reaction_type"),
            ):
                left, right = norm(corrected.get(corrected_key)), norm(row.get(row_key))
                if left and right and left != right:
                    conflicting_identity = True
                    break
            if not conflicting_identity:
                return row
        return None

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
    def _evidence_item_type(item: dict[str, Any]) -> str:
        evidence_kind = str(item.get("evidence_kind") or "").strip().lower()
        if evidence_kind in {"figure", "table"}:
            return evidence_kind
        source_record_type = str(item.get("source_record_type") or "").strip().lower()
        if source_record_type == "paper_section":
            return "section"
        if source_record_type == "dft_result_candidate_evidence":
            return "dft_result"
        raise ValueError(
            f"Evidence '{item.get('evidence_id') or '-'}' has no supported get_codex_item mapping."
        )

    @classmethod
    def _evidence_verification_requirement(cls, item: dict[str, Any]) -> dict[str, Any]:
        evidence_id = str(item.get("evidence_id") or "").strip()
        source_paper_id = str(item.get("source_paper_id") or "").strip()
        source_paper_code = str(item.get("source_paper_code") or "").strip()
        item_paper_id = str(item.get("item_paper_id") or source_paper_id).strip()
        item_paper_code = str(item.get("item_paper_code") or source_paper_code).strip()
        source_record_id = str(item.get("source_record_id") or "").strip()
        source_document_type = str(item.get("source_document_type") or "").strip()
        anchor = first_pdf_evidence_anchor(item) or {}
        page = anchor.get("page")
        missing = [
            name
            for name, value in (
                ("evidence_id", evidence_id),
                ("item_paper_id", item_paper_id),
                ("item_paper_code", item_paper_code),
                ("source_paper_id", source_paper_id),
                ("source_paper_code", source_paper_code),
                ("source_record_id", source_record_id),
                ("page", page),
                ("source_document_type", source_document_type),
            )
            if value in (None, "")
        ]
        if missing:
            raise ValueError(
                f"Evidence '{evidence_id or '-'}' cannot be verified directly; missing: {', '.join(missing)}."
            )
        try:
            normalized_page = int(page)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Evidence '{evidence_id}' has an invalid PDF page: {page!r}.") from exc
        requirement = {
            "evidence_id": evidence_id,
            "source_paper_id": source_paper_id,
            "source_paper_code": source_paper_code,
            "source_record_id": source_record_id,
            "item_type": cls._evidence_item_type(item),
            "page": normalized_page,
            "source_document_type": source_document_type,
        }
        # Keep the historical ordinary-evidence JSON shape stable.  When the
        # DFT row belongs to one paper but its PDF evidence belongs to another
        # (the main-paper-writeback + SI-evidence case), these fields are
        # mandatory so get_codex_item and read_paper_page cannot be conflated.
        if item_paper_id != source_paper_id or item_paper_code != source_paper_code:
            requirement["item_paper_id"] = item_paper_id
            requirement["item_paper_code"] = item_paper_code
        return requirement

    @staticmethod
    def _unique_evidence_checks(checks: Any) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for check in checks:
            if not isinstance(check, dict):
                continue
            evidence_id = str(check.get("evidence_id") or "").strip()
            if evidence_id and evidence_id not in unique:
                unique[evidence_id] = dict(check)
        return [unique[evidence_id] for evidence_id in sorted(unique)]

    @staticmethod
    def _unique_page_checks(checks: Any) -> list[dict[str, Any]]:
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for check in checks:
            if not isinstance(check, dict):
                continue
            paper_id = str(check.get("source_paper_id") or check.get("paper_id") or "").strip()
            try:
                page = int(check.get("page"))
            except (TypeError, ValueError):
                continue
            key = (paper_id, page)
            if not paper_id or key in unique:
                continue
            unique[key] = {
                "source_paper_id": paper_id,
                "source_paper_code": check.get("source_paper_code"),
                "page": page,
                "source_document_type": check.get("source_document_type"),
            }
        return [unique[key] for key in sorted(unique)]

    @classmethod
    def _live_public_payload(cls, value: Any) -> Any:
        """Strip filesystem/private implementation details from live task data."""

        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                public_key = str(key)
                lowered = public_key.strip().lower()
                if (
                    public_key.startswith("_")
                    or "byte" in lowered
                    or lowered.endswith("_path")
                    or lowered in {"path", "local_path", "server_path"}
                ):
                    continue
                public_value = cls._live_public_payload(item)
                if public_value is not _LIVE_TASK_OMIT:
                    cleaned[public_key] = public_value
            return cleaned
        if isinstance(value, list):
            return [
                public_value
                for item in value
                if (public_value := cls._live_public_payload(item)) is not _LIVE_TASK_OMIT
            ]
        if isinstance(value, tuple):
            return [
                public_value
                for item in value
                if (public_value := cls._live_public_payload(item)) is not _LIVE_TASK_OMIT
            ]
        if isinstance(value, set):
            return [
                public_value
                for item in sorted(value, key=str)
                if (public_value := cls._live_public_payload(item)) is not _LIVE_TASK_OMIT
            ]
        if isinstance(value, (bytes, bytearray, memoryview, Path)):
            return _LIVE_TASK_OMIT
        if isinstance(value, UUID):
            return str(value)
        return value

    @classmethod
    def _live_target_payload(cls, item: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "target_id",
            "target_type",
            "item_paper_id",
            "item_paper_code",
            "source_document_type",
            "source_paper_id",
            "source_paper_code",
            "catalyst_sample_id",
            "material_identity",
            "adsorbate",
            "property_type",
            "value",
            "value_upper",
            "value_kind",
            "value_type",
            "unit",
            "reaction_step",
            "reaction_type",
            "bond",
            "bond_pair",
            "configuration_index",
            "environment",
            "solvent_complex",
            "method",
            "catalyst_scope",
            "coordination_environment",
            "li_s_bond_length",
            "metal_centers",
            "metal_metal_distance",
            "metal_pairing_type",
            "support_material",
            "srr_lis_intermediate",
            "evidence_details",
            "source_section",
            "source_figure",
            "evidence_text",
            "confidence",
            "candidate_status",
            "primary_evidence_locator",
            "readonly",
            "eligible_as_write_target",
            "is_ml_ready",
            "is_rejected",
            "review_status",
        }
        return cls._live_public_payload(
            {key: value for key, value in item.items() if key in allowed}
        )

    @classmethod
    def _live_evidence_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        anchor = first_pdf_evidence_anchor(item) or {}
        quoted_text = cls._evidence_quote(item)
        public_item = cls._live_public_payload(item)
        public_item.pop("evidence_payload", None)
        public_item.pop("evidence_locators", None)
        public_item.update(
            {
                "item_type": cls._evidence_item_type(item),
                "item_paper_id": item.get("item_paper_id") or item.get("source_paper_id"),
                "item_paper_code": item.get("item_paper_code") or item.get("source_paper_code"),
                "source_paper_id": item.get("source_paper_id"),
                "source_paper_code": item.get("source_paper_code"),
                "source_document_type": item.get("source_document_type"),
                "source_record_id": item.get("source_record_id"),
                "page": anchor.get("page") or item.get("page") or item.get("page_start"),
                "figure": item.get("figure") or item.get("figure_label"),
                "table": item.get("table") or item.get("caption"),
                "section": item.get("section") or item.get("section_title"),
                "original_text": quoted_text,
                "quoted_text": quoted_text,
            }
        )
        return cls._live_public_payload(public_item)

    def _live_import_analysis_template(
        self,
        *,
        materials: dict[str, Any],
        review_source: dict[str, Any],
        target_evidence_map: dict[str, list[str]],
        evidence_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = materials["paper_metadata"]
        raw_evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in materials["evidence_map"].values()
            if str(item.get("evidence_id") or "").strip()
        }
        required_evidence_checks: list[dict[str, Any]] = []
        for item in raw_evidence_by_id.values():
            try:
                required_evidence_checks.append(self._evidence_verification_requirement(item))
            except ValueError:
                continue
        unique_evidence_checks = self._unique_evidence_checks(required_evidence_checks)
        unique_page_checks = self._unique_page_checks(unique_evidence_checks)
        audits: list[dict[str, Any]] = []
        for target_id in sorted(target_evidence_map):
            evidence_ids = list(target_evidence_map[target_id])
            primary = raw_evidence_by_id.get(evidence_ids[0]) if evidence_ids else None
            primary_public = self._live_evidence_item(primary) if primary else {}
            audits.append(
                {
                    "paper_id": metadata["paper_id"],
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "dft_results",
                    "decision": None,
                    "evidence_checked": False,
                    "evidence_ids": evidence_ids,
                    "evidence_location": {
                        "source_document_type": primary_public.get("source_document_type"),
                        "source_paper_id": primary_public.get("source_paper_id"),
                        "source_paper_code": primary_public.get("source_paper_code"),
                        "source_record_id": primary_public.get("source_record_id"),
                        "page": primary_public.get("page"),
                        "section": primary_public.get("section"),
                        "figure": primary_public.get("figure"),
                        "table": primary_public.get("table"),
                        "quoted_text": primary_public.get("quoted_text"),
                        "evidence_ids": evidence_ids,
                        "bundle_fingerprint": materials["bundle_fingerprint"],
                        "figure_table_completed_snapshot_fingerprint": materials[
                            "curated_evidence_snapshot"
                        ].get("completed_snapshot_fingerprint"),
                    },
                    "recommended_action": None,
                    "corrected_value": None,
                    "confidence": None,
                    "reason": None,
                    "source": "local_ai",
                    "source_label": review_source["reviewer_label"],
                    "agent_role": "local_ai_pdf_verifier",
                    "requires_local_ai_verification": True,
                    "local_ai_verification": {
                        "verified_against_pdf": False,
                        "required_tools": list(LOCAL_AI_REQUIRED_TOOLS),
                        "checked_evidence_ids": [],
                        "checked_pages": [],
                        "verification_note": None,
                    },
                    "writes_final_truth": False,
                    "confirmation_required": True,
                }
            )
        review_template = self._return_template(materials)
        review_template["review_source"] = deepcopy(review_source)
        review_metadata = {
            "schema_version": review_template["schema_version"],
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper_id": metadata["paper_id"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "catalyst_sample_id": materials["review_selection"]["catalyst_sample_id"],
            "dft_result_ids": materials["review_selection"]["dft_result_ids"],
            "review_mode": materials["review_mode"],
            "figure_table_completed_snapshot_fingerprint": materials["curated_evidence_snapshot"].get(
                "completed_snapshot_fingerprint"
            ),
            "paper_code": metadata["paper_code"],
            "overall_status": review_template["overall_status"],
            "web_ai_review_source": deepcopy(review_source),
            "review_source": deepcopy(review_source),
            "local_ai_verification_required": True,
            "required_local_ai_tools": list(LOCAL_AI_REQUIRED_TOOLS),
            "local_ai_verification_reuse_policy": (
                "Each audit must record complete coverage. One successful get_codex_item result may be reused for "
                "the same evidence_id, and one successful read_paper_page result may be reused for the same "
                "(source_paper_id, page)."
            ),
        }
        return {
            "paper_id": metadata["paper_id"],
            "source": "local_ai",
            "source_label": review_source["reviewer_label"],
            "reviewer": review_source["reviewer_label"],
            "auto_apply_review_rules": True,
            "raw_payload": {
                "review_metadata": review_metadata,
                "local_ai_verification_plan": {
                    "unique_evidence_checks": unique_evidence_checks,
                    "unique_page_checks": unique_page_checks,
                    "evidence_check_count": len(unique_evidence_checks),
                    "page_check_count": len(unique_page_checks),
                },
                "coverage_acknowledgement": review_template["coverage_acknowledgement"],
                "object_review_audits": audits,
                "review_notes": [],
            },
        }

    @staticmethod
    def _local_ai_writeback_contract() -> dict[str, Any]:
        export_authorization = {
            "decisions": ["PASS", "REVISE"],
            "required_recommended_action": "ready_for_ml_export",
            "otherwise": "not_authorized",
        }
        return {
            "dft_web_ai_is_suggestion_only": True,
            "required_tools": list(LOCAL_AI_REQUIRED_TOOLS),
            "export_authorization": export_authorization,
            "reuse_policy": {
                "get_codex_item": "one successful result may be reused for the same evidence_id",
                "read_paper_page": "one successful result may be reused for the same (source_paper_id, page)",
                "per_audit_coverage_record_still_required": True,
            },
            "required_local_ai_verification": {
                "verified_against_pdf": True,
                "used_tools_include": list(LOCAL_AI_REQUIRED_TOOLS),
                "get_codex_item_arguments": {
                    "paper_id": "item_paper_id (or source_paper_id for ordinary same-source evidence)",
                    "item_id": "source_record_id",
                },
                "read_paper_page_arguments": {
                    "paper_id": "source_paper_id",
                    "page": "page",
                },
                "checked_evidence_ids_cover": "all server-derived evidence_ids for this audit",
                "checked_pages_cover": "all server-derived (source_paper_id, page) pairs for this audit",
                "verification_note_required": True,
                "pdf_evidence_semantics": (
                    "read_paper_page reads stored database layout. verified_against_pdf also requires judgment "
                    "against the source PDF/page evidence included in the review bundle."
                ),
            },
            "writeback": {
                "tool": "import_analysis",
                "auto_apply_review_rules": True,
                "source": "local_ai",
                "export_authorization": export_authorization,
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
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "catalyst_sample_id": materials["review_selection"]["catalyst_sample_id"],
            "dft_result_ids": materials["review_selection"]["dft_result_ids"],
            "review_mode": materials["review_mode"],
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
                "missing_data_search_complete": False,
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
            "mode": materials["review_mode"],
            "target_ids": sorted(materials["target_dft_result_ids"]),
            "expected_count": len(materials["target_dft_result_ids"]),
            "completion_rule": (
                "Complete both tasks in this one package: return exactly one audit for every existing target_id, then "
                "scan every eligible packaged evidence item and append every genuinely missing DFT result as new_candidate. "
                "An empty target list still requires the missing-data scan."
            ),
            "targets": targets,
            "mandatory_discovery_pass": {
                "required": True,
                "scope": "all packaged evidence with eligible_for_auto_apply=true",
                "completion_field": "coverage_acknowledgement.missing_data_search_complete",
                "instruction": (
                    "After reviewing existing targets, inspect all eligible text, table, and figure evidence for DFT "
                    "results absent from both writable targets and existing_terminal_context."
                ),
            },
            "new_candidate_rule": {
                "target_type": "dft_results",
                "target_id": "new",
                "temporary_id": "new-dft-001",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "when_to_use": "Only when reviewed evidence contains a DFT result missing from both writable targets and existing_terminal_context.",
                "corrected_value_required_fields": ["material_identity", "property_type", "value", "unit"],
                "multiple_new_candidates": "Use target_id='new' for each new candidate and give each one a unique temporary_id.",
            },
        }

    @staticmethod
    def _output_rules(materials: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "offline_dft_review_output_rules_v1",
            "output_workflow": {
                "input_template": "WEB_AI_FILL_THIS.json",
                "schema": "return_schema.json",
                "output_filename": f"{materials['paper_metadata']['paper_code']}_web_ai_result.json",
                "output_type": "single_json_file_attachment",
                "reply_as_file_attachment": True,
                "do_not_generate_from_scratch": True,
                "do_not_wrap_in_markdown": True,
            },
            "immutable_fields": [
                "schema_version",
                "bundle_fingerprint",
                "chart_scope_type",
                "chart_run_id",
                "review_mode",
                "figure_table_completed_snapshot_fingerprint",
                "paper_id",
                "paper_code",
            ],
            "audit_variants": {
                "existing_target": {
                    "target_id": "must be a real ID from parsed/dft_review_checklist.json",
                    "decision_allowed": ["PASS", "REVISE", "REJECT", "NEEDS_HUMAN"],
                    "decision_forbidden": ["new_candidate"],
                },
                "new_candidate": {
                    "target_id": "new",
                    "decision_required": "new_candidate",
                    "field_name_required": "dft_results",
                    "temporary_id_required": True,
                    "corrected_value_required_fields": ["material_identity", "property_type", "value", "unit"],
                },
            },
            "hard_invariants": [
                "target_id='new' if and only if decision='new_candidate'",
                "decision='REVISE' requires corrected_value",
                "every object_review_audits item must cite real package evidence_ids",
                "unreviewed_supporting_context cannot alone support PASS, REVISE, REJECT, or new_candidate",
                "overall_status='completed' requires coverage_acknowledgement.missing_data_search_complete=true",
                "do not change immutable fields copied from WEB_AI_FILL_THIS.json",
            ],
            "final_self_check": [
                "Parse the output as JSON after writing it",
                "Validate it against return_schema.json",
                "Search every target_id='new' item and confirm decision='new_candidate'",
                "Search every PASS/REVISE/REJECT/NEEDS_HUMAN item and confirm target_id is a real existing target ID",
                "Confirm every eligible packaged evidence item was checked for missing DFT data before setting missing_data_search_complete=true",
                "Return only the completed JSON file",
            ],
        }

    @staticmethod
    def _start_here(materials: dict[str, Any]) -> str:
        paper_code = materials["paper_metadata"]["paper_code"]
        return f"""# START HERE — fill the supplied JSON file

Do not create a new response structure from memory.

1. Open `WEB_AI_FILL_THIS.json`.
2. Keep its fingerprint, paper, scope, mode, and schema fields unchanged.
3. Fill only the review values and `object_review_audits` array.
4. Review every existing target, then scan every eligible text/table/figure item for missing DFT data and append each distinct result as `new_candidate`.
5. Only after both passes finish, set `coverage_acknowledgement.missing_data_search_complete=true`.
6. Read `OUTPUT_RULES.json` before adding any audit item and validate the completed object against `return_schema.json`.
7. Save it as `{paper_code}_web_ai_result.json` and reply by attaching that one JSON file.

Critical relation: `target_id="new"` is valid only with `decision="new_candidate"`.
For `PASS`, `REVISE`, `REJECT`, or `NEEDS_HUMAN`, use a real existing target ID from `parsed/dft_review_checklist.json`.

Do not paste the JSON body into the chat. Do not return Markdown, prose, a code fence, `format_examples.json`, or an outer wrapper.
"""

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
                            "temporary_id": "new-dft-001",
                            "decision": "new_candidate",
                            "corrected_value": {
                                "material_identity": "Co-N4/G",
                                "property_type": "free_energy",
                                "value": 0.42,
                                "unit": "eV",
                                "reaction_step": "Li2S2 to Li2S",
                            },
                            "dedupe_analysis": {
                                "compared_target_ids": ["<terminal target_id when present>"],
                                "conclusion": "distinct",
                                "reason": "Material/property/value/reaction identity differs from the listed terminal rows.",
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
        mode = materials["review_mode"]
        summary = materials["evidence_summary"]
        return f"""# Literature AI 离线 DFT 核验任务

目标文献：`{metadata['paper_code']}`（paper_id=`{metadata['paper_id']}`）

审核模式：`{mode}`。已审核证据：图 {summary['reviewed_figures']}、表 {summary['reviewed_tables']}；待补充主文图 {summary['pending_main_figures']}、主文表 {summary['pending_main_tables']}；SI 原始线索 {summary['unreviewed_supporting_context']}。

你是审核建议来源，不是数据库执行者。你没有 MCP、数据库、服务器或外部检索工具，只能使用本压缩包中的材料。

## 必须遵守

0. 从 `WEB_AI_FILL_THIS.json` 开始，直接在该对象中填写；禁止脱离模板重新生成结构。先读 `OUTPUT_RULES.json`，完成后按 `return_schema.json` 自检。把结果保存为 `{metadata['paper_code']}_web_ai_result.json`，并以文件附件回复；不要把长 JSON 正文粘贴到聊天消息中。
1. 只核验当前这一篇主文献及包内相关支撑信息（SI）的 DFT 数据、计算参数、图表和文字证据。
2. 不得猜测。证据不足、材料身份不清或来源冲突时，使用 `NEEDS_HUMAN`，并写入 `uncertainties`。
3. 每条 `object_review_audits` 都必须引用一个或多个真实 `evidence_ids`。证据编号来自 `manifest.json` 和 `evidence/`，且必须和该 DFT 目标直接相关。
4. 本包是一次性 `comprehensive_review`：先对每个已有主文献 DFT candidate 提交 `PASS`、`REVISE`、`REJECT` 或 `NEEDS_HUMAN`，再扫描包内全部 `eligible_for_auto_apply=true` 的正文、图、表证据，追加所有确认漏提且不重复的 `new_candidate`。
5. 必须为 `manifest.json` 的每个 `target_dft_result_id` 提交 1 条审核结果；即使 expected_target_ids 为空，也必须执行全证据查漏。两步都完成后才可设置 `coverage_acknowledgement.missing_data_search_complete=true` 和 `overall_status=completed`。查漏前必须读取 `existing_terminal_context` 做去重。
6. 发现漏项时使用 `decision="new_candidate"`、`target_type="dft_results"`、`target_id="new"`、`field_name="dft_results"`，并为每个新增候选填写唯一 `temporary_id`；若存在终态上下文，还必须填写 `dedupe_analysis={{compared_target_ids:[...], conclusion:"distinct", reason:"..."}}`。`corrected_value` 至少包含 `material_identity`、`property_type`、`value`、`unit`。反向同样成立：`target_id="new"` 时 `decision` 必须是 `new_candidate`；PASS/REVISE/REJECT/NEEDS_HUMAN 必须使用 checklist 中的真实已有 target_id。
7. `reviewed_supporting_evidence` 中的 SI 可作为已审核支撑；`unreviewed_supporting_context` 只能用于发现线索或返回 `NEEDS_HUMAN`，其 `eligible_for_auto_apply=false`，不能单独支持 PASS、REVISE 或 new_candidate。
8. 不得声称已写数据库、已入库、已确认、已 verified 或已成为 ML_Ready。
9. 严格按 `return_schema.json` 输出一个 JSON 对象；不要修改 schema，不要用自由散文替代 JSON，也不要包 Markdown 代码块。
10. 保留 `return_template.json` 中的 `bundle_fingerprint`、`figure_table_completed_snapshot_fingerprint`、`paper_id`、`paper_code` 原值。
11. 更新 `coverage_acknowledgement.reviewed_target_ids`、`coverage_acknowledgement.coverage_complete` 和 `coverage_acknowledgement.missing_data_search_complete`；服务器会重新计算已有目标覆盖率，并要求明确确认全证据查漏已完成，任一步未完成都不会生成导入请求。
12. `format_examples.json` 只是格式示例；不要照抄示例 ID，也不要输出示例文件外层 wrapper。最终只输出 `return_template.json` 的对象结构。

## 材料顺序

先读 `START_HERE.md`、`WEB_AI_FILL_THIS.json`、`OUTPUT_RULES.json`、`manifest.json`、`parsed/paper_metadata.json`，然后逐份读取 `source/main.pdf` 和 `source/si/*.pdf` 原始 PDF；再读 `parsed/initial_dft_candidates.json`、`parsed/dft_review_checklist.json`、`format_examples.json`、`parsed/curated_figure_table_evidence_snapshot.json`、`evidence/text_snippets.jsonl`、相关表格和图片。`parsed/extracted_*.json` 提供证据编号与来源映射。

如果 `curated_figure_table_evidence_snapshot.json` 的 `stage_status` 不是 `completed` 或 `not_required`，或 `rag_quality_status=blocked`，说明已审核证据聚合尚不可用；此时不要继续产出 DFT JSON。不得用某一个已完成 run 为其他未审核图表放行。
必须先核验原始主文和全部 SI PDF，再逐条核验已有候选，最后扫描正文、SI、全部已审核图表寻找漏项；每个 `new_candidate` 必须绑定真实 PDF 页码和 evidence_id。不得从曲线估读，不得把实验数据当成 DFT 结果。

最终只回复一个符合 `return_schema.json` 的 JSON 文件附件，不要在聊天正文粘贴 JSON。
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
