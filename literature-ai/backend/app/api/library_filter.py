from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.services.citation_eligibility_service import CitationEligibilityService, CitationEligibilityUpdate
from app.services.paper_filter_service import PaperFilterCriteria, PaperFilterService

router = APIRouter()


class CitationEligibilityPayload(BaseModel):
    included_for_writing: bool | None = None
    exclude_from_citation: bool | None = None
    exclude_reason: str | None = None
    citation_priority: str | None = Field(default=None, pattern="^(high|medium|low|exclude)$")
    user_note: str | None = None


class BulkCitationEligibilityPayload(CitationEligibilityPayload):
    paper_ids: list[UUID] = Field(default_factory=list, min_length=1)


def _split_terms(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@router.get("/filter")
async def filter_papers(
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    journal_includes: str | None = Query(default=None, description="Comma-separated journal include terms"),
    journal_excludes: str | None = Query(default=None, description="Comma-separated journal exclude terms"),
    impact_factor_min: float | None = Query(default=None),
    impact_factor_max: float | None = Query(default=None),
    keyword: str | None = Query(default=None, description="Keyword in title or abstract"),
    has_pdf: bool | None = Query(default=None),
    has_parsed_text: bool | None = Query(default=None),
    has_extraction_output: bool | None = Query(default=None),
    has_verified_evidence: bool | None = Query(default=None),
    has_safe_verified_evidence: bool | None = Query(default=None),
    exclude_from_citation: bool | None = Query(default=None),
    citation_priority: str | None = Query(default=None, pattern="^(high|medium|low|exclude)$"),
    needs_metadata: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict:
    criteria = PaperFilterCriteria(
        year_min=year_min,
        year_max=year_max,
        journal_includes=_split_terms(journal_includes),
        journal_excludes=_split_terms(journal_excludes),
        impact_factor_min=impact_factor_min,
        impact_factor_max=impact_factor_max,
        keyword=keyword,
        has_pdf=has_pdf,
        has_parsed_text=has_parsed_text,
        has_extraction_output=has_extraction_output,
        has_verified_evidence=has_verified_evidence,
        has_safe_verified_evidence=has_safe_verified_evidence,
        exclude_from_citation=exclude_from_citation,
        citation_priority=citation_priority,
        needs_metadata=needs_metadata,
        limit=limit,
        offset=offset,
    )
    rows = PaperFilterService(session).filter(criteria)
    return {
        "total": len(rows),
        "items": [row.__dict__ for row in rows],
        "safety": {
            "read_only": True,
            "deletes_papers": False,
            "modifies_extraction": False,
            "modifies_review_verified": False,
            "unlocks_export_or_writing": False,
        },
    }


@router.post("/{paper_id}/citation-eligibility")
async def update_citation_eligibility(
    paper_id: UUID,
    payload: CitationEligibilityPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    service = CitationEligibilityService(session)
    try:
        row = service.update(paper_id, CitationEligibilityUpdate(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from exc
    return _eligibility_response(row)


@router.post("/citation-eligibility/bulk")
async def bulk_update_citation_eligibility(
    payload: BulkCitationEligibilityPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    update = CitationEligibilityUpdate(
        **payload.model_dump(exclude={"paper_ids"}),
    )
    try:
        rows = CitationEligibilityService(session).bulk_update(payload.paper_ids, update)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 400, detail=str(exc)) from exc
    return {
        "updated_count": len(rows),
        "items": [_eligibility_response(row) for row in rows],
        "safety": {
            "deletes_papers": False,
            "modifies_extraction": False,
            "modifies_review_verified": False,
            "unlocks_export_or_writing": False,
        },
    }


def _eligibility_response(row) -> dict:
    return {
        "paper_id": row.paper_id,
        "included_for_writing": row.included_for_writing,
        "exclude_from_citation": row.exclude_from_citation,
        "exclude_reason": row.exclude_reason,
        "citation_priority": row.citation_priority,
        "user_note": row.user_note,
        "updated_at": row.updated_at,
    }
