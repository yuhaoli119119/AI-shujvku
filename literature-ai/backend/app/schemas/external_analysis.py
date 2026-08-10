from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ExternalAnalysisImportRequest(BaseModel):
    paper_id: UUID
    source: str = Field(..., description="Source system, e.g. chatgpt, claude_web, manual")
    source_label: str | None = Field(default=None, description="Optional human-readable source label")
    raw_text: str | None = None
    auto_apply_review_rules: bool | None = Field(
        default=None,
        description=(
            "Compatibility switch for running import review rules. Ordinary non-DFT output remains a candidate "
            "requiring authenticated human review and is never an AI overwrite. For DFT, decision=new_candidate "
            "may materialize an unverified candidate; PASS/REVISE/REJECT still require authoritative verification."
        ),
    )
    reviewer: str | None = Field(
        default=None,
        description="Audit label recorded while import review rules retain or materialize eligible candidates.",
    )
    write_lock_token: str | None = Field(
        default=None,
        description="Compatibility lock token accepted by the import pipeline; it does not enable non-DFT AI overwrite.",
    )
    write_lock_tokens: list[str] = Field(
        default_factory=list,
        description="Additional compatibility lock tokens; controlled edits should use propose_correction or dedicated tools.",
    )
    raw_payload: dict[str, Any] | list[Any] | str | None = Field(
        default=None,
        description=(
            "Structured or free-form external analysis. Object-level reviews may be supplied under "
            "object_review_audits/object_reviews with target_type, target_id, field_name, decision, "
            "evidence_location, and corrected_value."
        ),
    )

    @model_validator(mode="after")
    def require_analysis_content(self) -> "ExternalAnalysisImportRequest":
        has_text = bool(str(self.raw_text or "").strip())
        payload = self.raw_payload
        has_payload = payload is not None
        if isinstance(payload, str):
            has_payload = bool(payload.strip())
        elif isinstance(payload, (dict, list)):
            has_payload = bool(payload)
        if not has_text and not has_payload:
            raise ValueError("import_analysis requires non-empty raw_text or raw_payload")
        return self


class ExternalObjectReviewAuditPayload(BaseModel):
    paper_id: str | None = None
    target_type: str
    target_id: str
    field_name: str | None = None
    decision: str | None = None
    evidence_checked: bool | None = None
    evidence_location: dict[str, Any] | list[Any] | str | None = None
    blocking_errors: list[Any] = Field(default_factory=list)
    recommended_action: str | None = None
    corrected_value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None
    source_label: str | None = None
    agent_role: str | None = None
    model_name: str | None = None
    reason: str | None = None
    writes_final_truth: bool = False
    confirmation_required: bool = True


class ExternalAnalysisMaterializeRequest(BaseModel):
    candidate_ids: list[UUID] | None = Field(
        default=None,
        description="Explicit candidate ids to materialize. Empty lists are rejected; use explicit_all=true for all.",
    )
    explicit_all: bool = Field(
        default=False,
        description="Required when candidate_ids is null/omitted and all candidates should be materialized.",
    )
    created_by: str = "system"


class ExternalAnalysisApplyReviewRulesRequest(BaseModel):
    reviewer: str | None = Field(
        default=None,
        description="Reviewer label recorded while applying compatibility import rules. Defaults to source_label/source/ide_ai.",
    )
    write_lock_token: str | None = Field(
        default=None,
        description="Optional pre-acquired dft_results lock for DFT candidate materialization. When omitted the service auto-acquires one.",
    )
    write_lock_tokens: list[str] = Field(
        default_factory=list,
        description="Additional compatibility lock tokens; they do not authorize non-DFT AI overwrite.",
    )


class PaperRelationshipResponse(BaseModel):
    id: UUID
    source_paper_id: UUID
    target_paper_id: UUID
    relationship_type: str
    note: str | None = None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ExternalAnalysisCandidateResponse(BaseModel):
    id: UUID
    run_id: UUID
    paper_id: UUID
    candidate_type: str
    normalized_payload: Any = None
    confidence: float | None = None
    mapping_reason: str | None = None
    evidence_payload: Any = None
    status: str
    materialized_target_type: str | None = None
    materialized_target_id: str | None = None
    action_mode: Literal["materialize", "apply_review_rules", "readonly"] = "readonly"
    action_scope: Literal["candidate", "run"] = "candidate"
    created_at: datetime

    model_config = {"from_attributes": True}


class ExternalAnalysisRunResponse(BaseModel):
    id: UUID
    paper_id: UUID
    source: str
    source_label: str | None = None
    source_identity: str | None = None
    source_identity_verified: bool = False
    raw_text: str | None = None
    raw_payload: Any = None
    normalized_payload: Any = None
    mapping_status: str
    mapping_error: str | None = None
    created_at: datetime
    candidates: list[ExternalAnalysisCandidateResponse] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    auto_apply_summary: dict[str, Any] | None = None

    model_config = {"from_attributes": True}
