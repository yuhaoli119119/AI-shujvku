from __future__ import annotations

import argparse
import os
from collections.abc import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "005_dft_identity_v2_nullable"


UPGRADE_STATEMENTS = (
    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS identity_version INTEGER",
    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS subject_key VARCHAR(128)",
    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS observation_key VARCHAR(128)",
    "ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS identity_payload JSONB",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS result_id UUID",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS issue_key_version INTEGER",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS issue_key VARCHAR(128)",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS lifecycle_version INTEGER",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS lifecycle_stage VARCHAR(64)",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS resolution_code VARCHAR(64)",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS parent_issue_id UUID",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS last_error_code VARCHAR(128)",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS retry_count INTEGER",
    "ALTER TABLE dft_audit_issues ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'dft_audit_issues'::regclass
              AND conname = 'dft_audit_issues_result_id_fkey'
        ) THEN
            ALTER TABLE dft_audit_issues
                ADD CONSTRAINT dft_audit_issues_result_id_fkey
                FOREIGN KEY (result_id) REFERENCES dft_results(id) ON DELETE SET NULL;
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'dft_audit_issues'::regclass
              AND conname = 'dft_audit_issues_parent_issue_id_fkey'
        ) THEN
            ALTER TABLE dft_audit_issues
                ADD CONSTRAINT dft_audit_issues_parent_issue_id_fkey
                FOREIGN KEY (parent_issue_id) REFERENCES dft_audit_issues(id) ON DELETE SET NULL;
        END IF;
    END
    $$
    """,
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_result_id ON dft_audit_issues (result_id)",
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_issue_key ON dft_audit_issues (issue_key)",
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_lifecycle_stage ON dft_audit_issues (lifecycle_stage)",
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_parent_issue_id ON dft_audit_issues (parent_issue_id)",
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issues_next_retry_at ON dft_audit_issues (next_retry_at)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_dft_results_identity_v2_observation
    ON dft_results (paper_id, identity_version, observation_key)
    WHERE observation_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS dft_audit_issue_sources (
        issue_id UUID NOT NULL,
        candidate_id UUID NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE dft_audit_issue_sources ADD COLUMN IF NOT EXISTS issue_id UUID",
    "ALTER TABLE dft_audit_issue_sources ADD COLUMN IF NOT EXISTS candidate_id UUID",
    """
    ALTER TABLE dft_audit_issue_sources
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'dft_audit_issue_sources'::regclass
              AND contype = 'p'
        ) THEN
            ALTER TABLE dft_audit_issue_sources
                ADD CONSTRAINT dft_audit_issue_sources_pkey
                PRIMARY KEY (issue_id, candidate_id);
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'dft_audit_issue_sources'::regclass
              AND conname = 'dft_audit_issue_sources_issue_id_fkey'
        ) THEN
            ALTER TABLE dft_audit_issue_sources
                ADD CONSTRAINT dft_audit_issue_sources_issue_id_fkey
                FOREIGN KEY (issue_id) REFERENCES dft_audit_issues(id) ON DELETE CASCADE;
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'dft_audit_issue_sources'::regclass
              AND conname = 'dft_audit_issue_sources_candidate_id_fkey'
        ) THEN
            ALTER TABLE dft_audit_issue_sources
                ADD CONSTRAINT dft_audit_issue_sources_candidate_id_fkey
                FOREIGN KEY (candidate_id) REFERENCES external_analysis_candidates(id) ON DELETE CASCADE;
        END IF;
    END
    $$
    """,
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issue_sources_issue_id ON dft_audit_issue_sources (issue_id)",
    "CREATE INDEX IF NOT EXISTS ix_dft_audit_issue_sources_candidate_id ON dft_audit_issue_sources (candidate_id)",
)


DOWNGRADE_STATEMENTS = (
    "DROP TABLE IF EXISTS dft_audit_issue_sources",
    "DROP INDEX IF EXISTS uq_dft_results_identity_v2_observation",
    "DROP INDEX IF EXISTS ix_dft_audit_issues_next_retry_at",
    "DROP INDEX IF EXISTS ix_dft_audit_issues_parent_issue_id",
    "DROP INDEX IF EXISTS ix_dft_audit_issues_lifecycle_stage",
    "DROP INDEX IF EXISTS ix_dft_audit_issues_issue_key",
    "DROP INDEX IF EXISTS ix_dft_audit_issues_result_id",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS next_retry_at",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS retry_count",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS last_error_code",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS parent_issue_id",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS resolution_code",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS lifecycle_stage",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS lifecycle_version",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS issue_key",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS issue_key_version",
    "ALTER TABLE IF EXISTS dft_audit_issues DROP COLUMN IF EXISTS result_id",
    "ALTER TABLE IF EXISTS dft_results DROP COLUMN IF EXISTS identity_payload",
    "ALTER TABLE IF EXISTS dft_results DROP COLUMN IF EXISTS observation_key",
    "ALTER TABLE IF EXISTS dft_results DROP COLUMN IF EXISTS subject_key",
    "ALTER TABLE IF EXISTS dft_results DROP COLUMN IF EXISTS identity_version",
)


def _execute(connection: Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(text(statement))


def upgrade(connection: Connection) -> None:
    """Install nullable Identity v2 storage without backfilling existing rows."""

    _execute(connection, UPGRADE_STATEMENTS)


def downgrade(connection: Connection) -> None:
    """Remove Identity v2 storage in dependency-safe reverse order."""

    _execute(connection, DOWNGRADE_STATEMENTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run {MIGRATION_VERSION}")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--upgrade", action="store_true")
    action.add_argument("--downgrade", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("LITAI_DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or LITAI_DATABASE_URL is required")

    engine = create_engine(args.database_url, future=True)
    try:
        with engine.begin() as connection:
            (upgrade if args.upgrade else downgrade)(connection)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
