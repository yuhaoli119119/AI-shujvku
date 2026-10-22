"""Versioned, zero-based merged-cell provenance; never a value inheritance rule."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MergedCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["merged_cell_v1"] = "merged_cell_v1"
    coordinate_basis: Literal["zero_based_data_rows"]
    merged_group_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\s\x00]+$")
    role: Literal["anchor", "continuation"]
    anchor_row: int = Field(ge=0, le=2147483647, strict=True)
    anchor_column: int = Field(ge=0, le=2147483647, strict=True)
    row_span: int = Field(ge=1, le=10000, strict=True)
    column_span: int = Field(ge=1, le=200, strict=True)
    cell_content: Literal["printed", "blank"]
    # Optional normalized rectangle in the source page, never inferred from spans.
    cell_bbox: list[Annotated[float, Field(strict=True, allow_inf_nan=False)]] | None = Field(default=None, min_length=4, max_length=4)
    bbox_basis: Literal["source_page_normalized"] | None = None

    @model_validator(mode="after")
    def geometry(self):
        if self.row_span * self.column_span <= 1:
            raise ValueError("merged_cell_requires_span")
        if self.role == "continuation" and self.cell_content != "blank":
            raise ValueError("merged_continuation_must_be_blank")
        if (self.cell_bbox is None) != (self.bbox_basis is None):
            raise ValueError("merged_bbox_basis_required")
        if self.cell_bbox is not None:
            x0, y0, x1, y1 = self.cell_bbox
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("merged_bbox_invalid")
        return self


def validate_cell_location(cell, row, column, *, rows=None, columns=None):
    cell = MergedCell.model_validate(cell)
    if type(row) is not int or type(column) is not int:
        raise ValueError("merged_cell_scalar_coordinates_required")
    if not (cell.anchor_row <= row < cell.anchor_row + cell.row_span and
            cell.anchor_column <= column < cell.anchor_column + cell.column_span):
        raise ValueError("merged_cell_coordinate_outside_span")
    if (cell.role == "anchor") != ((row, column) == (cell.anchor_row, cell.anchor_column)):
        raise ValueError("merged_cell_role_coordinate_mismatch")
    if rows is not None and cell.anchor_row + cell.row_span > rows:
        raise ValueError("merged_cell_row_out_of_range")
    if columns is not None and cell.anchor_column + cell.column_span > columns:
        raise ValueError("merged_cell_column_out_of_range")
    return cell


class MergedCellSource(BaseModel):
    merged_cell: MergedCell | None = None

    @model_validator(mode="after")
    def merged_location(self):
        if self.merged_cell is not None:
            if getattr(self, "source_kind", "table") != "table":
                raise ValueError("merged_cell_requires_table_source")
            validate_cell_location(self.merged_cell, self.table_row, self.table_column)
        return self
