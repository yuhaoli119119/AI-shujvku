from __future__ import annotations

from datetime import datetime, UTC
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models import ExtractionFieldReview, AuditLog, Paper
from app.utils.review_safety import verification_promotion_gate
from app.services.extraction_review_service import ExtractionReviewService
from app.services.review_target_resolver import canonical_target_type

class VerificationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def promote(
        self,
        *,
        review_id: UUID,
        target_status: str,
        reviewed_value: Any,
        actor_name: str,
        actor_source: str,
    ) -> tuple[ExtractionFieldReview, str]:
        if target_status not in {"verified", "safe_verified"}:
            raise ValueError("target_status must be 'verified' or 'safe_verified'")
        if not str(actor_name or "").strip() or not str(actor_source or "").strip():
            raise ValueError("authenticated_human_actor_required_for_final_verification")

        review = self.session.get(ExtractionFieldReview, review_id)
        if not review:
            raise LookupError("ExtractionFieldReview not found")

        paper = self.session.get(Paper, review.paper_id)
        is_metadata_only = not paper or not paper.pdf_path or paper.oa_status == "metadata_only"
        if is_metadata_only:
            raise ValueError("Cannot promote review for metadata-only paper (missing PDF).")

        # Check target exists
        try:
            ExtractionReviewService(self.session).get_target_or_raise(
                review.paper_id,
                canonical_target_type(review.target_type),
                review.target_id,
            )
        except ValueError as exc:
            raise ValueError(f"Invalid target_id: {exc}") from exc
        except LookupError as exc:
            raise LookupError(f"Target not found: {exc}") from exc

        promotion_gate = verification_promotion_gate(
            self.session,
            paper=paper,
            review=review,
        )
        if not promotion_gate.eligible:
            raise ValueError(
                "Cannot promote review: " + ",".join(promotion_gate.reasons)
            )

        before_state = {
            "reviewer_status": review.reviewer_status,
            "reviewed_value": review.reviewed_value,
            "target_resolution_status": review.target_resolution_status,
        }

        review.reviewer_status = "verified"
        review.reviewed_value = reviewed_value
        review.reviewer = actor_name
        review.review_payload = {
            "human_verification": {
                "reviewer": actor_name,
                "decision": "verified",
                "writes_final_truth": True,
                "verification_actor_type": "human",
                "source_label": actor_source,
                "identity_verified": True,
            }
        }

        after_state = {
            "reviewer_status": review.reviewer_status,
            "reviewed_value": review.reviewed_value,
            "target_resolution_status": review.target_resolution_status,
        }

        audit_id = str(uuid4())
        audit = AuditLog(
            id=UUID(audit_id),
            paper_id=review.paper_id,
            action="promote_to_verified",
            source=actor_source,
            target_type="ExtractionFieldReview",
            target_id=str(review.id),
            payload={
                "before_state": before_state,
                "after_state": after_state,
                "actor": actor_name,
                "actor_type": "human",
                "identity_verified": True,
            },
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.session.add(audit)
        
        self.session.add(review)
        self.session.commit()
        self.session.refresh(review)

        return review, audit_id
