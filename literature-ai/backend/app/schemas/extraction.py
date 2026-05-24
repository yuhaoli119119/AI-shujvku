from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.evidence import PageSpan


class EvidenceField(BaseModel):
    value: Any = None
    unit: str | None = None
    evidence_text: str = ""
    source_section: str | None = None
    page_span: PageSpan = Field(default_factory=PageSpan)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


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


class ExtractionResultsResponse(BaseModel):
    paper_id: UUID
    schemas: dict[str, Any]
    results: dict[str, list[dict[str, Any]]]
    validation_warnings: list[ValidationWarning] = Field(default_factory=list)
    validation_status: str = "unvalidated"


class ExtractionValidationResponse(BaseModel):
    paper_id: UUID
    status: str
    validation_warnings: list[ValidationWarning] = Field(default_factory=list)

