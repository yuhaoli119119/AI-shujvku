from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, ElectrochemicalPerformance, MechanismClaim, PaperChunk, PaperSection, WritingCard, Paper, FigureDataPoint
from app.services.embedding import (
    DeterministicEmbeddingService,
    EmbeddingService,
    EmbeddingUnavailableError,
    get_embedding_service,
)
from app.utils.review_safety import bulk_export_gate_results, writing_card_gate


logger = logging.getLogger(__name__)


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_+-]+", (text or "").lower()) if len(token) > 1}


class Retriever:
    """Hybrid lexical + embedding retriever over sections, facts, claims, and writing cards."""

    def __init__(self, session: Session, embedding_dimension: int = 1024, embedding: EmbeddingService | None = None) -> None:
        self.session = session
        self.embedding = embedding or DeterministicEmbeddingService(embedding_dimension)

    def retrieve(
        self,
        query: str,
        paper_ids: list[UUID] | None = None,
        limit_per_type: int = 5,
        target_paper_type: str | None = None,
        paper_type_filter: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        tokens = _tokenize(query)
        query_embedding = self._safe_query_embedding(query)
        result = {
            "sections": self._retrieve_sections(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "dft_results": self._retrieve_dft_results(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "electrochemical_performance": self._retrieve_electrochemical(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "mechanism_claims": self._retrieve_mechanism_claims(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "writing_cards": self._retrieve_writing_cards(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "figure_data_points": self._retrieve_figure_data(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
        }
        return self._global_dedup(result, limit_per_type)

    def _safe_query_embedding(self, query: str) -> list[float]:
        """Return a query vector when available, otherwise keep retrieval lexical.

        Search must remain responsive even when the configured embedding backend
        is unavailable, slow, rate limited, or misconfigured. The downstream
        scoring code already treats an empty vector as lexical-only retrieval.
        """
        try:
            return self.embedding.embed_text(query)
        except EmbeddingUnavailableError as exc:
            logger.warning("Embedding unavailable; falling back to lexical retrieval: %s", exc)
        except Exception as exc:
            logger.warning("Embedding failed; falling back to lexical retrieval: %s", exc)
        return []

    def _apply_type_filter(self, query: Any, model_class: Any, paper_type_filter: list[str] | None) -> Any:
        if not paper_type_filter:
            return query
        from sqlalchemy import or_
        query = query.join(Paper, model_class.paper_id == Paper.id)
        conditions = [Paper.paper_type.startswith(pt) for pt in paper_type_filter if pt]
        if conditions:
            query = query.where(or_(*conditions))
        return query

    def _retrieve_sections(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        chunk_results = self._retrieve_chunks(tokens, query_embedding, paper_ids, limit, paper_type_filter)
        if chunk_results:
            return chunk_results

        query = select(PaperSection)
        if paper_ids:
            query = query.where(PaperSection.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, PaperSection, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [PaperSection.section_title, PaperSection.section_type, PaperSection.text],
            fallback_limit=max(limit * 20, 200),
        )
        results = []
        for row in rows:
            text = row.text or ""
            haystack = " ".join(filter(None, [row.section_title, row.section_type, text]))
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, row.embedding, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            results.append(
                {
                    "type": "section",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": score,
                    "score_breakdown": score_info,
                    "text": text[:1200],
                    "section_title": row.section_title,
                    "section_type": row.section_type,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                }
            )
        return self._top_k(results, limit)

    def _retrieve_chunks(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._candidate_chunks(tokens, query_embedding, paper_ids, max(limit * 10, 50), paper_type_filter)
        results = []
        for row in rows:
            haystack = row.text or ""
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, row.embedding, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            results.append(
                {
                    "type": "section",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "section_id": row.section_id,
                    "score": score,
                    "score_breakdown": score_info,
                    "text": haystack[:1200],
                    "section_title": "Chunk",
                    "section_type": "chunk",
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "embedding_model": row.embedding_model,
                    "embedding_dimension": row.embedding_dimension,
                }
            )
        return self._top_k(results, limit)

    def _candidate_chunks(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None,
    ) -> list[PaperChunk]:
        dialect_name = self.session.bind.dialect.name if self.session.bind is not None else ""
        query = select(PaperChunk)
        if paper_ids:
            query = query.where(PaperChunk.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, PaperChunk, paper_type_filter)
        if dialect_name != "postgresql" or not query_embedding:
            base_query = query
            filtered_query = self._apply_token_prefilter(base_query, tokens, [PaperChunk.text])
            rows = list(self.session.scalars(filtered_query.limit(max(limit * 2, 100))).all())
            if rows or not tokens:
                return rows
            return list(self.session.scalars(base_query.limit(max(limit * 2, 100))).all())

        vector_literal = "[" + ",".join(f"{float(item):.8f}" for item in query_embedding) + "]"
        query_vector = sa.cast(sa.literal(vector_literal), PaperChunk.embedding.type)
        distance = PaperChunk.embedding.op("<=>")(query_vector)
        pg_query = (
            query.where(PaperChunk.embedding.is_not(None))
            .where(
                sa.or_(
                    PaperChunk.embedding_dimension.is_(None),
                    PaperChunk.embedding_dimension == len(query_embedding),
                )
            )
            .order_by(distance.asc())
            .limit(limit)
        )
        try:
            return list(self.session.scalars(pg_query).all())
        except Exception:
            base_query = query
            filtered_query = self._apply_token_prefilter(base_query, tokens, [PaperChunk.text])
            rows = list(self.session.scalars(filtered_query.limit(max(limit * 2, 100))).all())
            if rows or not tokens:
                return rows
            return list(self.session.scalars(base_query.limit(max(limit * 2, 100))).all())

    def _retrieve_dft_results(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        target_paper_type: str | None = None,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(DFTResult)
        if paper_ids:
            query = query.where(DFTResult.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, DFTResult, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [
                DFTResult.adsorbate,
                DFTResult.property_type,
                DFTResult.reaction_step,
                DFTResult.source_section,
                DFTResult.source_figure,
                DFTResult.evidence_text,
            ],
            fallback_limit=max(limit * 20, 200),
        )
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        results = []
        for row in rows:
            gate = gate_by_id[str(row.id)]
            if not gate.eligible:
                continue
            haystack = " ".join(
                filter(
                    None,
                    [
                        row.adsorbate,
                        row.property_type,
                        row.reaction_step,
                        row.source_section,
                        row.source_figure,
                        row.evidence_text,
                    ],
                )
            )
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            text = self._format_dft_result(row)
            
            bias = 0.25
            if target_paper_type:
                if target_paper_type.startswith("A"):
                    bias += 0.15
                elif target_paper_type.startswith("C"):
                    bias -= 0.15

            results.append(
                {
                    "type": "dft_result",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score + bias, 4),
                    "score_breakdown": score_info,
                    "text": text,
                    "property_type": row.property_type,
                    "adsorbate": row.adsorbate,
                    "value": row.value,
                    "unit": row.unit,
                    "evidence_text": row.evidence_text,
                }
            )
        return self._top_k(results, limit)

    def _retrieve_electrochemical(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        target_paper_type: str | None = None,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(ElectrochemicalPerformance)
        if paper_ids:
            query = query.where(ElectrochemicalPerformance.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, ElectrochemicalPerformance, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [
                ElectrochemicalPerformance.rate,
                ElectrochemicalPerformance.electrolyte_sulfur_ratio,
                ElectrochemicalPerformance.evidence_text,
            ],
            fallback_limit=max(limit * 20, 200),
        )
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="electrochemical_performance")
        results = []
        for row in rows:
            gate = gate_by_id[str(row.id)]
            if not gate.eligible:
                continue
            haystack = self._format_electrochemical(row)
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            
            bias = 0.15
            if target_paper_type:
                if target_paper_type.startswith("C"):
                    bias += 0.15
                elif target_paper_type.startswith("A"):
                    bias -= 0.15

            results.append(
                {
                    "type": "electrochemical_performance",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score + bias, 4),
                    "score_breakdown": score_info,
                    "text": haystack,
                    "capacity_value": row.capacity_value,
                    "rate": row.rate,
                    "cycle_number": row.cycle_number,
                    "evidence_text": row.evidence_text,
                }
            )
        return self._top_k(results, limit)

    def _retrieve_mechanism_claims(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(MechanismClaim)
        if paper_ids:
            query = query.where(MechanismClaim.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, MechanismClaim, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [MechanismClaim.claim_type, MechanismClaim.claim_text, MechanismClaim.evidence_text],
            fallback_limit=max(limit * 20, 200),
        )
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="mechanism_claims")
        results = []
        for row in rows:
            gate = gate_by_id[str(row.id)]
            if not gate.eligible:
                continue
            haystack = " ".join(filter(None, [row.claim_type, row.claim_text, row.evidence_text, " ".join(row.evidence_types or [])]))
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            results.append(
                {
                    "type": "mechanism_claim",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score + 0.2, 4),
                    "score_breakdown": score_info,
                    "text": row.claim_text,
                    "claim_type": row.claim_type,
                    "evidence_text": row.evidence_text,
                    "evidence_types": row.evidence_types or [],
                }
            )
        return self._top_k(results, limit)

    def _retrieve_writing_cards(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(WritingCard)
        if paper_ids:
            query = query.where(WritingCard.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, WritingCard, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [
                WritingCard.paper_type,
                WritingCard.research_gap,
                WritingCard.proposed_solution,
                WritingCard.core_hypothesis,
                WritingCard.abstract_logic,
                WritingCard.introduction_logic,
                WritingCard.discussion_logic,
            ],
            fallback_limit=max(limit * 20, 200),
        )
        results = []
        for row in rows:
            gate = writing_card_gate(row)
            if not gate.can_use_for_writing:
                continue
            haystack = " ".join(
                filter(
                    None,
                    [
                        row.paper_type,
                        row.research_gap,
                        row.proposed_solution,
                        row.core_hypothesis,
                        row.abstract_logic,
                        row.introduction_logic,
                        row.discussion_logic,
                    ],
                )
            )
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, row.embedding, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            results.append(
                {
                    "type": "writing_card",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score + 0.1, 4),
                    "score_breakdown": score_info,
                    "text": row.research_gap or row.proposed_solution or row.core_hypothesis or "",
                    "paper_type": row.paper_type,
                    "research_gap": row.research_gap,
                    "proposed_solution": row.proposed_solution,
                    "core_hypothesis": row.core_hypothesis,
                    "figure_logic": row.figure_logic,
                    "evidence_chain_status": gate.evidence_chain_status,
                    "review_gate_status": gate.review_gate_status,
                    "can_use_for_writing": gate.can_use_for_writing,
                }
            )
        return self._top_k(results, limit)

    def _retrieve_figure_data(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        target_paper_type: str | None = None,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        from app.db.models import PaperFigure
        query = select(FigureDataPoint, PaperFigure.caption).outerjoin(
            PaperFigure, FigureDataPoint.figure_id == PaperFigure.id
        )
        if paper_ids:
            query = query.where(FigureDataPoint.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, FigureDataPoint, paper_type_filter)
        filtered_query = self._apply_token_prefilter(
            query,
            tokens,
            [FigureDataPoint.metric_name, FigureDataPoint.unit, FigureDataPoint.sample_label, PaperFigure.caption],
        )
        rows = self.session.execute(filtered_query).all()
        if not rows and tokens:
            rows = self.session.execute(query.limit(max(limit * 20, 200))).all()
        results = []
        for row, caption in rows:
            fig_caption = caption or ""

            haystack = " ".join(
                filter(
                    None,
                    [
                        row.metric_name,
                        row.unit,
                        row.sample_label,
                        fig_caption,
                        str(row.conditions or ""),
                    ],
                )
            )
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            
            unit_str = f" {row.unit}" if row.unit else ""
            val_str = f": {row.metric_value}" if row.metric_value is not None else ""
            sample_str = f" for {row.sample_label}" if row.sample_label else ""
            cond_str = f" under {row.conditions}" if row.conditions else ""
            fig_suffix = f" (from Figure {fig_caption})" if fig_caption else " (from Figure)"
            evidence_text = f"{row.metric_name}{val_str}{unit_str}{sample_str}{cond_str}{fig_suffix}"

            bias = 0.20
            if target_paper_type:
                bias += 0.10

            results.append(
                {
                    "type": "figure_data_point",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score + bias, 4),
                    "score_breakdown": score_info,
                    "text": evidence_text,
                    "metric_name": row.metric_name,
                    "value": row.metric_value,
                    "unit": row.unit,
                    "sample_label": row.sample_label,
                    "conditions": str(row.conditions) if row.conditions else None,
                    "evidence_text": evidence_text,
                }
            )
        return self._top_k(results, limit)

    def _hybrid_score(
        self,
        query_tokens: set[str],
        query_embedding: list[float],
        text: str,
        stored_embedding: list[float] | None,
        allow_paper_fallback: bool,
    ) -> tuple[float, dict[str, float]]:
        lexical = self._score_text(query_tokens, text)
        semantic = 0.0
        if stored_embedding:
            semantic = max(0.0, self.embedding.cosine_similarity(query_embedding, stored_embedding))
        if lexical <= 0 and semantic <= 0 and allow_paper_fallback:
            lexical = 0.05
        if lexical <= 0 and semantic <= 0:
            return 0.0, {"lexical": 0.0, "semantic": 0.0, "hybrid": 0.0}
        hybrid = round((0.65 * lexical) + (0.35 * semantic), 4)
        return hybrid, {"lexical": round(lexical, 4), "semantic": round(semantic, 4), "hybrid": hybrid}

    @staticmethod
    def _score_text(query_tokens: set[str], text: str) -> float:
        text_tokens = _tokenize(text)
        if not query_tokens or not text_tokens:
            return 0.0
        overlap = query_tokens & text_tokens
        if not overlap:
            return 0.0
        return round(len(overlap) / max(1, len(query_tokens)), 4)

    @staticmethod
    def _global_dedup(
        retrieved: dict[str, list[dict[str, Any]]], limit_per_type: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """Cross-type dedup without collapsing distinct extracted records.

        Stable object identities are authoritative. Full-text fallback is only
        used for synthetic items without object_id, so two distinct extracted
        rows with identical text are still preserved for review.
        """
        best_scores_id: dict[str, float] = {}
        best_scores_content: dict[str, float] = {}
        
        import re
        import hashlib

        def _get_id_key(item: dict[str, Any], type_name: str) -> str:
            pid = str(item.get("paper_id", ""))
            oid = str(item.get("object_id", ""))
            if oid:
                return f"{pid}::{type_name}::{oid}"
            return ""

        def _get_content_key(item: dict[str, Any]) -> str:
            pid = str(item.get("paper_id", ""))
            text_content = str(item.get("text") or item.get("evidence_text") or "")
            normalized = re.sub(r"\s+", " ", text_content.strip().lower())[:80]
            fingerprint = hashlib.md5(normalized.encode('utf-8')).hexdigest()
            return f"{pid}::{fingerprint}"

        for type_name, items in retrieved.items():
            for item in items:
                score = item.get("score", 0.0)
                id_key = _get_id_key(item, type_name)
                content_key = _get_content_key(item)
                
                if id_key and (id_key not in best_scores_id or score > best_scores_id[id_key]):
                    best_scores_id[id_key] = score
                if not id_key and (content_key not in best_scores_content or score > best_scores_content[content_key]):
                    best_scores_content[content_key] = score

        emitted_id_keys: set[str] = set()
        emitted_content_keys: set[str] = set()
        
        for type_name, items in retrieved.items():
            filtered: list[dict[str, Any]] = []
            for item in items:
                score = item.get("score", 0.0)
                id_key = _get_id_key(item, type_name)
                content_key = _get_content_key(item)
                
                if id_key and score < best_scores_id[id_key]:
                    continue
                if not id_key and score < best_scores_content[content_key]:
                    continue
                if id_key and id_key in emitted_id_keys:
                    continue
                if not id_key and content_key in emitted_content_keys:
                    continue
                    
                filtered.append(item)
                if id_key:
                    emitted_id_keys.add(id_key)
                else:
                    emitted_content_keys.add(content_key)
                
            retrieved[type_name] = Retriever._top_k(filtered, limit_per_type)
        return retrieved

    @staticmethod
    def _apply_token_prefilter(query: Any, tokens: set[str], columns: list[Any], *, max_terms: int = 8) -> Any:
        terms = [
            token
            for token in sorted(tokens, key=lambda item: (-len(item), item))
            if len(token) >= 2
        ][:max_terms]
        if not terms:
            return query
        conditions = []
        for token in terms:
            pattern = f"%{token}%"
            conditions.extend(column.ilike(pattern) for column in columns if column is not None)
        if not conditions:
            return query
        return query.where(sa.or_(*conditions))

    def _scalars_with_token_prefilter(
        self,
        query: Any,
        tokens: set[str],
        columns: list[Any],
        *,
        fallback_limit: int,
    ) -> list[Any]:
        filtered_query = self._apply_token_prefilter(query, tokens, columns)
        rows = list(self.session.scalars(filtered_query).all())
        if rows or not tokens:
            return rows
        return list(self.session.scalars(query.limit(fallback_limit)).all())

    @staticmethod
    def _top_k(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        return sorted(items, key=lambda row: row["score"], reverse=True)[:limit]

    @staticmethod
    def _format_dft_result(row: DFTResult) -> str:
        value_part = ""
        if row.value is not None:
            value_part = f"{row.value} {row.unit or ''}".strip()
        parts = [
            row.adsorbate or "DFT result",
            row.property_type or "",
            value_part,
            row.reaction_step or "",
            row.evidence_text or "",
        ]
        return " | ".join(part for part in parts if part)

    @staticmethod
    def _format_electrochemical(row: ElectrochemicalPerformance) -> str:
        parts = []
        if row.capacity_value is not None:
            parts.append(f"capacity {row.capacity_value} mAh/g")
        if row.rate:
            parts.append(f"rate {row.rate}")
        if row.cycle_number is not None:
            parts.append(f"{row.cycle_number} cycles")
        if row.sulfur_loading_mg_cm2 is not None:
            parts.append(f"sulfur loading {row.sulfur_loading_mg_cm2} mg/cm2")
        if row.evidence_text:
            parts.append(row.evidence_text)
        return " | ".join(parts)
