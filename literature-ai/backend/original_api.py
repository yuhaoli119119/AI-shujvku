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


class RAGWriteRequest(BaseModel):
    topic: str
    paper_ids: list[UUID] = Field(default_factory=list)
    user_notes: str | None = None
    sections: list[str] = Field(
        default_factory=lambda: ["outline", "introduction", "dft_results", "discussion", "figure_storyline"]
    )
    limit_per_type: int = Field(default=5, ge=1, le=20)


class PaperListFilterParams(BaseModel):
    """Optional filter / pagination params for GET /api/papers."""

    q: str | None = None
    year: int | None = None
    journal: str | None = None
    has_dft_results: bool | None = None
    has_writing_cards: bool | None = None
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
    page: int | None = None
    figure_role: str | None = None

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

    model_config = {"from_attributes": True}


class MechanismClaimResponse(BaseModel):
    id: UUID
    catalyst_sample_id: UUID | None = None
    claim_type: str | None = None
    claim_text: str
    evidence_types: list[Any] = Field(default_factory=list)
    confidence: float | None = None
    evidence_text: str | None = None

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


class PaperListItemResponse(BaseModel):
    id: UUID
    doi: str | None = None
    title: str | None = None
    year: int | None = None
    journal: str | None = None
    authors: list = Field(default_factory=list)
    abstract: str | None = None
    pdf_path: str
    oa_status: str | None = None
    license: str | None = None
    tei_path: str | None = None
    docling_json_path: str | None = None
    markdown_path: str | None = None
    comprehensive_analysis: dict[str, Any] | None = None
    created_at: datetime
    counts: PaperCountsResponse = Field(default_factory=PaperCountsResponse)

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
    guard_actions: dict[str, str] = Field(default_factory=dict)


class WriterStatusResponse(BaseModel):
    backend_used: str = "rule"
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)
