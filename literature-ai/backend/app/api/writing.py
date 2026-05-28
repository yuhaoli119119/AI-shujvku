from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.services.writing_citation_candidate_service import (
    CitationCandidateFilters,
    CitationCandidateRequest,
    WritingCitationCandidateService,
)

router = APIRouter()


class CitationCandidateFiltersPayload(BaseModel):
    year_min: int | None = None
    year_max: int | None = None
    impact_factor_min: float | None = None
    impact_factor_max: float | None = None
    journal_include: list[str] = Field(default_factory=list)
    journal_exclude: list[str] = Field(default_factory=list)
    needs_metadata: bool | None = None
    has_pdf: bool | None = None
    has_parsed_text: bool | None = None
    has_extraction_output: bool | None = None
    has_verified_evidence: bool | None = None
    has_safe_verified_evidence: bool | None = None
    citation_priority: str | None = Field(default=None, pattern="^(high|medium|low|exclude)$")


class CitationCandidatePayload(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    max_candidates: int = Field(default=10, ge=1, le=50)
    filters: CitationCandidateFiltersPayload = Field(default_factory=CitationCandidateFiltersPayload)
    include_unverified_suggestions: bool = True
    include_pending_review: bool = True


@router.post("/citation-candidates")
async def citation_candidates(
    payload: CitationCandidatePayload,
    session: Session = Depends(get_db_session),
) -> dict:
    filters = CitationCandidateFilters(
        **payload.filters.model_dump(exclude={"journal_include", "journal_exclude"}),
        journal_include=tuple(item.strip() for item in payload.filters.journal_include if item.strip()),
        journal_exclude=tuple(item.strip() for item in payload.filters.journal_exclude if item.strip()),
    )
    request = CitationCandidateRequest(
        text=payload.text,
        max_candidates=payload.max_candidates,
        filters=filters,
        include_unverified_suggestions=payload.include_unverified_suggestions,
        include_pending_review=payload.include_pending_review,
    )
    try:
        return WritingCitationCandidateService(session).recommend(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
