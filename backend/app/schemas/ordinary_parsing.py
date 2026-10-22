from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ParseCheckpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=128)
    file_id: UUID
    stage: Literal['read', 'assets', 'rows']
    status: Literal['running', 'completed', 'partial', 'failed']
    reason: str | None = Field(default=None, max_length=1000)
    expected_revision: int = Field(ge=0)
