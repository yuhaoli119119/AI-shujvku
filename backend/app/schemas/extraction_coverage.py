"""Explicit evidence-backed declarations, not an inferred PDF inventory."""
from typing import Any, Literal, Annotated
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

Hash = str
PhysicalPage = Annotated[int, Field(strict=True, ge=1)]

class CoverageModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class CoverageFile(CoverageModel):
    file_id: UUID
    source_sha256: Hash = Field(pattern=r'^[0-9a-f]{64}$')
    reading_pdf_sha256: Hash = Field(pattern=r'^[0-9a-f]{64}$')
    declared_pages: list[PhysicalPage] = Field(max_length=10000)
    scope_note: str = Field(min_length=1, max_length=4000)

class CoverageSource(CoverageModel):
    file_id: UUID
    pages: list[PhysicalPage] = Field(min_length=1, max_length=1000)
    locator: str = Field(min_length=1, max_length=4000)
    quote: str | None = Field(default=None, max_length=8000)
    table_row: int | None = Field(default=None, ge=0, strict=True)
    table_column: int | None = Field(default=None, ge=0, strict=True)

class CoverageReason(CoverageModel):
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=4000)
    checked_pages: list[PhysicalPage] = Field(default_factory=list, max_length=10000)
    method: str | None = Field(default=None, max_length=4000)

class CoverageLink(CoverageModel):
    row_id: UUID | None = None
    row_key: str | None = Field(default=None, max_length=255)
    value_id: UUID | None = None
    source_id: UUID | None = None
    asset_id: UUID | None = None
    asset_key: str | None = Field(default=None, max_length=255)
    section_id: UUID | None = None

class CoverageItem(CoverageModel):
    item_key: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9_.:-]+$')
    title: str = Field(min_length=1, max_length=500)
    kind: Literal['row', 'value', 'table', 'figure', 'subfigure', 'section']
    source: CoverageSource
    identity: dict[str, str | None] = Field(default_factory=dict, max_length=20)
    condition: dict[str, Any] = Field(default_factory=dict, max_length=100)
    field_name: str | None = Field(default=None, min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)
    subfigure_label: str | None = Field(default=None, max_length=128)
    expected_columns: list[str] | None = Field(default=None, min_length=1, max_length=200)
    origin: Literal['reported', 'estimated', 'derived', 'image_pending'] = 'reported'
    state: Literal['pending', 'extracted', 'not_reported', 'unknown', 'not_applicable']
    reason: CoverageReason | None = None
    link: CoverageLink | None = None

    @model_validator(mode='after')
    def state_contract(self):
        if self.state != 'extracted' and self.reason is None:
            raise ValueError('coverage_reason_required')
        if self.state == 'extracted' and self.link is None:
            raise ValueError('coverage_exact_link_required')
        if self.state != 'extracted' and self.link is not None:
            raise ValueError('coverage_nonextracted_link_not_allowed')
        if self.kind in {'row', 'value'} and not (self.identity.get('material') or '').strip():
            raise ValueError('coverage_material_identity_required')
        if self.kind == 'value' and self.field_name is None:
            raise ValueError('coverage_field_name_required')
        if self.state == 'not_reported' and (not self.reason.checked_pages or not (self.reason.method or '').strip()):
            raise ValueError('coverage_not_reported_requires_checked_scope_and_method')
        if self.kind != 'value' and self.field_name is not None:
            raise ValueError('coverage_field_only_for_value')
        if self.kind != 'table' and self.expected_columns is not None:
            raise ValueError('coverage_columns_only_for_table')
        if self.kind not in {'row','value'} and (self.source.table_row is not None or self.source.table_column is not None):
            raise ValueError('coverage_cell_coordinates_only_for_scientific_fact')
        return self

class CoveragePlanRequest(CoverageModel):
    request_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_plan_revision: Hash | None = Field(pattern=r'^[0-9a-f]{64}$')
    expected_output_revision: Hash = Field(pattern=r'^[0-9a-f]{64}$')
    files: list[CoverageFile] = Field(min_length=1, max_length=200)
    items: list[CoverageItem] = Field(max_length=2000)
