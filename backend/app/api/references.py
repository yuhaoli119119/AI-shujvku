from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, ReferenceEntry
from app.db.session import get_db_session
from app.schemas.api import ReferenceEntryCreate, ReferenceEntryResponse

router = APIRouter()


@router.get("/{paper_id}/references", response_model=list[ReferenceEntryResponse])
async def list_references(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[ReferenceEntryResponse]:
    """List all reference entries for a paper, ordered by reference_number."""
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    rows = session.scalars(
        select(ReferenceEntry)
        .where(ReferenceEntry.paper_id == paper_id)
        .order_by(ReferenceEntry.reference_number.asc().nulls_last(), ReferenceEntry.created_at.asc())
    ).all()
    return [ReferenceEntryResponse.model_validate(r) for r in rows]


@router.post("/{paper_id}/references", response_model=ReferenceEntryResponse, status_code=201)
async def create_reference(
    paper_id: UUID,
    payload: ReferenceEntryCreate,
    session: Session = Depends(get_db_session),
) -> ReferenceEntryResponse:
    """Add a reference entry to a paper."""
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if payload.linked_paper_id:
        linked = session.get(Paper, payload.linked_paper_id)
        if not linked:
            raise HTTPException(status_code=404, detail="Linked paper not found")
    ref = ReferenceEntry(
        paper_id=paper_id,
        title=payload.title,
        authors=payload.authors,
        journal=payload.journal,
        year=payload.year,
        doi=payload.doi,
        volume=payload.volume,
        pages=payload.pages,
        reference_number=payload.reference_number,
        citation_context=payload.citation_context,
        linked_paper_id=payload.linked_paper_id,
    )
    session.add(ref)
    session.commit()
    session.refresh(ref)
    return ReferenceEntryResponse.model_validate(ref)


@router.put("/{paper_id}/references/{ref_id}", response_model=ReferenceEntryResponse)
async def update_reference(
    paper_id: UUID,
    ref_id: UUID,
    payload: ReferenceEntryCreate,
    session: Session = Depends(get_db_session),
) -> ReferenceEntryResponse:
    """Update a reference entry."""
    ref = session.scalar(
        select(ReferenceEntry).where(ReferenceEntry.id == ref_id, ReferenceEntry.paper_id == paper_id)
    )
    if not ref:
        raise HTTPException(status_code=404, detail="Reference entry not found")
    if payload.linked_paper_id:
        linked = session.get(Paper, payload.linked_paper_id)
        if not linked:
            raise HTTPException(status_code=404, detail="Linked paper not found")
    ref.title = payload.title
    ref.authors = payload.authors
    ref.journal = payload.journal
    ref.year = payload.year
    ref.doi = payload.doi
    ref.volume = payload.volume
    ref.pages = payload.pages
    ref.reference_number = payload.reference_number
    ref.citation_context = payload.citation_context
    ref.linked_paper_id = payload.linked_paper_id
    session.add(ref)
    session.commit()
    session.refresh(ref)
    return ReferenceEntryResponse.model_validate(ref)


@router.delete("/{paper_id}/references/{ref_id}")
async def delete_reference(
    paper_id: UUID,
    ref_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict:
    """Delete a reference entry."""
    ref = session.scalar(
        select(ReferenceEntry).where(ReferenceEntry.id == ref_id, ReferenceEntry.paper_id == paper_id)
    )
    if not ref:
        raise HTTPException(status_code=404, detail="Reference entry not found")
    session.delete(ref)
    session.commit()
    return {"status": "deleted", "ref_id": str(ref_id)}
