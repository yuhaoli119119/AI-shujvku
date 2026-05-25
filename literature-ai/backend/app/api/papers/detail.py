from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.api import ExtractionRunResponse, PaperDetailResponse
from app.schemas.evidence import EvidenceLocatorResponse
from app.services.paper_query import PaperQueryService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.paper_reprocessing import PaperReprocessingService

router = APIRouter()


@router.get("/{paper_id}", response_model=PaperDetailResponse)
async def get_paper(paper_id: UUID, session: Session = Depends(get_db_session)) -> PaperDetailResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    return detail


@router.delete("/{paper_id}")
async def delete_paper(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    session.delete(paper)
    session.commit()
    return {"status": "deleted", "paper_id": str(paper_id)}


@router.post("/{paper_id}/extract", response_model=ExtractionRunResponse)
async def rerun_stage2_extraction(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    settings = get_settings()
    summary = PaperReprocessingService(session=session, settings=settings).rerun_stage2(paper_id)
    return ExtractionRunResponse(
        paper_id=paper_id,
        status="completed",
        dft_settings=summary.get("dft_settings", 0),
        catalyst_samples=summary.get("catalyst_samples", 0),
        dft_results=summary.get("dft_results", 0),
        electrochemical_performance=summary.get("electrochemical_performance", 0),
        mechanism_claims=summary.get("mechanism_claims", 0),
        writing_cards=summary.get("writing_cards", 0),
    )


@router.get("/{paper_id}/evidence/locators", response_model=list[EvidenceLocatorResponse])
async def get_paper_evidence_locators(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[EvidenceLocatorResponse]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return EvidenceLocatorService(session).list_locators_for_paper(paper_id)
