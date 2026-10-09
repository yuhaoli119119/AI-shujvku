from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings
from app.domain.reaction_taxonomy import (
    PROFILE_VERSION,
    classify_reaction_record,
    validate_reaction_record,
)
from tools.audit_reaction_backfill import (
    HUMAN_SOURCES,
    QUERY_COLUMNS,
    REACTION_COLUMNS,
    TABLE_NAME,
    _candidate,
    _mapping,
)


WRITE_VALUES = {
    "reaction_type_source": "rule",
    "reaction_profile_version": PROFILE_VERSION,
    "reaction_validation_status": "valid",
}


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely backfill deterministic, validated DFT reaction types."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write eligible reaction fields. Omit for the default dry-run mode.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        help="Maximum number of DFT records to inspect, ordered by id.",
    )
    parser.add_argument(
        "--sample-limit",
        type=_non_negative_int,
        default=10,
        help="Maximum skipped/ambiguous/unsupported record samples retained per bucket.",
    )
    parser.add_argument(
        "--database-url",
        help="Optional SQLAlchemy database URL; defaults to LITAI_DATABASE_URL.",
    )
    return parser.parse_args(argv)


def _has_existing_reaction_type(row: Mapping[str, Any]) -> bool:
    return bool(str(row.get("reaction_type") or "").strip())


def _add_sample(
    samples: dict[str, list[dict[str, Any]]],
    key: str,
    sample: dict[str, Any],
    sample_limit: int,
) -> None:
    if len(samples[key]) < sample_limit:
        samples[key].append(sample)


def _read_rows(engine: Engine, *, limit: int | None = None) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    inspector = sa.inspect(engine)
    if not inspector.has_table(TABLE_NAME):
        raise RuntimeError(f"Required table {TABLE_NAME!r} does not exist")

    available_columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    selected_columns = [name for name in QUERY_COLUMNS if name in available_columns]
    required_columns = {"id", "property_type", "adsorbate", "reaction_step", "evidence_text"}
    missing_required = sorted(required_columns - available_columns)
    if missing_required:
        raise RuntimeError(f"Table {TABLE_NAME!r} is missing required columns: {missing_required}")

    dft_results = sa.table(TABLE_NAME, *(sa.column(name) for name in selected_columns))
    statement = sa.select(*(dft_results.c[name] for name in selected_columns)).order_by(
        dft_results.c.id
    )
    if limit is not None:
        statement = statement.limit(limit)

    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement).mappings()]

    schema_metadata = {
        "table": TABLE_NAME,
        "selected_columns": selected_columns,
        "reaction_columns_present": [name for name in REACTION_COLUMNS if name in available_columns],
        "reaction_columns_missing": [name for name in REACTION_COLUMNS if name not in available_columns],
    }
    return rows, schema_metadata


def _planned_update(row: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate(row)
    classification = classify_reaction_record(candidate)
    reaction_type = classification["reaction_type"]
    validation = validate_reaction_record(reaction_type, candidate)
    after = {
        "reaction_type": reaction_type,
        "reaction_type_source": WRITE_VALUES["reaction_type_source"],
        "reaction_type_confidence": classification["confidence"],
        "reaction_profile_version": WRITE_VALUES["reaction_profile_version"],
        "reaction_validation_status": WRITE_VALUES["reaction_validation_status"],
    }
    return {
        "record_id": str(row.get("id", "")),
        "reaction_type": reaction_type,
        "classification_status": classification["status"],
        "classification_reason": classification["reason"],
        "validation_status": validation["status"],
        "validation_property_type": validation.get("property_type"),
        "validation_reasons": validation.get("reasons") or [],
        "before": {name: row.get(name) for name in REACTION_COLUMNS},
        "after": after,
        "update_values": after,
    }


def build_backfill_report(
    rows: Iterable[Any],
    *,
    apply: bool = False,
    sample_limit: int = 10,
    schema_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if sample_limit < 0:
        raise ValueError("sample_limit must be zero or greater")

    counters = Counter(
        total_records=0,
        eligible_updates=0,
        applied_updates=0,
        skipped_existing=0,
        protected_human_labels=0,
        ambiguous=0,
        unsupported=0,
        writes_performed=0,
    )
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    planned_updates: list[dict[str, Any]] = []

    mapped_rows = sorted((_mapping(row) for row in rows), key=lambda row: str(row.get("id", "")))
    for row in mapped_rows:
        counters["total_records"] += 1
        record_id = str(row.get("id", ""))
        source = str(row.get("reaction_type_source") or "").strip().lower()

        if source in HUMAN_SOURCES:
            counters["protected_human_labels"] += 1
            _add_sample(
                samples,
                "protected_human_labels",
                {
                    "record_id": record_id,
                    "reaction_type": row.get("reaction_type"),
                    "reaction_type_source": row.get("reaction_type_source"),
                    "reason": "protected_human_label",
                },
                sample_limit,
            )
            continue

        if _has_existing_reaction_type(row):
            counters["skipped_existing"] += 1
            _add_sample(
                samples,
                "skipped_existing",
                {
                    "record_id": record_id,
                    "reaction_type": row.get("reaction_type"),
                    "reaction_type_source": row.get("reaction_type_source"),
                    "reason": "existing_reaction_type",
                },
                sample_limit,
            )
            continue

        plan = _planned_update(row)
        if plan["classification_status"] != "classified":
            counters["ambiguous"] += 1
            _add_sample(
                samples,
                "ambiguous",
                {
                    "record_id": record_id,
                    "reaction_type": plan["reaction_type"],
                    "reason": plan["classification_reason"],
                    "validation_reasons": plan["validation_reasons"],
                },
                sample_limit,
            )
            continue

        if plan["validation_status"] != "valid":
            counters["unsupported"] += 1
            _add_sample(
                samples,
                "unsupported",
                {
                    "record_id": record_id,
                    "reaction_type": plan["reaction_type"],
                    "validation_property_type": plan["validation_property_type"],
                    "reasons": plan["validation_reasons"] or ["validation_failed"],
                },
                sample_limit,
            )
            continue

        counters["eligible_updates"] += 1
        planned_updates.append(plan)

    if apply:
        counters["applied_updates"] = len(planned_updates)
        counters["writes_performed"] = len(planned_updates)

    report: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "dry_run": not apply,
        "apply": apply,
        **counters,
        "planned_updates": [
            {
                key: value
                for key, value in plan.items()
                if key != "update_values"
            }
            for plan in planned_updates
        ],
        "sample_records": dict(sorted(samples.items())),
    }
    if schema_metadata is not None:
        report["schema"] = dict(schema_metadata)
    return report


def _require_reaction_columns(schema_metadata: Mapping[str, Any]) -> None:
    missing = list(schema_metadata.get("reaction_columns_missing") or [])
    if missing:
        raise RuntimeError(
            f"Table {TABLE_NAME!r} is missing reaction columns required for apply: {missing}"
        )


def _build_update_statement(
    dft_results: sa.Table,
    plan: Mapping[str, Any],
) -> sa.sql.dml.Update:
    values = {name: plan["after"][name] for name in REACTION_COLUMNS}
    return (
        sa.update(dft_results)
        .where(dft_results.c.id == plan["record_id"])
        .where(
            sa.or_(
                dft_results.c.reaction_type.is_(None),
                dft_results.c.reaction_type == "",
            )
        )
        .where(
            sa.or_(
                dft_results.c.reaction_type_source.is_(None),
                sa.func.lower(sa.func.trim(dft_results.c.reaction_type_source)).not_in(
                    tuple(HUMAN_SOURCES)
                ),
            )
        )
        .values(**values)
    )


def _apply_planned_updates(engine: Engine, planned_updates: Sequence[Mapping[str, Any]]) -> int:
    if not planned_updates:
        return 0

    applied_updates = 0
    with engine.begin() as connection:
        metadata = sa.MetaData()
        dft_results = sa.Table(TABLE_NAME, metadata, autoload_with=connection)
        for plan in planned_updates:
            result = connection.execute(_build_update_statement(dft_results, plan))
            if result.rowcount and result.rowcount > 0:
                applied_updates += result.rowcount
    return applied_updates


def backfill_engine(
    engine: Engine,
    *,
    apply: bool = False,
    limit: int | None = None,
    sample_limit: int = 10,
) -> dict[str, Any]:
    rows, schema_metadata = _read_rows(engine, limit=limit)
    report = build_backfill_report(
        rows,
        apply=apply,
        sample_limit=sample_limit,
        schema_metadata=schema_metadata,
    )
    if apply:
        _require_reaction_columns(schema_metadata)
        applied_updates = _apply_planned_updates(engine, report["planned_updates"])
        report["applied_updates"] = applied_updates
        report["writes_performed"] = applied_updates
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = args.database_url or get_settings().database_url
    engine = sa.create_engine(database_url, future=True)
    try:
        report = backfill_engine(
            engine,
            apply=args.apply,
            limit=args.limit,
            sample_limit=args.sample_limit,
        )
    finally:
        engine.dispose()

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
