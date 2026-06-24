from __future__ import annotations

from typing import Literal

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
