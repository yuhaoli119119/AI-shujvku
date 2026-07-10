from __future__ import annotations

import math
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperSection
from app.rag.eligibility import is_rag_eligible
from app.rag.retriever import Retriever
from app.schemas.evidence import EvidenceRef, PageSpan
from app.schemas.retrieval import RetrievalSearchRequest, RetrievalSearchResponse, RetrievalSearchResult
from app.services.embedding import get_embedding_service
from app.services.content_knowledge_service import ContentKnowledgeService
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
        self.embedding_provider = settings.embedding_provider
        self.embedding_model = settings.embedding_model
        self.embedding_dimension = settings.embedding_dimension
        self.retriever = Retriever(session, embedding_dimension=settings.embedding_dimension, embedding=embedding)
        self.reranker = reranker or NoopReranker()

    def _vector_recall_status(self) -> str:
        provider = (self.embedding_provider or "deterministic").lower()
        if provider == "openai_compatible":
            return (
                f"enabled: {self.embedding_model} via openai_compatible "
                f"({self.embedding_dimension} dimensions)"
            )
        return f"enabled: deterministic embedding cosine fallback ({self.embedding_dimension} dimensions)"

    def search(self, payload: RetrievalSearchRequest) -> RetrievalSearchResponse:
        is_full_context = payload.mode == "full_context" and bool(payload.paper_ids)
        if is_full_context:
            sections = self._search_full_context(payload.paper_ids, payload.limit)
            content_items = self._content_knowledge_results(payload)
            items = self._compose_full_context(sections, content_items, payload.limit)
        else:
            retrieved = self.retriever.retrieve(
                query=payload.query,
                paper_ids=payload.paper_ids or None,
                limit_per_type=payload.limit_per_type,
                target_paper_type=payload.target_paper_type,
                paper_type_filter=normalize_paper_type_filter(payload.target_paper_type),
            )
            items = self._flatten_retrieved(retrieved)
            items = self._merge_content_knowledge(items, payload)

        actually_reranked = False
        if is_full_context:
            pass # Full context must preserve sequential reading order
        elif payload.rerank:
            items = self.reranker.rerank(items, payload.query)
            actually_reranked = True
        else:
            items = sorted(items, key=lambda item: item.score, reverse=True)

        limited = items if is_full_context else items[: payload.limit]
        
        reranker_name = self.reranker.name if actually_reranked else ("disabled_for_full_context" if payload.rerank and is_full_context else "disabled")
        
        return RetrievalSearchResponse(
            query=payload.query,
            mode=payload.mode,
            recall={
                "bm25": "enabled: deterministic lexical overlap over section/fact/card text",
                "vector": self._vector_recall_status(),
            },
            reranker={
                "enabled": actually_reranked,
                "name": reranker_name,
                "interface": "rerank(items, query) -> items",
            },
            total=len(limited),
            items=limited,
        )

    def _search_full_context(self, paper_ids: list[UUID], limit: int) -> list[RetrievalSearchResult]:
        ordered_paper_ids: list[UUID] = []
        seen_paper_ids: set[UUID] = set()
        for paper_id in paper_ids:
            if paper_id in seen_paper_ids:
                continue
            seen_paper_ids.add(paper_id)
            ordered_paper_ids.append(paper_id)
        if not ordered_paper_ids:
            return []

        paper_codes = dict(
            self.session.execute(
                select(Paper.id, Paper.paper_code).where(Paper.id.in_(ordered_paper_ids))
            ).all()
        )

        sections_by_paper: list[list[tuple[PaperSection, str, str]]] = []
        seen_section_ids: set[UUID] = set()
        for paper_id in ordered_paper_ids:
            valid_sections: list[tuple[PaperSection, str, str]] = []
            stmt = (
                select(PaperSection)
                .where(PaperSection.paper_id == paper_id)
                .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.id.asc())
            )
            for section in self.session.scalars(stmt).all():
                if section.id in seen_section_ids:
                    continue
                text = (section.text or "").strip()
                if not text:
                    continue
                text = _clean_retrieval_text(text)
                if not text:
                    continue
                seen_section_ids.add(section.id)
                valid_sections.append(
                    (section, text, _clean_retrieval_text(section.section_title))
                )
            sections_by_paper.append(valid_sections)

        allocations = [0] * len(sections_by_paper)
        remaining_slots = min(limit, sum(len(sections) for sections in sections_by_paper))
        while remaining_slots > 0:
            active_papers = [
                paper_index
                for paper_index, sections in enumerate(sections_by_paper)
                if allocations[paper_index] < len(sections)
            ]
            if not active_papers:
                break
            for paper_index in active_papers:
                if remaining_slots <= 0:
                    break
                allocations[paper_index] += 1
                remaining_slots -= 1

        items: list[RetrievalSearchResult] = []
        index = 0
        for paper_index, sections in enumerate(sections_by_paper):
            for section, text, section_title in sections[: allocations[paper_index]]:
                chunk_key = str(section.id)
                score = round(max(0.1, 1.0 - index * 0.001), 4)
                index += 1
                items.append(
                    RetrievalSearchResult(
                        score=score,
                        source="full_context",
                        paper_id=section.paper_id,
                        paper_code=paper_codes.get(section.paper_id),
                        chunk_id=chunk_key,
                        section_id=section.id,
                        section_title=section_title,
                        text=text,
                        page_start=section.page_start,
                        page_end=section.page_end,
                        score_breakdown={"bm25": 0.0, "vector": 0.0, "hybrid": score},
                        evidence=EvidenceRef(
                            paper_id=section.paper_id,
                            chunk_id=chunk_key,
                            section_id=section.id,
                            page_span=PageSpan(page_start=section.page_start, page_end=section.page_end),
                            evidence_text=text[:1200],
                            confidence=score,
                            source="full_context",
                            section_title=section_title,
                            target_type="section",
                            target_id=chunk_key,
                        ),
                        metadata={
                            "section_type": section.section_type,
                            "paper_code": paper_codes.get(section.paper_id),
                        },
                    )
                )
        return items

    def _full_context(self, paper_ids: list[UUID], limit: int) -> list[RetrievalSearchResult]:
        items: list[RetrievalSearchResult] = []
        paper_codes = dict(
            self.session.execute(
                select(Paper.id, Paper.paper_code).where(Paper.id.in_(set(paper_ids)))
            ).all()
        ) if paper_ids else {}
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
                        paper_code=paper_codes.get(section.paper_id),
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
                        metadata={
                            "section_type": section.section_type,
                            "paper_code": paper_codes.get(section.paper_id),
                        },
                    )
                )
        return items[:limit]

    def _merge_content_knowledge(
        self,
        items: list[RetrievalSearchResult],
        payload: RetrievalSearchRequest,
    ) -> list[RetrievalSearchResult]:
        seen = {
            (item.source_type, item.source_id)
            for item in items
            if item.source_type and item.source_id
        }
        merged = list(items)
        for item in self._content_knowledge_results(payload):
            key = (item.source_type, item.source_id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _compose_full_context(
        sections: list[RetrievalSearchResult],
        content_items: list[RetrievalSearchResult],
        limit: int,
    ) -> list[RetrievalSearchResult]:
        """Reserve a bounded content-evidence lane without reordering sections."""
        if not content_items:
            return sections[:limit]
        content_slots = min(len(content_items), max(1, limit // 4))
        section_slots = max(0, limit - content_slots)
        # The section lane remains in original paper/page order.  Content is
        # appended as an explicit evidence appendix, never interleaved into it.
        return [*sections[:section_slots], *content_items[:content_slots]]

    def _content_knowledge_results(self, payload: RetrievalSearchRequest) -> list[RetrievalSearchResult]:
        service = ContentKnowledgeService(self.session)
        # Retrieval is read-only.  Projection/backfill is explicit through the
        # content sync endpoint or source-change workers, never a query side effect.
        scored_items = service.search_for_rag(
            query=payload.query, paper_ids=payload.paper_ids or None,
            include_review_assist=payload.include_review_assist,
            limit=max(payload.limit_per_type * 2, payload.limit),
        )

        results: list[RetrievalSearchResult] = []
        for item, score_breakdown in scored_items:
            row = item.payload()
            score = score_breakdown["hybrid"]
            if score <= 0:
                continue
            paper_id = UUID(str(row["paper_id"]))
            locator = row.get("evidence_locator") if isinstance(row.get("evidence_locator"), dict) else {}
            page_start = row.get("page_start") or locator.get("page") or locator.get("page_start")
            page_end = row.get("page_end") or locator.get("page") or locator.get("page_end")
            text = _clean_retrieval_text(row.get("content") or row.get("evidence_text") or "")
            if not text:
                continue
            evidence_text = _clean_retrieval_text(row.get("evidence_text") or text) or text
            results.append(
                RetrievalSearchResult(
                    score=score,
                    source="content_knowledge",
                    source_type=row.get("source_type"),
                    source_id=row.get("source_id"),
                    paper_id=paper_id,
                    paper_code=row.get("paper_code"),
                    chunk_id=row.get("source_id"),
                    section_title=_clean_retrieval_text(row.get("section_title")),
                    text=text,
                    page=page_start,
                    evidence_text=evidence_text,
                    review_status=row.get("review_status"),
                    page_start=page_start,
                    page_end=page_end,
                    score_breakdown=score_breakdown,
                    evidence=EvidenceRef(
                        paper_id=paper_id,
                        chunk_id=row.get("source_id"),
                        page_span=PageSpan(page_start=page_start, page_end=page_end),
                        evidence_text=evidence_text,
                        confidence=score,
                        source="content_knowledge",
                        section_title=_clean_retrieval_text(row.get("section_title")),
                        target_type=row.get("source_type"),
                        target_id=row.get("source_id"),
                        locator_status=locator.get("locator_status"),
                        locator_confidence=locator.get("locator_confidence"),
                    ),
                    metadata=normalize_text_tree(
                        {
                            "category": row.get("category"),
                            "category_label": row.get("category_label"),
                            "source_table": row.get("source_table"),
                            "review_gate_status": row.get("review_gate_status"),
                            "candidate_status": row.get("candidate_status"),
                            "citation_policy": row.get("citation_policy"),
                            "can_use_for_writing": row.get("can_use_for_writing"),
                            "can_use_for_citation": row.get("can_use_for_citation"),
                            "risk_flags": row.get("risk_flags") or [],
                            "recommended_action": row.get("recommended_action"),
                            "source_ai": row.get("source_ai"),
                            "source_label": row.get("source_label"),
                            "content_knowledge_metadata": row.get("metadata") or {},
                        }
                    ),
                )
            )
        return results

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


def _content_knowledge_score(query: str, row: dict[str, Any]) -> float:
    query_tokens = _retrieval_tokens(query)
    if not query_tokens:
        return 0.0
    haystack = _retrieval_tokens(
        " ".join(
            [
                str(row.get("content") or ""),
                str(row.get("evidence_text") or ""),
                str(row.get("paper_title") or ""),
                str(row.get("paper_code") or ""),
                str(row.get("category") or ""),
                str(row.get("category_label") or ""),
                str(row.get("metadata") or ""),
            ]
        )
    )
    if not haystack:
        return 0.0
    overlap = len(query_tokens & haystack)
    if overlap == 0:
        return 0.0
    coverage = overlap / max(1, len(query_tokens))
    density = overlap / max(1, len(haystack))
    return round(min(0.98, 0.35 + coverage * 0.55 + density * 0.1), 4)


def _retrieval_tokens(value: Any) -> set[str]:
    text = str(value or "").casefold()
    tokens = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", text))
    compact = "".join(re.findall(r"[\u3400-\u9fff]", text))
    tokens.update(compact[index:index + 2] for index in range(max(0, len(compact) - 1)))
    return {token for token in tokens if token}

