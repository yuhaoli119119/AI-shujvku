from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.session import get_engine
from app.migrations.dft_audit_issue_source_backfill_v1 import (
    DFTAuditIssueSourceBackfillError,
    analyze,
    upgrade,
)


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit or apply the legacy DFT issue-source backfill")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--paper-id", type=UUID)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    engine = get_engine(get_settings().database_url)
    try:
        if args.dry_run:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    report = analyze(connection, paper_id=args.paper_id)
                    transaction.rollback()
                except BaseException:
                    if transaction.is_active:
                        transaction.rollback()
                    raise
        else:
            with engine.begin() as connection:
                report = upgrade(connection, paper_id=args.paper_id)
        _write_report(args.report, report)
        print(
            json.dumps(
                {
                    "mode": report["mode"],
                    "status": report.get("status", "validated"),
                    "paper_id": report["scope"]["paper_id"],
                    "expected_source_relations": report["expected_source_relations"],
                    "distinct_candidates": report["distinct_candidates"],
                    "error_count": report["error_count"],
                    "database_writes": report["database_writes"],
                    "report": str(args.report),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except DFTAuditIssueSourceBackfillError as exc:
        _write_report(args.report, exc.report)
        print(json.dumps({"status": "blocked", "error": str(exc), "report": str(args.report)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
