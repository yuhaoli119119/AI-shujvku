from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.evidence import CitationAuditRequest, CitationAuditResponse, ClaimEvidence, EvidenceClaimCreate
from app.services.evidence_service import EvidenceService

router = APIRouter()


@router.get("/claims", response_model=list[ClaimEvidence])
async def list_claims(
    paper_id: UUID | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    include_derived: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> list[ClaimEvidence]:
    return EvidenceService(session).list_claims(
        paper_id=paper_id,
        target_type=target_type,
        target_id=target_id,
        include_derived=include_derived,
        limit=limit,
    )


@router.post("/claims", response_model=ClaimEvidence)
async def create_claim(
    payload: EvidenceClaimCreate,
    session: Session = Depends(get_db_session),
) -> ClaimEvidence:
    return EvidenceService(session).create_claim(payload)


@router.post("/audit", response_model=CitationAuditResponse)
async def audit_claims(
    payload: CitationAuditRequest,
    session: Session = Depends(get_db_session),
) -> CitationAuditResponse:
    return EvidenceService(session).audit_text(
        payload.text,
        paper_ids=payload.paper_ids or None,
        evidence=payload.evidence,
        min_confidence=payload.min_confidence,
    )

