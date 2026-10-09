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
from app.db.session import get_db_session
from app.services.source_snapshot_reconciliation_service import (
    SourceSnapshotReconciliationError,
    SourceSnapshotReconciliationService,
)


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run or apply an auditable legacy source-snapshot reconciliation")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--discovery-run-id", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reason", default="legacy bundle manifest proves canonical source-content equivalence")
    parser.add_argument("--actor", default="source_snapshot_reconciliation")
    args = parser.parse_args(argv)

    session = next(get_db_session())
    try:
        service = SourceSnapshotReconciliationService(session, get_settings())
        report = service.dry_run(
            paper_id=UUID(args.paper_id),
            discovery_run_id=UUID(args.discovery_run_id),
            archive_path=args.archive,
        )
        report["mode"] = "apply" if args.apply else "dry_run"
        report["database_writes"] = 0
        if args.apply:
            result = service.reconcile(dry_run=report, reason=args.reason, actor=args.actor)
            report["apply_result"] = result
            report["database_writes"] = result["database_writes"]
            session.commit()
        else:
            session.rollback()
        _write_report(args.report, report)
        print(
            json.dumps(
                {
                    "mode": report["mode"],
                    "equivalent": report["comparison"]["equivalent"],
                    "database_writes": report["database_writes"],
                    "historical_fingerprint": report["historical_fingerprint"],
                    "current_fingerprint": report["current_fingerprint"],
                    "report": str(args.report),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (SourceSnapshotReconciliationError, ValueError) as exc:
        session.rollback()
        report = {"mode": "apply" if args.apply else "dry_run", "error": str(exc), "database_writes": 0}
        _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
