from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import (
    AISearchPayload,
    AISearchResponse,
    DiscoveryDownloadRequest,
    DiscoverySearchResponse,
    IngestResponse,
)
from app.services.discovery_service import DiscoveryService
from app.services.paper_identity import PaperIdentityService
from app.services.paper_ingestion import PaperIngestionService
from app.services.workflow_jobs import (
    JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
    build_job_runtime_context,
    create_job_or_reuse_active,
    dispatch_job,
    download_discovery_candidate,
    normalize_library_name,
    serialize_job,
)

from .common import rewrite_ai_search_query

router = APIRouter()


def _find_existing_paper(session: Session, doi: str | None, title: str | None):
    from app.services.workflow_jobs import _find_existing_paper as find_existing_paper

    return find_existing_paper(session, doi, title)


@router.get("/discovery/search", response_model=DiscoverySearchResponse)
async def discovery_search(
    q: str = Query(..., min_length=2, description="Keyword query for external literature search"),
    providers: list[str] = Query(default=["openalex", "arxiv"], description="External search providers"),
    limit: int = Query(default=30, ge=1, le=80, description="Maximum number of returned results"),
) -> DiscoverySearchResponse:
    service = DiscoveryService()
    items = service.search(query=q, providers=providers, limit=limit)
    return DiscoverySearchResponse(query=q, providers=providers, total=len(items), items=items)


@router.post("/discovery/download/jobs")
async def queue_discovery_download_and_ingest(
    payload: DiscoveryDownloadRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    target_library = normalize_library_name(payload.library_name)
    job_payload = {
        "identifier": payload.identifier,
        "providers": payload.providers,
        "library_name": target_library,
    }
    job, reused = create_job_or_reuse_active(
        session,
        job_type=JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
        library_name=target_library,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Discovery download ingest is queued in the worker.",
            "identifier": payload.identifier,
        },
    )
    dispatch_mode = "reused_active"
    if not reused:
        db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else settings.database_url
        dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=db_url)
        if dispatch_mode != "celery":
            session.refresh(job)
    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    data["deduplicated"] = reused
    return data


@router.post("/discovery/download", response_model=IngestResponse)
async def discovery_download_and_ingest(
    payload: DiscoveryDownloadRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    service = DiscoveryService()
    raw_paper, metadata = await run_in_threadpool(service.fetch_metadata, payload.identifier, payload.providers)

    target_library = normalize_library_name(payload.library_name)
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
    downloaded_pdf_name: str | None = None
    try:
        with TemporaryDirectory() as tmpdir:
            pdf_path = await download_discovery_candidate(service, raw_paper, metadata, Path(tmpdir))
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
    normalized_doi = PaperIdentityService.normalize_doi(doi)
    if normalized_doi and paper.doi != normalized_doi:
        paper.doi = normalized_doi
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


@router.post("/ai_search", response_model=AISearchResponse)
async def ai_search(
    payload: AISearchPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AISearchResponse:
    del session

    prompt_used, llm_status, llm_error, llm_diagnostics = rewrite_ai_search_query(
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
