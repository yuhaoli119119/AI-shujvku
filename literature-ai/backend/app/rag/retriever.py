from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample, DFTResult, ElectrochemicalPerformance, EvidenceLocator, EvidenceSpan, MechanismClaim, PaperChunk, PaperFigure, PaperSection, WritingCard, Paper, FigureDataPoint
from app.services.embedding import (
    DeterministicEmbeddingService,
    EmbeddingService,
    EmbeddingUnavailableError,
    get_embedding_service,
)
from app.rag.eligibility import is_rag_eligible, section_is_retrieval_candidate, writing_card_rag_review_status
from app.rag.cards import build_dft_card, build_evidence_card, build_figure_card, build_writing_card, paper_code_for
from app.utils.figure_summary import flatten_figure_key_elements
from app.utils.review_safety import bulk_export_gate_results, writing_card_gate


logger = logging.getLogger(__name__)


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_+-]+", (text or "").lower()) if len(token) > 1}


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Retriever:
    """Hybrid lexical + embedding retriever over sections, facts, claims, and writing cards."""

    def __init__(self, session: Session, embedding_dimension: int = 1024, embedding: EmbeddingService | None = None) -> None:
        self.session = session
        self.embedding = embedding or DeterministicEmbeddingService(embedding_dimension)
        self._semantic_enabled = not isinstance(self.embedding, DeterministicEmbeddingService)
        self._text_embedding_cache: dict[str, list[float]] = {}

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
            # Discovery candidates carry a separate formal-writing gate result.
            "sections": self._retrieve_sections(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "catalyst_samples": self._retrieve_catalyst_samples(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "dft_results": self._retrieve_dft_results(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "electrochemical_performance": self._retrieve_electrochemical(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
            "mechanism_claims": self._retrieve_mechanism_claims(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "writing_cards": self._retrieve_writing_cards(tokens, query_embedding, paper_ids, limit_per_type, paper_type_filter),
            "figure_cards": self._retrieve_figure_cards(tokens, query_embedding, paper_ids, limit_per_type, target_paper_type, paper_type_filter),
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
        chunk_results = self._retrieve_chunks(tokens, query_embedding, paper_ids, max(limit * 2, 4), paper_type_filter)

        query = select(PaperSection)
        if paper_ids:
            query = query.where(PaperSection.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, PaperSection, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [PaperSection.section_title, PaperSection.section_type, PaperSection.text],
            fallback_limit=max(limit * 20, 200),
            include_fallback=bool(query_embedding),
        )
        results = []
        for row in rows:
            if not section_is_retrieval_candidate(self.session, row):
                continue
            formal_eligible = is_rag_eligible(self.session, row, "section")
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
                    "section_id": row.id,
                    "score": score,
                    "score_breakdown": score_info,
                    "text": text[:1200],
                    "section_title": row.section_title,
                    "section_type": row.section_type,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "retrieval_tier": "formal_evidence" if formal_eligible else "discovery_candidate",
                    "can_use_for_writing": formal_eligible,
                }
            )
        return self._merge_section_results(chunk_results, results, limit)

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
            if not section_is_retrieval_candidate(self.session, row):
                continue
            formal_eligible = is_rag_eligible(self.session, row, "chunk")
            section = self.session.get(PaperSection, row.section_id) if row.section_id else None
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
                    "section_title": getattr(section, "section_title", None) or "Chunk",
                    "section_type": getattr(section, "section_type", None) or "chunk",
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "embedding_model": row.embedding_model,
                    "embedding_dimension": row.embedding_dimension,
                    "chunk_index": row.chunk_index,
                    "retrieval_tier": "formal_evidence" if formal_eligible else "discovery_candidate",
                    "can_use_for_writing": formal_eligible,
                }
            )
        return self._top_k(results, limit)

    @staticmethod
    def _merge_section_results(
        chunks: list[dict[str, Any]], sections: list[dict[str, Any]], limit: int,
    ) -> list[dict[str, Any]]:
        """Prefer precise chunks while retaining full-section boundary recall."""

        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, Any, Any, str]] = set()
        chunks_per_section: dict[tuple[str, str], int] = {}
        for item in sorted(chunks + sections, key=lambda row: row["score"], reverse=True):
            paper_key = str(item.get("paper_id") or "")
            section_key = str(item.get("section_id") or item.get("object_id") or "")
            group = (paper_key, section_key)
            is_chunk = item.get("chunk_index") is not None
            if is_chunk and chunks_per_section.get(group, 0) >= 2:
                continue
            text_key = re.sub(r"\s+", " ", str(item.get("text") or "").strip().lower())[:160]
            key = (paper_key, section_key, item.get("page_start"), item.get("page_end"), text_key)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if is_chunk:
                chunks_per_section[group] = chunks_per_section.get(group, 0) + 1
        return sorted(merged, key=lambda row: row["score"], reverse=True)[:limit]

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

    def _retrieve_catalyst_samples(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(CatalystSample)
        if paper_ids:
            query = query.where(CatalystSample.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, CatalystSample, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [
                CatalystSample.name,
                CatalystSample.catalyst_type,
                CatalystSample.coordination,
                CatalystSample.support,
                CatalystSample.synthesis_method,
                CatalystSample.evidence_strength,
            ],
            fallback_limit=max(limit * 20, 200),
            include_fallback=bool(query_embedding),
        )
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="catalyst_samples")
        results = []
        for row in rows:
            gate = gate_by_id[str(row.id)]
            if not gate.eligible:
                continue
            haystack = self._format_catalyst_sample(row)
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue

            locator = self._primary_evidence_locator(row, target_type="catalyst_samples")
            locator_page = locator.get("page") if isinstance(locator, dict) else None
            evidence_text = row.evidence_strength or (locator or {}).get("evidence_text") or haystack
            results.append(
                {
                    "type": "catalyst_sample",
                    **build_evidence_card(
                        self.session,
                        source_type="catalyst_sample",
                        source_id=row.id,
                        paper_id=row.paper_id,
                        evidence_text=evidence_text,
                        review_status=gate.review_status,
                        page=locator_page,
                    ),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
                    "score_breakdown": score_info,
                    "text": haystack,
                    "name": row.name,
                    "catalyst_type": row.catalyst_type,
                    "metal_centers": row.metal_centers or [],
                    "coordination": row.coordination,
                    "support": row.support,
                    "synthesis_method": row.synthesis_method,
                    "evidence_strength": row.evidence_strength,
                    "evidence_text": evidence_text,
                    "page_start": locator_page,
                    "page_end": locator_page,
                    "evidence_locator": locator,
                    "provenance_level": gate.provenance_level,
                    "locator_status": gate.locator_status,
                }
            )
        return self._top_k(results, limit)

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
            include_fallback=bool(query_embedding),
        )
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        results = []
        for row in rows:
            gate = gate_by_id[str(row.id)]
            if not gate.eligible or not is_rag_eligible(self.session, row, "dft_result"):
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
            locator = self._primary_evidence_locator(row, target_type="dft_results")
            locator_page = locator.get("page") if isinstance(locator, dict) else None

            results.append(
                {
                    "type": "dft_result",
                    **build_dft_card(
                        self.session,
                        row,
                        text=text,
                        gate=gate,
                        page=locator_page,
                    ),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
                    "score_breakdown": score_info,
                    "text": text,
                    "material_identity": self._material_identity(row),
                    "property_type": row.property_type,
                    "energy_type": (row.evidence_payload or {}).get("energy_type") if isinstance(row.evidence_payload, dict) else None,
                    "adsorbate": row.adsorbate,
                    "value": row.value,
                    "unit": row.unit,
                    "source_section": row.source_section,
                    "source_figure": row.source_figure,
                    "page_start": locator_page,
                    "page_end": locator_page,
                    "evidence_text": row.evidence_text,
                    "evidence_locator": locator,
                    "provenance_level": gate.provenance_level,
                    "locator_status": gate.locator_status,
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
            include_fallback=bool(query_embedding),
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
            locator = self._primary_evidence_locator(row, target_type="electrochemical_performance")
            locator_page = locator.get("page") if isinstance(locator, dict) else None

            results.append(
                {
                    "type": "electrochemical_performance",
                    **build_evidence_card(
                        self.session,
                        source_type="electrochemical_performance",
                        source_id=row.id,
                        paper_id=row.paper_id,
                        evidence_text=row.evidence_text or haystack,
                        review_status=gate.review_status,
                        page=locator_page,
                    ),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
                    "score_breakdown": score_info,
                    "text": haystack,
                    "capacity_value": row.capacity_value,
                    "rate": row.rate,
                    "cycle_number": row.cycle_number,
                    "evidence_text": row.evidence_text,
                    "page_start": locator_page,
                    "page_end": locator_page,
                    "evidence_locator": locator,
                    "provenance_level": gate.provenance_level,
                    "locator_status": gate.locator_status,
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
            include_fallback=bool(query_embedding),
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
            locator = self._primary_evidence_locator(row, target_type="mechanism_claims")
            locator_page = locator.get("page") if isinstance(locator, dict) else None
            results.append(
                {
                    "type": "mechanism_claim",
                    **build_evidence_card(
                        self.session,
                        source_type="mechanism_claim",
                        source_id=row.id,
                        paper_id=row.paper_id,
                        evidence_text=row.evidence_text or row.claim_text,
                        review_status=gate.review_status,
                        page=locator_page,
                    ),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
                    "score_breakdown": score_info,
                    "text": row.claim_text,
                    "claim_type": row.claim_type,
                    "evidence_text": row.evidence_text,
                    "evidence_types": row.evidence_types or [],
                    "page_start": locator_page,
                    "page_end": locator_page,
                    "evidence_locator": locator,
                    "provenance_level": gate.provenance_level,
                    "locator_status": gate.locator_status,
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
            include_fallback=bool(query_embedding),
        )
        results = []
        for row in rows:
            gate = writing_card_gate(row)
            if not is_rag_eligible(self.session, row, "writing_card"):
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
                    **build_writing_card(
                        self.session,
                        row,
                        evidence_text=row.research_gap or row.proposed_solution or row.core_hypothesis or "",
                        gate=gate,
                        review_status=writing_card_rag_review_status(self.session, row),
                    ),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
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

    def _retrieve_figure_cards(
        self,
        tokens: set[str],
        query_embedding: list[float],
        paper_ids: list[UUID] | None,
        limit: int,
        target_paper_type: str | None = None,
        paper_type_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(PaperFigure)
        if paper_ids:
            query = query.where(PaperFigure.paper_id.in_(paper_ids))
        query = self._apply_type_filter(query, PaperFigure, paper_type_filter)
        rows = self._scalars_with_token_prefilter(
            query,
            tokens,
            [
                PaperFigure.caption,
                PaperFigure.figure_label,
                PaperFigure.figure_role,
                PaperFigure.content_summary,
            ],
            fallback_limit=max(limit * 20, 200),
            include_fallback=bool(query_embedding),
        )
        results = []
        for row in rows:
            if not is_rag_eligible(self.session, row, "figure"):
                continue
            haystack = " ".join(
                filter(
                    None,
                    [
                        row.figure_label,
                        row.figure_role,
                        row.content_summary,
                        row.caption,
                        " ".join(flatten_figure_key_elements(row.key_elements)),
                    ],
                )
            )
            score, score_info = self._hybrid_score(tokens, query_embedding, haystack, None, allow_paper_fallback=bool(paper_ids))
            if score <= 0:
                continue
            caption = row.caption or ""
            summary = row.content_summary or caption
            evidence_text = caption or summary
            results.append(
                {
                    "type": "figure_card",
                    **build_figure_card(self.session, row, evidence_text=evidence_text),
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
                    "score_breakdown": score_info,
                    "text": summary,
                    "figure_label": row.figure_label,
                    "figure_role": row.figure_role,
                    "caption": caption,
                    "page": row.page,
                    "page_start": row.page,
                    "page_end": row.page,
                    "image_path": row.image_path,
                    "asset_url": f"/api/papers/assets/{row.image_path}" if row.image_path else None,
                    "content_summary": row.content_summary,
                    "key_elements": flatten_figure_key_elements(row.key_elements),
                    "evidence_locator": {
                        "page": row.page,
                        "figure": row.figure_label or caption,
                        "locator_status": "exact_page" if row.page is not None else "caption_only",
                    },
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
        query = select(FigureDataPoint, PaperFigure.caption, PaperFigure.page).outerjoin(
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
        if query_embedding and tokens:
            seen = {(result_row[0].id if result_row and result_row[0] is not None else None) for result_row in rows}
            fallback_rows = self.session.execute(query.limit(max(limit * 20, 200))).all()
            rows.extend(
                result_row
                for result_row in fallback_rows
                if (result_row[0].id if result_row and result_row[0] is not None else None) not in seen
            )
        elif not rows and tokens:
            rows = self.session.execute(query.limit(max(limit * 20, 200))).all()
        results = []
        for result_row in rows:
            row, caption = result_row[0], result_row[1]
            figure_page = result_row[2] if len(result_row) > 2 else None
            if not is_rag_eligible(self.session, row, "figure_data_point"):
                continue
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

            results.append(
                {
                    "type": "figure_data_point",
                    "source_type": "figure_data_point",
                    "source_id": str(row.id),
                    "paper_code": paper_code_for(self.session, row.paper_id),
                    "page": figure_page,
                    "review_status": "safe_verified_or_reliable_figure",
                    "paper_id": row.paper_id,
                    "object_id": row.id,
                    "score": round(score, 4),
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
        effective_embedding = stored_embedding if self._semantic_enabled else None
        if not effective_embedding and query_embedding and self._semantic_enabled:
            effective_embedding = self._safe_text_embedding(text)
        if query_embedding and effective_embedding:
            semantic = max(0.0, self.embedding.cosine_similarity(query_embedding, effective_embedding))
        if lexical <= 0 and semantic <= 0 and allow_paper_fallback:
            lexical = 0.05
        if lexical <= 0 and semantic <= 0:
            return 0.0, {"lexical": 0.0, "semantic": 0.0, "hybrid": 0.0}
        hybrid = round((0.65 * lexical) + (0.35 * semantic), 4) if semantic > 0 else round(lexical, 4)
        return hybrid, {"lexical": round(lexical, 4), "semantic": round(semantic, 4), "hybrid": hybrid}

    def _safe_text_embedding(self, text: str) -> list[float]:
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        if not normalized:
            return []
        cached = self._text_embedding_cache.get(normalized)
        if cached is not None:
            return cached
        if not self._semantic_enabled:
            return []
        try:
            raw_embedding = self.embedding.embed_text(normalized)
            embedding = raw_embedding if isinstance(raw_embedding, list) else []
        except EmbeddingUnavailableError as exc:
            logger.warning("Embedding unavailable for structured retrieval row; using lexical score only: %s", exc)
            embedding = []
        except Exception as exc:
            logger.warning("Embedding failed for structured retrieval row; using lexical score only: %s", exc)
            embedding = []
        self._text_embedding_cache[normalized] = embedding
        return embedding

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
            pattern = f"%{_escape_like(token)}%"
            conditions.extend(column.ilike(pattern, escape="\\") for column in columns if column is not None)
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
        include_fallback: bool = False,
    ) -> list[Any]:
        filtered_query = self._apply_token_prefilter(query, tokens, columns)
        rows = list(self.session.scalars(filtered_query).all())
        if include_fallback and tokens:
            seen = {getattr(row, "id", None) for row in rows}
            fallback_rows = list(self.session.scalars(query.limit(fallback_limit)).all())
            rows.extend(row for row in fallback_rows if getattr(row, "id", None) not in seen)
            return rows
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
    def _format_catalyst_sample(row: CatalystSample) -> str:
        metal_centers = ", ".join(str(item) for item in (row.metal_centers or []) if item)
        parts = [
            row.name,
            row.catalyst_type,
            f"metal centers {metal_centers}" if metal_centers else "",
            f"coordination {row.coordination}" if row.coordination else "",
            f"support {row.support}" if row.support else "",
            row.synthesis_method,
            row.evidence_strength,
        ]
        return " | ".join(part for part in parts if part)

    def _material_identity(self, row: DFTResult) -> dict[str, Any] | None:
        if row.catalyst_sample_id is None:
            return None
        catalyst = self.session.get(CatalystSample, row.catalyst_sample_id)
        if catalyst is None:
            return {"catalyst_sample_id": str(row.catalyst_sample_id)}
        return {
            "catalyst_sample_id": str(catalyst.id),
            "name": catalyst.name,
            "catalyst_type": catalyst.catalyst_type,
            "metal_centers": catalyst.metal_centers or [],
            "coordination": catalyst.coordination,
            "support": catalyst.support,
        }

    def _primary_evidence_locator(self, row: Any, *, target_type: str) -> dict[str, Any] | None:
        target_types = {
            target_type,
            target_type.rstrip("s"),
            "DFTResult" if target_type == "dft_results" else target_type,
        }
        target_id = str(row.id)
        locators = list(
            self.session.scalars(
                select(EvidenceLocator).where(
                    EvidenceLocator.paper_id == row.paper_id,
                    EvidenceLocator.target_id == target_id,
                    EvidenceLocator.target_type.in_(target_types),
                )
            ).all()
        )
        locators.sort(key=lambda item: (item.page is None, item.page or 999999, str(item.id)))
        if locators:
            locator = locators[0]
            return {
                "page": locator.page,
                "bbox": locator.bbox,
                "section": locator.section,
                "figure_id": str(locator.figure_id) if locator.figure_id else None,
                "target_type": locator.target_type,
                "field_name": locator.field_name,
                "evidence_text": locator.evidence_text,
                "locator_status": locator.locator_status,
                "locator_confidence": locator.locator_confidence,
            }
        span = self.session.scalars(
            select(EvidenceSpan).where(
                EvidenceSpan.paper_id == row.paper_id,
                EvidenceSpan.object_id == target_id,
                EvidenceSpan.object_type.in_(target_types),
                EvidenceSpan.text.is_not(None),
                EvidenceSpan.text != "",
            )
        ).first()
        if span is not None:
            return {
                "page": span.page,
                "section": span.section,
                "figure": span.figure,
                "table": span.table,
                "evidence_text": span.text,
                "locator_status": "exact_page" if span.page is not None else "text_only",
            }
        return None

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
