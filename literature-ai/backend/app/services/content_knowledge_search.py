from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, or_

from app.db.models import ContentEvidenceItem, Paper


def content_item_filters(
    *,
    paper_ids: Sequence[UUID],
    run_id: UUID | None,
    category: str | None,
    terms: Sequence[str],
    include_candidates: bool,
    include_blocked: bool,
    review_status: str | None,
    citation_status: str | None,
    source_trust: str | None,
    problem_status: str | None,
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [ContentEvidenceItem.paper_id.in_(paper_ids)]
    if run_id is not None:
        filters.append(ContentEvidenceItem.run_id == run_id)
    if category:
        filters.append(ContentEvidenceItem.category == category)
    if review_status:
        filters.append(ContentEvidenceItem.review_status == review_status)
    if citation_status:
        filters.append(ContentEvidenceItem.citation_status == citation_status)
    if not include_candidates:
        filters.append(ContentEvidenceItem.source_type != "external_analysis_candidate")
    if not include_blocked:
        filters.append(ContentEvidenceItem.citation_status != "blocked")
    if source_trust == "verified":
        filters.append(ContentEvidenceItem.source_identity_verified.is_(True))
    elif source_trust == "unverified":
        filters.append(ContentEvidenceItem.source_identity_verified.is_(False))
    if problem_status == "has_risk":
        filters.append(ContentEvidenceItem.risk_flags != [])
    filters.extend(_term_filter(term) for term in terms)
    return filters


def content_search_score(terms: Sequence[str]) -> ColumnElement[Any] | None:
    """Score matched rows without changing the all-terms-required contract."""
    if not terms:
        return None
    parts: list[ColumnElement[int]] = []
    for term in terms:
        parts.extend(
            [
                case((Paper.paper_code.ilike(_exact(term), escape="\\"), 40), else_=0),
                case((Paper.doi.ilike(_exact(term), escape="\\"), 35), else_=0),
                case((_contains(Paper.title, term), 12), else_=0),
                case((_contains(ContentEvidenceItem.content, term), 10), else_=0),
                case((_contains(ContentEvidenceItem.evidence_text, term), 9), else_=0),
                case((_contains(ContentEvidenceItem.section_title, term), 7), else_=0),
                case((_contains(ContentEvidenceItem.category, term), 5), else_=0),
                case((_contains(ContentEvidenceItem.source_type, term), 4), else_=0),
                case((_contains(Paper.paper_code, term), 15), else_=0),
                case((_contains(Paper.doi, term), 14), else_=0),
            ]
        )
    score = parts[0]
    for part in parts[1:]:
        score = score + part
    return score


def _term_filter(term: str) -> ColumnElement[bool]:
    return or_(
        _contains(ContentEvidenceItem.content, term),
        _contains(ContentEvidenceItem.evidence_text, term),
        _contains(ContentEvidenceItem.section_title, term),
        _contains(ContentEvidenceItem.category, term),
        _contains(ContentEvidenceItem.source_type, term),
        _contains(Paper.paper_code, term),
        _contains(Paper.title, term),
        _contains(Paper.doi, term),
    )


def _contains(column: Any, term: str) -> ColumnElement[bool]:
    return column.ilike(f"%{_escape_like(term)}%", escape="\\")


def _exact(term: str) -> str:
    return _escape_like(term)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
