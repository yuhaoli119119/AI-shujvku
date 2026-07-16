from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "007_content_web_review_bundle_v2"


def upgrade(connection: Connection) -> None:
    """Idempotent indexes for the v2 proposal-only bundle table.

    The table itself is created by the repository's established metadata
    bootstrap before migrations run; these statements also support old DBs.
    """
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_content_web_review_bundles_v2_paper_status "
        "ON content_web_review_bundles_v2 (paper_id, status)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_content_web_review_bundles_v2_fingerprint "
        "ON content_web_review_bundles_v2 (snapshot_fingerprint)"
    ))
