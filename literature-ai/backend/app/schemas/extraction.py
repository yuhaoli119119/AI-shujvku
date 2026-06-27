from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidenceLocatorResponse
from app.schemas.evidence import PageSpan


ReviewStatus = Literal[
    "pending",
    "verified",
    "rejected",
    "corrected",
    "needs_check",
    "gemini_pass",
    "gemini_revise",
    "gemini_flagged",
    "glm_pass",
    "glm_revise",
    "glm_flagged",
    "ai_pass",
    "ai_revise",
    "ai_flagged",
    "evidence_insufficient",
    "review_conflict",
    "blocked_by_schema",
    "unknown",
]
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
    review_payload: Any = None
    verified: bool = False
    created_at: str
    updated_at: str
    write_version: int = 1


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


class ProjectLibraryV4ExtractionSchema(BaseModel):
    catalyst_samples: list[dict[str, Any]] = Field(
        default_factory=list,
        description="CatalystSample identity records. Preserve paper-local catalyst identity, raw/normalized support, SAC/DAC scope, and M1/M2 source order.",
    )
    active_site_instances: list[dict[str, Any]] = Field(
        default_factory=list,
        description="ActiveSiteInstance records keyed by catalyst_sample_id plus active_site_context, structure_context, and dft_setting_id/ref.",
    )
    adsorbate_properties: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Adsorption properties bound to catalyst_sample_id and active_site_ref/key with adsorbate, value, unit, source_text, and source_location.",
    )
    reaction_step_properties: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Li2S reaction, barrier, migration, and RDS properties with reaction_type, reaction_step, reaction_species, energy_kind, value, and provenance.",
    )
    electronic_properties: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Bader charge, charge transfer, d-band, work function, and related electronic properties; do not swap M1/M2 by canonical metal-pair sorting.",
    )
    structure_properties: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structure fields such as metal_metal_distance_A, coordination_environment, adsorption_site, adsorption_mode, and metal_ligand_distance_A.",
    )
    ambiguous_records: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Facts whose catalyst, active-site instance, property type, unit, value, or evidence binding is not safe enough for ML-ready export.",
    )


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
    "ProjectLibraryV4Extraction",
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
    expected_write_version: int | None = None
    original_value: Any = None
    reviewed_value: Any = None
    unit: str | None = None
    evidence_text: str | None = None
    reviewer_status: ReviewStatus = "corrected"
    reviewer: str | None = None
    reviewer_note: str | None = None
    review_payload: Any = None


class ExtractionFieldReviewSaveRequest(BaseModel):
    reviews: list[ExtractionFieldReviewSaveItem] = Field(default_factory=list)


class ExtractionReviewMarkVerifiedRequest(BaseModel):
    target_type: str
    target_id: str
    field_names: list[str] = Field(default_factory=list)
    expected_write_versions: dict[str, int] = Field(default_factory=dict)
    expected_write_version: int | None = None
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
