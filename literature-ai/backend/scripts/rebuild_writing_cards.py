"""One-time, rollback-safe rebuild of evidence-grounded WritingCards."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import Paper
from app.db.session import get_engine
from app.services.paper_reprocessing import PaperReprocessingService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-id", action="append", default=[], help="Limit rebuilding to one or more paper UUIDs")
    args = parser.parse_args()
    settings = get_settings()
    engine = get_engine(settings.database_url)
    requested = [UUID(value) for value in args.paper_id]
    reason_counts: Counter[str] = Counter()
    rows: list[dict] = []

    with Session(engine) as session:
        stmt = select(Paper.id).order_by(Paper.created_at.asc())
        if requested:
            stmt = stmt.where(Paper.id.in_(requested))
        paper_ids = list(session.scalars(stmt).all())
        service = PaperReprocessingService(session, settings)
        for paper_id in paper_ids:
            try:
                row = service.rebuild_writing_card(paper_id)
            except Exception as exc:
                session.rollback()
                row = {"paper_id": str(paper_id), "status": "failed", "error": str(exc)}
            rows.append(row)
            reason_counts.update(row.get("blocked_reasons") or [])

    report = {
        "requested": len(paper_ids),
        "completed": sum(row["status"] == "completed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "rag_eligible": sum(bool(row.get("rag_eligible")) for row in rows),
        "blocked_reasons": dict(sorted(reason_counts.items())),
        "rows": rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
