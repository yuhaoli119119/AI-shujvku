from __future__ import annotations

# NOTE: This project uses PostgreSQL (with pgvector extension) as its database.
# All models use PostgreSQL-native types: UUID, JSONB, vector(N).
# Models target PostgreSQL and pgvector exclusively.

import json
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import DATABASE_V1_EMBEDDING_DIMENSION


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
    return JSONB()


EMBEDDING_DIMENSION = DATABASE_V1_EMBEDDING_DIMENSION


class Paper(Base):
    __tablename__ = "papers"
    __table_args__ = (
        sa.UniqueConstraint("library_name", "doi", name="uq_papers_library_doi"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    library_name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
        default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        server_default="\u9ed8\u8ba4\u6587\u732e\u5e93",
        index=True,
    )
    doi: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    journal: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    journal_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("journals.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    paper_code: Mapped[str | None] = mapped_column(sa.String(16), nullable=True, unique=True, index=True)
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


class Journal(Base):
    __tablename__ = "journals"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(sa.String(512), nullable=False, unique=True, index=True)
    print_issn: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    electronic_issn: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    publisher: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="active", server_default="active", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)


class JournalAlias(Base):
    __tablename__ = "journal_aliases"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("journals.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(sa.String(512), nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(sa.String(64), default="manual", server_default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


class JournalMetric(Base):
    __tablename__ = "journal_metrics"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("journals.id", ondelete="CASCADE"), index=True)
    metric_type: Mapped[str] = mapped_column(sa.String(32), default="JIF", server_default="JIF", nullable=False, index=True)
    metric_value: Mapped[float] = mapped_column(sa.Float, nullable=False, index=True)
    data_year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    release_year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    source_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    source_snapshot_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.UniqueConstraint(
            "journal_id",
            "metric_type",
            "data_year",
            "release_year",
            "source_name",
            name="uq_journal_metric_identity",
        ),
    )


class ElementProperty(Base):
    __tablename__ = "element_properties"

    symbol: Mapped[str] = mapped_column(sa.String(3), primary_key=True)
    name: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    atomic_number: Mapped[int] = mapped_column(sa.Integer, nullable=False, unique=True, index=True)
    atomic_mass: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    period: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    group_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, index=True)
    block: Mapped[str | None] = mapped_column(sa.String(1), nullable=True, index=True)
    electronegativity_pauling: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    electronegativity_allen: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    covalent_radius_pyykko_pm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    covalent_radius_cordero_pm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    vdw_radius_pm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    atomic_radius_pm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    valence_electron_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    common_oxidation_states: Mapped[list | None] = mapped_column(json_type(), nullable=True)
    data_source: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    data_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    license: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    source_snapshot_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)


class ElementIonicRadius(Base):
    __tablename__ = "element_ionic_radii"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(sa.ForeignKey("element_properties.symbol", ondelete="CASCADE"), index=True)
    oxidation_state: Mapped[int] = mapped_column(sa.Integer, nullable=False, index=True)
    coordination_number: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    spin_state: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    ionic_radius_pm: Mapped[float] = mapped_column(sa.Float, nullable=False)
    data_source: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    data_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    license: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    source_snapshot_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.UniqueConstraint(
            "symbol",
            "oxidation_state",
            "coordination_number",
            "spin_state",
            "data_source",
            "data_version",
            name="uq_element_ionic_radius_identity",
        ),
    )


class PaperSection(Base):
    __tablename__ = "paper_sections"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    section_title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    section_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    text: Mapped[str] = mapped_column(sa.Text)
    page_start: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    section_level: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    section_number: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    parent_heading: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    heading_path: Mapped[list | None] = mapped_column(json_type(), nullable=True)
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
    write_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1", nullable=False)


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


class ActiveSiteMetal(Base):
    __tablename__ = "active_site_metals"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    catalyst_sample_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("catalyst_samples.id", ondelete="CASCADE"), index=True
    )
    active_site_key: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    site_type: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    site_role: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    element_symbol: Mapped[str] = mapped_column(sa.String(3), nullable=False, index=True)
    element_order: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    order_source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    normalized_pair_key: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    evidence_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    enrichment_status: Mapped[str] = mapped_column(
        sa.String(32),
        default="system_enriched",
        server_default="system_enriched",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.UniqueConstraint(
            "catalyst_sample_id",
            "active_site_key",
            "site_role",
            name="uq_active_site_metal_role",
        ),
    )


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
    reaction_type: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    reaction_type_source: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    reaction_type_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    reaction_profile_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reaction_validation_status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
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
    candidate_identity: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    support_lifecycle_status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    support_writeback_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    support_writeback_dft_result_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("dft_results.id", ondelete="SET NULL"), nullable=True, index=True
    )
    support_lifecycle_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    support_lifecycle_actor: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    support_lifecycle_updated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=False), nullable=True
    )

    __table_args__ = (
        sa.UniqueConstraint("paper_id", "candidate_identity", name="uq_dft_result_candidate_identity"),
    )


class DFTAuditIssue(Base):
    __tablename__ = "dft_audit_issues"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(sa.String(64), default="dft_results", server_default="dft_results", index=True)
    target_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    issue_type: Mapped[str] = mapped_column(sa.String(64), index=True)
    severity: Mapped[str] = mapped_column(sa.String(16), default="medium", server_default="medium", index=True)
    status: Mapped[str] = mapped_column(sa.String(32), default="open", server_default="open", index=True)
    current_snapshot: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    suggested_value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(json_type(), nullable=True)
    suggested_dft: Mapped[dict | None] = mapped_column(json_type(), nullable=True)
    evidence_payload: Mapped[dict | list | None] = mapped_column(json_type(), nullable=True)
    source_identities: Mapped[list] = mapped_column(json_type(), default=list)
    source_candidate_ids: Mapped[list] = mapped_column(json_type(), default=list)
    fingerprint: Mapped[str] = mapped_column(sa.String(128), index=True)
    resolution_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.UniqueConstraint(
            "paper_id",
            "target_type",
            "target_id",
            "issue_type",
            "fingerprint",
            name="uq_dft_audit_issue_identity",
        ),
    )


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
    section_title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
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


class VerificationSessionPaperClaim(Base):
    __tablename__ = "verification_session_paper_claims"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(
        sa.ForeignKey("workflow_jobs.job_id", ondelete="CASCADE"), index=True
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(sa.String(32), default="active", server_default="active", index=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), index=True)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)

    __table_args__ = (
        sa.Index(
            "uq_verification_session_active_paper",
            "paper_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
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


class ModuleWriteLock(Base):
    __tablename__ = "module_write_locks"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    module_name: Mapped[str] = mapped_column(sa.String(64), index=True)
    locked_by: Mapped[str] = mapped_column(sa.String(128), index=True)
    lock_token: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, default=lambda: uuid.uuid4().hex)
    status: Mapped[str] = mapped_column(sa.String(32), default="active", server_default="active", index=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), index=True)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow)
    meta: Mapped[dict | None] = mapped_column("metadata", json_type(), nullable=True)

    __table_args__ = (
        sa.Index(
            "uq_module_write_locks_active_scope",
            "paper_id",
            "module_name",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
    )


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
    source_identity: Mapped[str | None] = mapped_column(
        sa.String(160),
        nullable=True,
        index=True,
        default="untrusted:external_analysis",
        server_default="untrusted:external_analysis",
    )
    source_identity_verified: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, server_default=sa.false(), nullable=False
    )
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
    write_version: Mapped[int] = mapped_column(sa.Integer, default=1, server_default="1", nullable=False)
    __table_args__ = (
        sa.UniqueConstraint("paper_id", "target_type", "target_id", "field_name", name="uq_extraction_field_review"),
    )
    __mapper_args__ = {"version_id_col": write_version}


class ShareToken(Base):
    __tablename__ = "share_tokens"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True, nullable=False)
    scope: Mapped[str] = mapped_column(sa.Text, default="all", server_default="all")
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=False), nullable=True)
    created_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)


# ---------------------------------------------------------------------------
# Literature Intake MVP — 两张检索/候选表，不入 papers，等待用户确认后才入库
# ---------------------------------------------------------------------------

class LiteratureIntakeSession(Base):
    """记录一次用户研究需求驱动的检索会话。

    状态机：searching → pending_review → reviewing → completed / cancelled
    """
    __tablename__ = "literature_intake_sessions"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    library_name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
        default="默认文献库",
        server_default="默认文献库",
        index=True,
    )
    user_need: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    original_query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    providers: Mapped[list] = mapped_column(json_type(), default=list)
    target_types: Mapped[list | None] = mapped_column(json_type(), nullable=True)
    max_results: Mapped[int] = mapped_column(sa.Integer, default=20, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(32),
        default="searching",
        server_default="searching",
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow
    )


class LiteratureIntakeCandidate(Base):
    """记录检索返回的单篇候选文献。

    状态机：
        pending_review → approved → ingesting → ingested / metadata_only / failed
        pending_review → rejected
        pending_review → duplicate（系统发现重复时自动标记）

    注意：candidates 不写入 papers 表，只有 ingesting 阶段才触发真正入库。
    """
    __tablename__ = "literature_intake_candidates"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("literature_intake_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # --- 元数据字段 ---
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(sa.String(512), nullable=True, index=True)
    year: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    journal: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    authors: Mapped[list] = mapped_column(json_type(), default=list)
    abstract: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    identifier: Mapped[str | None] = mapped_column(sa.String(512), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    providers: Mapped[list] = mapped_column(json_type(), default=list)
    # --- AI 筛选字段 ---
    relevance_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    screening_tier: Mapped[str | None] = mapped_column(
        sa.String(16), nullable=True, index=True
    )  # recommended / maybe / weak
    screening_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    risk_flags: Mapped[list] = mapped_column(json_type(), default=list)
    # --- 状态 ---
    status: Mapped[str] = mapped_column(
        sa.String(32),
        default="pending_review",
        server_default="pending_review",
        nullable=False,
        index=True,
    )
    reject_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # --- 关联 ---
    duplicate_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ingest_job_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    ingested_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=False), default=utcnow, onupdate=utcnow
    )
