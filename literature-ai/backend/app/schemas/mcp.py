from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MCPNoteResponse(BaseModel):
    id: UUID
    paper_id: UUID
    source: str
    content: str
    field_name: str | None = None
    page: int | None = None
    section_title: str | None = None
    quoted_text: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MCPCorrectionResponse(BaseModel):
    id: UUID
    paper_id: UUID
    source: str
    field_name: str
    target_path: str
    operation: str
    proposed_value: Any = None
    reason: str
    evidence_payload: Any = None
    status: str
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MCPCorrectionDetailResponse(MCPCorrectionResponse):
    current_value: Any = None
    target_exists: bool = True


class MCPParseJobResponse(BaseModel):
    id: UUID
    identifier: str
    providers: list[str] = Field(default_factory=list)
    requested_by: str
    status: str
    paper_id: UUID | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MCPCorrectionDecisionRequest(BaseModel):
    reason: str | None = None
