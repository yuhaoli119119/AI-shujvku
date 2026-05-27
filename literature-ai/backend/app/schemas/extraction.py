from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidenceLocatorResponse
from app.schemas.evidence import PageSpan


ReviewStatus = Literal["pending", "verified", "rejected", "corrected", "needs_check", "unknown"]
ReviewResolutionStatus = Literal["active", "remapped", "stale", "ambiguous", "unresolved", "unknown"]


class ExtractionFieldReviewResponse(BaseModel):
    id: UUID
    paper_id: UUID
    target_type: str
    target_id: str
    target_fingerprint: str | None = None
    target_label: str | None = None
    field_path: str | None = None
    target_resolution_status: ReviewResolutionStatus = "active"
    remapped_from_target_id: str | None = None
    last_resolved_target_id: str | None = None
    field_name: str
    original_value: Any = None
    reviewed_value: Any = None
    unit: str | None = None
    evidence_text: str | None = None
    reviewer_status: ReviewStatus
    reviewer: str | None = None
    reviewer_note: str | None = None
    verified: bool = False
    created_at: str
    updated_at: str


class EvidenceField(BaseModel):
    value: Any = None
    unit: str | None = None
    evidence_text: str = ""
    source_section: str | None = None
    page_span: PageSpan = Field(default_factory=PageSpan)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_locator: EvidenceLocatorResponse | None = None
    review: ExtractionFieldReviewResponse | None = None
    verified: bool = False


class CatalystSampleSchema(BaseModel):
    name: EvidenceField = Field(default_factory=EvidenceField)
    catalyst_type: EvidenceField = Field(default_factory=EvidenceField)
    metal_centers: EvidenceField = Field(default_factory=EvidenceField)
    coordination: EvidenceField = Field(default_factory=EvidenceField)
    support: EvidenceField = Field(default_factory=EvidenceField)
    synthesis_method: EvidenceField = Field(default_factory=EvidenceField)


class DFTSettingSchema(BaseModel):
    software: EvidenceField = Field(default_factory=EvidenceField)
    functional: EvidenceField = Field(default_factory=EvidenceField)
    dispersion_correction: EvidenceField = Field(default_factory=EvidenceField)
    pseudopotential: EvidenceField = Field(default_factory=EvidenceField)
    cutoff_energy: EvidenceField = Field(default_factory=EvidenceField)
    k_points: EvidenceField = Field(default_factory=EvidenceField)
    convergence_settings: EvidenceField = Field(default_factory=EvidenceField)
    vacuum_thickness: EvidenceField = Field(default_factory=EvidenceField)


class DFTResultSchema(BaseModel):
    catalyst: EvidenceField = Field(default_factory=EvidenceField)
    adsorbate: EvidenceField = Field(default_factory=EvidenceField)
    energy_type: EvidenceField = Field(default_factory=EvidenceField)
    value: EvidenceField = Field(default_factory=EvidenceField)
    reaction_step: EvidenceField = Field(default_factory=EvidenceField)


class MechanismClaimSchema(BaseModel):
    claim_type: EvidenceField = Field(default_factory=EvidenceField)
    claim_text: EvidenceField = Field(default_factory=EvidenceField)
    key_species: EvidenceField = Field(default_factory=EvidenceField)
    mechanism_direction: EvidenceField = Field(default_factory=EvidenceField)


class ElectrochemicalPerformanceSchema(BaseModel):
    sulfur_loading: EvidenceField = Field(default_factory=EvidenceField)
    sulfur_content: EvidenceField = Field(default_factory=EvidenceField)
    electrolyte_sulfur_ratio: EvidenceField = Field(default_factory=EvidenceField)
    capacity: EvidenceField = Field(default_factory=EvidenceField)
    cycle_number: EvidenceField = Field(default_factory=EvidenceField)
    rate: EvidenceField = Field(default_factory=EvidenceField)
    decay_per_cycle: EvidenceField = Field(default_factory=EvidenceField)


ExtractionSchemaName = Literal[
    "CatalystSample",
    "DFTSetting",
    "DFTResult",
    "MechanismClaim",
    "ElectrochemicalPerformance",
]


class ExtractionJobRequest(BaseModel):
    paper_id: UUID
    schemas: list[ExtractionSchemaName] = Field(
        default_factory=lambda: [
            "CatalystSample",
            "DFTSetting",
            "DFTResult",
            "MechanismClaim",
            "ElectrochemicalPerformance",
        ]
    )
    force: bool = False


class ValidationWarning(BaseModel):
    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    target_type: str
    target_id: str | None = None
    field: str | None = None
    value: Any = None


class ExtractionFieldReviewSaveItem(BaseModel):
    target_type: str
    target_id: str
    field_name: str
    original_value: Any = None
    reviewed_value: Any = None
    unit: str | None = None
    evidence_text: str | None = None
    reviewer_status: ReviewStatus = "corrected"
    reviewer: str | None = None
    reviewer_note: str | None = None


class ExtractionFieldReviewSaveRequest(BaseModel):
    reviews: list[ExtractionFieldReviewSaveItem] = Field(default_factory=list)


class ExtractionReviewMarkVerifiedRequest(BaseModel):
    target_type: str
    target_id: str
    field_names: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    reviewer_note: str | None = None


class ExtractionResultsResponse(BaseModel):
    paper_id: UUID
    schemas: dict[str, Any]
    results: dict[str, list[dict[str, Any]]]
    field_reviews: list[ExtractionFieldReviewResponse] = Field(default_factory=list)
    validation_warnings: list[ValidationWarning] = Field(default_factory=list)
    validation_status: str = "unvalidated"


class ExtractionValidationResponse(BaseModel):
    paper_id: UUID
    status: str
    results: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    field_reviews: list[ExtractionFieldReviewResponse] = Field(default_factory=list)
    validation_warnings: list[ValidationWarning] = Field(default_factory=list)


class ExtractionReviewAuditResponse(BaseModel):
    paper_id: UUID
    total_reviews: int = 0
    active: int = 0
    remapped: int = 0
    stale: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    items: list[ExtractionFieldReviewResponse] = Field(default_factory=list)


class ExtractionReviewPrepareResponse(BaseModel):
    paper_id: UUID
    created_count: int = 0
    existing_count: int = 0
    skipped_count: int = 0
    verified_count: int = 0
    safe_verified_count: int = 0
    review_ids: list[UUID] = Field(default_factory=list)
    items: list[ExtractionFieldReviewResponse] = Field(default_factory=list)
