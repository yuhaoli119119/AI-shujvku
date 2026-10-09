from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.migrations.dft_identity_v2 import downgrade, upgrade
from app.migrations.schema_target import MigrationSchemaError, resolve_migration_schema


DFT_RESULT_V2_COLUMNS = {
    "identity_version",
    "subject_key",
    "observation_key",
    "identity_payload",
}
DFT_ISSUE_V2_COLUMNS = {
    "result_id",
    "issue_key_version",
    "issue_key",
    "lifecycle_version",
    "lifecycle_stage",
    "resolution_code",
    "parent_issue_id",
    "last_error_code",
    "retry_count",
    "next_retry_at",
}


def _schema_url(database_url: str, schema: str) -> str:
    parsed = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema},public"
    return parsed.set(query=query).render_as_string(hide_password=False)


def _public_engine(database_url: str):
    parsed = make_url(database_url)
    query = dict(parsed.query)
    query.pop("options", None)
    query["options"] = "-csearch_path=public"
    return create_engine(parsed.set(query=query).render_as_string(hide_password=False), future=True)


def _public_source_snapshot(engine) -> dict:
    with engine.connect() as connection:
        exists = bool(
            connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class AS relation
                        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND relation.relname = 'dft_audit_issue_sources'
                          AND relation.relkind IN ('r', 'p')
                    )
                    """
                )
            )
        )
        if not exists:
            return {"exists": False}
        columns = connection.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'dft_audit_issue_sources'
                ORDER BY ordinal_position
                """
            )
        ).all()
        constraints = connection.execute(
            text(
                """
                SELECT con.conname, con.contype,
                       pg_get_constraintdef(con.oid, true)
                FROM pg_constraint AS con
                JOIN pg_class AS relation ON relation.oid = con.conrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'dft_audit_issue_sources'
                ORDER BY con.conname
                """
            )
        ).all()
        indexes = connection.execute(
            text(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = 'dft_audit_issue_sources'
                ORDER BY indexname
                """
            )
        ).all()
        count = connection.scalar(text("SELECT count(*) FROM public.dft_audit_issue_sources"))
        return {
            "exists": True,
            "row_count": int(count),
            "columns": [tuple(row) for row in columns],
            "constraints": [tuple(row) for row in constraints],
            "indexes": [tuple(row) for row in indexes],
        }


@pytest.fixture
def public_source_guard(shared_test_database):
    engine = _public_engine(shared_test_database.url)
    before = _public_source_snapshot(engine)
    try:
        yield engine, before
    finally:
        after = _public_source_snapshot(engine)
        engine.dispose()
        assert after == before


@pytest.fixture
def migration_schema(shared_test_database):
    schema = f"identity_v2_{uuid4().hex}"
    with shared_test_database.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(_schema_url(shared_test_database.url, schema), future=True)
    try:
        yield schema, engine
    finally:
        engine.dispose()
        with shared_test_database.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_fresh_database_startup_upgrade_uses_v2_schema_compatibility(migration_schema):
    schema, engine = migration_schema
    from app.db.models import Base
    from app.db.session import init_db

    with engine.begin() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == schema
        assert schema.startswith("identity_v2_")
        Base.metadata.create_all(
            connection.execution_options(schema_translate_map={None: schema}),
            checkfirst=False,
        )

    outcome = init_db(engine.url.render_as_string(hide_password=False), force=True)
    assert outcome.initialized is True
    with engine.begin() as connection:
        upgrade(connection)
        upgrade(connection)

    inspector = inspect(engine)
    result_columns = {
        column["name"]: column
        for column in inspector.get_columns("dft_results", schema=schema)
    }
    issue_columns = {
        column["name"]: column
        for column in inspector.get_columns("dft_audit_issues", schema=schema)
    }
    assert DFT_RESULT_V2_COLUMNS <= set(result_columns)
    assert DFT_ISSUE_V2_COLUMNS <= set(issue_columns)
    assert all(result_columns[name]["nullable"] for name in DFT_RESULT_V2_COLUMNS)
    assert all(issue_columns[name]["nullable"] for name in DFT_ISSUE_V2_COLUMNS)
    assert "dft_audit_issue_sources" in inspector.get_table_names(schema=schema)
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("dft_results", schema=schema)
    }
    assert indexes["uq_dft_results_identity_v2_observation"]["unique"] is True
    assert "observation_key IS NOT NULL" in str(
        indexes["uq_dft_results_identity_v2_observation"]["dialect_options"]["postgresql_where"]
    )
    source_indexes = {
        index["name"]
        for index in inspector.get_indexes("dft_audit_issue_sources", schema=schema)
    }
    assert {
        "ix_dft_audit_issue_sources_issue_id",
        "ix_dft_audit_issue_sources_candidate_id",
    } <= source_indexes
    assert inspector.get_pk_constraint("dft_audit_issue_sources", schema=schema)[
        "constrained_columns"
    ] == ["issue_id", "candidate_id"]


def test_legacy_upgrade_constraints_downgrade_and_reupgrade(
    migration_schema,
    public_source_guard,
):
    schema, engine = migration_schema
    public_engine, public_before = public_source_guard
    assert public_before["exists"] is True
    paper_id = uuid4()
    old_result_id = uuid4()
    old_issue_id = uuid4()
    with engine.begin() as connection:
        target = resolve_migration_schema(connection, expected_schema=schema)
        assert target.name == schema
        assert schema.startswith("identity_v2_")
        papers = target.table(connection, "papers")
        candidates = target.table(connection, "external_analysis_candidates")
        results = target.table(connection, "dft_results")
        issues = target.table(connection, "dft_audit_issues")
        connection.execute(text(f"CREATE TABLE {papers} (id UUID PRIMARY KEY)"))
        connection.execute(
            text(
                f"CREATE TABLE {candidates} ("
                f"id UUID PRIMARY KEY, paper_id UUID REFERENCES {papers}(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(
            text(
                f"CREATE TABLE {results} ("
                f"id UUID PRIMARY KEY, paper_id UUID NOT NULL REFERENCES {papers}(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(
            text(
                f"CREATE TABLE {issues} ("
                f"id UUID PRIMARY KEY, paper_id UUID NOT NULL REFERENCES {papers}(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(text(f"INSERT INTO {papers} (id) VALUES (:id)"), {"id": paper_id})
        connection.execute(
            text(f"INSERT INTO {results} (id, paper_id) VALUES (:id, :paper_id)"),
            {"id": old_result_id, "paper_id": paper_id},
        )
        connection.execute(
            text(f"INSERT INTO {issues} (id, paper_id) VALUES (:id, :paper_id)"),
            {"id": old_issue_id, "paper_id": paper_id},
        )
        upgrade(connection, target_schema=schema)
        upgrade(connection, target_schema=schema)

    with engine.connect() as connection:
        target = resolve_migration_schema(connection, expected_schema=schema)
        results = target.table(connection, "dft_results")
        issues = target.table(connection, "dft_audit_issues")
        old_result = connection.execute(
            text(
                "SELECT identity_version, subject_key, observation_key, identity_payload "
                f"FROM {results} WHERE id = :id"
            ),
            {"id": old_result_id},
        ).one()
        old_issue = connection.execute(
            text(
                "SELECT result_id, issue_key_version, issue_key, lifecycle_version, "
                "lifecycle_stage, resolution_code, parent_issue_id, last_error_code, "
                f"retry_count, next_retry_at FROM {issues} WHERE id = :id"
            ),
            {"id": old_issue_id},
        ).one()
    assert all(value is None for value in old_result)
    assert all(value is None for value in old_issue)

    _assert_v2_constraints(engine, paper_id, schema)

    with engine.begin() as connection:
        with pytest.raises(MigrationSchemaError, match="destructive_migration_requires_expected_schema"):
            downgrade(connection)
        downgrade(connection, target_schema=schema)
    after_first = _identity_schema_snapshot(engine, schema)
    assert _public_source_snapshot(public_engine) == public_before
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == schema
        downgrade(connection, target_schema=schema)
    after_second = _identity_schema_snapshot(engine, schema)
    assert after_second == after_first
    assert _public_source_snapshot(public_engine) == public_before
    inspector = inspect(engine)
    assert not (DFT_RESULT_V2_COLUMNS & {
        column["name"] for column in inspector.get_columns("dft_results", schema=schema)
    })
    assert not (DFT_ISSUE_V2_COLUMNS & {
        column["name"] for column in inspector.get_columns("dft_audit_issues", schema=schema)
    })
    assert "dft_audit_issue_sources" not in inspector.get_table_names(schema=schema)

    with engine.begin() as connection:
        upgrade(connection, target_schema=schema)
        upgrade(connection, target_schema=schema)
    with engine.connect() as connection:
        target = resolve_migration_schema(connection, expected_schema=schema)
        results = target.table(connection, "dft_results")
        row = connection.execute(
            text(
                "SELECT identity_version, subject_key, observation_key, identity_payload "
                f"FROM {results} WHERE id = :id"
            ),
            {"id": old_result_id},
        ).one()
    assert all(value is None for value in row)
    assert _public_source_snapshot(public_engine) == public_before


def _identity_schema_snapshot(engine, schema: str) -> dict:
    inspector = inspect(engine)
    tables = sorted(inspector.get_table_names(schema=schema))
    return {
        "tables": tables,
        "result_columns": sorted(
            column["name"] for column in inspector.get_columns("dft_results", schema=schema)
        ),
        "issue_columns": sorted(
            column["name"] for column in inspector.get_columns("dft_audit_issues", schema=schema)
        ),
        "result_indexes": sorted(
            index["name"] for index in inspector.get_indexes("dft_results", schema=schema)
        ),
    }


def _assert_v2_constraints(engine, paper_id, schema: str) -> None:
    result_a = uuid4()
    result_b = uuid4()
    parent_issue = uuid4()
    child_issue = uuid4()
    candidate_a = uuid4()
    candidate_b = uuid4()
    with engine.begin() as connection:
        target = resolve_migration_schema(connection, expected_schema=schema)
        results = target.table(connection, "dft_results")
        issues = target.table(connection, "dft_audit_issues")
        candidates = target.table(connection, "external_analysis_candidates")
        sources = target.table(connection, "dft_audit_issue_sources")
        connection.execute(
            text(
                f"INSERT INTO {results} "
                "(id, paper_id, identity_version, subject_key, observation_key) "
                "VALUES (:id, :paper_id, 2, 'subject-a', 'observation-a')"
            ),
            {"id": result_a, "paper_id": paper_id},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"INSERT INTO {results} "
                        "(id, paper_id, identity_version, subject_key, observation_key) "
                        "VALUES (:id, :paper_id, 2, 'subject-a', 'observation-a')"
                    ),
                    {"id": uuid4(), "paper_id": paper_id},
                )
        connection.execute(
            text(
                f"INSERT INTO {results} (id, paper_id, identity_version, observation_key) "
                "VALUES (:id, :paper_id, 2, NULL)"
            ),
            {"id": result_b, "paper_id": paper_id},
        )
        connection.execute(
            text(
                f"INSERT INTO {results} (id, paper_id, identity_version, observation_key) "
                "VALUES (:id, :paper_id, 2, NULL)"
            ),
            {"id": uuid4(), "paper_id": paper_id},
        )
        connection.execute(
            text(
                f"INSERT INTO {issues} (id, paper_id, result_id) "
                "VALUES (:id, :paper_id, :result_id)"
            ),
            {"id": parent_issue, "paper_id": paper_id, "result_id": result_a},
        )
        connection.execute(
            text(
                f"INSERT INTO {issues} (id, paper_id, parent_issue_id) "
                "VALUES (:id, :paper_id, :parent_issue_id)"
            ),
            {"id": child_issue, "paper_id": paper_id, "parent_issue_id": parent_issue},
        )
        for candidate_id in (candidate_a, candidate_b):
            connection.execute(
                text(
                    f"INSERT INTO {candidates} (id, paper_id) "
                    "VALUES (:id, :paper_id)"
                ),
                {"id": candidate_id, "paper_id": paper_id},
            )
        connection.execute(
            text(
                f"INSERT INTO {sources} (issue_id, candidate_id) "
                "VALUES (:issue_id, :candidate_id)"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_a},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"INSERT INTO {sources} (issue_id, candidate_id) "
                        "VALUES (:issue_id, :candidate_id)"
                    ),
                    {"issue_id": child_issue, "candidate_id": candidate_a},
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        f"INSERT INTO {sources} (issue_id, candidate_id) "
                        "VALUES (:issue_id, :candidate_id)"
                    ),
                    {"issue_id": child_issue, "candidate_id": uuid4()},
                )

        connection.execute(
            text(f"DELETE FROM {candidates} WHERE id = :id"),
            {"id": candidate_a},
        )
        assert connection.scalar(
            text(
                f"SELECT count(*) FROM {sources} "
                "WHERE issue_id = :issue_id AND candidate_id = :candidate_id"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_a},
        ) == 0

        connection.execute(
            text(f"DELETE FROM {results} WHERE id = :id"), {"id": result_a}
        )
        assert connection.scalar(
            text(f"SELECT result_id FROM {issues} WHERE id = :id"),
            {"id": parent_issue},
        ) is None

        connection.execute(
            text(f"DELETE FROM {issues} WHERE id = :id"), {"id": parent_issue}
        )
        assert connection.scalar(
            text(f"SELECT parent_issue_id FROM {issues} WHERE id = :id"),
            {"id": child_issue},
        ) is None
        connection.execute(
            text(
                f"INSERT INTO {sources} (issue_id, candidate_id) "
                "VALUES (:issue_id, :candidate_id)"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_b},
        )
        connection.execute(
            text(f"DELETE FROM {issues} WHERE id = :id"), {"id": child_issue}
        )
        assert connection.scalar(
            text(f"SELECT count(*) FROM {sources} WHERE candidate_id = :id"),
            {"id": candidate_b},
        ) == 0
