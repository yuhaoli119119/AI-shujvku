"""Bounded input contracts for the rebuilt literature MCP (no dispatch tools)."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from app.schemas.rebuild import RebuildVisualAssetRequest, RebuildVisualAssetCropRequest, RebuildSectionInput
from app.schemas.reading_assistance import ReadingAssistanceSave, AbstractTranslationSave

Revision = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
RequestId = Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$')]
WorkId = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class PaperRequest(Contract):
    paper_id: UUID


class Catalog(Contract):
    library_name: str | None = Field(default=None, max_length=255)
    query: str | None = Field(default=None, max_length=512)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class Workspace(PaperRequest):
    view: Literal['compact', 'rows', 'assets', 'sections', 'legacy_tables',
                  'delivery', 'delivery_checkpoints', 'delivery_gaps', 'delivery_receipts', 'delivery_jobs'] = 'compact'
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=25, ge=1, le=100, strict=True)
    expected_revision: Revision | None = None


class ReadSourceText(PaperRequest):
    file_id: UUID
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=5, ge=1, le=20, strict=True)
    source_sha256: Revision
    reading_pdf_sha256: Revision | None = None


class ViewSourcePage(PaperRequest):
    file_id: UUID
    page: int = Field(ge=1, strict=True)
    dpi: Literal[72, 120, 160] = 120
    source_sha256: Revision
    reading_pdf_sha256: Revision | None = None


class ViewFigureImage(PaperRequest):
    asset_id: UUID


class ListRows(PaperRequest):
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=25, ge=1, le=100, strict=True)
    expected_revision: Revision | None = None


class Quality(Workspace):
    include_asset_mechanics: bool = False
    asset_offset: int = Field(default=0, ge=0, strict=True)
    asset_limit: int = Field(default=16, ge=1, le=32, strict=True)
    expected_coverage_revision: Revision | None = None


class BeginWork(PaperRequest):
    request_id: RequestId
    file_ids: list[UUID] = Field(min_length=1, max_length=30)
    file_set_revision: Revision

    @model_validator(mode='after')
    def unique_files(self):
        if len(self.file_ids) != len(set(self.file_ids)):
            raise ValueError('duplicate_file_ids')
        return self


class WorkMutation(PaperRequest):
    work_id: WorkId
    request_id: RequestId
    expected_revision: Revision


class ExternalAsset(RebuildVisualAssetRequest):
    # Clients may refer to registered sources, never arbitrary filesystem paths.
    image_path: None = None
    request_id: None = None
    task_id: None = None
    file_id: UUID
    source_sha256: Revision
    status: Literal['draft', 'unreadable'] = 'draft'


class ExternalCrop(RebuildVisualAssetCropRequest):
    image_path: None = None
    request_id: None = None
    task_id: None = None
    file_id: UUID
    source_sha256: Revision
    status: Literal['draft', 'unreadable'] = 'draft'


class SaveAssets(WorkMutation):
    assets: list[ExternalAsset] = Field(min_length=1, max_length=50)


class CropFigure(WorkMutation):
    crop: ExternalCrop


class PreviewRows(PaperRequest):
    # Row/cell validation is intentionally performed in isolated savepoints by
    # the existing importer, preserving useful per-cell rejection receipts.
    rows: list[dict[str, JsonValue]] = Field(min_length=1, max_length=100)


class SaveDataRows(WorkMutation):
    rows: list[dict[str, JsonValue]] = Field(min_length=1, max_length=100)


class SaveSections(WorkMutation):
    sections: list[RebuildSectionInput] = Field(min_length=1, max_length=30)


class SaveReading(WorkMutation):
    reading: ReadingAssistanceSave | AbstractTranslationSave

    @model_validator(mode='after')
    def bound_identity(self):
        if self.reading.job_id != self.work_id or self.reading.request_id != self.request_id:
            raise ValueError('reading_work_and_request_identity_mismatch')
        return self


class FileStage(Contract):
    file_id: UUID
    stage: Literal['read', 'assets', 'rows', 'reading']
    status: Literal['running', 'completed', 'partial', 'failed', 'not_applicable']
    message: str = Field(default='', max_length=4000)


class ReportWork(WorkMutation):
    status: Literal['running', 'needs_check'] = 'running'
    message: str = Field(min_length=1, max_length=8000)
    file_stages: list[FileStage] = Field(default_factory=list, max_length=120)
    missing_items: list[Annotated[str, Field(max_length=4000)]] = Field(default_factory=list, max_length=100)


class FinishWork(WorkMutation):
    status: Literal['completed', 'partial', 'failed']
    message: str = Field(min_length=1, max_length=8000)
    file_stages: list[FileStage] = Field(default_factory=list, max_length=120)
    missing_items: list[Annotated[str, Field(max_length=4000)]] = Field(default_factory=list, max_length=100)


class WriteReceipt(PaperRequest):
    work_id: WorkId
    request_id: RequestId
