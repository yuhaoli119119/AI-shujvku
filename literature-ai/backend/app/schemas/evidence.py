from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class PageSpan(BaseModel):
    page_start: int | None = None
    page_end: int | None = None
    span_start: int | None = None
    span_end: int | None = None


class EvidenceRef(BaseModel):
    paper_id: UUID | None = None
    chunk_id: str | None = None
    section_id: UUID | None = None
    page_span: PageSpan = Field(default_factory=PageSpan)
    evidence_text: str
    confidence: float | None = None
    source: str = "unknown"
    section_title: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class ClaimEvidence(BaseModel):
    id: UUID | None = None
    claim_text: str
    source_type: str = "generated"
    target_type: str | None = None
    target_id: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    validation_status: str = "unverified"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceClaimCreate(BaseModel):
    claim_text: str
    source_type: str = "manual"
    target_type: str | None = None
    target_id: str | None = None
    evidence: EvidenceRef
    validation_status: str = "supported"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CitationAuditRequest(BaseModel):
    text: str
    paper_ids: list[UUID] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    min_confidence: float = Field(default=0.25, ge=0.0, le=1.0)


class CitationAuditItem(BaseModel):
    claim_text: str
    status: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    warning: str | None = None


class CitationAuditResponse(BaseModel):
    ok: bool
    total_claims: int
    supported_claims: int
    unsupported_claims: int
    claims: list[CitationAuditItem] = Field(default_factory=list)

