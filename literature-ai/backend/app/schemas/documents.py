from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class UnifiedSection(BaseModel):
    section_title: str | None = None
    section_type: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None


class UnifiedTable(BaseModel):
    caption: str | None = None
    markdown_content: str | None = None
    page: int | None = None
    extraction_source: str | None = None
    prov: list[Any] = Field(default_factory=list)


class UnifiedFigure(BaseModel):
    caption: str | None = None
    image_path: str | None = None
    page: int | None = None
    figure_role: str | None = None
    role_confidence: float | None = None
    content_summary: str | None = None
    key_elements: list[str] | None = None
    numerical_data_points: list[dict[str, Any]] | None = None
    prov: list[Any] = Field(default_factory=list)


class UnifiedPaperDocument(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    abstract: str = ""
    sections: list[UnifiedSection] = Field(default_factory=list)
    tables: list[UnifiedTable] = Field(default_factory=list)
    figures: list[UnifiedFigure] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    markdown: str = ""
    tei_xml: str = ""
    docling_json: dict[str, Any] = Field(default_factory=dict)
    source_pdf_path: Path
    tei_path: Path | None = None
    markdown_path: Path | None = None
    docling_json_path: Path | None = None
