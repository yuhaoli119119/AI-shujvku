from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "008_content_web_review_local_verification_v1"


def upgrade(connection: Connection) -> None:
    """Idempotent indexes for authenticated local-verification results."""

    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_content_web_review_local_result_item "
        "ON content_web_review_local_verification_results (bundle_id, plan_item_id)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_content_web_review_local_result_bundle_status "
        "ON content_web_review_local_verification_results (bundle_id, status)"
    ))
