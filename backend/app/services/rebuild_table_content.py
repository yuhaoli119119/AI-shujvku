"""Lossless table transport. This contract never reconstructs scientific cells."""
from __future__ import annotations

from typing import Literal
from uuid import UUID
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TableSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: UUID
    page_number: int = Field(ge=1, strict=True)
    role: Literal["caption", "body", "footnote"] = "body"
    locator: str = Field(min_length=1, max_length=4000)
    quote: str | None = None
    table_row: int | None = Field(default=None, ge=0, strict=True)
    table_column: int | None = Field(default=None, ge=0, strict=True)


class TableData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    columns: list[str] = Field(min_length=1, max_length=200)
    rows: list[list[str | None]] = Field(max_length=10000)
    footnotes: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table_row_width_mismatch")
        return self


def table_write_contract() -> dict:
    return {
        "version": "table_content_v1",
        "storage": "rebuild_visual_assets.provenance; no paper_tables dual write",
        "fields": {"table_data": "columns: string[], rows: (string|null)[][], footnotes: string[]",
                   "table_markdown": "original markdown string",
                   "table_sources": "file_id, physical page_number, role caption/body/footnote, locator, optional quote/table_row/table_column"},
        "priority": ["table_data", "table_markdown", "legacy markdown_content", "context_text"],
        "completion": "Plain text remains readable/copyable but is pending_structure, not complete. A formal representation still requires independent scientific review. Legacy bodies must be source-bound and frozen.",
        "rules": ["Keep original precision, non-numeric markers and missing cells; never infer rows or columns.",
                  "context_text is an exact plain-text fallback, not parsed as a table.",
                  "For new structured/markdown bodies provide same-file sources for caption and body on their actual physical pages; include all source pages in page_numbers.",
                  "Missing content remains empty/pending; extracted does not mean accepted."],
    }


def validate_table_provenance(provenance: dict, *, file_id, page_numbers: list, caption=None) -> None:
    """Validate only explicitly submitted body fields; legacy reads stay permissive."""
    if "table_markdown" in provenance and not isinstance(provenance["table_markdown"], str):
        raise ValueError("table_markdown_must_be_text")
    data = TableData.model_validate(provenance["table_data"]) if "table_data" in provenance else None
    has_body = data is not None or bool(provenance.get("table_markdown", "").strip())
    if has_body and not provenance.get("table_sources"):
        raise ValueError("table_body_sources_required")
    for raw in provenance.get("table_sources", []):
        source = TableSource.model_validate(raw)
        if not file_id or source.file_id != UUID(str(file_id)):
            raise ValueError("table_source_wrong_file")
        if source.page_number not in page_numbers:
            raise ValueError("table_source_page_not_in_asset")
        if not source.locator.strip():
            raise ValueError("table_source_locator_required")
        if data is not None:
            if source.table_row is not None and source.table_row >= len(data.rows):
                raise ValueError("table_source_row_out_of_range")
            if source.table_column is not None and source.table_column >= len(data.columns):
                raise ValueError("table_source_column_out_of_range")
    if has_body and not any(source.get("role", "body") == "body" for source in provenance["table_sources"]):
        raise ValueError("table_body_source_required")
    if has_body and caption and str(caption).strip() and not any(source.get("role") == "caption" for source in provenance["table_sources"]):
        raise ValueError("table_caption_source_required")


def table_content(asset) -> dict | None:
    if asset.asset_type != "table":
        return None
    provenance = asset.provenance if isinstance(asset.provenance, dict) else {}
    common = {"version": "table_content_v1", "file_id": str(asset.file_id) if asset.file_id else None,
              "page_numbers": asset.page_numbers or [], "sources": provenance.get("table_sources", [])}
    if isinstance(provenance.get("table_data"), dict):
        try:
            data = TableData.model_validate(provenance["table_data"]).model_dump()
            return {**common, "format": "structured", "data": data}
        except ValueError:
            pass  # Old malformed payload is not silently turned into invented cells.
    markdown = provenance.get("table_markdown")
    if isinstance(markdown, str) and markdown.strip():
        return {**common, "format": "markdown", "text": markdown}
    text = asset.context_text
    if isinstance(text, str) and text.strip():
        return {**common, "format": "text", "text": text}
    return {**common, "format": "empty", "text": ""}


def representation_complete(content: dict) -> bool:
    """Check representation syntax only; never infer scientific cells or truth."""
    if content["format"] == "structured":
        return bool(content["data"]["rows"])
    if content["format"] != "markdown":
        return False
    lines = content["text"].replace("\r\n", "\n").replace("\r", "\n").splitlines()
    def cells(line):
        return [part.strip() for part in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
    for index in range(1, len(lines)):
        header, divider = cells(lines[index - 1]), cells(lines[index])
        if "|" not in lines[index - 1] or len(header) < 2 or len(divider) != len(header):
            continue
        if not all(re.fullmatch(r":?-{3,}:?", part) for part in divider):
            continue
        rows = []
        for line in lines[index + 1:]:
            if not line.strip() or "|" not in line:
                break
            rows.append(cells(line))
        if rows and all(len(row) == len(header) for row in rows):
            return True
    return False
