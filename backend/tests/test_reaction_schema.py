from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db import session as db_session
from app.db.models import DFTResult, Paper


REACTION_COLUMNS = {
    "reaction_type": 32,
    "reaction_type_source": 32,
    "reaction_type_confidence": None,
    "reaction_profile_version": 64,
    "reaction_validation_status": 32,
}


def test_reaction_columns_are_nullable_and_reaction_type_is_indexed():
    table = DFTResult.__table__

    for name, length in REACTION_COLUMNS.items():
        column = table.c[name]
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None
        if length is not None:
            assert column.type.length == length

    assert any(
        index.name == "ix_dft_results_reaction_type"
        and [column.name for column in index.columns] == ["reaction_type"]
        for index in table.indexes
    )


def test_legacy_dft_result_constructor_keeps_reaction_fields_unset():
    row = DFTResult(
        paper_id=uuid4(),
        property_type="adsorption_energy",
        adsorbate="Li2S6",
        value=-1.2,
        unit="eV",
    )

    for name in REACTION_COLUMNS:
        assert getattr(row, name) is None


def test_reaction_fields_round_trip_in_isolated_database():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        paper = Paper(title="Reaction schema test", pdf_path="")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            value=-1.2,
            unit="eV",
            reaction_type="SRR_LiS",
            reaction_type_source="rule",
            reaction_type_confidence=0.95,
            reaction_profile_version="reaction_profiles_v1",
            reaction_validation_status="valid",
        )
        session.add(row)
        session.commit()
        row_id = row.id

    with factory() as session:
        stored = session.get(DFTResult, row_id)
        assert stored is not None
        assert stored.reaction_type == "SRR_LiS"
        assert stored.reaction_type_source == "rule"
        assert stored.reaction_type_confidence == 0.95
        assert stored.reaction_profile_version == "reaction_profiles_v1"
        assert stored.reaction_validation_status == "valid"
    engine.dispose()


def test_isolated_database_schema_has_nullable_reaction_columns():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("dft_results")
    }
    for name in REACTION_COLUMNS:
        assert columns[name]["nullable"] is True
    engine.dispose()


def test_postgresql_baseline_contains_reaction_columns_and_index():
    sql = Path("app/migrations/001_init.sql").read_text(encoding="utf-8")

    assert "reaction_type VARCHAR(32)" in sql
    assert "reaction_type_source VARCHAR(32)" in sql
    assert "reaction_type_confidence FLOAT" in sql
    assert "reaction_profile_version VARCHAR(64)" in sql
    assert "reaction_validation_status VARCHAR(32)" in sql
    assert "CREATE INDEX ix_dft_results_reaction_type ON dft_results (reaction_type);" in sql
    assert "reaction_type VARCHAR(32) NOT NULL" not in sql


def test_runtime_migration_is_nullable_and_idempotent_by_contract():
    source = Path("app/db/session.py").read_text(encoding="utf-8")

    for name, sql_type in {
        "reaction_type": "VARCHAR(32)",
        "reaction_type_source": "VARCHAR(32)",
        "reaction_type_confidence": "FLOAT",
        "reaction_profile_version": "VARCHAR(64)",
        "reaction_validation_status": "VARCHAR(32)",
    }.items():
        statement = f"ALTER TABLE dft_results ADD COLUMN IF NOT EXISTS {name} {sql_type}"
        assert statement in source
        assert f"{statement} NOT NULL" not in source

    assert (
        '"CREATE INDEX IF NOT EXISTS ix_dft_results_reaction_type "' in source
        and '"ON dft_results (reaction_type)"' in source
    )
    assert "CREATE TYPE reaction_type" not in source


def test_runtime_migration_can_run_twice_in_isolated_database():
    database_url = os.environ["LITAI_TEST_DATABASE_URL"]

    db_session.init_db(database_url, force=True)
    db_session.init_db(database_url, force=True)

    columns = {
        column["name"]: column
        for column in inspect(db_session.get_engine(database_url)).get_columns("dft_results")
    }
    assert set(REACTION_COLUMNS).issubset(columns)
    assert all(columns[name]["nullable"] is True for name in REACTION_COLUMNS)
