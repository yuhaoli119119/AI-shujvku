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


from sqlalchemy import select
from app.db.models import Paper, PaperImpactMetadata, PaperCitationEligibility

@router.get("/{paper_id}/citation-metadata-preview")
async def citation_metadata_preview(
    paper_id: UUID,
    session: Session = Depends(get_db_session)
) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    impact = session.scalar(select(PaperImpactMetadata).where(PaperImpactMetadata.paper_id == paper_id))
    eligibility = session.scalar(select(PaperCitationEligibility).where(PaperCitationEligibility.paper_id == paper_id))

    missing_warnings = []
    required_fields = {
        "title": paper.title,
        "authors": paper.authors if paper.authors else None,
        "journal": paper.journal,
        "year": paper.year,
        "DOI": paper.doi,
        "volume": None,
        "issue": None,
        "pages": None,
        "publisher": None,
        "impact factor": impact.impact_factor if impact else None
    }
    
    for field, val in required_fields.items():
        if not val:
            missing_warnings.append(f"Missing {field}")

    author_str = " and ".join(paper.authors) if paper.authors else "Unknown"
    bibtex_draft = f"""@article{{draft_{str(paper.id)[:8]},
  title={{{paper.title or 'Unknown'}}},
  author={{{author_str}}},
  journal={{{paper.journal or 'Unknown'}}},
  year={{{paper.year or 'Unknown'}}},
  doi={{{paper.doi or 'Unknown'}}},
  note={{DRAFT METADATA ONLY}}
}}"""

    csl_json_draft = {
        "id": f"draft_{str(paper.id)[:8]}",
        "type": "article-journal",
        "title": paper.title or "Unknown",
        "author": [{"family": a} for a in (paper.authors or ["Unknown"])],
        "container-title": paper.journal or "Unknown",
        "issued": {"date-parts": [[paper.year]]} if paper.year else {},
        "DOI": paper.doi or "Unknown",
        "note": "DRAFT METADATA ONLY"
    }

    exclude_from_citation = eligibility.exclude_from_citation if eligibility else False
    safety_status = "excluded" if exclude_from_citation else "eligible_for_draft"

    return {
        "paper_id": str(paper_id),
        "warning_banner": "DRAFT METADATA ONLY - Do not use as final citation",
        "metadata_preview": required_fields,
        "bibtex_draft": bibtex_draft,
        "csl_json_draft": csl_json_draft,
        "missing_metadata_warnings": missing_warnings,
        "citation_safety_status": safety_status,
        "evidence_status": "metadata_only",
        "safety": {
            "read_only": True,
            "modifies_db": False
        }
    }


@router.get("/metadata-diagnostics")
async def metadata_diagnostics(
    session: Session = Depends(get_db_session)
) -> dict:
    papers = session.scalars(select(Paper)).all()
    impact_map = {
        row.paper_id: row 
        for row in session.scalars(select(PaperImpactMetadata)).all()
    }
    
    items = []
    for paper in papers:
        impact = impact_map.get(paper.id)
        
        missing = []
        if not paper.title: missing.append("title")
        if not paper.authors: missing.append("authors")
        if not paper.journal: missing.append("journal")
        if not paper.year: missing.append("year")
        if not paper.doi: missing.append("DOI")
        
        # Currently unsupported DB fields
        missing.extend(["volume", "issue", "pages", "publisher"])
        
        if not impact or impact.impact_factor is None: 
            missing.append("impact factor")
            
        if missing:
            items.append({
                "paper_id": paper.id,
                "title": paper.title or "Unknown Title",
                "missing_fields": missing,
                "metadata_source": "user_import", # Assuming static until DB supports tracking
                "evidence_status_disclaimer": "Completeness of metadata does NOT imply evidence safety or verification.",
            })
            
    return {
        "total_papers_needing_metadata": len(items),
        "items": items,
        "safety_guardrails": {
            "online_scraping_enabled": False,
            "auto_completion_enabled": False,
            "safety_upgrade_on_completion": False,
            "message": "This endpoint is strictly read-only diagnostics. It performs no external lookups and does not alter paper statuses."
        }
    }
