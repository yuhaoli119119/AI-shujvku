from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings


@dataclass
class DoclingParseResult:
    markdown: str
    json_payload: dict[str, Any]
    tables: list[dict[str, Any]]
    figures: list[dict[str, Any]]
    page_blocks: list[dict[str, Any]]


class DoclingParser:
    """Thin adapter around Docling with a text-only fallback for offline setups."""

    # 装饰图 caption 关键词黑名单
    _DECORATIVE_CAPTION_KEYWORDS: tuple[str, ...] = (
        "crossmark", "cross mark", "checkmark",
        "elsevier", "springer", "wiley", "acs publications", "rsc publishing",
        "royal society", "nature publishing",
        "copyright", "\u00a9",
        "creative commons", "cc-by", "cc by",
        "doi:", "https://doi.org",
        "open access",
    )

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
            page_blocks = payload.get("pages", [])
            return DoclingParseResult(
                markdown=markdown,
                json_payload=payload,
                tables=tables,
                figures=figures,
                page_blocks=page_blocks,
            )
        except Exception:
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
    def _is_decorative_figure(caption: str | None, prov: list) -> bool:
        """判断图片是否为装饰性图片（CrossMark、出版商标识、版权标识等）。"""
        if not caption:
            # 无 caption 的图片大概率是装饰图，但保留有合理 bbox 的（可能是 caption 丢失的真实图片）
            if prov and isinstance(prov, list) and len(prov) > 0:
                item = prov[0]
                bbox = item.get("bbox") if isinstance(item, dict) else getattr(item, "bbox", None)
                if isinstance(bbox, dict):
                    w = abs(bbox.get("r", 0) - bbox.get("l", 0))
                    h = abs(bbox.get("b", 0) - bbox.get("t", 0))
                    # 非常小的图片（< 50pt）且无 caption → 装饰图
                    if w < 50 and h < 50:
                        return True
            return True

        caption_lower = caption.lower().strip()

        # 匹配黑名单关键词
        for kw in DoclingParser._DECORATIVE_CAPTION_KEYWORDS:
            if kw in caption_lower:
                return True

        return False

    @staticmethod
    def _extract_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
        tables = payload.get("tables") or payload.get("table_items") or []
        normalized = []
        for index, item in enumerate(tables, start=1):
            caption = DoclingParser._resolve_caption(item, payload) or f"Table {index}"
            prov = item.get("prov", [])
            normalized.append(
                {
                    "caption": caption,
                    "markdown_content": item.get("markdown") or item.get("text") or "",
                    "page": item.get("page_no") or item.get("page"),
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
            caption = DoclingParser._resolve_caption(item, payload) or f"Figure {index}"
            prov = item.get("prov", [])

            # 过滤装饰性图片（CrossMark、出版商标识等）
            if DoclingParser._is_decorative_figure(caption, prov):
                continue

            normalized.append(
                {
                    "caption": caption,
                    "page": item.get("page_no") or item.get("page"),
                    "figure_role": item.get("role") or "unknown",
                    "prov": prov,
                }
            )
        return normalized

    @staticmethod
    def _fallback_parse(pdf_path: Path) -> DoclingParseResult:
        text_pages: list[str] = []
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            for page in reader.pages:
                text_pages.append(page.extract_text() or "")
        except Exception:
            text_pages = []

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

        payload = {
            "pages": page_blocks,
            "tables": [],
            "figures": [],
            "fallback": True,
        }
        return DoclingParseResult(
            markdown="\n".join(markdown_parts).strip(),
            json_payload=payload,
            tables=[],
            figures=[],
            page_blocks=page_blocks,
        )
