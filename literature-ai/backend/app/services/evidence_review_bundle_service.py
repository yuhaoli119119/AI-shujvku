from __future__ import annotations

from datetime import UTC, datetime
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, Paper, PaperFigure, PaperRelationship, PaperTable, utcnow
from app.schemas.evidence_review_bundle import (
    OfflineEvidenceReviewFigureAction,
    OfflineEvidenceReviewResult,
    OfflineEvidenceReviewTableAction,
)
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.table_curation_service import TableCurationService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.figure_summary import normalize_figure_content_summary, normalize_figure_key_elements


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


class EvidenceReviewBundleService:
    """Build, validate, and auto-apply offline figure/table evidence review packages."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def build_zip(
        self,
        paper_id: UUID,
        *,
        include_pdf_files: bool = True,
        include_figure_files: bool = True,
    ) -> dict[str, Any]:
        materials = self._build_materials(paper_id)
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

        pdf_warnings: list[str] = []
        pdf_count = 0
        pdf_bytes_total = 0
        if include_pdf_files:
            for source_doc in materials["source_documents"]:
                pdf_path = self._private_path(source_doc.get("_pdf_abs_path"))
                if pdf_path is None:
                    continue
                if pdf_count >= MAX_SOURCE_PDF_COUNT:
                    pdf_warnings.append("source_pdf_file_limit_reached")
                    break
                size = pdf_path.stat().st_size
                if pdf_bytes_total + size > MAX_TOTAL_SOURCE_PDF_BYTES:
                    pdf_warnings.append("source_pdf_byte_limit_reached")
                    continue
                role = source_doc.get("role") or "source"
                code = _safe_name(str(source_doc.get("paper_code") or source_doc.get("paper_id") or role), role)
                filename = "main.pdf" if role == "main" else f"{code}.pdf"
                bundle_path = f"source/{role}/{filename}" if role != "main" else f"source/{filename}"
                data = pdf_path.read_bytes()
                files[bundle_path] = data
                source_doc["bundle_file"] = bundle_path
                pdf_count += 1
                pdf_bytes_total += len(data)

        figure_warnings: list[str] = []
        figure_file_count = 0
        figure_bytes_total = 0
        if include_figure_files:
            for figure in materials["extracted_figures"]:
                artifact = self._private_path(figure.get("_image_abs_path"))
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

        files["parsed/source_documents.json"] = _json_bytes(self._public_source_documents(materials["source_documents"]))
        files["parsed/extracted_figures.json"] = _json_bytes(self._public_records(materials["extracted_figures"]))
        files["return_template.json"] = _json_bytes(self._return_template(materials))
        files["instructions_for_web_ai.md"] = self._instructions(materials).encode("utf-8")

        inventory = [
            {"path": path, "size_bytes": len(data), "sha256": _sha256(data)}
            for path, data in sorted(files.items())
        ]
        manifest = {
            "schema_version": OFFLINE_EVIDENCE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "paper": {
                "paper_id": materials["paper_metadata"]["paper_id"],
                "paper_code": materials["paper_metadata"]["paper_code"],
                "title": materials["paper_metadata"].get("title"),
            },
            "review_scope": "single_paper_main_plus_supplementary_figures_and_tables",
            "expected_coverage": {
                "figure_ids": sorted(materials["figure_id_map"]),
                "table_ids": sorted(materials["table_id_map"]),
                "source_paper_ids": sorted(materials["source_paper_ids"]),
            },
            "auto_apply_policy": {
                "local_ai_role": "confirmation_and_readback_only",
                "figure_auto_confidence_min": FIGURE_AUTO_CONFIDENCE,
                "table_auto_confidence_min": TABLE_AUTO_CONFIDENCE,
                "auto_applies": ["figure KEEP metadata", "figure RECROP", "figure CREATE", "table UPDATE", "table CREATE"],
                "never_auto_applies": ["table MERGE", "table DELETE", "figure REJECT", "NEEDS_HUMAN"],
            },
            "counts": {
                "source_documents": len(materials["source_documents"]),
                "figures": len(materials["extracted_figures"]),
                "tables": len(materials["extracted_tables"]),
                "included_source_pdfs": pdf_count,
                "included_figure_files": figure_file_count,
            },
            "warnings": sorted(set(materials["warnings"] + pdf_warnings + figure_warnings)),
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
            "filename": f"{paper_code}_figure_table_evidence_review_bundle.zip",
            "content": buffer.getvalue(),
            "manifest": manifest,
        }

    def validate_result(self, paper_id: UUID, raw_payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = OfflineEvidenceReviewResult.model_validate(raw_payload)
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
                "execution_plan": [],
                "auto_apply_count": 0,
                "needs_confirmation_count": 0,
            }

        materials = self._build_materials(paper_id)
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
        if result.bundle_fingerprint != materials["bundle_fingerprint"]:
            add_error(
                "stale_or_mismatched_bundle",
                "bundle_fingerprint differs from the current figure/table evidence snapshot; export a new package and review again",
            )

        figure_ids_seen: set[str] = set()
        table_ids_seen: set[str] = set()
        evidence_ids = set(materials["evidence_map"])

        for index, action in enumerate(result.figure_actions):
            action_ref = f"figure_actions[{index}]"
            plan = self._validate_figure_action(action, materials, action_ref, evidence_ids)
            execution_plan.append(plan)
            for error in plan.pop("_errors", []):
                add_error(error["code"], error["message"], action_ref=action_ref)
            if action.figure_id:
                figure_ids_seen.add(action.figure_id)

        for index, action in enumerate(result.table_actions):
            action_ref = f"table_actions[{index}]"
            plan = self._validate_table_action(action, materials, action_ref, evidence_ids)
            execution_plan.append(plan)
            for error in plan.pop("_errors", []):
                add_error(error["code"], error["message"], action_ref=action_ref)
            for table_id in (action.table_id, action.source_table_id, action.target_table_id):
                if table_id:
                    table_ids_seen.add(table_id)

        for index, candidate in enumerate(result.dft_evidence_candidates):
            if candidate.evidence_id and candidate.evidence_id not in evidence_ids:
                add_error(
                    "unknown_dft_candidate_evidence_id",
                    f"dft_evidence_candidates[{index}].evidence_id is not present in the package",
                    action_ref=f"dft_evidence_candidates[{index}]",
                )

        if result.overall_status == "completed":
            missing_figures = sorted(materials["figure_id_map"] - figure_ids_seen)
            missing_tables = sorted(materials["table_id_map"] - table_ids_seen)
            if missing_figures:
                add_error(
                    "incomplete_figure_coverage",
                    "overall_status=completed requires a figure action for every current figure; missing: "
                    + ", ".join(missing_figures),
                )
            if missing_tables:
                add_error(
                    "incomplete_table_coverage",
                    "overall_status=completed requires a table action for every current table; missing: "
                    + ", ".join(missing_tables),
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

        if errors:
            for plan in execution_plan:
                plan["auto_apply"] = False
                blocked = list(plan.get("blocked_reasons") or [])
                if "result_has_validation_errors" not in blocked:
                    blocked.append("result_has_validation_errors")
                plan["blocked_reasons"] = blocked

        return {
            "valid": not errors,
            "paper_id": metadata["paper_id"],
            "paper_code": metadata["paper_code"],
            "bundle_fingerprint": materials["bundle_fingerprint"],
            "errors": errors,
            "warnings": warnings,
            "execution_plan": execution_plan,
            "auto_apply_count": sum(1 for item in execution_plan if item.get("auto_apply")),
            "needs_confirmation_count": sum(1 for item in execution_plan if not item.get("auto_apply")),
            "safety": {
                "validate_writes_database": False,
                "apply_endpoint_writes_database": True,
                "local_ai_role": "confirm payload validity, call apply, then read back changed figures/tables",
                "web_ai_writes_database": False,
            },
        }

    def apply_result(self, paper_id: UUID, raw_payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        validation = self.validate_result(paper_id, raw_payload)
        result = OfflineEvidenceReviewResult.model_validate(raw_payload) if validation["valid"] else None
        apply_blocking_errors: list[dict[str, str]] = []
        if result is not None and result.overall_status != "completed":
            apply_blocking_errors.append(
                {
                    "code": "apply_requires_completed_review",
                    "message": "The apply endpoint only records a curated evidence snapshot when overall_status='completed'.",
                }
            )
        if not validation["valid"] or dry_run:
            return {
                **validation,
                "dry_run": dry_run,
                "apply_ready": validation["valid"] and not apply_blocking_errors,
                "apply_blocking_errors": apply_blocking_errors,
                "applied_count": 0,
                "applied": [],
                "skipped": validation.get("execution_plan", []),
            }
        if apply_blocking_errors:
            return {
                **validation,
                "dry_run": False,
                "apply_ready": False,
                "apply_blocking_errors": apply_blocking_errors,
                "applied_count": 0,
                "applied": [],
                "skipped": validation.get("execution_plan", []),
            }

        assert result is not None
        materials = self._build_materials(paper_id)
        auto_op_ids = {
            item["op_id"]
            for item in validation.get("execution_plan", [])
            if item.get("auto_apply")
        }
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        reviewer = _short_source(result.review_source.reviewer_label)

        try:
            for index, action in enumerate(result.figure_actions):
                op_id = f"figure:{index}:{action.action}"
                if op_id not in auto_op_ids:
                    skipped.append({"op_id": op_id, "category": "figure", "action": action.action})
                    continue
                applied.append(self._apply_figure_action(action, materials, op_id, result.bundle_fingerprint, reviewer))

            for index, action in enumerate(result.table_actions):
                op_id = f"table:{index}:{action.action}"
                if op_id not in auto_op_ids:
                    skipped.append({"op_id": op_id, "category": "table", "action": action.action})
                    continue
                applied.append(self._apply_table_action(action, materials, op_id, result.bundle_fingerprint, reviewer))

            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="offline_evidence_review_applied",
                    source=reviewer,
                    target_type="offline_evidence_review",
                    target_id=result.bundle_fingerprint[:32],
                    payload={
                        "schema_version": result.schema_version,
                        "bundle_fingerprint": result.bundle_fingerprint,
                        "paper_code": result.paper_code,
                        "overall_status": result.overall_status,
                        "review_source": result.review_source.model_dump(mode="json"),
                        "applied": applied,
                        "skipped": skipped,
                        "execution_plan": validation.get("execution_plan", []),
                        "dft_evidence_candidates": [
                            item.model_dump(mode="json") for item in result.dft_evidence_candidates
                        ],
                        "uncertainties": result.uncertainties,
                        "notes": result.notes,
                        "local_ai_confirmation_required_before_call": True,
                    },
                    created_at=utcnow(),
                )
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        refreshed = self._build_materials(paper_id)
        return {
            **validation,
            "dry_run": False,
            "applied_count": len(applied),
            "applied": applied,
            "skipped": skipped,
            "post_apply_bundle_fingerprint": refreshed["bundle_fingerprint"],
            "safety": {
                "writes_database": True,
                "writes_final_dft_truth": False,
                "local_ai_next_step": "Read back changed figures/tables, then export the DFT review bundle.",
            },
        }

    def _validate_figure_action(
        self,
        action: OfflineEvidenceReviewFigureAction,
        materials: dict[str, Any],
        action_ref: str,
        evidence_ids: set[str],
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
        if action.action != "NEEDS_HUMAN" and not action.evidence_checked:
            block("evidence_not_checked", f"{action.action} requires evidence_checked=true")
        if action.action in {"RECROP", "CREATE"} and not self._has_page_geometry(action.source_paper_id, action.figure_id, action.page, materials):
            blocked.append("page_geometry_unavailable")
        if action.action in {"REJECT", "NEEDS_HUMAN"}:
            blocked.append("manual_confirmation_required")
        if action.confidence is None or action.confidence < FIGURE_AUTO_CONFIDENCE:
            blocked.append("confidence_below_auto_apply_threshold")

        auto = not errors and action.action in {"KEEP", "RECROP", "CREATE"} and not blocked
        return {
            "op_id": action_ref.replace("figure_actions[", "figure:").replace("]", f":{action.action}"),
            "category": "figure",
            "action": action.action,
            "target_id": target_id,
            "source_paper_id": action.source_paper_id,
            "auto_apply": auto,
            "blocked_reasons": blocked,
            "tool_hint": "system_deterministic_pdf_crop" if action.action in {"RECROP", "CREATE"} else "system_metadata_update",
            "payload": action.model_dump(mode="json"),
            "_errors": errors,
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
        if action.action != "NEEDS_HUMAN" and not action.evidence_checked:
            block("evidence_not_checked", f"{action.action} requires evidence_checked=true")
        if action.action in {"UPDATE", "CREATE"} and not self._looks_like_markdown_table(action.complete_markdown):
            block("invalid_markdown_table", f"{action.action} requires a complete markdown table with pipes and multiple rows")
        if action.action in {"MERGE", "DELETE", "NEEDS_HUMAN"}:
            blocked.append("manual_confirmation_required")
        if action.confidence is None or action.confidence < TABLE_AUTO_CONFIDENCE:
            blocked.append("confidence_below_auto_apply_threshold")

        auto = not errors and action.action in {"UPDATE", "CREATE"} and not blocked
        return {
            "op_id": action_ref.replace("table_actions[", "table:").replace("]", f":{action.action}"),
            "category": "table",
            "action": action.action,
            "target_id": target_id,
            "source_paper_id": action.source_paper_id,
            "auto_apply": auto,
            "blocked_reasons": blocked,
            "tool_hint": "table_curation_service",
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
    ) -> dict[str, Any]:
        preexisting = self._already_applied(op_id, bundle_fingerprint, target_type="paper_figure")
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
        else:
            figure = self.session.get(PaperFigure, UUID(str(action.figure_id)))
            if figure is None:
                raise ValueError("Figure not found during apply")
            target_id = str(figure.id)
            applied_updates = self._apply_figure_metadata(figure, action)
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
            paper_id=UUID(str(action.source_paper_id)) if action.action == "CREATE" else figure.paper_id,
            action="offline_evidence_review_op",
            source=reviewer,
            target_type="paper_figure",
            target_id=target_id,
            payload={
                "op_id": op_id,
                "bundle_fingerprint": bundle_fingerprint,
                "action": action.model_dump(mode="json"),
                "applied_updates": applied_updates,
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
        preexisting = self._already_applied(op_id, bundle_fingerprint, target_type="paper_table")
        if preexisting is not None:
            return preexisting

        evidence_payload = self._table_evidence_payload(action, bundle_fingerprint)
        service = TableCurationService(self.session, reviewer=reviewer)
        if action.action == "UPDATE":
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
                "action": action.model_dump(mode="json"),
                "table_result": result,
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

    def _build_materials(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        if not str(paper.paper_code or "").strip():
            raise ValueError("paper_code_required_before_offline_evidence_review_export")

        source_papers = self._source_papers(paper)
        source_ids = [item["paper"].id for item in source_papers]
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(source_ids))).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(source_ids))).all()
        source_by_id = {item["paper"].id: item for item in source_papers}
        warnings: list[str] = []

        source_documents = []
        for item in source_papers:
            source_doc = self._source_document_payload(item)
            warnings.extend(source_doc.pop("_warnings", []))
            source_documents.append(source_doc)
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
        }
        fingerprint_payload = {
            "schema_version": OFFLINE_EVIDENCE_REVIEW_BUNDLE_SCHEMA_VERSION,
            "paper_metadata": paper_metadata,
            "source_documents": self._public_source_documents(source_documents, include_bundle_file=False),
            "page_geometry": page_geometry,
            "extracted_tables": self._public_records(extracted_tables, include_bundle_file=False),
            "extracted_figures": self._public_records(extracted_figures, include_bundle_file=False),
        }
        bundle_fingerprint = _sha256(_canonical_json_bytes(fingerprint_payload))
        if not extracted_tables:
            warnings.append("no_tables_in_scope")
        if not extracted_figures:
            warnings.append("no_figures_in_scope")

        return {
            "paper_metadata": paper_metadata,
            "source_documents": source_documents,
            "page_geometry": page_geometry,
            "extracted_tables": extracted_tables,
            "extracted_figures": extracted_figures,
            "evidence_map": evidence_map,
            "figure_id_map": {str(row.id) for row in figures},
            "table_id_map": {str(row.id) for row in tables},
            "source_paper_ids": {str(item["paper"].id) for item in source_papers},
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
        }
        if action.bbox_norm is not None:
            payload["bbox"] = action.bbox_norm
        return payload

    def _already_applied(self, op_id: str, bundle_fingerprint: str, *, target_type: str) -> dict[str, Any] | None:
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
    def _instructions(materials: dict[str, Any]) -> str:
        metadata = materials["paper_metadata"]
        return f"""# Literature AI 离线图表证据整理任务

目标文献：`{metadata['paper_code']}`（paper_id=`{metadata['paper_id']}`）

你是图表证据审核建议来源，不是数据库执行者。你不能连接 MCP、数据库、服务器或外部检索；只能使用本压缩包中的 PDF、图片、表格和 JSON。

## 目标

把当前系统抽取的图片和表格还原成可信证据：

1. 图片：核对当前 crop 是否完整、是否对应原文图注；错误时给出原 PDF 页码和 `bbox_norm`，让系统从原 PDF 确定性重裁。
2. 表格：核对当前 markdown 是否缺行、缺列、跨页断裂或单位/脚注丢失；错误时返回完整 markdown 表格。
3. DFT 图表证据：只抽取“图/表中明确写出的 DFT 证据候选”，放入 `dft_evidence_candidates`，不得把它们说成已 verified 的 DFT 数据。

## 必须遵守

1. PDF 是最高优先级来源；当前抽取图片、表格只是候选。
2. 每个当前 figure/table 都要有一个 action；如果无法判断，用 `NEEDS_HUMAN`。
3. 图片 action：`KEEP`、`RECROP`、`CREATE`、`REJECT`、`NEEDS_HUMAN`。
4. 表格 action：`KEEP`、`UPDATE`、`CREATE`、`MERGE`、`DELETE`、`NEEDS_HUMAN`。
5. `RECROP` 和 `CREATE` 必须返回 `page` 与 `bbox_norm=[x0,y0,x1,y1]`；坐标是该 PDF 页面的归一化 top-left 坐标，范围 0 到 1。
6. `UPDATE` 和 `CREATE` 表格必须返回完整 `complete_markdown`，包含列名、单位、脚注相关信息；不要只返回差异片段。
7. 不要估读曲线、不要从柱状图/曲线图目测数值；只有图中文字、表格单元格、图注/脚注明确给出的 DFT 数值才可进入 `dft_evidence_candidates`。
8. 不得声称已经写库、已经确认、已经 verified 或已经 ML_Ready。
9. 严格按 `return_schema.json` 输出一个 JSON 对象；不要输出 Markdown 代码块。
10. 保留 `return_template.json` 中的 `bundle_fingerprint`、`paper_id`、`paper_code` 原值。

## 建议阅读顺序

1. `manifest.json`
2. `parsed/source_documents.json` 与 `source/page_geometry.json`
3. `source/*.pdf`
4. `parsed/extracted_figures.json`、`evidence/figures/*`
5. `parsed/extracted_tables.json`、`evidence/tables/*`
6. 输出符合 `return_schema.json` 的 JSON
"""
