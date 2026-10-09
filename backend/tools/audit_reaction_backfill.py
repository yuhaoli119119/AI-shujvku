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
    normalize_reaction_type,
    validate_reaction_record,
)


TABLE_NAME = "dft_results"
REACTION_COLUMNS = (
    "reaction_type",
    "reaction_type_source",
    "reaction_type_confidence",
    "reaction_profile_version",
    "reaction_validation_status",
)
QUERY_COLUMNS = (
    "id",
    "property_type",
    "adsorbate",
    "reaction_step",
    "evidence_text",
    *REACTION_COLUMNS,
)
HUMAN_SOURCES = frozenset({"human", "manual"})


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only audit of deterministic DFT reaction backfill candidates."
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    parser.add_argument(
        "--sample-limit",
        type=_non_negative_int,
        default=10,
        help="Maximum record IDs retained for each status, reaction type, and reason.",
    )
    parser.add_argument(
        "--database-url",
        help="Optional SQLAlchemy database URL; defaults to LITAI_DATABASE_URL.",
    )
    return parser.parse_args(argv)


def _mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    row_mapping = getattr(row, "_mapping", None)
    if row_mapping is not None:
        return row_mapping
    return {name: getattr(row, name, None) for name in QUERY_COLUMNS}


def _candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reaction_type": row.get("reaction_type"),
        "property_type": row.get("property_type"),
        "adsorbate": row.get("adsorbate"),
        "intermediate": row.get("intermediate") or row.get("adsorbate"),
        "reaction_step": row.get("reaction_step"),
        "evidence_text": row.get("evidence_text"),
    }


def _add_sample(
    samples: dict[str, list[str]], key: str, record_id: str, sample_limit: int
) -> None:
    bucket = samples[key]
    if len(bucket) < sample_limit:
        bucket.append(record_id)


def build_report(
    rows: Iterable[Any],
    *,
    sample_limit: int = 10,
    schema_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if sample_limit < 0:
        raise ValueError("sample_limit must be zero or greater")

    counters = Counter(
        total_records=0,
        unchanged_human_labels=0,
        classifiable=0,
        ambiguous=0,
        unsupported=0,
    )
    by_reaction_type: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_property_type: Counter[str] = Counter()
    by_reaction_and_property: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)

    mapped_rows = sorted((_mapping(row) for row in rows), key=lambda row: str(row.get("id", "")))
    for row in mapped_rows:
        counters["total_records"] += 1
        record_id = str(row.get("id", ""))
        source = str(row.get("reaction_type_source") or "").strip().lower()
        existing_type = normalize_reaction_type(row.get("reaction_type"))

        if source in HUMAN_SOURCES:
            counters["unchanged_human_labels"] += 1
            by_reaction_type[existing_type] += 1
            by_reason["protected_human_label"] += 1
            _add_sample(samples, "status:unchanged_human_labels", record_id, sample_limit)
            _add_sample(samples, f"reaction_type:{existing_type}", record_id, sample_limit)
            _add_sample(samples, "reason:protected_human_label", record_id, sample_limit)
            continue

        candidate = _candidate(row)
        classification = classify_reaction_record(candidate)
        reaction_type = classification["reaction_type"]
        reason = classification["reason"]
        by_reaction_type[reaction_type] += 1

        if classification["status"] != "classified":
            counters["ambiguous"] += 1
            by_reason[reason] += 1
            _add_sample(samples, "status:ambiguous", record_id, sample_limit)
            _add_sample(samples, f"reaction_type:{reaction_type}", record_id, sample_limit)
            _add_sample(samples, f"reason:{reason}", record_id, sample_limit)
            continue

        validation = validate_reaction_record(reaction_type, candidate)
        if not validation["valid"]:
            counters["unsupported"] += 1
            validation_reasons = validation["reasons"] or ["validation_failed"]
            for validation_reason in validation_reasons:
                by_reason[validation_reason] += 1
                _add_sample(samples, f"reason:{validation_reason}", record_id, sample_limit)
            _add_sample(samples, "status:unsupported", record_id, sample_limit)
            _add_sample(samples, f"reaction_type:{reaction_type}", record_id, sample_limit)
            continue

        counters["classifiable"] += 1
        by_reason[reason] += 1
        property_type = validation.get("property_type") or "unknown"
        by_property_type[property_type] += 1
        by_reaction_and_property[f"{reaction_type}:{property_type}"] += 1
        _add_sample(samples, "status:classifiable", record_id, sample_limit)
        _add_sample(samples, f"reaction_type:{reaction_type}", record_id, sample_limit)
        _add_sample(samples, f"reason:{reason}", record_id, sample_limit)

    report: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "dry_run": True,
        "writes_performed": 0,
        **counters,
        "by_reaction_type": dict(sorted(by_reaction_type.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_property_type": dict(sorted(by_property_type.items())),
        "by_reaction_and_property": dict(sorted(by_reaction_and_property.items())),
        "sample_record_ids": dict(sorted(samples.items())),
    }
    if schema_metadata is not None:
        report["schema"] = dict(schema_metadata)
    return report


def read_rows(engine: Engine) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
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
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement).mappings()]

    schema_metadata = {
        "table": TABLE_NAME,
        "selected_columns": selected_columns,
        "reaction_columns_present": [name for name in REACTION_COLUMNS if name in available_columns],
        "reaction_columns_missing": [name for name in REACTION_COLUMNS if name not in available_columns],
    }
    return rows, schema_metadata


def audit_engine(engine: Engine, *, sample_limit: int = 10) -> dict[str, Any]:
    rows, schema_metadata = read_rows(engine)
    return build_report(rows, sample_limit=sample_limit, schema_metadata=schema_metadata)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database_url = args.database_url or get_settings().database_url
    engine = sa.create_engine(database_url, future=True)
    try:
        report = audit_engine(engine, sample_limit=args.sample_limit)
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
