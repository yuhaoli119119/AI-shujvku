from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkbenchPrepareRequest(BaseModel):
    render_pages: bool = False


class ReviewCenterBatchStage2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[UUID] = Field(default_factory=list)
    mode: Literal[
        "prepare_filtered",
        "prepare_suspected_missing",
        "reparse_filtered",
        "deep_parse_suspected_missing",
    ] = "prepare_filtered"
    reviewer: str = "review_center_batch"


class ConflictAdjudicationActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID
    target_type: str
    target_id: str
    field_name: str
    reviewer: str = "review_center"


class ConflictAutoAdvanceBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[UUID] = Field(default_factory=list)
    reviewer: str = "ai_auto_advance"
    limit: int = Field(default=200, ge=1, le=1000)


class VerificationSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_ids: list[UUID] = Field(default_factory=list)
    paper_refs: list[str] = Field(default_factory=list)
    scope: Literal["all", "dft_only", "writing_only"] = "all"
    refresh_materials: bool = True
    reviewer: str = "review_center"


class VerificationSessionSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = "review_center"


class VerificationConflictDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: UUID
    target_type: str
    target_id: str
    field_name: str
    resolution: Literal["adopt_opinion", "reject_all"]
    reviewer: str = "review_center"
    opinion_source_id: str | None = None


class GeminiAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str = "paper"
    target_id: UUID | None = None
    decision: str = Field(pattern="^(PASS|REVISE|FLAG|INSUFFICIENT)$")
    reviewer: str = "gemini_auditor"
    agent_role: str | None = None
    model_name: str | None = None
    protocol_key: str = "gemini_audit_protocol"
    reviewer_note: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    field_names: list[str] = Field(default_factory=list)
    field_name: str | None = None
    proposed_value: Any = None
    evidence_payload: dict[str, Any] | list[Any] | None = None


class HumanConfirmRequest(BaseModel):
    confirm_human_review: bool = False
    reviewer: str = "human"
    note: str | None = None
    target_status: str = Field(default="Human_Confirmed", pattern="^(Human_Confirmed|ML_Ready|Citation_Ready)$")


class WorkbenchResponse(BaseModel):
    schema_version: str
    paper_id: UUID | str
    title: str | None = None
    workflow_status: str | None = None
    pdf_quality_status: str | None = None
    pdf_quality_score: float | None = None
    workspace_path: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
