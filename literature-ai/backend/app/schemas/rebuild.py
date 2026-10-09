from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
import json


VisualAssetType = Literal["figure", "table", "subfigure"]
ReactionType = Literal["SRR", "HER", "OER", "ORR", "CO2RR"]
ActiveSiteType = Literal["single_atom", "dual_atom", "cluster", "support", "bulk", "other"]
DataType = Literal["experimental", "dft"]
ValueType = Literal["explicit", "estimated", "derived", "missing"]
AnalysisComparisonField = Literal[
    "material",
    "material_family",
    "support",
    "active_site",
    "active_site_type",
    "configuration",
]


class RebuildValueSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: UUID | None = None
    asset_id: UUID | None = None
    source_key: str | None = Field(default=None, max_length=255)
    source_kind: Literal["explicit", "estimated", "derived", "table", "text", "figure"]
    page_number: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=128)
    table_row: int | None = Field(default=None, ge=0)
    table_column: int | None = Field(default=None, ge=0)
    quote: str | None = None
    estimate_basis: str | None = None


class RebuildDataValueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    raw_value: str | None = None
    numeric_value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=64)
    value_type: ValueType = "explicit"
    precision_digits: int | None = Field(default=None, ge=0, le=12)
    missing_reason: str | None = None
    sources: list[RebuildValueSourceInput] = Field(default_factory=list)


class RebuildDataRowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_key: str | None = Field(default=None, max_length=255)
    reaction: ReactionType
    material: str = Field(min_length=1)
    support: str | None = None
    active_site_type: ActiveSiteType
    active_site: str | None = None
    configuration: str | None = None
    material_family: str | None = Field(default=None, max_length=128)
    data_type: DataType
    condition: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    # Validate each cell in its own savepoint; malformed cells must not reject siblings.
    values: list[Any] = Field(default_factory=list)
    adsorbate: str | None = None
    intermediate: str | None = None
    reaction_step: str | None = None
    adsorption_site: str | None = None
    identity_context: str | None = None


class RebuildRowsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID
    rows: list[Any] = Field(min_length=1, max_length=200)


class RebuildRowIdentityChanges(BaseModel):
    """Only identity columns and the full scientific condition can be corrected."""
    model_config = ConfigDict(extra="forbid", strict=True)

    reaction: ReactionType | None = None
    material: str | None = Field(default=None, min_length=1, max_length=10000)
    support: str | None = Field(default=None, max_length=10000)
    active_site_type: ActiveSiteType | None = None
    active_site: str | None = Field(default=None, max_length=10000)
    configuration: str | None = Field(default=None, max_length=10000)
    material_family: str | None = Field(default=None, max_length=128)
    data_type: DataType | None = None
    condition: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set:
            raise ValueError("identity_changes_required")
        for field in {"reaction", "material", "active_site_type", "data_type", "condition"} & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"identity_field_cannot_be_null:{field}")
        if self.material is not None and not self.material.strip():
            raise ValueError("material_must_not_be_blank")
        if self.condition is not None:
            try:
                encoded = json.dumps(self.condition, allow_nan=False, ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                raise ValueError("condition_must_be_finite_json") from exc
            if len(encoded.encode("utf-8")) > 65536:
                raise ValueError("condition_too_large")
            if any(key.startswith("_rebuild_") for key in self.condition):
                raise ValueError("reserved_condition_key")
        return self


class RebuildRowIdentityCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID
    row_key: str = Field(min_length=1, max_length=255)
    request_id: UUID
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: RebuildRowIdentityChanges
    correction_reason: str = Field(min_length=1, max_length=4000)
    evidence: list[RebuildValueSourceInput] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_reason_and_evidence(self):
        if not self.correction_reason.strip():
            raise ValueError("correction_reason_required")
        for source in self.evidence:
            if not (source.file_id or source.asset_id):
                raise ValueError("correction_evidence_registered_file_or_asset_required")
            if not (source.page_number or (source.quote and source.quote.strip()) or source.asset_id):
                raise ValueError("correction_evidence_locator_required")
        return self


class RebuildAssetsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[RebuildVisualAssetRequest] = Field(min_length=1, max_length=200)


class RebuildValueCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_value: str | None = None
    numeric_value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=64)
    value_type: ValueType = "explicit"
    precision_digits: int | None = Field(default=None, ge=0, le=12)
    missing_reason: str | None = None
    correction_reason: str | None = None
    source: RebuildValueSourceInput | None = None


class RebuildVisualAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_key: str = Field(min_length=1, max_length=255)
    asset_type: VisualAssetType
    file_id: UUID | None = None
    logical_group_key: str | None = Field(default=None, max_length=255)
    figure_label: str | None = Field(default=None, max_length=64)
    subfigure_label: str | None = Field(default=None, max_length=64)
    caption: str | None = None
    page_numbers: list[int] = Field(default_factory=list)
    bbox: dict[str, Any] | None = None
    image_path: str | None = None
    x_axis_unit: str | None = Field(default=None, max_length=64)
    y_axis_unit: str | None = Field(default=None, max_length=64)
    material_mapping: dict[str, Any] | None = None
    context_text: str | None = None
    explanation: str | None = None
    reading_explanation: dict[str, Any] | None = None
    unreadable_fields: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None
    status: Literal["draft", "reviewed", "corrected", "unreadable"] = "draft"


class RebuildCropRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    bbox: list[float] = Field(min_length=4, max_length=4)


class RebuildVisualAssetCropRequest(RebuildVisualAssetRequest):
    regions: list[RebuildCropRegion] = Field(min_length=1, max_length=8)


class RebuildAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID | None = None

    x_field: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    y_field: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    reaction: ReactionType | None = None
    material: str | None = None
    material_family: str | None = None
    active_site_type: ActiveSiteType | None = None
    data_type: DataType | None = None
    required_condition_keys: list[str] = Field(default_factory=list, max_length=32)
    comparison_condition_keys: list[str] = Field(default_factory=list, max_length=32)
    comparison_fields: list[AnalysisComparisonField] = Field(
        default_factory=lambda: ["material"], max_length=6
    )
    include_estimated: bool = False
    max_samples: int = Field(default=5000, ge=1, le=5000)
