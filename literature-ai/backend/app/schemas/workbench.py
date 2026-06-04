from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkbenchPrepareRequest(BaseModel):
    render_pages: bool = False


class GeminiAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str = "paper"
    target_id: UUID | None = None
    decision: str = Field(pattern="^(PASS|REVISE|FLAG|INSUFFICIENT)$")
    reviewer: str = "gemini_auditor"
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
