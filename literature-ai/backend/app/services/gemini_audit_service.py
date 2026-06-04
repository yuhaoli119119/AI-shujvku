from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, Paper, PaperCorrection
from app.schemas.extraction import ExtractionFieldReviewSaveItem
from app.services.extraction_review_service import ExtractionReviewService
from app.services.review_service import ReviewService
from app.utils.workbench_status import (
    GEMINI_AUDIT_DECISIONS,
    normalize_choice,
    workflow_status_after_gemini,
)


REVIEW_STATUS_BY_DECISION = {
    "PASS": "gemini_pass",
    "REVISE": "gemini_revise",
    "FLAG": "gemini_flagged",
    "INSUFFICIENT": "evidence_insufficient",
}


class GeminiAuditService:
    """Record Gemini audit decisions without promoting data to final truth."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.reviews = ExtractionReviewService(session)

    def submit(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: UUID | None,
        decision: str,
        reviewer: str,
        reviewer_note: str | None = None,
        confidence: float | None = None,
        field_names: list[str] | None = None,
        field_name: str | None = None,
        proposed_value: Any = None,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        normalized_decision = normalize_choice(decision, GEMINI_AUDIT_DECISIONS, "INSUFFICIENT")
        review_rows = []
        correction_payload = None
        if target_id is not None and str(target_type or "").strip().lower() not in {"paper", "papers"}:
            review_rows = self._write_target_reviews(
                paper_id=paper_id,
                target_type=target_type,
                target_id=target_id,
                decision=normalized_decision,
                reviewer=reviewer,
                reviewer_note=reviewer_note,
                field_names=field_names or [],
            )
            self._mark_target_candidate_status(target_type, target_id, normalized_decision)
            if normalized_decision == "REVISE" and field_name:
                correction_payload = self._write_correction(
                    paper_id=paper_id,
                    target_type=target_type,
                    target_id=target_id,
                    field_name=field_name,
                    proposed_value=proposed_value,
                    reason=reviewer_note or "Gemini suggested a revision after checking the evidence package.",
                    reviewer=reviewer,
                    evidence_payload=evidence_payload,
                )
        paper.workflow_status = workflow_status_after_gemini(normalized_decision)
        audit = AuditLog(
            paper_id=paper_id,
            action="gemini_audit",
            source=reviewer or "gemini_auditor",
            target_type=target_type,
            target_id=str(target_id) if target_id else str(paper_id),
            payload={
                "decision": normalized_decision,
                "reviewer_note": reviewer_note,
                "confidence": confidence,
                "field_names": field_names or [],
                "field_name": field_name,
                "proposed_value": proposed_value,
                "evidence_payload": evidence_payload,
                "review_status_written": REVIEW_STATUS_BY_DECISION[normalized_decision],
                "human_confirmation_required": True,
            },
        )
        self.session.add(paper)
        self.session.add(audit)
        self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "workflow_status": paper.workflow_status,
            "decision": normalized_decision,
            "reviewer": reviewer,
            "review_count": len(review_rows),
            "reviews": [item.model_dump(mode="json") for item in review_rows],
            "correction": correction_payload,
            "audit_log_id": str(audit.id),
            "safety": {
                "gemini_marks_final_verified": False,
                "requires_human_confirmation": True,
                "requires_human_confirmation_for_final_library": True,
                "writes_export_unlock": False,
            },
        }

    def human_confirm(
        self,
        *,
        paper_id: UUID,
        target_status: str,
        reviewer: str,
        note: str | None,
        confirm_human_review: bool,
    ) -> dict[str, Any]:
        if not confirm_human_review:
            raise ValueError("Explicit human confirmation is required.")
        if target_status not in {"Human_Confirmed", "ML_Ready", "Citation_Ready"}:
            raise ValueError("Unsupported target_status.")
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        before = paper.workflow_status
        paper.workflow_status = target_status
        audit = AuditLog(
            paper_id=paper_id,
            action="human_confirm_workbench_status",
            source=reviewer or "human",
            target_type="paper",
            target_id=str(paper_id),
            payload={"before_status": before, "after_status": target_status, "note": note},
        )
        self.session.add(paper)
        self.session.add(audit)
        self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "before_status": before,
            "workflow_status": paper.workflow_status,
            "audit_log_id": str(audit.id),
            "note": note,
        }

    def _write_target_reviews(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: UUID,
        decision: str,
        reviewer: str,
        reviewer_note: str | None,
        field_names: list[str],
    ):
        canonical = self.reviews.canonical_target_type(target_type)
        target = self.reviews.get_target_or_raise(paper_id, canonical, str(target_id))
        snapshot = self.reviews.get_target_field_snapshot(canonical, target)
        selected = field_names or [
            field_name
            for field_name, item in snapshot.items()
            if not self._is_blank(item.get("value"))
        ]
        review_status = REVIEW_STATUS_BY_DECISION[decision]
        return self.reviews.save_reviews(
            paper_id,
            [
                ExtractionFieldReviewSaveItem(
                    target_type=canonical,
                    target_id=str(target_id),
                    field_name=name,
                    original_value=snapshot[name]["value"],
                    reviewed_value=snapshot[name]["value"],
                    unit=snapshot[name]["unit"],
                    evidence_text=snapshot[name]["evidence_text"],
                    reviewer_status=review_status,
                    reviewer=reviewer,
                    reviewer_note=reviewer_note or f"Gemini audit decision: {decision}",
                )
                for name in selected
                if name in snapshot
            ],
        )

    def _mark_target_candidate_status(self, target_type: str, target_id: UUID, decision: str) -> None:
        normalized = str(target_type or "").strip().lower()
        if normalized not in {"dft_results", "dft_result", "dftresult"}:
            return
        row = self.session.get(DFTResult, UUID(str(target_id)))
        if row is not None:
            row.candidate_status = workflow_status_after_gemini(decision)
            self.session.add(row)

    def _write_correction(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: UUID,
        field_name: str,
        proposed_value: Any,
        reason: str,
        reviewer: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any]:
        canonical_field = str(field_name or "").strip()
        if str(target_type).strip().lower() in {"dft_results", "dft_result", "dftresult"}:
            aliases = {
                "energy_type": "property_type",
                "property_type": "property_type",
                "energy": "property_type",
                "value": "value",
                "unit": "unit",
                "adsorbate": "adsorbate",
                "reaction_step": "reaction_step",
                "source_section": "source_section",
                "source_figure": "source_figure",
                "evidence_text": "evidence_text",
                "confidence": "confidence",
            }
            canonical_field = aliases.get(canonical_field, canonical_field)
            if canonical_field not in ReviewService.ALLOWED_DFT_RESULT_FIELDS:
                raise ValueError(f"Unsupported DFT correction field: {field_name}")
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name=str(target_type),
            target_path=f"{target_type}:{target_id}:{canonical_field}",
            operation="replace",
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        return {
            "id": str(correction.id),
            "target_path": correction.target_path,
            "status": correction.status,
            "proposed_value": correction.proposed_value,
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
