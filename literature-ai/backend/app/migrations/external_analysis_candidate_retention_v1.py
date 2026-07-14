from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.migrations.schema_target import MigrationSchema, resolve_migration_schema


MIGRATION_VERSION = "008_external_analysis_candidate_retention_v1"


class ExternalAnalysisCandidateRetentionMigrationError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def _table_exists(connection: Connection, schema: MigrationSchema, table_name: str) -> bool:
    return bool(
        connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = :schema
                      AND relation.relname = :table_name
                      AND relation.relkind IN ('r', 'p')
                )
                """
            ),
            {"schema": schema.name, "table_name": table_name},
        )
    )


def _column_names(connection: Connection, schema: MigrationSchema, table_name: str) -> set[str]:
    rows = connection.execute(
        text(
            """
            SELECT attribute.attname
            FROM pg_attribute AS attribute
            JOIN pg_class AS relation ON relation.oid = attribute.attrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = :schema
              AND relation.relname = :table_name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            """
        ),
        {"schema": schema.name, "table_name": table_name},
    )
    return {str(row[0]) for row in rows}


def _candidate_fk_rows(connection: Connection, schema: MigrationSchema) -> list[dict[str, str]]:
    rows = connection.execute(
        text(
            """
            SELECT constraint_row.conname AS constraint_name,
                   pg_get_constraintdef(constraint_row.oid, true) AS definition
            FROM pg_constraint AS constraint_row
            JOIN pg_class AS source_table ON source_table.oid = constraint_row.conrelid
            JOIN pg_namespace AS source_schema ON source_schema.oid = source_table.relnamespace
            JOIN pg_class AS target_table ON target_table.oid = constraint_row.confrelid
            JOIN pg_namespace AS target_schema ON target_schema.oid = target_table.relnamespace
            WHERE constraint_row.contype = 'f'
              AND source_schema.nspname = :schema
              AND target_schema.nspname = :schema
              AND source_table.relname = 'dft_audit_issue_sources'
              AND target_table.relname = 'external_analysis_candidates'
              AND (
                  SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
                  FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
                  JOIN pg_attribute AS attribute
                    ON attribute.attrelid = constraint_row.conrelid
                   AND attribute.attnum = key_column.attnum
              ) = ARRAY['candidate_id']::name[]
            ORDER BY constraint_row.conname
            """
        ),
        {"schema": schema.name},
    ).mappings()
    return [
        {
            "constraint_name": str(row["constraint_name"]),
            "definition": str(row["definition"]),
        }
        for row in rows
    ]


def _analyze(connection: Connection, schema: MigrationSchema) -> dict[str, Any]:
    required_tables = (
        "papers",
        "external_analysis_runs",
        "external_analysis_candidates",
        "dft_audit_issue_sources",
    )
    missing_tables = [name for name in required_tables if not _table_exists(connection, schema, name)]
    candidate_columns = (
        "archived_at",
        "archived_by",
        "archive_reason",
        "archive_context",
    )
    present_columns = (
        _column_names(connection, schema, "external_analysis_candidates")
        if "external_analysis_candidates" not in missing_tables
        else set()
    )
    missing_columns = [name for name in candidate_columns if name not in present_columns]
    recovery_table_present = _table_exists(
        connection,
        schema,
        "external_analysis_candidate_recoveries",
    )
    fk_rows = [] if missing_tables else _candidate_fk_rows(connection, schema)
    errors: list[dict[str, Any]] = []
    if missing_tables:
        errors.append({"reason": "required_tables_missing", "tables": missing_tables})
    elif len(fk_rows) != 1:
        errors.append(
            {
                "reason": "candidate_foreign_key_ambiguous",
                "foreign_keys": fk_rows,
            }
        )
    fk_is_restrict = (
        len(fk_rows) == 1
        and " ON DELETE RESTRICT" in fk_rows[0]["definition"].upper()
    )
    expected_changes = len(missing_columns)
    if not recovery_table_present:
        expected_changes += 1
    if len(fk_rows) == 1 and not fk_is_restrict:
        expected_changes += 1
    return {
        "migration_version": MIGRATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "target_schema": schema.name,
        "missing_tables": missing_tables,
        "missing_candidate_columns": missing_columns,
        "recovery_table_present": recovery_table_present,
        "candidate_foreign_keys": fk_rows,
        "candidate_foreign_key_is_restrict": fk_is_restrict,
        "expected_changes": expected_changes,
        "database_writes": 0,
        "errors": errors,
        "status": "blocked" if errors else ("noop" if expected_changes == 0 else "validated"),
    }


def analyze(
    connection: Connection,
    *,
    target_schema: str | None = None,
) -> dict[str, Any]:
    schema = resolve_migration_schema(connection, expected_schema=target_schema)
    return _analyze(connection, schema)


def upgrade(
    connection: Connection,
    *,
    target_schema: str | None = None,
    fault_after_foreign_key_drop: bool = False,
) -> dict[str, Any]:
    """Install candidate retention atomically in one explicitly resolved schema."""

    schema = resolve_migration_schema(connection, expected_schema=target_schema)
    report = _analyze(connection, schema)
    report["mode"] = "apply"
    if report["errors"]:
        raise ExternalAnalysisCandidateRetentionMigrationError(
            "candidate_retention_precondition_failed",
            report,
        )

    candidates = schema.table(connection, "external_analysis_candidates")
    runs = schema.table(connection, "external_analysis_runs")
    papers = schema.table(connection, "papers")
    sources = schema.table(connection, "dft_audit_issue_sources")
    recoveries = schema.table(connection, "external_analysis_candidate_recoveries")
    writes = 0

    column_definitions = {
        "archived_at": "TIMESTAMP",
        "archived_by": "VARCHAR(128)",
        "archive_reason": "TEXT",
        "archive_context": "JSONB",
    }
    for column_name in report["missing_candidate_columns"]:
        connection.execute(
            text(
                f"ALTER TABLE {candidates} ADD COLUMN "
                f"{schema.identifier(connection, column_name)} {column_definitions[column_name]}"
            )
        )
        writes += 1

    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS "
            f"{schema.identifier(connection, 'ix_external_analysis_candidates_archived_at')} "
            f"ON {candidates} (archived_at)"
        )
    )

    if not report["recovery_table_present"]:
        connection.execute(
            text(
                f"""
                CREATE TABLE {recoveries} (
                    candidate_id UUID PRIMARY KEY
                        REFERENCES {candidates}(id) ON DELETE RESTRICT,
                    paper_id UUID NOT NULL
                        REFERENCES {papers}(id) ON DELETE RESTRICT,
                    run_id UUID NOT NULL
                        REFERENCES {runs}(id) ON DELETE RESTRICT,
                    issue_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    audit_log_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    source_audit_index INTEGER NOT NULL,
                    recovery_version VARCHAR(128) NOT NULL,
                    payload_sha256 VARCHAR(64) NOT NULL,
                    match_manifest JSONB NOT NULL,
                    restored_state JSONB NOT NULL,
                    reason TEXT NOT NULL,
                    actor VARCHAR(128),
                    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        writes += 1
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS "
            f"{schema.identifier(connection, 'ix_external_analysis_candidate_recoveries_paper_run')} "
            f"ON {recoveries} (paper_id, run_id, executed_at)"
        )
    )

    fk_rows = report["candidate_foreign_keys"]
    if fk_rows and not report["candidate_foreign_key_is_restrict"]:
        constraint_name = fk_rows[0]["constraint_name"]
        quoted_constraint = schema.identifier(connection, constraint_name)
        connection.execute(text(f"ALTER TABLE {sources} DROP CONSTRAINT {quoted_constraint}"))
        if fault_after_foreign_key_drop:
            raise RuntimeError("fault_after_foreign_key_drop")
        connection.execute(
            text(
                f"ALTER TABLE {sources} ADD CONSTRAINT {quoted_constraint} "
                f"FOREIGN KEY (candidate_id) REFERENCES {candidates}(id) ON DELETE RESTRICT"
            )
        )
        writes += 1

    final = _analyze(connection, schema)
    final["mode"] = "apply"
    final["database_writes"] = writes
    final["status"] = "applied" if writes else "noop"
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run {MIGRATION_VERSION}")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("LITAI_DATABASE_URL"))
    parser.add_argument("--target-schema", default="public")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or LITAI_DATABASE_URL is required")

    from sqlalchemy import create_engine

    engine = create_engine(args.database_url, future=True)
    try:
        if args.dry_run:
            with engine.connect() as connection:
                report = analyze(connection, target_schema=args.target_schema)
        else:
            with engine.begin() as connection:
                report = upgrade(connection, target_schema=args.target_schema)
    except ExternalAnalysisCandidateRetentionMigrationError as exc:
        report = exc.report
        if args.report:
            Path(args.report).write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 2
    finally:
        engine.dispose()
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
