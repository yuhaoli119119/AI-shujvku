from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import PaperFigure
from app.db.session import session_scope
from app.utils.figure_filtering import decorative_figure_reason


DecorativeFigureRow = tuple[PaperFigure, str]


def find_decorative_figures(session: Session, paper_id: uuid.UUID | None = None) -> list[DecorativeFigureRow]:
    query = select(PaperFigure)
    if paper_id is not None:
        query = query.where(PaperFigure.paper_id == paper_id)

    rows: list[DecorativeFigureRow] = []
    for figure in session.scalars(query).all():
        reason = decorative_figure_reason(figure.caption, figure.prov)
        if reason:
            rows.append((figure, reason))
    return rows


def repair_decorative_figures(
    session: Session,
    *,
    apply: bool = False,
    paper_id: uuid.UUID | None = None,
) -> list[DecorativeFigureRow]:
    rows = find_decorative_figures(session, paper_id=paper_id)
    if apply:
        for figure, _reason in rows:
            session.delete(figure)
    return rows


def parse_paper_id(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    return uuid.UUID(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run first cleanup for existing PaperFigure rows that match the shared "
            "decorative figure filter. Default mode is --dry-run."
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="List candidate PaperFigure rows without writing changes. Default.")
    action.add_argument("--apply", action="store_true", help="Delete matching PaperFigure database rows. Does not delete files.")
    parser.add_argument("--paper-id", help="Optional paper UUID to limit the scan.")
    args = parser.parse_args()

    paper_id = parse_paper_id(args.paper_id)
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        rows = find_decorative_figures(session, paper_id=paper_id)
        print(f"Decorative figure dry-run: found {len(rows)} matching PaperFigure rows.")
        for figure, reason in rows:
            print(
                "- "
                f"paper_id={figure.paper_id} | "
                f"figure_id={figure.id} | "
                f"caption={figure.caption!r} | "
                f"image_path={figure.image_path or '-'} | "
                f"reason={reason}"
            )

        if not args.apply:
            session.rollback()
            print("No changes written. Re-run with --apply to delete matching PaperFigure database rows.")
            return 0

        for figure, _reason in rows:
            session.delete(figure)
        print(f"Applied decorative figure cleanup to {len(rows)} database rows. Image files were not deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
