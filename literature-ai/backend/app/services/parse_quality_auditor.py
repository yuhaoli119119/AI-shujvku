from __future__ import annotations

from pathlib import Path
import hashlib
import re
from typing import Iterable

from app.parsers.docling_parser import DoclingParser
from app.schemas.documents import UnifiedFigure, UnifiedTable


class ParseQualityAuditor:
    """Post-parse cleanup before tables and figures are persisted."""

    @classmethod
    def clean_tables(cls, tables: Iterable[UnifiedTable]) -> list[UnifiedTable]:
        candidates: list[UnifiedTable] = []
        for table in tables:
            cleaned = table.model_copy()
            cleaned.caption = cls._clean_table_caption(
                cleaned.caption,
                cleaned.markdown_content,
                cleaned.extraction_source,
            )
            if not cleaned.caption:
                continue
            if cls._is_body_table_reference(cleaned.caption, cleaned.extraction_source):
                continue
            if (cleaned.extraction_source or "").startswith("pypdf"):
                cleaned.markdown_content = cleaned.caption
            candidates.append(cleaned)

        best_by_key: dict[tuple[int | None, int | None], UnifiedTable] = {}
        unnumbered: list[UnifiedTable] = []
        for table in candidates:
            number = cls._table_number(table.caption)
            if number is None:
                unnumbered.append(table)
                continue
            key = (table.page, number)
            current = best_by_key.get(key)
            if current is None or cls._table_score(table) > cls._table_score(current):
                best_by_key[key] = table

        return [*best_by_key.values(), *unnumbered]

    @classmethod
    def clean_figures_before_extraction(cls, figures: Iterable[UnifiedFigure]) -> list[UnifiedFigure]:
        cleaned: list[UnifiedFigure] = []
        seen: set[tuple[int | None, int | None, str]] = set()
        for figure in figures:
            caption = cls._clean_figure_caption(figure.caption)
            if not caption or cls._is_body_figure_reference(caption):
                continue
            number = cls._figure_number(caption)
            key = (figure.page, number, cls._signature(caption, 120))
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(figure.model_copy(update={"caption": caption}))
        return cleaned

    @classmethod
    def clean_figures_after_extraction(cls, figures: Iterable[UnifiedFigure], figures_root: Path) -> list[UnifiedFigure]:
        best_by_page_number: dict[tuple[int | None, int], UnifiedFigure] = {}
        unnumbered: list[UnifiedFigure] = []

        for figure in figures:
            if not figure.image_path:
                continue
            image_path = figures_root / figure.image_path
            if not image_path.exists() or image_path.stat().st_size <= 0:
                continue

            number = cls._figure_number(figure.caption)
            if number is None:
                unnumbered.append(figure)
                continue

            key = (figure.page, number)
            current = best_by_page_number.get(key)
            if current is None or cls._figure_score(figure, figures_root) > cls._figure_score(current, figures_root):
                best_by_page_number[key] = figure

        deduped: list[UnifiedFigure] = []
        seen_hashes: set[str] = set()
        for figure in [*best_by_page_number.values(), *unnumbered]:
            image_path = figures_root / figure.image_path
            digest = cls._file_digest(image_path)
            if digest and digest in seen_hashes:
                continue
            if digest:
                seen_hashes.add(digest)
            deduped.append(figure)
        return deduped

    @staticmethod
    def _clean_table_caption(caption: str | None, markdown_content: str | None, extraction_source: str | None) -> str | None:
        cleaned = DoclingParser._clean_table_caption_text(caption)
        if not cleaned:
            return None
        cleaned = DoclingParser._strip_table_body_from_caption(cleaned, markdown_content or "")
        if (extraction_source or "").startswith("pypdf"):
            cleaned = ParseQualityAuditor._first_caption_sentence(cleaned, "table")
        return cleaned.strip() or None

    @staticmethod
    def _clean_figure_caption(caption: str | None) -> str | None:
        cleaned = DoclingParser._dedupe_caption_text(caption)
        if not cleaned:
            return None
        return ParseQualityAuditor._first_caption_sentence(cleaned, "figure").strip() or None

    @staticmethod
    def _first_caption_sentence(text: str, source: str) -> str:
        label = r"(?:Figure|Fig\.?|Scheme)" if source == "figure" else r"Table"
        match = re.match(rf"^\s*({label})\s+(\d+)\s*([\.:])\s*(.+)$", text, re.IGNORECASE)
        if not match:
            return text
        prefix = f"{match.group(1)} {match.group(2)}{match.group(3)}"
        body = match.group(4).strip()
        sentence = re.split(
            r"(?<=[.!?])\s+(?=(?:[A-Z][a-z]|\d+\s*[x×]|[A-Z][a-z]+ et al\.|[A-Z][A-Za-z-]+\s+(?:Section|Energies|Properties)))",
            body,
            maxsplit=1,
        )[0].strip()
        if source == "table":
            for marker in (
                " Method ",
                " P-Doping Positions ",
                " Eg Gap ",
                " Molecules ",
                " Li2S ",
                " 2.2. ",
                " 2.3. ",
                " 2.4. ",
            ):
                marker_index = sentence.find(marker)
                if marker_index > 30:
                    sentence = sentence[:marker_index].strip()
        return f"{prefix} {sentence}".strip()

    @staticmethod
    def _is_body_table_reference(caption: str, extraction_source: str | None) -> bool:
        if not (extraction_source or "").startswith("pypdf"):
            return False
        return bool(re.match(r"^Table\s+\d+\s*,\s+(?:and|the|this|these|results?)\b", caption, re.IGNORECASE))

    @staticmethod
    def _is_body_figure_reference(caption: str) -> bool:
        body_patterns = (
            r"^Figure\s+\d+\s+and\s+Figure\s+S\d+",
            r"^Figures?\s+\d+(?:\s+and\s+S?\d+|[-,])",
            r"^Fig\.\s*\d+[a-z]?\s+(?:shows?|presents?|depicts?|illustrates?)\b",
            r"^Figure\s+\d+[a-z]?\s+(?:shows?|presents?|depicts?|illustrates?)\b",
        )
        return any(re.match(pattern, caption, re.IGNORECASE) for pattern in body_patterns)

    @staticmethod
    def _table_number(caption: str | None) -> int | None:
        if not caption:
            return None
        match = re.search(r"\bTable\s+(\d+)\b", caption, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _figure_number(caption: str | None) -> int | None:
        if not caption:
            return None
        match = re.search(r"\b(?:Figure|Fig\.?|Scheme)\s*(\d+)\b", caption, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _table_score(table: UnifiedTable) -> float:
        score = 0.0
        source = table.extraction_source or ""
        content = table.markdown_content or ""
        if source == "docling":
            score += 100.0
        if "|" in content and "---" in content:
            score += 25.0
        score += min(content.count("\n"), 20) * 0.5
        score -= max(len(table.caption or "") - 350, 0) * 0.05
        return score

    @classmethod
    def _figure_score(cls, figure: UnifiedFigure, figures_root: Path) -> float:
        score = 0.0
        if figure.image_path:
            score += 100.0
            path = figures_root / figure.image_path
            if path.exists():
                score += min(path.stat().st_size / 50_000.0, 20.0)
        caption = figure.caption or ""
        if re.match(r"^(?:Figure|Fig\.?|Scheme)\s+\d+[\.:]", caption, re.IGNORECASE):
            score += 10.0
        score -= max(len(caption) - 700, 0) * 0.03
        return score

    @staticmethod
    def _signature(text: str, limit: int) -> str:
        return re.sub(r"\W+", " ", text.lower()).strip()[:limit]

    @staticmethod
    def _file_digest(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
