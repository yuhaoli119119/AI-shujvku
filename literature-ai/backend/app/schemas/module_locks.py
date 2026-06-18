from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ModuleWriteLockAcquireRequest(BaseModel):
    paper_id: UUID
    module_name: str = Field(..., description="Module scope, e.g. sections, writing_cards, figures, content, all_non_dft")
    locked_by: str
    ttl_minutes: int | None = Field(default=None, ge=1, le=240)
    metadata: dict[str, Any] | None = None


class ModuleWriteLockReleaseRequest(BaseModel):
    lock_token: str
    released_by: str | None = None


class ModuleWriteLockValidateRequest(BaseModel):
    paper_id: UUID
    module_names: list[str]
    lock_tokens: list[str] = Field(default_factory=list)
    locked_by: str | None = None


class ModuleWriteLockResponse(BaseModel):
    id: UUID
    paper_id: UUID
    module_name: str
    locked_by: str
    lock_token: str
    status: str
    expires_at: datetime
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    meta: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class ModuleWriteLockValidateResponse(BaseModel):
    valid: bool
    required_modules: list[str]
    covered_modules: list[str]
    missing_modules: list[str]
    lock_ids: list[str]
