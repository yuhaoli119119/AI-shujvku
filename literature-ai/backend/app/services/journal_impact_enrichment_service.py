from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Callable
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Journal, JournalAlias, JournalMetric, Paper, PaperImpactMetadata
from app.services.impact_metadata_import_service import (
    ImpactMetadataImportItem,
    ImpactMetadataImportService,
    normalize_journal_name,
)


ABLESCI_JOURNAL_URL = "https://www.ablesci.com/journal/index"
ABLESCI_SOURCE_NAME = "ablesci_jif_auto"
_DETAIL_LINK_RE = re.compile(
    r'<a\b[^>]*class=["\'][^"\']*\bjournal-name\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_LATEST_JIF_RE = re.compile(
    r"(20\d{2})\s*最新影响因子.*?<span[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</span>",
    re.DOTALL,
)
_ISSN_RE = re.compile(r"\b\d{4}-?\d{3}[\dX]\b", re.IGNORECASE)


@dataclass(frozen=True)
class ImpactEnrichmentResult:
    status: str
    impact_factor: float | None = None
    source_url: str | None = None


class AbleSciJournalMetricLookup:
    """Single-journal, exact-match AbleSci lookup used only when the local cache misses."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.client_factory = client_factory

    def lookup(
        self,
        *,
        journal_name: str,
        print_issn: str | None = None,
        electronic_issn: str | None = None,
    ) -> ImpactMetadataImportItem | None:
        journal_name = str(journal_name or "").strip()
        if not journal_name:
            return None
        identifiers = [value for value in (print_issn, electronic_issn) if str(value or "").strip()]
        identifiers.extend(_journal_search_terms(journal_name))
        with self.client_factory(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Literature-AI journal-metadata lookup/1.0",
            },
        ) as client:
            for identifier in identifiers:
                response = client.get(ABLESCI_JOURNAL_URL, params={"keywords": str(identifier).strip()})
                response.raise_for_status()
                for detail_url, result_name in self._search_results(response.text):
                    detail_response = client.get(detail_url)
                    detail_response.raise_for_status()
                    item = self._parse_detail(
                        detail_response.text,
                        detail_url=detail_url,
                        requested_journal=journal_name,
                        requested_issns=(print_issn, electronic_issn),
                        result_name=result_name,
                    )
                    if item is not None:
                        return item
        return None

    @staticmethod
    def _search_results(html: str) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for href, raw_name in _DETAIL_LINK_RE.findall(html or ""):
            name = _html_text(re.split(r"<br\s*/?>", raw_name, maxsplit=1, flags=re.IGNORECASE)[0])
            if name:
                results.append((urljoin(ABLESCI_JOURNAL_URL, unescape(href)), name))
        return results

    @staticmethod
    def _parse_detail(
        html: str,
        *,
        detail_url: str,
        requested_journal: str,
        requested_issns: tuple[str | None, str | None],
        result_name: str,
    ) -> ImpactMetadataImportItem | None:
        latest = _LATEST_JIF_RE.search(html or "")
        if latest is None:
            return None
        release_year = int(latest.group(1))
        impact_factor = float(latest.group(2))
        detail_text = _html_text(html)
        requested_issn_values = {_normalize_issn(value) for value in requested_issns if _normalize_issn(value)}
        page_issn_values = {_normalize_issn(value) for value in _ISSN_RE.findall(detail_text) if _normalize_issn(value)}
        journal_matches = _journal_name_matches(result_name, requested_journal)
        issn_matches = bool(requested_issn_values and requested_issn_values.intersection(page_issn_values))
        if not journal_matches and not issn_matches:
            return None
        aliases = () if journal_matches else (requested_journal,)
        return ImpactMetadataImportItem(
            journal=result_name,
            impact_factor=impact_factor,
            impact_factor_year=release_year - 1,
            impact_factor_source=ABLESCI_SOURCE_NAME,
            metric_type="JIF",
            data_year=release_year - 1,
            release_year=release_year,
            source_url=detail_url,
            aliases=aliases,
        )


class JournalImpactEnrichmentService:
    """Bind a newly written paper to a cached JIF, querying AbleSci only on a cache miss."""

    def __init__(
        self,
        session: Session,
        *,
        enabled: bool = True,
        timeout_seconds: float = 8.0,
        lookup: AbleSciJournalMetricLookup | None = None,
    ) -> None:
        self.session = session
        self.enabled = enabled
        self.lookup = lookup or AbleSciJournalMetricLookup(timeout_seconds=timeout_seconds)

    def enrich_paper(self, paper: Paper) -> ImpactEnrichmentResult:
        journal_name = str(paper.journal or "").strip()
        if not journal_name:
            return ImpactEnrichmentResult(status="journal_missing")
        existing = self.session.get(PaperImpactMetadata, paper.id)
        if existing is not None and existing.impact_factor is not None:
            return ImpactEnrichmentResult(status="already_present", impact_factor=existing.impact_factor)

        cached_metric = self._cached_metric(paper, journal_name)
        if cached_metric is not None:
            self._apply_cached_metric(paper, cached_metric)
            return ImpactEnrichmentResult(
                status="local_cache",
                impact_factor=cached_metric.metric_value,
                source_url=cached_metric.source_url,
            )
        if not self.enabled:
            return ImpactEnrichmentResult(status="lookup_disabled")

        try:
            item = self.lookup.lookup(journal_name=journal_name)
        except (httpx.HTTPError, ValueError):
            return ImpactEnrichmentResult(status="lookup_unavailable")
        if item is None:
            return ImpactEnrichmentResult(status="not_found")

        ImpactMetadataImportService(self.session).import_items(
            [item],
            dry_run=False,
            library_name=paper.library_name,
        )
        row = self.session.get(PaperImpactMetadata, paper.id)
        return ImpactEnrichmentResult(
            status="ablesci_lookup" if row is not None and row.impact_factor is not None else "lookup_unmatched",
            impact_factor=row.impact_factor if row is not None else None,
            source_url=item.source_url,
        )

    def _cached_metric(self, paper: Paper, journal_name: str) -> JournalMetric | None:
        journal = self.session.get(Journal, paper.journal_id) if paper.journal_id else None
        normalized = normalize_journal_name(journal_name)
        if journal is None and normalized:
            journal = self.session.scalar(select(Journal).where(Journal.normalized_name == normalized))
        if journal is None and normalized:
            alias = self.session.scalar(select(JournalAlias).where(JournalAlias.normalized_alias == normalized))
            journal = self.session.get(Journal, alias.journal_id) if alias is not None else None
        if journal is None and normalized:
            prefix_matches = [
                candidate
                for candidate in self.session.scalars(select(Journal)).all()
                if _journal_name_matches(candidate.canonical_name, journal_name)
            ]
            if len(prefix_matches) == 1:
                journal = prefix_matches[0]
        if journal is None:
            return None
        paper.journal_id = journal.id
        return self.session.scalar(
            select(JournalMetric)
            .where(JournalMetric.journal_id == journal.id, JournalMetric.metric_type == "JIF")
            .order_by(JournalMetric.release_year.desc(), JournalMetric.data_year.desc(), JournalMetric.updated_at.desc())
        )

    def _apply_cached_metric(self, paper: Paper, metric: JournalMetric) -> None:
        row = self.session.get(PaperImpactMetadata, paper.id)
        if row is None:
            self.session.add(
                PaperImpactMetadata(
                    paper_id=paper.id,
                    impact_factor=metric.metric_value,
                    impact_factor_source=metric.source_name,
                    impact_factor_year=metric.data_year,
                )
            )
            return
        row.impact_factor = metric.metric_value
        row.impact_factor_source = metric.source_name
        row.impact_factor_year = metric.data_year


def _html_text(value: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", value or "")).split())


def _normalize_issn(value: str | None) -> str:
    return re.sub(r"[^0-9X]", "", str(value or "").upper())


def _journal_name_matches(canonical_name: str, supplied_name: str) -> bool:
    canonical = normalize_journal_name(canonical_name)
    supplied = normalize_journal_name(supplied_name)
    if canonical == supplied:
        return True
    # Some metadata providers concatenate the full journal title and abbreviation.
    # This accepts only a complete canonical-title prefix, never a fuzzy substring.
    return len(canonical) >= 10 and supplied.startswith(canonical + " ")


def _journal_search_terms(journal_name: str) -> list[str]:
    """Try the supplied title first, then its full-title prefix before a repeated abbreviation."""
    terms = [journal_name]
    tokens = journal_name.split()
    if len(tokens) > 2:
        first_token = re.sub(r"[^A-Za-z0-9]", "", tokens[0]).casefold()
        for index, token in enumerate(tokens[1:], start=1):
            repeated_token = re.sub(r"[^A-Za-z0-9]", "", token).casefold()
            if first_token and repeated_token == first_token:
                prefix = " ".join(tokens[:index]).strip()
                if prefix and prefix not in terms:
                    terms.append(prefix)
                break
    return terms
