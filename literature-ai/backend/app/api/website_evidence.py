"""The reader's claim projection, without registering legacy write workflows."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.evidence import ClaimEvidence
from app.schemas.evidence import EvidenceRef, PageSpan
from app.services.evidence_service import EvidenceService

router = APIRouter()


class WebsiteEvidenceService(EvidenceService):
    def _derived_claim(self, claim_text, target_type, target_id, paper_id, evidence_text, confidence, source_section):
        # A derived sentence is a candidate display, not evidence of its own
        # truth. Missing source text must not be filled with the sentence.
        evidence = []
        if evidence_text:
            locator = self.locators.resolve_field_locator(paper_id=paper_id,target_type=target_type,
                target_id=str(target_id),field_name='evidence_text',evidence_text=evidence_text,
                source_section=source_section,page_span=PageSpan())
            evidence = [EvidenceRef(paper_id=paper_id,chunk_id=str(target_id),evidence_text=evidence_text,
                confidence=confidence,source=target_type,section_title=source_section,
                target_type=target_type,target_id=str(target_id),locator=locator)]
        return ClaimEvidence(claim_text=claim_text,source_type='derived',target_type=target_type,
            target_id=str(target_id),evidence=evidence,confidence=confidence,
            validation_status='unverified' if evidence else 'unsupported',
            metadata={'projection':'stored_candidate','missing_reason':None if evidence else 'missing_source_text'})


@router.get('/claims', response_model=list[ClaimEvidence])
def list_website_claims(paper_id: UUID | None = None, target_type: str | None = None,
                        target_id: str | None = None, include_derived: bool = True,
                        limit: int = Query(100,ge=1,le=500), session: Session = Depends(get_db_session)):
    with session.no_autoflush:
        if paper_id is not None and session.get(Paper,paper_id) is None:
            raise HTTPException(404,detail='Paper not found')
        return WebsiteEvidenceService(session).list_claims(paper_id=paper_id,target_type=target_type,
                            target_id=target_id,include_derived=include_derived,limit=limit)
