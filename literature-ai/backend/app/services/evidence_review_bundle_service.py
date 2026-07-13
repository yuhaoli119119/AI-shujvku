from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, DFTResult, EvidenceLocator, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper, PaperFigure, PaperTable, utcnow
from app.schemas.evidence_review_bundle import (
    OfflineEvidenceReviewFigureAction,
    OfflineEvidenceReviewResult,
    OfflineEvidenceReviewTableAction,
)
from app.services.figure_rag_quality import build_figure_rag_quality_summary
from app.services.figure_review_scope import (
    include_figure_in_chart_review_scope,
)
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.review_bundle_shared import compact_figure_artifact, linked_source_papers
from app.services.source_pdf_inventory import build_source_pdf_inventory, public_source_pdf_inventory
from app.services.review_service import ReviewService
from app.services.table_curation_service import TableCurationService
from app.services.task_log_service import TaskLogService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.figure_summary import (
    figure_summary_echoes_caption,
    flatten_figure_key_elements,
    normalize_figure_content_summary,
    normalize_figure_key_elements,
)


OFFLINE_EVIDENCE_REVIEW_BUNDLE_SCHEMA_VERSION = "offline_figure_table_evidence_bundle_v1"
MAX_FIGURE_FILES = 80
MAX_TOTAL_FIGURE_BYTES = 80 * 1024 * 1024
MAX_SOURCE_PDF_COUNT = 8
MAX_TOTAL_SOURCE_PDF_BYTES = 160 * 1024 * 1024
FIGURE_AUTO_CONFIDENCE = 0.70
TABLE_AUTO_CONFIDENCE = 0.75

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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized or fallback


def _short_source(value: str | None) -> str:
    text = str(value or "offline_evidence_review").strip() or "offline_evidence_review"
    return text[:64]


def _payload_sha256(payload: Any) -> str:
    return _sha256(_canonical_json_bytes(payload))


def pending_needs_human_actions_from_review_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    applied = response.get("applied") if isinstance(response, dict) else None
    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in applied or []:
        if not isinstance(item, dict) or item.get("action") != "NEEDS_HUMAN":
            continue
        category = str(item.get("category") or "unknown")
        target_id = str(item.get("target_id") or "")
        key = (category, target_id)
        if key in seen:
            continue
        seen.add(key)
        result = item.get("table_result") if isinstance(item.get("table_result"), dict) else {}
        evidence = result.get("evidence_payload") if isinstance(result.get("evidence_payload"), dict) else {}
        pending.append(
            {
                "code": "needs_human_pending",
                "category": category,
                "action": "NEEDS_HUMAN",
                "target_id": target_id or None,
                "blocked_reasons": ["needs_human", "local_ai_review_required", "confirmation_required"],
                "reason": evidence.get("reason") or "NEEDS_HUMAN is pending and cannot complete the chart review stage.",
                "requires_local_ai": True,
            }
        )
    return pending


FIGURE_REVIEWED_ACTIONS = {
    "KEEP",
    "RECROP",
    "CREATE",
    "RECROP_FIGURE",
    "CREATE_FIGURE_FROM_BBOX",
}
TABLE_REVIEWED_ACTIONS = {
    "KEEP",
    "UPDATE",
    "CREATE",
    "MERGE",
    "CREATE_TABLE",
    "UPDATE_TABLE",
    "MERGE_TABLE",
}
POSITIVE_REVIEW_DECISIONS = {"PASS", "APPROVE", "APPROVED", "ACCEPT", "ACCEPTED", "VERIFIED", "OK"}
FINALIZED_REVIEW_STATUSES = {"ai_reviewed", "ai_applied", "materialized"}


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


class EvidenceReviewBundleService:
    """Build, validate, and auto-apply offline figure/table evidence review packages."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def build_zip(
        self,
        paper_id: UUID,
        *,
        run_id: UUID | None = None,
        include_pdf_files: bool = True,
        include_figure_files: bool = True,
    ) -> dict[str, Any]:
        if not include_pdf_files:
            raise ValueError("source_pdfs_required_for_comprehensive_review")
        materials = self._build_materials(paper_id, run_id=run_id)
        files: dict[str, bytes] = {
            "parsed/paper_metadata.json": _json_bytes(materials["paper_metadata"]),
            "parsed/source_documents.json": _json_bytes(self._public_source_documents(materials["source_documents"])),
            "parsed/extracted_figures.json": _json_bytes(self._public_records(materials["extracted_figures"])),
            "parsed/extracted_tables.json": _json_bytes(self._public_records(materials["extracted_tables"])),
            "source/page_geometry.json": _json_bytes(materials["page_geometry"]),
            "return_schema.json": _json_bytes(OfflineEvidenceReviewResult.model_json_schema()),
        }

        for table in materials["extracted_tables"]:
            filename = _safe_name(table["evidence_id"].replace(":", "_"), "table") + ".md"
            files[f"evidence/tables/{filename}"] = self._table_markdown(table).encode("utf-8")

        effective_pdf_inventory = materials["source_pdf_inventory"]
        pdf_warnings = [
            f"{item['omitted_reason']}:{item.get('paper_code') or item.get('paper_id')}"
            for item in effective_pdf_inventory
            if item.get("omitted_reason")
        ]
        for item in effective_pdf_inventory:
            if item.get("included_in_bundle"):
                files[str(item["bundle_file"])] = Path(str(item["_pdf_abs_path"])).read_bytes()
        pdf_count = sum(1 for item in effective_pdf_inventory if item.get("included_in_bundle"))
        pdf_bytes_total = sum(int(item.get("size_bytes") or 0) for item in effective_pdf_inventory if item.get("included_in_bundle"))
        materials["paper_metadata"]["source_pdf_inventory"] = public_source_pdf_inventory(effective_pdf_inventory)

        figure_warnings: list[str] = []
        figure_file_count = 0
        figure_bytes_total = 0
        figure_original_bytes_total = 0
        omitted_files: list[dict[str, Any]] = []
        if include_figure_files:
            for figure in materials["extracted_figures"]:
                artifact = self._private_path(figure.get("_image_abs_path"))
                if artifact is None:
                    omitted_files.append({"kind": "image", "source_record_id": figure.get("source_record_id"), "reason": "missing_image"})
                    continue
                if figure_file_count >= MAX_FIGURE_FILES:
                    figure_warnings.append("figure_file_limit_reached")
                    omitted_files.append({"kind": "image", "source_record_id": figure.get("source_record_id"), "reason": "figure_file_limit_reached"})
                    break
                original_size = artifact.stat().st_size
                data, suffix, compacted = compact_figure_artifact(artifact)
                if figure_bytes_total + len(data) > MAX_TOTAL_FIGURE_BYTES:
                    figure_warnings.append("figure_byte_limit_reached")
                    omitted_files.append({"kind": "image", "source_record_id": figure.get("source_record_id"), "reason": "figure_byte_limit_reached"})
                    continue
                filename = _safe_name(figure["evidence_id"].replace(":", "_"), "figure") + suffix
                files[f"evidence/figures/{filename}"] = data
                figure["bundle_file"] = f"evidence/figures/{filename}"
                figure["bundle_image_format"] = suffix.lstrip(".")
                figure["bundle_image_compacted"] = compacted
                figure["bundle_image_original_size_bytes"] = original_size
                figure["bundle_image_size_bytes"] = len(data)
                figure_file_count += 1
                figure_bytes_total += len(data)
                figure_original_bytes_total += original_size

        for item in effective_pdf_inventory:
            if not item.get("included_in_bundle"):
                omitted_files.append({"kind": "pdf", "source_paper_id": item.get("paper_id"), "reason": item.get("omitted_reason") or "pdf_not_included"})

        files["parsed/source_documents.json"] = _json_bytes(self._public_source_documents(materials["source_documents"]))
        files["parsed/paper_metadata.json"] = _json_bytes(materials["paper_metadata"])
        files["parsed/extracted_figures.json"] = _json_bytes(self._public_records(materials["extracted_figures"]))
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
        bundle_id = str(uuid.uuid4())
        manifest = {
            "schema_version": OFFLINE_EVIDENCE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "bundle_type": "chart_field_review" if run_id else "figure_table_evidence_review",
            "generated_at": datetime.now(UTC).isoformat(),
            "scope_type": "external_analysis_run" if run_id else "paper",
            "run_id": str(run_id) if run_id else None,
            "chart_run_id": str(run_id) if run_id else None,
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper": {
                "paper_id": materials["paper_metadata"]["paper_id"],
                "paper_code": materials["paper_metadata"]["paper_code"],
                "title": materials["paper_metadata"].get("title"),
            },
            "review_scope": "single_paper_main_all_plus_dft_related_supplementary_figures_and_all_tables",
            "source_pdf_inventory": materials["paper_metadata"]["source_pdf_inventory"],
            "excluded_duplicate_figures": materials["excluded_duplicate_figures"],
            "field_writeback": ["key_elements", "content_summary", "figure_role", "caption", "page", "crop_status"],
            "expected_coverage": {
                "figure_ids": sorted(materials["figure_id_map"]),
                "table_ids": sorted(materials["table_id_map"]),
                "source_paper_ids": sorted(materials["source_paper_ids"]),
            },
            "target_figure_snapshots": self._public_records(materials["extracted_figures"], include_bundle_file=False),
            "target_table_snapshots": self._public_records(materials["extracted_tables"], include_bundle_file=False),
            "source_page_geometry": materials["page_geometry"],
            "auto_apply_policy": {
                "local_ai_role": "evidence_verification_and_atomic_resolution",
                "figure_auto_confidence_min": FIGURE_AUTO_CONFIDENCE,
                "table_auto_confidence_min": TABLE_AUTO_CONFIDENCE,
                "auto_applies": ["figure KEEP metadata", "figure RECROP", "figure CREATE", "table UPDATE", "table CREATE"],
                "local_ai_verified_actions": ["table MERGE", "table DELETE", "figure REJECT", "low-confidence actions"],
                "pending_actions": [
                    "NEEDS_HUMAN always remains unresolved until local AI or the user resolves it to KEEP/UPDATE/MERGE/DELETE/REJECT"
                ],
            },
            "counts": {
                "source_documents": len(materials["source_documents"]),
                "figures": len(materials["extracted_figures"]),
                "tables": len(materials["extracted_tables"]),
                "included_source_pdfs": pdf_count,
                "included_figure_files": figure_file_count,
                "original_figure_bytes": figure_original_bytes_total,
                "compressed_figure_bytes": figure_bytes_total,
                "excluded_duplicate_figures": len(materials["excluded_duplicate_figures"]),
            },
            "source_document_count": len(materials["source_documents"]),
            "chart_counts": {
                "main_figures": sum(1 for item in materials["extracted_figures"] if item.get("source_document_type") == "main_text"),
                "main_tables": sum(1 for item in materials["extracted_tables"] if item.get("source_document_type") == "main_text"),
                "si_figures": sum(1 for item in materials["extracted_figures"] if item.get("source_document_type") == "supplementary_information"),
                "si_tables": sum(1 for item in materials["extracted_tables"] if item.get("source_document_type") == "supplementary_information"),
            },
            "pdf_files": {"count": pdf_count, "bytes": pdf_bytes_total},
            "image_files": {"count": figure_file_count, "original_bytes": figure_original_bytes_total, "compressed_bytes": figure_bytes_total},
            "omitted_files": omitted_files,
            "warnings": sorted(set(materials["warnings"] + pdf_warnings + figure_warnings)),
            "retention_policy": "generated_in_memory_not_persisted_on_server",
            "files": inventory,
        }
        files["manifest.json"] = _json_bytes(manifest)

        self.session.add(AuditLog(
            paper_id=paper_id,
            action="chart_review_bundle_generated",
            source="web_ui",
            target_type="chart_review_bundle",
            target_id=bundle_id,
            payload={
                "bundle_id": bundle_id,
                "scope_type": manifest["scope_type"],
                "run_id": manifest["run_id"],
                "bundle_fingerprint": manifest["bundle_fingerprint"],
                "target_figure_ids": manifest["expected_coverage"]["figure_ids"],
                "target_table_ids": manifest["expected_coverage"]["table_ids"],
            },
            created_at=utcnow(),
        ))
        self.session.flush()

        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for path, data in sorted(files.items()):
                archive.writestr(path, data)

        paper_code = _safe_name(materials["paper_metadata"]["paper_code"], "paper")
        return {
            "filename": f"{paper_code}_figure_table_evidence_review_bundle.zip",
            "content": buffer.getvalue(),
            "manifest": manifest,
            "bundle_id": bundle_id,
        }

    def validate_result(
        self,
        paper_id: UUID,
        raw_payload: dict[str, Any],
        *,
        run_id: UUID | None = None,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        try:
            result = OfflineEvidenceReviewResult.model_validate(raw_payload)
        except ValidationError as exc:
            return {
                "valid": False,
                "stage_status": "invalid",
                "apply_ready": False,
                "errors": [
                    {
                        "code": "schema_validation_error",
                        "path": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ],
                "warnings": [],
                "execution_plan": [],
                "auto_apply_count": 0,
                "needs_confirmation_count": 0,
                "unresolved_count": 0,
                "unresolved_actions": [],
            }

        payload_run_id = self._payload_run_id(raw_payload)
        if run_id is not None and payload_run_id != run_id:
            return {"valid": False, "stage_status": "invalid", "apply_ready": False,
                    "errors": [{"code": "run_scope_mismatch", "message": "run_id does not match the requested chart review scope"}],
                    "warnings": [], "execution_plan": [], "auto_apply_count": 0,
                    "needs_confirmation_count": 0, "unresolved_count": 0, "unresolved_actions": []}
        run_id = payload_run_id
        materials = self._build_materials(paper_id, run_id=run_id)
        metadata = materials["paper_metadata"]
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        execution_plan: list[dict[str, Any]] = []

        def add_error(code: str, message: str, *, action_ref: str | None = None) -> None:
            item: dict[str, Any] = {"code": code, "message": message}
            if action_ref is not None:
                item["action_ref"] = action_ref
            errors.append(item)

        if result.paper_id != metadata["paper_id"]:
            add_error("paper_id_mismatch", "review_result paper_id does not match the selected paper")
        if result.paper_code != metadata["paper_code"]:
            add_error("paper_code_mismatch", "review_result paper_code does not match the selected paper")
        expected_scope = "external_analysis_run" if run_id else "paper"
        if result.scope_type != expected_scope or (result.run_id and result.run_id != str(run_id)):
            add_error("run_scope_mismatch", "review_result run_id/scope_type does not match the chart review bundle")
        if result.bundle_fingerprint != materials["bundle_fingerprint"]:
            add_error(
                "stale_or_mismatched_bundle",
                "bundle_fingerprint differs from the current figure/table evidence snapshot; export a new package and review again",
            )
        if local_ai_authorized and result.review_source.review_source_type != "local_ai":
            add_error(
                "local_ai_review_source_required",
                "Authenticated local-AI chart verification must identify review_source_type='local_ai'.",
            )
        if result.overall_status == "completed":
            missing_source_pdfs = [item for item in materials["source_pdf_inventory"] if not item.get("pdf_available")]
            omitted_source_pdfs = [
                item for item in materials["source_pdf_inventory"]
                if item.get("pdf_available") and not item.get("included_in_bundle")
            ]
            if missing_source_pdfs:
                add_error(
                    "source_pdf_missing_for_comprehensive_review",
                    "A completed main/SI chart review requires every source PDF: "
                    + ", ".join(str(item.get("paper_code") or item.get("paper_id")) for item in missing_source_pdfs),
                )
            if omitted_source_pdfs:
                add_error(
                    "source_pdf_not_in_bundle",
                    "A completed main/SI chart review requires every source PDF in the bundle: "
                    + ", ".join(
                        f"{item.get('paper_code') or item.get('paper_id')} ({item.get('omitted_reason') or 'not_included'})"
                        for item in omitted_source_pdfs
                    ),
                )

        figure_ids_seen: set[str] = set()
        table_ids_seen: set[str] = set()
        figure_refs: dict[str, list[str]] = {}
        table_refs: dict[str, list[str]] = {}
        evidence_ids = set(materials["evidence_map"])

        def note_ref(mapping: dict[str, list[str]], object_id: str | None, action_ref: str) -> None:
            if object_id:
                mapping.setdefault(object_id, []).append(action_ref)

        for index, action in enumerate(result.figure_actions):
            action_ref = f"figure_actions[{index}]"
            plan = self._validate_figure_action(
                action,
                materials,
                action_ref,
                evidence_ids,
                local_ai_authorized=local_ai_authorized,
            )
            execution_plan.append(plan)
            for error in plan.pop("_errors", []):
                add_error(error["code"], error["message"], action_ref=action_ref)
            if action.figure_id:
                figure_ids_seen.add(action.figure_id)
                note_ref(figure_refs, action.figure_id, action_ref)

        for index, action in enumerate(result.table_actions):
            action_ref = f"table_actions[{index}]"
            plan = self._validate_table_action(action, materials, action_ref, evidence_ids)
            execution_plan.append(plan)
            for error in plan.pop("_errors", []):
                add_error(error["code"], error["message"], action_ref=action_ref)
            for table_id in {action.table_id, action.source_table_id, action.target_table_id}:
                if table_id:
                    table_ids_seen.add(table_id)
                    note_ref(table_refs, table_id, action_ref)

        for object_id, refs in sorted(figure_refs.items()):
            if len(refs) > 1:
                add_error(
                    "duplicate_or_conflicting_figure_action",
                    f"figure_id '{object_id}' appears in multiple actions: " + ", ".join(refs),
                    action_ref=refs[0],
                )
        for object_id, refs in sorted(table_refs.items()):
            unique_refs = list(dict.fromkeys(refs))
            if len(unique_refs) > 1:
                add_error(
                    "duplicate_or_conflicting_table_action",
                    f"table_id '{object_id}' appears in multiple actions: " + ", ".join(unique_refs),
                    action_ref=unique_refs[0],
                )

        missing_figures = sorted(materials["figure_id_map"] - figure_ids_seen)
        missing_tables = sorted(materials["table_id_map"] - table_ids_seen)
        if missing_figures:
            add_error(
                "incomplete_figure_coverage",
                "Figure review requires one action for every current figure; missing: " + ", ".join(missing_figures),
            )
        if missing_tables:
            add_error(
                "incomplete_table_coverage",
                "Table review requires one action for every current table; missing: " + ", ".join(missing_tables),
            )

        for index, candidate in enumerate(result.dft_evidence_candidates):
            if candidate.evidence_id and candidate.evidence_id not in evidence_ids:
                add_error(
                    "unknown_dft_candidate_evidence_id",
                    f"dft_evidence_candidates[{index}].evidence_id is not present in the package",
                    action_ref=f"dft_evidence_candidates[{index}]",
                )

        if not result.figure_actions and materials["figure_id_map"]:
            warnings.append({"code": "no_figure_actions", "message": "No figure actions were returned."})
        if not result.table_actions and materials["table_id_map"]:
            warnings.append({"code": "no_table_actions", "message": "No table actions were returned."})
        if result.dft_evidence_candidates:
            warnings.append(
                {
                    "code": "dft_candidates_are_evidence_only",
                    "message": "Figure/table DFT candidates are stored for the later DFT review package; they are not verified DFT rows.",
                }
            )
        if result.overall_status != "completed":
            warnings.append(
                {
                    "code": "server_will_decide_completion",
                    "message": "The server finalizes the chart stage from coverage, validation, safe operations, and unresolved_actions rather than trusting overall_status alone.",
                }
            )

        if errors:
            for plan in execution_plan:
                plan["auto_apply"] = False
                blocked = list(plan.get("blocked_reasons") or [])
                if "result_has_validation_errors" not in blocked:
                    blocked.append("result_has_validation_errors")
                plan["blocked_reasons"] = blocked

        unresolved_actions = self._unresolved_actions(execution_plan)
        valid = not errors
        apply_ready = valid and not unresolved_actions
        return {
            "valid": valid,
            "stage_status": "ready_to_finalize" if apply_ready else ("needs_local_ai" if valid else "invalid"),
            "apply_ready": apply_ready,
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "scope_type": materials.get("scope_type", "paper"),
            "run_id": materials.get("run_id"),
            "chart_run_id": materials.get("run_id"),
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "coverage": {
                "expected_figure_ids": sorted(materials["figure_id_map"]),
                "expected_table_ids": sorted(materials["table_id_map"]),
                "covered_figure_ids": sorted(figure_ids_seen),
                "covered_table_ids": sorted(table_ids_seen),
                "missing_figure_ids": missing_figures,
                "missing_table_ids": missing_tables,
            },
            "rag_quality": {
                "figures": materials["figure_rag_quality"],
            },
            "rag_quality_status": materials["figure_rag_quality"]["status"],
            "errors": errors,
            "warnings": warnings,
            "execution_plan": execution_plan,
            "auto_apply_count": sum(1 for item in execution_plan if item.get("auto_apply")),
            "needs_confirmation_count": len(unresolved_actions),
            "unresolved_count": len(unresolved_actions),
            "unresolved_actions": unresolved_actions,
            "safety": {
                "validate_writes_database": False,
                "apply_endpoint_writes_database": True,
                "local_ai_role": "use authenticated MCP chart-review tools to verify every in-scope figure against its PDF, batch resolve, and finalize",
                "web_ai_writes_database": False,
                "all_figures_require_local_ai_verification": True,
                "local_ai_verification_authorized": local_ai_authorized,
            },
        }

    def apply_result(
        self,
        paper_id: UUID,
        raw_payload: dict[str, Any],
        *,
        run_id: UUID | None = None,
        dry_run: bool = False,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        payload_hash = _payload_sha256(raw_payload)
        payload_run_id = self._payload_run_id(raw_payload)
        run_id = run_id or payload_run_id
        if not dry_run:
            existing = self._existing_review_response(paper_id, payload_hash, run_id=run_id)
            if existing is not None:
                return existing

        validation = self.validate_result(
            paper_id,
            raw_payload,
            run_id=run_id,
            local_ai_authorized=local_ai_authorized,
        )
        result = OfflineEvidenceReviewResult.model_validate(raw_payload) if validation["valid"] else None
        if not validation["valid"] or dry_run:
            return {
                **validation,
                "dry_run": dry_run,
                "input_payload_sha256": payload_hash,
                "applied_count": 0,
                "applied": [],
                "skipped": validation.get("unresolved_actions", validation.get("execution_plan", [])),
                "chart_review_completed": False,
                "completed_snapshot_fingerprint": None,
            }

        assert result is not None
        materials = self._build_materials(paper_id, run_id=run_id)
        auto_op_ids = {
            item["op_id"]
            for item in validation.get("execution_plan", [])
            if item.get("auto_apply")
        }
        applied: list[dict[str, Any]] = []
        reviewer = _short_source(result.review_source.reviewer_label)
        unresolved_actions = list(validation.get("unresolved_actions") or [])

        try:
            for index, action in enumerate(result.figure_actions):
                op_id = f"figure:{index}:{action.action}"
                if op_id not in auto_op_ids:
                    continue
                applied.append(
                    self._apply_figure_action(
                        action,
                        materials,
                        op_id,
                        result.bundle_fingerprint,
                        reviewer,
                        local_ai_authorized=local_ai_authorized,
                    )
                )

            for index, action in enumerate(result.table_actions):
                op_id = f"table:{index}:{action.action}"
                if op_id not in auto_op_ids:
                    continue
                applied.append(self._apply_table_action(action, materials, op_id, result.bundle_fingerprint, reviewer))

            if unresolved_actions:
                response = self._record_partial_review(
                    paper_id=paper_id,
                    run_id=run_id,
                    result=result,
                    validation=validation,
                    applied=applied,
                    unresolved_actions=unresolved_actions,
                    reviewer=reviewer,
                    payload_hash=payload_hash,
                )
            else:
                response = self._record_completed_review(
                    paper_id=paper_id,
                    run_id=run_id,
                    result=result,
                    validation=validation,
                    applied=applied,
                    reviewer=reviewer,
                    payload_hash=payload_hash,
                )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        return response

    def get_review_task(self, paper_id: UUID, *, run_id: UUID | None = None) -> dict[str, Any]:
        materials = self._build_materials(paper_id, run_id=run_id)
        current_snapshot = self._scope_snapshot(materials)
        current_snapshot_fingerprint = materials["bundle_fingerprint"]
        latest = self._latest_review_audit(paper_id, run_id=run_id)
        latest_payload = latest.payload if latest is not None and isinstance(latest.payload, dict) else {}
        latest_response = latest_payload.get("response") if isinstance(latest_payload.get("response"), dict) else None
        stage_status = "not_started"
        unresolved_actions: list[dict[str, Any]] = []
        completed_snapshot_fingerprint = None
        state_payload = latest_response or latest_payload
        if state_payload:
            stage_status = str(state_payload.get("stage_status") or stage_status)
            unresolved_actions = list(state_payload.get("unresolved_actions") or [])
            completed_snapshot_fingerprint = state_payload.get("completed_snapshot_fingerprint")
        if (
            latest is None
            and not current_snapshot["figures"]
            and not current_snapshot["tables"]
        ):
            stage_status = "not_required"
            completed_snapshot_fingerprint = current_snapshot_fingerprint
            unresolved_actions = []
        elif (
            stage_status == "completed"
            and completed_snapshot_fingerprint != current_snapshot_fingerprint
        ):
            # Older workspace refreshes rewrote only the three derived crop
            # metadata fields after an offline RECROP, while preserving the
            # reviewed image, geometry, evidence, and provenance.  Recognize
            # that exact legacy shape without weakening any real stale check.
            if self._is_known_workspace_crop_metadata_drift(
                latest_payload.get("completed_snapshot") or state_payload.get("completed_snapshot"),
                current_snapshot,
            ):
                completed_snapshot_fingerprint = current_snapshot_fingerprint
            else:
                stage_status = "stale"
                unresolved_actions = [
                    {
                        "code": "figure_table_snapshot_changed",
                        "message": "Persisted figure/table content changed after chart review completion.",
                        "requires_local_ai": True,
                    }
                ]
        needs_human_pending = pending_needs_human_actions_from_review_payload(latest_payload)
        if stage_status == "completed" and needs_human_pending:
            stage_status = "needs_local_ai"
            unresolved_actions = [*unresolved_actions, *needs_human_pending]
        figure_rag_quality = materials["figure_rag_quality"]
        rag_quality_status = str(figure_rag_quality.get("status") or "ready")
        quality_blockers: list[dict[str, Any]] = []
        if stage_status == "completed" and rag_quality_status != "ready":
            stage_status = "needs_local_ai"
            quality_blockers = self._rag_quality_unresolved_actions(figure_rag_quality)
            unresolved_actions = [*unresolved_actions, *quality_blockers]
        stale_changed_ids = (
            self._changed_snapshot_object_ids(
                latest_payload.get("completed_snapshot") or state_payload.get("completed_snapshot"),
                current_snapshot,
            )
            if stage_status == "stale" and isinstance(state_payload, dict)
            else {"figures": set(), "tables": set()}
        )
        scope_completion = self._current_scope_completion(
            materials,
            run_id=run_id,
            reviewed_after=latest.created_at if latest is not None and stage_status == "stale" else None,
            changed_ids=stale_changed_ids,
        )
        if stage_status == "completed" and not scope_completion["complete"]:
            stage_status = "needs_local_ai"
            completed_snapshot_fingerprint = None
            existing_targets = {
                str(item.get("target_id") or "")
                for item in unresolved_actions
                if isinstance(item, dict)
            }
            for figure_id in scope_completion["missing_figure_ids"]:
                if figure_id in existing_targets:
                    continue
                figure_record = materials.get("figure_record_by_id", {}).get(figure_id) or {}
                unresolved_actions.append(
                    {
                        "code": "local_ai_full_figure_verification_required",
                        "category": "figure",
                        "action": "VERIFY_AGAINST_PDF",
                        "target_id": figure_id,
                        "source_paper_id": figure_record.get("source_paper_id"),
                        "evidence_ids": [figure_record.get("evidence_id")] if figure_record.get("evidence_id") else [],
                        "blocked_reasons": ["local_ai_full_figure_verification_required"],
                        "reason": "Every in-scope figure must be verified by local AI against its source PDF after the web-AI result is applied.",
                        "requires_local_ai": True,
                    }
                )
        if (
            stage_status in {"stale", "not_started", "needs_local_ai"}
            and rag_quality_status == "ready"
            and scope_completion["complete"]
        ):
            stage_status = "completed"
            completed_snapshot_fingerprint = current_snapshot_fingerprint
            unresolved_actions = []
        return {
            "schema_version": "chart_review_task_v1",
            "paper_id": materials["paper_metadata"]["paper_id"],
            "paper_code": materials["paper_metadata"]["paper_code"],
            "scope_type": materials.get("scope_type", "paper"),
            "run_id": materials.get("run_id"),
            "chart_run_id": materials.get("run_id"),
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "stage_status": stage_status,
            "apply_ready": stage_status in {"completed", "not_required"} and rag_quality_status == "ready",
            "rag_quality_status": rag_quality_status,
            "rag_quality": {
                "figures": figure_rag_quality,
            },
            "blocking_errors": [
                {
                    "code": "figure_rag_quality_incomplete",
                    "message": "Figure/table review has a completed action snapshot, but current figures are not RAG-ready.",
                    "blocked_count": figure_rag_quality.get("blocked"),
                }
            ]
            if quality_blockers
            else [],
            "current_snapshot_fingerprint": current_snapshot_fingerprint,
            "completed_snapshot_fingerprint": completed_snapshot_fingerprint,
            "latest_review_run_id": str(latest.id) if latest is not None else None,
            "reviewed_at": latest.created_at.isoformat() if latest is not None and latest.created_at else None,
            "unresolved_count": len(unresolved_actions),
            "unresolved_actions": unresolved_actions,
            "scope_completion": scope_completion,
            "counts": {
                "source_documents": len(materials["source_documents"]),
                "figures": len(materials["extracted_figures"]),
                "tables": len(materials["extracted_tables"]),
                "main_figures": sum(1 for item in materials["extracted_figures"] if item.get("source_document_type") == "main_text"),
                "main_tables": sum(1 for item in materials["extracted_tables"] if item.get("source_document_type") == "main_text"),
                "si_figures": sum(1 for item in materials["extracted_figures"] if item.get("source_document_type") == "supplementary_information"),
                "si_tables": sum(1 for item in materials["extracted_tables"] if item.get("source_document_type") == "supplementary_information"),
            },
            "paper_metadata": materials["paper_metadata"],
            "source_documents": self._public_source_documents(materials["source_documents"]),
            "page_geometry": materials["page_geometry"],
            "figures": self._public_records(materials["extracted_figures"]),
            "tables": self._public_records(materials["extracted_tables"]),
            "excluded_duplicate_figures": materials["excluded_duplicate_figures"],
        }

    def resolve_review_actions(
        self,
        paper_id: UUID,
        raw_payload: dict[str, Any],
        *,
        run_id: UUID | None = None,
        dry_run: bool = False,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        return self.apply_result(
            paper_id,
            raw_payload,
            run_id=run_id,
            dry_run=dry_run,
            local_ai_authorized=local_ai_authorized,
        )

    def finalize_review(
        self,
        paper_id: UUID,
        raw_payload: dict[str, Any] | None = None,
        *,
        run_id: UUID | None = None,
        dry_run: bool = False,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        if raw_payload is not None:
            response = self.apply_result(
                paper_id,
                raw_payload,
                run_id=run_id,
                dry_run=dry_run,
                local_ai_authorized=local_ai_authorized,
            )
            if dry_run or response.get("chart_review_completed"):
                return response
            return {
                **response,
                "finalize_ready": False,
                "finalize_blocking_errors": response.get("unresolved_actions") or response.get("errors") or [],
            }
        latest = self._latest_review_audit(paper_id, run_id=run_id, actions={"offline_evidence_review_applied"})
        materials = self._build_materials(paper_id, run_id=run_id)
        current_task = self.get_review_task(paper_id, run_id=run_id)
        if current_task["stage_status"] == "not_required":
            return {
                **current_task,
                "valid": True,
                "chart_review_completed": True,
                "finalize_ready": True,
                "unresolved_actions": [],
            }
        if latest is None:
            return {
                "valid": True,
                "paper_id": materials["paper_metadata"]["paper_id"],
                "paper_code": materials["paper_metadata"]["paper_code"],
                "bundle_fingerprint": materials["bundle_fingerprint"],
                "stage_status": "not_ready",
                "apply_ready": False,
                "chart_review_completed": False,
                "completed_snapshot_fingerprint": None,
                "unresolved_actions": [
                    {
                        "code": "no_completed_chart_review",
                        "message": "No completed chart review snapshot is recorded for this paper.",
                    }
                ],
            }
        if current_task["stage_status"] != "completed":
            return {
                **current_task,
                "valid": True,
                "chart_review_completed": False,
                "finalize_ready": False,
                "finalize_blocking_errors": current_task.get("unresolved_actions") or [],
            }
        payload = latest.payload if isinstance(latest.payload, dict) else {}
        response = dict(payload.get("response") or {})
        if response:
            response["idempotent"] = True
            response["stage_status"] = current_task["stage_status"]
            response["chart_review_completed"] = True
            response["current_snapshot_fingerprint"] = current_task["current_snapshot_fingerprint"]
            return response
        return {
            "valid": True,
            "paper_id": materials["paper_metadata"]["paper_id"],
            "paper_code": materials["paper_metadata"]["paper_code"],
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "stage_status": "completed",
            "apply_ready": True,
            "chart_review_completed": True,
            "review_run_id": str(latest.id),
            "completed_snapshot_fingerprint": payload.get("completed_snapshot_fingerprint"),
            "current_snapshot_fingerprint": current_task["current_snapshot_fingerprint"],
            "unresolved_actions": [],
        }

    def _validate_figure_action(
        self,
        action: OfflineEvidenceReviewFigureAction,
        materials: dict[str, Any],
        action_ref: str,
        evidence_ids: set[str],
        *,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        blocked: list[str] = []
        target_id = action.figure_id or "new"

        def block(code: str, message: str) -> None:
            errors.append({"code": code, "message": message})
            blocked.append(code)

        if action.figure_id and action.figure_id not in materials["figure_id_map"]:
            block("unknown_figure_id", f"figure_id '{action.figure_id}' is not present in the current bundle")
        if action.source_paper_id and action.source_paper_id not in materials["source_paper_ids"]:
            block("unknown_source_paper_id", f"source_paper_id '{action.source_paper_id}' is outside the current bundle")
        missing_evidence = [item for item in action.evidence_ids if item not in evidence_ids]
        if missing_evidence:
            block("unknown_evidence_id", "Evidence ids are not present in the package: " + ", ".join(missing_evidence))
        if self._figure_action_modifies_state(action) and not action.evidence_ids:
            block("missing_evidence_ids_for_modification", f"{action.action} modifies figure evidence and requires at least one valid evidence_id")
        if action.action != "NEEDS_HUMAN" and not action.evidence_checked:
            block("evidence_not_checked", f"{action.action} requires evidence_checked=true")
        if action.action in {"RECROP", "CREATE"} and not self._has_page_geometry(action.source_paper_id, action.figure_id, action.page, materials):
            blocked.append("page_geometry_unavailable")
        if action.action == "NEEDS_HUMAN":
            if not action.evidence_checked:
                blocked.append("evidence_not_checked")
            if not action.evidence_ids:
                blocked.append("missing_evidence_ids_for_pending_action")
            blocked.append("needs_human")
            blocked.append("local_ai_review_required")
            blocked.append("confirmation_required")
        trusted_local_verification = local_ai_authorized and self._has_local_ai_verification(
            action.local_ai_verification
        )
        if action.action == "REJECT":
            if not trusted_local_verification:
                blocked.append("reject_requires_local_ai")
                blocked.append("local_ai_pdf_verification_required")
        if (
            action.action != "NEEDS_HUMAN"
            and (action.confidence is None or action.confidence < FIGURE_AUTO_CONFIDENCE)
        ) and not trusted_local_verification:
            blocked.append("confidence_below_auto_apply_threshold")
        quality_reasons = self._projected_figure_action_quality_reasons(action, materials)
        if quality_reasons:
            block(
                "figure_rag_quality_incomplete",
                "Figure remains not RAG-ready after this action: " + ", ".join(quality_reasons),
            )

        auto = not errors and action.action in {"KEEP", "RECROP", "CREATE", "REJECT"} and not blocked
        return {
            "op_id": action_ref.replace("figure_actions[", "figure:").replace("]", f":{action.action}"),
            "action_ref": action_ref,
            "category": "figure",
            "action": action.action,
            "target_id": target_id,
            "source_paper_id": action.source_paper_id,
            "auto_apply": auto,
            "blocked_reasons": list(dict.fromkeys(blocked)),
            "completion_blockers": (
                []
                if trusted_local_verification or action.action == "NEEDS_HUMAN"
                else ["local_ai_full_figure_verification_required"]
            ),
            "local_ai_verified": trusted_local_verification,
            "tool_hint": "system_deterministic_pdf_crop" if action.action in {"RECROP", "CREATE"} else "system_metadata_update_or_final_status",
            "payload": action.model_dump(mode="json"),
            "_errors": errors,
        }

    @staticmethod
    def _projected_figure_action_quality_reasons(
        action: OfflineEvidenceReviewFigureAction,
        materials: dict[str, Any],
    ) -> list[str]:
        if action.action == "REJECT":
            return []
        existing = materials.get("figure_record_by_id", {}).get(str(action.figure_id or ""))
        if action.action in {"KEEP", "RECROP", "NEEDS_HUMAN"} and not isinstance(existing, dict):
            return []
        projected = dict(existing or {})
        if action.action in {"CREATE", "RECROP"}:
            projected["image_available"] = True
            projected["crop_status"] = "recropped" if action.action == "RECROP" else "ai_created_crop"
        for field in ("page", "caption", "figure_label", "figure_role", "content_summary", "key_elements"):
            value = getattr(action, field)
            if value is not None:
                projected[field] = value
        reasons: list[str] = []
        if not projected.get("image_available"):
            reasons.append("missing_image")
        if projected.get("page") is None:
            reasons.append("missing_page")
        if not str(projected.get("caption") or "").strip():
            reasons.append("missing_caption")
        role = str(projected.get("figure_role") or "").strip().lower()
        if role in {"noise", "noisy", "decorative", "publisher_logo"}:
            reasons.append(f"figure_role:{role}")
        if not role or role in {"unknown", "uncategorized", "unclassified", "other"}:
            reasons.append("missing_figure_role")
        summary = str(projected.get("content_summary") or "").strip()
        if not summary:
            reasons.append("missing_content_summary")
        elif figure_summary_echoes_caption(summary, projected.get("caption")):
            reasons.append("caption_echo_summary")
        key_elements = flatten_figure_key_elements(projected.get("key_elements"))
        if not key_elements:
            reasons.append("missing_key_elements")
        elif any(EvidenceReviewBundleService._is_placeholder_figure_key_element(item) for item in key_elements):
            reasons.append("placeholder_key_elements")
        return reasons

    @staticmethod
    def _is_placeholder_figure_key_element(value: Any) -> bool:
        normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        return normalized in {
            "verified_figure",
            "figure_verified",
            "reviewed_figure",
            "ai_verified",
            "verified",
            "reviewed",
            "ok",
        }

    def _validate_table_action(
        self,
        action: OfflineEvidenceReviewTableAction,
        materials: dict[str, Any],
        action_ref: str,
        evidence_ids: set[str],
    ) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        blocked: list[str] = []
        target_id = action.table_id or action.target_table_id or "new"

        def block(code: str, message: str) -> None:
            errors.append({"code": code, "message": message})
            blocked.append(code)

        for field_name, table_id in (
            ("table_id", action.table_id),
            ("source_table_id", action.source_table_id),
            ("target_table_id", action.target_table_id),
        ):
            if table_id and table_id not in materials["table_id_map"]:
                block("unknown_table_id", f"{field_name} '{table_id}' is not present in the current bundle")
        if action.source_paper_id and action.source_paper_id not in materials["source_paper_ids"]:
            block("unknown_source_paper_id", f"source_paper_id '{action.source_paper_id}' is outside the current bundle")
        missing_evidence = [item for item in action.evidence_ids if item not in evidence_ids]
        if missing_evidence:
            block("unknown_evidence_id", "Evidence ids are not present in the package: " + ", ".join(missing_evidence))
        if action.action in {"UPDATE", "CREATE", "MERGE", "DELETE"} and not action.evidence_ids:
            block("missing_evidence_ids_for_modification", f"{action.action} modifies table evidence and requires at least one valid evidence_id")
        if action.action != "NEEDS_HUMAN" and not action.evidence_checked:
            block("evidence_not_checked", f"{action.action} requires evidence_checked=true")
        if action.action in {"UPDATE", "CREATE"} and not self._looks_like_markdown_table(action.complete_markdown):
            block("invalid_markdown_table", f"{action.action} requires a complete markdown table with pipes and multiple rows")
        if action.action == "MERGE":
            if not action.source_table_id or not action.target_table_id:
                blocked.append("merge_requires_source_table_id_and_target_table_id")
                blocked.append("local_ai_pdf_verification_required")
                blocked.append("confirmation_required")
            elif action.source_table_id == action.target_table_id:
                blocked.append("merge_source_and_target_table_ids_must_differ")
                blocked.append("local_ai_pdf_verification_required")
                blocked.append("confirmation_required")
            if not self._has_local_ai_verification(action.local_ai_verification):
                blocked.append("merge_requires_local_ai")
                blocked.append("local_ai_pdf_verification_required")
        if action.action == "DELETE":
            if not self._has_local_ai_verification(action.local_ai_verification):
                blocked.append("delete_requires_local_ai")
                blocked.append("local_ai_pdf_verification_required")
        if action.action == "NEEDS_HUMAN":
            if not action.evidence_checked:
                blocked.append("evidence_not_checked")
            if not action.evidence_ids:
                blocked.append("missing_evidence_ids_for_pending_action")
            blocked.append("needs_human")
            blocked.append("local_ai_review_required")
            blocked.append("confirmation_required")
        if (
            action.action != "NEEDS_HUMAN"
            and (action.confidence is None or action.confidence < TABLE_AUTO_CONFIDENCE)
        ) and not self._has_local_ai_verification(action.local_ai_verification):
            blocked.append("confidence_below_auto_apply_threshold")

        auto = not errors and action.action in {"KEEP", "UPDATE", "CREATE", "MERGE", "DELETE"} and not blocked
        return {
            "op_id": action_ref.replace("table_actions[", "table:").replace("]", f":{action.action}"),
            "action_ref": action_ref,
            "category": "table",
            "action": action.action,
            "target_id": target_id,
            "source_paper_id": action.source_paper_id,
            "auto_apply": auto,
            "blocked_reasons": list(dict.fromkeys(blocked)),
            "tool_hint": "no_db_write_final_status" if action.action == "KEEP" else "table_curation_service",
            "payload": action.model_dump(mode="json"),
            "_errors": errors,
        }

    def _apply_figure_action(
        self,
        action: OfflineEvidenceReviewFigureAction,
        materials: dict[str, Any],
        op_id: str,
        bundle_fingerprint: str,
        reviewer: str,
        *,
        local_ai_authorized: bool = False,
    ) -> dict[str, Any]:
        preexisting = self._already_applied(
            op_id,
            bundle_fingerprint,
            target_type="paper_figure",
            expected_action=action.model_dump(mode="json"),
        )
        if preexisting is not None:
            return preexisting

        if action.action == "CREATE":
            source_paper_id = UUID(str(action.source_paper_id))
            paper = self.session.get(Paper, source_paper_id)
            if paper is None:
                raise ValueError("Source paper not found during figure creation")
            rel_path, bbox_used, pixel_size = self._render_pdf_crop(
                paper=paper,
                page=int(action.page or 1),
                bbox_norm=action.bbox_norm or [0.0, 0.0, 1.0, 1.0],
            )
            figure = PaperFigure(
                paper_id=source_paper_id,
                caption=action.caption,
                image_path=rel_path,
                page=action.page,
                figure_label=action.figure_label,
                figure_role=action.figure_role,
                role_confidence=action.confidence,
                content_summary=normalize_figure_content_summary(action.content_summary, action.caption),
                key_elements=normalize_figure_key_elements(action.key_elements)[0],
                crop_status="ai_created_crop",
                crop_confidence=action.confidence,
                crop_source="offline_evidence_review",
                prov=[self._figure_prov(action, bbox_used, pixel_size, reviewer, bundle_fingerprint)],
            )
            self.session.add(figure)
            self.session.flush()
            target_id = str(figure.id)
            applied_updates = {"created": True, "image_path": rel_path, "bbox": bbox_used}
        elif action.action == "REJECT":
            figure = self.session.get(PaperFigure, UUID(str(action.figure_id)))
            if figure is None:
                raise ValueError("Figure not found during rejection")
            target_id = str(figure.id)
            figure_paper_id = figure.paper_id
            evidence_payload = {
                "page": figure.page,
                "figure_label": figure.figure_label,
                "quoted_text": figure.caption or figure.content_summary or f"Figure object {figure.id}",
                "evidence_ids": list(action.evidence_ids),
                "reason": action.reason,
                "local_ai_verification": (
                    action.local_ai_verification.model_dump(mode="json")
                    if action.local_ai_verification is not None
                    else None
                ),
            }
            review_service = ReviewService(self.session)
            correction = review_service.propose_figure_deletion(
                paper_id=figure_paper_id,
                figure_id=figure.id,
                reason=action.reason,
                reviewer=reviewer,
                evidence_payload=evidence_payload,
            )
            approved = review_service.approve_correction(correction.id, reviewer=reviewer)
            applied_updates = {
                "deleted": True,
                "correction_id": str(approved.id),
                "actor_type": "ai",
                "local_ai_verification": evidence_payload["local_ai_verification"],
            }
        elif action.action == "NEEDS_HUMAN":
            figure = self.session.get(PaperFigure, UUID(str(action.figure_id)))
            if figure is None:
                raise ValueError("Figure not found during documented exception recording")
            target_id = str(figure.id)
            applied_updates = {
                "documented_exception": True,
                "requires_human_attention": True,
                "updated": False,
                "evidence_ids": list(action.evidence_ids),
                "reason": action.reason,
            }
        else:
            figure = self.session.get(PaperFigure, UUID(str(action.figure_id)))
            if figure is None:
                raise ValueError("Figure not found during apply")
            target_id = str(figure.id)
            applied_updates = self._apply_figure_metadata(figure, action)
            if action.action == "KEEP":
                figure.prov = list(figure.prov or []) + [
                    self._figure_keep_prov(action, reviewer, bundle_fingerprint)
                ]
            if action.action == "RECROP":
                paper = self.session.get(Paper, figure.paper_id)
                if paper is None:
                    raise ValueError("Figure paper not found during recrop")
                page = int(action.page or figure.page or 1)
                rel_path, bbox_used, pixel_size = self._render_pdf_crop(
                    paper=paper,
                    page=page,
                    bbox_norm=action.bbox_norm or [0.0, 0.0, 1.0, 1.0],
                )
                figure.image_path = rel_path
                figure.page = page
                figure.crop_status = "recropped"
                figure.crop_confidence = action.confidence
                figure.crop_source = "offline_evidence_review"
                figure.prov = list(figure.prov or []) + [
                    self._figure_prov(action, bbox_used, pixel_size, reviewer, bundle_fingerprint)
                ]
                figure.write_version = (figure.write_version or 1) + 1
                applied_updates.update({"image_path": rel_path, "bbox": bbox_used, "page": page, "crop_status": "recropped"})

        audit = AuditLog(
            paper_id=(
                UUID(str(action.source_paper_id))
                if action.action == "CREATE"
                else figure_paper_id
                if action.action == "REJECT"
                else figure.paper_id
            ),
            action="offline_evidence_review_op",
            source=reviewer,
            target_type="paper_figure",
            target_id=target_id,
            payload={
                "op_id": op_id,
                "bundle_fingerprint": bundle_fingerprint,
                "run_id": materials.get("run_id"),
                "chart_run_id": materials.get("run_id"),
                "action": action.model_dump(mode="json"),
                "applied_updates": applied_updates,
                "actor_type": (
                    "local_ai"
                    if local_ai_authorized and self._has_local_ai_verification(action.local_ai_verification)
                    else "web_ai_review_source"
                ),
            },
            created_at=utcnow(),
        )
        self.session.add(audit)
        self.session.flush()
        return {
            "op_id": op_id,
            "category": "figure",
            "action": action.action,
            "target_id": target_id,
            "audit_log_id": str(audit.id),
            "applied_updates": applied_updates,
            "idempotent": False,
        }

    def _apply_table_action(
        self,
        action: OfflineEvidenceReviewTableAction,
        materials: dict[str, Any],
        op_id: str,
        bundle_fingerprint: str,
        reviewer: str,
    ) -> dict[str, Any]:
        preexisting = self._already_applied(
            op_id,
            bundle_fingerprint,
            target_type="paper_table",
            expected_action=action.model_dump(mode="json"),
        )
        if preexisting is not None:
            return preexisting

        evidence_payload = self._table_evidence_payload(action, bundle_fingerprint)
        service = TableCurationService(self.session, reviewer=reviewer)
        if action.action == "KEEP":
            table = self.session.get(PaperTable, UUID(str(action.table_id)))
            if table is None:
                raise ValueError("Table not found during keep/final-status recording")
            target_id = str(table.id)
            result = {
                "paper_id": str(table.paper_id),
                "table_id": target_id,
                "action": "KEEP",
                "updated": False,
                "evidence_payload": evidence_payload,
            }
        elif action.action == "UPDATE":
            table = self.session.get(PaperTable, UUID(str(action.table_id)))
            if table is None:
                raise ValueError("Table not found during update")
            updates = {
                "markdown_content": action.complete_markdown,
                "extraction_source": "offline_evidence_review",
                "prov": self._updated_table_prov(table, action, bundle_fingerprint, reviewer),
            }
            if action.caption is not None:
                updates["caption"] = action.caption
            if action.page is not None:
                updates["page"] = action.page
            result = service.update_table(
                paper_id=table.paper_id,
                table_id=table.id,
                updates=updates,
                reason=action.reason,
                evidence_payload=evidence_payload,
            )
            target_id = str(table.id)
        elif action.action == "CREATE":
            source_paper_id = UUID(str(action.source_paper_id))
            table_payload = {
                "caption": action.caption,
                "markdown_content": action.complete_markdown,
                "page": action.page,
                "extraction_source": "offline_evidence_review",
                "prov": self._new_table_prov(action, bundle_fingerprint, reviewer),
            }
            result = service.create_table(
                paper_id=source_paper_id,
                table_payload=table_payload,
                reason=action.reason,
                evidence_payload=evidence_payload,
            )
            target_id = str(result.get("table_id") or "")
        elif action.action == "MERGE":
            target = self.session.get(PaperTable, UUID(str(action.target_table_id)))
            if target is None:
                raise ValueError("Target table not found during merge")
            target_updates = {
                key: value
                for key, value in {
                    "caption": action.caption,
                    "markdown_content": action.complete_markdown,
                    "page": action.page,
                }.items()
                if value is not None
            }
            result = service.merge_table(
                paper_id=target.paper_id,
                source_table_id=UUID(str(action.source_table_id)),
                target_table_id=target.id,
                target_updates=target_updates,
                reason=action.reason,
                evidence_payload=evidence_payload,
            )
            target_id = str(target.id)
        elif action.action == "DELETE":
            table = self.session.get(PaperTable, UUID(str(action.table_id)))
            if table is None:
                raise ValueError("Table not found during deletion")
            target_id = str(table.id)
            result = service.delete_table(
                paper_id=table.paper_id,
                table_id=table.id,
                reason=action.reason,
                evidence_payload=evidence_payload,
            )
        elif action.action == "NEEDS_HUMAN":
            table = self.session.get(PaperTable, UUID(str(action.table_id)))
            if table is None:
                raise ValueError("Table not found during documented exception recording")
            target_id = str(table.id)
            result = {
                "paper_id": str(table.paper_id),
                "table_id": target_id,
                "action": "NEEDS_HUMAN",
                "updated": False,
                "documented_exception": True,
                "requires_human_attention": True,
                "evidence_payload": evidence_payload,
            }
        else:
            raise ValueError(f"Table action {action.action} is not auto-applied")

        audit = AuditLog(
            paper_id=UUID(str(result.get("paper_id"))),
            action="offline_evidence_review_op",
            source=reviewer,
            target_type="paper_table",
            target_id=target_id,
            payload={
                "op_id": op_id,
                "bundle_fingerprint": bundle_fingerprint,
                "run_id": materials.get("run_id"),
                "chart_run_id": materials.get("run_id"),
                "action": action.model_dump(mode="json"),
                "table_result": result,
                "actor_type": "ai" if action.local_ai_verification is not None else "review_source",
            },
            created_at=utcnow(),
        )
        self.session.add(audit)
        self.session.flush()
        return {
            "op_id": op_id,
            "category": "table",
            "action": action.action,
            "target_id": target_id,
            "audit_log_id": str(audit.id),
            "table_result": result,
            "idempotent": False,
        }

    def _record_partial_review(
        self,
        *,
        paper_id: UUID,
        run_id: UUID | None,
        result: OfflineEvidenceReviewResult,
        validation: dict[str, Any],
        applied: list[dict[str, Any]],
        unresolved_actions: list[dict[str, Any]],
        reviewer: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        refreshed = self._build_materials(paper_id, run_id=run_id)
        response = {
            **validation,
            "dry_run": False,
            "input_payload_sha256": payload_hash,
            "stage_status": "needs_local_ai",
            "apply_ready": False,
            "chart_review_completed": False,
            "applied_count": len(applied),
            "applied": applied,
            "skipped": unresolved_actions,
            "unresolved_actions": unresolved_actions,
            "post_apply_bundle_fingerprint": refreshed["bundle_fingerprint"],
            "completed_snapshot_fingerprint": None,
            "safety": {
                "writes_database": bool(applied),
                "writes_final_dft_truth": False,
                "local_ai_next_step": "Call get_chart_review_task, resolve all unresolved_actions with authenticated MCP, then finalize_chart_review.",
            },
        }
        audit = AuditLog(
            paper_id=paper_id,
            action="offline_evidence_review_partial",
            source=reviewer,
            target_type="offline_evidence_review",
            target_id=result.bundle_fingerprint[:32],
            payload={
                "schema_version": result.schema_version,
                "stage_status": "needs_local_ai",
                "bundle_fingerprint": result.bundle_fingerprint,
                "scope_type": result.scope_type,
                "run_id": result.run_id,
                "input_payload_sha256": payload_hash,
                "paper_code": result.paper_code,
                "review_source": result.review_source.model_dump(mode="json"),
                "applied": applied,
                "unresolved_actions": unresolved_actions,
                "execution_plan": validation.get("execution_plan", []),
                "dft_evidence_candidates": [item.model_dump(mode="json") for item in result.dft_evidence_candidates],
                "uncertainties": result.uncertainties,
                "notes": result.notes,
                "response": response,
            },
            created_at=utcnow(),
        )
        self.session.add(audit)
        self.session.flush()
        response["review_run_id"] = str(audit.id)
        audit.payload = {**audit.payload, "response": response}
        self.session.add(audit)
        if run_id is not None:
            TaskLogService(self.session).refresh_external_analysis_task(
                run_id, last_action="chart_review_partial", lifecycle="needs_human"
            )
        return response

    def _record_completed_review(
        self,
        *,
        paper_id: UUID,
        run_id: UUID | None,
        result: OfflineEvidenceReviewResult,
        validation: dict[str, Any],
        applied: list[dict[str, Any]],
        reviewer: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        refreshed = self._build_materials(paper_id, run_id=run_id)
        final_errors = self._final_status_errors(result=result, validation=validation, applied=applied, refreshed=refreshed)
        if final_errors:
            raise ValueError("chart_review_finalize_failed: " + json.dumps(final_errors, ensure_ascii=False))
        completed_snapshot = self._scope_snapshot(refreshed)
        completed_snapshot_fingerprint = refreshed["bundle_fingerprint"]
        if run_id is None:
            self._mark_figures_review_completed(paper_id, reviewer)
        response = {
            **validation,
            "dry_run": False,
            "input_payload_sha256": payload_hash,
            "stage_status": "completed",
            "apply_ready": True,
            "chart_review_completed": True,
            "applied_count": len(applied),
            "applied": applied,
            "skipped": [],
            "unresolved_actions": [],
            "unresolved_count": 0,
            "post_apply_bundle_fingerprint": refreshed["bundle_fingerprint"],
            "current_snapshot_fingerprint": completed_snapshot_fingerprint,
            "completed_snapshot_fingerprint": completed_snapshot_fingerprint,
            "safety": {
                "writes_database": True,
                "writes_final_dft_truth": False,
                "local_ai_next_step": "Chart review is completed; DFT review bundles may consume the completed figure/table snapshot.",
            },
        }
        audit = AuditLog(
            paper_id=paper_id,
            action="offline_evidence_review_applied",
            source=reviewer,
            target_type="offline_evidence_review",
            target_id=completed_snapshot_fingerprint[:32],
            payload={
                "schema_version": result.schema_version,
                "stage_status": "completed",
                "bundle_fingerprint": result.bundle_fingerprint,
                "scope_type": result.scope_type,
                "run_id": result.run_id,
                "post_apply_bundle_fingerprint": refreshed["bundle_fingerprint"],
                "completed_snapshot_fingerprint": completed_snapshot_fingerprint,
                "completed_snapshot": completed_snapshot,
                "input_payload_sha256": payload_hash,
                "paper_code": result.paper_code,
                "overall_status": result.overall_status,
                "review_source": result.review_source.model_dump(mode="json"),
                "applied": applied,
                "skipped": [],
                "unresolved_actions": [],
                "execution_plan": validation.get("execution_plan", []),
                "dft_evidence_candidates": [item.model_dump(mode="json") for item in result.dft_evidence_candidates],
                "uncertainties": result.uncertainties,
                "notes": result.notes,
                "response": response,
            },
            created_at=utcnow(),
        )
        self.session.add(audit)
        self.session.flush()
        response["review_run_id"] = str(audit.id)
        audit.payload = {**audit.payload, "response": response}
        self.session.add(audit)
        if run_id is not None:
            TaskLogService(self.session).refresh_external_analysis_task(
                run_id, last_action="chart_review_applied", lifecycle="applied"
            )
        return response

    def _existing_review_response(self, paper_id: UUID, payload_hash: str, *, run_id: UUID | None = None) -> dict[str, Any] | None:
        current_materials = self._build_materials(paper_id, run_id=run_id)
        current_bundle_fingerprint = current_materials["bundle_fingerprint"]
        current_snapshot_fingerprint = current_bundle_fingerprint
        latest_rows = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == paper_id)
            .where(AuditLog.action.in_(["offline_evidence_review_applied", "offline_evidence_review_partial"]))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(100)
        ).all()
        for row in latest_rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("input_payload_sha256") != payload_hash:
                continue
            stored_run_id = self._optional_uuid(payload.get("run_id")) if payload.get("run_id") else None
            if stored_run_id != run_id:
                continue
            response = payload.get("response") if isinstance(payload.get("response"), dict) else None
            if response is None:
                continue
            if response.get("stage_status") == "completed":
                if response.get("completed_snapshot_fingerprint") != current_snapshot_fingerprint:
                    continue
                figure_rag_quality = current_materials.get("figure_rag_quality") or {}
                if int(figure_rag_quality.get("blocked") or 0) > 0:
                    continue
            elif response.get("post_apply_bundle_fingerprint") != current_bundle_fingerprint:
                continue
            elif response.get("unresolved_actions"):
                continue
            cloned = dict(response)
            cloned["idempotent"] = True
            cloned["review_run_id"] = str(row.id)
            cloned["current_snapshot_fingerprint"] = current_snapshot_fingerprint
            return cloned
        return None

    def _latest_review_audit(self, paper_id: UUID, *, run_id: UUID | None = None, actions: set[str] | None = None) -> AuditLog | None:
        action_names = sorted(actions or {"offline_evidence_review_applied", "offline_evidence_review_partial"})
        rows = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == paper_id)
            .where(AuditLog.action.in_(action_names))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        ).all()
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if (self._optional_uuid(payload.get("run_id")) if payload.get("run_id") else None) == run_id:
                return row
        return None

    @staticmethod
    def _unresolved_actions(execution_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unresolved: list[dict[str, Any]] = []
        for plan in execution_plan:
            blocked = list(plan.get("blocked_reasons") or [])
            for item in plan.get("completion_blockers") or []:
                if item not in blocked:
                    blocked.append(item)
            if plan.get("auto_apply") and not blocked:
                continue
            payload = plan.get("payload") if isinstance(plan.get("payload"), dict) else {}
            unresolved.append(
                {
                    "action_ref": plan.get("action_ref"),
                    "op_id": plan.get("op_id"),
                    "category": plan.get("category"),
                    "action": plan.get("action"),
                    "target_id": plan.get("target_id"),
                    "source_paper_id": plan.get("source_paper_id"),
                    "blocked_reasons": blocked or ["not_auto_applyable"],
                    "confidence": payload.get("confidence"),
                    "evidence_ids": payload.get("evidence_ids") or [],
                    "reason": payload.get("reason"),
                    "requires_local_ai": True,
                }
            )
        return unresolved

    @staticmethod
    def _rag_quality_unresolved_actions(figure_rag_quality: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for item in figure_rag_quality.get("blocked_items") or []:
            if not isinstance(item, dict):
                continue
            actions.append(
                {
                    "code": "figure_rag_quality_incomplete",
                    "category": "figure",
                    "action": "REPAIR_METADATA",
                    "target_id": item.get("source_id"),
                    "figure_label": item.get("figure_label"),
                    "page": item.get("page"),
                    "blocked_reasons": list(item.get("reasons") or ["figure_rag_quality_incomplete"]),
                    "reason": "Figure has completed review actions but is still not RAG-ready.",
                    "requires_local_ai": True,
                }
            )
        if actions:
            return actions
        if int(figure_rag_quality.get("blocked") or 0) > 0:
            return [
                {
                    "code": "figure_rag_quality_incomplete",
                    "category": "figure",
                    "action": "REPAIR_METADATA",
                    "target_id": None,
                    "blocked_reasons": ["figure_rag_quality_incomplete"],
                    "reason": "One or more figures are still not RAG-ready.",
                    "requires_local_ai": True,
                }
            ]
        return []

    def _current_scope_completion(
        self,
        materials: dict[str, Any],
        *,
        run_id: UUID | None = None,
        reviewed_after: datetime | None = None,
        changed_ids: dict[str, set[str]] | None = None,
    ) -> dict[str, Any]:
        figure_ids = set(materials.get("figure_id_map") or set())
        table_ids = set(materials.get("table_id_map") or set())
        reviewed_figures = self._reviewed_object_ids(
            object_ids=figure_ids,
            target_type="paper_figure",
            positive_actions=FIGURE_REVIEWED_ACTIONS,
            external_target_types={"figure", "figures", "paper_figure", "paper_figures"},
            run_id=run_id,
            require_local_ai_verification=True,
        )
        reviewed_tables = self._reviewed_object_ids(
            object_ids=table_ids,
            target_type="paper_table",
            positive_actions=TABLE_REVIEWED_ACTIONS,
            external_target_types={"table", "tables", "paper_table", "paper_tables"},
            run_id=run_id,
        )
        changed_ids = changed_ids or {"figures": set(), "tables": set()}
        changed_figure_ids = set(changed_ids.get("figures") or set()) & figure_ids
        changed_table_ids = set(changed_ids.get("tables") or set()) & table_ids
        reviewed_changed_figures = (
            self._reviewed_object_ids(
                object_ids=changed_figure_ids,
                target_type="paper_figure",
                positive_actions=FIGURE_REVIEWED_ACTIONS,
                external_target_types={"figure", "figures", "paper_figure", "paper_figures"},
                created_after=reviewed_after,
                run_id=run_id,
                require_local_ai_verification=True,
            )
            if reviewed_after is not None and changed_figure_ids
            else changed_figure_ids
        )
        reviewed_changed_tables = (
            self._reviewed_object_ids(
                object_ids=changed_table_ids,
                target_type="paper_table",
                positive_actions=TABLE_REVIEWED_ACTIONS,
                external_target_types={"table", "tables", "paper_table", "paper_tables"},
                created_after=reviewed_after,
                run_id=run_id,
            )
            if reviewed_after is not None and changed_table_ids
            else changed_table_ids
        )
        missing_figures = sorted(figure_ids - reviewed_figures)
        missing_tables = sorted(table_ids - reviewed_tables)
        missing_changed_figures = sorted(changed_figure_ids - reviewed_changed_figures)
        missing_changed_tables = sorted(changed_table_ids - reviewed_changed_tables)
        missing_figures = sorted(set(missing_figures) | set(missing_changed_figures))
        missing_tables = sorted(set(missing_tables) | set(missing_changed_tables))
        return {
            "complete": not missing_figures and not missing_tables,
            "figure_scope": "main_all_plus_dft_related_supplementary",
            "table_scope": "main_and_supplementary_all",
            "expected_figure_ids": sorted(figure_ids),
            "expected_table_ids": sorted(table_ids),
            "reviewed_figure_ids": sorted(reviewed_figures),
            "reviewed_table_ids": sorted(reviewed_tables),
            "changed_figure_ids": sorted(changed_figure_ids),
            "changed_table_ids": sorted(changed_table_ids),
            "missing_figure_ids": missing_figures,
            "missing_table_ids": missing_tables,
        }

    def _reviewed_object_ids(
        self,
        *,
        object_ids: set[str],
        target_type: str,
        positive_actions: set[str],
        external_target_types: set[str],
        created_after: datetime | None = None,
        run_id: UUID | None = None,
        require_local_ai_verification: bool = False,
    ) -> set[str]:
        if not object_ids:
            return set()
        reviewed: set[str] = set()
        rows = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.target_type == target_type)
            .where(AuditLog.target_id.in_(object_ids))
            .where(AuditLog.created_at > created_after if created_after is not None else True)
            .order_by(AuditLog.target_id.asc(), AuditLog.created_at.desc(), AuditLog.id.desc())
        ).all()
        seen: set[str] = set()
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            payload_run_id = self._optional_uuid(payload.get("run_id") or payload.get("chart_run_id"))
            if run_id is not None and payload_run_id != run_id:
                continue
            if run_id is None and payload_run_id is not None:
                continue
            target_id = str(row.target_id or "")
            if not target_id or target_id in seen:
                continue
            seen.add(target_id)
            action = self._audit_review_action(row)
            payload = row.payload if isinstance(row.payload, dict) else {}
            action_payload = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            trusted_local_verification = (
                payload.get("actor_type") == "local_ai"
                and self._has_local_ai_verification(action_payload.get("local_ai_verification"))
            )
            if action in positive_actions and (
                not require_local_ai_verification or trusted_local_verification
            ):
                reviewed.add(target_id)
        external_reviewed = (
            set()
            if require_local_ai_verification
            else self._external_reviewed_object_ids(
                object_ids=object_ids,
                external_target_types=external_target_types,
                created_after=created_after,
                run_id=run_id,
            )
        )
        return reviewed | external_reviewed

    @staticmethod
    def _audit_review_action(row: AuditLog) -> str:
        payload = row.payload if isinstance(row.payload, dict) else {}
        action_payload = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        table_result = payload.get("table_result") if isinstance(payload.get("table_result"), dict) else {}
        action = (
            action_payload.get("action")
            or table_result.get("action")
            or payload.get("action")
            or row.action
        )
        return str(action or "").strip().upper()

    def _external_reviewed_object_ids(
        self,
        *,
        object_ids: set[str],
        external_target_types: set[str],
        created_after: datetime | None = None,
        run_id: UUID | None = None,
    ) -> set[str]:
        if not object_ids:
            return set()
        reviewed: set[str] = set()
        normalized_types = {item.strip().lower() for item in external_target_types}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
            .where(ExternalAnalysisCandidate.status.in_(FINALIZED_REVIEW_STATUSES))
            .where(ExternalAnalysisCandidate.created_at > created_after if created_after is not None else True)
            .order_by(ExternalAnalysisCandidate.created_at.desc(), ExternalAnalysisCandidate.id.desc())
        ).all()
        for candidate in candidates:
            if run_id is not None and candidate.run_id != run_id:
                continue
            if run_id is None:
                continue
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_id = str(payload.get("target_id") or candidate.materialized_target_id or "").strip()
            target_type = str(payload.get("target_type") or candidate.materialized_target_type or "").strip().lower()
            decision = str(payload.get("decision") or payload.get("verdict") or "").strip().upper()
            if target_id in object_ids and target_type in normalized_types and decision in POSITIVE_REVIEW_DECISIONS:
                reviewed.add(target_id)
        return reviewed

    @staticmethod
    def _changed_snapshot_object_ids(previous: Any, current: dict[str, Any]) -> dict[str, set[str]]:
        if not isinstance(previous, dict):
            return {
                "figures": {str(item.get("id")) for item in current.get("figures") or [] if item.get("id")},
                "tables": {str(item.get("id")) for item in current.get("tables") or [] if item.get("id")},
            }

        def changed_ids(kind: str) -> set[str]:
            previous_by_id = {
                str(item.get("id")): item
                for item in previous.get(kind) or []
                if isinstance(item, dict) and item.get("id")
            }
            current_by_id = {
                str(item.get("id")): item
                for item in current.get(kind) or []
                if isinstance(item, dict) and item.get("id")
            }
            changed: set[str] = set()
            for item_id, item in current_by_id.items():
                if previous_by_id.get(item_id) != item:
                    changed.add(item_id)
            return changed

        return {
            "figures": changed_ids("figures"),
            "tables": changed_ids("tables"),
        }

    @staticmethod
    def _workspace_crop_metadata_from_prov(prov: Any, *, image_available: bool) -> dict[str, Any]:
        entries = prov if isinstance(prov, list) else []
        extraction = next(
            (item for item in reversed(entries) if isinstance(item, dict) and item.get("image_extraction")),
            None,
        )
        if image_available and extraction:
            return {
                "crop_status": "candidate_crop",
                "crop_confidence": extraction.get("confidence"),
                "crop_source": extraction.get("source") or extraction.get("image_extraction"),
            }
        if image_available:
            return {"crop_status": "needs_recrop", "crop_confidence": None, "crop_source": "legacy_image"}
        return {"crop_status": "caption_only", "crop_confidence": None, "crop_source": "caption"}

    @classmethod
    def _is_known_workspace_crop_metadata_drift(cls, previous: Any, current: dict[str, Any]) -> bool:
        """Accept only the historical workspace-refresh regression, never a real edit."""
        if not isinstance(previous, dict):
            return False
        changed_any = False
        for kind in ("figures", "tables"):
            previous_by_id = {
                str(item.get("id")): item for item in previous.get(kind) or []
                if isinstance(item, dict) and item.get("id")
            }
            current_by_id = {
                str(item.get("id")): item for item in current.get(kind) or []
                if isinstance(item, dict) and item.get("id")
            }
            if previous_by_id.keys() != current_by_id.keys():
                return False
            for item_id, before in previous_by_id.items():
                after = current_by_id[item_id]
                if before == after:
                    continue
                if kind != "figures":
                    return False
                changed_fields = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
                if changed_fields - {"crop_status", "crop_source", "crop_confidence"}:
                    return False
                has_review_crop = any(
                    isinstance(entry, dict)
                    and entry.get("action") == "offline_evidence_review_crop"
                    and str(entry.get("source_action") or "").upper() == "RECROP"
                    for entry in reversed(before.get("prov") or [])
                )
                if not has_review_crop:
                    return False
                expected = cls._workspace_crop_metadata_from_prov(
                    after.get("prov"), image_available=bool(after.get("image_available")),
                )
                if any(after.get(key) != value for key, value in expected.items()):
                    return False
                if before.get("crop_status") != "recropped" or before.get("crop_source") != "offline_evidence_review":
                    return False
                changed_any = True
        return changed_any

    @staticmethod
    def _figure_action_modifies_state(action: OfflineEvidenceReviewFigureAction) -> bool:
        if action.action in {"RECROP", "CREATE", "REJECT"}:
            return True
        if action.action != "KEEP":
            return False
        return any(
            value is not None
            for value in (
                action.caption,
                action.figure_label,
                action.figure_role,
                action.content_summary,
                action.key_elements,
            )
        )

    @staticmethod
    def _final_status_errors(
        *,
        result: OfflineEvidenceReviewResult,
        validation: dict[str, Any],
        applied: list[dict[str, Any]],
        refreshed: dict[str, Any],
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        unresolved = list(validation.get("unresolved_actions") or [])
        if unresolved:
            errors.append({"code": "unresolved_actions_present", "unresolved_actions": unresolved})
        covered_figures = {
            str(action.figure_id)
            for action in result.figure_actions
            if action.figure_id and action.action in {"KEEP", "RECROP", "NEEDS_HUMAN"}
        }
        covered_tables = {
            str(action.table_id)
            for action in result.table_actions
            if action.table_id and action.action in {"KEEP", "UPDATE", "NEEDS_HUMAN"}
        }
        for item in applied:
            if item.get("target_id"):
                if item.get("category") == "figure":
                    covered_figures.add(str(item["target_id"]))
                if item.get("category") == "table":
                    covered_tables.add(str(item["target_id"]))
        missing_figures = sorted(refreshed["figure_id_map"] - covered_figures)
        missing_tables = sorted(refreshed["table_id_map"] - covered_tables)
        if missing_figures:
            errors.append({"code": "finalize_incomplete_figure_status", "missing_figure_ids": missing_figures})
        if missing_tables:
            errors.append({"code": "finalize_incomplete_table_status", "missing_table_ids": missing_tables})
        figure_rag_quality = refreshed.get("figure_rag_quality") or {}
        if int(figure_rag_quality.get("blocked") or 0) > 0:
            errors.append(
                {
                    "code": "finalize_figure_rag_quality_incomplete",
                    "blocked_count": figure_rag_quality.get("blocked"),
                    "blocked_reasons": figure_rag_quality.get("blocked_reasons") or {},
                    "blocked_items": (figure_rag_quality.get("blocked_items") or [])[:50],
                }
            )
        return errors

    def _mark_figures_review_completed(self, paper_id: UUID, reviewer: str) -> None:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            return
        analysis = dict(paper.comprehensive_analysis or {})
        raw_progress = analysis.get("manual_review_progress") if isinstance(analysis.get("manual_review_progress"), dict) else {}

        def normalize_entry(module: str) -> dict[str, Any]:
            raw = raw_progress.get(module)
            if isinstance(raw, dict):
                return {
                    "completed": bool(raw.get("completed")),
                    "updated_at": raw.get("updated_at"),
                    "updated_by": raw.get("updated_by"),
                }
            return {"completed": bool(raw), "updated_at": None, "updated_by": None}

        progress = {
            "content": normalize_entry("content"),
            "figures": normalize_entry("figures"),
            "dft": normalize_entry("dft"),
        }
        progress["figures"] = {
            "completed": True,
            "updated_at": _utc_iso(),
            "updated_by": reviewer,
        }
        analysis["manual_review_progress"] = progress
        paper.comprehensive_analysis = analysis
        self.session.add(paper)

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        if not value:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("invalid_external_analysis_run_id")

    @classmethod
    def _payload_run_id(cls, payload: dict[str, Any]) -> UUID | None:
        run_id = cls._optional_uuid(payload.get("run_id"))
        chart_run_id = cls._optional_uuid(payload.get("chart_run_id"))
        if run_id is not None and chart_run_id is not None and run_id != chart_run_id:
            raise ValueError("chart_run_id and run_id must match")
        return chart_run_id or run_id

    def get_review_scope_options(self, paper_id: UUID, *, selected_run_id: UUID | None = None) -> dict[str, Any]:
        """Expose scope choices without silently selecting one for a write path."""
        paper_task = self.get_review_task(paper_id)
        runs = self.session.scalars(
            select(ExternalAnalysisRun)
            .where(ExternalAnalysisRun.paper_id == paper_id)
            .order_by(ExternalAnalysisRun.created_at.desc(), ExternalAnalysisRun.id.desc())
        ).all()
        options: list[dict[str, Any]] = []
        for run in runs:
            figure_ids, table_ids = self._run_target_ids(paper_id, run.id)
            if not figure_ids and not table_ids:
                continue
            task = self.get_review_task(paper_id, run_id=run.id)
            candidate_count = self.session.scalar(
                select(func.count(ExternalAnalysisCandidate.id)).where(
                    ExternalAnalysisCandidate.run_id == run.id,
                    ExternalAnalysisCandidate.paper_id == paper_id,
                )
            ) or 0
            options.append({
                "chart_run_id": str(run.id),
                "run_id": str(run.id),
                "paper_id": str(paper_id),
                "source": run.source,
                "source_label": run.source_label,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "candidate_count": int(candidate_count),
                "counts": {"figures": len(figure_ids), "tables": len(table_ids)},
                "stage_status": task.get("stage_status"),
                "unresolved_count": int(task.get("unresolved_count") or 0),
                "completed_snapshot_fingerprint": task.get("completed_snapshot_fingerprint"),
                "current_snapshot_fingerprint": task.get("current_snapshot_fingerprint"),
                "reviewed_at": task.get("reviewed_at"),
                "scope_type": "external_analysis_run",
                "selected": selected_run_id == run.id,
                # The scope signature intentionally excludes the run UUID.  Re-running
                # the same prompt against the same targets should not turn into several
                # indistinguishable choices in the review centre.
                "target_signature": json.dumps({
                    "source": str(run.source or "").strip().lower(),
                    "source_label": str(run.source_label or "").strip(),
                    "figures": sorted(figure_ids),
                    "tables": sorted(table_ids),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            })
        duplicate_groups: dict[str, list[dict[str, Any]]] = {}
        for option in options:
            duplicate_groups.setdefault(str(option["target_signature"]), []).append(option)
        for group_key, group in duplicate_groups.items():
            # Preserve the existing newest-first ordering, except that a completed
            # run is always the representative when a duplicate group has one.
            representative = next(
                (item for item in group if item.get("stage_status") == "completed"),
                group[0],
            )
            for item in group:
                item["duplicate_group_key"] = group_key
                item["duplicate_run_count"] = len(group)
                item["is_duplicate_representative"] = item is representative
        completed = sorted(
            [item for item in options if item.get("stage_status") == "completed"],
            # Main-figure completion is the DFT prerequisite.  A completed
            # table-only pass must not displace a completed main-figure pass.
            key=lambda item: (
                int((item.get("counts") or {}).get("figures") or 0) > 0,
                item.get("reviewed_at") or item.get("created_at") or "",
            ),
            reverse=True,
        )
        primary = next((item for item in options if item.get("chart_run_id") == str(selected_run_id)), None)
        if primary is None and completed:
            primary = completed[0]
        if primary is not None:
            primary["is_primary_completed_run"] = True
        return {
            "paper_scope": {**paper_task, "scope_type": "paper", "chart_run_id": None},
            "chart_runs": options,
            "chart_run_count": len(options),
            "selected_chart_run_id": str(selected_run_id) if selected_run_id else None,
            "primary_completed_run": primary,
        }

    def _run_target_ids(self, paper_id: UUID, run_id: UUID) -> tuple[set[str], set[str]]:
        run = self.session.get(ExternalAnalysisRun, run_id)
        if run is None or run.paper_id != paper_id:
            raise LookupError("external_analysis_run_not_found_for_paper")
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.run_id == run_id,
                ExternalAnalysisCandidate.paper_id == paper_id,
            )
        ).all()
        figure_ids: set[str] = set()
        table_ids: set[str] = set()
        for candidate in candidates:
            kind = str(candidate.candidate_type or "").lower()
            if "dft" in kind:
                continue
            payloads = [candidate.normalized_payload, candidate.evidence_payload]
            def visit(value: Any) -> None:
                if isinstance(value, dict):
                    for key, item in value.items():
                        key_lower = str(key).lower()
                        if key_lower in {"target_path", "path"} and item:
                            path_parts = str(item).split(":")
                            if len(path_parts) >= 2 and path_parts[0].lower() in {"figure", "figures"}:
                                figure_ids.add(path_parts[1])
                            elif len(path_parts) >= 2 and path_parts[0].lower() in {"table", "tables"}:
                                table_ids.add(path_parts[1])
                        if key_lower in {"figure_id", "figure_uuid"} and item:
                            figure_ids.add(str(item))
                        elif key_lower in {"table_id", "table_uuid"} and item:
                            table_ids.add(str(item))
                        elif key_lower in {"target_id", "object_id", "source_record_id"} and item:
                            target_type = str(value.get("target_type") or value.get("object_type") or "").lower()
                            if "figure" in target_type:
                                figure_ids.add(str(item))
                            elif "table" in target_type:
                                table_ids.add(str(item))
                        visit(item)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)
            for payload in payloads:
                visit(payload)
        return figure_ids, table_ids

    def _build_materials(self, paper_id: UUID, *, run_id: UUID | None = None) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        if not str(paper.paper_code or "").strip():
            raise ValueError("paper_code_required_before_offline_evidence_review_export")

        source_papers = linked_source_papers(
            self.session,
            paper,
            relationship_types=SUPPLEMENTARY_RELATIONSHIP_TYPES,
        )
        source_ids = [item["paper"].id for item in source_papers]
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(source_ids))).all()
        all_figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(source_ids))).all()
        source_by_id = {item["paper"].id: item for item in source_papers}
        dft_referenced_figure_ids = self._dft_referenced_figure_ids(source_ids, all_figures)
        excluded_duplicate_figures: list[dict[str, Any]] = []
        scope_candidate_figures = all_figures
        if run_id is None:
            scope_candidate_figures, excluded_duplicate_figures = self._deduplicate_scope_figures(
                all_figures,
                source_by_id=source_by_id,
            )
            for exclusion in excluded_duplicate_figures:
                if exclusion["excluded_figure_id"] in dft_referenced_figure_ids:
                    dft_referenced_figure_ids.add(exclusion["canonical_figure_id"])
        relevant_figures = [
            row
            for row in scope_candidate_figures
            if self._include_figure_in_bundle(
                row,
                source_by_id=source_by_id,
                referenced_by_dft=str(row.id) in dft_referenced_figure_ids,
            )
        ]
        figures = relevant_figures
        if run_id is not None:
            figure_ids, table_ids = self._run_target_ids(paper_id, run_id)
            figures = [row for row in figures if str(row.id) in figure_ids]
            tables = [row for row in tables if str(row.id) in table_ids]
            if not figures and not tables:
                raise ValueError("chart_review_no_targets_for_run")
        excluded_support_figure_count = sum(
            1
            for row in scope_candidate_figures
            if source_by_id[row.paper_id]["prefix"] == "si" and row not in relevant_figures
        )
        warnings: list[str] = []
        source_documents = []
        for item in source_papers:
            source_doc = self._source_document_payload(item)
            warnings.extend(source_doc.pop("_warnings", []))
            source_documents.append(source_doc)
        source_pdf_inventory = build_source_pdf_inventory(
            source_documents,
            max_count=MAX_SOURCE_PDF_COUNT,
            max_total_bytes=MAX_TOTAL_SOURCE_PDF_BYTES,
        )
        page_geometry = {
            "schema_version": "offline_source_page_geometry_v1",
            "source_documents": [
                {
                    "source_paper_id": item["paper_id"],
                    "role": item["role"],
                    "paper_code": item.get("paper_code"),
                    "pdf_available": item.get("pdf_available"),
                    "page_count": item.get("page_count"),
                    "page_sizes": item.get("page_sizes", []),
                }
                for item in source_documents
            ],
        }
        extracted_tables = self._tables(source_by_id=source_by_id, tables=tables)
        extracted_figures = self._figures(source_by_id=source_by_id, figures=figures, source_documents=source_documents)
        figure_rag_quality = build_figure_rag_quality_summary(self.session, figures)
        evidence_map = {
            item["evidence_id"]: item
            for item in [*extracted_tables, *extracted_figures]
        }

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
            "source_pdf_inventory": public_source_pdf_inventory(source_pdf_inventory),
            "excluded_duplicate_figures": excluded_duplicate_figures,
        }
        fingerprint_payload = {
            "schema_version": OFFLINE_EVIDENCE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "paper_metadata": paper_metadata,
            "source_documents": self._public_source_documents(source_documents, include_bundle_file=False),
            "source_pdf_inventory": public_source_pdf_inventory(source_pdf_inventory),
            "excluded_duplicate_figures": excluded_duplicate_figures,
            "page_geometry": page_geometry,
            "extracted_tables": self._public_records(extracted_tables, include_bundle_file=False),
            "extracted_figures": self._public_records(extracted_figures, include_bundle_file=False),
        }
        bundle_fingerprint = _sha256(_canonical_json_bytes(fingerprint_payload))
        if not extracted_tables:
            warnings.append("no_tables_in_scope")
        if not extracted_figures:
            warnings.append("no_figures_in_scope")
        if excluded_support_figure_count:
            warnings.append(f"excluded_non_dft_supplementary_figures:{excluded_support_figure_count}")
        if excluded_duplicate_figures:
            warnings.append(f"excluded_duplicate_figures:{len(excluded_duplicate_figures)}")

        return {
            "paper_metadata": paper_metadata,
            "source_documents": source_documents,
            "source_pdf_inventory": source_pdf_inventory,
            "excluded_duplicate_figures": excluded_duplicate_figures,
            "page_geometry": page_geometry,
            "extracted_tables": extracted_tables,
            "extracted_figures": extracted_figures,
            "figure_record_by_id": {str(item["source_record_id"]): item for item in extracted_figures},
            "figure_rag_quality": figure_rag_quality,
            "evidence_map": evidence_map,
            "figure_id_map": {str(row.id) for row in figures},
            "table_id_map": {str(row.id) for row in tables},
            "source_paper_ids": {str(item["paper"].id) for item in source_papers},
            "bundle_fingerprint": bundle_fingerprint,
            "warnings": warnings,
            "run_id": str(run_id) if run_id else None,
            "chart_run_id": str(run_id) if run_id else None,
            "scope_type": "external_analysis_run" if run_id else "paper",
        }

    @classmethod
    def _scope_snapshot(cls, materials: dict[str, Any]) -> dict[str, Any]:
        def normalize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = []
            for item in cls._public_records(items, include_bundle_file=False):
                row = dict(item)
                row["id"] = row.get("source_record_id") or row.get("id")
                rows.append(row)
            return rows
        return {
            "schema_version": "figure_table_content_snapshot_scoped_v1",
            "paper_id": materials["paper_metadata"]["paper_id"],
            "run_id": materials.get("run_id"),
            "figures": normalize(materials["extracted_figures"]),
            "tables": normalize(materials["extracted_tables"]),
            "fingerprint": materials["bundle_fingerprint"],
        }

    @classmethod
    def _include_figure_in_bundle(
        cls,
        row: PaperFigure,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        referenced_by_dft: bool = False,
    ) -> bool:
        source = source_by_id[row.paper_id]
        return include_figure_in_chart_review_scope(
            row,
            source_prefix=source["prefix"],
            referenced_by_dft=referenced_by_dft,
        )

    def _dft_referenced_figure_ids(
        self,
        source_ids: list[UUID],
        figures: list[PaperFigure],
    ) -> set[str]:
        figure_ids = {row.id for row in figures}
        if not figure_ids:
            return set()
        referenced = {
            str(figure_id)
            for figure_id in self.session.scalars(
                select(EvidenceLocator.figure_id).where(
                    EvidenceLocator.figure_id.in_(figure_ids),
                    EvidenceLocator.target_type.in_(("dft_result", "dft_results")),
                )
            ).all()
            if figure_id is not None
        }
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(source_ids))).all()
        figure_id_strings = {str(row.id) for row in figures}
        source_figure_tokens = {
            self._normalized_figure_text(row.source_figure)
            for row in dft_rows
            if self._normalized_figure_text(row.source_figure)
        }

        def collect_figure_ids(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in {"figure_id", "figure_uuid"} and str(item) in figure_id_strings:
                        referenced.add(str(item))
                    collect_figure_ids(item)
            elif isinstance(value, list):
                for item in value:
                    collect_figure_ids(item)

        for row in dft_rows:
            collect_figure_ids(row.evidence_payload)
        completed_chart_audits = self.session.scalars(
            select(AuditLog).where(
                AuditLog.paper_id.in_(source_ids),
                AuditLog.action == "offline_evidence_review_applied",
            )
        ).all()
        for audit in completed_chart_audits:
            payload = audit.payload if isinstance(audit.payload, dict) else {}
            for candidate in payload.get("dft_evidence_candidates") or []:
                if not isinstance(candidate, dict) or str(candidate.get("source_kind") or "").lower() != "figure":
                    continue
                source_record_id = str(candidate.get("source_record_id") or "")
                if source_record_id in figure_id_strings:
                    referenced.add(source_record_id)
        for figure in figures:
            normalized_figure = self._normalized_figure_text(f"{figure.figure_label or ''} {figure.caption or ''}")
            if any(
                token and f" {token} " in f" {normalized_figure} "
                for token in source_figure_tokens
            ):
                referenced.add(str(figure.id))
        return referenced

    @staticmethod
    def _normalized_figure_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @classmethod
    def _duplicate_figure_reason(cls, left: PaperFigure, right: PaperFigure) -> str | None:
        if left.paper_id != right.paper_id or left.page != right.page:
            return None
        left_caption = cls._normalized_figure_text(left.caption)
        right_caption = cls._normalized_figure_text(right.caption)
        if left.image_path and right.image_path and str(left.image_path) == str(right.image_path):
            return "same_page_same_image_reference"
        if len(left_caption) >= 20 and left_caption == right_caption:
            return "same_page_same_normalized_caption"
        if (
            len(left_caption) >= 40
            and len(right_caption) >= 40
            and SequenceMatcher(None, left_caption, right_caption).ratio() >= 0.97
        ):
            return "same_page_highly_similar_caption"
        return None

    @classmethod
    def _canonical_figure_score(cls, row: PaperFigure) -> tuple[int, int, int, int, int]:
        label = str(row.figure_label or "").strip()
        role = str(row.figure_role or "").strip().lower()
        formal_label = bool(re.match(r"^(?:figure|fig\.?)\s*s?\d+\b", label, re.IGNORECASE))
        meaningful_role = role not in {"", "unknown", "unclassified", "other"}
        return (
            int(formal_label),
            int(meaningful_role),
            int(bool(str(row.content_summary or "").strip())),
            len(row.key_elements or []),
            int(bool(row.image_path)),
        )

    @classmethod
    def _deduplicate_scope_figures(
        cls,
        figures: list[PaperFigure],
        *,
        source_by_id: dict[UUID, dict[str, Any]],
    ) -> tuple[list[PaperFigure], list[dict[str, Any]]]:
        si_figures = [row for row in figures if source_by_id[row.paper_id]["prefix"] == "si"]
        parent = {str(row.id): str(row.id) for row in si_figures}

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        for index, left in enumerate(si_figures):
            for right in si_figures[index + 1:]:
                reason = cls._duplicate_figure_reason(left, right)
                if not reason:
                    continue
                left_root = find(str(left.id))
                right_root = find(str(right.id))
                if left_root != right_root:
                    parent[right_root] = left_root
        groups: dict[str, list[PaperFigure]] = {}
        for row in si_figures:
            groups.setdefault(find(str(row.id)), []).append(row)
        excluded_ids: set[str] = set()
        exclusions: list[dict[str, Any]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            canonical = sorted(
                group,
                key=lambda row: tuple(-value for value in cls._canonical_figure_score(row)) + (str(row.id),),
            )[0]
            for row in sorted(group, key=lambda item: str(item.id)):
                if row.id == canonical.id:
                    continue
                excluded_ids.add(str(row.id))
                reason = cls._duplicate_figure_reason(row, canonical) or cls._duplicate_figure_reason(canonical, row) or "duplicate_scope_object"
                exclusions.append({
                    "source_paper_id": str(row.paper_id),
                    "page": row.page,
                    "excluded_figure_id": str(row.id),
                    "excluded_figure_label": row.figure_label,
                    "canonical_figure_id": str(canonical.id),
                    "canonical_figure_label": canonical.figure_label,
                    "reason": reason,
                })
        return [row for row in figures if str(row.id) not in excluded_ids], exclusions

    def _source_document_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        paper: Paper = item["paper"]
        warnings: list[str] = []
        pdf_abs_path = self._resolve_pdf(paper.pdf_path)
        pdf_available = pdf_abs_path is not None
        page_count = None
        page_sizes: list[dict[str, Any]] = []
        pdf_sha256 = None
        pdf_size_bytes = None
        if pdf_abs_path is not None:
            pdf_size_bytes = pdf_abs_path.stat().st_size
            pdf_sha256 = _file_sha256(pdf_abs_path)
            geometry, geometry_warning = self._pdf_geometry(pdf_abs_path)
            if geometry_warning:
                warnings.append(geometry_warning)
            page_count = len(geometry)
            page_sizes = geometry
        else:
            warnings.append(f"missing_pdf:{paper.paper_code or paper.id}")
        return {
            "source_document_type": item["source_document_type"],
            "role": item["prefix"],
            "relationship_id": item.get("relationship_id"),
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "title": paper.title,
            "doi": paper.doi,
            "pdf_available": pdf_available,
            "pdf_sha256": pdf_sha256,
            "pdf_size_bytes": pdf_size_bytes,
            "page_count": page_count,
            "page_sizes": page_sizes,
            "_pdf_abs_path": str(pdf_abs_path) if pdf_abs_path is not None else None,
            "_warnings": warnings,
        }

    def _tables(self, *, source_by_id: dict[UUID, dict[str, Any]], tables: list[PaperTable]) -> list[dict[str, Any]]:
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
            source = source_by_id[row.paper_id]
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:table:{counters[source['prefix']]:03d}"
            haystack = f"{row.caption or ''}\n{row.markdown_content or ''}"
            items.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_kind": "table",
                    "source_document_type": source["source_document_type"],
                    "source_paper_id": str(row.paper_id),
                    "source_paper_code": source["paper"].paper_code,
                    "source_record_id": str(row.id),
                    "caption": row.caption,
                    "page": row.page,
                    "markdown_content": row.markdown_content,
                    "extraction_source": row.extraction_source,
                    "prov": self._sanitize_for_bundle(row.prov),
                    "content_sha256": _sha256(haystack.encode("utf-8")),
                }
            )
        return items

    def _figures(
        self,
        *,
        source_by_id: dict[UUID, dict[str, Any]],
        figures: list[PaperFigure],
        source_documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        geometry_by_source = {item["paper_id"]: item.get("page_sizes") or [] for item in source_documents}
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
            source = source_by_id[row.paper_id]
            counters[source["prefix"]] += 1
            evidence_id = f"{source['prefix']}:figure:{counters[source['prefix']]:03d}"
            artifact = self._resolve_figure(row.image_path)
            artifact_hash = _file_sha256(artifact) if artifact is not None else None
            artifact_size = artifact.stat().st_size if artifact is not None else None
            haystack = f"{row.figure_label or ''}\n{row.caption or ''}\n{row.content_summary or ''}\n{row.key_elements or ''}"
            current_bbox = self._last_bbox_from_prov(row.prov)
            page_sizes = geometry_by_source.get(str(row.paper_id)) or []
            current_bbox_norm = self._bbox_to_norm(current_bbox, row.page, page_sizes)
            items.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_kind": "figure",
                    "source_document_type": source["source_document_type"],
                    "source_paper_id": str(row.paper_id),
                    "source_paper_code": source["paper"].paper_code,
                    "source_record_id": str(row.id),
                    "figure_label": row.figure_label,
                    "caption": row.caption,
                    "page": row.page,
                    "figure_role": row.figure_role,
                    "role_confidence": row.role_confidence,
                    "content_summary": row.content_summary,
                    "key_elements": row.key_elements,
                    "crop_status": row.crop_status,
                    "crop_confidence": row.crop_confidence,
                    "crop_source": row.crop_source,
                    "current_bbox_norm": current_bbox_norm,
                    "current_bbox_pdf": current_bbox,
                    "image_available": artifact is not None,
                    "image_sha256": artifact_hash,
                    "image_size_bytes": artifact_size,
                    "content_sha256": _sha256(haystack.encode("utf-8")),
                    "prov": self._sanitize_for_bundle(row.prov),
                    "_image_abs_path": str(artifact) if artifact is not None else None,
                }
            )
        return items

    def _apply_figure_metadata(self, figure: PaperFigure, action: OfflineEvidenceReviewFigureAction) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if action.caption is not None and figure.caption != action.caption:
            figure.caption = action.caption
            updates["caption"] = action.caption
        if action.figure_label is not None and figure.figure_label != action.figure_label:
            figure.figure_label = action.figure_label
            updates["figure_label"] = action.figure_label
        if action.figure_role is not None and figure.figure_role != action.figure_role:
            figure.figure_role = action.figure_role
            figure.role_confidence = action.confidence
            updates["figure_role"] = action.figure_role
            updates["role_confidence"] = action.confidence
        if action.content_summary is not None:
            normalized = normalize_figure_content_summary(action.content_summary, figure.caption)
            if figure.content_summary != normalized:
                figure.content_summary = normalized
                updates["content_summary"] = normalized
        if action.key_elements is not None:
            normalized_keys = normalize_figure_key_elements(action.key_elements)[0]
            if figure.key_elements != normalized_keys:
                figure.key_elements = normalized_keys
                updates["key_elements"] = normalized_keys
        if action.action == "KEEP" and updates:
            figure.crop_status = "candidate_crop" if figure.crop_status == "needs_review" else figure.crop_status
        return updates

    def _render_pdf_crop(self, *, paper: Paper, page: int, bbox_norm: list[float]) -> tuple[str, list[float], dict[str, int]]:
        pdf_abs_path = self._resolve_pdf(paper.pdf_path)
        if pdf_abs_path is None:
            raise ValueError("PDF file not found for deterministic crop")
        import fitz

        doc = fitz.open(str(pdf_abs_path))
        try:
            page_index = page - 1
            if page_index < 0 or page_index >= len(doc):
                raise ValueError(f"Page {page} is out of bounds for this PDF")
            pdf_page = doc[page_index]
            page_rect = pdf_page.rect
            target_rect = fitz.Rect(
                page_rect.x0 + bbox_norm[0] * page_rect.width,
                page_rect.y0 + bbox_norm[1] * page_rect.height,
                page_rect.x0 + bbox_norm[2] * page_rect.width,
                page_rect.y0 + bbox_norm[3] * page_rect.height,
            ).intersect(page_rect)
            if target_rect.is_empty:
                raise ValueError("Calculated crop rectangle is empty or invalid")
            pix = pdf_page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=target_rect, alpha=False)
            filename = f"{paper.id}_fig_{uuid.uuid4().hex[:8]}.png"
            rel_path = f"{paper.id}/{filename}"
            abs_path = self.settings.storage_paths["figures"] / str(paper.id) / filename
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(abs_path))
            return rel_path, [target_rect.x0, target_rect.y0, target_rect.x1, target_rect.y1], {"width": pix.width, "height": pix.height}
        finally:
            doc.close()

    @staticmethod
    def _figure_prov(
        action: OfflineEvidenceReviewFigureAction,
        bbox_used: list[float],
        pixel_size: dict[str, int],
        reviewer: str,
        bundle_fingerprint: str,
    ) -> dict[str, Any]:
        return {
            "action": "offline_evidence_review_crop",
            "source_action": action.action,
            "bbox": {
                "l": bbox_used[0],
                "t": bbox_used[1],
                "r": bbox_used[2],
                "b": bbox_used[3],
                "coord_origin": "TOPLEFT",
            },
            "bbox_norm": action.bbox_norm,
            "page_no": action.page,
            "pixel_size": pixel_size,
            "created_by": reviewer,
            "bundle_fingerprint": bundle_fingerprint,
            "reason": action.reason,
        }

    @staticmethod
    def _figure_keep_prov(
        action: OfflineEvidenceReviewFigureAction,
        reviewer: str,
        bundle_fingerprint: str,
    ) -> dict[str, Any]:
        return {
            "action": "offline_evidence_review_keep",
            "source_action": "KEEP",
            "created_by": reviewer,
            "bundle_fingerprint": bundle_fingerprint,
            "reason": action.reason,
        }

    def _updated_table_prov(
        self,
        table: PaperTable,
        action: OfflineEvidenceReviewTableAction,
        bundle_fingerprint: str,
        reviewer: str,
    ) -> list[Any]:
        return list(table.prov or []) + [self._table_prov(action, bundle_fingerprint, reviewer)]

    def _new_table_prov(
        self,
        action: OfflineEvidenceReviewTableAction,
        bundle_fingerprint: str,
        reviewer: str,
    ) -> list[Any]:
        return [self._table_prov(action, bundle_fingerprint, reviewer)]

    @staticmethod
    def _table_prov(action: OfflineEvidenceReviewTableAction, bundle_fingerprint: str, reviewer: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "offline_evidence_review_table",
            "source_action": action.action,
            "page_no": action.page,
            "created_by": reviewer,
            "bundle_fingerprint": bundle_fingerprint,
            "reason": action.reason,
        }
        if action.bbox_norm is not None:
            payload["bbox_norm"] = action.bbox_norm
        return payload

    @staticmethod
    def _table_evidence_payload(action: OfflineEvidenceReviewTableAction, bundle_fingerprint: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": "offline_evidence_review_result",
            "bundle_fingerprint": bundle_fingerprint,
            "page": action.page,
            "table": action.caption or action.table_id or action.target_table_id,
            "table_id": action.table_id or action.target_table_id or action.source_table_id,
            "quoted_text": (action.complete_markdown or action.caption or action.reason)[:2000],
            "evidence_ids": action.evidence_ids,
            "reason": action.reason,
            "local_ai_verification": (
                action.local_ai_verification.model_dump(mode="json")
                if action.local_ai_verification is not None
                else None
            ),
        }
        if action.bbox_norm is not None:
            payload["bbox"] = action.bbox_norm
        return payload

    @staticmethod
    def _has_local_ai_verification(verification: Any) -> bool:
        if isinstance(verification, dict):
            verified_against_pdf = verification.get("verified_against_pdf")
            raw_used_tools = verification.get("used_tools") or []
        else:
            verified_against_pdf = getattr(verification, "verified_against_pdf", None)
            raw_used_tools = getattr(verification, "used_tools", []) or []
        if verified_against_pdf is not True:
            return False
        used_tools = {str(item).strip() for item in raw_used_tools if str(item).strip()}
        return {"get_codex_item", "read_paper_page"} <= used_tools

    def _already_applied(
        self,
        op_id: str,
        bundle_fingerprint: str,
        *,
        target_type: str,
        expected_action: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        rows = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "offline_evidence_review_op")
            .where(AuditLog.target_type == target_type)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(50)
        ).all()
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("op_id") == op_id and payload.get("bundle_fingerprint") == bundle_fingerprint:
                existing_action = payload.get("action") if isinstance(payload.get("action"), dict) else None
                if expected_action is not None and existing_action is not None:
                    existing_key = json.dumps(existing_action, ensure_ascii=False, sort_keys=True, default=str)
                    expected_key = json.dumps(expected_action, ensure_ascii=False, sort_keys=True, default=str)
                    if existing_key != expected_key:
                        continue
                return {
                    "op_id": op_id,
                    "category": "figure" if target_type == "paper_figure" else "table",
                    "action": payload.get("action", {}).get("action") if isinstance(payload.get("action"), dict) else None,
                    "target_id": row.target_id,
                    "audit_log_id": str(row.id),
                    "idempotent": True,
                    "applied_updates": payload.get("applied_updates") or payload.get("table_result"),
                }
        return None

    def _resolve_pdf(self, pdf_path: Any) -> Path | None:
        if not pdf_path:
            return None
        return resolve_persisted_artifact_path(
            str(pdf_path),
            category="pdf",
            settings=self.settings,
            trusted_persisted_reference=True,
        )

    def _resolve_figure(self, image_path: Any) -> Path | None:
        if not image_path:
            return None
        return resolve_persisted_artifact_path(
            str(image_path),
            category="figures",
            settings=self.settings,
            trusted_persisted_reference=True,
        )

    @staticmethod
    def _private_path(value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.exists() and path.is_file() else None

    @staticmethod
    def _pdf_geometry(pdf_path: Path) -> tuple[list[dict[str, Any]], str | None]:
        try:
            import fitz

            doc = fitz.open(str(pdf_path))
            try:
                return [
                    {
                        "page": index + 1,
                        "width": float(page.rect.width),
                        "height": float(page.rect.height),
                        "rotation": int(page.rotation),
                    }
                    for index, page in enumerate(doc)
                ], None
            finally:
                doc.close()
        except Exception as exc:
            return [], f"pdf_geometry_unavailable:{type(exc).__name__}"

    @staticmethod
    def _last_bbox_from_prov(prov: Any) -> list[float] | None:
        if not isinstance(prov, list):
            return None
        for entry in reversed(prov):
            if not isinstance(entry, dict) or "bbox" not in entry:
                continue
            bbox = entry.get("bbox")
            if isinstance(bbox, dict):
                try:
                    return [
                        float(bbox.get("l", bbox.get("x0"))),
                        float(bbox.get("t", bbox.get("y0"))),
                        float(bbox.get("r", bbox.get("x1"))),
                        float(bbox.get("b", bbox.get("y1"))),
                    ]
                except Exception:
                    return None
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    return [float(item) for item in bbox]
                except Exception:
                    return None
        return None

    @staticmethod
    def _bbox_to_norm(bbox: list[float] | None, page: int | None, page_sizes: list[dict[str, Any]]) -> list[float] | None:
        if bbox is None or page is None:
            return None
        size = next((item for item in page_sizes if item.get("page") == page), None)
        if not size:
            return None
        width = float(size.get("width") or 0)
        height = float(size.get("height") or 0)
        if width <= 0 or height <= 0:
            return None
        return [
            max(0.0, min(1.0, bbox[0] / width)),
            max(0.0, min(1.0, bbox[1] / height)),
            max(0.0, min(1.0, bbox[2] / width)),
            max(0.0, min(1.0, bbox[3] / height)),
        ]

    @staticmethod
    def _has_page_geometry(
        source_paper_id: str | None,
        figure_id: str | None,
        page: int | None,
        materials: dict[str, Any],
    ) -> bool:
        if page is None:
            return False
        resolved_source = source_paper_id
        if resolved_source is None and figure_id:
            for figure in materials["extracted_figures"]:
                if figure.get("source_record_id") == figure_id:
                    resolved_source = figure.get("source_paper_id")
                    break
        if resolved_source is None:
            return False
        for source_doc in materials["page_geometry"].get("source_documents", []):
            if source_doc.get("source_paper_id") != resolved_source:
                continue
            return any(item.get("page") == page for item in source_doc.get("page_sizes") or [])
        return False

    @staticmethod
    def _looks_like_markdown_table(value: str | None) -> bool:
        if not value:
            return False
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return sum(1 for line in lines if "|" in line) >= 2

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
            "_pdf_abs_path",
            "_image_abs_path",
        }
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize_for_bundle(item)
                for key, item in value.items()
                if not str(key).startswith("_") and str(key).strip().lower() not in blocked_keys
            }
        if isinstance(value, list):
            return [cls._sanitize_for_bundle(item) for item in value]
        return value

    @classmethod
    def _public_records(cls, items: list[dict[str, Any]], *, include_bundle_file: bool = True) -> list[dict[str, Any]]:
        records = []
        for item in items:
            public = cls._sanitize_for_bundle(item)
            if not include_bundle_file:
                public.pop("bundle_file", None)
            records.append(public)
        return records

    @classmethod
    def _public_source_documents(cls, items: list[dict[str, Any]], *, include_bundle_file: bool = True) -> list[dict[str, Any]]:
        records = []
        for item in items:
            public = cls._sanitize_for_bundle(item)
            if not include_bundle_file:
                public.pop("bundle_file", None)
            records.append(public)
        return records

    @staticmethod
    def _table_markdown(table: dict[str, Any]) -> str:
        return (
            f"# {table['evidence_id']}\n\n"
            f"- source_document_type: {table['source_document_type']}\n"
            f"- source_paper_code: {table.get('source_paper_code') or '-'}\n"
            f"- source_record_id: {table.get('source_record_id') or '-'}\n"
            f"- page: {table.get('page') or '-'}\n"
            f"- caption: {table.get('caption') or '-'}\n\n"
            f"{table.get('markdown_content') or ''}\n"
        )

    @staticmethod
    def _return_template(materials: dict[str, Any]) -> dict[str, Any]:
        metadata = materials["paper_metadata"]
        return {
            "schema_version": "offline_figure_table_evidence_review_result_v1",
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "scope_type": materials.get("scope_type", "paper"),
            "run_id": materials.get("run_id"),
            "chart_run_id": materials.get("chart_run_id"),
            "review_source": {
                "review_source_type": "web_ai",
                "reviewer_label": "user-provided AI",
                "reviewer_model": None,
                "tool_capabilities": ["pdf_reading", "image_understanding", "table_reconstruction"],
            },
            "overall_status": "uncertain",
            "figure_actions": [],
            "table_actions": [],
            "dft_evidence_candidates": [],
            "uncertainties": [],
            "notes": [],
        }

    @staticmethod
    def _output_rules(materials: dict[str, Any]) -> dict[str, Any]:
        paper_code = materials["paper_metadata"]["paper_code"]
        return {
            "schema_version": "offline_figure_table_output_rules_v1",
            "output_workflow": {
                "input_template": "WEB_AI_FILL_THIS.json",
                "schema": "return_schema.json",
                "output_filename": f"{paper_code}_chart_review_result.json",
                "output_type": "single_json_file_attachment",
                "reply_as_file_attachment": True,
                "do_not_generate_from_scratch": True,
                "do_not_wrap_in_markdown": True,
            },
            "immutable_fields": [
                "schema_version", "bundle_fingerprint", "paper_id", "paper_code",
                "scope_type", "run_id", "chart_run_id",
            ],
            "hard_invariants": [
                "Every figure_actions and table_actions item must cite one or more real evidence_ids from this package.",
                "Never invent evidence_ids; copy them from parsed/extracted_figures.json, parsed/extracted_tables.json, or manifest.json.",
                "CREATE figure requires source_paper_id, page, bbox_norm, evidence_checked=true, and real evidence_ids.",
                "RECROP figure requires figure_id, page, bbox_norm, evidence_checked=true, and real evidence_ids.",
                "CREATE or UPDATE table requires source_paper_id or table_id as appropriate, complete_markdown, evidence_checked=true, and real evidence_ids.",
                "If a proposed new figure/table has no package evidence_id, remove that unsupported CREATE action instead of inventing an ID.",
                "Web AI must leave local_ai_verification null; authenticated local AI performs a separate full-figure PDF verification after this result is applied.",
                "Do not change immutable fields copied from WEB_AI_FILL_THIS.json.",
            ],
            "final_self_check": [
                "Parse the completed output as JSON.",
                "Validate it against return_schema.json.",
                "For every CREATE/RECROP/UPDATE/MERGE/DELETE/REJECT action, confirm evidence_ids is non-empty and every ID occurs in this package.",
                "For every CREATE figure, confirm source_paper_id, page, and bbox_norm are present.",
                "Return only the completed JSON file.",
            ],
        }

    @staticmethod
    def _start_here(materials: dict[str, Any]) -> str:
        paper_code = materials["paper_metadata"]["paper_code"]
        return f"""# START HERE — fill the supplied JSON file

Do not create a new response structure from memory.

1. Open `WEB_AI_FILL_THIS.json`.
2. Keep its paper, scope, run, fingerprint, and schema fields unchanged.
3. Before adding any action, find its real `evidence_id` in `parsed/extracted_figures.json`, `parsed/extracted_tables.json`, or `manifest.json`.
4. Every action must include one or more real `evidence_ids`; do not leave the array empty and do not invent IDs.
5. For a new figure (`CREATE`), also include `source_paper_id`, `page`, and `bbox_norm`.
6. Read `OUTPUT_RULES.json`, then validate the completed object against `return_schema.json`.
7. Save it as `{paper_code}_chart_review_result.json` and reply by attaching that one JSON file.

If a possible new figure/table cannot be tied to a package evidence_id, omit that CREATE action. Do not paste the JSON body into the chat. Do not return Markdown, prose, a code fence, or an outer wrapper.
"""

    @staticmethod
    def _instructions(materials: dict[str, Any]) -> str:
        metadata = materials["paper_metadata"]
        return f"""# Literature AI 离线图表证据整理任务

目标文献：`{metadata['paper_code']}`（paper_id=`{metadata['paper_id']}`）
审核范围：`{materials.get('scope_type', 'paper')}`；run_id=`{materials.get('run_id') or '-'}`

你是图表证据审核建议来源，不是数据库执行者。你不能连接 MCP、数据库、服务器或外部检索；只能使用本压缩包中的 PDF、图片、表格和 JSON。

## 目标

把当前系统抽取的图片和表格还原成可信证据：

1. 图片：核对当前 crop 是否完整、是否对应原文图注；错误时给出原 PDF 页码和 `bbox_norm`，让系统从原 PDF 确定性重裁。
2. 表格：核对当前 markdown 是否缺行、缺列、跨页断裂或单位/脚注丢失；错误时返回完整 markdown 表格。
3. DFT 图表证据：只抽取“图/表中明确写出的 DFT 证据候选”，放入 `dft_evidence_candidates`，不得把它们说成已 verified 的 DFT 数据。

## 必须遵守

1. PDF 是最高优先级来源；当前抽取图片、表格只是候选。
2. 每个当前 figure/table 都要有一个 action；如果无法判断，用 `NEEDS_HUMAN`，但它会被服务器视为待复核项，不能完成图表阶段。
3. 图片 action：`KEEP`、`RECROP`、`CREATE`、`REJECT`、`NEEDS_HUMAN`。
4. 表格 action：`KEEP`、`UPDATE`、`CREATE`、`MERGE`、`DELETE`、`NEEDS_HUMAN`。
5. 从 `WEB_AI_FILL_THIS.json` 开始填写，禁止脱离模板重建 JSON。每一条 figure/table action 都必须带至少一个包内真实 `evidence_ids`；从 `parsed/extracted_figures.json`、`parsed/extracted_tables.json` 或 `manifest.json` 复制，禁止编造或留空。`RECROP` 和 `CREATE` 还必须返回 `page` 与 `bbox_norm=[x0,y0,x1,y1]`；坐标是该 PDF 页面的归一化 top-left 坐标，范围 0 到 1。
6. 对每张保留的科学图片，`KEEP` 或 `RECROP` 后必须具备具体 `figure_role`、非图注复读的 `content_summary` 和具体 `key_elements`；缺任一项时在 action 中补齐。不要使用 unknown/unclassified/other 或 verified/reviewed/ok 这类占位词。
   在 run-scoped 图表审核中，`content_summary` 与 `key_elements` 是实际字段回填建议；只能填写 manifest 中当前 target figure/table，不能生成内容知识 claim。
7. `dft_relevance` 只能填写 `none`、`possible`、`explicit_dft`、`unknown` 四者之一；不要写 true/false/yes/no/dft_relevant。
8. `UPDATE` 和 `CREATE` 表格必须返回完整 `complete_markdown`，包含列名、单位、脚注相关信息；不要只返回差异片段。
9. `MERGE` 只用于两个已有表格对象合并，必须填写 `source_table_id` 和 `target_table_id`，且二者不能相同；不要同时给同一表格输出 KEEP/UPDATE/MERGE 多个 action。没有把握时用 `NEEDS_HUMAN`。
10. 不要估读曲线、不要从柱状图/曲线图目测数值；只有图中文字、表格单元格、图注/脚注明确给出的 DFT 数值才可进入 `dft_evidence_candidates`。
 11. 网页 AI 必须把所有 `local_ai_verification` 保持为 null；该字段只能由后续通过已认证 MCP 工作流运行的本地 AI 逐图核验后填写。
 12. 不得声称已经写库、已经确认、已经 verified、图表阶段已经完成或已经 ML_Ready；网页 AI 结果应用后仍必须经过本地 AI 全量图片复核。
 13. 先读 `START_HERE.md` 和 `OUTPUT_RULES.json`，严格按 `return_schema.json` 填写 JSON，保存为 `{metadata['paper_code']}_chart_review_result.json` 并以文件附件回复；不要把长 JSON 粘贴在聊天正文中，也不要输出 Markdown 代码块。
 14. 保留 `return_template.json` 中的 `bundle_fingerprint`、`paper_id`、`paper_code` 原值。
 15. `figure_table_evidence` 的缺失字段提醒不是可引用科学论断；不要把它提交到纸级内容审核包或用它升级 `citable`。

## 建议阅读顺序

1. `manifest.json`
2. `parsed/source_documents.json` 与 `source/page_geometry.json`
3. `source/*.pdf`
4. `parsed/extracted_figures.json`、`evidence/figures/*`
5. `parsed/extracted_tables.json`、`evidence/tables/*`
6. 输出符合 `return_schema.json` 的 JSON
"""
