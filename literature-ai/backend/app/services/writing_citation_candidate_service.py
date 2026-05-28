from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
    PaperSection,
    WritingCard,
)
from app.utils.review_safety import is_safe_verified_review


TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]+")
STOPWORDS = {
    "and",
    "are",
    "can",
    "for",
    "from",
    "has",
    "have",
    "into",
    "that",
    "the",
    "their",
    "this",
    "with",
    "within",
}
PRIORITY_BOOST = {"high": 0.18, "medium": 0.04, "low": -0.06}
TARGET_TYPE_BY_MODEL = {
    DFTResult: "dft_results",
    DFTSetting: "dft_settings",
    CatalystSample: "catalyst_samples",
    ElectrochemicalPerformance: "electrochemical_performance",
    MechanismClaim: "mechanism_claims",
    WritingCard: "writing_cards",
}


@dataclass(frozen=True)
class CitationCandidateFilters:
    year_min: int | None = None
    year_max: int | None = None
    impact_factor_min: float | None = None
    impact_factor_max: float | None = None
    journal_include: tuple[str, ...] = ()
    journal_exclude: tuple[str, ...] = ()
    needs_metadata: bool | None = None
    has_pdf: bool | None = None
    has_parsed_text: bool | None = None
    has_extraction_output: bool | None = None
    has_verified_evidence: bool | None = None
    has_safe_verified_evidence: bool | None = None
    citation_priority: str | None = None


@dataclass(frozen=True)
class CitationCandidateRequest:
    text: str
    max_candidates: int = 10
    filters: CitationCandidateFilters = field(default_factory=CitationCandidateFilters)
    include_unverified_suggestions: bool = True
    include_pending_review: bool = True


@dataclass
class Snippet:
    text: str
    source: str
    page: int | None = None
    locator_status: str = "missing"
    verified: bool = False
    safe_verified: bool = False
    matched_tokens: set[str] = field(default_factory=set)

    def response(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "page": self.page,
            "locator_status": self.locator_status,
            "verified": self.verified,
            "safe_verified": self.safe_verified,
        }


class WritingCitationCandidateService:
    """Read-only deterministic citation candidate recommendation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def recommend(self, request: CitationCandidateRequest) -> dict[str, Any]:
        query_tokens = tokenize(request.text)
        if len(query_tokens) < 2:
            raise ValueError("text must contain at least two searchable terms")

        papers = list(self.session.scalars(select(Paper)).all())
        eligibility = {row.paper_id: row for row in self.session.scalars(select(PaperCitationEligibility)).all()}
        impacts = {row.paper_id: row for row in self.session.scalars(select(PaperImpactMetadata)).all()}
        reviews = _group_by_paper(self.session.scalars(select(ExtractionFieldReview)).all())
        locators = _group_by_paper(self.session.scalars(select(EvidenceLocator)).all())
        evidence_claims = _group_by_paper(self.session.scalars(select(EvidenceClaim)).all())
        sections = _group_by_paper(self.session.scalars(select(PaperSection)).all())
        extraction_rows = self._extraction_rows_by_paper()

        candidates: list[dict[str, Any]] = []
        excluded_reasons: list[dict[str, Any]] = []
        needs_metadata_excluded = 0
        for paper in papers:
            row_eligibility = eligibility.get(paper.id)
            row_impact = impacts.get(paper.id)
            excluded_reason = self._exclude_reason(paper, row_eligibility, row_impact, request.filters)
            if excluded_reason:
                excluded_reasons.append({"paper_id": str(paper.id), "reason": excluded_reason})
                if excluded_reason == "needs_metadata_excluded_by_impact_factor_min":
                    needs_metadata_excluded += 1
                continue

            snippets = self._snippets(
                paper=paper,
                query_tokens=query_tokens,
                reviews=reviews.get(paper.id, []),
                locators=locators.get(paper.id, []),
                evidence_claims=evidence_claims.get(paper.id, []),
                sections=sections.get(paper.id, []),
                extraction_rows=extraction_rows.get(paper.id, []),
            )
            if not snippets:
                continue

            evidence_status = self._evidence_status(
                snippets=snippets,
                reviews=reviews.get(paper.id, []),
                locators=locators.get(paper.id, []),
                extraction_rows=extraction_rows.get(paper.id, []),
            )
            if evidence_status.startswith("pending") and not request.include_pending_review:
                excluded_reasons.append({"paper_id": str(paper.id), "reason": "pending_review_excluded"})
                continue
            if evidence_status in {"unverified_extraction", "metadata_only"} and not request.include_unverified_suggestions:
                excluded_reasons.append({"paper_id": str(paper.id), "reason": "unverified_suggestion_excluded"})
                continue

            feature_flags = self._feature_flags(
                paper=paper,
                reviews=reviews.get(paper.id, []),
                evidence_claims=evidence_claims.get(paper.id, []),
                sections=sections.get(paper.id, []),
                extraction_rows=extraction_rows.get(paper.id, []),
            )
            mismatch = self._feature_mismatch(feature_flags, request.filters)
            if mismatch:
                excluded_reasons.append({"paper_id": str(paper.id), "reason": mismatch})
                continue

            candidate = self._candidate_response(
                paper=paper,
                eligibility=row_eligibility,
                impact=row_impact,
                snippets=snippets,
                evidence_status=evidence_status,
                filters=request.filters,
            )
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                -item["recommendation_score"],
                item["citation_priority"] != "high",
                -(item["year"] or 0),
                item["title"] or "",
            )
        )
        candidates = candidates[: request.max_candidates]
        return {
            "query_text": request.text,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "excluded_count": len(excluded_reasons),
            "excluded_reasons": excluded_reasons,
            "warnings": _warnings(needs_metadata_excluded),
            "safety": {
                "read_only": True,
                "writes_db": False,
                "marks_verified": False,
                "unlocks_export_or_writing": False,
                "generates_bibliography": False,
            },
        }

    def _exclude_reason(
        self,
        paper: Paper,
        eligibility: PaperCitationEligibility | None,
        impact: PaperImpactMetadata | None,
        filters: CitationCandidateFilters,
    ) -> str | None:
        if eligibility and eligibility.exclude_from_citation:
            return "exclude_from_citation=true"
        priority = eligibility.citation_priority if eligibility else "medium"
        if priority == "exclude":
            return "citation_priority=exclude"
        if filters.citation_priority is not None and priority != filters.citation_priority:
            return "citation_priority_filter_mismatch"
        if filters.year_min is not None and (paper.year is None or paper.year < filters.year_min):
            return "year_below_min"
        if filters.year_max is not None and (paper.year is None or paper.year > filters.year_max):
            return "year_above_max"
        journal = (paper.journal or "").casefold()
        if filters.journal_include and not any(term.casefold() in journal for term in filters.journal_include):
            return "journal_include_filter_mismatch"
        if any(term.casefold() in journal for term in filters.journal_exclude):
            return "journal_exclude_filter_match"
        if filters.impact_factor_min is not None:
            if impact is None or impact.impact_factor is None:
                return "needs_metadata_excluded_by_impact_factor_min"
            if impact.impact_factor < filters.impact_factor_min:
                return "impact_factor_below_min"
        if filters.impact_factor_max is not None:
            if impact is None or impact.impact_factor is None:
                return "needs_metadata_excluded_by_impact_factor_max"
            if impact.impact_factor > filters.impact_factor_max:
                return "impact_factor_above_max"
        if filters.needs_metadata is not None and ((impact is None or impact.impact_factor is None) is not filters.needs_metadata):
            return "needs_metadata_filter_mismatch"
        return None

    def _snippets(
        self,
        *,
        paper: Paper,
        query_tokens: set[str],
        reviews: list[ExtractionFieldReview],
        locators: list[EvidenceLocator],
        evidence_claims: list[EvidenceClaim],
        sections: list[PaperSection],
        extraction_rows: list[Any],
    ) -> list[Snippet]:
        snippets: list[Snippet] = []
        self._add_if_matching(snippets, paper.title, "title", query_tokens)
        self._add_if_matching(snippets, paper.abstract, "abstract", query_tokens)
        for claim in evidence_claims:
            safe = str(claim.validation_status or "").lower() in {"verified", "supported"}
            self._add_if_matching(
                snippets,
                claim.evidence_text or claim.claim_text,
                "evidence",
                query_tokens,
                page=claim.page_start or claim.page_end,
                verified=safe,
                safe_verified=False,
            )
        for review in reviews:
            safe = is_safe_verified_review(review)
            verified = str(review.reviewer_status or "").lower() == "verified"
            locator = _best_locator_for_review(review, locators)
            self._add_if_matching(
                snippets,
                review.evidence_text or _review_value_text(review),
                "review",
                query_tokens,
                page=locator.page if locator else None,
                locator_status=_locator_status(locator),
                verified=verified,
                safe_verified=safe,
            )
        for locator in locators:
            self._add_if_matching(
                snippets,
                locator.evidence_text,
                "locator",
                query_tokens,
                page=locator.page,
                locator_status=_locator_status(locator),
            )
        for section in sections:
            self._add_if_matching(snippets, section.text, "section", query_tokens, page=section.page_start or section.page_end)
        for row in extraction_rows:
            self._add_if_matching(snippets, _extraction_text(row), "extraction", query_tokens)
        return sorted(snippets, key=lambda item: (-len(item.matched_tokens), item.source))[:5]

    def _add_if_matching(
        self,
        snippets: list[Snippet],
        text: Any,
        source: str,
        query_tokens: set[str],
        *,
        page: int | None = None,
        locator_status: str = "missing",
        verified: bool = False,
        safe_verified: bool = False,
    ) -> None:
        clean = _clean_text(text)
        if not clean:
            return
        matched = tokenize(clean) & query_tokens
        if matched:
            snippets.append(
                Snippet(
                    text=_snippet_text(clean, matched),
                    source=source,
                    page=page,
                    locator_status=locator_status,
                    verified=verified,
                    safe_verified=safe_verified,
                    matched_tokens=matched,
                )
            )

    def _evidence_status(
        self,
        *,
        snippets: list[Snippet],
        reviews: list[ExtractionFieldReview],
        locators: list[EvidenceLocator],
        extraction_rows: list[Any],
    ) -> str:
        if any(item.safe_verified for item in snippets):
            return "safe_verified"
        if any(item.verified for item in snippets):
            return "verified"
        matching_pending_reviews = [
            review
            for review in reviews
            if str(review.reviewer_status or "").lower() in {"pending", "unknown", ""}
            and any((tokenize(review.evidence_text) & snippet.matched_tokens) for snippet in snippets)
        ]
        if matching_pending_reviews:
            if any(_best_locator_for_review(review, locators) is not None for review in matching_pending_reviews):
                return "pending_with_locator"
            return "pending_without_locator"
        if extraction_rows and any(item.source == "extraction" for item in snippets):
            return "unverified_extraction"
        return "metadata_only"

    def _feature_flags(
        self,
        *,
        paper: Paper,
        reviews: list[ExtractionFieldReview],
        evidence_claims: list[EvidenceClaim],
        sections: list[PaperSection],
        extraction_rows: list[Any],
    ) -> dict[str, bool]:
        return {
            "has_pdf": bool(paper.pdf_path and str(paper.pdf_path).strip()),
            "has_parsed_text": bool(paper.tei_path or paper.docling_json_path or paper.markdown_path or sections),
            "has_extraction_output": bool(paper.comprehensive_analysis or extraction_rows),
            "has_verified_evidence": any(str(claim.validation_status or "").lower() in {"verified", "supported"} for claim in evidence_claims)
            or any(str(review.reviewer_status or "").lower() == "verified" for review in reviews),
            "has_safe_verified_evidence": any(is_safe_verified_review(review) for review in reviews),
        }

    def _feature_mismatch(self, flags: dict[str, bool], filters: CitationCandidateFilters) -> str | None:
        for key, actual in flags.items():
            expected = getattr(filters, key)
            if expected is not None and actual is not expected:
                return f"{key}_filter_mismatch"
        return None

    def _candidate_response(
        self,
        *,
        paper: Paper,
        eligibility: PaperCitationEligibility | None,
        impact: PaperImpactMetadata | None,
        snippets: list[Snippet],
        evidence_status: str,
        filters: CitationCandidateFilters,
    ) -> dict[str, Any]:
        priority = eligibility.citation_priority if eligibility else "medium"
        score = self._score(paper, priority, impact, snippets, evidence_status, filters)
        can_confirm = evidence_status == "safe_verified"
        matched_fields = sorted({item.source for item in snippets})
        warnings = []
        if evidence_status != "safe_verified":
            warnings.append("suggestion_only_needs_human_verification")
        if impact is None or impact.impact_factor is None:
            warnings.append("impact_factor_needs_metadata")
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "year": paper.year,
            "journal": paper.journal,
            "impact_factor": impact.impact_factor if impact else None,
            "impact_factor_year": impact.impact_factor_year if impact else None,
            "impact_factor_status": "available" if impact and impact.impact_factor is not None else "needs_metadata",
            "citation_priority": priority,
            "exclude_from_citation": False,
            "recommendation_score": round(score, 4),
            "recommendation_tier": _tier(score),
            "evidence_status": evidence_status,
            "can_be_used_as_confirmed_citation": can_confirm,
            "requires_human_verification": not can_confirm,
            "matched_fields": matched_fields,
            "supporting_snippets": [item.response() for item in snippets],
            "reason": _reason(matched_fields, evidence_status),
            "warnings": warnings,
        }

    def _score(
        self,
        paper: Paper,
        priority: str,
        impact: PaperImpactMetadata | None,
        snippets: list[Snippet],
        evidence_status: str,
        filters: CitationCandidateFilters,
    ) -> float:
        overlap_score = min(sum(len(item.matched_tokens) for item in snippets) / 12, 0.45)
        score = 0.25 + overlap_score + PRIORITY_BOOST.get(priority, 0.0)
        if evidence_status == "safe_verified":
            score += 0.2
        elif evidence_status == "verified":
            score += 0.12
        elif evidence_status.startswith("pending"):
            score += 0.05
        elif evidence_status == "metadata_only":
            score -= 0.08
        if paper.pdf_path and str(paper.pdf_path).strip():
            score += 0.03
        if paper.year and (filters.year_min is not None or filters.year_max is not None):
            score += min(max((paper.year - 2015) / 100, 0), 0.08)
        if impact and impact.impact_factor is not None:
            score += min(impact.impact_factor / 100, 0.12)
        else:
            score -= 0.04
        return max(0.0, min(score, 1.0))

    def _extraction_rows_by_paper(self) -> dict[UUID, list[Any]]:
        rows: dict[UUID, list[Any]] = {}
        for model in TARGET_TYPE_BY_MODEL:
            for item in self.session.scalars(select(model)).all():
                rows.setdefault(item.paper_id, []).append(item)
        return rows


def tokenize(text: Any) -> set[str]:
    return {token.casefold() for token in TOKEN_RE.findall(str(text or "")) if len(token) > 2 and token.casefold() not in STOPWORDS}


def _group_by_paper(rows: list[Any]) -> dict[UUID, list[Any]]:
    grouped: dict[UUID, list[Any]] = {}
    for row in rows:
        paper_id = getattr(row, "paper_id", None)
        if paper_id is not None:
            grouped.setdefault(paper_id, []).append(row)
    return grouped


def _clean_text(text: Any) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split())


def _snippet_text(text: str, matched: set[str]) -> str:
    words = text.split()
    if len(words) <= 34:
        return text
    first_match = next((idx for idx, word in enumerate(words) if tokenize(word) & matched), 0)
    start = max(first_match - 12, 0)
    end = min(start + 34, len(words))
    return " ".join(words[start:end])


def _review_value_text(review: ExtractionFieldReview) -> str:
    return _clean_text(review.reviewed_value if review.reviewed_value is not None else review.original_value)


def _extraction_text(row: Any) -> str:
    values = []
    for name in (
        "claim_text",
        "evidence_text",
        "property_type",
        "value",
        "unit",
        "name",
        "catalyst_type",
        "software",
        "functional",
        "research_gap",
        "proposed_solution",
    ):
        value = getattr(row, name, None)
        if value not in (None, "", [], {}):
            values.append(str(value))
    return " ".join(values)


def _best_locator_for_review(review: ExtractionFieldReview, locators: list[EvidenceLocator]) -> EvidenceLocator | None:
    matches = [
        locator
        for locator in locators
        if str(locator.target_id or "") == str(review.target_id or "")
        and str(locator.target_type or "").casefold() in {str(review.target_type or "").casefold(), ""}
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (_locator_status(item) == "missing", item.page is None))[0]


def _locator_status(locator: EvidenceLocator | None) -> str:
    if locator is None:
        return "missing"
    status = str(locator.locator_status or "").casefold()
    if status in {"exact", "exact_page"}:
        return "exact"
    if status in {"page_only", "approximate", "approximate_candidate"}:
        return "page_only"
    return status or "missing"


def _tier(score: float) -> str:
    if score >= 0.72:
        return "strong"
    if score >= 0.48:
        return "moderate"
    return "weak"


def _reason(fields: list[str], evidence_status: str) -> str:
    field_text = ", ".join(fields)
    if evidence_status == "safe_verified":
        return f"Matches query terms in {field_text}; supporting review passed the safe verified gate."
    if evidence_status == "verified":
        return f"Matches query terms in {field_text}; evidence is verified but still should be checked before use."
    if evidence_status.startswith("pending"):
        return f"Matches query terms in {field_text}, but evidence is pending and requires human verification."
    if evidence_status == "unverified_extraction":
        return f"Matches extracted content in {field_text}; this is an unverified suggestion."
    return f"Matches metadata in {field_text}; metadata-only relevance cannot be used as direct evidence."


def _warnings(needs_metadata_excluded: int) -> list[str]:
    if needs_metadata_excluded:
        return [f"{needs_metadata_excluded} candidate(s) excluded because impact_factor_min was set and impact metadata is missing"]
    return []
