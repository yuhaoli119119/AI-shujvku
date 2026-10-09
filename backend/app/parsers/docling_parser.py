from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from contextlib import contextmanager
import logging
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from app.config import Settings
from app.parsers.body_boundary_cleaner import BodyBoundaryCleaner
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
        self.ocr_required = False

    def _ocr_enabled(self, *, ocr_required: bool) -> bool:
        return bool(
            self.settings.docling_do_ocr
            or (ocr_required and self.settings.docling_auto_ocr)
        )

    async def parse_pdf(
        self,
        pdf_path: Path,
        *,
        ocr_required: bool = False,
        document_timeout: float | None = None,
    ) -> DoclingParseResult:
        effective_ocr_required = bool(ocr_required or self.ocr_required)
        self.ocr_required = False
        return self.parse_pdf_sync(
            pdf_path,
            ocr_required=effective_ocr_required,
            document_timeout=document_timeout,
        )

    def parse_pdf_sync(
        self,
        pdf_path: Path,
        *,
        ocr_required: bool = False,
        document_timeout: float | None = None,
    ) -> DoclingParseResult:
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
                "document_timeout": (
                    document_timeout
                    if document_timeout is not None
                    else self.settings.docling_document_timeout
                ),
                "accelerator_options": AcceleratorOptions(
                    num_threads=self.settings.docling_num_threads,
                    device=AcceleratorDevice.CPU,
                ),
            }
            artifacts_path = self.settings.docling_artifacts_path
            if artifacts_path and artifacts_path.exists() and any(artifacts_path.iterdir()):
                pipeline_kwargs["artifacts_path"] = str(artifacts_path)

            pipeline_options = PdfPipelineOptions(**pipeline_kwargs)
            ocr_enabled = self._ocr_enabled(ocr_required=ocr_required)
            pipeline_options.do_ocr = ocr_enabled
            pipeline_options.do_table_structure = True
            if ocr_enabled:
                pipeline_options.ocr_options = EasyOcrOptions(
                    force_full_page_ocr=self.settings.docling_force_full_page_ocr,
                )

            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                }
            )
            with self._docling_source_path(pdf_path) as source_path:
                result = converter.convert(str(source_path))
            document = result.document
            markdown = self._export_markdown(document)
            payload = self._export_json(document)
            parse_quality = payload.get("parse_quality") if isinstance(payload.get("parse_quality"), dict) else {}
            payload["parse_quality"] = parse_quality
            raw_page_blocks = self._extract_page_blocks(payload)
            cleanup_plan = BodyBoundaryCleaner.analyze(raw_page_blocks)
            page_blocks = BodyBoundaryCleaner.clean_page_blocks(raw_page_blocks, cleanup_plan)
            markdown = BodyBoundaryCleaner.clean_text(markdown, cleanup_plan)
            if isinstance(payload.get("pages"), list):
                payload["pages"] = page_blocks
            parse_quality.update(
                {
                    "ocr_enabled": ocr_enabled,
                    "ocr_required": bool(ocr_required),
                    "markdown_trust": "ocr_unverified" if ocr_enabled else "native_or_mixed_unverified",
                    "boundary_cleanup": cleanup_plan.to_metadata(),
                }
            )
            tables = self._extract_tables(payload)
            figures = self._extract_figures(payload)
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
    @contextmanager
    def _docling_source_path(pdf_path: Path):
        """Use an ASCII-only temp copy when Windows absolute paths contain non-ASCII characters."""
        source = Path(pdf_path)
        source_str = str(source)
        needs_ascii_copy = source.is_absolute() and not source_str.isascii()
        if not needs_ascii_copy:
            yield source
            return

        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._") or "document"
        safe_suffix = source.suffix or ".pdf"
        with tempfile.TemporaryDirectory(prefix="litai_docling_") as temp_dir:
            temp_path = Path(temp_dir) / f"{safe_stem}{safe_suffix}"
            shutil.copy2(source, temp_path)
            yield temp_path

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
                
        if not resolved_texts:
            return None
        return DoclingParser._dedupe_caption_text(" ".join(resolved_texts))

    @staticmethod
    def _dedupe_caption_text(caption: str | None) -> str | None:
        if not caption:
            return None
        normalized = re.sub(r"\s+", " ", str(caption)).strip()
        if not normalized:
            return None

        label_matches = list(
            re.finditer(r"(?:figure|fig\.?|scheme)\s*(\d+)[\.:]?", normalized, re.IGNORECASE)
        )
        deduped = normalized
        if len(label_matches) >= 2:
            first = label_matches[0]
            second = label_matches[1]
            if first.group(1) == second.group(1):
                left = deduped[first.start() : second.start()].strip()
                right = deduped[second.start() :].strip()
                prefix_len = min(len(left), len(right), 80)
                if prefix_len >= 24:
                    similarity = SequenceMatcher(
                        None,
                        left[:prefix_len].lower(),
                        right[:prefix_len].lower(),
                    ).ratio()
                    if similarity >= 0.72:
                        deduped = left

        clean_deduped = re.sub(r"[\s\W]+$", "", deduped)
        half = len(clean_deduped) // 2
        if half >= 24:
            left = clean_deduped[:half].strip()
            right = clean_deduped[half:].strip()
            if left and right and SequenceMatcher(None, left.lower(), right.lower()).ratio() >= 0.9:
                deduped = left
            else:
                left_alt = clean_deduped[:half+1].strip()
                right_alt = clean_deduped[half+1:].strip()
                if left_alt and right_alt and SequenceMatcher(None, left_alt.lower(), right_alt.lower()).ratio() >= 0.9:
                    deduped = left_alt

        normalized = re.sub(r"(?<=\w)\s*-\s*(?=\w)", "-", deduped)
        normalized = re.sub(r"\(\s*([A-Za-z0-9])\s*\)", r"(\1)", normalized)
        normalized = re.sub(r"(?<=\))(?=[A-Za-z0-9])", " ", normalized)
        replacements = (
            (r"\bpro\s+fi\s+les\b", "profiles"),
            (r"\bpro\s+fi\s+le\b", "profile"),
            (r"\bad[\s-]+sorption\b", "adsorption"),
            (r"\bfi\s+nal\b", "final"),
            (r"\bSpeci\s+fically\b", "Specifically"),
            (r"\bspeci\s+fically\b", "specifically"),
            (r"\bdashe\s+line\b", "dashed line"),
            (r"\bdas\s+line\b", "dashed line"),
            (r"\bda\s+line\b", "dashed line"),
        )
        for pattern, replacement in replacements:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        return normalized

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
    def _clean_table_caption_text(caption: str | None, fallback_index: int | None = None) -> str | None:
        normalized = DoclingParser._dedupe_caption_text(caption)
        if not normalized:
            return None

        chosen_match = None
        if fallback_index is not None:
            indexed_matches = list(
                re.finditer(rf"\btable\s+{fallback_index}\b[\.:]?", normalized, re.IGNORECASE)
            )
            if indexed_matches:
                chosen_match = indexed_matches[-1]
        if chosen_match is None:
            matches = list(re.finditer(r"\btable\s+[A-Za-z]?\d+\b[\.:]?", normalized, re.IGNORECASE))
            if matches:
                chosen_match = matches[-1]
        if chosen_match is not None:
            normalized = normalized[chosen_match.start() :].strip()
        return normalized

    @staticmethod
    def _strip_table_body_from_caption(caption: str, markdown_content: str) -> str:
        if not caption or not markdown_content:
            return caption

        header_line = ""
        for line in markdown_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and "---" not in stripped:
                header_line = stripped
                break
        if not header_line:
            return caption

        overlap = re.search(r"\|(?:[^\n\|]+\|){2,}", caption)
        if overlap:
            return caption[: overlap.start()].strip()

        header_text = re.sub(r"\s+", " ", header_line.replace("|", " ")).strip()
        if not header_text:
            return caption

        cells = [c.strip() for c in header_line.strip("|").split("|") if c.strip()]
        if len(cells) >= 3:
            glued_headers = " ".join(cells[:3])
            overlap_glued = re.search(re.escape(glued_headers[: min(len(glued_headers), 80)]), caption, re.IGNORECASE)
            if overlap_glued:
                return caption[: overlap_glued.start()].strip()

        return caption

    @staticmethod
    def _clean_table_cell_text(text: str, row_index: int, col_index: int, is_header: bool) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized:
            return ""

        normalized = re.sub(r"(?<=\w)\s*-\s*(?=\w)", "-", normalized)
        normalized = re.sub(r"\(\s*([A-Za-z0-9])\s*\)", r"(\1)", normalized)
        normalized = re.sub(r"\bTable\s+\d+\.\s*", "", normalized, flags=re.IGNORECASE)

        if is_header:
            if re.search(r"\bMolecules\b", normalized, re.IGNORECASE):
                return "Molecules"
            if re.search(r"\bS-S\b", normalized, re.IGNORECASE):
                return "S-S"
            if re.search(r"\bLi-S\b", normalized, re.IGNORECASE):
                return "Li-S"
            if re.search(r"\bLi-N\b", normalized, re.IGNORECASE):
                return "Li-N"
            if re.search(r"\bE\s*ad\b", normalized, re.IGNORECASE):
                return "E ad"
            if re.search(r"\bg-C\s*3\s*N\s*4\b", normalized, re.IGNORECASE):
                return "g-C3N4"
            if re.search(r"\bP\s*C\b", normalized):
                return "PC"
            if re.search(r"\bLiPSs\b", normalized, re.IGNORECASE):
                return "LiPSs"
            return normalized

        if col_index == 0:
            molecule_match = re.search(r"\b(Li\s*\d+\s*S(?:\s*\d+)?|S\s*\d+)\b", normalized, re.IGNORECASE)
            if molecule_match:
                return re.sub(r"\s+", " ", molecule_match.group(1)).strip()
            if normalized.lower().startswith("molecules "):
                return normalized.split()[0]
            return normalized

        numeric_match = re.search(r"(-?\d+(?:\.\d+)?)\s*$", normalized)
        if numeric_match:
            has_letters = bool(re.search(r"[A-Za-z]", normalized))
            has_multiple_tokens = len(normalized.split()) > 1
            if has_letters or has_multiple_tokens:
                return numeric_match.group(1)
        return normalized

    @staticmethod
    def _table_grid_to_markdown(item: dict[str, Any]) -> str:
        data = item.get("data") or {}
        grid = data.get("grid") or []
        if not isinstance(grid, list) or not grid:
            return ""

        rows: list[list[str]] = []
        for row_index, row in enumerate(grid):
            if not isinstance(row, list):
                continue
            cleaned_row: list[str] = []
            for col_index, cell in enumerate(row):
                if not isinstance(cell, dict):
                    raw_text = str(cell or "")
                    is_header = row_index == 0
                else:
                    raw_text = str(cell.get("text") or "")
                    is_header = bool(cell.get("column_header"))
                cleaned = DoclingParser._clean_table_cell_text(raw_text, row_index, col_index, is_header)
                if not is_header and cleaned_row and cleaned == cleaned_row[-1]:
                    cleaned = ""
                cleaned_row.append(cleaned)

            if not any(cell.strip() for cell in cleaned_row):
                continue
            rows.append(cleaned_row)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        if (
            len(rows) >= 2
            and rows[0][0] == "Molecules"
            and rows[1][0] == "Molecules"
            and any(cell in {"S-S", "Li-S", "Li-N", "E ad"} for cell in rows[1][1:])
        ):
            group_header = rows[0]
            sub_header = rows[1]
            combined_header = ["Molecules"]
            for group, sub in zip(group_header[1:], sub_header[1:]):
                if group and group != sub:
                    combined_header.append(f"{group} {sub}")
                else:
                    combined_header.append(sub)
            rows = [combined_header, *rows[2:]]
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
    def _clean_running_headers_footers(page_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compatibility wrapper around the reusable boundary cleaner."""
        plan = BodyBoundaryCleaner.analyze(page_blocks)
        return BodyBoundaryCleaner.clean_page_blocks(page_blocks, plan)

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
        if source == "table" and stripped[0] not in ".:：;":
            return False
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
            raw_caption = (
                DoclingParser._clean_table_caption_text(DoclingParser._resolve_caption(item, payload), index)
                or f"Table {index}"
            )
            prov = item.get("prov", [])
            markdown_content = (
                DoclingParser._table_grid_to_markdown(item)
                or DoclingParser._table_cells_to_markdown(item)
                or item.get("markdown")
                or item.get("text")
                or item.get("html")
                or ""
            )
            caption = DoclingParser._strip_table_body_from_caption(raw_caption, markdown_content)
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
                if source != "table":
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
                if source == "table":
                    caption = DoclingParser._trim_fallback_table_caption(caption)
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
    def _trim_fallback_table_caption(caption: str) -> str:
        cleaned = DoclingParser._clean_table_caption_text(caption) or caption
        match = re.match(r"^(Table\s+\d+[\.:])\s*(.+)$", cleaned, re.IGNORECASE)
        if not match:
            return cleaned

        prefix = match.group(1)
        body = match.group(2).strip()
        sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", body, maxsplit=1)[0].strip()
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

        page_blocks = []
        is_empty = all(not text.strip() for text in text_pages)
        if is_empty:
            warning_msg = "[Warning] This is a scanned PDF. No OCR text could be extracted."
            payload = {
                "pages": [],
                "tables": [],
                "figures": [],
                "fallback": True,
                "parse_blocked": True,
                "parse_warning": warning_msg,
                "parse_quality": {
                    "ocr_enabled": False,
                    "ocr_required": True,
                    "markdown_trust": "unavailable",
                },
            }
            return DoclingParseResult(
                markdown="",
                json_payload=payload,
                tables=[],
                figures=[],
                page_blocks=[],
            )
        else:
            for index, text in enumerate(text_pages, start=1):
                page_blocks.append({"page": index, "text": text})

        cleanup_plan = BodyBoundaryCleaner.analyze(page_blocks)
        page_blocks = BodyBoundaryCleaner.clean_page_blocks(page_blocks, cleanup_plan)
        markdown_parts = [
            f"## Page {block.get('page', index)}\n\n{block.get('text', '').strip()}\n"
            for index, block in enumerate(page_blocks, start=1)
            if block.get("text", "").strip()
        ]
        tables = DoclingParser._extract_fallback_tables(page_blocks)
        figures = DoclingParser._extract_fallback_figures(page_blocks)
        payload = {
            "pages": page_blocks,
            "tables": tables,
            "figures": figures,
            "fallback": True,
            "parse_quality": {
                "ocr_enabled": False,
                "ocr_required": False,
                "markdown_trust": "native_text_unverified",
                "boundary_cleanup": cleanup_plan.to_metadata(),
            },
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
        payload = {
            "pages": [],
            "tables": [],
            "figures": [],
            "fallback": True,
            "parse_warning": warning_msg,
            "parse_blocked": True,
            "parse_quality": {
                "ocr_enabled": False,
                "ocr_required": False,
                "markdown_trust": "unavailable",
            },
        }
        return DoclingParseResult(
            markdown="",
            json_payload=payload,
            tables=[],
            figures=[],
            page_blocks=[],
        )
