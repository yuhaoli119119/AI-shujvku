"""Explicit checkpoints; these declarations do not authorize scientific writes."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

Stage = Literal['source_scope','coverage','extract','reconcile','handoff']
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Page = Annotated[int, Field(strict=True,ge=1)]

class CheckModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class CheckBinding(CheckModel):
    output_revision: Digest
    source_revision: Digest
    receipt_revision: Digest
    plan_revision: Digest | None
    plan_generation: int | None = Field(ge=1)
    known_unknown_revision: Digest

class CheckScope(CheckModel):
    file_id: UUID
    source_sha256: Digest
    reading_pdf_sha256: Digest
    pages: list[Page] = Field(min_length=1,max_length=10000)
    locator: str = Field(min_length=1,max_length=2000)

class UnknownImport(CheckModel):
    operation: Literal['rows.import','assets.import','assets.upsert','assets.crop']
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_.:-]+$')
    request_sha256: Digest
    reason: str = Field(min_length=1,max_length=2000)

class StageCheckRequest(CheckModel):
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_check_revision: Digest | None
    expected_binding: CheckBinding
    stage: Stage
    source_scope: list[CheckScope] = Field(default_factory=list,max_length=200)
    item_keys: list[Annotated[str, Field(min_length=1,max_length=160,pattern=r'^[A-Za-z0-9_.:-]+$')]] = Field(default_factory=list,max_length=2000)
    receipt_ids: list[UUID] = Field(default_factory=list,max_length=200)
    unknown_requests: list[UnknownImport] = Field(default_factory=list,max_length=200)
    review_statement: str = Field(min_length=1,max_length=3000)
