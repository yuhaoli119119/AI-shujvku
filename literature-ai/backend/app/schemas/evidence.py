from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class PageSpan(BaseModel):
    page_start: int | None = None
    page_end: int | None = None
    span_start: int | None = None
    span_end: int | None = None


class EvidenceBBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    width: float | None = None
    height: float | None = None
    coordinate_system: str = "pdf_points"


class EvidenceLocatorResponse(BaseModel):
    id: UUID | None = None
    paper_id: UUID
    claim_id: UUID | None = None
    chunk_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    field_name: str | None = None
    evidence_text: str
    page: int | None = None
    bbox: EvidenceBBox | None = None
    section: str | None = None
    source_type: str = "unknown"
    locator_status: str = "missing_locator"
    provenance_level: str = "unavailable"
    can_jump_to_pdf_page: bool = False
    can_highlight_in_pdf: bool = False
    locator_confidence: float = 0.0
    parser_source: str = "unknown"
    figure_id: UUID | None = None
    table_id: UUID | None = None
    equation_id: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    warning_reason: str | None = None


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
    bbox: EvidenceBBox | None = None
    parser_source: str = "unknown"
    locator_status: str | None = None
    locator_confidence: float | None = None
    locator_warning: str | None = None
    locator: EvidenceLocatorResponse | None = None


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
