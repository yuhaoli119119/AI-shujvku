from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    EvidenceLocator,
    ExtractionFieldReview,
    Journal,
    JournalAlias,
    JournalMetric,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
    PaperRelationship,
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
    metric_type: str = "JIF"
    data_year: int | None = None
    release_year: int | None = None
    source_url: str | None = None
    source_snapshot_hash: str | None = None
    aliases: tuple[str, ...] = ()
    row_number: int | None = None


@dataclass(frozen=True)
class InvalidImpactMetadataItem:
    row_number: int | None
    journal: str | None
    reason: str


LIS_DUAL_ATOM_ABLESCI_2026_SOURCE = "ablesci_jif_2026"


def lis_dual_atom_ablesci_2026_items() -> list[ImpactMetadataImportItem]:
    """Curated Li-S dual-atom journal JIF snapshot from AbleSci pages supplied for this library."""
    rows = [
        ("Small", 11.8, "https://www.ablesci.com/journal/detail?id=pq49Lr", ()),
        ("Acta Materialia", 10.7, "https://www.ablesci.com/journal/detail?id=05Vg0D", ()),
        ("Energy Storage Materials", 19.3, "https://www.ablesci.com/journal/detail?id=pB4B6D", ()),
        (
            "Journal of the American Chemical Society",
            16.6,
            "https://www.ablesci.com/journal/detail?id=pnGnw5",
            ("JACS", "J. Am. Chem. Soc.", "Journal of the American Chemical Society J. Am. Chem. Soc."),
        ),
        ("Computational Materials Science", 3.3, "https://www.ablesci.com/journal/detail?id=poqZ3D", ()),
        ("ACS Omega", 5.2, "https://www.ablesci.com/journal/detail?id=52zRX5", ()),
        (
            "Physical Chemistry Chemical Physics",
            3.0,
            "https://www.ablesci.com/journal/detail?id=52a68p",
            ("PCCP", "Phys. Chem. Chem. Phys.", "Physical Chemistry Chemical Physics Phys. Chem. Chem. Phys."),
        ),
        ("Advanced Energy Materials", 26.0, "https://www.ablesci.com/journal/detail?id=r8M8xp", ()),
        (
            "The Journal of Physical Chemistry Letters",
            4.5,
            "https://www.ablesci.com/journal/detail?id=pL8Zbp",
            (
                "J. Phys. Chem. Lett.",
                "Journal of Physical Chemistry Letters",
                "The Journal of Physical Chemistry Letters J. Phys. Chem. Lett.",
            ),
        ),
        ("Chemical Engineering Journal", 12.5, "https://www.ablesci.com/journal/detail?id=w5g8JD", ()),
        ("Journal of Energy Storage", 10.7, "https://www.ablesci.com/journal/detail?id=DG6QW5", ()),
    ]
    return [
        ImpactMetadataImportItem(
            journal=journal,
            impact_factor=value,
            impact_factor_year=2025,
            impact_factor_source=LIS_DUAL_ATOM_ABLESCI_2026_SOURCE,
            metric_type="JIF",
            data_year=2025,
            release_year=2026,
            source_url=url,
            aliases=aliases,
            row_number=index,
        )
        for index, (journal, value, url, aliases) in enumerate(rows, start=1)
    ]


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
        library_name: str | None = None,
    ) -> dict[str, Any]:
        before = self._snapshot(library_name=library_name)
        if require_impact_table and not before["tables"]["paper_impact_metadata"]:
            raise ValueError("paper_impact_metadata table is missing")
        if expected_papers_total is not None and before["papers_total"] != expected_papers_total:
            raise ValueError(f"Expected papers_total={expected_papers_total}, got {before['papers_total']}")

        deduped_items = self._dedupe_items(items)
        papers_by_journal = self._papers_by_normalized_effective_journal(library_name=library_name)
        existing = {
            row.paper_id: row
            for row in self.session.scalars(select(PaperImpactMetadata)).all()
        }
        operations: dict[UUID, tuple[ImpactMetadataImportItem, bool, Journal | None]] = {}
        unmatched_items: list[dict[str, Any]] = []
        matched_paper_ids: set[UUID] = set()
        journal_upserted_count = 0
        journal_metric_upserted_count = 0
        for item in deduped_items:
            match_keys = {normalize_journal_name(item.journal)}
            match_keys.update(normalize_journal_name(alias) for alias in item.aliases if normalize_journal_name(alias))
            match_keys.update(key for key in (_normalize_issn(item.issn), _normalize_issn(item.eissn)) if key)
            journal = None
            if not dry_run:
                journal, journal_created = self._upsert_journal(item)
                metric_created = self._upsert_metric(journal, item)
                journal_upserted_count += 1 if journal_created else 0
                journal_metric_upserted_count += 1 if metric_created else 0
            matches = []
            seen_paper_ids: set[UUID] = set()
            for key in match_keys:
                for paper, inherited_from_main in papers_by_journal.get(key, []):
                    if paper.id in seen_paper_ids:
                        continue
                    seen_paper_ids.add(paper.id)
                    matches.append((paper, inherited_from_main))
            if not matches:
                unmatched_items.append(_item_response(item))
                continue
            for paper, inherited_from_main in matches:
                operations[paper.id] = (item, inherited_from_main, journal)
                matched_paper_ids.add(paper.id)

        imported_count = 0
        updated_count = 0
        journal_bound_count = 0
        for paper_id, (item, inherited_from_main, journal) in operations.items():
            source = _impact_source_for_cache(item, inherited_from_main)
            compat_year = item.data_year if item.data_year is not None else item.impact_factor_year
            if journal is not None:
                paper = self.session.get(Paper, paper_id)
                if paper is not None and paper.journal_id != journal.id:
                    paper.journal_id = journal.id
                    journal_bound_count += 1
            row = existing.get(paper_id)
            if row is None:
                imported_count += 1
                if not dry_run:
                    self.session.add(
                        PaperImpactMetadata(
                            paper_id=paper_id,
                            impact_factor=item.impact_factor,
                            impact_factor_source=source,
                            impact_factor_year=compat_year,
                        )
                    )
                continue
            if (
                row.impact_factor != item.impact_factor
                or row.impact_factor_source != source
                or row.impact_factor_year != compat_year
            ):
                updated_count += 1
                if not dry_run:
                    row.impact_factor = item.impact_factor
                    row.impact_factor_source = source
                    row.impact_factor_year = compat_year

        if not dry_run:
            self.session.flush()

        needs_metadata_remaining = self._needs_metadata_remaining(pending_paper_ids=set() if dry_run else set(operations), library_name=library_name)
        after = self._snapshot(library_name=library_name)
        if not dry_run:
            self._assert_safety(before, after)

        source_values = sorted({item.impact_factor_source for item in deduped_items})
        year_values = sorted({item.impact_factor_year for item in deduped_items if item.impact_factor_year is not None})
        return {
            "imported_count": imported_count,
            "updated_count": updated_count,
            "matched_paper_count": len(matched_paper_ids),
            "journal_upserted_count": journal_upserted_count,
            "journal_metric_upserted_count": journal_metric_upserted_count,
            "journal_bound_count": journal_bound_count,
            "unmatched_items": unmatched_items,
            "invalid_items": [],
            "needs_metadata_remaining": needs_metadata_remaining,
            "source": source_values[0] if len(source_values) == 1 else source_values,
            "impact_factor_year": year_values[0] if len(year_values) == 1 else year_values,
            "metric_type": sorted({item.metric_type for item in deduped_items}),
            "data_year": sorted({item.data_year for item in deduped_items if item.data_year is not None}),
            "release_year": sorted({item.release_year for item in deduped_items if item.release_year is not None}),
            "library_name": library_name,
            "active_db_write_performed": not dry_run and bool(deduped_items),
            "dry_run": dry_run,
            "before_snapshot": before,
            "after_snapshot": after,
            "safety": {
                "writes_papers_table": "journal_id_only",
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
            key = (
                normalize_journal_name(item.journal),
                item.data_year if item.data_year is not None else item.impact_factor_year,
                item.release_year,
                item.metric_type,
                item.impact_factor_source,
            )
            deduped[key] = item
        return list(deduped.values())

    def _papers_by_normalized_effective_journal(self, *, library_name: str | None) -> dict[str, list[tuple[Paper, bool]]]:
        stmt = select(Paper)
        if library_name:
            stmt = stmt.where(Paper.library_name == library_name)
        papers = self.session.scalars(stmt).all()
        papers_by_id = {paper.id: paper for paper in papers}
        parent_by_supplementary_id: dict[UUID, Paper] = {}
        if "paper_relationships" in set(inspect(self.session.get_bind()).get_table_names()):
            rows = self.session.scalars(
                select(PaperRelationship).where(
                    func.lower(PaperRelationship.relationship_type).in_(
                        ("supplementary", "supplementary_information", "supporting_information", "si")
                    )
                )
            ).all()
            for row in rows:
                parent = papers_by_id.get(row.source_paper_id)
                if parent is not None:
                    parent_by_supplementary_id[row.target_paper_id] = parent

        by_journal: dict[str, list[tuple[Paper, bool]]] = {}
        for paper in papers:
            parent = parent_by_supplementary_id.get(paper.id)
            effective_journal = parent.journal if parent is not None and parent.journal else paper.journal
            normalized = normalize_journal_name(effective_journal)
            if normalized:
                by_journal.setdefault(normalized, []).append((paper, parent is not None))
            for issn in self._paper_journal_issn_keys(paper, parent):
                by_journal.setdefault(issn, []).append((paper, parent is not None))
        return by_journal

    def _paper_journal_issn_keys(self, paper: Paper, parent: Paper | None) -> list[str]:
        journal_id = parent.journal_id if parent is not None and parent.journal_id else paper.journal_id
        if not journal_id:
            return []
        journal = self.session.get(Journal, journal_id)
        if journal is None:
            return []
        return [key for key in (_normalize_issn(journal.print_issn), _normalize_issn(journal.electronic_issn)) if key]

    def _upsert_journal(self, item: ImpactMetadataImportItem) -> tuple[Journal, bool]:
        issn_keys = [key for key in (_normalize_issn(item.issn), _normalize_issn(item.eissn)) if key]
        journal: Journal | None = None
        if issn_keys:
            journal = self.session.scalar(
                select(Journal).where(
                    or_(
                        Journal.print_issn.in_(issn_keys),
                        Journal.electronic_issn.in_(issn_keys),
                    )
                )
            )
        normalized = normalize_journal_name(item.journal)
        if journal is None:
            journal = self.session.scalar(select(Journal).where(Journal.normalized_name == normalized))
        if journal is None:
            alias = self.session.scalar(select(JournalAlias).where(JournalAlias.normalized_alias == normalized))
            if alias is not None:
                journal = self.session.get(Journal, alias.journal_id)
        created = False
        if journal is None:
            journal = Journal(
                canonical_name=item.journal,
                normalized_name=normalized,
                print_issn=_normalize_issn(item.issn),
                electronic_issn=_normalize_issn(item.eissn),
                status="active",
            )
            self.session.add(journal)
            self.session.flush()
            created = True
        else:
            if _normalize_issn(item.issn) and not journal.print_issn:
                journal.print_issn = _normalize_issn(item.issn)
            if _normalize_issn(item.eissn) and not journal.electronic_issn:
                journal.electronic_issn = _normalize_issn(item.eissn)
            if not journal.canonical_name:
                journal.canonical_name = item.journal
        self._upsert_alias(journal, item.journal, source=item.impact_factor_source)
        for alias_value in item.aliases:
            self._upsert_alias(journal, alias_value, source=item.impact_factor_source)
        return journal, created

    def _upsert_alias(self, journal: Journal, alias_value: str, *, source: str) -> None:
        normalized = normalize_journal_name(alias_value)
        if not normalized:
            return
        existing = self.session.scalar(select(JournalAlias).where(JournalAlias.normalized_alias == normalized))
        if existing is None:
            self.session.add(
                JournalAlias(
                    journal_id=journal.id,
                    alias=alias_value,
                    normalized_alias=normalized,
                    source=source,
                )
            )

    def _upsert_metric(self, journal: Journal, item: ImpactMetadataImportItem) -> bool:
        data_year = item.data_year if item.data_year is not None else item.impact_factor_year
        source_snapshot_hash = item.source_snapshot_hash or _source_snapshot_hash(item)
        metric = self.session.scalar(
            select(JournalMetric).where(
                JournalMetric.journal_id == journal.id,
                JournalMetric.metric_type == item.metric_type,
                JournalMetric.data_year == data_year,
                JournalMetric.release_year == item.release_year,
                JournalMetric.source_name == item.impact_factor_source,
            )
        )
        if metric is None:
            self.session.add(
                JournalMetric(
                    journal_id=journal.id,
                    metric_type=item.metric_type,
                    metric_value=item.impact_factor,
                    data_year=data_year,
                    release_year=item.release_year,
                    source_name=item.impact_factor_source,
                    source_url=item.source_url,
                    source_snapshot_hash=source_snapshot_hash,
                    retrieved_at=datetime.utcnow(),
                )
            )
            return True
        metric.metric_value = item.impact_factor
        metric.source_url = item.source_url or metric.source_url
        metric.source_snapshot_hash = source_snapshot_hash
        metric.retrieved_at = datetime.utcnow()
        return False

    def _needs_metadata_remaining(self, *, pending_paper_ids: set[UUID], library_name: str | None) -> int:
        impact_ids = set(self.session.scalars(select(PaperImpactMetadata.paper_id)).all())
        impact_ids.update(pending_paper_ids)
        stmt = select(Paper.id)
        if library_name:
            stmt = stmt.where(Paper.library_name == library_name)
        all_paper_ids = set(self.session.scalars(stmt).all())
        return len(all_paper_ids - impact_ids)

    def _snapshot(self, *, library_name: str | None = None) -> dict[str, Any]:
        inspector = inspect(self.session.get_bind())
        tables = set(inspector.get_table_names())
        has_papers = "papers" in tables
        has_impact = "paper_impact_metadata" in tables
        has_journals = "journals" in tables
        has_journal_aliases = "journal_aliases" in tables
        has_journal_metrics = "journal_metrics" in tables
        has_eligibility = "paper_citation_eligibility" in tables
        has_reviews = "extraction_field_reviews" in tables
        has_locators = "evidence_locators" in tables
        paper_count_stmt = select(func.count(Paper.id))
        bound_count_stmt = select(func.count(Paper.id)).where(Paper.journal_id.is_not(None))
        if library_name:
            paper_count_stmt = paper_count_stmt.where(Paper.library_name == library_name)
            bound_count_stmt = bound_count_stmt.where(Paper.library_name == library_name)
        return {
            "papers_total": self.session.scalar(paper_count_stmt) if has_papers else 0,
            "paper_journal_bound_rows": self.session.scalar(bound_count_stmt) if has_papers else 0,
            "paper_impact_metadata_rows": self.session.scalar(select(func.count(PaperImpactMetadata.paper_id))) if has_impact else 0,
            "journal_rows": self.session.scalar(select(func.count(Journal.id))) if has_journals else 0,
            "journal_alias_rows": self.session.scalar(select(func.count(JournalAlias.id))) if has_journal_aliases else 0,
            "journal_metric_rows": self.session.scalar(select(func.count(JournalMetric.id))) if has_journal_metrics else 0,
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
                "journals": has_journals,
                "journal_aliases": has_journal_aliases,
                "journal_metrics": has_journal_metrics,
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
    source = _clean_optional_text(row.get("source_name")) or _clean_optional_text(row.get("impact_factor_source")) or default_source or "user_imported"
    year = _parse_optional_int(row.get("impact_factor_year"))
    if year is None:
        year = default_year
    data_year = _parse_optional_int(row.get("data_year"))
    if data_year is None:
        data_year = year
    release_year = _parse_optional_int(row.get("release_year"))
    metric_type = (_clean_optional_text(row.get("metric_type")) or "JIF").upper()
    aliases = _parse_aliases(row.get("aliases"))
    return (
        ImpactMetadataImportItem(
            journal=journal,
            impact_factor=impact_factor,
            impact_factor_year=year,
            impact_factor_source=source,
            issn=_clean_optional_text(row.get("issn")),
            eissn=_clean_optional_text(row.get("eissn")),
            note=_clean_optional_text(row.get("note")),
            metric_type=metric_type,
            data_year=data_year,
            release_year=release_year,
            source_url=_clean_optional_text(row.get("source_url")),
            source_snapshot_hash=_clean_optional_text(row.get("source_snapshot_hash")),
            aliases=aliases,
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


def _parse_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(text for item in value if (text := _clean_optional_text(item)))
    text = _clean_optional_text(value)
    if not text:
        return ()
    return tuple(part.strip() for part in text.split("|") if part.strip())


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
        "metric_type": item.metric_type,
        "data_year": item.data_year,
        "release_year": item.release_year,
        "source_url": item.source_url,
        "aliases": list(item.aliases),
    }


def _normalize_issn(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9Xx]", "", value)
    if len(cleaned) != 8:
        return value.strip().upper()
    return f"{cleaned[:4]}-{cleaned[4:]}".upper()


def _impact_source_for_cache(item: ImpactMetadataImportItem, inherited_from_main: bool) -> str:
    if inherited_from_main:
        return f"{item.impact_factor_source}:inherited_from_main"
    return item.impact_factor_source


def _source_snapshot_hash(item: ImpactMetadataImportItem) -> str:
    payload = {
        "journal": item.journal,
        "impact_factor": item.impact_factor,
        "metric_type": item.metric_type,
        "data_year": item.data_year if item.data_year is not None else item.impact_factor_year,
        "release_year": item.release_year,
        "source": item.impact_factor_source,
        "source_url": item.source_url,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
