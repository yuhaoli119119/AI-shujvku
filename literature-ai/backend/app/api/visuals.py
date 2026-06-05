from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    FigureDataPoint,
    Paper,
    PaperFigure,
    PaperSection,
    WorkflowJob,
)
from app.db.session import get_db_session
from app.utils.library_names import build_library_name_clause, normalize_library_name

router = APIRouter()


def _paper_filters(library_name: str | None) -> list[Any]:
    if not library_name:
        return []
    return [build_library_name_clause(Paper.library_name, normalize_library_name(library_name))]


def _count(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def _paper_count(session: Session, library_name: str | None) -> int:
    stmt = select(func.count(Paper.id))
    for clause in _paper_filters(library_name):
        stmt = stmt.where(clause)
    return _count(session, stmt)


def _joined_count(session: Session, model, library_name: str | None) -> int:
    stmt = select(func.count(model.id)).join(Paper, model.paper_id == Paper.id)
    for clause in _paper_filters(library_name):
        stmt = stmt.where(clause)
    return _count(session, stmt)


@router.get("/overview")
async def visualization_overview(
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    filters = _paper_filters(library_name)

    total_papers = _paper_count(session, library_name)
    pdf_stmt = select(func.count(Paper.id)).where(Paper.pdf_path.is_not(None), Paper.pdf_path != "")
    parsed_stmt = (
        select(func.count(func.distinct(PaperSection.paper_id)))
        .join(Paper, PaperSection.paper_id == Paper.id)
    )
    for clause in filters:
        pdf_stmt = pdf_stmt.where(clause)
        parsed_stmt = parsed_stmt.where(clause)

    years = []
    year_stmt = select(Paper.year, func.count(Paper.id)).group_by(Paper.year).order_by(Paper.year.desc())
    for clause in filters:
        year_stmt = year_stmt.where(clause)
    for year, count in session.execute(year_stmt).all():
        years.append({"year": year, "count": int(count or 0)})

    journals = []
    journal_stmt = (
        select(Paper.journal, func.count(Paper.id))
        .group_by(Paper.journal)
        .order_by(func.count(Paper.id).desc())
        .limit(12)
    )
    for clause in filters:
        journal_stmt = journal_stmt.where(clause)
    for journal, count in session.execute(journal_stmt).all():
        journals.append({"journal": journal or "未记录期刊", "count": int(count or 0)})

    type_counts: Counter[str] = Counter()
    type_stmt = select(Paper.paper_type, func.count(Paper.id)).group_by(Paper.paper_type)
    for clause in filters:
        type_stmt = type_stmt.where(clause)
    for paper_type, count in session.execute(type_stmt).all():
        key = str(paper_type or "Unknown").strip() or "Unknown"
        type_counts[key] += int(count or 0)

    matrix_rows = []
    matrix_stmt = (
        select(
            DFTResult.property_type,
            DFTResult.adsorbate,
            func.count(DFTResult.id),
            func.avg(DFTResult.confidence),
        )
        .join(Paper, DFTResult.paper_id == Paper.id)
        .group_by(DFTResult.property_type, DFTResult.adsorbate)
        .order_by(func.count(DFTResult.id).desc())
        .limit(200)
    )
    for clause in filters:
        matrix_stmt = matrix_stmt.where(clause)
    for property_type, adsorbate, count, avg_confidence in session.execute(matrix_stmt).all():
        matrix_rows.append(
            {
                "property_type": property_type or "未标注属性",
                "adsorbate": adsorbate or "未标注物种",
                "count": int(count or 0),
                "avg_confidence": round(float(avg_confidence or 0), 3),
            }
        )

    dft_status = []
    status_stmt = (
        select(DFTResult.candidate_status, func.count(DFTResult.id))
        .join(Paper, DFTResult.paper_id == Paper.id)
        .group_by(DFTResult.candidate_status)
        .order_by(func.count(DFTResult.id).desc())
    )
    for clause in filters:
        status_stmt = status_stmt.where(clause)
    for status, count in session.execute(status_stmt).all():
        dft_status.append({"status": status or "unknown", "count": int(count or 0)})

    tasks = []
    task_stmt = select(WorkflowJob).order_by(WorkflowJob.created_at.desc()).limit(12)
    if library_name:
        task_stmt = task_stmt.where(build_library_name_clause(WorkflowJob.library_name, library_name))
    for job in session.scalars(task_stmt).all():
        tasks.append(
            {
                "job_id": job.job_id,
                "type": job.type,
                "status": job.status,
                "title": (job.payload or {}).get("title") if isinstance(job.payload, dict) else None,
                "action": (job.payload or {}).get("action") if isinstance(job.payload, dict) else None,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
        )

    return {
        "library_name": normalize_library_name(library_name) if library_name else None,
        "summary": {
            "papers": total_papers,
            "pdf_available": _count(session, pdf_stmt),
            "parsed_papers": _count(session, parsed_stmt),
            "figures": _joined_count(session, PaperFigure, library_name),
            "figure_data_points": _joined_count(session, FigureDataPoint, library_name),
            "dft_settings": _joined_count(session, DFTSetting, library_name),
            "catalyst_samples": _joined_count(session, CatalystSample, library_name),
            "dft_results": _joined_count(session, DFTResult, library_name),
        },
        "years": years,
        "journals": journals,
        "paper_types": [{"type": key, "count": value} for key, value in sorted(type_counts.items())],
        "dft_matrix": matrix_rows,
        "dft_status": dft_status,
        "recent_tasks": tasks,
    }
