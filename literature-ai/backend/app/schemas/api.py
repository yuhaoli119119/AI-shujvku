from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# 鈹€鈹€ Request schemas 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


class IngestFromPathRequest(BaseModel):
    pdf_path: str
    title: str | None = None
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    abstract: str | None = None
    library_name: str | None = None


class RAGWriteRequest(BaseModel):
    topic: str
    paper_ids: list[UUID] = Field(default_factory=list)
    user_notes: str | None = None
    sections: list[str] = Field(
        default_factory=lambda: ["outline", "introduction", "dft_results", "discussion", "figure_storyline"]
    )
    limit_per_type: int = Field(default=5, ge=1, le=20)
    target_paper_type: str | None = None


class PaperListFilterParams(BaseModel):
    """Optional filter / pagination params for GET /api/papers."""

    q: str | None = None
    library_name: str | None = None
    source_path: str | None = None
    year: int | None = None
    journal: str | None = None
    has_dft_results: bool | None = None
    has_writing_cards: bool | None = None
    paper_type: str | None = None
    sort_by: str = Field(default="year_serial", pattern="^(year_serial|created_at|title)$")
    sort_order: str = Field(default="desc", pattern="^(asc|desc)$")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class IngestResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    status: str


class DiscoverySearchItemResponse(BaseModel):
    identifier: str
    title: str
    doi: str | None = None
    year: int | None = None
    journal: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    is_open_access: bool | None = None
    databases: list[str] = Field(default_factory=list)


class DiscoverySearchResponse(BaseModel):
    query: str
    providers: list[str] = Field(default_factory=list)
    total: int = 0
    items: list[DiscoverySearchItemResponse] = Field(default_factory=list)


class DiscoveryDownloadRequest(BaseModel):
    identifier: str
    providers: list[str] = Field(default_factory=list)
    library_name: str | None = None


class ExtractionRunResponse(BaseModel):
    paper_id: UUID
    status: str
    dft_settings: int = 0
    catalyst_samples: int = 0
    dft_results: int = 0
    electrochemical_performance: int = 0
    mechanism_claims: int = 0
    writing_cards: int = 0
    comprehensive_analysis: int = 0


class PaperSectionResponse(BaseModel):
    id: UUID
    section_title: str | None = None
    section_type: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None

    model_config = {"from_attributes": True}


class PaperTranslationPreviewRequest(BaseModel):
    include_abstract: bool = True
    section_ids: list[UUID] = Field(default_factory=list)
    max_sections: int = Field(default=3, ge=1, le=8)
    max_chars_per_item: int = Field(default=3500, ge=300, le=8000)


class PaperTranslationItemResponse(BaseModel):
    source_type: str
    section_id: UUID | None = None
    title: str
    page_start: int | None = None
    page_end: int | None = None
    source_text: str
    translated_text: str


class PaperTranslationPreviewResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    target_language: str = "zh-CN"
    backend_used: str = "writer_llm"
    llm_status: str = "preview"
    items: list[PaperTranslationItemResponse] = Field(default_factory=list)


class PaperTableResponse(BaseModel):
    id: UUID
    caption: str | None = None
    markdown_content: str | None = None
    page: int | None = None
    extraction_source: str | None = None

    model_config = {"from_attributes": True}


class PaperFigureResponse(BaseModel):
    id: UUID
    caption: str | None = None
    image_path: str | None = None
    asset_url: str | None = None
    page: int | None = None
    figure_role: str | None = None
    role_confidence: float | None = None
    content_summary: str | None = None
    key_elements: list[str] | None = None
    prov: list[Any] | None = None
    figure_label: str | None = None
    crop_status: str = "candidate_crop"
    crop_confidence: float | None = None
    crop_source: str | None = None
    image_review: dict[str, Any] | None = None
    review_required: bool | None = None
    flags: list[str] = Field(default_factory=list)
    object_review_audit_count: int = 0
    object_review_audits: list[dict[str, Any]] = Field(default_factory=list)
    latest_object_review_audit: dict[str, Any] | None = None
    conflict_count: int = 0
    field_conflicts: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DFTSettingResponse(BaseModel):
    id: UUID
    software: str | None = None
    functional: str | None = None
    dispersion_correction: str | None = None
    pseudopotential: str | None = None
    cutoff_energy_ev: float | None = None
    k_points: str | None = None
    convergence_settings: dict[str, Any] | None = None
    vacuum_thickness_a: float | None = None
    raw_json: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class CatalystSampleResponse(BaseModel):
    id: UUID
    name: str | None = None
    catalyst_type: str | None = None
    metal_centers: list[Any] = Field(default_factory=list)
    coordination: str | None = None
    support: str | None = None
    synthesis_method: str | None = None
    evidence_strength: str | None = None

    model_config = {"from_attributes": True}


class DFTResultResponse(BaseModel):
    id: UUID
    catalyst_sample_id: UUID | None = None
    adsorbate: str | None = None
    property_type: str | None = None
    value: float | None = None
    unit: str | None = None
    reaction_step: str | None = None
    source_section: str | None = None
    source_figure: str | None = None
    evidence_text: str | None = None
    confidence: float | None = None
    candidate_status: str = "system_candidate"
    evidence_payload: dict[str, Any] | None = None
    extraction_protocol_version: str | None = None

    model_config = {"from_attributes": True}


class MechanismClaimResponse(BaseModel):
    id: UUID
    catalyst_sample_id: UUID | None = None
    claim_type: str | None = None
    claim_text: str
    evidence_types: list[Any] = Field(default_factory=list)
    confidence: float | None = None
    evidence_text: str | None = None
    evidence_status: str = "missing"
    locator_status: str = "missing_locator"
    confidence_status: str = "missing"
    object_review_audit_count: int = 0
    object_review_audits: list[dict[str, Any]] = Field(default_factory=list)
    latest_object_review_audit: dict[str, Any] | None = None
    conflict_count: int = 0
    field_conflicts: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ElectrochemicalPerformanceResponse(BaseModel):
    id: UUID
    catalyst_sample_id: UUID | None = None
    sulfur_loading_mg_cm2: float | None = None
    sulfur_content_wt_percent: float | None = None
    electrolyte_sulfur_ratio: str | None = None
    capacity_value: float | None = None
    cycle_number: int | None = None
    rate: str | None = None
    decay_per_cycle: float | None = None
    evidence_text: str | None = None

    model_config = {"from_attributes": True}


class WritingCardResponse(BaseModel):
    id: UUID
    paper_type: str | None = None
    research_gap: str | None = None
    proposed_solution: str | None = None
    core_hypothesis: str | None = None
    evidence_chain: dict[str, Any] | list[Any] | None = None
    section_strategy: dict[str, Any] | None = None
    figure_logic: Any = None
    abstract_logic: str | None = None
    introduction_logic: str | None = None
    discussion_logic: str | None = None
    evidence_chain_status: str = "missing"
    review_gate_status: str = "blocked"
    can_use_for_writing: bool = False
    blocked_reasons: list[str] = Field(default_factory=list)
    evidence_status: str = "missing"
    safety_status: str = "blocked"
    safe_verified: bool = False
    object_review_audit_count: int = 0
    object_review_audits: list[dict[str, Any]] = Field(default_factory=list)
    latest_object_review_audit: dict[str, Any] | None = None
    conflict_count: int = 0
    field_conflicts: list[dict[str, Any]] = Field(default_factory=list)


class FigureDataPointResponse(BaseModel):
    id: UUID
    figure_id: UUID
    metric_name: str
    metric_value: float | None = None
    unit: str | None = None
    conditions: dict[str, Any] | None = None
    sample_label: str | None = None
    confidence: float = 1.0

    model_config = {"from_attributes": True}


class PaperCountsResponse(BaseModel):
    sections: int = 0
    tables: int = 0
    figures: int = 0
    dft_settings: int = 0
    catalyst_samples: int = 0
    dft_results: int = 0
    electrochemical_performance: int = 0
    mechanism_claims: int = 0
    writing_cards: int = 0
    comprehensive_analysis: int = 0
    figure_data_points: int = 0


class PaperArtifactStatusResponse(BaseModel):
    pdf_exists: bool = False
    pdf_file_size: int | None = None
    pdf_path_kind: str = "missing"
    markdown_has_content: bool = False
    docling_json_has_content: bool = False
    grobid_tei_has_content: bool = False
    ai_reading_package_exists: bool = False
    workspace_exists: bool = False
    artifact_ready_for_external_audit: bool = False
    blocking_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PaperListItemResponse(BaseModel):
    id: UUID
    library_name: str | None = None
    doi: str | None = None
    title: str | None = None
    title_zh: str | None = None
    year: int | None = None
    journal: str | None = None
    authors: list = Field(default_factory=list)
    abstract: str | None = None
    abstract_zh: str | None = None
    full_translation_zh: str | None = None
    pdf_path: str
    pdf_size: int | None = None
    oa_status: str | None = None
    license: str | None = None
    tei_path: str | None = None
    docling_json_path: str | None = None
    markdown_path: str | None = None
    comprehensive_analysis: dict[str, Any] | None = None
    paper_type: str | None = None
    type_confidence: float | None = None
    classification_source: str | None = None
    workflow_status: str = "Imported"
    pdf_quality_status: str | None = None
    pdf_quality_score: float | None = None
    pdf_quality_report: dict[str, Any] | None = None
    workspace_path: str | None = None
    created_at: datetime
    counts: PaperCountsResponse = Field(default_factory=PaperCountsResponse)
    serial_number: int | None = None
    relationship_summary: dict[str, int] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class PaperDetailResponse(PaperListItemResponse):
    sections: list[PaperSectionResponse] = Field(default_factory=list)
    tables: list[PaperTableResponse] = Field(default_factory=list)
    figures: list[PaperFigureResponse] = Field(default_factory=list)
    dft_settings_items: list[DFTSettingResponse] = Field(default_factory=list)
    catalyst_samples_items: list[CatalystSampleResponse] = Field(default_factory=list)
    dft_results_items: list[DFTResultResponse] = Field(default_factory=list)
    electrochemical_performance_items: list[ElectrochemicalPerformanceResponse] = Field(default_factory=list)
    mechanism_claims_items: list[MechanismClaimResponse] = Field(default_factory=list)
    writing_cards_items: list[WritingCardResponse] = Field(default_factory=list)
    figure_data_points_items: list[FigureDataPointResponse] = Field(default_factory=list)
    outgoing_relationships: list[PaperRelationshipItemResponse] = Field(default_factory=list)
    incoming_relationships: list[PaperRelationshipItemResponse] = Field(default_factory=list)
    references: list[ReferenceEntryResponse] = Field(default_factory=list)
    artifact_status: PaperArtifactStatusResponse = Field(default_factory=PaperArtifactStatusResponse)


class CodexContextResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    schema_version: str = "codex_context_v1"
    context: dict[str, Any] = Field(default_factory=dict)
    markdown: str = ""
    token_budget_hint: dict[str, Any] = Field(default_factory=dict)


class CodexItemContextResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    item_type: str
    item_id: UUID
    schema_version: str = "codex_item_context_v1"
    context: dict[str, Any] = Field(default_factory=dict)
    markdown: str = ""
    token_budget_hint: dict[str, Any] = Field(default_factory=dict)


class PaperKnowledgeCandidateResponse(BaseModel):
    id: str
    paper_id: str
    category: str
    title: str
    content: str
    source_type: str
    source_id: str | None = None
    evidence_text: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    confidence: float | None = None
    candidate_status: str = "candidate_unverified"
    evidence_state: str = "text_only_candidate"
    recommended_action: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperKnowledgeContextResponse(BaseModel):
    schema_version: str = "paper_knowledge_context_v1"
    paper_id: str
    title: str | None = None
    reliability_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    candidates: list[PaperKnowledgeCandidateResponse] = Field(default_factory=list)
    markdown: str = ""


class DFTResultVerifyRequest(BaseModel):
    confirm_reviewed_against_pdf: bool = False
    reviewer: str | None = "codex_review"
    reviewer_note: str | None = None
    field_names: list[str] = Field(default_factory=list)


class DFTResultVerifyResponse(BaseModel):
    paper_id: UUID
    dft_result_id: UUID
    field_names: list[str] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    export_safety: dict[str, Any] = Field(default_factory=dict)
    audit_log_id: UUID | None = None


class DFTResultRejectRequest(BaseModel):
    confirm_reject_candidate: bool = False
    reviewer: str | None = "codex_review"
    reviewer_note: str | None = None
    field_names: list[str] = Field(default_factory=list)


class DFTResultRejectResponse(BaseModel):
    paper_id: UUID
    dft_result_id: UUID
    field_names: list[str] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    export_safety: dict[str, Any] = Field(default_factory=dict)
    audit_log_id: UUID | None = None


class DFTResultCorrectionProposalRequest(BaseModel):
    confirm_correction_proposal: bool = False
    field_name: str
    proposed_value: Any = None
    reason: str
    reviewer: str | None = "codex_review"
    evidence_payload: dict[str, Any] | list[Any] | None = None


class DFTResultCorrectionProposalResponse(BaseModel):
    correction: dict[str, Any] = Field(default_factory=dict)


class RAGWriteResponse(BaseModel):
    topic: str
    query: str
    backend_used: str = "rule"
    prompt_preview: str = ""
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)
    outline: list[str] = Field(default_factory=list)
    introduction: str = ""
    dft_results: str = ""
    discussion: str = ""
    figure_storyline: list[str] = Field(default_factory=list)
    retrieved: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    citation_guard: dict[str, Any] = Field(default_factory=dict)
    evidence_claims: list[dict[str, Any]] = Field(default_factory=list)
    citation_audit: dict[str, Any] = Field(default_factory=dict)
    guard_actions: dict[str, str] = Field(default_factory=dict)


class WriterStatusResponse(BaseModel):
    backend_used: str = "rule"
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)

class PaperRelationshipItemResponse(BaseModel):
    id: UUID
    source_paper_id: UUID
    target_paper_id: UUID
    relationship_type: str
    note: str | None = None
    confidence: float | None = None
    created_at: datetime
    related_paper_title: str | None = None

    model_config = {"from_attributes": True}


class ReferenceEntryResponse(BaseModel):
    id: UUID
    reference_number: int | None = None
    title: str | None = None
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    citation_context: str | None = None

    model_config = {"from_attributes": True}


class InternalAIParseRequest(BaseModel):
    source_label: str = "Internal AI Parsing"
    auto_apply: bool = False


class InternalAIParseResponse(BaseModel):
    run_id: UUID
    mapping_status: str
    created_notes: int = 0
    created_corrections: int = 0
    created_relationships: int = 0
    skipped_candidates: int = 0
    auto_applied_corrections: int = 0



class LibraryCreateRequest(BaseModel):
    name: str
    root_path: str
    description: str | None = None


class LibraryImportRequest(BaseModel):
    root_path: str


class LibraryInfoResponse(BaseModel):
    name: str
    root_path: str
    description: str = ""
    paper_count: int = 0
    is_active: bool = False
    created_at: str | None = None


class PaperLibraryResponse(BaseModel):
    name: str
    paper_count: int = 0



class AIWorkflowPayload(BaseModel):
    query: str
    model: str | None = None
    library_name: str | None = None
    providers: list[str] | None = None
    max_results: int = 10
    max_downloads: int = 5
    skip_existing: bool = True

class ClassifyBatchPayload(BaseModel):
    library_name: str | None = None
    batch_size: int = 20
    interval: float = 5.0
    overwrite: bool = False


class AISearchPayload(BaseModel):
    query: str
    model: str | None = None
    providers: list[str] | None = None
    max_results: int = 10
    skip_guard: bool = False
    target_types: list[str] | None = None

class AISearchResponse(BaseModel):
    query: str
    prompt_used: str
    providers: list[str]
    papers: list[dict[str, Any]]
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)
    result_annotation_status: str | None = None

class AIWorkflowFailedItemResponse(BaseModel):
    identifier: str
    title: str | None = None
    code: str
    reason: str

class AIWorkflowIngestedPaperResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    status: str
    identifier: str
    doi: str | None = None

class AIWorkflowResponse(BaseModel):
    query: str
    prompt_used: str
    providers: list[str]
    searched_total: int
    attempted_downloads: int
    ingested: list[AIWorkflowIngestedPaperResponse] = Field(default_factory=list)
    failed: list[AIWorkflowFailedItemResponse] = Field(default_factory=list)
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)





class ReferenceEntryCreate(BaseModel):
    title: str | None = None
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    volume: str | None = None
    pages: str | None = None
    reference_number: int | None = None
    citation_context: str | None = None
    linked_paper_id: UUID | None = None



class WriterSettingsResponse(BaseModel):
    writer_backend: str | None = None
    writer_model: str | None = None
    writer_api_base: str | None = None
    writer_api_key: str | None = None
    writer_fallback_backend: str | None = None

class WriterSettingsUpdateRequest(BaseModel):
    writer_backend: str
    writer_model: str
    writer_api_base: str | None = None
    writer_api_key: str | None = None
    writer_fallback_backend: str | None = None

