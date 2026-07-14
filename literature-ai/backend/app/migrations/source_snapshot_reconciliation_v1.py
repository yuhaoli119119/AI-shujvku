from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "006_source_snapshot_reconciliation_v1"


UPGRADE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS source_snapshot_reconciliations (
        id UUID PRIMARY KEY,
        paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        discovery_run_id UUID NOT NULL REFERENCES external_analysis_runs(id) ON DELETE CASCADE,
        historical_fingerprint VARCHAR(64) NOT NULL,
        historical_algorithm_version VARCHAR(128) NOT NULL,
        historical_manifest JSONB NOT NULL,
        current_fingerprint VARCHAR(64) NOT NULL,
        current_algorithm_version VARCHAR(128) NOT NULL,
        current_manifest JSONB NOT NULL,
        comparison JSONB NOT NULL,
        reason TEXT NOT NULL,
        actor VARCHAR(128),
        executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_source_snapshot_reconciliation_identity
    ON source_snapshot_reconciliations (
        paper_id, discovery_run_id, historical_fingerprint, current_fingerprint, current_algorithm_version
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_source_snapshot_reconciliations_paper_run
    ON source_snapshot_reconciliations (paper_id, discovery_run_id, executed_at DESC)
    """,
)


def _execute(connection: Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(text(statement))


def upgrade(connection: Connection) -> None:
    _execute(connection, UPGRADE_STATEMENTS)
