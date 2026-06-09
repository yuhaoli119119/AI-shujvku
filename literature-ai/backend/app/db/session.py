from __future__ import annotations

from contextlib import contextmanager
import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE: This project uses PostgreSQL (with pgvector extension) as its
# primary database, NOT SQLite. The default connection string is:
#   postgresql+psycopg://literature_ai:literature_ai@postgres:5432/literature_ai
#
# IMPORTANT for AI developers:
# - PostgreSQL supports concurrent read/write — no SQLite-style file locking.
# - pgvector provides HNSW vector indexing for semantic search.
# - JSONB columns are used for structured data (not plain JSON).
# - UUID columns are native PostgreSQL UUID type (not CHAR(32)).
# - session_scope() commits on success, rolls back on exception.
# - Avoid holding sessions open during long external calls (e.g. VLM inference);
#   read what you need, close the session, then do the slow work.
# ──────────────────────────────────────────────────────────────────────────────

_engines: dict[str, object] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}
logger = logging.getLogger(__name__)


def get_engine(database_url: str):
    if database_url not in _engines:
        engine = create_engine(database_url, future=True, pool_size=20, max_overflow=50)
        _engines[database_url] = engine
        _session_factories[database_url] = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _engines[database_url]


def init_db(database_url: str) -> None:
    engine = get_engine(database_url)
    if engine.dialect.name == "postgresql":
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
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE papers DROP CONSTRAINT IF EXISTS papers_doi_key"))
            connection.execute(text("DROP INDEX IF EXISTS ix_papers_doi"))
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

    if "papers" in table_names:
        with engine.begin() as connection:
            execute_migration_step(
                "papers",
                "comprehensive_analysis",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS comprehensive_analysis JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN comprehensive_analysis JSON"
                ),
            )
            execute_migration_step(
                "papers",
                "library_name",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS library_name VARCHAR(255) NOT NULL DEFAULT '\u9ed8\u8ba4\u6587\u732e\u5e93'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN library_name VARCHAR(255) NOT NULL DEFAULT '\u9ed8\u8ba4\u6587\u732e\u5e93'"
                ),
            )
            serial_added = execute_migration_step(
                "papers",
                "serial_number",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS serial_number INTEGER"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN serial_number INTEGER"
                ),
            )
            try:
                if serial_added or has_column("papers", "serial_number"):
                    if engine.dialect.name == "postgresql":
                        # Backfill serial_number for existing papers ordered by created_at per library
                        connection.execute(text("""
                            UPDATE papers SET serial_number = sub.rn FROM (
                              SELECT id, ROW_NUMBER() OVER (PARTITION BY library_name ORDER BY created_at) AS rn
                              FROM papers WHERE serial_number IS NULL
                            ) sub WHERE papers.id = sub.id
                        """))
                    else:
                        # SQLite: fetch and update individually
                        rows = connection.execute(
                            text("SELECT id, library_name, created_at FROM papers WHERE serial_number IS NULL ORDER BY library_name, created_at")
                        ).fetchall()
                        counters: dict[str, int] = {}
                        for row in rows:
                            lib = row[1] or "\u9ed8\u8ba4\u6587\u732e\u5e93"
                            if lib not in counters:
                                max_q = connection.execute(
                                    text("SELECT MAX(serial_number) FROM papers WHERE library_name = :lib AND serial_number IS NOT NULL"),
                                    {"lib": lib}
                                ).scalar()
                                counters[lib] = (max_q or 0)
                            counters[lib] += 1
                            connection.execute(
                                text("UPDATE papers SET serial_number = :sn WHERE id = :pid"),
                                {"sn": counters[lib], "pid": row[0]}
                            )
            except Exception:
                logger.exception("Automatic database migration failed while backfilling papers.serial_number")
            execute_migration_step(
                "papers",
                "paper_type",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS paper_type VARCHAR(20)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN paper_type VARCHAR(20)"
                ),
            )
            execute_migration_step(
                "papers",
                "type_confidence",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS type_confidence DOUBLE PRECISION"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN type_confidence FLOAT"
                ),
            )
            execute_migration_step(
                "papers",
                "classification_source",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS classification_source VARCHAR(20)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN classification_source VARCHAR(20)"
                ),
            )
            execute_migration_step(
                "papers",
                "workflow_status",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS workflow_status VARCHAR(64) NOT NULL DEFAULT 'Imported'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN workflow_status VARCHAR(64) NOT NULL DEFAULT 'Imported'"
                ),
            )
            execute_migration_step(
                "papers",
                "pdf_quality_status",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_status VARCHAR(32)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN pdf_quality_status VARCHAR(32)"
                ),
            )
            execute_migration_step(
                "papers",
                "pdf_quality_score",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_score DOUBLE PRECISION"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN pdf_quality_score FLOAT"
                ),
            )
            execute_migration_step(
                "papers",
                "pdf_quality_report",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS pdf_quality_report JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN pdf_quality_report JSON"
                ),
            )
            execute_migration_step(
                "papers",
                "workspace_path",
                (
                    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS workspace_path TEXT"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE papers ADD COLUMN workspace_path TEXT"
                ),
            )
            execute_migration_step(
                "paper_tables",
                "prov",
                (
                    "ALTER TABLE paper_tables ADD COLUMN IF NOT EXISTS prov JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_tables ADD COLUMN prov JSON"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "role_confidence",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS role_confidence FLOAT"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN role_confidence FLOAT"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "content_summary",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS content_summary TEXT"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN content_summary TEXT"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "key_elements",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS key_elements JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN key_elements JSON"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "prov",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS prov JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN prov JSON"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "figure_label",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS figure_label VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN figure_label VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "crop_status",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_status VARCHAR(32) NOT NULL DEFAULT 'candidate_crop'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN crop_status VARCHAR(32) NOT NULL DEFAULT 'candidate_crop'"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "crop_confidence",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_confidence DOUBLE PRECISION"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN crop_confidence FLOAT"
                ),
            )
            execute_migration_step(
                "paper_figures",
                "crop_source",
                (
                    "ALTER TABLE paper_figures ADD COLUMN IF NOT EXISTS crop_source VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE paper_figures ADD COLUMN crop_source VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "dft_results",
                "reaction_step",
                (
                    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS reaction_step VARCHAR(255)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE dft_results ADD COLUMN reaction_step VARCHAR(255)"
                ),
            )
            execute_migration_step(
                "dft_results",
                "candidate_status",
                (
                    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS candidate_status VARCHAR(64) NOT NULL DEFAULT 'system_candidate'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE dft_results ADD COLUMN candidate_status VARCHAR(64) NOT NULL DEFAULT 'system_candidate'"
                ),
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
                (
                    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS evidence_payload JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE dft_results ADD COLUMN evidence_payload JSON"
                ),
            )
            execute_migration_step(
                "dft_results",
                "extraction_protocol_version",
                (
                    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS extraction_protocol_version VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE dft_results ADD COLUMN extraction_protocol_version VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "claim_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS claim_id UUID"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN claim_id CHAR(32)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "chunk_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS chunk_id VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN chunk_id VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "source_type",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'unknown'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN source_type VARCHAR(32) NOT NULL DEFAULT 'unknown'"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "page",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS page INTEGER"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN page INTEGER"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "bbox",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS bbox JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN bbox JSON"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "section",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS section VARCHAR(255)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN section VARCHAR(255)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "figure_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS figure_id UUID"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN figure_id CHAR(32)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "table_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS table_id UUID"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN table_id CHAR(32)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "equation_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS equation_id VARCHAR(128)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN equation_id VARCHAR(128)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "target_type",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS target_type VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN target_type VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "target_id",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS target_id VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN target_id VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "field_name",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS field_name VARCHAR(128)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN field_name VARCHAR(128)"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "char_start",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS char_start INTEGER"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN char_start INTEGER"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "char_end",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS char_end INTEGER"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN char_end INTEGER"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "locator_status",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS locator_status VARCHAR(32) NOT NULL DEFAULT 'missing'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN locator_status VARCHAR(32) NOT NULL DEFAULT 'missing'"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "locator_confidence",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS locator_confidence DOUBLE PRECISION NOT NULL DEFAULT 0"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN locator_confidence FLOAT NOT NULL DEFAULT 0"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "parser_source",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS parser_source VARCHAR(32) NOT NULL DEFAULT 'unknown'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN parser_source VARCHAR(32) NOT NULL DEFAULT 'unknown'"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "warning_reason",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS warning_reason TEXT"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN warning_reason TEXT"
                ),
            )
            execute_migration_step(
                "evidence_locators",
                "updated_at",
                (
                    "ALTER TABLE evidence_locators ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE evidence_locators ADD COLUMN updated_at DATETIME"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_fingerprint",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_fingerprint VARCHAR(128)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN target_fingerprint VARCHAR(128)"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_label",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_label VARCHAR(255)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN target_label VARCHAR(255)"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "field_path",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS field_path VARCHAR(255)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN field_path VARCHAR(255)"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "target_resolution_status",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS target_resolution_status VARCHAR(32) NOT NULL DEFAULT 'active'"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN target_resolution_status VARCHAR(32) NOT NULL DEFAULT 'active'"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "remapped_from_target_id",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS remapped_from_target_id VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN remapped_from_target_id VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "last_resolved_target_id",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS last_resolved_target_id VARCHAR(64)"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN last_resolved_target_id VARCHAR(64)"
                ),
            )
            execute_migration_step(
                "extraction_field_reviews",
                "review_payload",
                (
                    "ALTER TABLE extraction_field_reviews ADD COLUMN IF NOT EXISTS review_payload JSONB"
                    if engine.dialect.name == "postgresql"
                    else "ALTER TABLE extraction_field_reviews ADD COLUMN review_payload JSON"
                ),
            )


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


def switch_database(database_url: str, storage_root: str | None = None) -> None:
    """Switch the active database at runtime.

    Disposes the old engine, updates the environment variable,
    clears settings cache, and initializes the new database.
    Optionally updates storage_root in the active settings instance.

    Args:
        database_url: New SQLite or PostgreSQL URL.
        storage_root: If provided, the storage directory for the new library
                      (e.g. ``/path/to/library/storage``). Settings.storage_root
                      will be updated to this path.
    """
    import os
    from pathlib import Path

    from app.config import get_settings

    settings = get_settings()
    if (
        bool(getattr(settings, "force_configured_database", False))
        and database_url.strip().lower().startswith("sqlite")
        and not settings.database_url.strip().lower().startswith("sqlite")
    ):
        raise RuntimeError(
            "Runtime SQLite switching is disabled because LITAI_FORCE_CONFIGURED_DATABASE=true "
            f"and the configured database is {settings.database_url!r}."
        )
    old_url = settings.database_url

    # Dispose old engine if it exists
    if old_url in _engines:
        old_engine = _engines.pop(old_url, None)
        _session_factories.pop(old_url, None)
        if old_engine:
            old_engine.dispose()

    # Update environment so Settings picks it up
    os.environ["LITAI_DATABASE_URL"] = database_url
    if storage_root is not None:
        os.environ["LITAI_STORAGE_ROOT"] = storage_root
    get_settings.cache_clear()

    # Also patch the in-process settings instance so callers that already
    # hold a reference see the new storage_root immediately.
    if storage_root is not None:
        new_settings = get_settings()
        object.__setattr__(new_settings, "storage_root", Path(storage_root))

    # Initialize new database
    init_db(database_url)
