from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper
from app.utils.library_names import build_library_name_clause, normalize_library_name


def renumber_library_papers_by_year(session: Session, library_name: str | None) -> int:
    """Assign #001 to the newest paper in a library, then old papers follow."""
    normalized_library = normalize_library_name(library_name)
    papers = list(
        session.scalars(
            select(Paper)
            .where(build_library_name_clause(Paper.library_name, normalized_library))
            .order_by(
                Paper.year.is_(None).asc(),
                Paper.year.desc(),
                Paper.created_at.desc(),
                Paper.title.is_(None).asc(),
                Paper.title.asc(),
                Paper.id.asc(),
            )
        ).all()
    )
    changed = 0
    for index, paper in enumerate(papers, start=1):
        if paper.serial_number != index:
            paper.serial_number = index
            session.add(paper)
            changed += 1
    if changed:
        session.flush()
    return changed
