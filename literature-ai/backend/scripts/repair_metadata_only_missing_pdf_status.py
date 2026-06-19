from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import AuditLog, Paper
from app.db.session import session_scope


REPAIR_ACTION = "repair_metadata_only_missing_pdf_status"


def _is_target(paper: Paper) -> bool:
    report = paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {}
    return (
        str(paper.oa_status or "").strip() == "metadata_only"
        and not str(paper.pdf_path or "").strip()
        and str(report.get("reason") or "").strip() == "missing_pdf_reference"
        and (
            str(paper.workflow_status or "").strip() == "Needs_Human_Confirmation"
            or str(paper.pdf_quality_status or "").strip() == "Broken"
            or paper.pdf_quality_report is not None
        )
    )


def find_candidates(session, *, library_name: str | None = None) -> list[Paper]:
    stmt = select(Paper)
    if library_name:
        stmt = stmt.where(Paper.library_name == library_name)
    papers = session.scalars(stmt).all()
    return [paper for paper in papers if _is_target(paper)]


def repair_candidates(session, papers: list[Paper], *, apply: bool = False) -> None:
    if not apply:
        return
    for paper in papers:
        before = {
            "workflow_status": paper.workflow_status,
            "pdf_quality_status": paper.pdf_quality_status,
            "pdf_quality_score": paper.pdf_quality_score,
            "pdf_quality_report": paper.pdf_quality_report,
        }
        paper.workflow_status = "Imported" if str(paper.workflow_status or "").strip() == "Needs_Human_Confirmation" else paper.workflow_status
        paper.pdf_quality_status = None
        paper.pdf_quality_score = None
        paper.pdf_quality_report = None
        session.add(
            AuditLog(
                paper_id=paper.id,
                action=REPAIR_ACTION,
                source="maintenance_script",
                target_type="paper",
                target_id=str(paper.id),
                payload={
                    "before": before,
                    "after": {
                        "workflow_status": paper.workflow_status,
                        "pdf_quality_status": paper.pdf_quality_status,
                        "pdf_quality_score": paper.pdf_quality_score,
                        "pdf_quality_report": paper.pdf_quality_report,
                    },
                },
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run first repair for metadata-only papers that were incorrectly marked "
            "Broken / Needs_Human_Confirmation because prepare workspace synthesized a missing_pdf_reference quality report."
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="List affected records without writing changes. This is the default.")
    action.add_argument("--apply", action="store_true", help="Apply the repair.")
    parser.add_argument("--library-name", default=None, help="Optional library filter for a narrower repair scope.")
    args = parser.parse_args()

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        papers = find_candidates(session, library_name=args.library_name)

        print(f"Metadata-only missing-PDF status dry-run: found {len(papers)} affected records.")
        for paper in papers:
            report = paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {}
            print(
                "- "
                f"paper_id={paper.id} | "
                f"paper_code={paper.paper_code or '-'} | "
                f"library={paper.library_name} | "
                f"title={paper.title or '-'} | "
                f"workflow_status={paper.workflow_status or '-'} | "
                f"pdf_quality_status={paper.pdf_quality_status or '-'} | "
                f"reason={report.get('reason') or '-'}"
            )

        if not args.apply:
            session.rollback()
            print("No changes written. Re-run with --apply to repair these records.")
            return 0

        repair_candidates(session, papers, apply=True)
        print(f"Applied metadata-only missing-PDF status repair to {len(papers)} records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
