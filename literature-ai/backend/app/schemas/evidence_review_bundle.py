from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.dft_review_bundle import OfflineReviewSource, OverallReviewStatus


FigureEvidenceAction = Literal["KEEP", "RECROP", "CREATE", "REJECT", "NEEDS_HUMAN"]
TableEvidenceAction = Literal["KEEP", "UPDATE", "CREATE", "MERGE", "DELETE", "NEEDS_HUMAN"]
DFTEvidenceSourceKind = Literal["figure", "table"]
DFTRelevance = Literal["none", "possible", "explicit_dft", "unknown"]


def _strip_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _normalize_string_list(value: list[Any] | None) -> list[Any] | None:
    if value is None:
        return None
    normalized: list[Any] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text)
        else:
            normalized.append(item)
    return normalized


class OfflineEvidenceBBoxMixin(BaseModel):
    bbox_norm: list[float] | None = Field(
        default=None,
        description="Normalized PDF page bbox [x0, y0, x1, y1] in top-left page coordinates, each value in [0, 1].",
    )

    @field_validator("bbox_norm")
    @classmethod
    def validate_bbox_norm(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        if len(value) != 4:
            raise ValueError("bbox_norm must contain exactly four numbers")
        coords = [float(item) for item in value]
        if any(item < 0.0 or item > 1.0 for item in coords):
            raise ValueError("bbox_norm coordinates must be between 0 and 1")
        if coords[0] >= coords[2] or coords[1] >= coords[3]:
            raise ValueError("bbox_norm must satisfy x0 < x1 and y0 < y1")
        return coords


class OfflineEvidenceFigureAction(OfflineEvidenceBBoxMixin):
    model_config = ConfigDict(extra="forbid")

    action: FigureEvidenceAction
    figure_id: str | None = Field(default=None, max_length=64)
    source_paper_id: str | None = Field(default=None, max_length=64)
    page: int | None = Field(default=None, ge=1)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_checked: bool
    figure_label: str | None = Field(default=None, max_length=64)
    caption: str | None = None
    figure_role: str | None = Field(default=None, max_length=128)
    content_summary: str | None = None
    key_elements: list[Any] | None = None
    dft_relevance: DFTRelevance = "unknown"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    blocking_errors: list[str] = Field(default_factory=list)

    @field_validator("figure_id", "source_paper_id", "figure_label", "figure_role")
    @classmethod
    def strip_optional_ids(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("caption", "content_summary")
    @classmethod
    def strip_optional_long_text(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped

    @field_validator("evidence_ids", "blocking_errors")
    @classmethod
    def normalize_string_lists(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @field_validator("key_elements")
    @classmethod
    def normalize_key_elements(cls, value: list[Any] | None) -> list[Any] | None:
        return _normalize_string_list(value)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "OfflineEvidenceFigureAction":
        if self.action in {"KEEP", "RECROP", "REJECT"} and not self.figure_id:
            raise ValueError(f"{self.action} requires figure_id")
        if self.action == "CREATE" and self.figure_id:
            raise ValueError("CREATE must not reuse an existing figure_id")
        if self.action == "CREATE" and not self.source_paper_id:
            raise ValueError("CREATE requires source_paper_id")
        if self.action in {"RECROP", "CREATE"}:
            if self.page is None:
                raise ValueError(f"{self.action} requires page")
            if self.bbox_norm is None:
                raise ValueError(f"{self.action} requires bbox_norm")
        return self


class OfflineEvidenceTableAction(OfflineEvidenceBBoxMixin):
    model_config = ConfigDict(extra="forbid")

    action: TableEvidenceAction
    table_id: str | None = Field(default=None, max_length=64)
    source_table_id: str | None = Field(default=None, max_length=64)
    target_table_id: str | None = Field(default=None, max_length=64)
    source_paper_id: str | None = Field(default=None, max_length=64)
    page: int | None = Field(default=None, ge=1)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_checked: bool
    caption: str | None = None
    complete_markdown: str | None = None
    structured_rows: list[dict[str, Any]] | None = None
    footnotes: list[str] = Field(default_factory=list)
    dft_relevance: DFTRelevance = "unknown"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    blocking_errors: list[str] = Field(default_factory=list)

    @field_validator("table_id", "source_table_id", "target_table_id", "source_paper_id")
    @classmethod
    def strip_optional_ids(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("caption", "complete_markdown")
    @classmethod
    def strip_optional_text_fields(cls, value: str | None) -> str | None:
        return _strip_optional_text(value)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped

    @field_validator("evidence_ids", "footnotes", "blocking_errors")
    @classmethod
    def normalize_string_lists(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @model_validator(mode="after")
    def validate_action_shape(self) -> "OfflineEvidenceTableAction":
        if self.action in {"KEEP", "UPDATE", "DELETE"} and not self.table_id:
            raise ValueError(f"{self.action} requires table_id")
        if self.action == "CREATE":
            if self.table_id:
                raise ValueError("CREATE must not reuse an existing table_id")
            if not self.source_paper_id:
                raise ValueError("CREATE requires source_paper_id")
            if not (self.caption or self.complete_markdown):
                raise ValueError("CREATE requires caption or complete_markdown")
        if self.action == "MERGE":
            if not self.source_table_id or not self.target_table_id:
                raise ValueError("MERGE requires source_table_id and target_table_id")
            if self.source_table_id == self.target_table_id:
                raise ValueError("MERGE source_table_id and target_table_id must differ")
        if self.action in {"UPDATE", "CREATE"} and self.complete_markdown is None:
            raise ValueError(f"{self.action} requires complete_markdown")
        return self


class OfflineFigureTableDFTEvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_kind: DFTEvidenceSourceKind
    source_record_id: str | None = Field(default=None, max_length=64)
    evidence_id: str | None = Field(default=None, max_length=96)
    page: int | None = Field(default=None, ge=1)
    material_identity: str | None = None
    property_type: str | None = None
    value: Any = None
    unit: str | None = None
    figure_label: str | None = Field(default=None, max_length=64)
    table_caption: str | None = None
    raw_text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)

    @field_validator(
        "source_record_id",
        "evidence_id",
        "material_identity",
        "property_type",
        "unit",
        "figure_label",
        "table_caption",
        "raw_text",
        "reason",
    )
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_required_text(self) -> "OfflineFigureTableDFTEvidenceCandidate":
        if not self.raw_text:
            raise ValueError("raw_text must not be blank")
        if not self.reason:
            raise ValueError("reason must not be blank")
        return self


class OfflineEvidenceReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["offline_figure_table_evidence_review_result_v1"] = (
        "offline_figure_table_evidence_review_result_v1"
    )
    bundle_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    paper_id: str = Field(min_length=1, max_length=64)
    paper_code: str = Field(min_length=1, max_length=64)
    review_source: OfflineReviewSource
    overall_status: OverallReviewStatus
    figure_actions: list[OfflineEvidenceFigureAction] = Field(default_factory=list)
    table_actions: list[OfflineEvidenceTableAction] = Field(default_factory=list)
    dft_evidence_candidates: list[OfflineFigureTableDFTEvidenceCandidate] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("bundle_fingerprint", "paper_id", "paper_code")
    @classmethod
    def strip_identity(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identity value must not be blank")
        return stripped

    @field_validator("uncertainties", "notes")
    @classmethod
    def normalize_notes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


OfflineEvidenceReviewFigureAction = OfflineEvidenceFigureAction
OfflineEvidenceReviewTableAction = OfflineEvidenceTableAction
