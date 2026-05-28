from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from uuid import UUID

from app.db.session import get_db_session
from app.services.writing_citation_insertion_service import (
    CitationInsertionDraftRequest,
    WritingCitationInsertionService,
)
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


class CitationInsertionDraftPayload(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    selected_paper_id: UUID
    citation_marker: str | None = None
    insertion_mode: str = Field(default="parenthetical", pattern="^(parenthetical|narrative|comment_only)$")
    citation_style: str = Field(default="draft_author_year", pattern="^(draft_author_year|placeholder)$")
    candidate_evidence_status: str | None = None
    candidate_can_be_used_as_confirmed_citation: bool | None = None
    candidate_requires_human_verification: bool | None = None
    supporting_snippet: str | None = None
    user_note: str | None = None


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


@router.post("/citation-insertion-draft")
async def citation_insertion_draft(
    payload: CitationInsertionDraftPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    result = WritingCitationInsertionService(session).draft(
        CitationInsertionDraftRequest(
            text=payload.text,
            selected_paper_id=payload.selected_paper_id,
            citation_marker=payload.citation_marker,
            insertion_mode=payload.insertion_mode,
            citation_style=payload.citation_style,
            candidate_evidence_status=payload.candidate_evidence_status,
            candidate_can_be_used_as_confirmed_citation=payload.candidate_can_be_used_as_confirmed_citation,
            candidate_requires_human_verification=payload.candidate_requires_human_verification,
            supporting_snippet=payload.supporting_snippet,
            user_note=payload.user_note,
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return result
