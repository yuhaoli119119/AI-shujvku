from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTResult, ExtractionFieldReview, Paper, PaperCorrection, WorkflowJob
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.extraction_review_service import ExtractionReviewService
from app.services.review_service import ReviewService
from app.utils.evidence_anchors import has_evidence_anchor
from app.utils.review_safety import is_export_eligible_extraction


DFT_REVIEW_FIELD_ALIASES = {
    "property_type": "energy_type",
    "energy": "energy_type",
    "energy_type": "energy_type",
    "unit": "unit",
    "energy_value": "value",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
    "catalyst": "catalyst",
    "value": "value",
}

DFT_CORRECTION_FIELD_ALIASES = {
    "catalyst": "catalyst_sample_id",
    "catalyst_id": "catalyst_sample_id",
    "catalyst_sample": "catalyst_sample_id",
    "catalyst_sample_id": "catalyst_sample_id",
    "material_binding": "catalyst_sample_id",
    "structure_binding": "catalyst_sample_id",
    "energy_type": "property_type",
    "property_type": "property_type",
    "energy": "property_type",
    "value": "value",
    "energy_value": "value",
    "unit": "unit",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
    "source_section": "source_section",
    "source_figure": "source_figure",
    "evidence_text": "evidence_text",
    "confidence": "confidence",
}


class DFTResultReviewService:
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
                review = self.review_service._get_or_create_review(
                    paper_id,
                    "dft_results",
                    str(result_id),
                    field_name,
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
        self.session.commit()
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
        self.session.commit()
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

    def apply_imported_opinion(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        opinion: dict[str, Any],
        reviewer: str | None = None,
        expected_row_state: dict[str, Any] | None = None,
        expected_write_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")
        if not isinstance(opinion, dict):
            raise ValueError("A structured imported opinion payload is required.")
        self._guard_expected_row_state(row=row, expected_row_state=expected_row_state)

        reviewer_name = reviewer or "codex_review"
        decision = str(opinion.get("decision") or opinion.get("status") or "").strip().upper()
        if not decision:
            raise ValueError("Imported opinion is missing a decision.")
        if decision in {"NEEDS_HUMAN", "NEEDS_MANUAL", "MANUAL"}:
            raise ValueError("NEEDS_HUMAN opinions cannot be auto-applied to DFT results.")

        evidence_payload = self._imported_evidence_payload(opinion)
        reason = str(opinion.get("reason") or "").strip() or "Applied imported AI opinion from the DFT review queue."
        source_label = str(opinion.get("source_label") or opinion.get("source") or "imported_ai").strip()

        if decision in self.IMPORTED_NEGATIVE_DECISIONS:
            rejected = self.reject_result(
                paper_id=paper_id,
                result_id=result_id,
                confirm_reject_candidate=True,
                reviewer=reviewer_name,
                reviewer_note=f"Applied imported AI rejection from {source_label}. {reason}".strip(),
            )
            return {
                "paper_id": str(paper_id),
                "dft_result_id": str(result_id),
                "action": "reject",
                "source_label": source_label,
                "applied_corrections": [],
                "review_result": rejected,
            }

        applied_corrections: list[dict[str, Any]] = []
        corrected_value = opinion.get("corrected_value")
        material_identity = self._first_text(
            corrected_value.get("material_identity") if isinstance(corrected_value, dict) else None,
            corrected_value.get("material") if isinstance(corrected_value, dict) else None,
            corrected_value.get("catalyst") if isinstance(corrected_value, dict) else None,
            opinion.get("normalized_material"),
            opinion.get("normalized_material_or_catalyst"),
        )

        if material_identity or row.catalyst_sample_id:
            binding = self._apply_material_binding(
                row=row,
                material_identity=material_identity,
                reviewer=reviewer_name,
                reason=reason,
                evidence_payload=evidence_payload,
            )
            if binding:
                applied_corrections.append(binding)

        for field_name, proposed_value in self._imported_field_updates(row=row, opinion=opinion).items():
            applied_corrections.append(
                self._approve_dft_correction(
                    paper_id=paper_id,
                    result_id=result_id,
                    field_name=field_name,
                    proposed_value=proposed_value,
                    reviewer=reviewer_name,
                    reason=reason,
                    evidence_payload=evidence_payload,
                )
            )

        verify_field_names = self._imported_verify_field_names(
            row=row,
            opinion=opinion,
            applied_corrections=applied_corrections,
        )
        verified = self.verify_result(
            paper_id=paper_id,
            result_id=result_id,
            confirm_reviewed_against_pdf=True,
            reviewer=reviewer_name,
            reviewer_note=f"Applied imported AI opinion from {source_label}. {reason}".strip(),
            field_names=verify_field_names or None,
            expected_write_versions=expected_write_versions or {},
            evidence_payload=evidence_payload,
        )
        audit = AuditLog(
            paper_id=paper_id,
            action="apply_imported_dft_opinion",
            source=reviewer_name,
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "source_label": source_label,
                "decision": decision,
                "applied_correction_fields": [item.get("field_name") for item in applied_corrections],
                "verified_field_names": verify_field_names,
                "expected_row_state": expected_row_state or {},
            },
        )
        self.session.add(audit)
        self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "action": "verify",
            "source_label": source_label,
            "applied_corrections": applied_corrections,
            "review_result": verified,
            "audit_log_id": str(audit.id),
        }

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

    def _apply_material_binding(
        self,
        *,
        row: DFTResult,
        material_identity: str | None,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any] | None:
        if not self._has_anchor(evidence_payload):
            raise ValueError("Applying imported material binding requires a PDF evidence anchor.")
        target_sample_id = str(row.catalyst_sample_id) if row.catalyst_sample_id else None
        if not material_identity and target_sample_id:
            return None
        if material_identity:
            if self._existing_material_binding_matches(row=row, material_identity=material_identity):
                return None
            target_sample_id = self._resolve_or_create_catalyst_sample_id(
                paper_id=row.paper_id,
                material_identity=material_identity,
                reviewer=reviewer,
                reason=reason,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
        if not target_sample_id:
            return None
        return self._approve_dft_correction(
            paper_id=row.paper_id,
            result_id=row.id,
            field_name="catalyst_sample_id",
            proposed_value=target_sample_id,
            reviewer=reviewer,
            reason=reason,
            evidence_payload=evidence_payload,
            write_lock_tokens=write_lock_tokens,
        )

    def _existing_material_binding_matches(self, *, row: DFTResult, material_identity: str) -> bool:
        if not row.catalyst_sample_id:
            return False
        sample = self.session.get(CatalystSample, row.catalyst_sample_id)
        if sample is None:
            return False
        current_name = self._normalized_text(sample.name)
        expected_name = self._normalized_text(material_identity)
        if not current_name or not expected_name:
            return False
        return current_name == expected_name or current_name in expected_name or expected_name in current_name

    def _resolve_or_create_catalyst_sample_id(
        self,
        *,
        paper_id: UUID,
        material_identity: str,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> str:
        first_anchor = self._first_anchor(evidence_payload)
        proposed_value = {
            "name": material_identity,
            "structure_name": material_identity,
            "evidence_strength": self._first_text(
                first_anchor.get("quoted_text") if first_anchor else None,
                reason,
            ),
        }
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="catalyst_samples",
            target_path="catalyst_samples:new:create",
            operation="create",
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        approved = ReviewService(self.session).approve_correction(
            correction.id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
        )
        self.session.flush()
        payload = approved.evidence_payload if isinstance(approved.evidence_payload, dict) else {}
        resolution = payload.get("sample_resolution") if isinstance(payload, dict) else {}
        sample_id = resolution.get("catalyst_sample_id") if isinstance(resolution, dict) else None
        if not sample_id:
            raise ValueError("Imported material identity could not be resolved to a catalyst sample.")
        return str(sample_id)

    def _approve_dft_correction(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        field_name: str,
        proposed_value: Any,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        canonical_field = DFT_CORRECTION_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="dft_results",
            target_path=f"dft_results:{result_id}:{canonical_field}",
            operation="replace",
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        approved = ReviewService(self.session).approve_correction(
            correction.id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
        )
        self.session.flush()
        return {
            "correction_id": str(approved.id),
            "field_name": canonical_field,
            "proposed_value": proposed_value,
            "status": approved.status,
        }

    def _imported_field_updates(self, *, row: DFTResult, opinion: dict[str, Any]) -> dict[str, Any]:
        corrected_value = opinion.get("corrected_value")
        updates: dict[str, Any] = {}
        if isinstance(corrected_value, dict):
            property_candidates = ("property_type", "property", "energy_type")
            property_present = any(candidate in corrected_value for candidate in property_candidates)
            property_value = (
                self._first_text(*(corrected_value.get(candidate) for candidate in property_candidates))
                if property_present
                else self._first_text(opinion.get("normalized_energy_type"))
            )
            if property_present and self._normalized_text(property_value) != self._normalized_text(row.property_type):
                updates["property_type"] = property_value

            value_present = "value" in corrected_value
            value = corrected_value.get("value")
            unit_present = "unit" in corrected_value
            unit = corrected_value.get("unit") if unit_present else None
            normalized_value, normalized_unit = self._normalize_imported_dft_value(
                value=value,
                unit=unit,
                property_type=property_value or row.property_type,
            )
            if value_present:
                numeric_value = normalized_value
                if self._numeric_key(numeric_value) != self._numeric_key(row.value):
                    updates["value"] = numeric_value

            if unit_present and self._normalized_text(normalized_unit) != self._normalized_text(row.unit):
                updates["unit"] = normalized_unit

            adsorbate_present = "adsorbate" in corrected_value
            adsorbate = self._first_text(corrected_value.get("adsorbate")) if adsorbate_present else None
            if adsorbate_present and self._normalized_text(adsorbate) != self._normalized_text(row.adsorbate):
                updates["adsorbate"] = adsorbate

            reaction_step_present = "reaction_step" in corrected_value
            reaction_step = self._first_text(corrected_value.get("reaction_step")) if reaction_step_present else None
            if reaction_step_present and self._normalized_text(reaction_step) != self._normalized_text(row.reaction_step):
                updates["reaction_step"] = reaction_step
            return updates

        field_name = DFT_CORRECTION_FIELD_ALIASES.get(
            str(opinion.get("field_name") or "").strip(),
            str(opinion.get("field_name") or "").strip(),
        )
        if field_name in ReviewService.ALLOWED_DFT_RESULT_FIELDS and corrected_value not in (None, ""):
            current_value = getattr(row, field_name, None)
            if field_name == "value":
                numeric_value, normalized_unit = self._normalize_imported_dft_value(
                    value=corrected_value,
                    unit=row.unit,
                    property_type=row.property_type,
                )
                if numeric_value is not None and self._numeric_key(numeric_value) != self._numeric_key(current_value):
                    updates["value"] = numeric_value
                if normalized_unit and self._normalized_text(normalized_unit) != self._normalized_text(row.unit):
                    updates["unit"] = normalized_unit
            elif self._normalized_text(corrected_value) != self._normalized_text(current_value):
                updates[field_name] = corrected_value
        return updates

    def _imported_verify_field_names(
        self,
        *,
        row: DFTResult,
        opinion: dict[str, Any],
        applied_corrections: list[dict[str, Any]],
    ) -> list[str]:
        preferred: list[str] = []
        corrected_fields = {str(item.get("field_name") or "").strip() for item in applied_corrections}
        if corrected_fields:
            for field_name in corrected_fields:
                mapped = self._review_field_name_from_correction_field(field_name)
                if mapped and mapped not in preferred:
                    preferred.append(mapped)
            return preferred

        corrected_value = opinion.get("corrected_value")
        if row.catalyst_sample_id:
            preferred.append("catalyst")
        if isinstance(corrected_value, dict):
            if any(key in corrected_value for key in ("value", "unit")):
                preferred.append("value")
            if "adsorbate" in corrected_value:
                preferred.append("adsorbate")
            if any(key in corrected_value for key in ("property_type", "property", "energy_type")):
                preferred.append("energy_type")
            if "reaction_step" in corrected_value:
                preferred.append("reaction_step")
        field_name = DFT_CORRECTION_FIELD_ALIASES.get(
            str(opinion.get("field_name") or "").strip(),
            str(opinion.get("field_name") or "").strip(),
        )
        mapped = self._review_field_name_from_correction_field(field_name)
        if mapped and mapped not in preferred:
            preferred.append(mapped)
        return preferred

    @staticmethod
    def _review_field_name_from_correction_field(field_name: str) -> str | None:
        normalized = str(field_name or "").strip()
        if normalized == "property_type":
            return "energy_type"
        if normalized == "catalyst_sample_id":
            return "catalyst"
        if normalized in {"value", "adsorbate", "reaction_step"}:
            return normalized
        return None

    def _guard_expected_row_state(
        self,
        *,
        row: DFTResult,
        expected_row_state: dict[str, Any] | None,
    ) -> None:
        if expected_row_state is None:
            return
        if not isinstance(expected_row_state, dict):
            raise ValueError("expected_row_state must be an object.")
        for field_name, expected_value in expected_row_state.items():
            current_value = self._dft_row_state_value(row, field_name)
            if field_name == "value":
                if self._numeric_key(current_value) != self._numeric_key(expected_value):
                    raise ValueError("write_conflict:dft_result_state_stale")
                continue
            if self._normalized_text(current_value) != self._normalized_text(expected_value):
                raise ValueError("write_conflict:dft_result_state_stale")

    @staticmethod
    def _dft_row_state_value(row: DFTResult, field_name: str) -> Any:
        normalized = str(field_name or "").strip()
        if normalized == "catalyst_sample_id":
            return str(row.catalyst_sample_id) if row.catalyst_sample_id else None
        if normalized in {
            "candidate_status",
            "property_type",
            "adsorbate",
            "reaction_step",
            "value",
            "unit",
            "source_section",
            "source_figure",
        }:
            return getattr(row, normalized, None)
        raise ValueError(f"Unsupported expected_row_state field: {field_name}")

    @staticmethod
    def _imported_evidence_payload(opinion: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
        payload = opinion.get("evidence_payload")
        if isinstance(payload, (dict, list)) and has_evidence_anchor(payload):
            return payload
        location = opinion.get("evidence_location")
        if isinstance(location, dict):
            return location
        return payload if isinstance(payload, (dict, list)) else None

    @staticmethod
    def _first_anchor(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _numeric_key(value: Any) -> str:
        if value in (None, ""):
            return ""
        try:
            return f"{float(value):.8g}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _normalize_imported_dft_value(
        *,
        value: Any,
        unit: str | None,
        property_type: Any = None,
    ) -> tuple[float | None, str | None]:
        if value in (None, ""):
            return None, unit
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return None, unit
        unit_text = str(unit or "").strip()
        unit_key = unit_text.lower().replace(" ", "")
        if unit_key in {"mev"}:
            return numeric_value / 1000.0, "eV"
        if unit_key in {"ev"}:
            return numeric_value, "eV"
        if "gpu" in unit_key:
            ascii_key = "".join(ch for ch in unit_key if ch.isascii())
            if any(marker in ascii_key for marker in ("10^3", "x10^3", "103")) or (
                ascii_key.startswith("10") and ascii_key != "gpu"
            ):
                return numeric_value * 1000.0, "GPU"
            return numeric_value, "GPU"
        return numeric_value, unit_text or unit

    def _select_review_fields(
        self,
        snapshot: dict[str, dict[str, Any]],
        field_names: list[str] | None,
    ) -> list[str]:
        if field_names:
            selected = []
            for field_name in field_names:
                canonical = DFT_REVIEW_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
                if canonical and canonical not in selected:
                    selected.append(canonical)
            return selected

        selected = ["value"] if not self._is_blank(snapshot.get("value", {}).get("value")) else []
        for field_name in ["adsorbate", "energy_type", "reaction_step"]:
            value = snapshot.get(field_name, {}).get("value")
            if not self._is_blank(value) and field_name not in selected:
                selected.append(field_name)
        return selected

    @staticmethod
    def _gate_payload(row: DFTResult, gate: Any) -> dict[str, Any]:
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

    @staticmethod
    def _correction_payload(correction: PaperCorrection) -> dict[str, Any]:
        return {
            "id": str(correction.id),
            "paper_id": str(correction.paper_id),
            "source": correction.source,
            "field_name": correction.field_name,
            "target_path": correction.target_path,
            "operation": correction.operation,
            "proposed_value": correction.proposed_value,
            "reason": correction.reason,
            "evidence_payload": correction.evidence_payload,
            "status": correction.status,
            "reviewed_at": correction.reviewed_at.isoformat() if correction.reviewed_at else None,
            "reviewed_by": correction.reviewed_by,
            "created_at": correction.created_at.isoformat() if correction.created_at else None,
        }

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict)):
            return len(value) == 0
        return False
