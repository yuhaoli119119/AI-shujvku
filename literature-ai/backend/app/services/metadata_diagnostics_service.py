from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperImpactMetadata


@dataclass(frozen=True)
class MetadataFieldDefinition:
    code: str
    label: str
    category: str
    completion_hint: str


REQUIRED_METADATA_FIELDS: tuple[MetadataFieldDefinition, ...] = (
    MetadataFieldDefinition(
        code="title",
        label="title",
        category="bibliographic",
        completion_hint="Prefer DOI provider metadata; fall back to GROBID/Docling title extraction or manual correction.",
    ),
    MetadataFieldDefinition(
        code="authors",
        label="authors",
        category="citation",
        completion_hint="Prefer DOI provider metadata; keep an empty list until author metadata is confirmed.",
    ),
    MetadataFieldDefinition(
        code="journal",
        label="journal",
        category="bibliographic",
        completion_hint="Use Crossref/OpenAlex style metadata from DOI/URL ingestion, or manually correct the paper record.",
    ),
    MetadataFieldDefinition(
        code="year",
        label="year",
        category="bibliographic",
        completion_hint="Use DOI/URL provider metadata first; PDF header parsing is a fallback only.",
    ),
    MetadataFieldDefinition(
        code="doi",
        label="DOI",
        category="bibliographic",
        completion_hint="Normalize DOI from provider metadata or the first pages of the PDF; never merge conflicting DOI values automatically.",
    ),
    MetadataFieldDefinition(
        code="impact_factor",
        label="impact factor",
        category="journal_quality",
        completion_hint="Import a trusted journal impact-factor CSV/JSON through /api/library/impact-metadata/import; diagnostics never scrape IF online.",
    ),
)

UNSUPPORTED_CITATION_FIELDS: tuple[dict[str, str], ...] = (
    {
        "code": "volume",
        "label": "volume",
        "reason": "The current Paper schema does not store journal volume yet, so diagnostics must not count it as missing.",
    },
    {
        "code": "issue",
        "label": "issue",
        "reason": "The current Paper schema does not store issue yet, so diagnostics must not count it as missing.",
    },
    {
        "code": "pages",
        "label": "pages",
        "reason": "PDF page count is not the same as article page range; no article page-range field exists yet.",
    },
    {
        "code": "publisher",
        "label": "publisher",
        "reason": "Publisher is not part of the current Paper schema and should be added deliberately if needed.",
    },
)


def paper_metadata_state(paper: Paper, impact: PaperImpactMetadata | None = None) -> dict[str, Any]:
    missing = missing_metadata_fields(paper, impact)
    missing_codes = [item["code"] for item in missing]
    has_bibliographic_gap = any(
        code in missing_codes for code in ("title", "authors", "journal", "year", "doi")
    )
    has_impact_gap = "impact_factor" in missing_codes
    if not missing_codes:
        status = "complete"
    elif has_bibliographic_gap and has_impact_gap:
        status = "needs_bibliographic_and_impact_metadata"
    elif has_bibliographic_gap:
        status = "needs_bibliographic_metadata"
    else:
        status = "needs_impact_metadata"
    return {
        "status": status,
        "complete": not missing_codes,
        "missing_fields": [item["label"] for item in missing],
        "missing_field_codes": missing_codes,
        "missing_field_details": missing,
    }


def missing_metadata_fields(paper: Paper, impact: PaperImpactMetadata | None = None) -> list[dict[str, str]]:
    checks = {
        "title": _has_text(paper.title),
        "authors": _has_authors(paper.authors),
        "journal": _has_text(paper.journal),
        "year": _has_valid_year(paper.year),
        "doi": _has_text(paper.doi),
        "impact_factor": bool(impact and impact.impact_factor is not None),
    }
    missing: list[dict[str, str]] = []
    for field in REQUIRED_METADATA_FIELDS:
        if checks[field.code]:
            continue
        missing.append(
            {
                "code": field.code,
                "label": field.label,
                "category": field.category,
                "completion_hint": field.completion_hint,
            }
        )
    return missing


class MetadataDiagnosticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def build_report(self) -> dict[str, Any]:
        papers = self.session.scalars(
            select(Paper).order_by(Paper.year.is_(None).asc(), Paper.year.asc(), Paper.title.asc())
        ).all()
        impact_map = {
            row.paper_id: row
            for row in self.session.scalars(select(PaperImpactMetadata)).all()
        }

        total = len(papers)
        coverage = {
            field.code: {
                "code": field.code,
                "label": field.label,
                "category": field.category,
                "present_count": 0,
                "missing_count": 0,
                "coverage_ratio": 0.0,
                "completion_hint": field.completion_hint,
            }
            for field in REQUIRED_METADATA_FIELDS
        }
        items: list[dict[str, Any]] = []

        for paper in papers:
            impact = impact_map.get(paper.id)
            state = paper_metadata_state(paper, impact)
            present_codes = {field.code for field in REQUIRED_METADATA_FIELDS} - set(state["missing_field_codes"])
            for code in present_codes:
                coverage[code]["present_count"] += 1
            for code in state["missing_field_codes"]:
                coverage[code]["missing_count"] += 1

            if state["missing_fields"]:
                items.append(
                    {
                        "paper_id": paper.id,
                        "title": paper.title or "Unknown Title",
                        "year": paper.year,
                        "journal": paper.journal,
                        "doi": paper.doi,
                        "impact_factor": impact.impact_factor if impact else None,
                        "impact_factor_year": impact.impact_factor_year if impact else None,
                        "impact_factor_source": impact.impact_factor_source if impact else "unknown",
                        "missing_fields": state["missing_fields"],
                        "missing_field_codes": state["missing_field_codes"],
                        "missing_field_details": state["missing_field_details"],
                        "metadata_status": state["status"],
                        "metadata_source": "papers + paper_impact_metadata",
                        "suggested_actions": _suggested_actions(state["missing_field_codes"], paper),
                        "evidence_status_disclaimer": "Metadata completeness does NOT imply evidence safety or verification.",
                    }
                )

        for row in coverage.values():
            row["coverage_ratio"] = (row["present_count"] / total) if total else 1.0

        return {
            "schema_version": "metadata_diagnostics_v2",
            "total_papers": total,
            "total_papers_needing_metadata": len(items),
            "complete_papers": total - len(items),
            "coverage": list(coverage.values()),
            "items": items,
            "unsupported_current_fields": list(UNSUPPORTED_CITATION_FIELDS),
            "impact_metadata_import_template": {
                "endpoint": "/api/library/impact-metadata/import",
                "match_key": "normalized journal name",
                "required_columns": [
                    "journal",
                    "impact_factor",
                    "impact_factor_year",
                    "impact_factor_source",
                ],
                "sample_csv": "journal,impact_factor,impact_factor_year,impact_factor_source\nAdvanced Energy Materials,24.4,2024,user_imported\n",
            },
            "completion_protocol": [
                "Run diagnostics first; do not treat missing metadata as evidence failure.",
                "Complete DOI/year/journal/title/authors from DOI or URL provider metadata when possible; PDF parsing is fallback.",
                "Reject or manually confirm DOI conflicts before merging records.",
                "Complete impact factors only from a trusted user-supplied import file; this endpoint performs no online scraping.",
                "Completing metadata must not mark any extraction, review, citation, or ML gate as verified.",
            ],
            "safety_guardrails": {
                "online_scraping_enabled": False,
                "auto_completion_enabled": False,
                "safety_upgrade_on_completion": False,
                "writes_database": False,
                "message": "This endpoint is strictly read-only diagnostics. It performs no external lookups and does not alter paper, extraction, review, or citation statuses.",
            },
        }


def _has_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def _has_authors(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_text(item) for item in value)
    return _has_text(value)


def _has_valid_year(value: Any) -> bool:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return False
    return 1000 <= year <= 3000


def _suggested_actions(missing_codes: list[str], paper: Paper) -> list[str]:
    actions: list[str] = []
    if any(code in missing_codes for code in ("title", "authors", "journal", "year", "doi")):
        if paper.doi:
            actions.append("Re-fetch metadata by DOI or manually update missing bibliographic fields.")
        else:
            actions.append("Use DOI/URL ingestion or inspect the PDF first pages to recover DOI, journal, and year.")
    if "impact_factor" in missing_codes:
        journal = paper.journal or "<journal>"
        actions.append(
            f"Import trusted impact metadata for journal '{journal}' through /api/library/impact-metadata/import."
        )
    return actions
