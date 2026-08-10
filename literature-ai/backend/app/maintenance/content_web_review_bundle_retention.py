from __future__ import annotations

import argparse
import json
import os
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.content_web_review_bundle_retention_service import (
    ContentWebReviewBundleRetentionService,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely clean unused content web-review v2 bundles"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="apply protected cleanup rules")
    parser.add_argument("--paper-id")
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--database-url", default=os.getenv("LITAI_DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or LITAI_DATABASE_URL is required")
    try:
        paper_id = UUID(args.paper_id) if args.paper_id else None
    except ValueError as exc:
        parser.error(f"--paper-id must be a UUID: {exc}")

    engine = create_engine(args.database_url, future=True)
    try:
        with Session(engine) as session:
            report = ContentWebReviewBundleRetentionService(session).cleanup(
                paper_id=paper_id,
                older_than_days=args.older_than_days,
                limit=args.limit,
                dry_run=not args.apply,
            )
            if args.apply:
                session.commit()
            else:
                session.rollback()
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
