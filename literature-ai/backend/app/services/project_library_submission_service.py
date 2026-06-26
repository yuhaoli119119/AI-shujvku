from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTResult, ExternalAnalysisCandidate, Paper
from app.domain.project_library_context import get_project_library_context
from app.normalizers.chemistry_normalizer import canonicalize_adsorbate, get_property_taxonomy
from app.schemas.project_library import ProjectLibraryUserSubmitRequest
from app.services.project_library_bundle_service import ProjectLibraryBundleService


class ProjectLibrarySubmissionBlockedError(ValueError):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(detail.get("message") or "project_library_v4_submit_blocked")
        self.detail = detail


@dataclass
class PreparedSubmission:
    request: ProjectLibraryUserSubmitRequest
    paper: Paper
    catalyst: CatalystSample
    record: DFTResult | None
    source_candidates: list[ExternalAnalysisCandidate]
    action: str
    submitted_by: str
    active_site_instance_key: str
    active_site_ref: dict[str, Any]
    hard_blockers: list[str]
    ml_blockers: list[str]
    warnings: list[str]
    normalized_submission: dict[str, Any]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _token(value: Any) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in _clean_text(value).lower()).strip("_")


class ProjectLibrarySubmissionService:
    PREVIEW_SCHEMA_VERSION = "project_library_v4_user_submit_preview_v1"
    RESULT_SCHEMA_VERSION = "project_library_v4_user_submit_result_v1"
    SUBMISSION_ACTION = "project_library_v4_user_submit"
    FINAL_USER_SUBMITTED_STATUS = "final_user_submitted"
    HARD_BLOCKING_DECISIONS = {"ambiguous", "needs_user_decision", "manual_verification_required"}
    VALID_PROJECT_LIBRARY_V4_SCHEMA_MARKERS = {"project_library_ml_export_v4", "project_library_v4"}
    VALID_SOURCE_TARGET_TYPES = {"dft_results", "dft_result"}
    PERSISTED_FIELD_TARGETS = [
        "catalyst_sample_id",
        "adsorbate",
        "property_type",
        "value",
        "unit",
        "reaction_step",
        "reaction_type",
        "evidence_text",
        "confidence",
        "candidate_status",
    ]
    EVIDENCE_PAYLOAD_FIELDS = [
        "schema_version",
        "source_schema_version",
        "project_library_context",
        "database_write_authority",
        "ai_consensus_auto_adopt_allowed",
        "submitted_by_user",
        "submitted_by",
        "submission_status",
        "decision_status",
        "user_edits",
        "resolved_conflicts",
        "source_candidate_ids",
        "active_site_instance_key",
        "active_site_ref",
        "energy_kind",
        "source_text",
        "source_location",
        "support_raw",
        "support_normalized",
        "support_confidence",
        "dft_setting_id",
    ]

    def __init__(self, session: Session) -> None:
        self.session = session

    def preview(self, payload: ProjectLibraryUserSubmitRequest) -> dict[str, Any]:
        prepared = self._prepare(payload)
        return {
            "schema_version": self.PREVIEW_SCHEMA_VERSION,
            "context_key": payload.context_key,
            "paper_id": str(prepared.paper.id),
            "record_id": str(prepared.record.id) if prepared.record is not None else None,
            "action": prepared.action,
            "can_submit": not prepared.hard_blockers,
            "writes_to_database": False,
            "database_write_authority": "user_submit_only",
            "visible_in_v4_export": not prepared.hard_blockers,
            "ready_only_export_eligible": False,
            "hard_blockers": prepared.hard_blockers,
            "ml_blockers": prepared.ml_blockers,
            "warnings": prepared.warnings,
            "resolved_source_candidate_ids": [str(candidate.id) for candidate in prepared.source_candidates],
            "persisted_field_targets": list(self.PERSISTED_FIELD_TARGETS),
            "evidence_payload_fields": list(self.EVIDENCE_PAYLOAD_FIELDS),
            "normalized_submission": prepared.normalized_submission,
        }

    def submit(self, payload: ProjectLibraryUserSubmitRequest) -> dict[str, Any]:
        prepared = self._prepare(payload)
        if prepared.hard_blockers:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Li-S SAC/DAC v4 user submit is blocked until the required user decision is resolved.",
                    "hard_blockers": prepared.hard_blockers,
                    "ml_blockers": prepared.ml_blockers,
                }
            )

        row = prepared.record or self._create_row(prepared)
        row = self._apply_row_submission(row=row, prepared=prepared)
        self.session.add(row)
        self.session.flush()

        consumed_candidate_ids: list[str] = []
        for candidate in prepared.source_candidates:
            candidate.status = "user_submitted"
            candidate.materialized_target_type = "dft_results"
            candidate.materialized_target_id = str(row.id)
            self.session.add(candidate)
            consumed_candidate_ids.append(str(candidate.id))

        audit = AuditLog(
            paper_id=prepared.paper.id,
            action=self.SUBMISSION_ACTION,
            source=prepared.submitted_by,
            target_type="dft_results",
            target_id=str(row.id),
            payload={
                "schema_version": self.RESULT_SCHEMA_VERSION,
                "context_key": payload.context_key,
                "action": prepared.action,
                "database_write_authority": "user_submit_only",
                "ai_consensus_auto_adopt_allowed": False,
                "record_id": str(row.id),
                "submitted_by": prepared.submitted_by,
                "source_candidate_ids": consumed_candidate_ids,
                "persisted_field_targets": list(self.PERSISTED_FIELD_TARGETS),
                "evidence_payload_fields": list(self.EVIDENCE_PAYLOAD_FIELDS),
                "ml_blockers": prepared.ml_blockers,
            },
        )
        self.session.add(audit)
        self.session.commit()
        self.session.refresh(row)
        self.session.refresh(audit)

        export_payload = ProjectLibraryBundleService(self.session).build_ml_export_v4(
            context_key=payload.context_key,
            paper_id=prepared.paper.id,
            ready_only=False,
        )
        export_record = next((item for item in export_payload["records"] if item["record_id"] == str(row.id)), None)

        return {
            "schema_version": self.RESULT_SCHEMA_VERSION,
            "context_key": payload.context_key,
            "paper_id": str(prepared.paper.id),
            "record_id": str(row.id),
            "action": prepared.action,
            "writes_to_database": True,
            "database_write_authority": "user_submit_only",
            "visible_in_v4_export": export_record is not None,
            "ready_only_export_eligible": bool(export_record and export_record.get("ml_ready")),
            "candidate_status": row.candidate_status,
            "audit_log_id": str(audit.id),
            "consumed_source_candidate_ids": consumed_candidate_ids,
            "persisted_field_targets": list(self.PERSISTED_FIELD_TARGETS),
            "evidence_payload_fields": list(self.EVIDENCE_PAYLOAD_FIELDS),
            "export_record": export_record,
        }

    def _prepare(self, payload: ProjectLibraryUserSubmitRequest) -> PreparedSubmission:
        context = get_project_library_context(payload.context_key)
        if context.key != "li_s_sac_dac":
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_context_required",
                    "message": "Only the Li-S SAC/DAC project-library v4 context is supported by this submit API.",
                    "hard_blockers": ["non_li_s_project_library_context"],
                    "ml_blockers": [],
                }
            )
        if payload.database_write_authority != "user_submit_only":
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Li-S SAC/DAC v4 submit requires database_write_authority=user_submit_only.",
                    "hard_blockers": ["database_write_authority_must_be_user_submit_only"],
                    "ml_blockers": [],
                }
            )
        if payload.ai_consensus_auto_adopt_allowed:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Li-S SAC/DAC v4 submit forbids ai_consensus_auto_adopt_allowed=true.",
                    "hard_blockers": ["ai_consensus_auto_adopt_must_remain_disabled"],
                    "ml_blockers": [],
                }
            )

        paper = self.session.get(Paper, UUID(payload.paper_id))
        if paper is None:
            raise LookupError("Paper not found for project-library v4 submit.")

        source_candidates = self._source_candidates(paper_id=paper.id, candidate_ids=payload.source_candidate_ids)
        row = self._target_row(paper_id=paper.id, payload=payload, source_candidates=source_candidates)

        catalyst = self._resolve_catalyst(paper_id=paper.id, payload=payload, row=row, source_candidates=source_candidates)
        active_site_instance_key, active_site_ref = self._resolve_active_site(payload=payload, catalyst=catalyst, row=row)
        submitted_by = _clean_text(payload.submitted_by or payload.user_id)

        hard_blockers: list[str] = []
        ml_blockers: list[str] = []
        warnings: list[str] = []

        if not submitted_by:
            hard_blockers.append("missing_submitted_by")

        decision_status_token = _token(payload.decision_status or "ready_for_submission")
        if decision_status_token in self.HARD_BLOCKING_DECISIONS:
            hard_blockers.append("needs_user_decision")
            ml_blockers.append("needs_user_decision")

        if not active_site_instance_key:
            hard_blockers.append("missing_active_site_instance_key")
        if not active_site_ref:
            hard_blockers.append("missing_active_site_ref")

        taxonomy = get_property_taxonomy(payload.property_type)
        canonical_property_type = taxonomy["canonical_property_type"]
        canonical_adsorbate = canonicalize_adsorbate(payload.adsorbate) or payload.adsorbate
        energy_kind = _token(payload.energy_kind)
        if canonical_property_type == "adsorption_energy" and not canonical_adsorbate:
            ml_blockers.append("missing_adsorbate")
        if canonical_property_type in {"reaction_barrier", "gibbs_free_energy_change", "reaction_energy"} and energy_kind in {"", "unknown"}:
            ml_blockers.append("unknown_energy_kind")
        if canonical_property_type in {"reaction_barrier", "gibbs_free_energy_change", "reaction_energy"} and not _clean_text(payload.reaction_step):
            ml_blockers.append("missing_reaction_step")
        if not _clean_text(payload.source_text):
            hard_blockers.append("missing_source_text")
            ml_blockers.append("missing_source_text")
        if row is None and not payload.record_id:
            warnings.append("No existing DFTResult record_id was provided; submit will create a new user-submitted DFTResult.")

        normalized_submission = {
            "schema_version": payload.schema_version,
            "project_library_context": context.key,
            "paper_id": str(paper.id),
            "record_id": str(row.id) if row is not None else None,
            "catalyst_sample_id": str(catalyst.id),
            "active_site_instance_key": active_site_instance_key,
            "active_site_ref": active_site_ref,
            "property_type": payload.property_type,
            "adsorbate": payload.adsorbate,
            "reaction_step": payload.reaction_step,
            "energy_kind": payload.energy_kind,
            "value": payload.value,
            "unit": payload.unit,
            "source_text": payload.source_text,
            "source_location": payload.source_location or {},
            "submitted_by": submitted_by,
            "source_candidate_ids": [str(candidate.id) for candidate in source_candidates],
        }

        return PreparedSubmission(
            request=payload,
            paper=paper,
            catalyst=catalyst,
            record=row,
            source_candidates=source_candidates,
            action="update_existing_dft_result" if row is not None else "create_new_dft_result",
            submitted_by=submitted_by,
            active_site_instance_key=active_site_instance_key,
            active_site_ref=active_site_ref,
            hard_blockers=sorted(set(hard_blockers)),
            ml_blockers=sorted(set(ml_blockers)),
            warnings=warnings,
            normalized_submission=normalized_submission,
        )

    def _source_candidates(self, *, paper_id: UUID, candidate_ids: list[str]) -> list[ExternalAnalysisCandidate]:
        if not candidate_ids:
            return []
        uuids = [UUID(candidate_id) for candidate_id in dict.fromkeys(candidate_ids)]
        rows = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.id.in_(uuids),
            )
        ).all()
        found = {str(row.id) for row in rows}
        missing = [candidate_id for candidate_id in candidate_ids if candidate_id not in found]
        if missing:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Some source_candidate_ids do not belong to this paper.",
                    "hard_blockers": ["invalid_source_candidate_ids"],
                    "ml_blockers": [],
                    "missing_source_candidate_ids": missing,
                }
            )
        for row in rows:
            self._validate_source_candidate_for_project_library_v4(row)
        return list(rows)

    def _validate_source_candidate_for_project_library_v4(self, candidate: ExternalAnalysisCandidate) -> None:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        schema_markers = {
            _clean_text(payload.get("schema_version")),
            _clean_text(payload.get("source_schema_version")),
            _clean_text(payload.get("export_schema_version")),
        }
        schema_ok = any(marker in self.VALID_PROJECT_LIBRARY_V4_SCHEMA_MARKERS for marker in schema_markers if marker)
        context_marker = _clean_text(
            payload.get("project_library_context")
            or payload.get("context_key")
            or payload.get("project_context")
        )
        target_type = _clean_text(payload.get("target_type")).lower()
        target_id = _clean_text(payload.get("target_id"))
        write_authority = _clean_text(payload.get("database_write_authority"))
        auto_adopt = payload.get("ai_consensus_auto_adopt_allowed")
        if isinstance(auto_adopt, str):
            auto_adopt_enabled = _token(auto_adopt) in {"true", "1", "yes"}
        else:
            auto_adopt_enabled = bool(auto_adopt)

        valid = (
            candidate.candidate_type == "object_review_audit"
            and (target_type in self.VALID_SOURCE_TARGET_TYPES or target_id.lower() == "new")
            and schema_ok
            and context_marker == "li_s_sac_dac"
            and write_authority == "user_submit_only"
            and not auto_adopt_enabled
        )
        if valid:
            return
        raise ProjectLibrarySubmissionBlockedError(
            {
                "code": "project_library_v4_submit_blocked",
                "message": "source_candidate_ids must reference Li-S SAC/DAC v4 DFT review candidates only.",
                "hard_blockers": ["invalid_source_candidate_for_project_library_v4"],
                "ml_blockers": [],
                "candidate_id": str(candidate.id),
            }
        )

    def _target_row(
        self,
        *,
        paper_id: UUID,
        payload: ProjectLibraryUserSubmitRequest,
        source_candidates: list[ExternalAnalysisCandidate],
    ) -> DFTResult | None:
        if payload.record_id:
            row = self.session.get(DFTResult, UUID(payload.record_id))
            if row is None or row.paper_id != paper_id:
                raise LookupError("Requested DFTResult record_id was not found for this paper.")
            return row

        target_ids = {
            str(item.get("target_id"))
            for candidate in source_candidates
            for item in [candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}]
            if str(item.get("target_type") or "").strip().lower() in {"dft_results", "dft_result"}
            and str(item.get("target_id") or "").strip()
            and str(item.get("target_id") or "").strip().lower() != "new"
        }
        if len(target_ids) > 1:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Source candidates point to multiple DFTResult rows; keep the submission unresolved until the user chooses one row.",
                    "hard_blockers": ["ambiguous_source_dft_result"],
                    "ml_blockers": [],
                }
            )
        if len(target_ids) == 1:
            row_id = next(iter(target_ids))
            row = self.session.get(DFTResult, UUID(row_id))
            if row is None or row.paper_id != paper_id:
                raise LookupError("Source candidate target DFTResult was not found for this paper.")
            return row
        return None

    def _resolve_catalyst(
        self,
        *,
        paper_id: UUID,
        payload: ProjectLibraryUserSubmitRequest,
        row: DFTResult | None,
        source_candidates: list[ExternalAnalysisCandidate],
    ) -> CatalystSample:
        catalyst_id = (
            _clean_text(payload.catalyst_sample_id)
            or _clean_text((payload.active_site_ref or {}).get("catalyst_sample_id"))
            or _clean_text((row.evidence_payload or {}).get("active_site_ref", {}).get("catalyst_sample_id") if row and isinstance(row.evidence_payload, dict) else None)
            or _clean_text(str(row.catalyst_sample_id) if row and row.catalyst_sample_id else "")
        )
        if not catalyst_id:
            for candidate in source_candidates:
                payload_dict = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
                ref = payload_dict.get("active_site_ref")
                if isinstance(ref, dict) and ref.get("catalyst_sample_id"):
                    catalyst_id = _clean_text(ref.get("catalyst_sample_id"))
                    break
        if not catalyst_id:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Li-S SAC/DAC v4 submit requires catalyst_sample_id to resolve the active-site bundle.",
                    "hard_blockers": ["missing_catalyst_sample_id"],
                    "ml_blockers": [],
                }
            )
        catalyst = self.session.get(CatalystSample, UUID(catalyst_id))
        if catalyst is None or catalyst.paper_id != paper_id:
            raise ProjectLibrarySubmissionBlockedError(
                {
                    "code": "project_library_v4_submit_blocked",
                    "message": "Resolved catalyst_sample_id does not belong to this paper.",
                    "hard_blockers": ["invalid_catalyst_sample_id"],
                    "ml_blockers": [],
                }
            )
        return catalyst

    def _resolve_active_site(
        self,
        *,
        payload: ProjectLibraryUserSubmitRequest,
        catalyst: CatalystSample,
        row: DFTResult | None,
    ) -> tuple[str, dict[str, Any]]:
        active_site_ref = dict(payload.active_site_ref or {})
        if row is not None and isinstance(row.evidence_payload, dict):
            existing_ref = row.evidence_payload.get("active_site_ref")
            if isinstance(existing_ref, dict):
                active_site_ref = {**existing_ref, **active_site_ref}
        active_site_instance_key = (
            _clean_text(payload.active_site_instance_key)
            or _clean_text(active_site_ref.get("active_site_instance_key"))
        )
        if active_site_instance_key:
            active_site_ref = {
                **active_site_ref,
                "paper_id": str(catalyst.paper_id),
                "catalyst_sample_id": str(catalyst.id),
                "active_site_instance_key": active_site_instance_key,
                "binding_source": "user_submit",
            }
        return active_site_instance_key, active_site_ref

    def _create_row(self, prepared: PreparedSubmission) -> DFTResult:
        payload = prepared.request
        source_location = payload.source_location or {}
        return DFTResult(
            paper_id=prepared.paper.id,
            catalyst_sample_id=prepared.catalyst.id,
            adsorbate=payload.adsorbate,
            property_type=payload.property_type,
            value=payload.value,
            unit=payload.unit,
            reaction_step=payload.reaction_step,
            reaction_type="SRR_LiS",
            source_section=_clean_text(source_location.get("section") or source_location.get("section_title")) or None,
            source_figure=_clean_text(source_location.get("figure") or source_location.get("figure_label")) or None,
            evidence_text=payload.source_text,
            confidence=payload.confidence_level,
            candidate_status=self.FINAL_USER_SUBMITTED_STATUS,
            evidence_payload={},
        )

    def _apply_row_submission(self, *, row: DFTResult, prepared: PreparedSubmission) -> DFTResult:
        payload = prepared.request
        row.catalyst_sample_id = prepared.catalyst.id
        row.adsorbate = payload.adsorbate
        row.property_type = payload.property_type
        row.value = payload.value
        row.unit = payload.unit
        row.reaction_step = payload.reaction_step
        row.reaction_type = "SRR_LiS"
        row.source_section = _clean_text((payload.source_location or {}).get("section") or (payload.source_location or {}).get("section_title")) or row.source_section
        row.source_figure = _clean_text((payload.source_location or {}).get("figure") or (payload.source_location or {}).get("figure_label")) or row.source_figure
        row.evidence_text = payload.source_text
        row.confidence = payload.confidence_level
        row.candidate_status = self.FINAL_USER_SUBMITTED_STATUS

        merged_payload = dict(row.evidence_payload or {}) if isinstance(row.evidence_payload, dict) else {}
        merged_payload.update(
            {
                "schema_version": payload.schema_version,
                "source_schema_version": payload.schema_version,
                "project_library_context": payload.context_key,
                "database_write_authority": "user_submit_only",
                "ai_consensus_auto_adopt_allowed": False,
                "submitted_by_user": True,
                "submitted_by": prepared.submitted_by,
                "submission_status": self.FINAL_USER_SUBMITTED_STATUS,
                "decision_status": payload.decision_status or "ready_for_submission",
                "user_edits": payload.user_edits,
                "resolved_conflicts": payload.resolved_conflicts,
                "source_candidate_ids": [str(candidate.id) for candidate in prepared.source_candidates],
                "active_site_instance_key": prepared.active_site_instance_key,
                "active_site_ref": prepared.active_site_ref,
                "energy_kind": payload.energy_kind,
                "source_text": payload.source_text,
                "source_location": payload.source_location or {},
                "support_raw": payload.support_raw,
                "support_normalized": payload.support_normalized,
                "support_confidence": payload.support_confidence,
                "dft_setting_id": payload.dft_setting_id,
            }
        )
        row.evidence_payload = merged_payload
        return row
