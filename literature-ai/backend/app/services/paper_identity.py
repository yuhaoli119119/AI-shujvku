from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper


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
        r"(?i)(?:arxiv:|abs/)?((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)"
    )

    @classmethod
    def normalize_doi(cls, doi: str | None) -> str | None:
        if not doi:
            return None
        normalized = cls.DOI_PREFIX_RE.sub("", doi.strip()).strip().lower()
        return normalized or None

    @classmethod
    def normalize_title(cls, title: str | None) -> str | None:
        if not title:
            return None
        normalized = cls.TITLE_PUNCT_RE.sub(" ", title.strip().lower())
        normalized = cls.TITLE_SPACE_RE.sub(" ", normalized).strip()
        return normalized or None

    @classmethod
    def extract_arxiv_id(cls, value: str | None) -> str | None:
        if not value:
            return None
        match = cls.ARXIV_ID_RE.search(value.strip())
        if not match:
            return None
        return match.group(1).lower()

    @classmethod
    def identity_score(
        cls,
        metadata_a: Mapping[str, Any] | None,
        metadata_b: Mapping[str, Any] | None,
    ) -> float:
        meta_a = metadata_a or {}
        meta_b = metadata_b or {}

        doi_a = cls.normalize_doi(cls._string(meta_a.get("doi")))
        doi_b = cls.normalize_doi(cls._string(meta_b.get("doi")))
        if doi_a and doi_b and doi_a == doi_b:
            return cls.DOI_EXACT_MATCH_SCORE

        arxiv_a = cls.extract_arxiv_id(cls._first_non_empty(meta_a, "arxiv_id", "identifier", "source_path", "url"))
        arxiv_b = cls.extract_arxiv_id(cls._first_non_empty(meta_b, "arxiv_id", "identifier", "source_path", "url"))
        if arxiv_a and arxiv_b and arxiv_a == arxiv_b:
            return cls.ARXIV_EXACT_MATCH_SCORE

        title_a = cls.normalize_title(cls._string(meta_a.get("title")))
        title_b = cls.normalize_title(cls._string(meta_b.get("title")))
        if not title_a or not title_b:
            return 0.0

        score = 0.0
        if title_a == title_b:
            score = cls.TITLE_EXACT_MATCH_SCORE
        elif title_a in title_b or title_b in title_a:
            score = cls.TITLE_PARTIAL_MATCH_SCORE
        else:
            tokens_a = set(title_a.split())
            tokens_b = set(title_b.split())
            if tokens_a and tokens_b:
                overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
                if overlap >= 0.8:
                    score = 0.6
                elif overlap >= 0.65:
                    score = 0.45

        year_a = cls._coerce_year(meta_a.get("year"))
        year_b = cls._coerce_year(meta_b.get("year"))
        if year_a and year_b:
            if year_a == year_b:
                score += cls.YEAR_EXACT_BONUS
            elif abs(year_a - year_b) == 1:
                score += cls.YEAR_NEAR_BONUS
            else:
                score = max(score - 0.2, 0.0)

        return min(score, 1.0)

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
            "doi": doi,
            "title": title,
            "year": year,
            "arxiv_id": arxiv_id,
        }
        stmt = select(Paper)
        if library_name:
            stmt = stmt.where(Paper.library_name == library_name)
        candidates = session.scalars(stmt).all()

        best_match: Paper | None = None
        best_score = 0.0
        for candidate in candidates:
            candidate_meta = cls.metadata_for_paper(candidate)
            score = cls.identity_score(incoming, candidate_meta)
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
            "doi": doi,
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
            score = cls.identity_score(incoming, cls.metadata_for_paper(candidate))
            if score >= cls.TITLE_YEAR_AUTO_MERGE_THRESHOLD and score > best_score:
                best_match = candidate
                best_score = score
        return best_match

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
