from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.db.models import (
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
)


_PUNCTUATION_RE = re.compile(r"[\s\.,;:!?'\"()\[\]{}\-_/\\]+")


@dataclass(frozen=True)
class ImpactMetadataImportItem:
    journal: str
    impact_factor: float
    impact_factor_year: int | None
    impact_factor_source: str
    issn: str | None = None
    eissn: str | None = None
    note: str | None = None
    row_number: int | None = None


@dataclass(frozen=True)
class InvalidImpactMetadataItem:
    row_number: int | None
    journal: str | None
    reason: str


def normalize_journal_name(value: str | None) -> str:
    """Normalize only safe exact-match differences; no fuzzy matching."""
    if value is None:
        return ""
    normalized = _PUNCTUATION_RE.sub(" ", value.strip().casefold())
    return " ".join(normalized.split())


def parse_impact_metadata_csv(text: str) -> tuple[list[ImpactMetadataImportItem], list[InvalidImpactMetadataItem]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], [InvalidImpactMetadataItem(row_number=None, journal=None, reason="CSV header is missing")]
    items: list[ImpactMetadataImportItem] = []
    invalid: list[InvalidImpactMetadataItem] = []
    for row_number, row in enumerate(reader, start=2):
        item, error = _parse_item(row, row_number=row_number, default_source=None, default_year=None)
        if error:
            invalid.append(error)
        elif item:
            items.append(item)
    return items, invalid


def parse_impact_metadata_json(payload: str | dict[str, Any] | list[Any]) -> tuple[list[ImpactMetadataImportItem], list[InvalidImpactMetadataItem]]:
    data: Any
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            return [], [InvalidImpactMetadataItem(row_number=None, journal=None, reason=f"Invalid JSON: {exc.msg}")]
    else:
        data = payload

    default_source = None
    default_year = None
    rows: list[Any]
    if isinstance(data, dict):
        rows = data.get("items") if isinstance(data.get("items"), list) else []
        default_source = _clean_optional_text(data.get("source"))
        default_year = _parse_optional_int(data.get("year"))
    elif isinstance(data, list):
        rows = data
    else:
        return [], [InvalidImpactMetadataItem(row_number=None, journal=None, reason="JSON payload must be an object or list")]

    items: list[ImpactMetadataImportItem] = []
    invalid: list[InvalidImpactMetadataItem] = []
    if not rows:
        invalid.append(InvalidImpactMetadataItem(row_number=None, journal=None, reason="No items provided"))
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            invalid.append(InvalidImpactMetadataItem(row_number=row_number, journal=None, reason="Item must be an object"))
            continue
        item, error = _parse_item(row, row_number=row_number, default_source=default_source, default_year=default_year)
        if error:
            invalid.append(error)
        elif item:
            items.append(item)
    return items, invalid


class ImpactMetadataImportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def import_items(
        self,
        items: list[ImpactMetadataImportItem],
        *,
        dry_run: bool = False,
        expected_papers_total: int | None = None,
        require_impact_table: bool = True,
    ) -> dict[str, Any]:
        before = self._snapshot()
        if require_impact_table and not before["tables"]["paper_impact_metadata"]:
            raise ValueError("paper_impact_metadata table is missing")
        if expected_papers_total is not None and before["papers_total"] != expected_papers_total:
            raise ValueError(f"Expected papers_total={expected_papers_total}, got {before['papers_total']}")

        deduped_items = self._dedupe_items(items)
        papers_by_journal = self._papers_by_normalized_journal()
        existing = {
            row.paper_id: row
            for row in self.session.scalars(select(PaperImpactMetadata)).all()
        }
        operations: dict[UUID, ImpactMetadataImportItem] = {}
        unmatched_items: list[dict[str, Any]] = []
        matched_paper_ids: set[UUID] = set()
        for item in deduped_items:
            matches = papers_by_journal.get(normalize_journal_name(item.journal), [])
            if not matches:
                unmatched_items.append(_item_response(item))
                continue
            for paper in matches:
                operations[paper.id] = item
                matched_paper_ids.add(paper.id)

        imported_count = 0
        updated_count = 0
        for paper_id, item in operations.items():
            row = existing.get(paper_id)
            if row is None:
                imported_count += 1
                if not dry_run:
                    self.session.add(
                        PaperImpactMetadata(
                            paper_id=paper_id,
                            impact_factor=item.impact_factor,
                            impact_factor_source=item.impact_factor_source,
                            impact_factor_year=item.impact_factor_year,
                        )
                    )
                continue
            if (
                row.impact_factor != item.impact_factor
                or row.impact_factor_source != item.impact_factor_source
                or row.impact_factor_year != item.impact_factor_year
            ):
                updated_count += 1
                if not dry_run:
                    row.impact_factor = item.impact_factor
                    row.impact_factor_source = item.impact_factor_source
                    row.impact_factor_year = item.impact_factor_year

        if not dry_run:
            self.session.flush()

        needs_metadata_remaining = self._needs_metadata_remaining(pending_paper_ids=set() if dry_run else set(operations))
        after = self._snapshot()
        if not dry_run:
            self._assert_safety(before, after)

        source_values = sorted({item.impact_factor_source for item in deduped_items})
        year_values = sorted({item.impact_factor_year for item in deduped_items if item.impact_factor_year is not None})
        return {
            "imported_count": imported_count,
            "updated_count": updated_count,
            "matched_paper_count": len(matched_paper_ids),
            "unmatched_items": unmatched_items,
            "invalid_items": [],
            "needs_metadata_remaining": needs_metadata_remaining,
            "source": source_values[0] if len(source_values) == 1 else source_values,
            "impact_factor_year": year_values[0] if len(year_values) == 1 else year_values,
            "active_db_write_performed": not dry_run and bool(operations),
            "dry_run": dry_run,
            "before_snapshot": before,
            "after_snapshot": after,
            "safety": {
                "writes_papers_table": False,
                "writes_reviews": False,
                "writes_evidence_locators": False,
                "marks_verified": False,
                "unlocks_export_or_writing": False,
                "online_fetch_or_scrape": False,
            },
        }

    def _dedupe_items(self, items: list[ImpactMetadataImportItem]) -> list[ImpactMetadataImportItem]:
        deduped: dict[tuple[str, int | None, str], ImpactMetadataImportItem] = {}
        for item in items:
            key = (normalize_journal_name(item.journal), item.impact_factor_year, item.impact_factor_source)
            deduped[key] = item
        return list(deduped.values())

    def _papers_by_normalized_journal(self) -> dict[str, list[Paper]]:
        papers = self.session.scalars(select(Paper)).all()
        by_journal: dict[str, list[Paper]] = {}
        for paper in papers:
            normalized = normalize_journal_name(paper.journal)
            if normalized:
                by_journal.setdefault(normalized, []).append(paper)
        return by_journal

    def _needs_metadata_remaining(self, *, pending_paper_ids: set[UUID]) -> int:
        impact_ids = set(self.session.scalars(select(PaperImpactMetadata.paper_id)).all())
        impact_ids.update(pending_paper_ids)
        all_paper_ids = set(self.session.scalars(select(Paper.id)).all())
        return len(all_paper_ids - impact_ids)

    def _snapshot(self) -> dict[str, Any]:
        inspector = inspect(self.session.get_bind())
        tables = set(inspector.get_table_names())
        has_papers = "papers" in tables
        has_impact = "paper_impact_metadata" in tables
        has_eligibility = "paper_citation_eligibility" in tables
        has_reviews = "extraction_field_reviews" in tables
        has_locators = "evidence_locators" in tables
        return {
            "papers_total": self.session.scalar(select(func.count(Paper.id))) if has_papers else 0,
            "paper_impact_metadata_rows": self.session.scalar(select(func.count(PaperImpactMetadata.paper_id))) if has_impact else 0,
            "paper_citation_eligibility_rows": self.session.scalar(select(func.count(PaperCitationEligibility.paper_id))) if has_eligibility else 0,
            "review_rows": self.session.scalar(select(func.count(ExtractionFieldReview.id))) if has_reviews else 0,
            "evidence_locator_rows": self.session.scalar(select(func.count(EvidenceLocator.id))) if has_locators else 0,
            "verified_review_rows": self.session.scalar(
                select(func.count(ExtractionFieldReview.id)).where(
                    func.lower(ExtractionFieldReview.reviewer_status) == "verified"
                )
            )
            if has_reviews
            else 0,
            "safe_verified_rows": self.session.scalar(
                select(func.count(ExtractionFieldReview.id)).where(
                    func.lower(ExtractionFieldReview.reviewer_status) == "verified",
                    func.lower(ExtractionFieldReview.target_resolution_status) == "active",
                )
            )
            if has_reviews
            else 0,
            "included_for_writing_rows": self.session.scalar(
                select(func.count(PaperCitationEligibility.paper_id)).where(
                    PaperCitationEligibility.included_for_writing.is_(True)
                )
            )
            if has_eligibility
            else 0,
            "tables": {
                "papers": has_papers,
                "paper_impact_metadata": has_impact,
                "paper_citation_eligibility": has_eligibility,
            },
        }

    def _assert_safety(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        unchanged = (
            "papers_total",
            "paper_citation_eligibility_rows",
            "review_rows",
            "evidence_locator_rows",
            "verified_review_rows",
            "safe_verified_rows",
            "included_for_writing_rows",
        )
        changed = [key for key in unchanged if before[key] != after[key]]
        if changed:
            raise RuntimeError(f"Impact metadata import safety check failed; changed: {', '.join(changed)}")


def _parse_item(
    row: dict[str, Any],
    *,
    row_number: int,
    default_source: str | None,
    default_year: int | None,
) -> tuple[ImpactMetadataImportItem | None, InvalidImpactMetadataItem | None]:
    journal = _clean_optional_text(row.get("journal"))
    if not journal:
        return None, InvalidImpactMetadataItem(row_number=row_number, journal=None, reason="journal is required")
    raw_impact_factor = row.get("impact_factor")
    impact_factor = _parse_optional_float(raw_impact_factor)
    if impact_factor is None:
        return None, InvalidImpactMetadataItem(row_number=row_number, journal=journal, reason="impact_factor is required and must be numeric")
    source = _clean_optional_text(row.get("impact_factor_source")) or default_source or "user_imported"
    year = _parse_optional_int(row.get("impact_factor_year"))
    if year is None:
        year = default_year
    return (
        ImpactMetadataImportItem(
            journal=journal,
            impact_factor=impact_factor,
            impact_factor_year=year,
            impact_factor_source=source,
            issn=_clean_optional_text(row.get("issn")),
            eissn=_clean_optional_text(row.get("eissn")),
            note=_clean_optional_text(row.get("note")),
            row_number=row_number,
        ),
        None,
    )


def _parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _item_response(item: ImpactMetadataImportItem) -> dict[str, Any]:
    return {
        "row_number": item.row_number,
        "journal": item.journal,
        "impact_factor": item.impact_factor,
        "impact_factor_year": item.impact_factor_year,
        "impact_factor_source": item.impact_factor_source,
        "issn": item.issn,
        "eissn": item.eissn,
        "note": item.note,
    }
