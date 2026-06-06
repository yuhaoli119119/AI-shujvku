from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class VectorType(sa.types.UserDefinedType):
    cache_ok = True

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def get_col_spec(self, **_: object) -> str:
        return f"vector({self.dimension})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            if dialect.name == "postgresql":
                return "[" + ",".join(f"{float(item):.8f}" for item in value) + "]"
            return json.dumps([float(item) for item in value])

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    return [float(item) for item in stripped.strip("[]").split(",") if item]
                return json.loads(stripped)
            return value

        return process


def json_type():
    return JSONB().with_variant(sa.JSON(), "sqlite")


EMBEDDING_DIMENSION = int(os.getenv("LITAI_EMBEDDING_DIMENSION", "1024"))


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    library_name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
        default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        server_default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        index=True,
    )
    doi: Mapped[str | None] = mapped_column(sa.String(512), unique=True, nullable=True)
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    journal: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    authors: Mapped[list] = mapped_column(json_type(), default=list)
    abstract: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    pdf_path: Mapped[str] = mapped_column(sa.Text)
    source_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    oa_status: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    license: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    tei_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    docling_json_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    serial_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    comprehensive_analysis: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    paper_type: Mapped[str | None] = mapped_column(sa.String(20), nullable=True, index=True)
    type_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True, index=True)
    classification_source: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    workflow_status: Mapped[str] = mapped_column(
        sa.String(64),
        default="Imported",
        server_default="Imported",
        nullable=False,
        index=True,
    )
    pdf_quality_status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    pdf_quality_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    pdf_quality_report: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class PaperCitationEligibility(Base):
    __tablename__ = "paper_citation_eligibility"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    included_for_writing: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    exclude_from_citation: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False, index=True)
    exclude_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    citation_priority: Mapped[str] = mapped_column(sa.String(16), default="medium", nullable=False, index=True)
    user_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)


class PaperImpactMetadata(Base):
    __tablename__ = "paper_impact_metadata"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    impact_factor: Mapped[float | None] = mapped_column(sa.Float, nullable=True, index=True)
    impact_factor_source: Mapped[str] = mapped_column(sa.String(64), default="unknown", nullable=False)
    impact_factor_year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)


class PaperSection(Base):
    __tablename__ = "paper_sections"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    section_title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    section_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    text: Mapped[str] = mapped_column(sa.Text)
    page_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(EMBEDDING_DIMENSION), nullable=True)


class PaperChunk(Base):
    __tablename__ = "paper_chunks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("paper_sections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    page_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    token_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(EMBEDDING_DIMENSION), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(sa.String(128), nullable=True, index=True)
    embedding_dimension: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("paper_id", "section_id", "chunk_index", name="uq_paper_chunk_section_index"),
    )


class PaperTable(Base):
    __tablename__ = "paper_tables"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    caption: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    markdown_content: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    page: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    extraction_source: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    prov: Mapped[list | None] = mapped_column(json_type(), nullable=True)


class PaperFigure(Base):
    __tablename__ = "paper_figures"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    caption: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    page: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    figure_role: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    role_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    content_summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    key_elements: Mapped[list | None] = mapped_column(json_type(), nullable=True)
    prov: Mapped[list | None] = mapped_column(json_type(), nullable=True)
    figure_label: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    crop_status: Mapped[str] = mapped_column(
        sa.String(32),
        default="candidate_crop",
        server_default="candidate_crop",
        nullable=False,
        index=True,
    )
    crop_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    crop_source: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class CatalystSample(Base):
    __tablename__ = "catalyst_samples"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    catalyst_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    metal_centers: Mapped[list] = mapped_column(json_type(), default=list)
    coordination: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    support: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    synthesis_method: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    evidence_strength: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class DFTSetting(Base):
    __tablename__ = "dft_settings"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    software: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    functional: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    dispersion_correction: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    pseudopotential: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    cutoff_energy_ev: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    k_points: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    convergence_settings: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    vacuum_thickness_a: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(json_type(), nullable=True)


class DFTResult(Base):
    __tablename__ = "dft_results"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    catalyst_sample_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("catalyst_samples.id", ondelete="SET NULL"), nullable=True)
    adsorbate: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    property_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reaction_step: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    source_section: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    source_figure: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    candidate_status: Mapped[str] = mapped_column(
        sa.String(64),
        default="system_candidate",
        server_default="system_candidate",
        nullable=False,
        index=True,
    )
    evidence_payload: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    extraction_protocol_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class MechanismClaim(Base):
    __tablename__ = "mechanism_claims"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    catalyst_sample_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("catalyst_samples.id", ondelete="SET NULL"), nullable=True)
    claim_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    claim_text: Mapped[str] = mapped_column(sa.Text)
    evidence_types: Mapped[list] = mapped_column(json_type(), default=list)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class ElectrochemicalPerformance(Base):
    __tablename__ = "electrochemical_performance"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    catalyst_sample_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("catalyst_samples.id", ondelete="SET NULL"), nullable=True)
    sulfur_loading_mg_cm2: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    sulfur_content_wt_percent: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    electrolyte_sulfur_ratio: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    capacity_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    cycle_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    rate: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    decay_per_cycle: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class WritingCard(Base):
    __tablename__ = "writing_cards"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    paper_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    research_gap: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    proposed_solution: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    core_hypothesis: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    evidence_chain: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    section_strategy: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    figure_logic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    abstract_logic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    introduction_logic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    discussion_logic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(EMBEDDING_DIMENSION), nullable=True)


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    object_type: Mapped[str] = mapped_column(sa.String(64))
    object_id: Mapped[str] = mapped_column(sa.String(64))
    text: Mapped[str] = mapped_column(sa.Text)
    page: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    figure: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    table: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)


class EvidenceClaim(Base):
    __tablename__ = "evidence_claims"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    claim_text: Mapped[str] = mapped_column(sa.Text)
    source_type: Mapped[str] = mapped_column(sa.String(64), default="manual", index=True)
    target_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    chunk_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("paper_sections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    page_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    span_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    evidence_text: Mapped[str] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    validation_status: Mapped[str] = mapped_column(sa.String(32), default="unverified", index=True)
    meta: Mapped[dict | None] = mapped_column("metadata", json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class EvidenceLocator(Base):
    __tablename__ = "evidence_locators"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("evidence_claims.id", ondelete="CASCADE"), nullable=True, index=True
    )
    chunk_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(sa.String(32), default="unknown", index=True)
    page: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    bbox: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    section: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    figure_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("paper_figures.id", ondelete="SET NULL"), nullable=True, index=True
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("paper_tables.id", ondelete="SET NULL"), nullable=True, index=True
    )
    equation_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True, index=True)
    target_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    field_name: Mapped[str | None] = mapped_column(sa.String(128), nullable=True, index=True)
    evidence_text: Mapped[str] = mapped_column(sa.Text)
    char_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    locator_status: Mapped[str] = mapped_column(sa.String(32), default="missing", index=True)
    locator_confidence: Mapped[float] = mapped_column(sa.Float, default=0.0)
    parser_source: Mapped[str] = mapped_column(sa.String(32), default="unknown", index=True)
    warning_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)


class PaperNote(Base):
    __tablename__ = "paper_notes"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(sa.String(64))
    content: Mapped[str] = mapped_column(sa.Text)
    field_name: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    page: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    section_title: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    quoted_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class PaperCorrection(Base):
    __tablename__ = "paper_corrections"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(sa.String(64))
    field_name: Mapped[str] = mapped_column(sa.String(128))
    target_path: Mapped[str] = mapped_column(sa.String(255))
    operation: Mapped[str] = mapped_column(sa.String(32), default="replace")
    proposed_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(json_type(), nullable=True)
    reason: Mapped[str] = mapped_column(sa.Text)
    evidence_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=False), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class ParseJob(Base):
    __tablename__ = "parse_jobs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    identifier: Mapped[str] = mapped_column(sa.String(512), index=True)
    providers: Mapped[list] = mapped_column(json_type(), default=list)
    requested_by: Mapped[str] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(32), default="pending")
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow
    )


class WorkflowJob(Base):
    __tablename__ = "workflow_jobs"

    job_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    type: Mapped[str] = mapped_column(sa.String(64), index=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="queued", index=True)
    progress: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    result: Mapped[dict | list | str | None] = mapped_column(json_type(), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    library_name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
        default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        server_default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        index=True,
    )
    payload: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    runtime_context: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(sa.String(64))
    source: Mapped[str] = mapped_column(sa.String(64))
    target_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    payload: Mapped[dict | list | str | None] = mapped_column(json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class PaperRelationship(Base):
    __tablename__ = "paper_relationships"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    source_paper_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    target_paper_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    relationship_type: Mapped[str] = mapped_column(sa.String(64))
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_by: Mapped[str] = mapped_column(sa.String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class ExternalAnalysisRun(Base):
    __tablename__ = "external_analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(sa.String(64))
    source_label: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    raw_payload: Mapped[dict | list | str | None] = mapped_column(json_type(), nullable=True)
    normalized_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    mapping_status: Mapped[str] = mapped_column(sa.String(32), default="pending")
    mapping_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class ExternalAnalysisCandidate(Base):
    __tablename__ = "external_analysis_candidates"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("external_analysis_runs.id", ondelete="CASCADE"), index=True
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    candidate_type: Mapped[str] = mapped_column(sa.String(32))
    normalized_payload: Mapped[dict | list | str | None] = mapped_column(json_type(), nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    mapping_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    evidence_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="pending")
    materialized_target_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    materialized_target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class ReferenceEntry(Base):
    __tablename__ = "reference_entries"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    authors: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    journal: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    doi: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    volume: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pages: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reference_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    citation_context: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    linked_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class FigureDataPoint(Base):
    __tablename__ = "figure_data_points"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    figure_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("paper_figures.id", ondelete="CASCADE"), index=True)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str] = mapped_column(sa.String(255))
    metric_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    conditions: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    sample_label: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0)
    raw_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class ExtractionFieldReview(Base):
    __tablename__ = "extraction_field_reviews"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(sa.String(64), index=True)
    target_id: Mapped[str] = mapped_column(sa.String(64), index=True)
    target_fingerprint: Mapped[str | None] = mapped_column(sa.String(128), nullable=True, index=True)
    target_label: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    field_path: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    target_resolution_status: Mapped[str] = mapped_column(sa.String(32), default="active", index=True)
    remapped_from_target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    last_resolved_target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    field_name: Mapped[str] = mapped_column(sa.String(128), index=True)
    original_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(json_type(), nullable=True)
    reviewed_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(json_type(), nullable=True)
    unit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    reviewer_status: Mapped[str] = mapped_column(sa.String(32), default="pending", index=True)
    reviewer: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    review_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        sa.UniqueConstraint("paper_id", "target_type", "target_id", "field_name", name="uq_extraction_field_review"),
    )
