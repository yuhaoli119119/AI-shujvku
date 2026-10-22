from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.migrations.schema_target import resolve_migration_schema


MIGRATION_VERSION = "009_content_web_review_bundle_v2_active_generation_v1"


def upgrade(
    connection: Connection,
    *,
    target_schema: str | None = None,
) -> dict[str, Any]:
    """Add nullable active-generation ownership without rewriting old rows."""

    schema = resolve_migration_schema(connection, expected_schema=target_schema)
    bundles = schema.table(connection, "content_web_review_bundles_v2")
    active_index = schema.identifier(
        connection,
        "uq_content_web_review_bundles_v2_active_generation_key",
    )
    retention_index = schema.identifier(
        connection,
        "ix_content_web_review_bundles_v2_paper_status_created",
    )
    connection.execute(
        text(
            f"ALTER TABLE {bundles} ADD COLUMN IF NOT EXISTS "
            "active_generation_key VARCHAR(64)"
        )
    )
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {active_index} "
            f"ON {bundles} (active_generation_key)"
        )
    )
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {retention_index} "
            f"ON {bundles} (paper_id, status, created_at)"
        )
    )
    return {
        "migration_version": MIGRATION_VERSION,
        "target_schema": schema.name,
        "active_generation_column": True,
        "unique_index": True,
        "retention_index": True,
    }
