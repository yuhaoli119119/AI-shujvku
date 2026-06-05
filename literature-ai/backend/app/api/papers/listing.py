from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Paper
from app.db.session import get_db_session, session_scope
from app.schemas.api import (
    ExtractionRunResponse,
    PaperDetailResponse,
    PaperLibraryResponse,
    PaperListFilterParams,
    PaperListItemResponse,
)
from app.services.paper_query import PaperQueryService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.workflow_jobs import DEFAULT_LIBRARY_NAME
from app.utils.library_names import build_library_name_clause, normalize_library_name

router = APIRouter()


@router.get("/", response_model=list[PaperListItemResponse])
async def list_papers(
    q: str | None = Query(default=None, description="Keyword search across title, DOI, journal, abstract, authors, and sections"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    source_path: str | None = Query(default=None, description="Exact local source path used during ingest/path"),
    year: int | None = Query(default=None, description="Filter by publication year"),
    journal: str | None = Query(default=None, description="Filter by journal name (fuzzy)"),
    has_dft_results: bool | None = Query(default=None, description="Only papers with/without DFT results"),
    has_writing_cards: bool | None = Query(default=None, description="Only papers with/without writing cards"),
    sort_by: str = Query(default="year_serial", description="Sort papers by year+serial, created_at, or title"),
    sort_order: str = Query(default="desc", description="Sort direction: asc or desc"),
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    offset: int = Query(default=0, ge=0, description="Skip N items"),
    session: Session = Depends(get_db_session),
) -> list[PaperListItemResponse]:
    filters = PaperListFilterParams(
        q=q,
        library_name=normalize_library_name(library_name) if library_name is not None else None,
        source_path=source_path,
        year=year,
        journal=journal,
        has_dft_results=has_dft_results,
        has_writing_cards=has_writing_cards,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    return PaperQueryService(session).list_papers(filters=filters)


@router.get("/libraries", response_model=list[PaperLibraryResponse])
async def list_libraries(session: Session = Depends(get_db_session)) -> list[PaperLibraryResponse]:
    rows = session.execute(
        select(Paper.library_name, func.count(Paper.id))
        .group_by(Paper.library_name)
        .order_by(Paper.library_name.asc())
    ).all()
    totals: dict[str, int] = {}
    for name, count in rows:
        normalized = normalize_library_name(name)
        totals[normalized] = totals.get(normalized, 0) + int(count or 0)
    libraries = [
        PaperLibraryResponse(name=name, paper_count=count)
        for name, count in sorted(totals.items(), key=lambda item: item[0])
    ]
    if not any(item.name == DEFAULT_LIBRARY_NAME for item in libraries):
        libraries.insert(0, PaperLibraryResponse(name=DEFAULT_LIBRARY_NAME, paper_count=0))
    return libraries


@router.get("/type-stats")
async def get_paper_type_stats(
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    session: Session = Depends(get_db_session),
) -> dict:
    labels = {
        "A": {"label": "A类 (Core)", "color": "#6366f1"},
        "B": {"label": "B类 (Related)", "color": "#14b8a6"},
        "C": {"label": "C类 (Background)", "color": "#f59e0b"},
        "uncategorized": {"label": "未分类 (Uncategorized)", "color": "#9ca3af"},
    }
    counts = {key: 0 for key in labels}
    stmt = select(Paper.paper_type, func.count(Paper.id)).group_by(Paper.paper_type)
    if library_name is not None:
        stmt = stmt.where(build_library_name_clause(Paper.library_name, library_name))

    for raw_type, count in session.execute(stmt).all():
        normalized = str(raw_type or "").strip().upper()
        key = normalized if normalized in {"A", "B", "C"} else "uncategorized"
        counts[key] += int(count or 0)

    total = sum(counts.values())
    items = []
    for key in ("A", "B", "C", "uncategorized"):
        count = counts[key]
        items.append(
            {
                "key": key,
                "label": labels[key]["label"],
                "color": labels[key]["color"],
                "count": count,
                "percent": round((count / total) * 100, 2) if total else 0,
            }
        )
    return {
        "library_name": normalize_library_name(library_name) if library_name is not None else None,
        "total": total,
        "items": items,
    }


@router.delete("/libraries/{library_name}")
async def delete_library(library_name: str, session: Session = Depends(get_db_session)) -> dict:
    target = normalize_library_name(library_name)
    if target == DEFAULT_LIBRARY_NAME:
        raise HTTPException(status_code=400, detail="Cannot delete the default library")
    from sqlalchemy import delete as sa_delete

    result = session.execute(sa_delete(Paper).where(build_library_name_clause(Paper.library_name, target)))
    session.commit()
    return {"status": "deleted", "library_name": target, "deleted_count": result.rowcount}


@router.get("/stream")
async def stream_papers(
    request: Request,
    q: str | None = Query(default=None),
    library_name: str | None = Query(default=None),
    year: int | None = Query(default=None),
    journal: str | None = Query(default=None),
    has_dft_results: bool | None = Query(default=None),
    has_writing_cards: bool | None = Query(default=None),
    sort_by: str = Query(default="year_serial"),
    sort_order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    settings = get_settings()

    def fetch_updated_papers():
        with session_scope(settings.database_url) as poll_session:
            filters = PaperListFilterParams(
                q=q,
                library_name=normalize_library_name(library_name) if library_name is not None else None,
                year=year,
                journal=journal,
                has_dft_results=has_dft_results,
                has_writing_cards=has_writing_cards,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )
            papers = PaperQueryService(poll_session).list_papers(filters=filters)
            total_stmt = select(func.count(Paper.id))
            if library_name is not None:
                total_stmt = total_stmt.where(build_library_name_clause(Paper.library_name, library_name))
            total_papers = poll_session.scalar(total_stmt) or 0
            return [paper.model_dump(mode="json") for paper in papers], total_papers

    async def event_generator():
        last_count = -1
        error_count = 0
        while True:
            try:
                if await request.is_disconnected():
                    break

                papers, total_papers = await run_in_threadpool(fetch_updated_papers)
                error_count = 0
                current_count = len(papers)
                if current_count != last_count:
                    last_count = current_count
                    yield f"event: papers_update\ndata: {json.dumps(papers)}\n\n"
                yield f"event: heartbeat\ndata: {json.dumps({'total': total_papers, 'displayed': current_count})}\n\n"
                await asyncio.sleep(3)
            except Exception as exc:
                logging.getLogger(__name__).error("Error in SSE papers stream: %s", exc, exc_info=True)
                yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n"
                error_count += 1
                await asyncio.sleep(min(3 * (2 ** (error_count - 1)), 30))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
async def get_papers_status(session: Session = Depends(get_db_session)):
    total = session.scalar(select(func.count(Paper.id))) or 0
    last_paper = session.scalars(select(Paper).order_by(Paper.created_at.desc()).limit(1)).first()
    return {
        "total": total,
        "last_added": {
            "id": str(last_paper.id) if last_paper else None,
            "title": last_paper.title if last_paper else None,
            "created_at": last_paper.created_at.isoformat() if last_paper and last_paper.created_at else None,
        }
        if last_paper
        else None,
    }
