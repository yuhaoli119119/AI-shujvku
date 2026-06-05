from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaperSection
from app.rag.retriever import Retriever
from app.schemas.evidence import EvidenceRef, PageSpan
from app.schemas.retrieval import RetrievalSearchRequest, RetrievalSearchResponse, RetrievalSearchResult
from app.services.embedding import get_embedding_service
from app.utils.paper_type import normalize_paper_type_filter


class NoopReranker:
    name = "noop_score_sort"

    def rerank(self, items: list[RetrievalSearchResult], query: str) -> list[RetrievalSearchResult]:
        return sorted(items, key=lambda item: item.score, reverse=True)


class RetrievalService:
    """Unified retrieval service for full-context paper reading and focused review search."""

    def __init__(self, session: Session, reranker: NoopReranker | None = None) -> None:
        self.session = session
        from app.config import get_settings

        settings = get_settings()
        embedding = get_embedding_service(
            provider=settings.embedding_provider,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        self.retriever = Retriever(session, embedding_dimension=settings.embedding_dimension, embedding=embedding)
        self.reranker = reranker or NoopReranker()

    def search(self, payload: RetrievalSearchRequest) -> RetrievalSearchResponse:
        if payload.mode == "full_context" and payload.paper_ids:
            items = self._full_context(payload.paper_ids, payload.limit)
        else:
            retrieved = self.retriever.retrieve(
                query=payload.query,
                paper_ids=payload.paper_ids or None,
                limit_per_type=payload.limit_per_type,
                target_paper_type=payload.target_paper_type,
                paper_type_filter=normalize_paper_type_filter(payload.target_paper_type),
            )
            items = self._flatten_retrieved(retrieved)

        if payload.rerank:
            items = self.reranker.rerank(items, payload.query)
        else:
            items = sorted(items, key=lambda item: item.score, reverse=True)

        limited = items[: payload.limit]
        return RetrievalSearchResponse(
            query=payload.query,
            mode=payload.mode,
            recall={
                "bm25": "enabled: deterministic lexical overlap over section/fact/card text",
                "vector": "enabled: deterministic embedding cosine fallback",
            },
            reranker={
                "enabled": payload.rerank,
                "name": self.reranker.name,
                "interface": "rerank(items, query) -> items",
            },
            total=len(limited),
            items=limited,
        )

    def _full_context(self, paper_ids: list[UUID], limit: int) -> list[RetrievalSearchResult]:
        stmt = (
            select(PaperSection)
            .where(PaperSection.paper_id.in_(paper_ids))
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
            .limit(limit)
        )
        items: list[RetrievalSearchResult] = []
        for index, section in enumerate(self.session.scalars(stmt).all()):
            text = (section.text or "").strip()
            if not text:
                continue
            score = round(max(0.1, 1.0 - index * 0.01), 4)
            items.append(
                RetrievalSearchResult(
                    score=score,
                    source="full_context",
                    paper_id=section.paper_id,
                    chunk_id=str(section.id),
                    section_id=section.id,
                    section_title=section.section_title,
                    text=text,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    score_breakdown={"bm25": 0.0, "vector": 0.0, "hybrid": score},
                    evidence=EvidenceRef(
                        paper_id=section.paper_id,
                        chunk_id=str(section.id),
                        section_id=section.id,
                        page_span=PageSpan(page_start=section.page_start, page_end=section.page_end),
                        evidence_text=text[:1200],
                        confidence=score,
                        source="full_context",
                        section_title=section.section_title,
                        target_type="section",
                        target_id=str(section.id),
                    ),
                    metadata={"section_type": section.section_type},
                )
            )
        return items

    @staticmethod
    def _flatten_retrieved(retrieved: dict[str, list[dict[str, Any]]]) -> list[RetrievalSearchResult]:
        flat: list[RetrievalSearchResult] = []
        for source, rows in (retrieved or {}).items():
            for row in rows:
                text = row.get("evidence_text") or row.get("text") or ""
                if not text:
                    continue
                paper_id = row.get("paper_id")
                object_id = row.get("object_id")
                section_id = row.get("section_id") or (object_id if row.get("type") == "section" else None)
                score_breakdown = row.get("score_breakdown") or {}
                normalized_breakdown = {
                    "bm25": float(score_breakdown.get("lexical", score_breakdown.get("bm25", 0.0)) or 0.0),
                    "vector": float(score_breakdown.get("semantic", score_breakdown.get("vector", 0.0)) or 0.0),
                    "hybrid": float(score_breakdown.get("hybrid", row.get("score", 0.0)) or 0.0),
                }
                flat.append(
                    RetrievalSearchResult(
                        score=float(row.get("score") or 0.0),
                        source=source,
                        paper_id=paper_id,
                        chunk_id=str(object_id) if object_id else None,
                        section_id=section_id,
                        section_title=row.get("section_title") or row.get("source_section"),
                        text=text,
                        page_start=row.get("page_start"),
                        page_end=row.get("page_end"),
                        score_breakdown=normalized_breakdown,
                        evidence=EvidenceRef(
                            paper_id=paper_id,
                            chunk_id=str(object_id) if object_id else None,
                            section_id=section_id,
                            page_span=PageSpan(page_start=row.get("page_start"), page_end=row.get("page_end")),
                            evidence_text=text,
                            confidence=float(row.get("confidence") or row.get("score") or 0.0),
                            source=source,
                            section_title=row.get("section_title") or row.get("source_section"),
                            target_type=row.get("type"),
                            target_id=str(object_id) if object_id else None,
                        ),
                        metadata={k: v for k, v in row.items() if k not in {"text", "evidence_text", "score", "score_breakdown"}},
                    )
                )
        return flat

