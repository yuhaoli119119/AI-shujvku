from __future__ import annotations

import json
from copy import deepcopy

import pytest
import sqlalchemy as sa

from tools.audit_reaction_backfill import audit_engine, build_report, parse_args


def test_definite_lis_record_is_classifiable_as_srr() -> None:
    report = build_report(
        [
            {
                "id": "lis-1",
                "property_type": "adsorption energy",
                "adsorbate": "Li2S6",
                "reaction_step": "Li2S8 to Li2S6",
                "evidence_text": "Polysulfide adsorption in the sulfur reduction reaction.",
            }
        ]
    )

    assert report["classifiable"] == 1
    assert report["by_reaction_type"] == {"SRR_LiS": 1}
    assert report["by_property_type"] == {"adsorption_energy": 1}


def test_shared_h_without_context_remains_ambiguous() -> None:
    report = build_report(
        [{"id": "h-1", "property_type": "adsorption energy", "adsorbate": "*H"}]
    )

    assert report["ambiguous"] == 1
    assert report["classifiable"] == 0
    assert report["by_reaction_type"] == {"UNKNOWN": 1}
    assert report["by_reason"] == {"insufficient_or_shared_context": 1}


def test_explicit_non_human_reaction_type_is_validated_and_counted() -> None:
    report = build_report(
        [
            {
                "id": "her-1",
                "reaction_type": "HER",
                "reaction_type_source": "rule",
                "property_type": "gibbs free energy",
                "adsorbate": "*H",
            }
        ]
    )

    assert report["classifiable"] == 1
    assert report["by_reaction_type"] == {"HER": 1}
    assert report["by_reason"] == {"explicit_reaction_type": 1}


@pytest.mark.parametrize("source", ["human", "manual", " HUMAN "])
def test_human_and_manual_labels_are_protected(source: str) -> None:
    row = {
        "id": "human-1",
        "reaction_type": "HER",
        "reaction_type_source": source,
        "property_type": "not supported",
        "adsorbate": "Li2S6",
    }
    original = deepcopy(row)

    report = build_report([row])

    assert row == original
    assert report["unchanged_human_labels"] == 1
    assert report["classifiable"] == report["unsupported"] == report["ambiguous"] == 0
    assert report["by_reason"] == {"protected_human_label": 1}


def test_unsupported_validation_reasons_are_stable() -> None:
    rows = [
        {"id": "b", "reaction_type": "HER", "property_type": "unknown", "adsorbate": "*OH"},
        {"id": "a", "reaction_type": "HER", "property_type": "unknown", "adsorbate": "*OH"},
    ]

    report = build_report(rows, sample_limit=5)

    assert report["unsupported"] == 2
    assert report["by_reason"] == {
        "intermediate_out_of_scope": 2,
        "property_out_of_scope": 2,
    }
    assert report["sample_record_ids"]["reason:intermediate_out_of_scope"] == ["a", "b"]


def test_sample_limit_and_json_serialization() -> None:
    rows = [
        {"id": f"row-{index}", "property_type": "adsorption energy", "adsorbate": "*H"}
        for index in range(5)
    ]

    report = build_report(rows, sample_limit=2)

    assert all(len(record_ids) <= 2 for record_ids in report["sample_record_ids"].values())
    assert json.loads(json.dumps(report))["dry_run"] is True
    assert report["writes_performed"] == 0


def _make_engine_with_dft_table(*, reaction_columns: bool) -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    metadata = sa.MetaData()
    columns = [
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("property_type", sa.String),
        sa.Column("adsorbate", sa.String),
        sa.Column("reaction_step", sa.String),
        sa.Column("evidence_text", sa.String),
        sa.Column("value", sa.Float),
        sa.Column("unit", sa.String),
    ]
    if reaction_columns:
        columns.extend(
            [
                sa.Column("reaction_type", sa.String),
                sa.Column("reaction_type_source", sa.String),
                sa.Column("reaction_type_confidence", sa.Float),
                sa.Column("reaction_profile_version", sa.String),
                sa.Column("reaction_validation_status", sa.String),
            ]
        )
    sa.Table("dft_results", metadata, *columns)
    metadata.create_all(engine)
    return engine


def test_old_schema_without_reaction_columns_can_be_audited() -> None:
    engine = _make_engine_with_dft_table(reaction_columns=False)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dft_results "
                "(id, property_type, adsorbate, reaction_step, evidence_text, value, unit) "
                "VALUES ('old-1', 'adsorption energy', 'Li2S4', NULL, 'polysulfide', -1.2, 'eV')"
            )
        )

    report = audit_engine(engine)

    assert report["classifiable"] == 1
    assert report["schema"]["reaction_columns_present"] == []
    assert report["schema"]["reaction_columns_missing"] == [
        "reaction_type",
        "reaction_type_source",
        "reaction_type_confidence",
        "reaction_profile_version",
        "reaction_validation_status",
    ]
    engine.dispose()


def test_audit_is_read_only_and_preserves_all_database_content() -> None:
    engine = _make_engine_with_dft_table(reaction_columns=True)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dft_results "
                "(id, property_type, adsorbate, reaction_step, evidence_text, value, unit, "
                "reaction_type, reaction_type_source) VALUES "
                "('new-1', 'gibbs free energy', '*H', NULL, 'HER activity', 0.1, 'eV', "
                "'HER', 'human')"
            )
        )
    with engine.connect() as connection:
        before = connection.execute(sa.text("SELECT * FROM dft_results ORDER BY id")).mappings().all()

    statements: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip())

    report = audit_engine(engine)

    with engine.connect() as connection:
        after = connection.execute(sa.text("SELECT * FROM dft_results ORDER BY id")).mappings().all()
    sa.event.remove(engine, "before_cursor_execute", capture_statement)

    assert list(before) == list(after)
    assert len(before) == len(after) == 1
    assert report["unchanged_human_labels"] == 1
    forbidden = ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP", "TRUNCATE")
    audit_statements = statements[:-1]
    assert audit_statements
    assert not any(statement.upper().startswith(forbidden) for statement in audit_statements)
    engine.dispose()


def test_cli_exposes_no_write_or_apply_mode() -> None:
    args = parse_args([])

    assert args.output is None
    assert args.sample_limit == 10
    assert args.database_url is None
    assert set(vars(args)) == {"output", "sample_limit", "database_url"}
