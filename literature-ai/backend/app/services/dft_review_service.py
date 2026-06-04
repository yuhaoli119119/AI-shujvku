from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, PaperCorrection
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.extraction_review_service import ExtractionReviewService
from app.services.review_service import ReviewService
from app.utils.review_safety import is_export_eligible_extraction


DFT_REVIEW_FIELD_ALIASES = {
    "property_type": "energy_type",
    "energy": "energy_type",
    "energy_type": "energy_type",
    "unit": "value",
    "energy_value": "value",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
    "catalyst": "catalyst",
    "value": "value",
}

DFT_CORRECTION_FIELD_ALIASES = {
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

        reviews = self.review_service.mark_verified(
            paper_id,
            ExtractionReviewMarkVerifiedRequest(
                target_type="dft_results",
                target_id=str(result_id),
                field_names=selected_fields,
                reviewer=reviewer or "codex_review",
                reviewer_note=reviewer_note or "Verified through the DFT candidate review workflow.",
            ),
        )
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
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

    def reject_result(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        confirm_reject_candidate: bool,
        reviewer: str | None = None,
        reviewer_note: str | None = None,
        field_names: list[str] | None = None,
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
        self.session.commit()
        self.session.refresh(correction)
        return self._correction_payload(correction)

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
