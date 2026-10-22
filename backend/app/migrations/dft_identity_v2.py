from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from app.migrations.schema_target import MigrationSchema, resolve_migration_schema


MIGRATION_VERSION = "005_dft_identity_v2_nullable"


RESULT_COLUMNS = (
    "ADD COLUMN IF NOT EXISTS identity_version INTEGER",
    "ADD COLUMN IF NOT EXISTS subject_key VARCHAR(128)",
    "ADD COLUMN IF NOT EXISTS observation_key VARCHAR(128)",
    "ADD COLUMN IF NOT EXISTS identity_payload JSONB",
)

ISSUE_COLUMNS = (
    "ADD COLUMN IF NOT EXISTS result_id UUID",
    "ADD COLUMN IF NOT EXISTS issue_key_version INTEGER",
    "ADD COLUMN IF NOT EXISTS issue_key VARCHAR(128)",
    "ADD COLUMN IF NOT EXISTS lifecycle_version INTEGER",
    "ADD COLUMN IF NOT EXISTS lifecycle_stage VARCHAR(64)",
    "ADD COLUMN IF NOT EXISTS resolution_code VARCHAR(64)",
    "ADD COLUMN IF NOT EXISTS parent_issue_id UUID",
    "ADD COLUMN IF NOT EXISTS last_error_code VARCHAR(128)",
    "ADD COLUMN IF NOT EXISTS retry_count INTEGER",
    "ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP",
)


def _constraint_exists(
    connection: Connection,
    schema: MigrationSchema,
    *,
    table_name: str,
    constraint_name: str,
    constraint_type: str | None = None,
) -> bool:
    type_filter = "AND con.contype = :constraint_type" if constraint_type else ""
    parameters = {
        "schema": schema.name,
        "table_name": table_name,
        "constraint_name": constraint_name,
    }
    if constraint_type:
        parameters["constraint_type"] = constraint_type
    return bool(
        connection.scalar(
            text(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint AS con
                    JOIN pg_class AS relation ON relation.oid = con.conrelid
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = :schema
                      AND relation.relname = :table_name
                      AND con.conname = :constraint_name
                      {type_filter}
                )
                """
            ),
            parameters,
        )
    )


def _add_constraint(
    connection: Connection,
    schema: MigrationSchema,
    *,
    table_name: str,
    constraint_name: str,
    definition: str,
    constraint_type: str | None = None,
) -> None:
    if _constraint_exists(
        connection,
        schema,
        table_name=table_name,
        constraint_name=constraint_name,
        constraint_type=constraint_type,
    ):
        return
    connection.execute(
        text(
            f"ALTER TABLE {schema.table(connection, table_name)} "
            f"ADD CONSTRAINT {schema.identifier(connection, constraint_name)} {definition}"
        )
    )


def upgrade(connection: Connection, *, target_schema: str | None = None) -> None:
    """Install nullable Identity v2 storage in exactly one resolved schema."""

    schema = resolve_migration_schema(connection, expected_schema=target_schema)
    dft_results = schema.table(connection, "dft_results")
    dft_issues = schema.table(connection, "dft_audit_issues")
    candidates = schema.table(connection, "external_analysis_candidates")
    sources = schema.table(connection, "dft_audit_issue_sources")

    for column in RESULT_COLUMNS:
        connection.execute(text(f"ALTER TABLE {dft_results} {column}"))
    for column in ISSUE_COLUMNS:
        connection.execute(text(f"ALTER TABLE {dft_issues} {column}"))

    _add_constraint(
        connection,
        schema,
        table_name="dft_audit_issues",
        constraint_name="dft_audit_issues_result_id_fkey",
        definition=f"FOREIGN KEY (result_id) REFERENCES {dft_results}(id) ON DELETE SET NULL",
    )
    _add_constraint(
        connection,
        schema,
        table_name="dft_audit_issues",
        constraint_name="dft_audit_issues_parent_issue_id_fkey",
        definition=f"FOREIGN KEY (parent_issue_id) REFERENCES {dft_issues}(id) ON DELETE SET NULL",
    )

    issue_indexes = (
        ("ix_dft_audit_issues_result_id", "result_id"),
        ("ix_dft_audit_issues_issue_key", "issue_key"),
        ("ix_dft_audit_issues_lifecycle_stage", "lifecycle_stage"),
        ("ix_dft_audit_issues_parent_issue_id", "parent_issue_id"),
        ("ix_dft_audit_issues_next_retry_at", "next_retry_at"),
    )
    for index_name, columns in issue_indexes:
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {schema.identifier(connection, index_name)} "
                f"ON {dft_issues} ({columns})"
            )
        )
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS "
            f"{schema.identifier(connection, 'uq_dft_results_identity_v2_observation')} "
            f"ON {dft_results} (paper_id, identity_version, observation_key) "
            "WHERE observation_key IS NOT NULL"
        )
    )

    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {sources} (
                issue_id UUID NOT NULL,
                candidate_id UUID NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(text(f"ALTER TABLE {sources} ADD COLUMN IF NOT EXISTS issue_id UUID"))
    connection.execute(text(f"ALTER TABLE {sources} ADD COLUMN IF NOT EXISTS candidate_id UUID"))
    connection.execute(
        text(
            f"ALTER TABLE {sources} ADD COLUMN IF NOT EXISTS created_at "
            "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    )
    _add_constraint(
        connection,
        schema,
        table_name="dft_audit_issue_sources",
        constraint_name="dft_audit_issue_sources_pkey",
        definition="PRIMARY KEY (issue_id, candidate_id)",
        constraint_type="p",
    )
    _add_constraint(
        connection,
        schema,
        table_name="dft_audit_issue_sources",
        constraint_name="dft_audit_issue_sources_issue_id_fkey",
        definition=f"FOREIGN KEY (issue_id) REFERENCES {dft_issues}(id) ON DELETE CASCADE",
    )
    _add_constraint(
        connection,
        schema,
        table_name="dft_audit_issue_sources",
        constraint_name="dft_audit_issue_sources_candidate_id_fkey",
        definition=f"FOREIGN KEY (candidate_id) REFERENCES {candidates}(id) ON DELETE CASCADE",
    )
    for index_name, columns in (
        ("ix_dft_audit_issue_sources_issue_id", "issue_id"),
        ("ix_dft_audit_issue_sources_candidate_id", "candidate_id"),
    ):
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {schema.identifier(connection, index_name)} "
                f"ON {sources} ({columns})"
            )
        )


def downgrade(connection: Connection, *, target_schema: str | None = None) -> None:
    """Remove Identity v2 storage only from an explicitly confirmed schema."""

    schema = resolve_migration_schema(
        connection,
        expected_schema=target_schema,
        destructive=True,
    )
    dft_results = schema.table(connection, "dft_results")
    dft_issues = schema.table(connection, "dft_audit_issues")
    sources = schema.table(connection, "dft_audit_issue_sources")

    connection.execute(text(f"DROP TABLE IF EXISTS {sources}"))
    for index_name in (
        "uq_dft_results_identity_v2_observation",
        "ix_dft_audit_issues_next_retry_at",
        "ix_dft_audit_issues_parent_issue_id",
        "ix_dft_audit_issues_lifecycle_stage",
        "ix_dft_audit_issues_issue_key",
        "ix_dft_audit_issues_result_id",
    ):
        connection.execute(
            text(
                f"DROP INDEX IF EXISTS {schema.quoted_name}."
                f"{schema.identifier(connection, index_name)}"
            )
        )
    for column in (
        "next_retry_at",
        "retry_count",
        "last_error_code",
        "parent_issue_id",
        "resolution_code",
        "lifecycle_stage",
        "lifecycle_version",
        "issue_key",
        "issue_key_version",
        "result_id",
    ):
        connection.execute(text(f"ALTER TABLE IF EXISTS {dft_issues} DROP COLUMN IF EXISTS {column}"))
    for column in ("identity_payload", "observation_key", "subject_key", "identity_version"):
        connection.execute(text(f"ALTER TABLE IF EXISTS {dft_results} DROP COLUMN IF EXISTS {column}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run {MIGRATION_VERSION}")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--upgrade", action="store_true")
    action.add_argument("--downgrade", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("LITAI_DATABASE_URL"))
    parser.add_argument("--target-schema", default="public")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or LITAI_DATABASE_URL is required")

    engine = create_engine(args.database_url, future=True)
    try:
        with engine.begin() as connection:
            if args.upgrade:
                upgrade(connection, target_schema=args.target_schema)
            else:
                downgrade(connection, target_schema=args.target_schema)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
