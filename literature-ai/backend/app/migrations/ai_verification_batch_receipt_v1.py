from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "009_ai_verification_batch_receipt_v1"


def upgrade(connection: Connection) -> None:
    """Create the durable idempotency receipt used by direct AI verification."""

    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS ai_verification_batch_receipts (
            id UUID PRIMARY KEY,
            paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            source_identity VARCHAR(160) NOT NULL,
            request_id VARCHAR(128) NOT NULL,
            request_fingerprint VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'committed',
            receipt_payload JSONB,
            committed_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_ai_verification_batch_receipt_request
                UNIQUE (paper_id, source_identity, request_id)
        )
    """))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ai_verification_batch_receipt_paper_created "
        "ON ai_verification_batch_receipts (paper_id, created_at)"
    ))
