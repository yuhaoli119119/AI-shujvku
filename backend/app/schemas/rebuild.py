from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class RebuildSectionSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_number: int = Field(ge=1)
    locator: str = Field(min_length=1, max_length=4000)
    quote: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def require_location(self):
        if not self.locator.strip() or not self.quote.strip():
            raise ValueError("section_source_quote_and_locator_required")
        return self


class RebuildSectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_key: str = Field(min_length=1, max_length=128)
    file_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_basis: Literal["source_pdf", "derived_pdf"]
    reading_pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    section_title: str = Field(min_length=1, max_length=4000)
    section_type: str | None = Field(default=None, max_length=64)
    text: str = Field(min_length=1, max_length=1000000)
    explanation_zh: str = Field(min_length=1, max_length=200000)
    page_start: int = Field(ge=1, strict=True)
    page_end: int = Field(ge=1, strict=True)
    section_level: int | None = Field(default=None, ge=1, le=16, strict=True)
    section_number: str | None = Field(default=None, max_length=64)
    parent_heading: str | None = Field(default=None, max_length=4000)
    heading_path: list[str] = Field(default_factory=list, max_length=16)
    sources: list[RebuildSectionSourceInput] = Field(min_length=1, max_length=1000)
    conflicts_with_section_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_section_evidence(self):
        for value in (self.section_key, self.section_title, self.text, self.explanation_zh):
            if not value.strip():
                raise ValueError("section_content_required")
        if self.page_end < self.page_start:
            raise ValueError("section_page_range_invalid")
        if self.page_end - self.page_start + 1 > len(self.sources):
            raise ValueError("section_source_required_for_each_physical_page")
        pages = {source.page_number for source in self.sources}
        if pages != set(range(self.page_start, self.page_end + 1)):
            raise ValueError("section_source_required_for_each_physical_page")
        if self.page_basis == "derived_pdf" and self.reading_pdf_sha256 is None:
            raise ValueError("derived_pdf_sha256_required")
        return self


class RebuildSectionsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    sections: list[RebuildSectionInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_duplicate_keys(self):
        keys = [(section.file_id, section.section_key) for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("section_key_repeated_in_request")
        return self


class RebuildSectionCorrectionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RebuildSectionCorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["extraction_boundary"]
    file_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_section: RebuildSectionCorrectionTarget
    new_sections: list[RebuildSectionCorrectionTarget] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=20000)
    sources: list[RebuildSectionSourceInput] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_explicit_relation(self):
        ids = [target.section_id for target in self.new_sections]
        if not self.reason.strip():
            raise ValueError("section_correction_reason_required")
        if len(ids) != len(set(ids)) or self.old_section.section_id in ids:
            raise ValueError("section_correction_targets_invalid")
        return self


class RebuildSectionCorrectionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    corrections: list[RebuildSectionCorrectionInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_duplicate_old_sections(self):
        ids = [item.old_section.section_id for item in self.corrections]
        if len(ids) != len(set(ids)):
            raise ValueError("section_correction_old_repeated")
        return self
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

    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    task_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")

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

    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    task_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")

    assets: list[Any] = Field(min_length=1, max_length=200)


class RebuildValueCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_value: str | None = None
    numeric_value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=64)
    value_type: ValueType = "explicit"
    precision_digits: int | None = Field(default=None, ge=0, le=12)
    missing_reason: str | None = None
    correction_reason: str = Field(min_length=1, max_length=4000)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID
    notes: str | None = None
    source: RebuildValueSourceInput | None = None


class RebuildVisualAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    task_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")

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
    reading_explanation: dict[str, Any] | None = Field(
        default=None, description="Null means no new reading; preserves any existing structured reading. Empty or conflicting declarations are rejected."
    )
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

class ProcessingStatusReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str = Field(min_length=1, max_length=160)
    agent: str = Field(default='external_ai', min_length=1, max_length=128)
    status: Literal['queued','parsing','parsed','partial','failed']
    message: str = Field(default='', max_length=2000)
    no_extractable_content_reason: str | None = Field(default=None, max_length=2000)
    file_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ProcessingStatusReportEnvelope(BaseModel):
    """Compatibility with clients using the formerly embedded request body."""
    model_config = ConfigDict(extra="forbid")
    payload: ProcessingStatusReport


class ExtractionFileCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reading_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_pages: list[Annotated[int, Field(strict=True, ge=1)]] = Field(max_length=20000)
    remaining_reason: str | None = Field(default=None, max_length=4000)
    no_extractable_content_reason: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def valid_pages(self):
        if any(type(page) is not int or page < 1 for page in self.covered_pages):
            raise ValueError("coverage_pages_must_be_physical_positive_integers")
        if len(set(self.covered_pages)) != len(self.covered_pages):
            raise ValueError("coverage_pages_repeated")
        return self


class ExtractionFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    task_id: str = Field(min_length=1, max_length=160)
    coverage: list[ExtractionFileCoverage] = Field(min_length=1, max_length=100)


class ExtractionReviewEvidence(RebuildSectionSourceInput):
    model_config = ConfigDict(extra="forbid", strict=False)
    file_id: UUID
    page_number: int = Field(ge=1, strict=True)


class ExtractionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    reviewer_task_id: str = Field(min_length=1, max_length=160)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["accepted", "needsfix", "partial", "review_incomplete"]
    message: str = Field(min_length=1, max_length=20000)
    # Explicit independent evidence, never inferred from a native turn/status.
    reviewed_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence_locators: list[ExtractionReviewEvidence] = Field(default_factory=list, max_length=1000)
    unresolved_conflicts: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def nonblank_review(self):
        if not self.message.strip() or not self.reviewer_task_id.strip():
            raise ValueError("review_identity_and_message_required")
        if self.decision == "accepted" and (not self.reviewed_file_ids or not self.evidence_locators):
            raise ValueError("accepted_review_evidence_required")
        return self


class ReviewProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    reviewer_task_id: str = Field(min_length=1, max_length=160)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RebuildQueueControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: bool
    limits: dict[str, int | None] | None = None


class RebuildQueueEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    batch_id: str | None = Field(default=None, max_length=160)
    stage: Literal["writer", "reviewer"]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_id: str | None = Field(default=None, max_length=64)
    model: Literal["cn:deepseek-v4.1-flash"] = "cn:deepseek-v4.1-flash"
    token_allowance: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def explicit_review_freeze(self):
        if self.stage == "reviewer" and not self.freeze_id:
            raise ValueError("queue_review_freeze_required")
        return self


class RebuildQueueClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["writer", "reviewer"]
    worker_id: str = Field(min_length=1, max_length=160)


class RebuildQueueAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_token: UUID
    worker_id: str = Field(min_length=1, max_length=160)
    manual_recheck: bool = False


class RebuildSourceIdentity(BaseModel):
    """Complete replacement; omitted fields cannot silently clear provenance."""
    model_config = ConfigDict(extra="forbid")

    file_id: UUID
    asset_id: UUID | None
    source_kind: Literal["explicit", "estimated", "derived", "table", "text", "figure"]
    page_number: int | None = Field(ge=1, le=2147483647, strict=True)
    label: str | None = Field(max_length=128)
    table_row: int | None = Field(ge=0, le=2147483647, strict=True)
    table_column: int | None = Field(ge=0, le=2147483647, strict=True)
    quote: str | None
    estimate_basis: str | None


class RebuildSourceCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID
    correction_reason: str = Field(min_length=1, max_length=4000)
    source: RebuildSourceIdentity


class RebuildDedupPair(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keep_value_id: UUID
    duplicate_value_id: UUID
    same_fact: Literal[True]
    reason: str = Field(min_length=1, max_length=4000)
    # Explicit scientific identity assertion, including any differing field/condition.
    identity_assertion: str = Field(min_length=1, max_length=4000)


class RebuildDedupPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pairs: list[RebuildDedupPair] = Field(min_length=1, max_length=20)


class RebuildDedupApplyRequest(RebuildDedupPreviewRequest):
    expected_plan_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirm_delete_value_ids: list[UUID]
    confirm_delete_row_ids: list[UUID]
    expected_move_source_ids: list[UUID]
    request_id: UUID
    reason: str = Field(min_length=1, max_length=4000)
