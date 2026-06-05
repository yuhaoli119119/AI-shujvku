from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, ElectrochemicalPerformance, MechanismClaim, PaperChunk, PaperSection, WritingCard, Paper, FigureDataPoint
from app.services.embedding import DeterministicEmbeddingService, get_embedding_service, EmbeddingService
from app.utils.review_safety import is_export_eligible_extraction, writing_card_gate


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_+-]+", (text or "").lower()) if len(token) > 1}


class Retriever:
    """Hybrid lexical + embedding retriever over sections, facts, claims, and writing cards."""

    def __init__(self, session: Session, embedding_dimension: int = 1536, embedding: EmbeddingService | None = None) -> None:
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
        query_embedding = self.embedding.embed_text(query)
        result = {
            "sections": self._retrieve_sections(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "dft_results": self._retrieve_dft_results(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "electrochemical_performance": self._retrieve_electrochemical(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "mechanism_claims": self._retrieve_mechanism_claims(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "writing_cards": self._retrieve_writing_cards(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "figure_data_points": self._retrieve_figure_data(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
        }
        return self._global_dedup(result, limit_per_type)

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
        rows = self.session.scalars(query).all()
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
        rows = self._candidate_chunks(query_embedding, paper_ids, max(limit * 10, 50), paper_type_filter)
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
            return list(self.session.scalars(query).all())

        vector_literal = "[" + ",".join(f"{float(item):.8f}" for item in query_embedding) + "]"
        query_vector = sa.cast(sa.literal(vector_literal), PaperChunk.embedding.type)
        distance = PaperChunk.embedding.op("<=>")(query_vector)
        pg_query = query.where(PaperChunk.embedding.is_not(None)).order_by(distance.asc()).limit(limit)
        try:
            return list(self.session.scalars(pg_query).all())
        except Exception:
            return list(self.session.scalars(query).all())

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
        rows = self.session.scalars(query).all()
        results = []
        for row in rows:
            gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
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
        rows = self.session.scalars(query).all()
        results = []
        for row in rows:
            gate = is_export_eligible_extraction(self.session, row, target_type="electrochemical_performance")
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
        rows = self.session.scalars(query).all()
        results = []
        for row in rows:
            gate = is_export_eligible_extraction(self.session, row, target_type="mechanism_claims")
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
        rows = self.session.scalars(query).all()
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
        rows = self.session.execute(query).all()
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
            fig_suffix = f" (from Figure {fig_caption})" if fig_caption else " (from Figure)"
            evidence_text = f"{row.metric_name}{val_str}{unit_str}{sample_str}{fig_suffix}"

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
        text_embedding = stored_embedding or self.embedding.embed_text(text)
        semantic = max(0.0, self.embedding.cosine_similarity(query_embedding, text_embedding))
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
        """Cross-type dedup: same paper_id + fingerprint keeps the higher-score entry.

        Two-pass approach:
        1. Compute global best score per dedup_key across all types.
        2. Filter each type — remove items superseded by a higher-score
           duplicate in another type, and keep only the first among ties.
        """
        # Pass 1: global best score per dedup_key
        best_scores: dict[str, float] = {}
        for items in retrieved.values():
            for item in items:
                text_content = str(item.get("text") or item.get("evidence_text") or "")
                fingerprint = text_content.strip().lower()[:80]
                dedup_key = f"{item.get('paper_id', '')}::{fingerprint}"
                score = item.get("score", 0.0)
                if dedup_key not in best_scores or score > best_scores[dedup_key]:
                    best_scores[dedup_key] = score

        # Pass 2: filter each type
        emitted_keys: set[str] = set()
        for type_name, items in retrieved.items():
            filtered: list[dict[str, Any]] = []
            for item in items:
                text_content = str(item.get("text") or item.get("evidence_text") or "")
                fingerprint = text_content.strip().lower()[:80]
                dedup_key = f"{item.get('paper_id', '')}::{fingerprint}"
                score = item.get("score", 0.0)
                # Skip if superseded by a higher-score duplicate
                if score < best_scores[dedup_key]:
                    continue
                # Skip if an equal-score duplicate was already emitted
                if dedup_key in emitted_keys:
                    continue
                filtered.append(item)
                emitted_keys.add(dedup_key)
            retrieved[type_name] = Retriever._top_k(filtered, limit_per_type)
        return retrieved

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
