from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceClaim,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
    PaperSection,
    WritingCard,
)
from app.services.metadata_diagnostics_service import paper_metadata_state
from app.utils.review_safety import is_safe_verified_review


PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, "exclude": 3}


@dataclass(frozen=True)
class PaperFilterCriteria:
    year_min: int | None = None
    year_max: int | None = None
    journal_includes: tuple[str, ...] = ()
    journal_excludes: tuple[str, ...] = ()
    impact_factor_min: float | None = None
    impact_factor_max: float | None = None
    keyword: str | None = None
    has_pdf: bool | None = None
    has_parsed_text: bool | None = None
    has_extraction_output: bool | None = None
    has_verified_evidence: bool | None = None
    has_safe_verified_evidence: bool | None = None
    exclude_from_citation: bool | None = None
    citation_priority: str | None = None
    needs_metadata: bool | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class FilteredPaper:
    id: UUID
    doi: str | None
    title: str | None
    year: int | None
    journal: str | None
    abstract: str | None
    pdf_path: str
    has_pdf: bool
    has_parsed_text: bool
    has_extraction_output: bool
    has_verified_evidence: bool
    has_safe_verified_evidence: bool
    included_for_writing: bool
    exclude_from_citation: bool
    exclude_reason: str | None
    citation_priority: str
    user_note: str | None
    eligibility_updated_at: datetime | None
    impact_factor: float | None
    impact_factor_source: str
    impact_factor_year: int | None
    impact_factor_status: str
    metadata_completeness_status: str
    metadata_missing_fields: list[str]
    metadata_missing_field_codes: list[str]


class PaperFilterService:
    """Read-only paper filtering for citation candidate selection."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def filter(self, criteria: PaperFilterCriteria) -> list[FilteredPaper]:
        paper_ids = self._candidate_paper_ids(criteria)
        if not paper_ids:
            return []
        feature_map = self._feature_map(paper_ids)
        eligibility_map = {
            row.paper_id: row
            for row in self.session.scalars(
                select(PaperCitationEligibility).where(PaperCitationEligibility.paper_id.in_(paper_ids))
            ).all()
        }
        impact_map = {
            row.paper_id: row
            for row in self.session.scalars(
                select(PaperImpactMetadata).where(PaperImpactMetadata.paper_id.in_(paper_ids))
            ).all()
        }

        papers = self.session.scalars(select(Paper).where(Paper.id.in_(paper_ids))).all()
        rows = [
            self._serialize(paper, feature_map.get(paper.id, {}), eligibility_map.get(paper.id), impact_map.get(paper.id))
            for paper in papers
        ]
        rows = [row for row in rows if self._matches_python_filters(row, criteria)]
        rows.sort(key=lambda row: (PRIORITY_RANK.get(row.citation_priority, 1), row.year is None, -(row.year or 0), row.title or ""))
        return rows[criteria.offset : criteria.offset + criteria.limit]

    def _candidate_paper_ids(self, criteria: PaperFilterCriteria) -> list[UUID]:
        stmt = select(Paper.id)
        if criteria.year_min is not None:
            stmt = stmt.where(Paper.year >= criteria.year_min)
        if criteria.year_max is not None:
            stmt = stmt.where(Paper.year <= criteria.year_max)
        for term in criteria.journal_includes:
            stmt = stmt.where(func.lower(func.coalesce(Paper.journal, "")).contains(term.lower()))
        for term in criteria.journal_excludes:
            stmt = stmt.where(~func.lower(func.coalesce(Paper.journal, "")).contains(term.lower()))
        if criteria.keyword:
            keyword = f"%{criteria.keyword.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(func.coalesce(Paper.title, "")).like(keyword),
                    func.lower(func.coalesce(Paper.abstract, "")).like(keyword),
                )
            )
        return list(self.session.scalars(stmt).all())

    def _feature_map(self, paper_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
        features: dict[UUID, dict[str, Any]] = {paper_id: {} for paper_id in paper_ids}
        self._mark_exists(features, PaperSection, "has_sections")
        for model in (DFTSetting, CatalystSample, DFTResult, ElectrochemicalPerformance, MechanismClaim, WritingCard):
            self._mark_exists(features, model, "has_extraction_tables")
        verified_claims = self.session.execute(
            select(EvidenceClaim.paper_id).where(
                EvidenceClaim.paper_id.in_(paper_ids),
                func.lower(EvidenceClaim.validation_status).in_(("verified", "supported")),
            )
        ).all()
        for (paper_id,) in verified_claims:
            features[paper_id]["has_verified_claim"] = True
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id.in_(paper_ids))
        ).all()
        for review in reviews:
            if str(review.reviewer_status or "").lower() == "verified":
                features[review.paper_id]["has_verified_review"] = True
            if is_safe_verified_review(review):
                features[review.paper_id]["has_safe_verified_review"] = True
        return features

    def _mark_exists(self, features: dict[UUID, dict[str, Any]], model: Any, key: str) -> None:
        rows = self.session.execute(select(model.paper_id).where(model.paper_id.in_(list(features)))).all()
        for (paper_id,) in rows:
            features[paper_id][key] = True

    def _serialize(
        self,
        paper: Paper,
        features: dict[str, Any],
        eligibility: PaperCitationEligibility | None,
        impact: PaperImpactMetadata | None,
    ) -> FilteredPaper:
        exclude = eligibility.exclude_from_citation if eligibility else False
        priority = eligibility.citation_priority if eligibility else "medium"
        impact_factor = impact.impact_factor if impact else None
        metadata_state = paper_metadata_state(paper, impact)
        has_parsed = bool(paper.tei_path or paper.docling_json_path or paper.markdown_path or features.get("has_sections"))
        has_extraction = bool(paper.comprehensive_analysis or features.get("has_extraction_tables"))
        has_verified = bool(features.get("has_verified_claim") or features.get("has_verified_review"))
        return FilteredPaper(
            id=paper.id,
            doi=paper.doi,
            title=paper.title,
            year=paper.year,
            journal=paper.journal,
            abstract=paper.abstract,
            pdf_path=paper.pdf_path,
            has_pdf=bool(paper.pdf_path and str(paper.pdf_path).strip()),
            has_parsed_text=has_parsed,
            has_extraction_output=has_extraction,
            has_verified_evidence=has_verified,
            has_safe_verified_evidence=bool(features.get("has_safe_verified_review")),
            included_for_writing=eligibility.included_for_writing if eligibility else True,
            exclude_from_citation=exclude,
            exclude_reason=eligibility.exclude_reason if eligibility else None,
            citation_priority=priority,
            user_note=eligibility.user_note if eligibility else None,
            eligibility_updated_at=eligibility.updated_at if eligibility else None,
            impact_factor=impact_factor,
            impact_factor_source=impact.impact_factor_source if impact else "unknown",
            impact_factor_year=impact.impact_factor_year if impact else None,
            impact_factor_status="known" if impact_factor is not None else "needs_metadata",
            metadata_completeness_status=metadata_state["status"],
            metadata_missing_fields=metadata_state["missing_fields"],
            metadata_missing_field_codes=metadata_state["missing_field_codes"],
        )

    def _matches_python_filters(self, row: FilteredPaper, criteria: PaperFilterCriteria) -> bool:
        if criteria.exclude_from_citation is None:
            if row.exclude_from_citation:
                return False
        elif row.exclude_from_citation is not criteria.exclude_from_citation:
            return False
        if criteria.citation_priority is not None and row.citation_priority != criteria.citation_priority:
            return False
        if criteria.impact_factor_min is not None and (
            row.impact_factor is None or row.impact_factor < criteria.impact_factor_min
        ):
            return False
        if criteria.impact_factor_max is not None and (
            row.impact_factor is None or row.impact_factor > criteria.impact_factor_max
        ):
            return False
        for attr in (
            "has_pdf",
            "has_parsed_text",
            "has_extraction_output",
            "has_verified_evidence",
            "has_safe_verified_evidence",
        ):
            expected = getattr(criteria, attr)
            if expected is not None and getattr(row, attr) is not expected:
                return False
        if criteria.needs_metadata is not None and (row.metadata_completeness_status != "complete") is not criteria.needs_metadata:
            return False
        return True
