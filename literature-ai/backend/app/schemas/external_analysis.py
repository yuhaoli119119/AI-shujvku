from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ExternalAnalysisImportRequest(BaseModel):
    paper_id: UUID
    source: str = Field(..., description="Source system, e.g. chatgpt, claude_web, manual")
    source_label: str | None = Field(default=None, description="Optional human-readable source label")
    raw_text: str | None = None
    raw_payload: dict[str, Any] | list[Any] | str | None = None


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
    created_at: datetime

    model_config = {"from_attributes": True}


class ExternalAnalysisRunResponse(BaseModel):
    id: UUID
    paper_id: UUID
    source: str
    source_label: str | None = None
    raw_text: str | None = None
    raw_payload: Any = None
    normalized_payload: Any = None
    mapping_status: str
    mapping_error: str | None = None
    created_at: datetime
    candidates: list[ExternalAnalysisCandidateResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}
