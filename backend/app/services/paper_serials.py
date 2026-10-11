from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import Paper
from app.services.paper_codes import ensure_paper_codes
from app.utils.library_names import normalize_library_name


def renumber_library_papers_by_year(session: Session, library_name: str | None) -> int:
    """Compatibility entrypoint: numbering follows intake time, never display year."""
    name = normalize_library_name(library_name)
    papers = session.scalars(select(Paper).where(Paper.library_name == name)).all()
    return len(ensure_paper_codes(session, papers))
