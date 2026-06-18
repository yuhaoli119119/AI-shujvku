from __future__ import annotations

import math
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaperSection
from app.rag.eligibility import is_rag_eligible
from app.rag.retriever import Retriever
from app.schemas.evidence import EvidenceRef, PageSpan
from app.schemas.retrieval import RetrievalSearchRequest, RetrievalSearchResponse, RetrievalSearchResult
from app.services.embedding import get_embedding_service
from app.utils.text_cleaning import normalize_text_tree, repair_mojibake_text
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
        is_full_context = payload.mode == "full_context" and bool(payload.paper_ids)
        if is_full_context:
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

        actually_reranked = False
        if is_full_context:
            pass # Full context must preserve sequential reading order
        elif payload.rerank:
            items = self.reranker.rerank(items, payload.query)
            actually_reranked = True
        else:
            items = sorted(items, key=lambda item: item.score, reverse=True)

        limited = items[: payload.limit]
        
        reranker_name = self.reranker.name if actually_reranked else ("disabled_for_full_context" if payload.rerank and is_full_context else "disabled")
        
        return RetrievalSearchResponse(
            query=payload.query,
            mode=payload.mode,
            recall={
                "bm25": "enabled: deterministic lexical overlap over section/fact/card text",
                "vector": "enabled: deterministic embedding cosine fallback",
            },
            reranker={
                "enabled": actually_reranked,
                "name": reranker_name,
                "interface": "rerank(items, query) -> items",
            },
            total=len(limited),
            items=limited,
        )

    def _full_context(self, paper_ids: list[UUID], limit: int) -> list[RetrievalSearchResult]:
        items: list[RetrievalSearchResult] = []
        limit_per_paper = max(1, math.ceil(limit / len(paper_ids))) if paper_ids else limit
        index = 0
        for paper_id in paper_ids:
            stmt = (
                select(PaperSection)
                .where(PaperSection.paper_id == paper_id)
                .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.id.asc())
                .limit(limit_per_paper)
            )
            for section in self.session.scalars(stmt).all():
                if not is_rag_eligible(self.session, section, "section"):
                    continue
                text = (section.text or "").strip()
                if not text:
                    continue
                text = _clean_retrieval_text(text)
                section_title = _clean_retrieval_text(section.section_title)
                score = round(max(0.1, 1.0 - index * 0.001), 4)
                index += 1
                items.append(
                    RetrievalSearchResult(
                        score=score,
                        source="full_context",
                        paper_id=section.paper_id,
                        chunk_id=str(section.id),
                        section_id=section.id,
                        section_title=section_title,
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
                            section_title=section_title,
                            target_type="section",
                            target_id=str(section.id),
                        ),
                        metadata={"section_type": section.section_type},
                    )
                )
        return items[:limit]

    @staticmethod
    def _flatten_retrieved(retrieved: dict[str, list[dict[str, Any]]]) -> list[RetrievalSearchResult]:
        flat: list[RetrievalSearchResult] = []
        for source, rows in (retrieved or {}).items():
            for row in rows:
                text = _clean_retrieval_text(row.get("evidence_text") or row.get("text") or "")
                if not text:
                    continue
                paper_id = row.get("paper_id")
                object_id = row.get("object_id")
                section_id = row.get("section_id") or (object_id if row.get("type") == "section" else None)
                score_breakdown = row.get("score_breakdown") or {}
                locator = row.get("evidence_locator") if isinstance(row.get("evidence_locator"), dict) else {}
                page_start = row.get("page_start") or locator.get("page")
                page_end = row.get("page_end") or locator.get("page")
                normalized_breakdown = {
                    "bm25": float(score_breakdown.get("lexical", score_breakdown.get("bm25", 0.0)) or 0.0),
                    "vector": float(score_breakdown.get("semantic", score_breakdown.get("vector", 0.0)) or 0.0),
                    "hybrid": float(score_breakdown.get("hybrid", row.get("score", 0.0)) or 0.0),
                }
                flat.append(
                    RetrievalSearchResult(
                        score=float(row.get("score") or 0.0),
                        source=source,
                        source_type=row.get("source_type") or row.get("type") or source,
                        source_id=str(row.get("source_id") or object_id) if (row.get("source_id") or object_id) else None,
                        paper_id=paper_id,
                        paper_code=row.get("paper_code"),
                        chunk_id=str(object_id) if object_id else None,
                        section_id=section_id,
                        section_title=_clean_retrieval_text(row.get("section_title") or row.get("source_section")),
                        text=text,
                        page=row.get("page") or page_start,
                        evidence_text=_clean_retrieval_text(row.get("evidence_text") or text),
                        review_status=row.get("review_status") or row.get("review_gate_status") or row.get("provenance_level"),
                        page_start=page_start,
                        page_end=page_end,
                        score_breakdown=normalized_breakdown,
                        evidence=EvidenceRef(
                            paper_id=paper_id,
                            chunk_id=str(object_id) if object_id else None,
                            section_id=section_id,
                            page_span=PageSpan(page_start=page_start, page_end=page_end),
                            evidence_text=text,
                            confidence=float(row.get("confidence") or row.get("score") or 0.0),
                            source=source,
                            section_title=_clean_retrieval_text(row.get("section_title") or row.get("source_section")),
                            target_type=row.get("type"),
                            target_id=str(object_id) if object_id else None,
                            locator_status=locator.get("locator_status") or row.get("locator_status"),
                            locator_confidence=locator.get("locator_confidence"),
                        ),
                        metadata=normalize_text_tree({k: v for k, v in row.items() if k not in {"text", "evidence_text", "score", "score_breakdown"}}),
                    )
                )
        return flat


def _clean_retrieval_text(value: Any) -> str | None:
    if value is None:
        return None
    text = repair_mojibake_text(str(value)) or ""
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

