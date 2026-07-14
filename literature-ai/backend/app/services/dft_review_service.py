from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTResult, ExtractionFieldReview, Paper, PaperCorrection, WorkflowJob, utcnow
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_identity_service import DFTIdentityV2
from app.services.extraction_review_service import ExtractionReviewService
from app.services.dft_review_fields import DFT_CORRECTION_FIELD_ALIASES, DFT_REVIEW_FIELD_ALIASES
from app.services.dft_review_imported import DFTImportedOpinionMixin
from app.services.dft_review_materials import DFTMaterialBindingMixin
from app.services.review_service import ReviewService
from app.utils.evidence_anchors import has_evidence_anchor
from app.utils.review_safety import DFT_REJECTED_STATUSES, is_export_eligible_extraction


__all__ = [
    "DFT_CORRECTION_FIELD_ALIASES",
    "DFT_REVIEW_FIELD_ALIASES",
    "DFTResultReviewService",
]


class DFTResultReviewService(
    DFTImportedOpinionMixin,
    DFTMaterialBindingMixin,
):
    """Promote evidence-backed DFT candidates through the existing review gate."""

    IMPORTED_NEGATIVE_DECISIONS = {"REJECT", "REJECTED", "BLOCK", "DENY", "DROP"}

    def __init__(self, session: Session) -> None:
        self.session = session
        self.review_service = ExtractionReviewService(session)
        self.issue_lifecycle = DFTAuditIssueLifecycleService(session)

    def begin_import_batch(self, *, paper_id: UUID, rows: list[DFTResult]) -> None:
        self.review_service.begin_dft_import_batch(paper_id=paper_id, rows=rows)

    def end_import_batch(self) -> None:
        self.review_service.end_dft_import_batch()

    def current_review_versions(self, *, paper_id: UUID, result_id: UUID) -> dict[str, int]:
        target_id = str(result_id)
        if target_id in self.review_service._batch_target_ids:
            return {
                field_name: int(review.write_version or 1)
                for (cached_target_id, field_name), review in self.review_service._batch_reviews.items()
                if cached_target_id == target_id
            }
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == target_id,
            )
        ).all()
        return {str(review.field_name): int(review.write_version or 1) for review in reviews}

    def verify_result(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        confirm_reviewed_against_pdf: bool,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
        expected_write_versions: dict[str, int] | None = None,
        expected_write_version: int | None = None,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
        verification_actor_type: str = "human",
        source_label: str | None = None,
        commit: bool = True,
        compact_result: bool = False,
    ) -> dict[str, Any]:
        if not confirm_reviewed_against_pdf:
            raise ValueError("Explicit PDF/evidence review confirmation is required.")
        if verification_actor_type not in {"human", "ai"}:
            raise ValueError("verification_actor_type must be human or ai")

        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")
        if str(row.candidate_status or "").strip().lower() in DFT_REJECTED_STATUSES:
            raise ValueError("rejected_dft_result_cannot_be_verified")

        snapshot = self.review_service.get_target_field_snapshot("dft_results", row)
        selected_fields = self._select_review_fields(snapshot, field_names)
        if not selected_fields:
            raise ValueError("No non-empty DFT result fields are available for verification.")

        verification_note = reviewer_note or "Verified through the DFT candidate review workflow."
        try:
            reviews = self.review_service.mark_verified(
                paper_id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type="dft_results",
                    target_id=str(result_id),
                    field_names=selected_fields,
                    expected_write_versions=expected_write_versions or {},
                    expected_write_version=expected_write_version,
                    reviewer=reviewer or "codex_review",
                    reviewer_note=verification_note,
                ),
                commit=False,
                verification_actor_type=verification_actor_type,
                source_label=source_label,
                imported_evidence_payload=evidence_payload,
            )
        except ValueError as exc:
            if "missing_evidence_reference" not in str(exc) or not self._has_anchor(evidence_payload):
                raise
            note = reviewer_note or "Verified through imported IDE-AI evidence anchors."
            reviews = []
            for field_name in selected_fields:
                field_snapshot = snapshot[field_name]
                existing_review = self.review_service._find_review(
                    paper_id,
                    "dft_results",
                    str(result_id),
                    field_name,
                )
                expected_version = (expected_write_versions or {}).get(field_name)
                if expected_version is None and len(selected_fields) == 1:
                    expected_version = expected_write_version
                if existing_review is not None:
                    self.review_service._guard_expected_write_version(
                        existing_review,
                        expected_version,
                        created=False,
                    )
                review = existing_review or self.review_service._get_or_create_review(
                    paper_id,
                    "dft_results",
                    str(result_id),
                    field_name,
                )
                self.review_service._guard_expected_write_version(
                    review,
                    expected_version,
                    created=existing_review is None and getattr(review, "_created_by_get_or_create", False),
                )
                review.original_value = field_snapshot["value"]
                review.reviewed_value = field_snapshot["value"]
                review.unit = field_snapshot["unit"]
                review.evidence_text = field_snapshot["evidence_text"]
                review.reviewer_status = "verified"
                review.reviewer = reviewer or "codex_review"
                review.reviewer_note = note
                verification_key = "human_verification" if verification_actor_type == "human" else "ai_verification"
                review.review_payload = {
                    verification_key: {
                        "reviewer": reviewer or "codex_review",
                        "reviewer_note": note,
                        "decision": "verified",
                        "writes_final_truth": True,
                        "verification_actor_type": verification_actor_type,
                        "source_label": source_label,
                    },
                    "imported_evidence_payload": evidence_payload,
                }
                review.target_resolution_status = "active"
                review.remapped_from_target_id = None
                review.last_resolved_target_id = str(result_id)
                self.review_service.resolver._refresh_review_identity(review, "dft_results", row)
                self.session.add(review)
                self.session.flush()
                reviews.append(self.review_service._serialize(review))
        reviewer_name = reviewer or "codex_review"
        if verification_actor_type == "ai":
            row.candidate_status = "ai_verified_ml_ready"
            row.ml_ready_at = utcnow()
            row.ml_ready_source = source_label or reviewer_name
            row.local_ai_verification_payload = {
                "reviewer": reviewer_name,
                "source_label": source_label,
                "field_names": selected_fields,
                "evidence_payload": evidence_payload,
                "reviewer_note": verification_note,
                "final_decision": "ready_for_ml_export",
            }
        self.session.add(row)
        self.session.flush()
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        result_identity = self.issue_lifecycle.identity_for_result(row)
        identity_block_reason = None if result_identity.observation_key else (
            result_identity.error_code or "invalid_v2_result_identity"
        )
        if identity_block_reason:
            gate = replace(
                gate,
                eligible=False,
                reasons=tuple(dict.fromkeys([*gate.reasons, identity_block_reason])),
            )
        closed_issues = self.issue_lifecycle.apply_verify(
            paper_id=paper_id,
            result_id=result_id,
            reviewer=reviewer_name,
            actor_type=verification_actor_type,
            export_gate_passed=gate.eligible,
        )
        if closed_issues and not gate.eligible:
            self.session.flush()
            gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
            if identity_block_reason:
                gate = replace(
                    gate,
                    eligible=False,
                    reasons=tuple(dict.fromkeys([*gate.reasons, identity_block_reason])),
                )
        if gate.eligible:
            row.candidate_status = "ai_verified_ml_ready" if verification_actor_type == "ai" else "ML_Ready"
            if verification_actor_type == "ai" and row.ml_ready_at is None:
                row.ml_ready_at = utcnow()
                row.ml_ready_source = source_label or reviewer_name
        else:
            row.candidate_status = (
                "human_reviewed_needs_evidence"
                if verification_actor_type == "human"
                else "ai_repair_failed_not_imported"
            )
            if verification_actor_type == "ai":
                row.ml_ready_at = None
                row.ml_ready_source = None
                row.local_ai_verification_payload = {
                    **(row.local_ai_verification_payload or {}),
                    "final_decision": "repair_failed_not_exportable",
                    "blocked_reasons": list(gate.reasons),
                }
        self.session.add(row)
        if gate.eligible:
            additionally_closed = self.issue_lifecycle.apply_verify(
                paper_id=paper_id,
                result_id=result_id,
                reviewer=reviewer_name,
                actor_type=verification_actor_type,
                export_gate_passed=True,
            )
            known_ids = {issue.id for issue in closed_issues}
            closed_issues.extend(issue for issue in additionally_closed if issue.id not in known_ids)
        audit = AuditLog(
            paper_id=paper_id,
            action="verify_dft_result",
            source=reviewer_name,
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "field_names": selected_fields,
                "review_ids": [str(item.id) for item in reviews],
                "is_exportable": gate.eligible,
                "blocked_reasons": list(gate.reasons),
                "closed_audit_issue_ids": [str(issue.id) for issue in closed_issues],
                "actor_type": verification_actor_type,
                "source_label": source_label,
            },
        )
        self.session.add(audit)
        self._add_workflow_job(
            paper_id=paper_id,
            action="verify_dft_result",
            payload={
                "dft_result_id": str(result_id),
                "field_names": selected_fields,
                "is_exportable": gate.eligible,
                "blocked_reasons": list(gate.reasons),
                "actor_type": verification_actor_type,
            },
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        result = {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "field_names": selected_fields,
            "export_safety": self._gate_payload(row, gate),
            "closed_audit_issue_ids": [str(issue.id) for issue in closed_issues],
            "actor_type": verification_actor_type,
            "audit_log_id": str(audit.id),
        }
        if compact_result:
            result["review_ids"] = [str(item.id) for item in reviews]
        else:
            result["reviews"] = [item.model_dump(mode="json") for item in reviews]
        return result

    def _rewrite_verified_review_payloads(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        field_names: list[str],
        reviewer: str,
        reviewer_note: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        verification_actor_type: str,
        source_label: str | None,
    ):
        rows = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(result_id),
                ExtractionFieldReview.field_name.in_(field_names),
            )
        ).all()
        for review in rows:
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            payload.pop("human_verification", None)
            payload["ai_verification"] = {
                "reviewer": reviewer,
                "reviewer_note": reviewer_note,
                "decision": "verified",
                "writes_final_truth": True,
                "verification_actor_type": verification_actor_type,
                "source_label": source_label,
            }
            if evidence_payload is not None:
                payload["imported_evidence_payload"] = evidence_payload
            review.review_payload = payload
            self.session.add(review)
        self.session.flush()
        return [self.review_service._serialize(row) for row in rows]

    def _attach_imported_evidence_payload(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        field_names: list[str],
        evidence_payload: dict[str, Any] | list[Any],
    ) -> None:
        rows = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(result_id),
                ExtractionFieldReview.field_name.in_(field_names),
            )
        ).all()
        for review in rows:
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            review.review_payload = {
                **payload,
                "imported_evidence_payload": evidence_payload,
            }
            self.session.add(review)
        self.session.flush()

    @staticmethod
    def _has_anchor(evidence_payload: dict[str, Any] | list[Any] | None) -> bool:
        return has_evidence_anchor(evidence_payload)

    def reject_result(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        confirm_reject_candidate: bool,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
        expected_write_versions: dict[str, int] | None = None,
        expected_write_version: int | None = None,
        verification_actor_type: str = "human",
        source_label: str | None = None,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        if not confirm_reject_candidate:
            raise ValueError("Explicit DFT candidate rejection confirmation is required.")
        if verification_actor_type not in {"human", "ai"}:
            raise ValueError("verification_actor_type must be human or ai")

        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")

        snapshot = self.review_service.get_target_field_snapshot("dft_results", row)
        selected_fields = self._select_review_fields(snapshot, field_names)
        if not selected_fields:
            raise ValueError("No non-empty DFT result fields are available for rejection.")

        note = reviewer_note or "Rejected through the DFT candidate review workflow."
        reviews = self.review_service.save_reviews(
            paper_id,
            [
                ExtractionFieldReviewSaveItem(
                    target_type="dft_results",
                    target_id=str(result_id),
                    field_name=field_name,
                    expected_write_version=(
                        (expected_write_versions or {}).get(field_name)
                        if field_name in (expected_write_versions or {})
                        else expected_write_version
                    ),
                    original_value=snapshot[field_name]["value"],
                    reviewed_value=None,
                    unit=snapshot[field_name]["unit"],
                    evidence_text=snapshot[field_name]["evidence_text"],
                    reviewer_status="rejected",
                    reviewer=reviewer or "codex_review",
                    reviewer_note=note,
                )
                for field_name in selected_fields
            ],
            commit=False,
        )
        if verification_actor_type == "ai":
            reviews = self._rewrite_rejected_review_payloads(
                paper_id=paper_id,
                result_id=result_id,
                field_names=selected_fields,
                reviewer=reviewer or "codex_review",
                reviewer_note=note,
                evidence_payload=evidence_payload,
                source_label=source_label,
            )
        row.candidate_status = "Rejected"
        self.session.add(row)
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        reviewer_name = reviewer or "codex_review"
        closed_issues = self.issue_lifecycle.apply_reject(
            paper_id=paper_id,
            result_id=result_id,
            reviewer=reviewer_name,
            actor_type=verification_actor_type,
        )
        audit = AuditLog(
            paper_id=paper_id,
            action="reject_dft_result",
            source=reviewer_name,
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "field_names": selected_fields,
                "review_ids": [str(item.id) for item in reviews],
                "blocked_reasons": list(gate.reasons),
                "review_status": gate.review_status,
                "closed_audit_issue_ids": [str(issue.id) for issue in closed_issues],
                "actor_type": verification_actor_type,
                "source_label": source_label,
            },
        )
        self.session.add(audit)
        self._add_workflow_job(
            paper_id=paper_id,
            action="reject_dft_result",
            payload={
                "dft_result_id": str(result_id),
                "field_names": selected_fields,
                "blocked_reasons": list(gate.reasons),
                "actor_type": verification_actor_type,
            },
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "field_names": selected_fields,
            "reviews": [item.model_dump(mode="json") for item in reviews],
            "export_safety": self._gate_payload(row, gate),
            "closed_audit_issue_ids": [str(issue.id) for issue in closed_issues],
            "actor_type": verification_actor_type,
            "audit_log_id": str(audit.id),
        }

    def _rewrite_rejected_review_payloads(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        field_names: list[str],
        reviewer: str,
        reviewer_note: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        source_label: str | None,
    ):
        rows = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(result_id),
                ExtractionFieldReview.field_name.in_(field_names),
            )
        ).all()
        for review in rows:
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            payload.pop("human_verification", None)
            payload["ai_verification"] = {
                "reviewer": reviewer,
                "reviewer_note": reviewer_note,
                "decision": "rejected",
                "writes_final_truth": True,
                "verification_actor_type": "ai",
                "source_label": source_label,
            }
            if evidence_payload is not None:
                payload["imported_evidence_payload"] = evidence_payload
            review.review_payload = payload
            self.session.add(review)
        self.session.flush()
        return [self.review_service._serialize(row) for row in rows]

    def revoke_result(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")

        snapshot = self.review_service.get_target_field_snapshot("dft_results", row)
        selected_fields = self._select_review_fields(snapshot, field_names)
        if not selected_fields:
            raise ValueError("No DFT review fields are available for revocation.")

        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(result_id),
                ExtractionFieldReview.field_name.in_(selected_fields),
            )
        ).all()
        if not reviews:
            raise ValueError("This DFT result has no review state to revoke.")

        note = reviewer_note or "Revoked from the Literature Library DFT panel and returned to the pending queue."
        for review in reviews:
            review.reviewer_status = "pending"
            review.reviewer = reviewer or "codex_review"
            review.reviewer_note = note
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            human_verification = payload.get("human_verification") if isinstance(payload.get("human_verification"), dict) else {}
            review.review_payload = {
                **payload,
                "human_verification": {
                    **human_verification,
                    "reviewer": reviewer or "codex_review",
                    "reviewer_note": note,
                    "decision": "revoked",
                    "writes_final_truth": False,
                },
            }
            self.session.add(review)

        row.candidate_status = "system_candidate"
        self.session.add(row)
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        audit = AuditLog(
            paper_id=paper_id,
            action="revoke_dft_result_review",
            source=reviewer or "codex_review",
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "field_names": selected_fields,
                "review_ids": [str(item.id) for item in reviews],
                "is_exportable": gate.eligible,
                "blocked_reasons": list(gate.reasons),
            },
        )
        self.session.add(audit)
        self._add_workflow_job(
            paper_id=paper_id,
            action="revoke_dft_result_review",
            payload={
                "dft_result_id": str(result_id),
                "field_names": selected_fields,
                "is_exportable": gate.eligible,
                "blocked_reasons": list(gate.reasons),
            },
        )
        self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "field_names": selected_fields,
            "reviews": [self.review_service._serialize(item).model_dump(mode="json") for item in reviews],
            "export_safety": self._gate_payload(row, gate),
            "audit_log_id": str(audit.id),
        }

    def verify_results_batch(
        self,
        *,
        paper_id: UUID,
        result_ids: list[UUID],
        confirm_reviewed_against_pdf: bool,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        if not confirm_reviewed_against_pdf:
            raise ValueError("Explicit PDF/evidence review confirmation is required.")

        verified: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for rid in result_ids:
            try:
                existing_reviews = self.session.scalars(
                    select(ExtractionFieldReview).where(
                        ExtractionFieldReview.paper_id == paper_id,
                        ExtractionFieldReview.target_type == "dft_results",
                        ExtractionFieldReview.target_id == str(rid),
                    )
                ).all()
                result = self.verify_result(
                    paper_id=paper_id,
                    result_id=rid,
                    confirm_reviewed_against_pdf=True,
                    reviewer=reviewer,
                    reviewer_note=reviewer_note,
                    field_names=field_names,
                    expected_write_versions={
                        review.field_name: review.write_version
                        for review in existing_reviews
                    },
                )
                verified.append(result)
            except Exception as exc:
                skipped.append({"dft_result_id": str(rid), "reason": str(exc)})
        return {
            "paper_id": str(paper_id),
            "total_requested": len(result_ids),
            "verified": len(verified),
            "skipped": len(skipped),
            "verified_items": verified,
            "skipped_items": skipped,
        }

    def reject_results_batch(
        self,
        *,
        paper_id: UUID,
        result_ids: list[UUID],
        confirm_reject_candidate: bool,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        if not confirm_reject_candidate:
            raise ValueError("Explicit DFT candidate rejection confirmation is required.")

        rejected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for rid in result_ids:
            try:
                result = self.reject_result(
                    paper_id=paper_id,
                    result_id=rid,
                    confirm_reject_candidate=True,
                    reviewer=reviewer,
                    reviewer_note=reviewer_note,
                    field_names=field_names,
                )
                rejected.append(result)
            except Exception as exc:
                skipped.append({"dft_result_id": str(rid), "reason": str(exc)})
        return {
            "paper_id": str(paper_id),
            "total_requested": len(result_ids),
            "rejected": len(rejected),
            "skipped": len(skipped),
            "rejected_items": rejected,
            "skipped_items": skipped,
        }

    def propose_correction(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        confirm_correction_proposal: bool,
        field_name: str,
        proposed_value: Any,
        reason: str,
        reviewer: str | None = None,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        if not confirm_correction_proposal:
            raise ValueError("Explicit DFT correction proposal confirmation is required.")
        if not reason or not reason.strip():
            raise ValueError("A correction reason is required.")

        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")

        canonical_field = DFT_CORRECTION_FIELD_ALIASES.get(
            str(field_name or "").strip(),
            str(field_name or "").strip(),
        )
        if canonical_field not in ReviewService.ALLOWED_DFT_RESULT_FIELDS:
            raise ValueError(f"Unsupported DFT result correction field: {field_name}")
        if canonical_field == "catalyst_sample_id":
            if not has_evidence_anchor(evidence_payload):
                raise ValueError("DFT catalyst/material binding requires at least one evidence anchor from the source PDF, section, table, figure, or quoted text.")
            try:
                proposed_uuid = UUID(str(proposed_value))
            except (TypeError, ValueError) as exc:
                raise ValueError("DFT catalyst/material binding requires a valid catalyst_sample_id UUID.") from exc
            catalyst = self.session.get(CatalystSample, proposed_uuid)
            if catalyst is None:
                raise ValueError("Target catalyst sample does not exist.")
            if catalyst.paper_id != paper_id:
                raise ValueError("Target catalyst sample does not belong to this paper.")
            proposed_value = str(catalyst.id)

        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer or "codex_review",
            field_name="dft_results",
            target_path=f"dft_results:{result_id}:{canonical_field}",
            operation="replace",
            proposed_value=proposed_value,
            reason=reason.strip(),
            evidence_payload=evidence_payload,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="propose_dft_result_correction",
                source=reviewer or "codex_review",
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={
                    "dft_result_id": str(result_id),
                    "field_name": canonical_field,
                    "target_path": correction.target_path,
                },
            )
        )
        self._add_workflow_job(
            paper_id=paper_id,
            action="propose_dft_result_correction",
            payload={
                "dft_result_id": str(result_id),
                "field_name": canonical_field,
                "target_path": correction.target_path,
                "correction_id": str(correction.id),
            },
        )
        self.session.commit()
        self.session.refresh(correction)
        return self._correction_payload(correction)

    def manually_update_result(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        confirm_manual_update: bool,
        updates: dict[str, Any],
        reason: str,
        reviewer: str | None = None,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
        commit: bool = True,
        prepared_identity: DFTIdentityV2 | None = None,
        identity_conflict_exclude_ids: set[UUID] | None = None,
    ) -> dict[str, Any]:
        if not confirm_manual_update:
            raise ValueError("Explicit manual DFT update confirmation is required.")
        if not str(reason or "").strip():
            raise ValueError("A manual DFT update reason is required.")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("At least one DFT result field update is required.")

        row = self.session.scalar(
            select(DFTResult).where(DFTResult.id == result_id).with_for_update()
        )
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")

        canonical_updates: dict[str, Any] = {}
        for field_name, value in updates.items():
            canonical_field = DFT_CORRECTION_FIELD_ALIASES.get(
                str(field_name or "").strip(),
                str(field_name or "").strip(),
            )
            if canonical_field not in ReviewService.ALLOWED_DFT_RESULT_FIELDS:
                raise ValueError(f"Unsupported DFT result update field: {field_name}")
            canonical_updates[canonical_field] = self._normalize_manual_update_value(
                canonical_field,
                value,
            )

        before = {
            field_name: self._json_value(getattr(row, field_name))
            for field_name in canonical_updates
        }
        changed_updates = {
            field_name: value
            for field_name, value in canonical_updates.items()
            if self._json_value(getattr(row, field_name)) != self._json_value(value)
        }
        if not changed_updates:
            raise ValueError("The submitted DFT values are unchanged.")

        target_sample_id = changed_updates.get("catalyst_sample_id")
        if target_sample_id is not None:
            target_sample = self.session.scalar(
                select(CatalystSample)
                .where(CatalystSample.id == target_sample_id)
                .with_for_update()
            )
            if target_sample is None:
                raise LookupError("Target catalyst sample not found.")
            if target_sample.paper_id != paper_id:
                raise ValueError("Target catalyst sample must belong to the same paper as the DFT result.")

        reviewer_name = str(reviewer or "literature_library_user").strip() or "literature_library_user"
        correction_evidence = self._manual_update_evidence(row, evidence_payload)
        corrections: list[PaperCorrection] = []
        review_service = ReviewService(self.session)
        for field_name, proposed_value in changed_updates.items():
            correction = PaperCorrection(
                paper_id=paper_id,
                source=reviewer_name,
                field_name="dft_results",
                target_path=f"dft_results:{result_id}:{field_name}",
                operation="replace",
                proposed_value=self._json_value(proposed_value),
                reason=str(reason).strip(),
                evidence_payload=correction_evidence,
                status="pending",
            )
            self.session.add(correction)
            self.session.flush()
            corrections.append(
                review_service.approve_correction(
                    correction.id,
                    reviewer="human",
                )
            )

        self.session.flush()
        self.session.refresh(row)
        identity = prepared_identity or self.issue_lifecycle.build_identity(
            paper_id=paper_id,
            payload=self.issue_lifecycle.authoritative_payload_for_result(row),
        )
        self._assert_identity_observation_keys_available(
            paper_id=paper_id,
            identities={row.id: identity},
            exclude_ids=identity_conflict_exclude_ids or {row.id},
        )
        self.issue_lifecycle.apply_result_identity(row, identity)

        invalidated_reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(result_id),
            )
        ).all()
        for review in invalidated_reviews:
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            review.reviewer_status = "pending"
            review.reviewed_value = None
            review.reviewer = reviewer_name
            review.reviewer_note = "Invalidated because the DFT row was manually edited and requires re-verification."
            review.review_payload = {
                **payload,
                "human_verification": {
                    "reviewer": reviewer_name,
                    "reviewer_note": review.reviewer_note,
                    "decision": "invalidated_by_manual_update",
                    "writes_final_truth": False,
                },
            }
            self.session.add(review)

        row.candidate_status = "system_candidate"
        self.session.add(row)
        self.session.flush()
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        audit = AuditLog(
            paper_id=paper_id,
            action="manual_update_dft_result",
            source=reviewer_name,
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "changed_fields": list(changed_updates),
                "before": before,
                "after": {
                    field_name: self._json_value(getattr(row, field_name))
                    for field_name in changed_updates
                },
                "reason": str(reason).strip(),
                "correction_ids": [str(item.id) for item in corrections],
                "invalidated_review_ids": [str(item.id) for item in invalidated_reviews],
                "blocked_reasons": list(gate.reasons),
            },
        )
        self.session.add(audit)
        workflow_job_id = self._add_workflow_job(
            paper_id=paper_id,
            action="manual_update_dft_result",
            payload={
                "dft_result_id": str(result_id),
                "changed_fields": list(changed_updates),
                "correction_ids": [str(item.id) for item in corrections],
                "invalidated_review_ids": [str(item.id) for item in invalidated_reviews],
                "blocked_reasons": list(gate.reasons),
            },
        )
        self.session.flush()
        if commit:
            self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "changed_fields": list(changed_updates),
            "corrections": [self._correction_payload(item) for item in corrections],
            "invalidated_review_ids": [str(item.id) for item in invalidated_reviews],
            "export_safety": self._gate_payload(row, gate),
            "audit_log_id": str(audit.id),
            "reverification_task_id": workflow_job_id,
        }

    def rebind_result_group(
        self,
        *,
        paper_id: UUID,
        source_sample_id: UUID,
        target_sample_id: UUID,
        dft_result_ids: list[UUID],
        expected_result_count: int,
        confirm_rebind: bool,
        reason: str,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        if not confirm_rebind:
            raise ValueError("Explicit DFT group rebind confirmation is required.")
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("A DFT group rebind reason is required.")
        if source_sample_id == target_sample_id:
            raise ValueError("Source and target catalyst samples must be different.")
        requested_ids = list(dft_result_ids or [])
        requested_id_set = set(requested_ids)
        if not requested_ids:
            raise ValueError("At least one DFT result ID is required.")
        if len(requested_ids) != len(requested_id_set):
            raise ValueError("dft_result_ids must not contain duplicates.")

        source_rows_stmt = (
            select(DFTResult)
            .where(
                DFTResult.paper_id == paper_id,
                DFTResult.catalyst_sample_id == source_sample_id,
            )
            .order_by(DFTResult.id.asc())
        )
        # Single-row edits lock DFTResult before CatalystSample. Use the same
        # order here, then refresh membership after the sample locks are held.
        self.session.scalars(source_rows_stmt.with_for_update()).all()

        samples = self.session.scalars(
            select(CatalystSample)
            .where(CatalystSample.id.in_([source_sample_id, target_sample_id]))
            .order_by(CatalystSample.id.asc())
            .with_for_update()
        ).all()
        samples_by_id = {sample.id: sample for sample in samples}
        source_sample = samples_by_id.get(source_sample_id)
        target_sample = samples_by_id.get(target_sample_id)
        if source_sample is None or source_sample.paper_id != paper_id:
            raise LookupError("Source catalyst sample not found for this paper.")
        if target_sample is None:
            raise LookupError("Target catalyst sample not found.")
        if target_sample.paper_id != paper_id or target_sample.paper_id != source_sample.paper_id:
            raise ValueError("Source and target catalyst samples must belong to the same paper.")

        request_fingerprint = self._rebind_request_fingerprint(
            source_sample_id=source_sample_id,
            target_sample_id=target_sample_id,
            dft_result_ids=requested_ids,
            expected_result_count=expected_result_count,
        )
        current_rows = self.session.scalars(
            source_rows_stmt.with_for_update().execution_options(populate_existing=True)
        ).all()
        if not current_rows:
            previous_audit = self._find_successful_rebind_audit(
                paper_id=paper_id,
                source_sample_id=source_sample_id,
                request_fingerprint=request_fingerprint,
            )
            if previous_audit is not None:
                rebound_rows = self.session.scalars(
                    select(DFTResult)
                    .where(DFTResult.id.in_(requested_ids))
                    .order_by(DFTResult.id.asc())
                    .with_for_update()
                ).all()
                if (
                    len(rebound_rows) == len(requested_ids)
                    and all(
                        row.paper_id == paper_id and row.catalyst_sample_id == target_sample_id
                        for row in rebound_rows
                    )
                ):
                    return self._rebind_response_from_audit(previous_audit, status="already_rebound")

        actual_count = len(current_rows)
        if expected_result_count != actual_count:
            raise ValueError(
                "DFT group result count is stale: "
                f"expected {expected_result_count}, found {actual_count}."
            )
        current_id_set = {row.id for row in current_rows}
        if requested_id_set != current_id_set:
            missing = sorted(str(row_id) for row_id in current_id_set - requested_id_set)
            unexpected = sorted(str(row_id) for row_id in requested_id_set - current_id_set)
            raise ValueError(
                "dft_result_ids must exactly cover every DFT result currently bound to the source sample; "
                f"missing={missing}, unexpected={unexpected}."
            )

        identities = {
            row.id: self.issue_lifecycle.build_identity(
                paper_id=paper_id,
                payload=self.issue_lifecycle.authoritative_payload_for_result(
                    row,
                    catalyst_sample=target_sample,
                ),
            )
            for row in current_rows
        }
        self._assert_identity_observation_keys_available(
            paper_id=paper_id,
            identities=identities,
            exclude_ids=current_id_set,
        )

        for row in current_rows:
            self.issue_lifecycle.clear_result_observation_key_for_rekey(row)
        self.session.flush()

        reviewer_name = str(reviewer or "literature_library_user").strip() or "literature_library_user"
        update_results: list[dict[str, Any]] = []
        for result_id in requested_ids:
            update_results.append(
                self.manually_update_result(
                    paper_id=paper_id,
                    result_id=result_id,
                    confirm_manual_update=True,
                    updates={"catalyst_sample_id": target_sample_id},
                    reason=reason_text,
                    reviewer=reviewer_name,
                    commit=False,
                    prepared_identity=identities[result_id],
                    identity_conflict_exclude_ids=current_id_set,
                )
            )

        remaining_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(DFTResult)
                .where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.catalyst_sample_id == source_sample_id,
                )
            )
            or 0
        )
        correction_ids = [
            str(correction["id"])
            for result in update_results
            for correction in result.get("corrections", [])
        ]
        invalidated_review_ids = [
            str(review_id)
            for result in update_results
            for review_id in result.get("invalidated_review_ids", [])
        ]
        result_audit_log_ids = [str(result["audit_log_id"]) for result in update_results]
        reverification_task_ids = [
            str(result["reverification_task_id"])
            for result in update_results
            if result.get("reverification_task_id")
        ]
        rebound_result_ids = [str(result_id) for result_id in requested_ids]
        audit_payload = {
            "request_fingerprint": request_fingerprint,
            "source_sample_id": str(source_sample_id),
            "target_sample_id": str(target_sample_id),
            "binding_change": {
                "from_catalyst_sample_id": str(source_sample_id),
                "to_catalyst_sample_id": str(target_sample_id),
            },
            "rebound_result_ids": rebound_result_ids,
            "rebound_result_count": len(rebound_result_ids),
            "expected_result_count": expected_result_count,
            "reason": reason_text,
            "requires_reverification": True,
            "result_audit_log_ids": result_audit_log_ids,
            "correction_ids": correction_ids,
            "invalidated_review_ids": invalidated_review_ids,
            "reverification_task_ids": reverification_task_ids,
            "remaining_dft_result_count": remaining_count,
        }
        audit = AuditLog(
            paper_id=paper_id,
            action="rebind_dft_result_group",
            source=reviewer_name,
            target_type="catalyst_samples",
            target_id=str(source_sample_id),
            payload=audit_payload,
        )
        self.session.add(audit)
        self.session.flush()
        self.session.commit()
        self.session.refresh(audit)
        return {
            "status": "rebound",
            "source_sample_id": str(source_sample_id),
            "target_sample_id": str(target_sample_id),
            "rebound_result_ids": rebound_result_ids,
            "rebound_result_count": len(rebound_result_ids),
            "requires_reverification": True,
            "audit_log_id": str(audit.id),
            "result_audit_log_ids": result_audit_log_ids,
            "correction_ids": correction_ids,
            "invalidated_review_ids": invalidated_review_ids,
            "reverification_task_ids": reverification_task_ids,
            "remaining_dft_result_count": remaining_count,
        }

    def _assert_identity_observation_keys_available(
        self,
        *,
        paper_id: UUID,
        identities: dict[UUID, DFTIdentityV2],
        exclude_ids: set[UUID],
    ) -> None:
        result_ids_by_key: dict[str, list[UUID]] = {}
        for result_id, identity in identities.items():
            if identity.observation_key:
                result_ids_by_key.setdefault(identity.observation_key, []).append(result_id)
        internal_conflicts = {
            key: result_ids
            for key, result_ids in result_ids_by_key.items()
            if len(result_ids) > 1
        }
        if internal_conflicts:
            key, result_ids = sorted(internal_conflicts.items())[0]
            raise ValueError(
                "write_conflict:dft_identity_batch_observation_key_conflict:"
                f"{key}:{','.join(sorted(str(result_id) for result_id in result_ids))}"
            )
        observation_keys = sorted(result_ids_by_key)
        if not observation_keys:
            return
        conflicts = self.session.scalars(
            select(DFTResult)
            .where(
                DFTResult.paper_id == paper_id,
                DFTResult.identity_version == 2,
                DFTResult.observation_key.in_(observation_keys),
                DFTResult.id.notin_(exclude_ids),
            )
            .order_by(DFTResult.id.asc())
            .with_for_update()
        ).all()
        if conflicts:
            conflict = conflicts[0]
            raise ValueError(
                "write_conflict:dft_identity_observation_key_conflict:"
                f"{conflict.observation_key}:{conflict.id}"
            )

    def _find_successful_rebind_audit(
        self,
        *,
        paper_id: UUID,
        source_sample_id: UUID,
        request_fingerprint: str,
    ) -> AuditLog | None:
        audits = self.session.scalars(
            select(AuditLog)
            .where(
                AuditLog.paper_id == paper_id,
                AuditLog.action == "rebind_dft_result_group",
                AuditLog.target_type == "catalyst_samples",
                AuditLog.target_id == str(source_sample_id),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .with_for_update()
        ).all()
        return next(
            (
                audit
                for audit in audits
                if isinstance(audit.payload, dict)
                and audit.payload.get("request_fingerprint") == request_fingerprint
            ),
            None,
        )

    @staticmethod
    def _rebind_request_fingerprint(
        *,
        source_sample_id: UUID,
        target_sample_id: UUID,
        dft_result_ids: list[UUID],
        expected_result_count: int,
    ) -> str:
        canonical = json.dumps(
            {
                "source_sample_id": str(source_sample_id),
                "target_sample_id": str(target_sample_id),
                "dft_result_ids": sorted(str(result_id) for result_id in dft_result_ids),
                "expected_result_count": expected_result_count,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _rebind_response_from_audit(audit: AuditLog, *, status: str) -> dict[str, Any]:
        payload = audit.payload if isinstance(audit.payload, dict) else {}
        return {
            "status": status,
            "source_sample_id": payload.get("source_sample_id"),
            "target_sample_id": payload.get("target_sample_id"),
            "rebound_result_ids": list(payload.get("rebound_result_ids") or []),
            "rebound_result_count": int(payload.get("rebound_result_count") or 0),
            "requires_reverification": bool(payload.get("requires_reverification", True)),
            "audit_log_id": str(audit.id),
            "result_audit_log_ids": list(payload.get("result_audit_log_ids") or []),
            "correction_ids": list(payload.get("correction_ids") or []),
            "invalidated_review_ids": list(payload.get("invalidated_review_ids") or []),
            "reverification_task_ids": list(payload.get("reverification_task_ids") or []),
            "remaining_dft_result_count": int(payload.get("remaining_dft_result_count") or 0),
        }

    @staticmethod
    def _json_value(value: Any) -> Any:
        return str(value) if isinstance(value, UUID) else value

    @staticmethod
    def _normalize_manual_update_value(field_name: str, value: Any) -> Any:
        if field_name in {"value", "value_upper", "confidence"}:
            if value in ("", None):
                return None
            try:
                normalized = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"DFT field {field_name} requires a numeric value.") from exc
            if field_name == "confidence" and not 0 <= normalized <= 1:
                raise ValueError("DFT confidence must be between 0 and 1.")
            return normalized
        if field_name == "catalyst_sample_id":
            if value in ("", None):
                return None
            try:
                return UUID(str(value))
            except (TypeError, ValueError) as exc:
                raise ValueError("DFT catalyst_sample_id must be a valid UUID.") from exc
        if value is None:
            return None
        normalized_text = str(value).strip()
        return normalized_text or None

    @staticmethod
    def _manual_update_evidence(
        row: DFTResult,
        evidence_payload: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any] | list[Any] | None:
        if evidence_payload:
            return evidence_payload
        base = dict(row.evidence_payload or {}) if isinstance(row.evidence_payload, dict) else {}
        if row.source_section and not base.get("section"):
            base["section"] = row.source_section
        if row.source_figure and not base.get("figure"):
            base["figure"] = row.source_figure
        if row.evidence_text and not base.get("quoted_text"):
            base["quoted_text"] = row.evidence_text
        return base or None

    def _add_workflow_job(self, *, paper_id: UUID, action: str, payload: dict[str, Any]) -> str:
        paper = self.session.get(Paper, paper_id)
        job = WorkflowJob(
            job_id=str(uuid4()),
            type="dft_review_gate",
            status="completed",
            library_name=getattr(paper, "library_name", None) or "默认文献库",
            payload={
                "action": action,
                "paper_id": str(paper_id),
                "title": getattr(paper, "title", None),
                **payload,
            },
            progress={"completed": True},
            result={"status": "recorded"},
        )
        self.session.add(job)
        return job.job_id
