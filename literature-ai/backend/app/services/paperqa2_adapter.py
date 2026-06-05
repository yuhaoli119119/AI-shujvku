from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperChunk


class PaperQA2UnavailableError(RuntimeError):
    """Raised when the optional paper-qa dependency is not installed."""


@dataclass(frozen=True)
class PaperQA2TextRecord:
    paper_id: UUID
    chunk_id: UUID
    citation: str
    text: str
    metadata: dict[str, Any]


class PaperQA2Adapter:
    """Optional bridge from the Literature AI database to PaperQA2.

    The local PostgreSQL database remains the source of truth. This adapter only
    exports parsed chunks into PaperQA2's in-memory document model for evaluation
    or deep-question answering.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def text_records(self, paper_ids: list[UUID] | None = None, limit: int | None = None) -> list[PaperQA2TextRecord]:
        stmt = select(PaperChunk, Paper).join(Paper, PaperChunk.paper_id == Paper.id)
        if paper_ids:
            stmt = stmt.where(PaperChunk.paper_id.in_(paper_ids))
        stmt = stmt.order_by(Paper.created_at.asc(), PaperChunk.chunk_index.asc())
        if limit:
            stmt = stmt.limit(limit)

        records: list[PaperQA2TextRecord] = []
        for chunk, paper in self.session.execute(stmt).all():
            citation = self._citation_for_paper(paper)
            records.append(
                PaperQA2TextRecord(
                    paper_id=paper.id,
                    chunk_id=chunk.id,
                    citation=citation,
                    text=chunk.text,
                    metadata={
                        "paper_id": str(paper.id),
                        "chunk_id": str(chunk.id),
                        "title": paper.title,
                        "year": paper.year,
                        "journal": paper.journal,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                    },
                )
            )
        return records

    def build_docs(self, paper_ids: list[UUID] | None = None, limit: int | None = None) -> Any:
        try:
            from paperqa import Docs
        except Exception as exc:
            raise PaperQA2UnavailableError("Install paper-qa to use PaperQA2Adapter.build_docs") from exc

        docs = Docs()
        for record in self.text_records(paper_ids=paper_ids, limit=limit):
            try:
                docs.add_text(record.text, citation=record.citation, dockey=str(record.chunk_id))
            except TypeError:
                docs.add_text(record.text, citation=record.citation)
        return docs

    @staticmethod
    def _citation_for_paper(paper: Paper) -> str:
        year = f" ({paper.year})" if paper.year else ""
        title = paper.title or str(paper.id)
        journal = f", {paper.journal}" if paper.journal else ""
        return f"{title}{year}{journal}"
