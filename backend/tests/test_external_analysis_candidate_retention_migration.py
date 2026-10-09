from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.migrations.external_analysis_candidate_retention_v1 import analyze, upgrade


def _schema_engine(database_url: str, schema: str):
    parsed = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema},public"
    return create_engine(parsed.set(query=query).render_as_string(hide_password=False), future=True)


def _public_snapshot(database_url: str) -> dict:
    parsed = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = "-csearch_path=public"
    engine = create_engine(parsed.set(query=query).render_as_string(hide_password=False), future=True)
    try:
        with engine.connect() as connection:
            return {
                "candidate_columns": connection.execute(
                    text(
                        """
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='external_analysis_candidates'
                        ORDER BY ordinal_position
                        """
                    )
                ).scalars().all(),
                "source_rows": connection.scalar(
                    text("SELECT count(*) FROM public.dft_audit_issue_sources")
                ),
                "source_constraints": connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid, true)
                        FROM pg_constraint
                        WHERE conrelid='public.dft_audit_issue_sources'::regclass
                        ORDER BY conname
                        """
                    )
                ).all(),
            }
    finally:
        engine.dispose()


@pytest.fixture
def legacy_retention_schema(shared_test_database):
    schema = f"retention_v1_{uuid4().hex}"
    with shared_test_database.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = _schema_engine(shared_test_database.url, schema)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            q = connection.dialect.identifier_preparer.quote_schema(schema)
            connection.execute(text(f"CREATE TABLE {q}.papers (id UUID PRIMARY KEY)"))
            connection.execute(
                text(
                    f"CREATE TABLE {q}.external_analysis_runs ("
                    f"id UUID PRIMARY KEY, paper_id UUID NOT NULL REFERENCES {q}.papers(id))"
                )
            )
            connection.execute(
                text(
                    f"CREATE TABLE {q}.external_analysis_candidates ("
                    f"id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES {q}.external_analysis_runs(id) ON DELETE CASCADE, "
                    f"paper_id UUID NOT NULL REFERENCES {q}.papers(id))"
                )
            )
            connection.execute(text(f"CREATE TABLE {q}.dft_audit_issues (id UUID PRIMARY KEY)"))
            connection.execute(
                text(
                    f"CREATE TABLE {q}.dft_audit_issue_sources ("
                    f"issue_id UUID NOT NULL REFERENCES {q}.dft_audit_issues(id) ON DELETE CASCADE, "
                    f"candidate_id UUID NOT NULL REFERENCES {q}.external_analysis_candidates(id) ON DELETE CASCADE, "
                    f"PRIMARY KEY(issue_id,candidate_id))"
                )
            )
        yield schema, engine
    finally:
        engine.dispose()
        with shared_test_database.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_008_upgrades_legacy_schema_to_restrict_and_is_idempotent(
    legacy_retention_schema,
    shared_test_database,
):
    schema, engine = legacy_retention_schema
    public_before = _public_snapshot(shared_test_database.url)
    with engine.connect() as connection:
        dry_run = analyze(connection, target_schema=schema)
    assert dry_run["missing_candidate_columns"] == [
        "archived_at",
        "archived_by",
        "archive_reason",
        "archive_context",
    ]
    assert dry_run["candidate_foreign_key_is_restrict"] is False

    with engine.begin() as connection:
        first = upgrade(connection, target_schema=schema)
    with engine.begin() as connection:
        second = upgrade(connection, target_schema=schema)
    assert first["database_writes"] == 6
    assert first["candidate_foreign_key_is_restrict"] is True
    assert second["database_writes"] == 0
    assert second["status"] == "noop"

    with engine.connect() as connection:
        columns = set(
            connection.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema=:schema AND table_name='external_analysis_candidates'
                    """
                ),
                {"schema": schema},
            ).scalars()
        )
        assert {"archived_at", "archived_by", "archive_reason", "archive_context"} <= columns
        assert connection.scalar(
            text("SELECT to_regclass(:name)"),
            {"name": f"{schema}.external_analysis_candidate_recoveries"},
        ) is not None
    assert _public_snapshot(shared_test_database.url) == public_before


def test_008_failure_rolls_back_fk_and_all_schema_changes(
    legacy_retention_schema,
    shared_test_database,
):
    schema, engine = legacy_retention_schema
    public_before = _public_snapshot(shared_test_database.url)
    with pytest.raises(RuntimeError, match="fault_after_foreign_key_drop"):
        with engine.begin() as connection:
            upgrade(
                connection,
                target_schema=schema,
                fault_after_foreign_key_drop=True,
            )
    with engine.connect() as connection:
        report = analyze(connection, target_schema=schema)
    assert report["candidate_foreign_key_is_restrict"] is False
    assert report["recovery_table_present"] is False
    assert len(report["missing_candidate_columns"]) == 4
    assert _public_snapshot(shared_test_database.url) == public_before


def test_008_restrict_blocks_direct_candidate_and_cascading_run_deletes(
    legacy_retention_schema,
    shared_test_database,
):
    schema, engine = legacy_retention_schema
    public_before = _public_snapshot(shared_test_database.url)
    paper_id, run_id, candidate_id, issue_id = (uuid4() for _ in range(4))
    with engine.begin() as connection:
        upgrade(connection, target_schema=schema)
        q = connection.dialect.identifier_preparer.quote_schema(schema)
        connection.execute(text(f"INSERT INTO {q}.papers(id) VALUES (:id)"), {"id": paper_id})
        connection.execute(
            text(f"INSERT INTO {q}.external_analysis_runs(id,paper_id) VALUES (:id,:paper_id)"),
            {"id": run_id, "paper_id": paper_id},
        )
        connection.execute(
            text(
                f"INSERT INTO {q}.external_analysis_candidates(id,run_id,paper_id) "
                f"VALUES (:id,:run_id,:paper_id)"
            ),
            {"id": candidate_id, "run_id": run_id, "paper_id": paper_id},
        )
        connection.execute(text(f"INSERT INTO {q}.dft_audit_issues(id) VALUES (:id)"), {"id": issue_id})
        connection.execute(
            text(
                f"INSERT INTO {q}.dft_audit_issue_sources(issue_id,candidate_id) "
                f"VALUES (:issue_id,:candidate_id)"
            ),
            {"issue_id": issue_id, "candidate_id": candidate_id},
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(f"DELETE FROM {q}.external_analysis_candidates WHERE id=:id"),
                {"id": candidate_id},
            )
        transaction.rollback()

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(f"DELETE FROM {q}.external_analysis_runs WHERE id=:id"),
                {"id": run_id},
            )
        transaction.rollback()

    with engine.connect() as connection:
        q = connection.dialect.identifier_preparer.quote_schema(schema)
        assert connection.scalar(
            text(f"SELECT count(*) FROM {q}.external_analysis_candidates WHERE id=:id"),
            {"id": candidate_id},
        ) == 1
        assert connection.scalar(
            text(f"SELECT count(*) FROM {q}.external_analysis_runs WHERE id=:id"),
            {"id": run_id},
        ) == 1
    assert _public_snapshot(shared_test_database.url) == public_before
