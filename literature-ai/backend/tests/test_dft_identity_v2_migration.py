from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.migrations.dft_identity_v2 import downgrade, upgrade


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
    from app.db.session import init_db

    outcome = init_db(engine.url.render_as_string(hide_password=False), force=True)
    assert outcome.initialized is True
    with engine.begin() as connection:
        upgrade(connection)
        upgrade(connection)

    inspector = inspect(engine)
    result_columns = {
        column["name"]: column for column in inspector.get_columns("dft_results")
    }
    issue_columns = {
        column["name"]: column for column in inspector.get_columns("dft_audit_issues")
    }
    assert DFT_RESULT_V2_COLUMNS <= set(result_columns)
    assert DFT_ISSUE_V2_COLUMNS <= set(issue_columns)
    assert all(result_columns[name]["nullable"] for name in DFT_RESULT_V2_COLUMNS)
    assert all(issue_columns[name]["nullable"] for name in DFT_ISSUE_V2_COLUMNS)
    assert "dft_audit_issue_sources" in inspector.get_table_names()
    indexes = {index["name"]: index for index in inspector.get_indexes("dft_results")}
    assert indexes["uq_dft_results_identity_v2_observation"]["unique"] is True
    assert "observation_key IS NOT NULL" in str(
        indexes["uq_dft_results_identity_v2_observation"]["dialect_options"]["postgresql_where"]
    )
    source_indexes = {
        index["name"] for index in inspector.get_indexes("dft_audit_issue_sources")
    }
    assert {
        "ix_dft_audit_issue_sources_issue_id",
        "ix_dft_audit_issue_sources_candidate_id",
    } <= source_indexes
    assert inspector.get_pk_constraint("dft_audit_issue_sources")["constrained_columns"] == [
        "issue_id",
        "candidate_id",
    ]


def test_legacy_upgrade_constraints_downgrade_and_reupgrade(migration_schema):
    schema, engine = migration_schema
    paper_id = uuid4()
    old_result_id = uuid4()
    old_issue_id = uuid4()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE papers (id UUID PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE external_analysis_candidates ("
                "id UUID PRIMARY KEY, paper_id UUID REFERENCES papers(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE dft_results ("
                "id UUID PRIMARY KEY, paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE dft_audit_issues ("
                "id UUID PRIMARY KEY, paper_id UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(text("INSERT INTO papers (id) VALUES (:id)"), {"id": paper_id})
        connection.execute(
            text("INSERT INTO dft_results (id, paper_id) VALUES (:id, :paper_id)"),
            {"id": old_result_id, "paper_id": paper_id},
        )
        connection.execute(
            text("INSERT INTO dft_audit_issues (id, paper_id) VALUES (:id, :paper_id)"),
            {"id": old_issue_id, "paper_id": paper_id},
        )
        upgrade(connection)
        upgrade(connection)

    with engine.connect() as connection:
        old_result = connection.execute(
            text(
                "SELECT identity_version, subject_key, observation_key, identity_payload "
                "FROM dft_results WHERE id = :id"
            ),
            {"id": old_result_id},
        ).one()
        old_issue = connection.execute(
            text(
                "SELECT result_id, issue_key_version, issue_key, lifecycle_version, "
                "lifecycle_stage, resolution_code, parent_issue_id, last_error_code, "
                "retry_count, next_retry_at FROM dft_audit_issues WHERE id = :id"
            ),
            {"id": old_issue_id},
        ).one()
    assert all(value is None for value in old_result)
    assert all(value is None for value in old_issue)

    _assert_v2_constraints(engine, paper_id)

    with engine.begin() as connection:
        downgrade(connection)
        downgrade(connection)
    inspector = inspect(engine)
    assert not (DFT_RESULT_V2_COLUMNS & {
        column["name"] for column in inspector.get_columns("dft_results")
    })
    assert not (DFT_ISSUE_V2_COLUMNS & {
        column["name"] for column in inspector.get_columns("dft_audit_issues")
    })
    assert "dft_audit_issue_sources" not in inspector.get_table_names()

    with engine.begin() as connection:
        upgrade(connection)
        upgrade(connection)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT identity_version, subject_key, observation_key, identity_payload "
                "FROM dft_results WHERE id = :id"
            ),
            {"id": old_result_id},
        ).one()
    assert all(value is None for value in row)


def _assert_v2_constraints(engine, paper_id) -> None:
    result_a = uuid4()
    result_b = uuid4()
    parent_issue = uuid4()
    child_issue = uuid4()
    candidate_a = uuid4()
    candidate_b = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dft_results "
                "(id, paper_id, identity_version, subject_key, observation_key) "
                "VALUES (:id, :paper_id, 2, 'subject-a', 'observation-a')"
            ),
            {"id": result_a, "paper_id": paper_id},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO dft_results "
                        "(id, paper_id, identity_version, subject_key, observation_key) "
                        "VALUES (:id, :paper_id, 2, 'subject-a', 'observation-a')"
                    ),
                    {"id": uuid4(), "paper_id": paper_id},
                )
        connection.execute(
            text(
                "INSERT INTO dft_results (id, paper_id, identity_version, observation_key) "
                "VALUES (:id, :paper_id, 2, NULL)"
            ),
            {"id": result_b, "paper_id": paper_id},
        )
        connection.execute(
            text(
                "INSERT INTO dft_results (id, paper_id, identity_version, observation_key) "
                "VALUES (:id, :paper_id, 2, NULL)"
            ),
            {"id": uuid4(), "paper_id": paper_id},
        )
        connection.execute(
            text(
                "INSERT INTO dft_audit_issues (id, paper_id, result_id) "
                "VALUES (:id, :paper_id, :result_id)"
            ),
            {"id": parent_issue, "paper_id": paper_id, "result_id": result_a},
        )
        connection.execute(
            text(
                "INSERT INTO dft_audit_issues (id, paper_id, parent_issue_id) "
                "VALUES (:id, :paper_id, :parent_issue_id)"
            ),
            {"id": child_issue, "paper_id": paper_id, "parent_issue_id": parent_issue},
        )
        for candidate_id in (candidate_a, candidate_b):
            connection.execute(
                text(
                    "INSERT INTO external_analysis_candidates (id, paper_id) "
                    "VALUES (:id, :paper_id)"
                ),
                {"id": candidate_id, "paper_id": paper_id},
            )
        connection.execute(
            text(
                "INSERT INTO dft_audit_issue_sources (issue_id, candidate_id) "
                "VALUES (:issue_id, :candidate_id)"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_a},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO dft_audit_issue_sources (issue_id, candidate_id) "
                        "VALUES (:issue_id, :candidate_id)"
                    ),
                    {"issue_id": child_issue, "candidate_id": candidate_a},
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO dft_audit_issue_sources (issue_id, candidate_id) "
                        "VALUES (:issue_id, :candidate_id)"
                    ),
                    {"issue_id": child_issue, "candidate_id": uuid4()},
                )

        connection.execute(
            text("DELETE FROM external_analysis_candidates WHERE id = :id"),
            {"id": candidate_a},
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM dft_audit_issue_sources "
                "WHERE issue_id = :issue_id AND candidate_id = :candidate_id"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_a},
        ) == 0

        connection.execute(
            text("DELETE FROM dft_results WHERE id = :id"), {"id": result_a}
        )
        assert connection.scalar(
            text("SELECT result_id FROM dft_audit_issues WHERE id = :id"),
            {"id": parent_issue},
        ) is None

        connection.execute(
            text("DELETE FROM dft_audit_issues WHERE id = :id"), {"id": parent_issue}
        )
        assert connection.scalar(
            text("SELECT parent_issue_id FROM dft_audit_issues WHERE id = :id"),
            {"id": child_issue},
        ) is None
        connection.execute(
            text(
                "INSERT INTO dft_audit_issue_sources (issue_id, candidate_id) "
                "VALUES (:issue_id, :candidate_id)"
            ),
            {"issue_id": child_issue, "candidate_id": candidate_b},
        )
        connection.execute(
            text("DELETE FROM dft_audit_issues WHERE id = :id"), {"id": child_issue}
        )
        assert connection.scalar(
            text("SELECT count(*) FROM dft_audit_issue_sources WHERE candidate_id = :id"),
            {"id": candidate_b},
        ) == 0
