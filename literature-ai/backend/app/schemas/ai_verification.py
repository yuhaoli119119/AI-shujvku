from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AIVerificationSubmission(BaseModel):
    """One decision produced by the single authenticated AI caller."""

    model_config = ConfigDict(extra="forbid")

    target_type: str
    target_id: str
    field_name: str
    decision: Literal["accept", "correct", "reject", "exception"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_text: str = ""
    page: int | None = Field(default=None, ge=1)
    proposed_value: Any = None
    reasoning_summary: str = Field(default="", max_length=2000)
    expected_target_fingerprint: str
    expected_write_version: int | None = Field(default=None, ge=1)


class AIVerificationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submissions: list[AIVerificationSubmission] = Field(default_factory=list, max_length=50)
    dry_run: bool = True


class SectionPageFragmentCandidateRef(BaseModel):
    """Opaque reference to one server-recovered page-fragment candidate."""

    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    fragment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SectionPageFragmentMaterializationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    parent_section_id: str
    candidates: list[SectionPageFragmentCandidateRef] = Field(min_length=1, max_length=20)
    dry_run: bool = True
