from __future__ import annotations

from datetime import datetime, UTC
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models import ExtractionFieldReview, AuditLog, Paper
from app.utils.review_safety import has_required_evidence_reference, has_required_evidence_text
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
        reviewer: str | None = None,
    ) -> tuple[ExtractionFieldReview, str]:
        if target_status not in {"verified", "safe_verified"}:
            raise ValueError("target_status must be 'verified' or 'safe_verified'")

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

        # Basic constraints for any verification
        if target_status in {"verified", "safe_verified"}:
            has_ref = has_required_evidence_reference(
                self.session,
                paper_id=review.paper_id,
                target_type=review.target_type,
                target_id=review.target_id,
            )
            has_text = has_required_evidence_text(review)
            
            # Strict checks apply to ALL verification promotions to prevent bypassing the export gate
            if not has_ref or not has_text:
                raise ValueError("Cannot promote review: missing explicit evidence text or exact locator.")
            
            if review.target_resolution_status not in {"active", "remapped"}:
                raise ValueError("Cannot promote review: target is stale or ambiguous.")

        before_state = {
            "reviewer_status": review.reviewer_status,
            "reviewed_value": review.reviewed_value,
            "target_resolution_status": review.target_resolution_status,
        }

        review.reviewer_status = "verified"
        review.reviewed_value = reviewed_value
        review.reviewer = reviewer

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
            source=reviewer or "system",
            target_type="ExtractionFieldReview",
            target_id=str(review.id),
            payload={
                "before_state": before_state,
                "after_state": after_state,
            },
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.session.add(audit)
        
        self.session.add(review)
        self.session.commit()
        self.session.refresh(review)

        return review, audit_id
