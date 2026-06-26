from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DFTExportMetadataV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    schema_version: Literal["dft_results_ml_v2"]
    created_at: str
    filters: dict[str, Any] = Field(default_factory=dict)
    safety_gate: Literal["safe_verified_with_required_evidence"]
    eligible_count: int
    blocked_count: int
    blocked_reasons: dict[str, int] = Field(default_factory=dict)
    total_candidates: int
    numeric_record_count: int
    numeric_ml_ready_count: int
    numeric_blocked_count: int
    lm_record_count: int
    history_backfill_mode: str
    ml_setting_field: Literal["linked_dft_setting"]


class DFTPaperPayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str | None = None
    doi: str | None = None
    journal: str | None = None
    year: int | None = None
    authors: list[str] | str | None = None


class DFTCatalystPayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalyst_sample_id: str
    name: str | None = None
    catalyst_type: str | None = None
    metal_centers: list[Any] | None = None
    coordination: str | None = None
    support: str | None = None
    synthesis_method: str | None = None
    evidence_strength: str | None = None


class DFTSettingPayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dft_setting_id: str
    software: str | None = None
    functional: str | None = None
    dispersion_correction: str | None = None
    pseudopotential: str | None = None
    cutoff_energy_ev: float | None = None
    k_points: str | None = None
    convergence_settings: dict[str, Any] | None = None
    vacuum_thickness_a: float | None = None
    raw_json: dict[str, Any] | None = None
    match_score: int | None = None
    match_reasons: list[str] | None = None


class DFTTargetPayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_type: str | None = None
    normalized_property_type: str
    canonical_property_type: str
    property_family: str
    property_subtype: str | None = None
    physical_dimension: str
    ml_role: Literal["target", "descriptor"]
    adsorbate: str | None = None
    canonical_adsorbate: str | None = None
    value: float | None = None
    unit: str | None = None
    reaction_step: str | None = None
    normalized_value: float | None = None
    normalized_unit: str | None = None
    normalization_status: str
    normalization_blockers: list[str] = Field(default_factory=list)
    normalization_basis: str | None = None


class DFTLMClaimPayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_type: str | None = None
    normalized_property_type: str
    canonical_property_type: str
    property_family: str
    property_subtype: str | None = None
    physical_dimension: str
    ml_role: Literal["lm_auxiliary"]
    adsorbate: str | None = None
    canonical_adsorbate: str | None = None
    value: float | None = None
    unit: str | None = None
    reaction_step: str | None = None
    normalized_value: float | None = None
    normalized_unit: str | None = None
    normalization_status: str
    normalization_blockers: list[str] = Field(default_factory=list)
    normalization_basis: str | None = None
    evidence_text: str


class DFTSampleContextV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_key: str
    instance_key: str
    instance_anchor_key: str
    material_scope_key: str
    target_context_key: str
    instance_scope_level: str
    instance_components: dict[str, Any] = Field(default_factory=dict)
    history_backfill_applied: bool
    numeric_record_count: int | None = None
    target_record_count: int | None = None
    descriptor_record_count: int | None = None
    material_scope_count: int | None = None
    descriptor_instance_ambiguous: bool | None = None


class DFTProvenancePayloadV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_section: str | None = None
    source_figure: str | None = None
    evidence_text: str | None = None
    confidence: float | None = None
    review_status: str
    review_gate_status: str
    provenance_level: str
    locator_status: str
    gate_reasons: list[str] = Field(default_factory=list)
    safety_gate: Literal["safe_verified_with_required_evidence"]
    evidence_payload: dict[str, Any] | None = None
    catalyst_binding_source: str | None = None


class DFTNumericRecordV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    paper: DFTPaperPayloadV2
    target: DFTTargetPayloadV2
    catalyst: DFTCatalystPayloadV2 | None = None
    catalyst_candidates: list[DFTCatalystPayloadV2] = Field(default_factory=list)
    dft_settings: list[DFTSettingPayloadV2] = Field(default_factory=list)
    paper_level_dft_settings: list[DFTSettingPayloadV2] = Field(default_factory=list)
    linked_dft_setting: DFTSettingPayloadV2 | None = None
    setting_link_status: str
    setting_link_reason: str
    setting_link_candidates: list[DFTSettingPayloadV2] = Field(default_factory=list)
    recommended_ml_setting_field: Literal["linked_dft_setting"]
    provenance: DFTProvenancePayloadV2
    descriptor_fields: dict[str, Any] = Field(default_factory=dict)
    sample_context: DFTSampleContextV2
    ml_blockers: list[str] = Field(default_factory=list)
    ml_readiness_score: int
    is_ml_ready: bool


class DFTLMRecordV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    paper: DFTPaperPayloadV2
    catalyst: DFTCatalystPayloadV2 | None = None
    catalyst_candidates: list[DFTCatalystPayloadV2] = Field(default_factory=list)
    dft_settings: list[DFTSettingPayloadV2] = Field(default_factory=list)
    paper_level_dft_settings: list[DFTSettingPayloadV2] = Field(default_factory=list)
    linked_dft_setting: DFTSettingPayloadV2 | None = None
    setting_link_status: str
    setting_link_reason: str
    setting_link_candidates: list[DFTSettingPayloadV2] = Field(default_factory=list)
    recommended_ml_setting_field: Literal["linked_dft_setting"]
    provenance: DFTProvenancePayloadV2
    sample_context: DFTSampleContextV2
    claim: DFTLMClaimPayloadV2


class DFTMLDatasetExportV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: DFTExportMetadataV2
    records: list[DFTNumericRecordV2] = Field(default_factory=list)
    lm_records: list[DFTLMRecordV2] = Field(default_factory=list)


def select_training_records_v2(payload: dict[str, Any] | DFTMLDatasetExportV2) -> list[DFTNumericRecordV2]:
    """Return only records that satisfy the v2 training contract.

    Downstream callers must not treat `paper_level_dft_settings` as a substitute
    for a result-level linked setting.
    """
    dataset = payload if isinstance(payload, DFTMLDatasetExportV2) else DFTMLDatasetExportV2.model_validate(payload)
    if dataset.metadata.schema_version != "dft_results_ml_v2":
        return []
    ready_records: list[DFTNumericRecordV2] = []
    for record in dataset.records:
        if not record.is_ml_ready:
            continue
        if record.recommended_ml_setting_field != "linked_dft_setting":
            continue
        if record.linked_dft_setting is None:
            continue
        if record.target.normalized_value is None:
            continue
        ready_records.append(record)
    return ready_records


TabularTaskKeyV3 = Literal[
    "SRR_LiS:adsorption_energy",
    "SRR_LiS:reaction_barrier",
    "SRR_LiS:rds_gibbs_free_energy",
]


class DFTDatasetContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["dft_results_ml_v3"]
    dataset_version: str
    source_schema_version: Literal["dft_results_ml_v2"]
    source_dataset_version: str
    task: TabularTaskKeyV3
    task_status: Literal["candidate"]
    task_profile_version: str
    reaction_profile: Literal["SRR_LiS"]
    reaction_profile_version: str
    normalization_version: str
    created_at: str
    filters: dict[str, Any] = Field(default_factory=dict)
    property_type_fields: list[str] = Field(default_factory=list)
    property_type_display_priority: list[str] = Field(default_factory=list)
    source_candidate_count: int
    candidate_count: int
    task_candidate_count: int
    returned_count: int
    label_ready_count: int
    tabular_ready_count: int
    excluded_counts: dict[str, int] = Field(default_factory=dict)


class DFTProvenancePayloadV3(DFTProvenancePayloadV2):
    model_config = ConfigDict(extra="forbid")

    page_locators: list[int] = Field(default_factory=list)


class DFTSplitGroupValuesV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    catalyst_family: str | None = None


class DFTNumericRecordV3(DFTNumericRecordV2):
    model_config = ConfigDict(extra="forbid")

    provenance: DFTProvenancePayloadV3
    reaction_type: Literal["SRR_LiS"]
    reaction_profile_version: str | None = None
    reaction_validation_status: Literal["valid"]
    label_ready: bool
    tabular_ml_ready: bool
    label_blockers: list[str] = Field(default_factory=list)
    feature_blockers: list[str] = Field(default_factory=list)
    task_profile: TabularTaskKeyV3
    task_profile_version: str
    split_group_values: DFTSplitGroupValuesV3


class DFTMLDatasetExportV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: DFTDatasetContractV3
    manifest: DFTDatasetContractV3
    records: list[DFTNumericRecordV3] = Field(default_factory=list)


def select_training_records_v3(
    payload: dict[str, Any] | DFTMLDatasetExportV3,
) -> list[DFTNumericRecordV3]:
    """Return records satisfying the complete task-scoped v3 training contract."""
    dataset = payload if isinstance(payload, DFTMLDatasetExportV3) else DFTMLDatasetExportV3.model_validate(payload)
    manifest = dataset.manifest
    if dataset.metadata != manifest:
        return []
    target_by_task = {
        "SRR_LiS:adsorption_energy": "adsorption_energy",
        "SRR_LiS:reaction_barrier": "reaction_barrier",
        "SRR_LiS:rds_gibbs_free_energy": "gibbs_free_energy_change",
    }
    expected_target = target_by_task[manifest.task]
    ready_records: list[DFTNumericRecordV3] = []
    for record in dataset.records:
        if not record.label_ready or not record.tabular_ml_ready:
            continue
        if record.reaction_type != manifest.reaction_profile:
            continue
        if record.reaction_validation_status != "valid":
            continue
        if record.task_profile != manifest.task:
            continue
        if record.task_profile_version != manifest.task_profile_version:
            continue
        if record.target.canonical_property_type != expected_target:
            continue
        if record.target.normalized_value is None or record.linked_dft_setting is None:
            continue
        ready_records.append(record)
    return ready_records
