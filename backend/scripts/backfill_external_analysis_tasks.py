from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.session import session_scope
from app.services.task_log_service import TaskLogService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report or backfill one agent_activity task for each candidate-bearing external-analysis run."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="Report missing tasks without writing.")
    action.add_argument("--apply", action="store_true", help="Create missing task rows.")
    parser.add_argument("--paper-code", help="Limit the scan to one Paper.paper_code.")
    parser.add_argument("--paper-id", help="Limit the scan to one paper UUID.")
    args = parser.parse_args()

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        report = TaskLogService(session).backfill_missing_external_analysis_tasks(
            paper_code=args.paper_code,
            paper_id=args.paper_id,
            apply=bool(args.apply),
        )
        if not args.apply:
            session.rollback()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
