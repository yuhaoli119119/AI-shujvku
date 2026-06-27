from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectLibraryQueuePaperStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str | None = None
    library_name: str
    imported: bool
    parsed: bool
    has_dft: bool
    pending_review: bool
    export_ready: bool
    training_ready: bool
    needs_fields: bool
    dominant_state: Literal[
        "training_ready",
        "export_ready",
        "needs_fields",
        "pending_review",
        "has_dft",
        "parsed",
        "imported",
    ]
    dft_result_count: int
    task_candidate_count: int
    label_ready_count: int
    tabular_ready_count: int
    matched_tasks: list[str] = Field(default_factory=list)
    blocker_counts: dict[str, int] = Field(default_factory=dict)


class ProjectLibraryQueueCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_count: int
    imported_count: int
    parsed_count: int
    with_dft_count: int
    pending_review_count: int
    export_ready_count: int
    training_ready_count: int
    needs_fields_count: int


class ProjectLibraryQueuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_queue_v1"]
    context_key: str
    context_version: str
    context_display_name_zh: str
    reaction_types: list[str] = Field(default_factory=list)
    tabular_tasks: list[str] = Field(default_factory=list)
    library_name: str | None = None
    read_only: bool
    auto_verification_applied: bool
    counts: ProjectLibraryQueueCounts
    papers: list[ProjectLibraryQueuePaperStatus] = Field(default_factory=list)


class ProjectLibraryTaskQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    reaction_type: str
    task_status: str
    task_candidate_count: int
    label_ready_count: int
    training_ready_count: int
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    blocker_counts: dict[str, int] = Field(default_factory=dict)


class ProjectLibraryNeedsFieldsPaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str | None = None
    library_name: str
    dominant_state: str
    task_candidate_count: int
    label_ready_count: int
    training_ready_count: int
    matched_tasks: list[str] = Field(default_factory=list)
    blocker_counts: dict[str, int] = Field(default_factory=dict)
    feature_candidate_blocker_counts: dict[str, int] = Field(default_factory=dict)


class ProjectLibraryQualityCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_count: int
    parsed_count: int
    with_dft_count: int
    needs_fields_count: int
    srr_lis_task_candidate_count: int
    label_ready_count: int
    training_ready_count: int
    feature_candidate_blocked_paper_count: int
    catalyst_sample_count: int = 0
    active_site_instance_count: int = 0
    ambiguous_records_count: int = 0
    manual_verification_required_count: int = 0


class ProjectLibraryQualityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_quality_v1"]
    context_key: str
    context_version: str
    context_display_name_zh: str
    library_name: str | None = None
    read_only: bool
    auto_verification_applied: bool
    counts: ProjectLibraryQualityCounts
    blocker_counts: dict[str, int] = Field(default_factory=dict)
    feature_candidate_blocker_counts: dict[str, int] = Field(default_factory=dict)
    sample_quality: dict[str, Any] = Field(default_factory=dict)
    tasks: list[ProjectLibraryTaskQualitySummary] = Field(default_factory=list)
    needs_fields_papers: list[ProjectLibraryNeedsFieldsPaper] = Field(default_factory=list)


class ProjectLibraryBaselineSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "insufficient", "skipped"]
    target: str
    n_rows: int
    n_train: int
    n_test: int
    split_key: str
    feature_columns: list[str] = Field(default_factory=list)
    baseline_mae: float | None = None
    ridge_mae: float | None = None
    warnings: list[str] = Field(default_factory=list)
    train_groups: list[str] = Field(default_factory=list)
    test_groups: list[str] = Field(default_factory=list)


class ProjectLibraryMLExportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_ml_export_v1"]
    context_key: str
    context_version: str
    context_display_name_zh: str
    library_name: str | None = None
    task: str
    reaction_type: str
    read_only: bool
    auto_verification_applied: bool
    status: Literal["ready", "not_ready"]
    ready_for_baseline: bool
    blockers: list[str] = Field(default_factory=list)
    csv_filename: str
    candidate_manifest: dict[str, object]
    training_manifest: dict[str, object]
    baseline: ProjectLibraryBaselineSummary


class ProjectLibraryBundlePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["project_library_bundles_v1"]
    context_key: str
    context_version: str
    context_display_name_zh: str
    library_name: str | None = None
    read_only: bool
    auto_verification_applied: bool
    database_write_authority: Literal["user_submit_only"]
    ai_review_policy: dict[str, Any]
    element_descriptor_contract: dict[str, Any]
    counts: dict[str, int] = Field(default_factory=dict)
    ambiguous_records: list[dict[str, Any]] = Field(default_factory=list)
    manual_verification_required: list[dict[str, Any]] = Field(default_factory=list)
    bundles: list[dict[str, Any]] = Field(default_factory=list)


class ProjectLibraryMLExportV4Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_ml_export_v4"]
    read_only: bool
    auto_verification_applied: bool
    status: Literal["ready", "not_ready"]
    manifest: dict[str, Any]
    records: list[dict[str, Any]] = Field(default_factory=list)
    sample_records: list[dict[str, Any]] = Field(default_factory=list)


class ProjectLibraryUserSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_ml_export_v4"]
    context_key: str
    paper_id: str
    record_id: str | None = None
    database_write_authority: Literal["user_submit_only"]
    ai_consensus_auto_adopt_allowed: bool = False
    active_site_instance_key: str | None = None
    active_site_ref: dict[str, Any] | None = None
    catalyst_sample_id: str | None = None
    property_type: str
    adsorbate: str | None = None
    reaction_step: str | None = None
    energy_kind: Literal[
        "thermodynamic_energy",
        "activation_barrier",
        "free_energy_change",
        "electronic_descriptor",
        "structural_descriptor",
        "unknown",
    ] | None = None
    value: float
    unit: str
    source_text: str | None = None
    source_location: dict[str, Any] | None = None
    submitted_by: str | None = None
    user_id: str | None = None
    user_edits: dict[str, Any] = Field(default_factory=dict)
    resolved_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    source_candidate_ids: list[str] = Field(default_factory=list)
    decision_status: str | None = None
    confidence_level: float | None = None
    support_raw: str | None = None
    support_normalized: str | None = None
    support_confidence: str | None = None
    dft_setting_id: str | None = None
    bader_charge_M1: float | None = None
    bader_charge_M2: float | None = None
    charge_transfer_e: float | None = None
    charge_transfer_direction: str | None = None
    state_context: str | None = None
    site_label: str | None = None
    metal_metal_distance_A: float | None = None
    coordination_environment: str | None = None
    adsorption_site: str | None = None
    adsorption_mode: str | None = None
    metal_ligand_distance_A: float | None = None


class ProjectLibraryUserSubmitPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_v4_user_submit_preview_v1"]
    context_key: str
    paper_id: str
    record_id: str | None = None
    action: Literal["update_existing_dft_result", "create_new_dft_result"]
    can_submit: bool
    writes_to_database: bool
    database_write_authority: Literal["user_submit_only"]
    visible_in_v4_export: bool
    ready_only_export_eligible: bool
    hard_blockers: list[str] = Field(default_factory=list)
    ml_blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    resolved_source_candidate_ids: list[str] = Field(default_factory=list)
    persisted_field_targets: list[str] = Field(default_factory=list)
    evidence_payload_fields: list[str] = Field(default_factory=list)
    normalized_submission: dict[str, Any] = Field(default_factory=dict)


class ProjectLibraryUserSubmitResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["project_library_v4_user_submit_result_v1"]
    context_key: str
    paper_id: str
    record_id: str
    action: Literal["update_existing_dft_result", "create_new_dft_result"]
    writes_to_database: bool
    database_write_authority: Literal["user_submit_only"]
    visible_in_v4_export: bool
    ready_only_export_eligible: bool
    candidate_status: str
    audit_log_id: str
    consumed_source_candidate_ids: list[str] = Field(default_factory=list)
    persisted_field_targets: list[str] = Field(default_factory=list)
    evidence_payload_fields: list[str] = Field(default_factory=list)
    export_record: dict[str, Any] | None = None
