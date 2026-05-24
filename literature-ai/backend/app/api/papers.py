import csv
import io
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import get_db_session, session_scope
from app.schemas.api import (
    AIWorkflowPayload,
    AIWorkflowResponse,
    AISearchPayload,
    AISearchResponse,
    AIWorkflowFailedItemResponse,
    AIWorkflowIngestedPaperResponse,
    DiscoveryDownloadRequest,
    DiscoverySearchResponse,
    ExtractionRunResponse,
    IngestFromPathRequest,
    IngestResponse,
    PaperDetailResponse,
    PaperLibraryResponse,
    PaperListFilterParams,
    PaperListItemResponse,
    ClassifyBatchPayload,
)

from app.services.discovery_service import DiscoveryService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_query import PaperQueryService
from app.services.paper_reprocessing import PaperReprocessingService

router = APIRouter()

DEFAULT_LIBRARY_NAME = "\u9ed8\u8ba4\u6587\u732e\u5e93"
AI_WORKFLOW_JOBS: dict[str, dict[str, Any]] = {}
AI_WORKFLOW_JOBS_LOCK = threading.Lock()
MAX_AI_WORKFLOW_JOBS = 50


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remember_ai_workflow_job(job_id: str, **updates: Any) -> dict[str, Any]:
    with AI_WORKFLOW_JOBS_LOCK:
        job = AI_WORKFLOW_JOBS.setdefault(
            job_id,
            {
                "job_id": job_id,
                "status": "queued",
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
                "progress": {},
                "result": None,
                "error": None,
            },
        )
        job.update(updates)
        job["updated_at"] = _utc_now_iso()
        if len(AI_WORKFLOW_JOBS) > MAX_AI_WORKFLOW_JOBS:
            oldest = sorted(AI_WORKFLOW_JOBS.values(), key=lambda item: item["created_at"])
            for item in oldest[: len(AI_WORKFLOW_JOBS) - MAX_AI_WORKFLOW_JOBS]:
                AI_WORKFLOW_JOBS.pop(item["job_id"], None)
        return dict(job)


async def _run_ai_workflow_job(job_id: str, payload: AIWorkflowPayload, database_url: str) -> None:
    _remember_ai_workflow_job(
        job_id,
        status="running",
        progress={
            "phase": "search_and_ingest",
            "message": "AI workflow is searching, deduplicating, downloading, and metadata-ingesting failures.",
            "max_results": payload.max_results,
            "max_downloads": payload.max_downloads,
        },
    )
    try:
        with session_scope(database_url) as session:
            result = await ai_workflow(payload=payload, session=session, settings=get_settings())
        _remember_ai_workflow_job(
            job_id,
            status="completed",
            progress={
                "phase": "completed",
                "searched_total": result.searched_total,
                "attempted_downloads": result.attempted_downloads,
                "ingested": len(result.ingested),
                "failed": len(result.failed),
            },
            result=result.model_dump(mode="json"),
            error=None,
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("AI workflow job failed: %s", job_id)
        _remember_ai_workflow_job(
            job_id,
            status="failed",
            progress={"phase": "failed"},
            error=f"{type(exc).__name__}: {exc}",
        )

def _normalize_library_name(library_name: str | None) -> str:
    return (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME


def _build_chat_completions_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"


def _find_existing_paper(session: Session, doi: str | None, title: str | None) -> Paper | None:
    if doi:
        existing = session.scalar(select(Paper).where(Paper.doi == doi))
        if existing:
            return existing
    if title:
        return session.scalar(select(Paper).where(Paper.title == title))
    return None


async def _download_discovery_candidate(
    service: DiscoveryService,
    raw_paper,
    metadata: dict[str, object],
    dest_dir: Path,
) -> Path:
    try:
        return await run_in_threadpool(service.download_pdf, raw_paper, dest_dir)
    except Exception as primary_exc:
        pdf_url = metadata.get("pdf_url") or metadata.get("oa_url") or metadata.get("url")
        if not pdf_url:
            raise primary_exc
        filename = f"{uuid4()}.pdf"
        try:
            return await run_in_threadpool(service.download_pdf_url, str(pdf_url), dest_dir, filename)
        except Exception:
            raise primary_exc


def _rewrite_ai_search_query(query: str, model: str, settings: Settings) -> tuple[str, str | None, str | None, dict]:
    diagnostics: dict[str, object] = {
        "mode": "raw_query",
        "requested_model": model,
    }
    api_base = (settings.writer_api_base or "").strip()
    api_key = (settings.writer_api_key or "").strip()
    if not api_base or not api_key:
        missing = []
        if not api_base:
            missing.append("writer_api_base")
        if not api_key:
            missing.append("writer_api_key")
        diagnostics["mode"] = "missing_configuration"
        diagnostics["missing_configuration"] = missing
        diagnostics["request_url"] = _build_chat_completions_url(api_base) if api_base else None
        return query, "missing_configuration", None, diagnostics

    try:
        import httpx

        request_url = _build_chat_completions_url(api_base)
        diagnostics["request_url"] = request_url
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert scientific literature search assistant. "
                    "Convert the user's natural language request into a precise academic search query. "
                    "Return only the rewritten query string without quotes or explanation."
                ),
            },
            {"role": "user", "content": query},
        ]
        with httpx.Client(timeout=settings.writer_timeout_seconds) as client:
            response = client.post(
                request_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model.strip() or settings.writer_model,
                    "messages": messages,
                    "temperature": 0.1,
                },
            )
        response.raise_for_status()
        data = response.json()
        rewritten = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        rewritten = rewritten.strip() if isinstance(rewritten, str) else ""
        if not rewritten:
            diagnostics["mode"] = "empty_response_fallback"
            return query, "fallback:empty_response", "LLM returned empty content", diagnostics
        diagnostics["mode"] = "live_llm"
        diagnostics["message_count"] = len(messages)
        return rewritten, "ok", None, diagnostics
    except Exception as exc:
        diagnostics["mode"] = "fallback"
        diagnostics["fallback_reason"] = type(exc).__name__
        logging.getLogger(__name__).warning("AI search query rewrite failed: %s", exc)
        return query, f"fallback:{type(exc).__name__}", str(exc), diagnostics


@router.post("/ingest/path", response_model=IngestResponse)
async def ingest_from_path(
    payload: IngestFromPathRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    source_path = Path(payload.pdf_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="PDF path does not exist")

    external_meta = None
    if any([payload.title, payload.doi, payload.authors, payload.year, payload.journal, payload.abstract]):
        external_meta = {
            "title": payload.title,
            "doi": payload.doi,
            "authors": payload.authors,
            "year": payload.year,
            "journal": payload.journal,
            "abstract": payload.abstract,
        }

    service = PaperIngestionService(session=session, settings=settings)
    paper = await service.ingest_pdf(
        source_path=source_path,
        original_filename=source_path.name,
        external_metadata=external_meta,
        source_reference=str(source_path.resolve()),
        library_name=_normalize_library_name(payload.library_name),
    )
    return IngestResponse(paper_id=paper.id, title=paper.title, status="completed")


@router.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
    if file.size and file.size > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 30MB.")

    external_metadata = None
    if identifier:
        from app.services.discovery_service import DiscoveryService
        try:
            svc = DiscoveryService()
            _, external_metadata = await run_in_threadpool(svc.fetch_metadata, identifier)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to fetch metadata for {identifier}: {e}")

    service = PaperIngestionService(session=session, settings=settings)
    paper = await service.ingest_upload(
        file=file,
        external_metadata=external_metadata,
        library_name=_normalize_library_name(library_name),
    )
    return IngestResponse(paper_id=paper.id, title=paper.title, status="completed")


@router.get("", response_model=list[PaperListItemResponse])
async def list_papers(
    q: str | None = Query(default=None, description="Keyword search across title, DOI, journal, abstract, authors, and sections"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    source_path: str | None = Query(default=None, description="Exact local source path used during ingest/path"),
    year: int | None = Query(default=None, description="Filter by publication year"),
    journal: str | None = Query(default=None, description="Filter by journal name (fuzzy)"),
    has_dft_results: bool | None = Query(default=None, description="Only papers with/without DFT results"),
    has_writing_cards: bool | None = Query(default=None, description="Only papers with/without writing cards"),
    limit: int = Query(default=50, ge=1, le=200, description="Page size"),
    offset: int = Query(default=0, ge=0, description="Skip N items"),
    session: Session = Depends(get_db_session),
) -> list[PaperListItemResponse]:
    filters = PaperListFilterParams(
        q=q,
        library_name=_normalize_library_name(library_name) if library_name is not None else None,
        source_path=source_path,
        year=year,
        journal=journal,
        has_dft_results=has_dft_results,
        has_writing_cards=has_writing_cards,
        limit=limit,
        offset=offset,
    )
    return PaperQueryService(session).list_papers(filters=filters)


@router.get("/libraries", response_model=list[PaperLibraryResponse])
async def list_libraries(
    session: Session = Depends(get_db_session),
) -> list[PaperLibraryResponse]:
    rows = session.execute(
        select(Paper.library_name, func.count(Paper.id))
        .group_by(Paper.library_name)
        .order_by(Paper.library_name.asc())
    ).all()
    libraries = [
        PaperLibraryResponse(name=_normalize_library_name(name), paper_count=count)
        for name, count in rows
    ]
    if not any(item.name == DEFAULT_LIBRARY_NAME for item in libraries):
        libraries.insert(0, PaperLibraryResponse(name=DEFAULT_LIBRARY_NAME, paper_count=0))
    return libraries


@router.delete("/libraries/{library_name}")
async def delete_library(
    library_name: str,
    session: Session = Depends(get_db_session),
) -> dict:
    target = _normalize_library_name(library_name)
    if target == DEFAULT_LIBRARY_NAME:
        raise HTTPException(status_code=400, detail="Cannot delete the default library")
    from sqlalchemy import delete as sa_delete

    result = session.execute(sa_delete(Paper).where(Paper.library_name == target))
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
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """SSE endpoint for real-time paper list updates.
    
    Sends initial paper list, then sends updates whenever papers are added/modified.
    Clients should reconnect if connection drops.
    """
    import asyncio
    import json
    import logging
    from app.services.paper_query import PaperQueryService
    from app.db.session import session_scope
    from app.config import get_settings

    settings = get_settings()

    def fetch_updated_papers():
        with session_scope(settings.database_url) as poll_session:
            filters = PaperListFilterParams(
                q=q,
                library_name=_normalize_library_name(library_name) if library_name is not None else None,
                year=year,
                journal=journal,
                has_dft_results=has_dft_results,
                has_writing_cards=has_writing_cards,
                limit=limit,
                offset=offset,
            )
            papers = PaperQueryService(poll_session).list_papers(filters=filters)
            from app.db.models import Paper
            from sqlalchemy import func
            total_stmt = select(func.count(Paper.id))
            if library_name is not None:
                total_stmt = total_stmt.where(Paper.library_name == _normalize_library_name(library_name))
            total_papers = poll_session.scalar(total_stmt) or 0
            serialized_papers = [p.model_dump(mode="json") for p in papers]
            return serialized_papers, total_papers

    async def event_generator():
        last_count = -1
        error_count = 0
        while True:
            try:
                # Early break if client disconnected
                if await request.is_disconnected():
                    break

                # Run the blocking database calls in a threadpool to prevent event loop blocking
                papers, total_papers = await run_in_threadpool(fetch_updated_papers)
                error_count = 0  # Reset error count on success
                
                # Only send if data changed
                current_count = len(papers)
                if current_count != last_count:
                    last_count = current_count
                    data = json.dumps(papers)
                    yield f"event: papers_update\ndata: {data}\n\n"
                
                # Also send ingestion status periodically
                heartbeat_data = json.dumps({"total": total_papers, "displayed": current_count})
                yield f"event: heartbeat\ndata: {heartbeat_data}\n\n"
                
                await asyncio.sleep(3)  # Poll every 3 seconds
                
            except Exception as e:
                logging.getLogger(__name__).error(f"Error in SSE papers stream: {e}", exc_info=True)
                error_data = json.dumps({"message": str(e)})
                yield f"event: error\ndata: {error_data}\n\n"
                
                error_count += 1
                # Exponential backoff: 3s, 6s, 12s, 24s, capped at 30s
                sleep_time = min(3 * (2 ** (error_count - 1)), 30)
                await asyncio.sleep(sleep_time)
    
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@router.get("/status")
async def get_papers_status(
    session: Session = Depends(get_db_session),
):
    """Get current papers status for polling."""
    from app.db.models import Paper
    
    from sqlalchemy import func
    total = session.scalar(select(func.count(Paper.id))) or 0
    
    # Get last added paper
    last_paper = session.scalars(
        select(Paper).order_by(Paper.created_at.desc()).limit(1)
    ).first()
    
    return {
        "total": total,
        "last_added": {
            "id": str(last_paper.id) if last_paper else None,
            "title": last_paper.title if last_paper else None,
            "created_at": last_paper.created_at.isoformat() if last_paper and last_paper.created_at else None,
        } if last_paper else None
    }


@router.get("/discovery/search", response_model=DiscoverySearchResponse)
async def discovery_search(
    q: str = Query(..., min_length=2, description="Keyword query for external literature search"),
    providers: list[str] = Query(default=["openalex", "arxiv"], description="External search providers"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum number of returned results"),
) -> DiscoverySearchResponse:
    service = DiscoveryService()
    items = service.search(query=q, providers=providers, limit=limit)
    return DiscoverySearchResponse(query=q, providers=providers, total=len(items), items=items)


@router.post("/discovery/download", response_model=IngestResponse)
async def discovery_download_and_ingest(
    payload: DiscoveryDownloadRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    service = DiscoveryService()
    
    # Fetch metadata asynchronously
    raw_paper, metadata = await run_in_threadpool(
        service.fetch_metadata, payload.identifier, payload.providers
    )

    target_library = _normalize_library_name(payload.library_name)
    doi = metadata.get("doi")
    existing = _find_existing_paper(session, doi=doi, title=metadata.get("title"))
    if existing:
        if existing.library_name != target_library:
            existing.library_name = target_library
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return IngestResponse(paper_id=existing.id, title=existing.title, status="already_exists")

    ingestion = PaperIngestionService(session=session, settings=settings)
    
    # Download PDF safely within a temp directory
    downloaded_pdf_name: str | None = None
    try:
        with TemporaryDirectory() as tmpdir:
            dest_dir = Path(tmpdir)
            pdf_path = await _download_discovery_candidate(service, raw_paper, metadata, dest_dir)
            downloaded_pdf_name = pdf_path.name
            
            paper = await ingestion.ingest_pdf(
                source_path=pdf_path,
                original_filename=pdf_path.name,
                copy_pdf=True,
                external_metadata=metadata,
                source_reference=None,
                library_name=target_library,
            )
        response_status = "completed"
    except Exception:
        paper = ingestion.ingest_metadata_only(
            external_metadata=metadata,
            identifier=payload.identifier,
            library_name=target_library,
            source_reference=metadata.get("url") or payload.identifier,
        )
        response_status = "metadata_only"

    updated = False
    if doi and paper.doi != doi:
        paper.doi = doi
        updated = True
    if metadata.get("title") and (not paper.title or (downloaded_pdf_name and paper.title == downloaded_pdf_name)):
        paper.title = metadata["title"]
        updated = True
    if metadata.get("year") and not paper.year:
        paper.year = metadata["year"]
        updated = True
    if metadata.get("journal") and not paper.journal:
        paper.journal = metadata["journal"]
        updated = True
    if metadata.get("authors") and not paper.authors:
        paper.authors = metadata["authors"]
        updated = True
    if metadata.get("abstract") and not paper.abstract:
        paper.abstract = metadata["abstract"]
        updated = True
    if updated:
        session.add(paper)
        session.commit()
        session.refresh(paper)

    return IngestResponse(paper_id=paper.id, title=paper.title, status=response_status)



# ---------------------------------------------------------------------------
# /export/csv 闂?Download all DFT results as a structured CSV file
# ---------------------------------------------------------------------------

@router.get("/export/csv")
async def export_dft_results_csv(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    session: Session = Depends(get_db_session),
):
    """Export all DFT results (optionally filtered) as a downloadable CSV file.
    
    Each row is one data point, with paper metadata columns for database building.
    """
    from app.db.models import DFTResult as DR, Paper as P, CatalystSample as CS

    # Join DFTResult 闂?Paper (and optionally CatalystSample)
    stmt = (
        select(DR, P)
        .join(P, DR.paper_id == P.id)
        .order_by(P.year.desc().nulls_last(), P.title)
    )
    if property_type:
        stmt = stmt.where(DR.property_type.ilike(f"%{property_type}%"))
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)

    rows = session.execute(stmt).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "paper_id", "title", "doi", "journal", "year", "authors",
        "property_type", "adsorbate", "value", "unit", "reaction_step",
        "source_section", "source_figure", "confidence", "evidence_text",
    ])
    for dr, paper in rows:
        authors_str = ", ".join(paper.authors) if isinstance(paper.authors, list) else (paper.authors or "")
        writer.writerow([
            str(paper.id),
            paper.title or "",
            paper.doi or "",
            paper.journal or "",
            paper.year or "",
            authors_str,
            dr.property_type or "",
            dr.adsorbate or "",
            dr.value if dr.value is not None else "",
            dr.unit or "",
            dr.reaction_step or "",
            dr.source_section or "",
            dr.source_figure or "",
            dr.confidence if dr.confidence is not None else "",
            (dr.evidence_text or "").replace("\n", " "),
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=dft_results_export.csv"},
    )


# ---------------------------------------------------------------------------
# /compare 闂?Cross-paper DFT result comparison
# ---------------------------------------------------------------------------

@router.get("/compare")
async def compare_dft_results(
    property_type: str = Query(..., description="Property type to compare, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Optional adsorbate filter, e.g. Li2S4"),
    catalyst_type: str | None = Query(default=None, description="Optional catalyst type filter: single_atom or dual_atom"),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    min_confidence: float = Query(default=0.3, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
):
    """Cross-paper comparison of a specific DFT property across the entire library.

    Returns each matching data point with paper metadata and (if available)
    the catalyst sample that produced it. Results are sorted by value ascending
    so you can directly compare e.g. adsorption energies across catalysts.
    """
    from app.db.models import DFTResult as DR, Paper as P, CatalystSample as CS
    from collections import defaultdict

    stmt = (
        select(DR, P)
        .join(P, DR.paper_id == P.id)
        .where(DR.property_type.ilike(f"%{property_type}%"))
        .where(DR.confidence >= min_confidence)
        .order_by(DR.value.asc().nulls_last())
        .limit(limit)
    )
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)

    rows = session.execute(stmt).all()

    # Bulk-load catalyst samples for all papers in result set
    paper_ids = list({str(paper.id) for _, paper in rows})
    catalyst_by_paper: dict[str, list] = defaultdict(list)
    if paper_ids:
        from sqlalchemy import text as sa_text
        cat_rows = session.scalars(
            select(CS).where(CS.paper_id.in_([r[1].id for r in rows]))
        ).all()
        for cat in cat_rows:
            catalyst_by_paper[str(cat.paper_id)].append({
                "name": cat.name,
                "type": cat.catalyst_type,
                "metal_centers": cat.metal_centers,
                "coordination": cat.coordination,
                "support": cat.support,
            })

    # Apply catalyst_type filter after joining (post-filter on catalyst data)
    items = []
    for dr, paper in rows:
        pid = str(paper.id)
        cats = catalyst_by_paper.get(pid, [])
        if catalyst_type:
            cats = [c for c in cats if (c.get("type") or "").lower() == catalyst_type.lower()]
            if not cats and catalyst_by_paper.get(pid):  # paper has catalysts but none match
                continue
        items.append({
            "paper_id": pid,
            "title": paper.title,
            "doi": paper.doi,
            "journal": paper.journal,
            "year": paper.year,
            "property_type": dr.property_type,
            "adsorbate": dr.adsorbate,
            "value": dr.value,
            "unit": dr.unit,
            "reaction_step": dr.reaction_step,
            "confidence": dr.confidence,
            "evidence_text": dr.evidence_text,
            "source_section": dr.source_section,
            "source_figure": dr.source_figure,
            "catalysts": cats,
        })

    # Summary statistics
    numeric_values = [it["value"] for it in items if it["value"] is not None]
    stats = {}
    if numeric_values:
        stats = {
            "count": len(numeric_values),
            "min": round(min(numeric_values), 4),
            "max": round(max(numeric_values), 4),
            "mean": round(sum(numeric_values) / len(numeric_values), 4),
            "unit": items[0]["unit"] if items else None,
        }

    return {
        "query": {"property_type": property_type, "adsorbate": adsorbate, "catalyst_type": catalyst_type},
        "stats": stats,
        "total": len(items),
        "items": items,
    }


@router.get("/aggregate")
async def aggregate_papers(
    session: Session = Depends(get_db_session),
):
    """Cross-paper aggregation of DFT results and catalyst samples.

    Groups extracted data across all papers, normalizes naming variations,
    and flags potential aliases for curator review.
    """
    from collections import defaultdict
    from app.db.models import DFTResult as DR, CatalystSample as CS

    dft_rows = session.scalars(
        select(DR).order_by(DR.adsorbate.asc().nulls_last())
    ).all()
    adsorbate_groups = defaultdict(list)
    for row in dft_rows:
        key_raw = (row.adsorbate or "").strip()
        if not key_raw:
            continue
        key = re.sub(r"[^a-zA-Z0-9]", "", key_raw).lower()
        adsorbate_groups[key].append({
            "adsorbate": row.adsorbate, "property_type": row.property_type,
            "value": row.value, "unit": row.unit, "reaction_step": row.reaction_step,
            "paper_id": str(row.paper_id), "source_section": row.source_section,
            "source_figure": row.source_figure, "confidence": row.confidence,
        })

    cat_rows = session.scalars(
        select(CS).order_by(CS.name.asc().nulls_last())
    ).all()
    catalyst_groups = defaultdict(list)
    for row in cat_rows:
        key_raw = (row.name or "").strip()
        if not key_raw:
            continue
        key = re.sub(r"[^a-zA-Z0-9]", "", key_raw).lower()
        catalyst_groups[key].append({
            "name": row.name, "catalyst_type": row.catalyst_type,
            "metal_centers": row.metal_centers, "coordination": row.coordination,
            "support": row.support, "synthesis_method": row.synthesis_method,
            "paper_id": str(row.paper_id),
        })

    aliases = []
    for key, items in sorted(adsorbate_groups.items()):
        if len(items) < 2:
            continue
        raw_names = sorted(set(it["adsorbate"] for it in items if it["adsorbate"]))
        if len(raw_names) > 1:
            aliases.append({
                "type": "adsorbate", "canonical_key": key,
                "variants": raw_names,
                "paper_count": len(set(it["paper_id"] for it in items)),
            })
    for key, items in sorted(catalyst_groups.items()):
        if len(items) < 2:
            continue
        raw_names = sorted(set(it["name"] for it in items if it["name"]))
        if len(raw_names) > 1:
            aliases.append({
                "type": "catalyst", "canonical_key": key,
                "variants": raw_names,
                "paper_count": len(set(it["paper_id"] for it in items)),
            })

    return {
        "adsorbate_groups": dict(sorted(adsorbate_groups.items())),
        "catalyst_groups": dict(sorted(catalyst_groups.items())),
        "possible_name_aliases": aliases,
    }

@router.get("/assets/{filename}")
async def get_asset(filename: str):
    settings = get_settings()
    file_path = settings.storage_paths["figures"] / filename
    if not file_path.exists():
        file_path = settings.storage_paths["tables"] / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))


@router.get("/{paper_id}", response_model=PaperDetailResponse)
async def get_paper(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> PaperDetailResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    return detail


@router.delete("/{paper_id}")
async def delete_paper(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    session.delete(paper)
    session.commit()
    return {"status": "deleted", "paper_id": str(paper_id)}


@router.post("/{paper_id}/extract", response_model=ExtractionRunResponse)
async def rerun_stage2_extraction(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ExtractionRunResponse:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    service = PaperReprocessingService(session=session, settings=settings)
    summary = service.rerun_stage2(paper_id)
    return ExtractionRunResponse(
        paper_id=paper_id,
        status="completed",
        dft_settings=summary.get("dft_settings", 0),
        catalyst_samples=summary.get("catalyst_samples", 0),
        dft_results=summary.get("dft_results", 0),
        electrochemical_performance=summary.get("electrochemical_performance", 0),
        mechanism_claims=summary.get("mechanism_claims", 0),
        writing_cards=summary.get("writing_cards", 0),
    )


@router.post("/ai_search", response_model=AISearchResponse)
async def ai_search(
    payload: AISearchPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AISearchResponse:
    del session

    prompt_used, llm_status, llm_error, llm_diagnostics = _rewrite_ai_search_query(
        payload.query,
        payload.model,
        settings,
    )

    service = DiscoveryService()
    active_providers = payload.providers or service.DEFAULT_SEARCH_PROVIDERS
    raw_results = await run_in_threadpool(
        service.search,
        prompt_used,
        active_providers,
        payload.max_results,
        payload.target_types,
    )

    guard_status = "skipped_by_request" if payload.skip_guard else "not_applicable"
    guard_report = None if payload.skip_guard else {
        "reason": "citation_guard_is_not_applied_to_discovery_search_results"
    }
    guarded_results = []
    for paper in raw_results:
        item = dict(paper)
        item["guard_status"] = guard_status
        item["guard_report"] = guard_report
        guarded_results.append(item)

    return AISearchResponse(
        query=payload.query,
        prompt_used=prompt_used,
        providers=active_providers,
        total=len(guarded_results),
        papers=guarded_results,
        llm_status=llm_status,
        llm_error=llm_error,
        llm_diagnostics=llm_diagnostics,
        result_annotation_status=guard_status,
    )


@router.post("/ai_workflow/jobs")
async def start_ai_workflow_job(
    payload: AIWorkflowPayload,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start the heavy search/download/ingest workflow in the background.

    The synchronous /ai_workflow endpoint is kept for external agents and tests,
    while the web UI should use this job API to avoid browser request timeouts.
    """
    job_id = str(uuid4())
    _remember_ai_workflow_job(
        job_id,
        status="queued",
        query=payload.query,
        library_name=_normalize_library_name(payload.library_name),
        progress={
            "phase": "queued",
            "max_results": payload.max_results,
            "max_downloads": payload.max_downloads,
        },
    )
    background_tasks.add_task(_run_ai_workflow_job, job_id, payload, settings.database_url)
    return _remember_ai_workflow_job(job_id)


@router.get("/ai_workflow/jobs/{job_id}")
async def get_ai_workflow_job(job_id: str) -> dict[str, Any]:
    with AI_WORKFLOW_JOBS_LOCK:
        job = AI_WORKFLOW_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="AI workflow job not found")
        return dict(job)


async def _run_classify_batch_job(job_id: str, payload: ClassifyBatchPayload, database_url: str) -> None:
    _remember_ai_workflow_job(
        job_id,
        status="running",
        progress={
            "phase": "classify_batch",
            "message": "Initializing batch classification job.",
            "completed": 0,
            "total": 0,
            "failed": 0,
        },
    )
    try:
        import asyncio
        from app.config import get_settings
        
        target_library = _normalize_library_name(payload.library_name)
        with session_scope(database_url) as session:
            stmt = select(Paper).where(Paper.library_name == target_library)
            if not payload.overwrite:
                stmt = stmt.where((Paper.paper_type.is_(None)) | (Paper.paper_type == "Unknown"))
            
            papers = list(session.scalars(stmt).all())
            total = len(papers)
            
            _remember_ai_workflow_job(
                job_id,
                progress={
                    "phase": "classify_batch",
                    "message": f"Found {total} papers to classify.",
                    "completed": 0,
                    "total": total,
                    "failed": 0,
                },
            )
            
            failed_items = []
            classified_count = 0
            
            for i in range(0, total, payload.batch_size):
                batch = papers[i:i + payload.batch_size]
                reprocess = PaperReprocessingService(session=session, settings=get_settings())
                
                for idx, paper in enumerate(batch):
                    try:
                        await run_in_threadpool(reprocess.classify_single_paper, paper.id, payload.overwrite)
                        classified_count += 1
                    except Exception as e:
                        logging.getLogger(__name__).warning("Failed to classify paper %s: %s", paper.id, e)
                        failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(e)})
                    
                    current_idx = i + idx + 1
                    _remember_ai_workflow_job(
                        job_id,
                        progress={
                            "phase": "classify_batch",
                            "message": f"Classified {current_idx}/{total} papers.",
                            "completed": current_idx,
                            "total": total,
                            "failed": len(failed_items),
                        },
                    )
                
                if i + payload.batch_size < total:
                    await asyncio.sleep(payload.interval)
            
            _remember_ai_workflow_job(
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "message": f"Successfully completed batch classification.",
                    "completed": total,
                    "total": total,
                    "failed": len(failed_items),
                },
                result={
                    "total": total,
                    "classified": classified_count,
                    "failed_count": len(failed_items),
                    "failed_items": failed_items
                }
            )
    except Exception as exc:
        logging.getLogger(__name__).exception("Batch classification job failed: %s", job_id)
        _remember_ai_workflow_job(
            job_id,
            status="failed",
            progress={"phase": "failed"},
            error=f"{type(exc).__name__}: {exc}",
        )


@router.post("/classify-batch/jobs")
async def start_classify_batch_job(
    payload: ClassifyBatchPayload,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start batch classification of papers in the background."""
    job_id = str(uuid4())
    _remember_ai_workflow_job(
        job_id,
        status="queued",
        library_name=_normalize_library_name(payload.library_name),
        progress={
            "phase": "queued",
            "message": "Classifying batch task is queued.",
            "completed": 0,
            "total": 0,
            "failed": 0,
        },
    )
    background_tasks.add_task(_run_classify_batch_job, job_id, payload, settings.database_url)
    return _remember_ai_workflow_job(job_id)


@router.post("/classify-batch")
async def run_classify_batch_sync(
    payload: ClassifyBatchPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Run batch classification of papers synchronously."""
    target_library = _normalize_library_name(payload.library_name)
    stmt = select(Paper).where(Paper.library_name == target_library)
    if not payload.overwrite:
        stmt = stmt.where((Paper.paper_type.is_(None)) | (Paper.paper_type == "Unknown"))
    
    papers = list(session.scalars(stmt).all())
    total = len(papers)
    
    failed_items = []
    classified_count = 0
    reprocess = PaperReprocessingService(session=session, settings=settings)
    
    for paper in papers:
        try:
            reprocess.classify_single_paper(paper.id, payload.overwrite)
            classified_count += 1
        except Exception as e:
            failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(e)})
            
    return {
        "status": "completed",
        "total": total,
        "classified": classified_count,
        "failed_count": len(failed_items),
        "failed_items": failed_items
    }



@router.post("/ai_workflow", response_model=AIWorkflowResponse)
async def ai_workflow(
    payload: AIWorkflowPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AIWorkflowResponse:
    prompt_used, llm_status, llm_error, llm_diagnostics = _rewrite_ai_search_query(
        payload.query,
        payload.model,
        settings,
    )

    service = DiscoveryService()
    active_providers = payload.providers or service.DEFAULT_SEARCH_PROVIDERS
    raw_results = await run_in_threadpool(
        service.search,
        prompt_used,
        active_providers,
        payload.max_results,
    )

    ingestion = PaperIngestionService(session=session, settings=settings)
    target_library = _normalize_library_name(payload.library_name)
    ingested: list[AIWorkflowIngestedPaperResponse] = []
    failed: list[AIWorkflowFailedItemResponse] = []
    attempted_downloads = 0

    for item in raw_results:
        if attempted_downloads >= payload.max_downloads:
            break
        identifier = item.get("doi") or item.get("url") or item.get("identifier") or item.get("title") or ""
        if not identifier:
            failed.append(
                AIWorkflowFailedItemResponse(
                    identifier="",
                    title=item.get("title"),
                    code="missing_identifier",
                    reason="missing_identifier",
                )
            )
            continue

        doi = item.get("doi")
        existing = _find_existing_paper(session, doi=doi if payload.skip_existing else None, title=item.get("title") if payload.skip_existing else None)
        if payload.skip_existing and existing:
            if existing.library_name != target_library:
                existing.library_name = target_library
                session.add(existing)
                session.commit()
                session.refresh(existing)
            ingested.append(
                AIWorkflowIngestedPaperResponse(
                    paper_id=existing.id,
                    title=existing.title,
                    status="already_exists",
                    identifier=identifier,
                    doi=doi,
                )
            )
            continue

        attempted_downloads += 1
        try:
            raw_paper, metadata = await run_in_threadpool(
                service.fetch_metadata, identifier, active_providers
            )
            existing = _find_existing_paper(session, doi=metadata.get("doi"), title=metadata.get("title")) if payload.skip_existing else None
            if existing:
                if existing.library_name != target_library:
                    existing.library_name = target_library
                    session.add(existing)
                    session.commit()
                    session.refresh(existing)
                ingested.append(
                    AIWorkflowIngestedPaperResponse(
                        paper_id=existing.id,
                        title=existing.title,
                        status="already_exists",
                        identifier=identifier,
                        doi=metadata.get("doi"),
                    )
                )
                continue
            item_status = "completed"
            try:
                with TemporaryDirectory() as tmpdir:
                    dest_dir = Path(tmpdir)
                    pdf_path = await _download_discovery_candidate(service, raw_paper, metadata, dest_dir)
                    paper = await ingestion.ingest_pdf(
                        source_path=pdf_path,
                        original_filename=pdf_path.name,
                        copy_pdf=True,
                        external_metadata=metadata,
                        source_reference=None,
                        library_name=target_library,
                    )
            except Exception:
                paper = ingestion.ingest_metadata_only(
                    external_metadata=metadata,
                    identifier=identifier,
                    library_name=target_library,
                    source_reference=metadata.get("url") or identifier,
                )
                item_status = "metadata_only"
            ingested.append(
                AIWorkflowIngestedPaperResponse(
                    paper_id=paper.id,
                    title=paper.title,
                    status=item_status,
                    identifier=identifier,
                    doi=paper.doi,
                )
            )
        except Exception as exc:
            failed.append(
                AIWorkflowFailedItemResponse(
                    identifier=identifier,
                    title=item.get("title"),
                    code="download_or_ingest_failed",
                    reason=str(exc),
                )
            )

    return AIWorkflowResponse(
        query=payload.query,
        prompt_used=prompt_used,
        providers=active_providers,
        searched_total=len(raw_results),
        attempted_downloads=attempted_downloads,
        ingested=ingested,
        failed=failed,
        llm_status=llm_status,
        llm_error=llm_error,
        llm_diagnostics=llm_diagnostics,
    )
