from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from tools.backfill_dft_reaction_type import (
    _build_update_statement,
    backfill_engine,
    build_backfill_report,
    main,
    parse_args,
)


def _make_engine(database_url: str = "sqlite+pysqlite:///:memory:") -> sa.Engine:
    engine = sa.create_engine(database_url, future=True)
    metadata = sa.MetaData()
    sa.Table(
        "dft_results",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("property_type", sa.String),
        sa.Column("adsorbate", sa.String),
        sa.Column("reaction_step", sa.String),
        sa.Column("evidence_text", sa.String),
        sa.Column("value", sa.Float),
        sa.Column("unit", sa.String),
        sa.Column("reaction_type", sa.String),
        sa.Column("reaction_type_source", sa.String),
        sa.Column("reaction_type_confidence", sa.Float),
        sa.Column("reaction_profile_version", sa.String),
        sa.Column("reaction_validation_status", sa.String),
    )
    metadata.create_all(engine)
    return engine


def _insert_rows(engine: sa.Engine, rows: list[dict[str, object]]) -> None:
    with engine.begin() as connection:
        connection.execute(sa.table("dft_results", *[sa.column(name) for name in rows[0]]).insert(), rows)


def _rows_by_id(engine: sa.Engine) -> dict[str, dict[str, object]]:
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT * FROM dft_results ORDER BY id")).mappings().all()
    return {str(row["id"]): dict(row) for row in rows}


def _base_row(record_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": record_id,
        "property_type": "adsorption energy",
        "adsorbate": "Li2S6",
        "reaction_step": "Li2S8 to Li2S6",
        "evidence_text": "Polysulfide adsorption in the sulfur reduction reaction.",
        "value": -1.23,
        "unit": "eV",
        "reaction_type": None,
        "reaction_type_source": None,
        "reaction_type_confidence": None,
        "reaction_profile_version": None,
        "reaction_validation_status": None,
    }
    row.update(overrides)
    return row


def test_dry_run_reports_eligible_updates_without_writing() -> None:
    engine = _make_engine()
    _insert_rows(engine, [_base_row("valid-1")])
    before = _rows_by_id(engine)

    report = backfill_engine(engine, apply=False)

    assert report["dry_run"] is True
    assert report["eligible_updates"] == 1
    assert report["applied_updates"] == 0
    assert report["writes_performed"] == 0
    assert report["planned_updates"][0]["record_id"] == "valid-1"
    assert _rows_by_id(engine) == before
    engine.dispose()


def test_apply_updates_only_valid_deterministic_records() -> None:
    engine = _make_engine()
    _insert_rows(
        engine,
        [
            _base_row("valid-1"),
            _base_row(
                "ambiguous-1",
                adsorbate="*H",
                reaction_step=None,
                evidence_text="Adsorption energy was calculated.",
            ),
            _base_row("unsupported-1", property_type="unknown descriptor"),
        ],
    )

    report = backfill_engine(engine, apply=True)
    rows = _rows_by_id(engine)

    assert report["eligible_updates"] == 1
    assert report["applied_updates"] == 1
    assert report["writes_performed"] == 1
    assert report["ambiguous"] == 1
    assert report["unsupported"] == 1
    assert rows["valid-1"]["reaction_type"] == "SRR_LiS"
    assert rows["valid-1"]["reaction_type_source"] == "rule"
    assert rows["valid-1"]["reaction_validation_status"] == "valid"
    assert rows["valid-1"]["reaction_profile_version"] == "reaction_profiles_v1"
    assert rows["valid-1"]["reaction_type_confidence"] == 0.9
    assert rows["ambiguous-1"]["reaction_type"] is None
    assert rows["unsupported-1"]["reaction_type"] is None
    engine.dispose()


def test_human_and_manual_sources_are_protected_from_apply() -> None:
    engine = _make_engine()
    _insert_rows(
        engine,
        [
            _base_row("human-1", reaction_type="HER", reaction_type_source="human"),
            _base_row("manual-1", reaction_type="OER", reaction_type_source=" manual "),
        ],
    )

    report = backfill_engine(engine, apply=True)
    rows = _rows_by_id(engine)

    assert report["protected_human_labels"] == 2
    assert report["eligible_updates"] == 0
    assert report["writes_performed"] == 0
    assert rows["human-1"]["reaction_type"] == "HER"
    assert rows["human-1"]["reaction_type_source"] == "human"
    assert rows["manual-1"]["reaction_type"] == "OER"
    assert rows["manual-1"]["reaction_type_source"] == " manual "
    engine.dispose()


def test_existing_non_empty_reaction_type_is_skipped_by_default() -> None:
    engine = _make_engine()
    _insert_rows(
        engine,
        [_base_row("existing-1", reaction_type="UNKNOWN", reaction_type_source="rule")],
    )

    report = backfill_engine(engine, apply=True)
    rows = _rows_by_id(engine)

    assert report["skipped_existing"] == 1
    assert report["eligible_updates"] == 0
    assert report["writes_performed"] == 0
    assert rows["existing-1"]["reaction_type"] == "UNKNOWN"
    assert rows["existing-1"]["reaction_type_source"] == "rule"
    engine.dispose()


def test_apply_preserves_original_dft_fields() -> None:
    engine = _make_engine()
    _insert_rows(
        engine,
        [
            _base_row(
                "valid-1",
                property_type="adsorption energy",
                adsorbate="Li2S4",
                evidence_text="Li-S polysulfide adsorption evidence.",
                value=-2.5,
                unit="eV",
            )
        ],
    )

    before = _rows_by_id(engine)["valid-1"]
    backfill_engine(engine, apply=True)
    after = _rows_by_id(engine)["valid-1"]

    for field in ("value", "unit", "property_type", "adsorbate", "evidence_text"):
        assert after[field] == before[field]
    assert after["reaction_type"] == "SRR_LiS"
    engine.dispose()


def test_build_report_exposes_before_after_and_validation_details() -> None:
    report = build_backfill_report([_base_row("valid-1")])

    planned = report["planned_updates"][0]
    assert planned["record_id"] == "valid-1"
    assert planned["reaction_type"] == "SRR_LiS"
    assert planned["validation_property_type"] == "adsorption_energy"
    assert planned["before"]["reaction_type"] is None
    assert planned["after"]["reaction_type"] == "SRR_LiS"
    assert planned["after"]["reaction_type_source"] == "rule"


def test_postgresql_uuid_id_update_uses_reflected_column_type() -> None:
    metadata = sa.MetaData()
    dft_results = sa.Table(
        "dft_results",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("reaction_type", sa.String),
        sa.Column("reaction_type_source", sa.String),
        sa.Column("reaction_type_confidence", sa.Float),
        sa.Column("reaction_profile_version", sa.String),
        sa.Column("reaction_validation_status", sa.String),
    )
    report = build_backfill_report(
        [
            _base_row(
                "11111111-1111-1111-1111-111111111111",
                adsorbate="Li2S6",
                evidence_text="Polysulfide adsorption in lithium-sulfur SRR.",
            )
        ]
    )

    statement = _build_update_statement(dft_results, report["planned_updates"][0])
    compiled = statement.compile(dialect=postgresql.dialect())
    id_bind = next(
        bind
        for bind in compiled.binds.values()
        if bind.value == "11111111-1111-1111-1111-111111111111"
    )

    assert isinstance(id_bind.type, postgresql.UUID)
    assert "reaction_type" in compiled.string
    assert "value" not in compiled.string
    assert "unit" not in compiled.string
    assert "property_type" not in compiled.string
    assert "evidence_text" not in compiled.string


def test_cli_defaults_to_dry_run_and_outputs_json(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "backfill.sqlite"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    engine = _make_engine(database_url)
    _insert_rows(engine, [_base_row("valid-1")])
    engine.dispose()

    exit_code = main(["--database-url", database_url])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["dry_run"] is True
    assert report["eligible_updates"] == 1
    assert report["writes_performed"] == 0


def test_cli_can_write_json_output_file(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "backfill.sqlite"
    output_path = tmp_path / "report.json"
    database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
    engine = _make_engine(database_url)
    _insert_rows(engine, [_base_row("valid-1")])
    engine.dispose()

    exit_code = main(["--database-url", database_url, "--output", str(output_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""
    assert json.loads(output_path.read_text(encoding="utf-8"))["eligible_updates"] == 1


def test_cli_arguments_are_safe_by_default() -> None:
    args = parse_args([])

    assert args.apply is False
    assert args.limit is None
    assert args.sample_limit == 10
    assert args.database_url is None
