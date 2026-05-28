from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Paper, PaperCitationEligibility, utcnow


VALID_PRIORITIES = {"high", "medium", "low", "exclude"}


@dataclass(frozen=True)
class CitationEligibilityUpdate:
    included_for_writing: bool | None = None
    exclude_from_citation: bool | None = None
    exclude_reason: str | None = None
    citation_priority: str | None = None
    user_note: str | None = None


class CitationEligibilityService:
    """Paper-level citation eligibility writes only.

    This service intentionally does not mutate papers, extraction results,
    review rows, evidence rows, export gates, or writing gates.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def update(self, paper_id: UUID, payload: CitationEligibilityUpdate) -> PaperCitationEligibility:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise ValueError(f"Paper not found: {paper_id}")
        row = self.session.get(PaperCitationEligibility, paper_id)
        if row is None:
            row = PaperCitationEligibility(paper_id=paper_id)
            self.session.add(row)
            self.session.flush()
        self._apply(row, payload)
        self.session.commit()
        self.session.refresh(row)
        return row

    def bulk_update(
        self,
        paper_ids: list[UUID],
        payload: CitationEligibilityUpdate,
    ) -> list[PaperCitationEligibility]:
        rows: list[PaperCitationEligibility] = []
        for paper_id in paper_ids:
            paper = self.session.get(Paper, paper_id)
            if paper is None:
                raise ValueError(f"Paper not found: {paper_id}")
            row = self.session.get(PaperCitationEligibility, paper_id)
            if row is None:
                row = PaperCitationEligibility(paper_id=paper_id)
                self.session.add(row)
                self.session.flush()
            self._apply(row, payload)
            rows.append(row)
        self.session.commit()
        for row in rows:
            self.session.refresh(row)
        return rows

    def _apply(self, row: PaperCitationEligibility, payload: CitationEligibilityUpdate) -> None:
        if payload.citation_priority is not None and payload.citation_priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid citation_priority: {payload.citation_priority}")
        if payload.included_for_writing is not None:
            row.included_for_writing = payload.included_for_writing
        if payload.exclude_from_citation is not None:
            row.exclude_from_citation = payload.exclude_from_citation
        if payload.exclude_reason is not None:
            row.exclude_reason = payload.exclude_reason
        if payload.citation_priority is not None:
            row.citation_priority = payload.citation_priority
            if payload.citation_priority == "exclude":
                row.exclude_from_citation = True
        if payload.user_note is not None:
            row.user_note = payload.user_note
        row.updated_at = utcnow()
