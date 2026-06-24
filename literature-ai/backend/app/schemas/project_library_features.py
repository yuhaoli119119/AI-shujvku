from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


FeatureExtractionStatus = Literal["ready", "candidate_usable", "needs_fields"]


class ProjectLibraryFeatureValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_key: str
    value: Any = None
    unit: str | None = None
    source: str | None = None
    evidence_text: str | None = None
    normalized: bool = False
    unknown: bool = False


class ProjectLibraryFeatureExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_key: str
    feature_set: Literal["structure", "experimental_performance"]
    status: FeatureExtractionStatus
    read_only: bool = True
    auto_verification_applied: bool = False
    blockers: list[str] = Field(default_factory=list)
    fields: dict[str, ProjectLibraryFeatureValue] = Field(default_factory=dict)

