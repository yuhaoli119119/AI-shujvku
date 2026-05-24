from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidenceRef


class RetrievalSearchRequest(BaseModel):
    query: str
    paper_ids: list[UUID] = Field(default_factory=list)
    mode: str = Field(default="focused", pattern="^(focused|full_context)$")
    limit: int = Field(default=12, ge=1, le=100)
    limit_per_type: int = Field(default=5, ge=1, le=20)
    target_paper_type: str | None = None
    rerank: bool = True


class RetrievalSearchResult(BaseModel):
    score: float
    source: str
    paper_id: UUID
    chunk_id: str | None = None
    section_id: UUID | None = None
    section_title: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    evidence: EvidenceRef
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalSearchResponse(BaseModel):
    query: str
    mode: str
    recall: dict[str, str]
    reranker: dict[str, Any]
    total: int
    items: list[RetrievalSearchResult] = Field(default_factory=list)

