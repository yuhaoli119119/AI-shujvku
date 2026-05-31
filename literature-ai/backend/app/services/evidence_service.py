from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceSpan,
    MechanismClaim,
    Paper,
    PaperSection,
    WritingCard,
)
from app.schemas.evidence import (
    CitationAuditItem,
    CitationAuditResponse,
    ClaimEvidence,
    EvidenceClaimCreate,
    EvidenceRef,
    PageSpan,
)
from app.services.evidence_locator_service import EvidenceLocatorService


TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_+\-.]+")
SENTENCE_PATTERN = re.compile(r"(?<=[.!?。！？])\s+|[\n\r]+")


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(text or "") if len(token) > 1}


class EvidenceService:
    """Builds the shared claim-to-evidence shape used by answers, drafts, and extraction."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.locators = EvidenceLocatorService(session)

    def create_claim(self, payload: EvidenceClaimCreate) -> ClaimEvidence:
        ev = payload.evidence
        row = EvidenceClaim(
            claim_text=payload.claim_text,
            source_type=payload.source_type,
            target_type=payload.target_type,
            target_id=payload.target_id,
            paper_id=ev.paper_id,
            chunk_id=ev.chunk_id,
            section_id=ev.section_id,
            page_start=ev.page_span.page_start,
            page_end=ev.page_span.page_end,
            span_start=ev.page_span.span_start,
            span_end=ev.page_span.span_end,
            evidence_text=ev.evidence_text,
            confidence=ev.confidence,
            validation_status=payload.validation_status,
            meta=payload.metadata,
        )
        self.session.add(row)
        self.session.flush()
        try:
            self.locators.create_locator_for_claim(row, ev)
            self.session.commit()
            self.session.refresh(row)
            return self._claim_row_to_schema(row)
        except Exception:
            self.session.rollback()
            raise

    def list_claims(
        self,
        *,
        paper_id: UUID | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        include_derived: bool = True,
        limit: int = 100,
    ) -> list[ClaimEvidence]:
        stmt = select(EvidenceClaim).order_by(EvidenceClaim.created_at.desc()).limit(limit)
        if paper_id:
            stmt = stmt.where(EvidenceClaim.paper_id == paper_id)
        if target_type:
            stmt = stmt.where(EvidenceClaim.target_type == target_type)
        if target_id:
            stmt = stmt.where(EvidenceClaim.target_id == target_id)
        claims = [self._claim_row_to_schema(row) for row in self.session.scalars(stmt).all()]
        if include_derived:
            claims.extend(self._derived_claims(paper_id=paper_id, target_type=target_type, target_id=target_id))
        return claims[:limit]

    def evidence_refs_for_papers(self, paper_ids: list[UUID] | None = None, limit: int = 500) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = []

        span_stmt = select(EvidenceSpan).order_by(EvidenceSpan.id.asc())
        if paper_ids:
            span_stmt = span_stmt.where(EvidenceSpan.paper_id.in_(paper_ids))
        for span in self.session.scalars(span_stmt.limit(limit)).all():
            refs.append(
                EvidenceRef(
                    paper_id=span.paper_id,
                    chunk_id=span.object_id,
                    page_span=PageSpan(page_start=span.page, page_end=span.page),
                    evidence_text=span.text,
                    confidence=span.confidence,
                    source="evidence_span",
                    section_title=span.section,
                    target_type=span.object_type,
                    target_id=span.object_id,
                    locator=self.locators.resolve_field_locator(
                        paper_id=span.paper_id,
                        target_type=span.object_type,
                        target_id=span.object_id,
                        field_name="evidence_text",
                        evidence_text=span.text,
                        source_section=span.section,
                        page_span=PageSpan(page_start=span.page, page_end=span.page),
                    ),
                )
            )

        if len(refs) < limit:
            refs.extend(self._fallback_refs_from_sections(paper_ids, limit - len(refs)))
        return refs[:limit]

    def audit_text(
        self,
        text: str,
        *,
        paper_ids: list[UUID] | None = None,
        evidence: list[EvidenceRef] | None = None,
        min_confidence: float = 0.25,
    ) -> CitationAuditResponse:
        claims = self._split_claims(text)
        refs = list(evidence or [])
        if not refs:
            refs = self.evidence_refs_for_papers(paper_ids or None)

        results: list[CitationAuditItem] = []
        supported = 0
        for claim in claims:
            matches = self._match_evidence(claim, refs, min_confidence=min_confidence)
            if matches:
                supported += 1
                results.append(CitationAuditItem(claim_text=claim, status="supported", evidence=matches[:3]))
            else:
                results.append(
                    CitationAuditItem(
                        claim_text=claim,
                        status="unsupported",
                        evidence=[],
                        warning="No evidence link reached the confidence or lexical-overlap threshold.",
                    )
                )
        return CitationAuditResponse(
            ok=supported == len(claims),
            total_claims=len(claims),
            supported_claims=supported,
            unsupported_claims=len(claims) - supported,
            claims=results,
        )

    def claims_from_generated_sections(
        self,
        sections: dict[str, Any],
        retrieved: dict[str, list[dict[str, Any]]],
    ) -> list[ClaimEvidence]:
        refs = self._refs_from_retrieved(retrieved)
        output: list[ClaimEvidence] = []
        for section_name, value in sections.items():
            if isinstance(value, list):
                candidate_text = ". ".join(str(item) for item in value)
            elif isinstance(value, str):
                candidate_text = value
            else:
                continue
            for claim in self._split_claims(candidate_text):
                evidence = self._match_evidence(claim, refs, min_confidence=0.1)[:3]
                output.append(
                    ClaimEvidence(
                        claim_text=claim,
                        source_type="writer",
                        target_type=section_name,
                        evidence=evidence,
                        confidence=max([ev.confidence or 0.0 for ev in evidence], default=0.0),
                        validation_status="supported" if evidence else "unsupported",
                    )
                )
        return output

    def _claim_row_to_schema(self, row: EvidenceClaim) -> ClaimEvidence:
        evidence = EvidenceRef(
            paper_id=row.paper_id,
            chunk_id=row.chunk_id,
            section_id=row.section_id,
            page_span=PageSpan(
                page_start=row.page_start,
                page_end=row.page_end,
                span_start=row.span_start,
                span_end=row.span_end,
            ),
            evidence_text=row.evidence_text,
            confidence=row.confidence,
            source="evidence_claim",
            target_type=row.target_type,
            target_id=row.target_id,
            locator=self.locators.get_claim_locator(row.id),
        )
        return ClaimEvidence(
            id=row.id,
            claim_text=row.claim_text,
            source_type=row.source_type,
            target_type=row.target_type,
            target_id=row.target_id,
            evidence=[evidence],
            confidence=row.confidence,
            validation_status=row.validation_status,
            metadata=row.meta or {},
        )

    def _derived_claims(
        self,
        *,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
    ) -> list[ClaimEvidence]:
        derived: list[ClaimEvidence] = []

        if target_type and target_type not in {"dft_result", "mechanism_claim", "electrochemical_performance", "writing_card"}:
            return derived

        valid_target_uuid = None
        if target_id:
            try:
                valid_target_uuid = UUID(target_id)
            except ValueError:
                return []

        dft_stmt = select(DFTResult)
        mech_stmt = select(MechanismClaim)
        perf_stmt = select(ElectrochemicalPerformance)
        card_stmt = select(WritingCard)

        if paper_id:
            dft_stmt = dft_stmt.where(DFTResult.paper_id == paper_id)
            mech_stmt = mech_stmt.where(MechanismClaim.paper_id == paper_id)
            perf_stmt = perf_stmt.where(ElectrochemicalPerformance.paper_id == paper_id)
            card_stmt = card_stmt.where(WritingCard.paper_id == paper_id)
        if valid_target_uuid:
            dft_stmt = dft_stmt.where(DFTResult.id == valid_target_uuid)
            mech_stmt = mech_stmt.where(MechanismClaim.id == valid_target_uuid)
            perf_stmt = perf_stmt.where(ElectrochemicalPerformance.id == valid_target_uuid)
            card_stmt = card_stmt.where(WritingCard.id == valid_target_uuid)

        if not target_type or target_type == "dft_result":
            for row in self.session.scalars(dft_stmt).all():
                if target_id and str(row.id) != target_id:
                    continue
                text = self._format_dft_claim(row)
                if text:
                    derived.append(self._derived_claim(text, "dft_result", row.id, row.paper_id, row.evidence_text, row.confidence, row.source_section))

        if not target_type or target_type == "mechanism_claim":
            for row in self.session.scalars(mech_stmt).all():
                if target_id and str(row.id) != target_id:
                    continue
                derived.append(self._derived_claim(row.claim_text, "mechanism_claim", row.id, row.paper_id, row.evidence_text, row.confidence, None))

        if not target_type or target_type == "electrochemical_performance":
            for row in self.session.scalars(perf_stmt).all():
                if target_id and str(row.id) != target_id:
                    continue
                text = self._format_perf_claim(row)
                if text:
                    derived.append(self._derived_claim(text, "electrochemical_performance", row.id, row.paper_id, row.evidence_text, None, None))

        if not target_type or target_type == "writing_card":
            for row in self.session.scalars(card_stmt).all():
                if target_id and str(row.id) != target_id:
                    continue
                text = row.core_hypothesis or row.research_gap or row.proposed_solution
                ev_text = None
                chain = row.evidence_chain if isinstance(row.evidence_chain, list) else []
                if chain:
                    first = chain[0]
                    ev_text = first.get("text") if isinstance(first, dict) else str(first)
                if text:
                    derived.append(self._derived_claim(text, "writing_card", row.id, row.paper_id, ev_text, 0.7, None))

        return derived

    def _derived_claim(
        self,
        claim_text: str,
        target_type: str,
        target_id: Any,
        paper_id: UUID,
        evidence_text: str | None,
        confidence: float | None,
        source_section: str | None,
    ) -> ClaimEvidence:
        ev_text = evidence_text or claim_text
        return ClaimEvidence(
            claim_text=claim_text,
            source_type="derived",
            target_type=target_type,
            target_id=str(target_id),
            evidence=[
                EvidenceRef(
                    paper_id=paper_id,
                    chunk_id=str(target_id),
                    evidence_text=ev_text,
                    confidence=confidence,
                    source=target_type,
                    section_title=source_section,
                    target_type=target_type,
                    target_id=str(target_id),
                    locator=self.locators.resolve_field_locator(
                        paper_id=paper_id,
                        target_type=target_type,
                        target_id=str(target_id),
                        field_name="evidence_text",
                        evidence_text=ev_text,
                        source_section=source_section,
                        page_span=PageSpan(),
                    ),
                )
            ],
            confidence=confidence,
            validation_status="supported" if ev_text else "unsupported",
        )

    def _fallback_refs_from_sections(self, paper_ids: list[UUID] | None, limit: int) -> list[EvidenceRef]:
        stmt = select(PaperSection).order_by(PaperSection.page_start.asc().nulls_last())
        if paper_ids:
            stmt = stmt.where(PaperSection.paper_id.in_(paper_ids))
        refs = []
        for row in self.session.scalars(stmt.limit(limit)).all():
            text = (row.text or "").strip()
            if not text:
                continue
            refs.append(
                EvidenceRef(
                    paper_id=row.paper_id,
                    chunk_id=str(row.id),
                    section_id=row.id,
                    page_span=PageSpan(page_start=row.page_start, page_end=row.page_end),
                    evidence_text=text[:1200],
                    confidence=0.45,
                    source="paper_section",
                    section_title=row.section_title,
                    target_type="section",
                    target_id=str(row.id),
                    locator=self.locators.resolve_field_locator(
                        paper_id=row.paper_id,
                        target_type="section",
                        target_id=str(row.id),
                        field_name="evidence_text",
                        evidence_text=text[:1200],
                        source_section=row.section_title,
                        page_span=PageSpan(page_start=row.page_start, page_end=row.page_end),
                    ),
                )
            )
        return refs

    @staticmethod
    def _format_dft_claim(row: DFTResult) -> str:
        value = f"{row.value} {row.unit or ''}".strip() if row.value is not None else ""
        parts = [row.adsorbate, row.property_type, value, row.reaction_step]
        return " ".join(str(part) for part in parts if part).strip()

    @staticmethod
    def _format_perf_claim(row: ElectrochemicalPerformance) -> str:
        parts = []
        if row.capacity_value is not None:
            parts.append(f"capacity {row.capacity_value} mAh/g")
        if row.rate:
            parts.append(f"rate {row.rate}")
        if row.cycle_number is not None:
            parts.append(f"{row.cycle_number} cycles")
        if row.sulfur_loading_mg_cm2 is not None:
            parts.append(f"sulfur loading {row.sulfur_loading_mg_cm2} mg/cm2")
        return " ".join(parts)

    @staticmethod
    def _split_claims(text: str) -> list[str]:
        claims = []
        for part in SENTENCE_PATTERN.split(text or ""):
            cleaned = re.sub(r"\s+", " ", part).strip(" -;\t")
            if len(cleaned) >= 18:
                claims.append(cleaned)
        return claims

    @staticmethod
    def _match_evidence(claim: str, refs: list[EvidenceRef], *, min_confidence: float) -> list[EvidenceRef]:
        claim_tokens = tokenize(claim)
        if not claim_tokens:
            return []
        ranked: list[tuple[float, EvidenceRef]] = []
        for ref in refs:
            if (ref.confidence or 0.0) < min_confidence:
                continue
            evidence_tokens = tokenize(ref.evidence_text)
            if not evidence_tokens:
                continue
            overlap = claim_tokens & evidence_tokens
            score = len(overlap) / max(1, min(len(claim_tokens), 12))
            if score >= 0.22 or (len(overlap) >= 2 and any(char.isdigit() for char in claim)):
                adjusted = score + min(ref.confidence or 0.0, 1.0) * 0.15
                ranked.append((adjusted, ref))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [ref for _, ref in ranked]

    @staticmethod
    def _refs_from_retrieved(retrieved: dict[str, list[dict[str, Any]]]) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = []
        for source, items in (retrieved or {}).items():
            for item in items:
                text = item.get("evidence_text") or item.get("text") or ""
                if not text:
                    continue
                refs.append(
                    EvidenceRef(
                        paper_id=item.get("paper_id"),
                        chunk_id=str(item.get("object_id") or item.get("chunk_id") or ""),
                        section_id=item.get("section_id"),
                        page_span=PageSpan(page_start=item.get("page_start"), page_end=item.get("page_end")),
                        evidence_text=text,
                        confidence=item.get("confidence") or item.get("score"),
                        source=source,
                        section_title=item.get("section_title") or item.get("source_section"),
                        target_type=item.get("type"),
                        target_id=str(item.get("object_id") or ""),
                        bbox=item.get("bbox"),
                        parser_source=item.get("parser_source") or "unknown",
                    )
                )
        return refs
