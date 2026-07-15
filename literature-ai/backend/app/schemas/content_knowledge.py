from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ContentReviewDecision = Literal["approve_citable", "writing_only", "needs_human", "reject"]


class ContentKnowledgeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ContentReviewDecision
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=4000)
    expected_updated_at: datetime

    @field_validator("reviewer")
    @classmethod
    def normalize_reviewer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reviewer must not be blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def require_reason_for_nonapproval(self) -> "ContentKnowledgeReviewRequest":
        if self.decision in {"reject", "needs_human"} and not self.reason:
            raise ValueError(f"reason is required for decision={self.decision}")
        return self
