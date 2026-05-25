from __future__ import annotations

from contextlib import contextmanager
import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

_engines: dict[str, object] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}
logger = logging.getLogger(__name__)


def get_engine(database_url: str):
    if database_url not in _engines:
        engine = create_engine(database_url, future=True)
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
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)
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
                            max_q = connection.execute(
                                text("SELECT MAX(serial_number) FROM papers WHERE library_name = :lib AND serial_number IS NOT NULL"),
                                {"lib": lib}
                            ).scalar()
                            if lib not in counters:
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
