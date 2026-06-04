from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

from app.config import Settings
from app.utils.figure_filtering import is_decorative_figure

logger = logging.getLogger(__name__)


@dataclass
class DoclingParseResult:
    markdown: str
    json_payload: dict[str, Any]
    tables: list[dict[str, Any]]
    figures: list[dict[str, Any]]
    page_blocks: list[dict[str, Any]]


class DoclingParser:
    """Thin adapter around Docling with a text-only fallback for offline setups."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def parse_pdf(self, pdf_path: Path) -> DoclingParseResult:
        return self.parse_pdf_sync(pdf_path)

    def parse_pdf_sync(self, pdf_path: Path) -> DoclingParseResult:
        try:
            if not self.settings.docling_enabled:
                return self._fallback_parse(pdf_path)

            import os
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import AcceleratorDevice, AcceleratorOptions, EasyOcrOptions, PdfPipelineOptions
            from docling.document_converter import DocumentConverter
            from docling.document_converter import PdfFormatOption

            os.environ.setdefault("OMP_NUM_THREADS", str(self.settings.docling_num_threads))
            pipeline_kwargs: dict[str, Any] = {
                "document_timeout": self.settings.docling_document_timeout,
                "accelerator_options": AcceleratorOptions(
                    num_threads=self.settings.docling_num_threads,
                    device=AcceleratorDevice.CPU,
                ),
            }
            artifacts_path = self.settings.docling_artifacts_path
            if artifacts_path and artifacts_path.exists() and any(artifacts_path.iterdir()):
                pipeline_kwargs["artifacts_path"] = str(artifacts_path)

            pipeline_options = PdfPipelineOptions(**pipeline_kwargs)
            pipeline_options.do_ocr = self.settings.docling_do_ocr
            pipeline_options.do_table_structure = True
            if self.settings.docling_do_ocr:
                pipeline_options.ocr_options = EasyOcrOptions(
                    force_full_page_ocr=self.settings.docling_force_full_page_ocr,
                )

            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                }
            )
            result = converter.convert(str(pdf_path))
            document = result.document
            markdown = self._export_markdown(document)
            payload = self._export_json(document)
            tables = self._extract_tables(payload)
            figures = self._extract_figures(payload)
            page_blocks = self._extract_page_blocks(payload)
            return DoclingParseResult(
                markdown=markdown,
                json_payload=payload,
                tables=tables,
                figures=figures,
                page_blocks=page_blocks,
            )
        except Exception as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            logger.warning("Docling parse failed for %s; falling back to pypdf text extraction: %s", pdf_path, exc)
            return self._fallback_parse(pdf_path)

    @staticmethod
    def _export_markdown(document: Any) -> str:
        if hasattr(document, "export_to_markdown"):
            return document.export_to_markdown()
        if hasattr(document, "to_markdown"):
            return document.to_markdown()
        return ""

    @staticmethod
    def _export_json(document: Any) -> dict[str, Any]:
        if hasattr(document, "export_to_dict"):
            payload = document.export_to_dict()
        elif hasattr(document, "model_dump"):
            payload = document.model_dump()
        else:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _resolve_caption(item: dict[str, Any], payload: dict[str, Any]) -> str | None:
        captions = item.get("captions", [])
        if not captions:
            return item.get("caption")
        
        resolved_texts = []
        for cap in captions:
            if isinstance(cap, dict) and "$ref" in cap:
                ref_path = cap["$ref"]
                parts = ref_path.lstrip("#/").split("/")
                current = payload
                try:
                    for part in parts:
                        if isinstance(current, list):
                            current = current[int(part)]
                        elif isinstance(current, dict):
                            current = current[part]
                        else:
                            current = None
                            break
                    if isinstance(current, dict) and "text" in current:
                        resolved_texts.append(current["text"])
                    elif isinstance(current, str):
                        resolved_texts.append(current)
                except (KeyError, IndexError, ValueError):
                    pass
            elif isinstance(cap, dict) and "text" in cap:
                resolved_texts.append(cap["text"])
            elif isinstance(cap, str):
                resolved_texts.append(cap)
                
        return " ".join(resolved_texts) if resolved_texts else None

    @staticmethod
    def _first_prov(item: dict[str, Any]) -> dict[str, Any]:
        prov = item.get("prov") or []
        return prov[0] if prov and isinstance(prov[0], dict) else {}

    @staticmethod
    def _resolve_page(item: dict[str, Any]) -> int | None:
        page = item.get("page_no") or item.get("page")
        if page is None:
            page = DoclingParser._first_prov(item).get("page_no")
        try:
            return int(page) if page is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _table_cells_to_markdown(item: dict[str, Any]) -> str:
        data = item.get("data") or {}
        cells = data.get("table_cells") or item.get("table_cells") or []
        if not cells:
            return ""

        max_row = 0
        max_col = 0
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            max_row = max(max_row, int(cell.get("end_row_offset_idx") or cell.get("row") or 0))
            max_col = max(max_col, int(cell.get("end_col_offset_idx") or cell.get("col") or 0))
        if max_row <= 0 or max_col <= 0:
            return ""

        grid = [["" for _ in range(max_col)] for _ in range(max_row)]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            text = re.sub(r"\s+", " ", str(cell.get("text") or "")).strip()
            if not text:
                continue
            row = int(cell.get("start_row_offset_idx") or cell.get("row") or 0)
            col = int(cell.get("start_col_offset_idx") or cell.get("col") or 0)
            if 0 <= row < max_row and 0 <= col < max_col:
                grid[row][col] = text if not grid[row][col] else grid[row][col] + " / " + text

        rows = [[cell.strip() for cell in row] for row in grid if any(cell.strip() for cell in row)]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        header = rows[0]
        body = rows[1:] if len(rows) > 1 else []
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(lines)

    @staticmethod
    def _extract_page_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
        pages = payload.get("pages")
        if isinstance(pages, list) and pages and isinstance(pages[0], dict) and pages[0].get("text"):
            return pages

        by_page: dict[int, list[str]] = {}
        for item in payload.get("texts") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not text:
                continue
            page = DoclingParser._resolve_page(item)
            if page is None:
                continue
            by_page.setdefault(page, []).append(str(text))
        return [
            {"page": page, "text": "\n".join(parts)}
            for page, parts in sorted(by_page.items())
            if any(part.strip() for part in parts)
        ]

    @staticmethod
    def _is_decorative_figure(caption: str | None, prov: list) -> bool:
        """Detect decorative figures such as CrossMark, publisher logos, and bare labels."""
        return is_decorative_figure(caption, prov)

    @staticmethod
    def _looks_like_fallback_caption_start(line: str, source: str) -> bool:
        """Filter body references that look like captions in plain PDF text."""
        label_pattern = (
            r"^\s*(?:figure|fig\.?|scheme)\s+\d+(?P<tail>.*)$"
            if source == "figure"
            else r"^\s*table\s+\d+(?P<tail>.*)$"
        )
        match = re.match(label_pattern, line, re.IGNORECASE)
        if not match:
            return False

        tail = match.group("tail") or ""
        if not tail.strip():
            return False
        stripped = tail.strip()
        if stripped[0] in ".:：);-–" or stripped.startswith(("(", "[")):
            return True

        # Body references often appear as "Fig. 3a presents ..." or
        # "Figure 6 shows ..."; these are not standalone captions.
        if tail and tail[0].isalpha():
            return False
        first_word = re.match(r"([A-Za-z]+)", stripped)
        if first_word and first_word.group(1).lower() in {
            "show",
            "shows",
            "shown",
            "present",
            "presents",
            "presented",
            "provide",
            "provides",
            "provided",
            "demonstrate",
            "demonstrates",
            "illustrate",
            "illustrates",
            "display",
            "displays",
            "depict",
            "depicts",
            "report",
            "reports",
            "summarize",
            "summarizes",
            "compare",
            "compares",
            "does",
            "is",
            "are",
            "can",
        }:
            return False
        return True

    @staticmethod
    def _extract_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
        tables = payload.get("tables") or payload.get("table_items") or []
        normalized = []
        for index, item in enumerate(tables, start=1):
            caption = DoclingParser._resolve_caption(item, payload) or f"Table {index}"
            prov = item.get("prov", [])
            markdown_content = (
                item.get("markdown")
                or item.get("text")
                or item.get("html")
                or DoclingParser._table_cells_to_markdown(item)
                or ""
            )
            normalized.append(
                {
                    "caption": caption,
                    "markdown_content": markdown_content,
                    "page": DoclingParser._resolve_page(item),
                    "extraction_source": "docling",
                    "prov": prov,
                }
            )
        return normalized

    @staticmethod
    def _extract_figures(payload: dict[str, Any]) -> list[dict[str, Any]]:
        figures = payload.get("figures") or payload.get("pictures") or []
        normalized = []
        for index, item in enumerate(figures, start=1):
            caption = DoclingParser._resolve_caption(item, payload)
            prov = item.get("prov", [])

            if DoclingParser._is_decorative_figure(caption, prov):
                continue

            normalized.append(
                {
                    "caption": caption,
                    "page": DoclingParser._resolve_page(item),
                    "figure_role": item.get("role") or "unknown",
                    "prov": prov,
                }
            )
        return normalized

    @staticmethod
    def _extract_caption_blocks(page_blocks: list[dict[str, Any]], label_regex: str, source: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pattern = re.compile(label_regex, re.IGNORECASE)
        stop_pattern = re.compile(r"^(?:figure|fig\.?|scheme|table)\s+\d+[\.:：-]?", re.IGNORECASE)
        for block in page_blocks:
            page = block.get("page")
            lines = [line.strip() for line in (block.get("text") or "").splitlines()]
            for index, line in enumerate(lines):
                if not pattern.match(line):
                    continue
                if not DoclingParser._looks_like_fallback_caption_start(line, source):
                    continue
                parts = [line]
                for next_line in lines[index + 1 : index + 12]:
                    if not next_line:
                        break
                    if stop_pattern.match(next_line):
                        break
                    if re.match(r"^(?:references|acknowledg|associated content)\b", next_line, re.IGNORECASE):
                        break
                    parts.append(next_line)
                    if len(" ".join(parts)) > 900:
                        break
                caption = re.sub(r"\s+", " ", " ".join(parts)).strip()
                if caption:
                    results.append(
                        {
                            "caption": caption,
                            "markdown_content": "\n".join(parts) if source == "table" else None,
                            "page": page,
                            "extraction_source": "pypdf_caption_fallback",
                            "prov": [{"page_no": page, "fallback": "pypdf_caption"}] if page else [],
                        }
                    )
        return results

    @staticmethod
    def _extract_fallback_tables(page_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tables = DoclingParser._extract_caption_blocks(page_blocks, r"^\s*table\s+\d+[\.:：-]?", "table")
        return [
            {
                "caption": item["caption"],
                "markdown_content": item.get("markdown_content") or item["caption"],
                "page": item.get("page"),
                "extraction_source": item["extraction_source"],
                "prov": item.get("prov") or [],
            }
            for item in tables
        ]

    @staticmethod
    def _extract_fallback_figures(page_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        figures = DoclingParser._extract_caption_blocks(page_blocks, r"^\s*(?:figure|fig\.?|scheme)\s+\d+[\.:：-]?", "figure")
        normalized = []
        seen_by_page_number: dict[tuple[int | None, int], int] = {}
        for item in figures:
            caption = item["caption"]
            if DoclingParser._is_decorative_figure(caption, item.get("prov") or []):
                continue
            payload = {
                "caption": caption,
                "page": item.get("page"),
                "figure_role": "unknown",
                "prov": item.get("prov") or [],
            }
            number_match = re.search(r"(?:figure|fig\.?|scheme)\s*(\d+)", caption, re.IGNORECASE)
            key = (item.get("page"), int(number_match.group(1))) if number_match else None
            if key is not None and key in seen_by_page_number:
                existing_index = seen_by_page_number[key]
                existing_caption = normalized[existing_index]["caption"] or ""
                if caption[:4].isupper() and not existing_caption[:4].isupper():
                    normalized[existing_index] = payload
                continue
            if key is not None:
                seen_by_page_number[key] = len(normalized)
            normalized.append(payload)
        return normalized

    @staticmethod
    def _fallback_parse(pdf_path: Path) -> DoclingParseResult:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")
        text_pages: list[str] = []
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            for page in reader.pages:
                text_pages.append(page.extract_text() or "")
        except Exception as e:
            return DoclingParser._warning_fallback_result(
                f"[Warning] Failed to read PDF text with pypdf: {e}"
            )

        if not text_pages:
            return DoclingParser._warning_fallback_result(
                f"[Warning] No pages extracted from PDF file {pdf_path}. The file may be empty or corrupted."
            )

        markdown_parts = []
        page_blocks = []
        is_empty = all(not text.strip() for text in text_pages)
        if is_empty:
            warning_msg = "[Warning] This is a scanned PDF. No OCR text could be extracted."
            markdown_parts.append(f"## Page 1\n\n{warning_msg}\n")
            page_blocks.append({"page": 1, "text": warning_msg})
        else:
            for index, text in enumerate(text_pages, start=1):
                markdown_parts.append(f"## Page {index}\n\n{text.strip()}\n")
                page_blocks.append({"page": index, "text": text})

        tables = DoclingParser._extract_fallback_tables(page_blocks)
        figures = DoclingParser._extract_fallback_figures(page_blocks)
        payload = {
            "pages": page_blocks,
            "tables": tables,
            "figures": figures,
            "fallback": True,
        }
        return DoclingParseResult(
            markdown="\n".join(markdown_parts).strip(),
            json_payload=payload,
            tables=tables,
            figures=figures,
            page_blocks=page_blocks,
        )

    @staticmethod
    def _warning_fallback_result(warning_msg: str) -> DoclingParseResult:
        page_blocks = [{"page": 1, "text": warning_msg}]
        payload = {
            "pages": page_blocks,
            "tables": [],
            "figures": [],
            "fallback": True,
            "parse_warning": warning_msg,
        }
        return DoclingParseResult(
            markdown=f"## Page 1\n\n{warning_msg}",
            json_payload=payload,
            tables=[],
            figures=[],
            page_blocks=page_blocks,
        )
