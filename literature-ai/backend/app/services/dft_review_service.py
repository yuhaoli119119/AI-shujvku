from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTResult, ExtractionFieldReview, Paper, PaperCorrection, WorkflowJob
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.extraction_review_service import ExtractionReviewService
from app.services.dft_review_fields import DFT_CORRECTION_FIELD_ALIASES, DFT_REVIEW_FIELD_ALIASES
from app.services.dft_review_imported import DFTImportedOpinionMixin
from app.services.dft_review_materials import DFTMaterialBindingMixin
from app.services.review_service import ReviewService
from app.utils.evidence_anchors import has_evidence_anchor
from app.utils.review_safety import is_export_eligible_extraction


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
        commit: bool = True,
    ) -> dict[str, Any]:
        if not confirm_reviewed_against_pdf:
            raise ValueError("Explicit PDF/evidence review confirmation is required.")

        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")

        snapshot = self.review_service.get_target_field_snapshot("dft_results", row)
        selected_fields = self._select_review_fields(snapshot, field_names)
        if not selected_fields:
            raise ValueError("No non-empty DFT result fields are available for verification.")

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
                    reviewer_note=reviewer_note or "Verified through the DFT candidate review workflow.",
                ),
                commit=commit,
            )
            if self._has_anchor(evidence_payload):
                self._attach_imported_evidence_payload(
                    paper_id=paper_id,
                    result_id=result_id,
                    field_names=selected_fields,
                    evidence_payload=evidence_payload,
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
                review.review_payload = {
                    "human_verification": {
                        "reviewer": reviewer or "codex_review",
                        "reviewer_note": note,
                        "decision": "verified",
                        "writes_final_truth": True,
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
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        row.candidate_status = "ML_Ready" if gate.eligible else "human_reviewed_needs_evidence"
        self.session.add(row)
        audit = AuditLog(
            paper_id=paper_id,
            action="verify_dft_result",
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
            action="verify_dft_result",
            payload={
                "dft_result_id": str(result_id),
                "field_names": selected_fields,
                "is_exportable": gate.eligible,
                "blocked_reasons": list(gate.reasons),
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
            "audit_log_id": str(audit.id),
        }

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
        commit: bool = True,
    ) -> dict[str, Any]:
        if not confirm_reject_candidate:
            raise ValueError("Explicit DFT candidate rejection confirmation is required.")

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
            commit=commit,
        )
        row.candidate_status = "Rejected"
        self.session.add(row)
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        audit = AuditLog(
            paper_id=paper_id,
            action="reject_dft_result",
            source=reviewer or "codex_review",
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "field_names": selected_fields,
                "review_ids": [str(item.id) for item in reviews],
                "blocked_reasons": list(gate.reasons),
                "review_status": gate.review_status,
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
            "audit_log_id": str(audit.id),
        }

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
                result = self.verify_result(
                    paper_id=paper_id,
                    result_id=rid,
                    confirm_reviewed_against_pdf=True,
                    reviewer=reviewer,
                    reviewer_note=reviewer_note,
                    field_names=field_names,
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

    def _add_workflow_job(self, *, paper_id: UUID, action: str, payload: dict[str, Any]) -> None:
        paper = self.session.get(Paper, paper_id)
        self.session.add(
            WorkflowJob(
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
        )
