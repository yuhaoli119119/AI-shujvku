from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE: This project uses PostgreSQL (with pgvector extension) as its
# primary database. The default connection string is:
#   postgresql+psycopg://literature_ai:literature_ai@postgres:5432/literature_ai
#
# IMPORTANT for AI developers:
# - PostgreSQL supports concurrent read/write.
# - pgvector provides HNSW vector indexing for semantic search.
# - JSONB columns are used for structured data (not plain JSON).
# - UUID columns are native PostgreSQL UUID type (not CHAR(32)).
# - session_scope() commits on success, rolls back on exception.
# - Avoid holding sessions open during long external calls (e.g. VLM inference);
#   read what you need, close the session, then do the slow work.
# ──────────────────────────────────────────────────────────────────────────────

_engines: dict[str, object] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}
_initialized_urls: set[str] = set()  # Track which DB URLs have already been initialized
logger = logging.getLogger(__name__)


def get_engine(database_url: str):
    if not database_url.strip().lower().startswith("postgresql"):
        raise RuntimeError("Only PostgreSQL is supported. Configure LITAI_DATABASE_URL.")
    if database_url not in _engines:
        engine_kwargs: dict[str, object] = {
            "future": True,
            "pool_size": 20,
            "max_overflow": 50,
        }
        if database_url.startswith("postgresql+psycopg"):
            # Disable psycopg3 auto-prepared statements. They can become unstable
            # across pooled/background worker connections and have been observed to
            # leave ingest jobs stuck with DuplicatePreparedStatement errors.
            engine_kwargs["connect_args"] = {"prepare_threshold": None}
        engine = create_engine(database_url, **engine_kwargs)
        _engines[database_url] = engine
        _session_factories[database_url] = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _engines[database_url]


def init_db(database_url: str, *, force: bool = False) -> None:
    """Initialize the database schema and run migrations.

    Args:
        database_url: SQLAlchemy connection string.
        force: If True, re-run migrations even if this URL was already initialized.
               Use after schema changes that need to be applied immediately.
    """
    if not database_url.strip().lower().startswith("postgresql"):
        raise RuntimeError("Only PostgreSQL is supported. Configure LITAI_DATABASE_URL.")
    # Skip redundant initialization — migrations are idempotent but expensive
    # (each init_db call does ~50 inspector.get_columns() queries on PostgreSQL).
    if not force and database_url in _initialized_urls:
        logger.debug("init_db: %s already initialized, skipping migrations", _mask_url_internal(database_url))
        return

    engine = get_engine(database_url)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        for extension in ("vector", "pgcrypto", "pg_trgm"):
            try:
                connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
            except Exception:
                logger.warning(
                    "Could not create PostgreSQL extension %s; assuming it is preinstalled or managed externally",
                    extension,
                )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE papers DROP CONSTRAINT IF EXISTS papers_doi_key"))
        connection.execute(text("DROP INDEX IF EXISTS ix_papers_doi"))
        connection.execute(text("ALTER TABLE paper_notes ALTER COLUMN section_title TYPE TEXT"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_library_doi "
                "ON papers (library_name, doi) WHERE doi IS NOT NULL"
            )
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_chunks_paper_id ON paper_chunks(paper_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_chunks_section_id ON paper_chunks(section_id)"))
        try:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS paper_chunks_embedding_hnsw "
                    "ON paper_chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
        except Exception:
            logger.warning("Could not create paper_chunks HNSW index; pgvector may be unavailable")
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS paper_chunks_text_gin "
                "ON paper_chunks USING gin (to_tsvector('simple', coalesce(text, '')))"
            )
        )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    def has_column(table_name: str, column_name: str) -> bool:
        if table_name not in table_names:
            return False
        return any(column["name"] == column_name for column in inspector.get_columns(table_name))

    def execute_migration_step(table_name: str, column_name: str, statement: str) -> bool:
        if has_column(table_name, column_name):
            return False
        try:
            connection.execute(text(statement))
            return True
        except Exception:
            logger.exception(
                "Automatic database migration failed for %s.%s using %s",
                table_name,
                column_name,
                engine.dialect.name,
            )
            return False

    if "dft_results" in table_names:
        with engine.begin() as connection:
            execute_migration_step(
                "dft_results",
                "reaction_type",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type VARCHAR(32)",
            )
            execute_migration_step(
                "dft_results",
                "reaction_type_source",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type_source VARCHAR(32)",
            )
            execute_migration_step(
                "dft_results",
                "reaction_type_confidence",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_type_confidence FLOAT",
            )
            execute_migration_step(
                "dft_results",
                "reaction_profile_version",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_profile_version VARCHAR(64)",
            )
            execute_migration_step(
                "dft_results",
                "reaction_validation_status",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_validation_status VARCHAR(32)",
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_dft_results_reaction_type "
                    "ON dft_results (reaction_type)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS dft_audit_issues ("
                    "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
                    "paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE, "
                    "target_type VARCHAR(64) NOT NULL DEFAULT 'dft_results', "
                    "target_id VARCHAR(64), "
                    "issue_type VARCHAR(64) NOT NULL, "
                    "severity VARCHAR(16) NOT NULL DEFAULT 'medium', "
                    "status VARCHAR(32) NOT NULL DEFAULT 'open', "
                    "current_snapshot JSONB, "
                    "suggested_value JSONB, "
                    "suggested_dft JSONB, "
                    "evidence_payload JSONB, "
                    "source_identities JSONB NOT NULL DEFAULT '[]'::jsonb, "
                    "source_candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                    "fingerprint VARCHAR(128) NOT NULL, "
                    "resolution_note TEXT, "
                    "resolved_by VARCHAR(128), "
                    "resolved_at TIMESTAMP, "
                    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "CONSTRAINT uq_dft_audit_issue_identity UNIQUE "
                    "(paper_id, target_type, target_id, issue_type, fingerprint)"
                    ")"
                )
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_paper_id ON dft_audit_issues (paper_id)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_status ON dft_audit_issues (status)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_issue_type ON dft_audit_issues (issue_type)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_target_id ON dft_audit_issues (target_id)"))

    should_backfill_paper_codes = False

    if "papers" in table_names:
        with engine.begin() as connection:
            execute_migration_step(
                "papers",
                "comprehensive_analysis",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS comprehensive_analysis JSONB",
            )
            execute_migration_step(
                "papers",
                "library_name",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS library_name VARCHAR(255) NOT NULL DEFAULT '\u9ed8\u8ba4\u6587\u732e\u5e93'",
            )
            serial_added = execute_migration_step(
                "papers",
                "serial_number",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS serial_number INTEGER",
            )
            try:
                if serial_added or has_column("papers", "serial_number"):
                    # Backfill serial_number for existing papers ordered by created_at per library.
                    connection.execute(text("""
                        UPDATE papers SET serial_number = sub.rn FROM (
                          SELECT id, ROW_NUMBER() OVER (PARTITION BY library_name ORDER BY created_at) AS rn
                          FROM papers WHERE serial_number IS NULL
                        ) sub WHERE papers.id = sub.id
                    """))
            except Exception:
                logger.exception("Automatic database migration failed while backfilling papers.serial_number")
            execute_migration_step(
                "papers",
                "paper_type",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS paper_type VARCHAR(20)",
            )
            execute_migration_step(
                "papers",
                "paper_code",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS paper_code VARCHAR(16)",
            )
            try:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_papers_paper_code "
                        "ON papers (paper_code) WHERE paper_code IS NOT NULL AND paper_code <> ''"
                    )
                )
            except Exception:
                logger.exception("Automatic database migration failed while indexing papers.paper_code")
            execute_migration_step(
                "papers",
                "type_confidence",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS type_confidence DOUBLE PRECISION",
            )
            execute_migration_step(
                "papers",
                "classification_source",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS classification_source VARCHAR(20)",
            )
            execute_migration_step(
                "papers",
                "workflow_status",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS workflow_status VARCHAR(64) NOT NULL DEFAULT 'Imported'",
            )
            execute_migration_step(
                "papers",
                "pdf_quality_status",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_status VARCHAR(32)",
            )
            execute_migration_step(
                "papers",
                "pdf_quality_score",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_score DOUBLE PRECISION",
            )
            execute_migration_step(
                "papers",
                "pdf_quality_report",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_report JSONB",
            )
            execute_migration_step(
                "papers",
                "workspace_path",
                "ALTER TABLE papers ADD COLUMN IF NOT EXISTS workspace_path TEXT",
            )
            should_backfill_paper_codes = True
            execute_migration_step(
                "paper_tables",
                "prov",
                "ALTER TABLE paper_tables ADD COLUMN IF NOT EXISTS prov JSONB",
            )
            execute_migration_step(
                "paper_sections",
                "section_level",
                "ALTER TABLE paper_sections ADD COLUMN IF NOT EXISTS section_level INTEGER",
            )
            execute_migration_step(
                "paper_sections",
                "section_number",
                "ALTER TABLE paper_sections ADD COLUMN IF NOT EXISTS section_number VARCHAR(64)",
            )
            execute_migration_step(
                "paper_sections",
                "parent_heading",
                "ALTER TABLE paper_sections ADD COLUMN IF NOT EXISTS parent_heading TEXT",
            )
            execute_migration_step(
                "paper_sections",
                "heading_path",
                "ALTER TABLE paper_sections ADD COLUMN IF NOT EXISTS heading_path JSONB",
            )
            execute_migration_step(
                "paper_figures",
                "role_confidence",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS role_confidence FLOAT",
            )
            execute_migration_step(
                "paper_figures",
                "content_summary",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS content_summary TEXT",
            )
            execute_migration_step(
                "paper_figures",
                "key_elements",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS key_elements JSONB",
            )
            execute_migration_step(
                "paper_figures",
                "prov",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS prov JSONB",
            )
            execute_migration_step(
                "paper_figures",
                "figure_label",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS figure_label VARCHAR(64)",
            )
            execute_migration_step(
                "paper_figures",
                "crop_status",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_status VARCHAR(32) NOT NULL DEFAULT 'candidate_crop'",
            )
            execute_migration_step(
                "paper_figures",
                "crop_confidence",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_confidence DOUBLE PRECISION",
            )
            execute_migration_step(
                "paper_figures",
                "crop_source",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_source VARCHAR(64)",
            )
            execute_migration_step(
                "dft_results",
                "reaction_step",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_step VARCHAR(255)",
            )
            execute_migration_step(
                "dft_results",
                "candidate_status",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS candidate_status VARCHAR(64) NOT NULL DEFAULT 'system_candidate'",
            )
            try:
                connection.execute(
                    text(
                        "UPDATE dft_results SET candidate_status = 'system_candidate' "
                        "WHERE candidate_status IS NULL "
                        "OR candidate_status = '' "
                        "OR candidate_status = 'Codex_Candidate' "
                        "OR ("
                        "candidate_status NOT IN ('system_candidate', 'Rejected', 'human_reviewed_needs_evidence') "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM extraction_field_reviews r "
                        "WHERE r.target_type = 'dft_results' "
                        "AND r.target_id = CAST(dft_results.id AS TEXT) "
                        "AND r.reviewer_status IN ('verified', 'safe_verified')"
                        ")"
                        ")"
                    )
                )
            except Exception:
                logger.exception("Automatic database migration failed while downgrading DFT candidates")
            execute_migration_step(
                "dft_results",
                "evidence_payload",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS evidence_payload JSONB",
            )
            execute_migration_step(
                "dft_results",
                "extraction_protocol_version",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS extraction_protocol_version VARCHAR(64)",
            )
            execute_migration_step(
                "paper_figures",
                "write_version",
                "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS write_version INTEGER NOT NULL DEFAULT 1",
            )
            execute_migration_step(
                "dft_results",
                "candidate_identity",
                "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS candidate_identity VARCHAR(64)",
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dft_result_candidate_identity "
                    "ON dft_results (paper_id, candidate_identity)"
                )
            )
            execute_migration_step(
                "evidence_locators",
                "claim_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS claim_id UUID",
            )
            execute_migration_step(
                "evidence_locators",
                "chunk_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS chunk_id VARCHAR(64)",
            )
            execute_migration_step(
                "evidence_locators",
                "source_type",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'unknown'",
            )
            execute_migration_step(
                "evidence_locators",
                "page",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS page INTEGER",
            )
            execute_migration_step(
                "evidence_locators",
                "bbox",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS bbox JSONB",
            )
            execute_migration_step(
                "evidence_locators",
                "section",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS section VARCHAR(255)",
            )
            execute_migration_step(
                "evidence_locators",
                "figure_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS figure_id UUID",
            )
            execute_migration_step(
                "evidence_locators",
                "table_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS table_id UUID",
            )
            execute_migration_step(
                "evidence_locators",
                "equation_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS equation_id VARCHAR(128)",
            )
            execute_migration_step(
                "evidence_locators",
                "target_type",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS target_type VARCHAR(64)",
            )
            execute_migration_step(
                "evidence_locators",
                "target_id",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS target_id VARCHAR(64)",
            )
            execute_migration_step(
                "evidence_locators",
                "field_name",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS field_name VARCHAR(128)",
            )
            execute_migration_step(
                "evidence_locators",
                "char_start",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS char_start INTEGER",
            )
            execute_migration_step(
                "evidence_locators",
                "char_end",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS char_end INTEGER",
            )
            execute_migration_step(
                "evidence_locators",
                "locator_status",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS locator_status VARCHAR(32) NOT NULL DEFAULT 'missing'",
            )
            execute_migration_step(
                "evidence_locators",
                "locator_confidence",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS locator_confidence DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            execute_migration_step(
                "evidence_locators",
                "parser_source",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS parser_source VARCHAR(32) NOT NULL DEFAULT 'unknown'",
            )
            execute_migration_step(
                "evidence_locators",
                "warning_reason",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS warning_reason TEXT",
            )
            execute_migration_step(
                "evidence_locators",
                "updated_at",
                "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_fingerprint",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_fingerprint VARCHAR(128)",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_label",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_label VARCHAR(255)",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "field_path",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS field_path VARCHAR(255)",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_resolution_status",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_resolution_status VARCHAR(32) NOT NULL DEFAULT 'active'",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "remapped_from_target_id",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS remapped_from_target_id VARCHAR(64)",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "last_resolved_target_id",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS last_resolved_target_id VARCHAR(64)",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "review_payload",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS review_payload JSONB",
            )
            execute_migration_step(
                "extraction_field_reviews",
                "write_version",
                "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS write_version INTEGER NOT NULL DEFAULT 1",
            )

    if should_backfill_paper_codes:
        try:
            from app.services.paper_codes import ensure_paper_codes

            # Run paper_code repair after the schema migration transaction commits.
            # Otherwise PostgreSQL can block on the outer DDL transaction's table locks.
            with Session(engine) as backfill_session:
                ensure_paper_codes(backfill_session)
                backfill_session.commit()
        except Exception:
            logger.exception("Automatic database migration failed while backfilling papers.paper_code")

    # Mark this URL as initialized so subsequent init_db() calls can skip
    _initialized_urls.add(database_url)


def _mask_url_internal(database_url: str) -> str:
    """Mask credentials in a database URL for logging."""
    if "@" in database_url:
        return database_url.split("@")[-1]
    return "***"


def get_db_session():
    from app.config import get_settings

    settings = get_settings()
    factory = _session_factories.get(settings.database_url)
    if factory is None:
        get_engine(settings.database_url)
        factory = _session_factories[settings.database_url]
    session = factory()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(database_url: str):
    factory = _session_factories.get(database_url)
    if factory is None:
        get_engine(database_url)
        factory = _session_factories[database_url]
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
