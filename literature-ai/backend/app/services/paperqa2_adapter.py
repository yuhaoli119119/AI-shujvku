from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperChunk


class PaperQA2UnavailableError(RuntimeError):
    """Raised when the optional paper-qa package is not installed."""


@dataclass(frozen=True)
class PaperQA2Evidence:
    paper_id: UUID
    chunk_id: UUID
    text: str
    page_start: int | None
    page_end: int | None


class PaperQA2Adapter:
    """Optional PaperQA2 bridge over the PostgreSQL-backed LitAI corpus.

    PaperQA2 never becomes the source of truth here. We construct an in-memory
    Docs object from Paper/PaperChunk rows that already live in PostgreSQL.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_docs(self, paper_ids: list[UUID] | None = None, limit_chunks: int = 500):
        paperqa = self._paperqa_module()
        stmt = (
            select(Paper, PaperChunk)
            .join(PaperChunk, PaperChunk.paper_id == Paper.id)
            .where(PaperChunk.text.is_not(None))
            .order_by(Paper.created_at.asc(), PaperChunk.chunk_index.asc())
            .limit(limit_chunks)
        )
        if paper_ids:
            stmt = stmt.where(Paper.id.in_(paper_ids))
        rows = self.session.execute(stmt).all()

        docs = paperqa.Docs()
        chunks_by_paper: dict[UUID, list[PaperChunk]] = defaultdict(list)
        papers: dict[UUID, Paper] = {}
        for paper, chunk in rows:
            papers[paper.id] = paper
            chunks_by_paper[paper.id].append(chunk)

        for paper_id, chunks in chunks_by_paper.items():
            paper = papers[paper_id]
            doc = paperqa.Doc(
                docname=str(paper.title or paper.doi or paper.id),
                citation=self._citation_for_paper(paper),
                dockey=str(paper.id),
            )
            texts = [
                paperqa.Text(
                    text=chunk.text,
                    name=f"{paper.id}:{chunk.id}",
                    doc=doc,
                )
                for chunk in chunks
                if (chunk.text or "").strip()
            ]
            if texts:
                docs.add_texts(texts, doc)
        return docs

    async def aquery(
        self,
        query: str,
        *,
        paper_ids: list[UUID] | None = None,
        settings: Any | None = None,
        limit_chunks: int = 500,
    ) -> Any:
        docs = self.build_docs(paper_ids=paper_ids, limit_chunks=limit_chunks)
        return await docs.aquery(query, settings=settings)

    def query(
        self,
        query: str,
        *,
        paper_ids: list[UUID] | None = None,
        settings: Any | None = None,
        limit_chunks: int = 500,
    ) -> Any:
        return asyncio.run(
            self.aquery(query, paper_ids=paper_ids, settings=settings, limit_chunks=limit_chunks)
        )

    def evidence_rows(self, paper_ids: list[UUID] | None = None, limit_chunks: int = 500) -> list[PaperQA2Evidence]:
        stmt = (
            select(PaperChunk)
            .where(PaperChunk.text.is_not(None))
            .order_by(PaperChunk.paper_id.asc(), PaperChunk.chunk_index.asc())
            .limit(limit_chunks)
        )
        if paper_ids:
            stmt = stmt.where(PaperChunk.paper_id.in_(paper_ids))
        return [
            PaperQA2Evidence(
                paper_id=chunk.paper_id,
                chunk_id=chunk.id,
                text=chunk.text,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
            )
            for chunk in self.session.scalars(stmt).all()
        ]

    @staticmethod
    def _paperqa_module():
        try:
            import paperqa
        except ImportError as exc:
            raise PaperQA2UnavailableError(
                "PaperQA2 is optional. Install it with `pip install paper-qa` to run this adapter or benchmark."
            ) from exc
        missing = [name for name in ("Docs", "Doc", "Text") if not hasattr(paperqa, name)]
        if missing:
            raise PaperQA2UnavailableError(f"Installed paperqa package is missing expected APIs: {', '.join(missing)}")
        return paperqa

    @staticmethod
    def _citation_for_paper(paper: Paper) -> str:
        authors = paper.authors or []
        if isinstance(authors, list) and authors:
            author_text = str(authors[0])
        else:
            author_text = "Unknown"
        year_text = str(paper.year) if paper.year else "n.d."
        title = str(paper.title or paper.doi or paper.id)
        return f"{author_text} ({year_text}). {title}."
