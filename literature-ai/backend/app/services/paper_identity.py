from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Paper


DEFAULT_LIBRARY_NAME = "\u9ed8\u8ba4\u6587\u732e\u5e93"


class PaperIdentityService:
    DOI_EXACT_MATCH_SCORE = 1.0
    ARXIV_EXACT_MATCH_SCORE = 0.98
    TITLE_EXACT_MATCH_SCORE = 0.78
    TITLE_PARTIAL_MATCH_SCORE = 0.68
    YEAR_EXACT_BONUS = 0.15
    YEAR_NEAR_BONUS = 0.05
    TITLE_YEAR_AUTO_MERGE_THRESHOLD = 0.9

    DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)
    TITLE_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")
    TITLE_SPACE_RE = re.compile(r"\s+")
    ARXIV_ID_RE = re.compile(
        r"(?i)(?:arxiv:|arxiv\.org/(?:abs|pdf)/|abs/)?"
        r"((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?)"
    )

    @classmethod
    def normalize_doi(cls, doi: str | None) -> str | None:
        if not doi:
            return None
        normalized = str(doi).strip()
        normalized = normalized.split("?", 1)[0].split("#", 1)[0]
        normalized = cls.DOI_PREFIX_RE.sub("", normalized).strip().lower()
        normalized = normalized.rstrip(".,;)")
        return normalized or None

    @classmethod
    def normalize_title(cls, title: str | None) -> str | None:
        if not title:
            return None
        normalized = cls.TITLE_PUNCT_RE.sub(" ", str(title).strip().lower())
        normalized = cls.TITLE_SPACE_RE.sub(" ", normalized).strip()
        return normalized or None

    @classmethod
    def extract_arxiv_id(cls, value: str | None) -> str | None:
        if not value:
            return None
        match = cls.ARXIV_ID_RE.search(str(value).strip())
        if not match:
            return None
        return re.sub(r"v\d+$", "", match.group(1).lower())

    @classmethod
    def identity_score(
        cls,
        metadata_a: Mapping[str, Any] | None,
        metadata_b: Mapping[str, Any] | None,
    ) -> float:
        return float(cls.identity_match_report(metadata_a, metadata_b)["score"])

    @classmethod
    def identity_match_report(
        cls,
        metadata_a: Mapping[str, Any] | None,
        metadata_b: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        meta_a = metadata_a or {}
        meta_b = metadata_b or {}

        doi_a = cls.normalize_doi(cls._string(meta_a.get("doi")))
        doi_b = cls.normalize_doi(cls._string(meta_b.get("doi")))
        if doi_a and doi_b:
            if doi_a == doi_b:
                return {
                    "score": cls.DOI_EXACT_MATCH_SCORE,
                    "decision": "exact_doi",
                    "reason": "normalized DOI values match",
                    "doi_conflict": False,
                }
            return {
                "score": 0.0,
                "decision": "doi_conflict",
                "reason": "normalized DOI values differ",
                "doi_conflict": True,
            }

        arxiv_a = cls.extract_arxiv_id(cls._first_non_empty(meta_a, "arxiv_id", "identifier", "source_path", "url"))
        arxiv_b = cls.extract_arxiv_id(cls._first_non_empty(meta_b, "arxiv_id", "identifier", "source_path", "url"))
        if arxiv_a and arxiv_b and arxiv_a == arxiv_b:
            return {
                "score": cls.ARXIV_EXACT_MATCH_SCORE,
                "decision": "exact_arxiv",
                "reason": "normalized arXiv identifiers match",
                "doi_conflict": False,
            }

        score = cls._title_year_score(meta_a, meta_b)
        if score >= cls.TITLE_YEAR_AUTO_MERGE_THRESHOLD:
            return {
                "score": score,
                "decision": "high_confidence_title_year",
                "reason": "normalized title similarity and year are high confidence",
                "doi_conflict": False,
            }
        return {
            "score": score,
            "decision": "low_confidence",
            "reason": "identity evidence did not reach the title/year auto-merge threshold",
            "doi_conflict": False,
        }

    @classmethod
    def find_existing_paper(
        cls,
        session: Session,
        doi: str | None,
        title: str | None,
        year: int | None,
        arxiv_id: str | None,
        library_name: str | None,
    ) -> Paper | None:
        incoming = {
            "doi": cls.normalize_doi(doi),
            "title": title,
            "year": year,
            "arxiv_id": arxiv_id,
        }
        stmt = select(Paper)
        if library_name:
            stmt = stmt.where(Paper.library_name == library_name)

        best_match: Paper | None = None
        best_score = 0.0
        for candidate in session.scalars(stmt).all():
            report = cls.identity_match_report(incoming, cls.metadata_for_paper(candidate))
            if report["decision"] not in {"exact_doi", "exact_arxiv", "high_confidence_title_year"}:
                continue
            score = float(report["score"])
            if score > best_score:
                best_score = score
                best_match = candidate
        return best_match

    @classmethod
    def find_metadata_placeholder(
        cls,
        session: Session,
        doi: str | None,
        title: str | None,
        year: int | None,
        arxiv_id: str | None,
        library_name: str | None,
    ) -> Paper | None:
        incoming = {
            "doi": cls.normalize_doi(doi),
            "title": title,
            "year": year,
            "arxiv_id": arxiv_id,
        }
        stmt = select(Paper).where(Paper.oa_status == "metadata_only")
        if library_name:
            stmt = stmt.where(Paper.library_name == library_name)

        best_match: Paper | None = None
        best_score = 0.0
        for candidate in session.scalars(stmt).all():
            report = cls.identity_match_report(incoming, cls.metadata_for_paper(candidate))
            if report["decision"] not in {"exact_doi", "exact_arxiv", "high_confidence_title_year"}:
                continue
            score = float(report["score"])
            if score > best_score:
                best_match = candidate
                best_score = score
        return best_match

    @classmethod
    def upsert_metadata_only(
        cls,
        session: Session,
        *,
        external_metadata: Mapping[str, Any],
        identifier: str | None = None,
        library_name: str | None = None,
        source_reference: str | None = None,
        classify_callback: Callable[[str, str | None], dict[str, Any]] | None = None,
    ) -> Paper:
        library = library_name or DEFAULT_LIBRARY_NAME
        metadata = dict(external_metadata or {})
        doi = cls.normalize_doi(cls._string(metadata.get("doi")))
        title = cls._string(metadata.get("title")) or identifier or "Untitled paper"
        year = cls._coerce_year(metadata.get("year"))
        source_path = (
            source_reference
            or cls._string(metadata.get("url"))
            or cls._string(metadata.get("source_path"))
            or cls._string(metadata.get("identifier"))
            or identifier
        )
        arxiv_id = cls.extract_arxiv_id(
            cls._string(metadata.get("arxiv_id")) or cls._string(metadata.get("identifier")) or source_path or identifier
        )

        existing = cls.find_existing_paper(
            session,
            doi=doi,
            title=title,
            year=year,
            arxiv_id=arxiv_id,
            library_name=library,
        )
        if existing is None and (doi or arxiv_id):
            existing = cls.find_existing_paper(
                session,
                doi=doi,
                title=title,
                year=year,
                arxiv_id=arxiv_id,
                library_name=None,
            )

        if existing is not None:
            if existing.oa_status == "metadata_only":
                cls._fill_missing_metadata(
                    existing,
                    metadata=metadata,
                    title=title,
                    year=year,
                    doi=doi,
                    source_path=source_path,
                    classify_callback=classify_callback,
                )
                session.add(existing)
                session.commit()
                session.refresh(existing)
            return existing

        classification = classify_callback(title, cls._string(metadata.get("journal"))) if classify_callback else {}
        max_serial = session.execute(
            select(func.max(Paper.serial_number)).where(Paper.library_name == library)
        ).scalar_one()
        paper = Paper(
            serial_number=(max_serial or 0) + 1,
            title=title,
            year=year,
            journal=cls._string(metadata.get("journal")),
            authors=metadata.get("authors") or [],
            abstract=cls._string(metadata.get("abstract")),
            source_path=source_path,
            pdf_path="",
            doi=doi,
            paper_type=classification.get("paper_type", "research"),
            type_confidence=classification.get("type_confidence"),
            classification_source=classification.get("classification_source"),
            library_name=library,
            oa_status="metadata_only",
            license=cls._string(metadata.get("license")),
        )
        session.add(paper)
        session.commit()
        session.refresh(paper)
        return paper

    @classmethod
    def metadata_for_paper(cls, paper: Paper) -> dict[str, Any]:
        return {
            "doi": paper.doi,
            "title": paper.title,
            "year": paper.year,
            "arxiv_id": cls.extract_arxiv_id(paper.source_path or paper.title or paper.doi),
            "identifier": paper.source_path,
            "source_path": paper.source_path,
            "url": paper.source_path,
        }

    @classmethod
    def _fill_missing_metadata(
        cls,
        paper: Paper,
        *,
        metadata: Mapping[str, Any],
        title: str | None,
        year: int | None,
        doi: str | None,
        source_path: str | None,
        classify_callback: Callable[[str, str | None], dict[str, Any]] | None,
    ) -> None:
        if doi and paper.doi != doi:
            paper.doi = doi
        if title and not paper.title:
            paper.title = title
        if year and not paper.year:
            paper.year = year
        cls._fill_if_empty(paper, "journal", cls._string(metadata.get("journal")))
        cls._fill_if_empty(paper, "authors", metadata.get("authors") or None)
        cls._fill_if_empty(paper, "abstract", cls._string(metadata.get("abstract")))
        cls._fill_if_empty(paper, "source_path", source_path)
        cls._fill_if_empty(paper, "license", cls._string(metadata.get("license")))
        if not paper.paper_type and classify_callback:
            classification = classify_callback(paper.title or title or "Untitled paper", paper.journal)
            paper.paper_type = classification.get("paper_type", "research")
            paper.type_confidence = paper.type_confidence or classification.get("type_confidence")
            paper.classification_source = paper.classification_source or classification.get("classification_source")

    @staticmethod
    def _fill_if_empty(paper: Paper, field_name: str, value: Any) -> None:
        if value is None or value == "":
            return
        current = getattr(paper, field_name)
        if current is None or current == "" or current == []:
            setattr(paper, field_name, value)

    @classmethod
    def _title_year_score(cls, metadata_a: Mapping[str, Any], metadata_b: Mapping[str, Any]) -> float:
        title_a = cls.normalize_title(cls._string(metadata_a.get("title")))
        title_b = cls.normalize_title(cls._string(metadata_b.get("title")))
        if not title_a or not title_b:
            return 0.0

        score = 0.0
        if title_a == title_b:
            score = cls.TITLE_EXACT_MATCH_SCORE
        elif title_a in title_b or title_b in title_a:
            score = cls.TITLE_PARTIAL_MATCH_SCORE
        else:
            ratio = SequenceMatcher(None, title_a, title_b).ratio()
            tokens_a = set(title_a.split())
            tokens_b = set(title_b.split())
            overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b)) if tokens_a and tokens_b else 0.0
            score = max(ratio * 0.65, overlap * 0.75)

        year_a = cls._coerce_year(metadata_a.get("year"))
        year_b = cls._coerce_year(metadata_b.get("year"))
        if year_a and year_b:
            if year_a == year_b:
                score += cls.YEAR_EXACT_BONUS
            elif abs(year_a - year_b) == 1:
                score += cls.YEAR_NEAR_BONUS
            else:
                score = max(score - 0.2, 0.0)

        return min(score, 1.0)

    @staticmethod
    def _string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _first_non_empty(cls, payload: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = cls._string(payload.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _coerce_year(value: Any) -> int | None:
        if value is None:
            return None
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        return year if 1000 <= year <= 3000 else None
