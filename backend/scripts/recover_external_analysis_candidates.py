from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db.session import get_engine
from app.services.external_analysis_candidate_recovery_service import (
    ExternalAnalysisCandidateRecoveryError,
    ExternalAnalysisCandidateRecoveryService,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply audited recovery of referenced external-analysis candidate UUIDs."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--reconcile-existing-dry-run", action="store_true")
    action.add_argument("--reconcile-existing-apply", action="store_true")
    parser.add_argument("--paper-id", action="append", required=True)
    parser.add_argument("--actor", default="candidate_recovery_v1")
    parser.add_argument(
        "--reason",
        default="restore candidate UUIDs deleted before reference retention was enforced",
    )
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    paper_ids = [UUID(value) for value in args.paper_id]

    settings = get_settings()
    engine = get_engine(settings.database_url)
    report: dict
    exit_code = 0
    with Session(bind=engine, autoflush=False, expire_on_commit=False) as session:
        service = ExternalAnalysisCandidateRecoveryService(session)
        try:
            if args.dry_run:
                report = service.public_analyze(paper_ids)
                session.rollback()
            elif args.reconcile_existing_dry_run:
                report = service.analyze_existing_states(paper_ids)
                session.rollback()
            elif args.reconcile_existing_apply:
                report = service.reconcile_existing_states(
                    paper_ids,
                    actor=args.actor,
                    reason=args.reason,
                )
                session.commit()
            else:
                report = service.apply(
                    paper_ids,
                    actor=args.actor,
                    reason=args.reason,
                )
                session.commit()
        except ExternalAnalysisCandidateRecoveryError as exc:
            session.rollback()
            report = exc.report
            exit_code = 2
        except Exception:
            session.rollback()
            raise

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.report:
        Path(args.report).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
