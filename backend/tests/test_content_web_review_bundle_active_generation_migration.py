from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.migrations.content_web_review_bundle_v2_active_generation_v1 import (
    upgrade,
)


def _schema_engine(database_url: str, schema: str):
    parsed = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema},public"
    return create_engine(
        parsed.set(query=query).render_as_string(hide_password=False),
        future=True,
    )


def test_active_generation_migration_upgrades_legacy_table_idempotently(
    shared_test_database,
):
    schema = f"bundle_active_{uuid4().hex}"
    existing_id = uuid4()
    with shared_test_database.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = _schema_engine(shared_test_database.url, schema)
    try:
        with engine.begin() as connection:
            q = connection.dialect.identifier_preparer.quote_schema(schema)
            connection.execute(
                text(
                    f"CREATE TABLE {q}.content_web_review_bundles_v2 ("
                    "id UUID PRIMARY KEY, paper_id UUID NOT NULL, "
                    "policy_version VARCHAR(64) NOT NULL, "
                    "snapshot_fingerprint VARCHAR(64) NOT NULL, "
                    "manifest JSONB NOT NULL, proposal_payload JSONB, "
                    "status VARCHAR(32) NOT NULL, created_by VARCHAR(128), "
                    "created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {q}.content_web_review_bundles_v2 "
                    "(id,paper_id,policy_version,snapshot_fingerprint,manifest,"
                    "status,created_at,updated_at) "
                    "VALUES (:id,:paper_id,'legacy','fp',CAST(:manifest AS JSONB),"
                    "'generated',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"id": existing_id, "paper_id": uuid4(), "manifest": "{}"},
            )
        with engine.begin() as connection:
            first = upgrade(connection, target_schema=schema)
            second = upgrade(connection, target_schema=schema)
        assert first["unique_index"] is True
        assert second["unique_index"] is True
        with engine.begin() as connection:
            q = connection.dialect.identifier_preparer.quote_schema(schema)
            rows = connection.execute(
                text(
                    f"SELECT id, active_generation_key "
                    f"FROM {q}.content_web_review_bundles_v2"
                )
            ).all()
            assert rows == [(existing_id, None)]
            connection.execute(
                text(
                    f"INSERT INTO {q}.content_web_review_bundles_v2 "
                    "(id,paper_id,policy_version,snapshot_fingerprint,manifest,"
                    "status,created_at,updated_at,active_generation_key) "
                    "VALUES (:id,:paper_id,'legacy','fp',CAST(:manifest AS JSONB),"
                    "'generated',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)"
                ),
                {"id": uuid4(), "paper_id": uuid4(), "manifest": "{}"},
            )
            connection.execute(
                text(
                    f"UPDATE {q}.content_web_review_bundles_v2 "
                    "SET active_generation_key='same-key' WHERE id=:id"
                ),
                {"id": existing_id},
            )
        with engine.connect() as connection:
            transaction = connection.begin()
            q = connection.dialect.identifier_preparer.quote_schema(schema)
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        f"UPDATE {q}.content_web_review_bundles_v2 "
                        "SET active_generation_key='same-key' "
                        "WHERE id<>:id"
                    ),
                    {"id": existing_id},
                )
            transaction.rollback()
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    """
                    SELECT count(*) FROM information_schema.columns
                    WHERE table_schema=:schema
                      AND table_name='content_web_review_bundles_v2'
                      AND column_name='active_generation_key'
                    """
                ),
                {"schema": schema},
            ) == 1
            assert connection.scalar(
                text(
                    f"SELECT count(*) FROM "
                    f"{connection.dialect.identifier_preparer.quote_schema(schema)}."
                    "content_web_review_bundles_v2"
                )
            ) == 2
    finally:
        engine.dispose()
        with shared_test_database.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
