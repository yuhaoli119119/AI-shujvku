from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class ExternalReviewNoteModel(BaseModel):
    content: str
    field_name: str | None = None
    page: int | None = None
    section_title: str | None = None
    quoted_text: str | None = None
    confidence: float | None = Field(default=0.7, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalCorrectionProposalModel(BaseModel):
    field_name: str
    target_path: str
    operation: str = "replace"
    proposed_value: Any = None
    reason: str
    evidence_payload: dict[str, Any] | list[Any] | None = None
    confidence: float | None = Field(default=0.7, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalSupportingPaperModel(BaseModel):
    relationship_type: str = "supports"
    target_paper_id: str | None = None
    target_doi: str | None = None
    target_title: str | None = None
    note: str | None = None
    confidence: float | None = Field(default=0.6, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalAuditOpinionModel(BaseModel):
    paper_id: str | None = None
    source: str | None = None
    verdict: str | None = None
    recommended_action: str | None = None
    suspected_missing: list[Any] = Field(default_factory=list)
    metadata_status: str | None = None
    section_structure_status: str | None = None
    table_status: str | None = None
    figure_status: str | None = None
    dft_status: str | None = None
    evidence_examples: list[Any] = Field(default_factory=list)
    raw_payload: dict[str, Any] | list[Any] | str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = "candidate"
    verification_status: str = "unverified"
    mapping_reason: str | None = None


class ExternalObjectReviewAuditModel(BaseModel):
    paper_id: str | None = None
    target_type: str
    target_id: str
    field_name: str | None = None
    decision: str | None = None
    adjudication_role: str | None = None
    adjudication_scope: str | None = None
    selected_source_ids: list[str] = Field(default_factory=list)
    normalized_energy_type: str | None = None
    normalized_material: str | None = None
    structure_name: str | None = None
    adsorbate: str | None = None
    reaction_step: str | None = None
    evidence_checked: bool | None = None
    evidence_location: dict[str, Any] | list[Any] | str | None = None
    dedupe_signature: str | None = None
    borrowed_from_reference: bool = False
    supporting_evidence: list[Any] = Field(default_factory=list)
    blocking_errors: list[Any] = Field(default_factory=list)
    recommended_action: str | None = None
    corrected_value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None
    source_label: str | None = None
    agent_role: str | None = None
    model_name: str | None = None
    reason: str | None = None
    raw_payload: dict[str, Any] | list[Any] | str | None = None
    status: str = "candidate"
    verification_status: str = "unverified"
    writes_final_truth: bool = False
    human_confirmation_required: bool = True
    mapping_reason: str | None = None


class ExternalAnalysisNormalizedModel(BaseModel):
    review_notes: list[ExternalReviewNoteModel] = Field(default_factory=list)
    correction_proposals: list[ExternalCorrectionProposalModel] = Field(default_factory=list)
    supporting_papers: list[ExternalSupportingPaperModel] = Field(default_factory=list)
    external_audit_opinions: list[ExternalAuditOpinionModel] = Field(default_factory=list)
    object_review_audits: list[ExternalObjectReviewAuditModel] = Field(default_factory=list)
    unmapped_items: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class MaterializationResult:
    created_notes: int = 0
    created_corrections: int = 0
    created_relationships: int = 0
    auto_applied_corrections: int = 0
    idempotent_noops: int = 0
    skipped_candidates: int = 0
    deferred_review_candidates: int = 0
