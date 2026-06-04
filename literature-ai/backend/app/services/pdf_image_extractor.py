from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

import fitz

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CropCandidate:
    rect: fitz.Rect
    source: str
    score: float


class PdfImageExtractor:
    """Extract real article figure crops from PDFs with validation and fallbacks."""

    min_crop_width = 24.0
    min_crop_height = 24.0
    render_zoom = 2.0

    @staticmethod
    def _extract_figure_number(caption: str | None) -> int | None:
        if not caption:
            return None
        match = re.search(r"(?:Figure|Fig\.?|Scheme)\s*(\d+)", caption, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    @classmethod
    def extract_figures(cls, pdf_path: Path, figures: list[Any], output_dir: Path) -> None:
        """Populate figure.image_path by rendering the best available crop.

        Candidate order is deliberately evidence-based:
        1. Docling/PDF provenance bbox when it is plausible.
        2. Raster image blocks near the figure caption.
        3. A caption-anchored crop above the caption for vector/composite figures.
        """
        if not figures:
            return

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            logger.error("Failed to open PDF for image extraction: %s", exc)
            return

        try:
            for index, figure in enumerate(figures, start=1):
                cls._extract_one(doc=doc, pdf_path=pdf_path, figure=figure, index=index, output_dir=output_dir)
        finally:
            doc.close()

    @classmethod
    def _extract_one(cls, *, doc: fitz.Document, pdf_path: Path, figure: Any, index: int, output_dir: Path) -> None:
        caption = cls._get(figure, "caption")
        page_no = cls._figure_page_number(figure)
        if page_no is None:
            logger.debug("Skipping figure without page number: %s", caption)
            return
        page_index = page_no - 1
        if page_index < 0 or page_index >= len(doc):
            logger.debug("Skipping figure with out-of-range page %s: %s", page_no, caption)
            return

        page = doc[page_index]
        candidates = cls._crop_candidates(page=page, figure=figure, caption=caption)
        best = cls._best_candidate(candidates, page.rect)
        if best is None:
            logger.debug("No usable crop candidate for figure on page %s: %s", page_no, caption)
            return

        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(cls.render_zoom, cls.render_zoom), clip=best.rect, alpha=False)
            if pix.width < 16 or pix.height < 16:
                return
            filename = cls._output_filename(pdf_path, caption, index)
            out_path = cls._unique_output_path(output_dir / filename)
            pix.save(str(out_path))
            cls._set(figure, "image_path", out_path.name)
            cls._append_crop_provenance(figure, page_no=page_no, candidate=best, output_name=out_path.name)
        except Exception as exc:
            logger.warning("Failed to extract figure %s from %s: %s", index, pdf_path.name, exc)

    @classmethod
    def _crop_candidates(cls, *, page: fitz.Page, figure: Any, caption: str | None) -> list[_CropCandidate]:
        candidates: list[_CropCandidate] = []
        page_rect = page.rect
        prov_rect = cls._rect_from_provenance(figure, page_rect)
        if prov_rect is not None:
            area_fraction = cls._area_fraction(prov_rect, page_rect)
            score = 92.0 if area_fraction >= 0.025 else 45.0
            candidates.append(_CropCandidate(cls._pad_rect(prov_rect, page_rect, 4.0), "docling_bbox", score))

        caption_rect = cls._find_caption_rect(page, caption)
        image_blocks = cls._image_blocks(page)
        if caption_rect is not None:
            near_blocks = [
                rect
                for rect in image_blocks
                if rect.y1 <= caption_rect.y0 + 12
                and caption_rect.y0 - rect.y1 <= max(360.0, page_rect.height * 0.55)
            ]
            overlapping_blocks = [
                rect for rect in near_blocks if cls._horizontal_overlap_fraction(rect, caption_rect) >= 0.08
            ]
            if overlapping_blocks:
                union = cls._union_rects(overlapping_blocks)
                score = 98.0 if len(overlapping_blocks) == 1 else 94.0
                candidates.append(_CropCandidate(cls._pad_rect(union, page_rect, 6.0), "image_block_near_caption", score))

            anchor = cls._caption_anchor_crop(page_rect, caption_rect)
            if anchor is not None:
                candidates.append(_CropCandidate(anchor, "caption_anchor_above", 72.0))

        if image_blocks:
            largest = max(image_blocks, key=lambda rect: rect.get_area())
            candidates.append(_CropCandidate(cls._pad_rect(largest, page_rect, 6.0), "largest_image_block", 60.0))

        return candidates

    @classmethod
    def _rect_from_provenance(cls, figure: Any, page_rect: fitz.Rect) -> fitz.Rect | None:
        for item in cls._prov_items(figure):
            bbox = cls._get_from(item, "bbox")
            if not bbox:
                continue
            rect = cls._rect_from_bbox(bbox, page_rect)
            if rect is not None:
                return rect
        return None

    @classmethod
    def _rect_from_bbox(cls, bbox: Any, page_rect: fitz.Rect) -> fitz.Rect | None:
        try:
            left = float(cls._get_from(bbox, "l", "x0", "left"))
            top = float(cls._get_from(bbox, "t", "y0", "top"))
            right = float(cls._get_from(bbox, "r", "x1", "right"))
            bottom = float(cls._get_from(bbox, "b", "y1", "bottom"))
        except (TypeError, ValueError):
            return None

        if max(abs(left), abs(top), abs(right), abs(bottom)) <= 1.5:
            left *= page_rect.width
            right *= page_rect.width
            top *= page_rect.height
            bottom *= page_rect.height

        coord_origin = str(cls._get_from(bbox, "coord_origin") or "TOPLEFT").upper()
        if coord_origin.endswith("BOTTOMLEFT") or coord_origin == "BOTTOMLEFT":
            y0 = page_rect.height - top
            y1 = page_rect.height - bottom
            rect = fitz.Rect(left, min(y0, y1), right, max(y0, y1))
        else:
            rect = fitz.Rect(min(left, right), min(top, bottom), max(left, right), max(top, bottom))
        return cls._usable_rect(rect, page_rect)

    @staticmethod
    def _find_caption_rect(page: fitz.Page, caption: str | None) -> fitz.Rect | None:
        variants = []
        if caption:
            words = caption.split()
            for count in (10, 8, 6):
                first_words = " ".join(words[:count])
                if len(first_words) >= 8 and first_words not in variants:
                    variants.append(first_words)

        figure_number = PdfImageExtractor._extract_figure_number(caption)
        if figure_number is not None:
            variants.extend(
                [
                    f"Figure {figure_number}",
                    f"Fig. {figure_number}",
                    f"Fig {figure_number}",
                    f"Scheme {figure_number}",
                ]
            )

        for variant in variants:
            try:
                rects = page.search_for(variant)
            except Exception:
                rects = []
            if rects:
                return rects[0]
        return None

    @classmethod
    def _image_blocks(cls, page: fitz.Page) -> list[fitz.Rect]:
        blocks: list[fitz.Rect] = []
        try:
            payload = page.get_text("dict")
        except Exception:
            return blocks
        for block in payload.get("blocks") or []:
            if block.get("type") != 1:
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            rect = cls._usable_rect(fitz.Rect(*bbox), page.rect)
            if rect is not None and rect.get_area() >= 1200:
                blocks.append(rect)
        return blocks

    @classmethod
    def _caption_anchor_crop(cls, page_rect: fitz.Rect, caption_rect: fitz.Rect) -> fitz.Rect | None:
        margin_x = max(18.0, page_rect.width * 0.035)
        x0 = page_rect.x0 + margin_x
        x1 = page_rect.x1 - margin_x
        center_x = (caption_rect.x0 + caption_rect.x1) / 2.0
        if page_rect.width >= 560.0:
            if center_x < page_rect.x0 + page_rect.width * 0.45:
                x1 = page_rect.x0 + page_rect.width * 0.52
            elif center_x > page_rect.x0 + page_rect.width * 0.55:
                x0 = page_rect.x0 + page_rect.width * 0.48
        y0 = max(page_rect.y0 + 18.0, caption_rect.y0 - page_rect.height * 0.46)
        y1 = max(page_rect.y0 + 18.0, caption_rect.y0 - 6.0)
        rect = fitz.Rect(x0, y0, x1, y1)
        return cls._usable_rect(rect, page_rect)

    @classmethod
    def _best_candidate(cls, candidates: list[_CropCandidate], page_rect: fitz.Rect) -> _CropCandidate | None:
        usable: list[_CropCandidate] = []
        for candidate in candidates:
            rect = cls._usable_rect(candidate.rect, page_rect)
            if rect is None:
                continue
            area_fraction = cls._area_fraction(rect, page_rect)
            if area_fraction < 0.002 or area_fraction > 0.88:
                continue
            usable.append(_CropCandidate(rect, candidate.source, candidate.score + min(area_fraction, 0.25) * 20.0))
        if not usable:
            return None
        return max(usable, key=lambda candidate: candidate.score)

    @classmethod
    def _usable_rect(cls, rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect | None:
        normalized = fitz.Rect(
            min(rect.x0, rect.x1),
            min(rect.y0, rect.y1),
            max(rect.x0, rect.x1),
            max(rect.y0, rect.y1),
        ).intersect(page_rect)
        if normalized.is_empty or normalized.width < cls.min_crop_width or normalized.height < cls.min_crop_height:
            return None
        return normalized

    @staticmethod
    def _pad_rect(rect: fitz.Rect, page_rect: fitz.Rect, padding: float) -> fitz.Rect:
        padded = fitz.Rect(rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y1 + padding)
        return padded.intersect(page_rect)

    @staticmethod
    def _area_fraction(rect: fitz.Rect, page_rect: fitz.Rect) -> float:
        page_area = max(page_rect.get_area(), 1.0)
        return rect.get_area() / page_area

    @staticmethod
    def _horizontal_overlap_fraction(rect: fitz.Rect, anchor: fitz.Rect) -> float:
        overlap = max(0.0, min(rect.x1, anchor.x1) - max(rect.x0, anchor.x0))
        return overlap / max(min(rect.width, anchor.width), 1.0)

    @staticmethod
    def _union_rects(rects: list[fitz.Rect]) -> fitz.Rect:
        union = fitz.Rect(rects[0])
        for rect in rects[1:]:
            union.include_rect(rect)
        return union

    @classmethod
    def _figure_page_number(cls, figure: Any) -> int | None:
        page = cls._get(figure, "page")
        if page is None:
            for item in cls._prov_items(figure):
                page = cls._get_from(item, "page_no", "page")
                if page is not None:
                    break
        try:
            return int(page) if page is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _prov_items(cls, figure: Any) -> list[Any]:
        prov = cls._get(figure, "prov") or []
        return prov if isinstance(prov, list) else []

    @staticmethod
    def _output_filename(pdf_path: Path, caption: str | None, index: int) -> str:
        figure_number = PdfImageExtractor._extract_figure_number(caption)
        if figure_number is not None:
            suffix = f"fig_{figure_number}"
        else:
            suffix = f"fig_a{index}"
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", pdf_path.stem).strip("._") or "paper"
        return f"{safe_stem}_{suffix}.png"

    @staticmethod
    def _unique_output_path(path: Path) -> Path:
        if not path.exists():
            return path
        counter = 2
        while True:
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _append_crop_provenance(figure: Any, *, page_no: int, candidate: _CropCandidate, output_name: str) -> None:
        entry = {
            "image_extraction": "pymupdf",
            "source": candidate.source,
            "page_no": page_no,
            "bbox": {
                "l": candidate.rect.x0,
                "t": candidate.rect.y0,
                "r": candidate.rect.x1,
                "b": candidate.rect.y1,
                "coord_origin": "TOPLEFT",
            },
            "image_path": output_name,
            "confidence": min(0.99, max(0.3, candidate.score / 100.0)),
        }
        prov = PdfImageExtractor._prov_items(figure)
        if hasattr(figure, "prov"):
            figure.prov = [*prov, entry]
        elif isinstance(figure, dict):
            figure["prov"] = [*prov, entry]

    @staticmethod
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    @staticmethod
    def _set(obj: Any, key: str, value: Any) -> None:
        if isinstance(obj, dict):
            obj[key] = value
        elif hasattr(obj, key):
            setattr(obj, key, value)

    @staticmethod
    def _get_from(obj: Any, *keys: str) -> Any:
        for key in keys:
            if isinstance(obj, dict) and key in obj:
                return obj.get(key)
            if hasattr(obj, key):
                return getattr(obj, key)
        return None
