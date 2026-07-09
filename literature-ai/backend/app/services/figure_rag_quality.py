from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import PaperFigure
from app.rag.quality import build_rag_quality_summary


def build_figure_rag_quality_summary(
    session: Session,
    figures: list[PaperFigure],
) -> dict[str, Any]:
    summary = build_rag_quality_summary(
        session,
        figures=figures,
        dft_results=[],
        writing_cards=[],
    )["figures"]
    return {
        **summary,
        "status": "ready" if int(summary.get("blocked") or 0) == 0 else "blocked",
    }
