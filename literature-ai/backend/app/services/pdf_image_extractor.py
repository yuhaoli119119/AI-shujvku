import logging
import re
from pathlib import Path
from typing import Any

import fitz

logger = logging.getLogger(__name__)


class PdfImageExtractor:
    """Extracts image regions from PDFs based on bounding box coordinates."""

    @staticmethod
    def _extract_figure_number(caption: str | None) -> int | None:
        """从 caption 中提取真实图号。

        例如 'Figure 3. Schematic diagram...' -> 3
             'Fig. 5: XRD patterns...' -> 5
             'Scheme 1: ...' -> 1
        """
        if not caption:
            return None
        match = re.search(r'(?:Figure|Fig\.?|Scheme)\s*(\d+)', caption, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def extract_figures(pdf_path: Path, figures: list[Any], output_dir: Path) -> None:
        """
        Extracts image regions from a PDF based on docling 'prov' bounding boxes,
        and sets 'image_path' in the figure object to the saved relative path.
        """
        if not figures:
            return

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            doc = fitz.open(str(pdf_path))
        except Exception as e:
            logger.error(f"Failed to open PDF for image extraction: {e}")
            return

        for idx, fig in enumerate(figures):
            # 'prov' might be a pydantic field or dict
            prov = getattr(fig, "prov", [])
            if not prov:
                continue

            item = prov[0]
            if isinstance(item, dict):
                page_no = item.get("page_no")
                bbox = item.get("bbox")
            else:
                page_no = getattr(item, "page_no", None)
                bbox = getattr(item, "bbox", None)

            if not page_no or not bbox:
                continue

            try:
                page_idx = page_no - 1
                if page_idx < 0 or page_idx >= len(doc):
                    continue

                page = doc[page_idx]

                if isinstance(bbox, dict):
                    l = bbox.get("l", 0)
                    t = bbox.get("t", 0)
                    r = bbox.get("r", 0)
                    b = bbox.get("b", 0)
                    coord_origin = bbox.get("coord_origin", "TOPLEFT")
                else:
                    l = getattr(bbox, "l", 0)
                    t = getattr(bbox, "t", 0)
                    r = getattr(bbox, "r", 0)
                    b = getattr(bbox, "b", 0)
                    coord_origin = getattr(bbox, "coord_origin", "TOPLEFT")

                if coord_origin == "BOTTOMLEFT":
                    page_height = page.rect.height
                    # In docling BOTTOMLEFT, y goes up. 
                    # t (top) > b (bottom)
                    y0 = page_height - t
                    y1 = page_height - b
                    rect = fitz.Rect(l, min(y0, y1), r, max(y0, y1))
                else:
                    rect = fitz.Rect(l, t, r, b)

                # Ensure rect is within page boundaries
                rect = rect.intersect(page.rect)
                if rect.is_empty or rect.width < 10 or rect.height < 10:
                    continue

                zoom = 2.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, clip=rect)

                if pix.width < 10 or pix.height < 10:
                    continue

                # 优先使用 caption 中的真实图号，否则回退为自动编号
                caption = getattr(fig, "caption", None) or (fig.get("caption") if isinstance(fig, dict) else None)
                fig_number = PdfImageExtractor._extract_figure_number(caption)
                if fig_number is not None:
                    filename = f"{pdf_path.stem}_fig_{fig_number}.png"
                else:
                    filename = f"{pdf_path.stem}_fig_a{idx + 1}.png"
                out_path = output_dir / filename

                pix.save(str(out_path))

                # Since fig is likely a Pydantic model
                if hasattr(fig, "image_path"):
                    setattr(fig, "image_path", out_path.name)
                elif isinstance(fig, dict):
                    fig["image_path"] = out_path.name

            except Exception as e:
                logger.warning(f"Failed to extract figure {idx} from {pdf_path.name}: {e}")

        doc.close()
