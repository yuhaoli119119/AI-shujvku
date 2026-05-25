from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper, WorkflowJob
from app.db.session import session_scope
from app.schemas.api import (
    AIWorkflowFailedItemResponse,
    AIWorkflowIngestedPaperResponse,
    AIWorkflowPayload,
    AIWorkflowResponse,
    ClassifyBatchPayload,
)
from app.services.discovery_service import DiscoveryService
from app.services.paper_identity import PaperIdentityService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService


logger = logging.getLogger(__name__)
DEFAULT_LIBRARY_NAME = "\u9ed8\u8ba4\u6587\u732e\u5e93"
JOB_TYPE_AI_WORKFLOW = "ai_workflow"
JOB_TYPE_CLASSIFY_BATCH = "classify_batch"
JOB_TYPE_EXTRACTION = "extraction"
JOB_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}


class JobCancelledError(RuntimeError):
    pass


def normalize_library_name(library_name: str | None) -> str:
    return (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME


def validate_job_status(status: str) -> str:
    if status not in JOB_STATUSES:
        raise ValueError(f"Unsupported workflow job status: {status}")
    return status


def build_job_runtime_context(settings: Settings) -> dict[str, Any]:
    return {
        "database_url": settings.database_url,
        "storage_root": str(settings.storage_root),
    }


def build_runtime_settings(base_settings: Settings, runtime_context: dict[str, Any] | None) -> Settings:
    context = runtime_context or {}
    return base_settings.model_copy(
        update={
            "database_url": context.get("database_url", base_settings.database_url),
            "storage_root": Path(context["storage_root"]) if context.get("storage_root") else base_settings.storage_root,
        }
    )


def serialize_job(job: WorkflowJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress or {},
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.replace(tzinfo=timezone.utc).isoformat() if job.created_at else None,
        "updated_at": job.updated_at.replace(tzinfo=timezone.utc).isoformat() if job.updated_at else None,
        "library_name": job.library_name,
    }


def _merge_progress(existing: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    merged.update(updates)
    return merged


def create_job(
    session: Session,
    *,
    job_type: str,
    library_name: str | None,
    payload: dict[str, Any],
    runtime_context: dict[str, Any],
    progress: dict[str, Any],
) -> WorkflowJob:
    job = WorkflowJob(
        job_id=str(uuid4()),
        type=job_type,
        status="queued",
        progress=progress,
        library_name=normalize_library_name(library_name),
        payload=payload,
        runtime_context=runtime_context,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def list_jobs(
    session: Session,
    *,
    job_type: str | None = None,
    library_name: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[WorkflowJob]:
    stmt = select(WorkflowJob)
    if job_type:
        stmt = stmt.where(WorkflowJob.type == job_type)
    if library_name:
        stmt = stmt.where(WorkflowJob.library_name == normalize_library_name(library_name))
    if status:
        stmt = stmt.where(WorkflowJob.status == validate_job_status(status))
    stmt = stmt.order_by(desc(WorkflowJob.created_at)).limit(limit)
    return list(session.scalars(stmt).all())


def get_job(session: Session, job_id: str) -> WorkflowJob | None:
    return session.get(WorkflowJob, job_id)


def get_job_or_raise(session: Session, job_id: str) -> WorkflowJob:
    job = get_job(session, job_id)
    if job is None:
        raise ValueError(f"Workflow job not found: {job_id}")
    return job


def update_job(
    session: Session,
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: Any = None,
    error: str | None = None,
) -> WorkflowJob:
    job = get_job_or_raise(session, job_id)
    if status is not None:
        job.status = validate_job_status(status)
    if progress is not None:
        job.progress = progress
    if result is not None or status == "completed":
        job.result = result
    if error is not None or status == "completed":
        job.error = error
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def is_job_cancelled(session: Session, job_id: str) -> bool:
    job = get_job(session, job_id)
    return bool(job and job.status == "cancelled")


def assert_job_not_cancelled(session: Session, job_id: str) -> None:
    if is_job_cancelled(session, job_id):
        raise JobCancelledError(f"Workflow job cancelled: {job_id}")


def cancel_job(session: Session, job_id: str) -> WorkflowJob:
    job = get_job_or_raise(session, job_id)
    if job.status not in {"queued", "running"}:
        raise ValueError(f"Only queued or running jobs can be cancelled: {job.status}")

    message = "Cancellation requested."
    if job.status == "running":
        message = "Soft cancel requested while task is running."

    job.status = "cancelled"
    job.progress = _merge_progress(
        job.progress,
        {
            "phase": "cancelled",
            "message": message,
            "cancel_mode": "soft",
        },
    )
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def clone_job_for_retry(session: Session, job_id: str) -> WorkflowJob:
    source = get_job_or_raise(session, job_id)
    if source.status not in {"failed", "cancelled"}:
        raise ValueError(f"Only failed or cancelled jobs can be retried: {source.status}")

    retry_payload = dict(source.payload or {})
    retry_progress = {
        "phase": "queued",
        "message": f"Retry queued from job {source.job_id}.",
        "retried_from_job_id": source.job_id,
    }
    if source.type == JOB_TYPE_AI_WORKFLOW:
        retry_progress.update(
            {
                "max_results": retry_payload.get("max_results"),
                "max_downloads": retry_payload.get("max_downloads"),
            }
        )
    if source.type == JOB_TYPE_EXTRACTION:
        retry_progress.update({"paper_id": retry_payload.get("paper_id"), "schemas": retry_payload.get("schemas")})

    return create_job(
        session,
        job_type=source.type,
        library_name=source.library_name,
        payload=retry_payload,
        runtime_context=dict(source.runtime_context or {}),
        progress=retry_progress,
    )


def _find_existing_paper(
    session: Session,
    doi: str | None,
    title: str | None,
    year: int | None = None,
    arxiv_id: str | None = None,
    library_name: str | None = None,
) -> Paper | None:
    identity = PaperIdentityService()
    existing = identity.find_existing_paper(
        session,
        doi=identity.normalize_doi(doi),
        title=title,
        year=year,
        arxiv_id=arxiv_id,
        library_name=library_name,
    )
    if existing is not None:
        return existing
    if doi:
        return identity.find_existing_paper(
            session,
            doi=identity.normalize_doi(doi),
            title=title,
            year=year,
            arxiv_id=arxiv_id,
            library_name=None,
        )
    return None


async def download_discovery_candidate(
    service: DiscoveryService,
    raw_paper: Any,
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


async def execute_ai_workflow(
    payload: AIWorkflowPayload,
    *,
    session: Session,
    settings: Settings,
    job_id: str | None = None,
) -> AIWorkflowResponse:
    from app.api.papers.common import rewrite_ai_search_query

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
    )

    ingestion = PaperIngestionService(session=session, settings=settings)
    target_library = normalize_library_name(payload.library_name)
    ingested: list[AIWorkflowIngestedPaperResponse] = []
    failed: list[AIWorkflowFailedItemResponse] = []
    attempted_downloads = 0

    for item in raw_results:
        if job_id:
            assert_job_not_cancelled(session, job_id)
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
        existing = _find_existing_paper(
            session,
            doi=doi if payload.skip_existing else None,
            title=item.get("title") if payload.skip_existing else None,
            year=item.get("year") if payload.skip_existing else None,
            arxiv_id=PaperIdentityService.extract_arxiv_id(str(identifier)) if payload.skip_existing else None,
            library_name=target_library,
        )
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
            existing = (
                _find_existing_paper(
                    session,
                    doi=metadata.get("doi"),
                    title=metadata.get("title"),
                    year=metadata.get("year"),
                    arxiv_id=PaperIdentityService.extract_arxiv_id(
                        str(metadata.get("arxiv_id") or metadata.get("identifier") or metadata.get("url") or identifier)
                    ),
                    library_name=target_library,
                )
                if payload.skip_existing
                else None
            )
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
                    pdf_path = await download_discovery_candidate(
                        service,
                        raw_paper,
                        metadata,
                        Path(tmpdir),
                    )
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


def run_ai_workflow_job(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = AIWorkflowPayload.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "search_and_ingest",
                "message": "AI workflow is searching, deduplicating, downloading, and metadata-ingesting failures.",
                "max_results": payload.max_results,
                "max_downloads": payload.max_downloads,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            result = asyncio.run(
                execute_ai_workflow(
                    payload,
                    session=job_session,
                    settings=runtime_settings,
                    job_id=job_id,
                )
            )
            assert_job_not_cancelled(job_session, job_id)

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
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
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except Exception as exc:
        logger.exception("AI workflow job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={"phase": "failed"},
                error=f"{type(exc).__name__}: {exc}",
            )


def run_classify_batch_sync(
    payload: ClassifyBatchPayload,
    *,
    session: Session,
    settings: Settings,
) -> dict[str, Any]:
    target_library = normalize_library_name(payload.library_name)
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
        except Exception as exc:
            failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(exc)})

    return {
        "status": "completed",
        "total": total,
        "classified": classified_count,
        "failed_count": len(failed_items),
        "failed_items": failed_items,
    }


def run_classify_batch_job(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = ClassifyBatchPayload.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "classify_batch",
                "message": "Initializing batch classification job.",
                "completed": 0,
                "total": 0,
                "failed": 0,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            target_library = normalize_library_name(payload.library_name)
            stmt = select(Paper).where(Paper.library_name == target_library)
            if not payload.overwrite:
                stmt = stmt.where((Paper.paper_type.is_(None)) | (Paper.paper_type == "Unknown"))

            papers = list(job_session.scalars(stmt).all())
            total = len(papers)
            failed_items = []
            classified_count = 0
            reprocess = PaperReprocessingService(session=job_session, settings=runtime_settings)

            with session_scope(control_db_url) as control_session:
                update_job(
                    control_session,
                    job_id,
                    progress={
                        "phase": "classify_batch",
                        "message": f"Found {total} papers to classify.",
                        "completed": 0,
                        "total": total,
                        "failed": 0,
                    },
                )

            for index, paper in enumerate(papers, start=1):
                assert_job_not_cancelled(job_session, job_id)
                try:
                    reprocess.classify_single_paper(paper.id, payload.overwrite)
                    classified_count += 1
                except Exception as exc:
                    logger.warning("Failed to classify paper %s: %s", paper.id, exc)
                    failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(exc)})

                with session_scope(control_db_url) as control_session:
                    update_job(
                        control_session,
                        job_id,
                        progress={
                            "phase": "classify_batch",
                            "message": f"Classified {index}/{total} papers.",
                            "completed": index,
                            "total": total,
                            "failed": len(failed_items),
                        },
                    )

                if index < total and payload.interval > 0:
                    asyncio.run(asyncio.sleep(payload.interval))

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "message": "Successfully completed batch classification.",
                    "completed": total,
                    "total": total,
                    "failed": len(failed_items),
                },
                result={
                    "total": total,
                    "classified": classified_count,
                    "failed_count": len(failed_items),
                    "failed_items": failed_items,
                },
                error=None,
            )
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except Exception as exc:
        logger.exception("Batch classification job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={"phase": "failed"},
                error=f"{type(exc).__name__}: {exc}",
            )


def run_extraction_job(job_id: str, control_database_url: str | None = None) -> None:
    from app.schemas.extraction import ExtractionJobRequest

    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = ExtractionJobRequest.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "extraction",
                "message": "Running schema-driven scientific extraction.",
                "paper_id": str(payload.paper_id),
                "schemas": payload.schemas,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            reprocess = PaperReprocessingService(session=job_session, settings=runtime_settings)
            summary = reprocess.rerun_stage2(payload.paper_id)
            assert_job_not_cancelled(job_session, job_id)

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "message": "Extraction completed.",
                    "paper_id": str(payload.paper_id),
                    **summary,
                },
                result={"paper_id": str(payload.paper_id), "summary": summary, "schemas": payload.schemas},
                error=None,
            )
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except Exception as exc:
        logger.exception("Extraction job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={"phase": "failed"},
                error=f"{type(exc).__name__}: {exc}",
            )


def dispatch_job(
    job_id: str,
    background_tasks: BackgroundTasks | None = None,
    *,
    control_database_url: str | None = None,
) -> str:
    from kombu import Connection

    from app.workers.tasks import run_workflow_job_task

    try:
        with Connection(get_settings().celery_broker_url, connect_timeout=1) as connection:
            connection.ensure_connection(max_retries=0)
        run_workflow_job_task.delay(job_id)
        return "celery"
    except Exception as exc:
        logger.warning("Celery dispatch failed for job %s, falling back to in-process background task: %s", job_id, exc)
        if background_tasks is None:
            raise
        background_tasks.add_task(run_workflow_job_by_id, job_id, control_database_url)
        return "background_tasks"


def run_workflow_job_by_id(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    job_database_url = control_database_url or base_settings.database_url
    with session_scope(job_database_url) as session:
        job = get_job_or_raise(session, job_id)
        if job.status == "cancelled":
            return
        job_type = job.type

    if job_type == JOB_TYPE_AI_WORKFLOW:
        run_ai_workflow_job(job_id, control_database_url)
        return
    if job_type == JOB_TYPE_CLASSIFY_BATCH:
        run_classify_batch_job(job_id, control_database_url)
        return
    if job_type == JOB_TYPE_EXTRACTION:
        run_extraction_job(job_id, control_database_url)
        return
    raise ValueError(f"Unsupported workflow job type: {job_type}")
