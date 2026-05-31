from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.services.verification_service import VerificationService

router = APIRouter()

class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_status: str
    reviewed_value: Any
    reviewer: str | None = None
    confirm_human_review: bool

@router.post("/{review_id}/promote")
async def promote_verification(
    review_id: UUID,
    payload: PromotionRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if not payload.confirm_human_review:
        raise HTTPException(status_code=400, detail="Explicit human confirmation is required.")
        
    try:
        review, audit_id = VerificationService(session).promote(
            review_id=review_id,
            target_status=payload.target_status,
            reviewed_value=payload.reviewed_value,
            reviewer=payload.reviewer,
        )

        return {
            "id": review.id,
            "reviewer_status": review.reviewer_status,
            "reviewed_value": review.reviewed_value,
            "audit_log_id": audit_id,
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
