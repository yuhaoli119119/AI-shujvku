from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.extraction import (
    ExtractionReviewAuditResponse,
    ExtractionFieldReviewResponse,
    ExtractionFieldReviewSaveRequest,
    ExtractionJobRequest,
    ExtractionResultsResponse,
    ExtractionReviewMarkVerifiedRequest,
    ExtractionValidationResponse,
)
from app.schemas.evidence import EvidenceLocatorResponse
from app.services.extraction_review_service import ExtractionReviewService
from app.services.extraction_schema_service import ExtractionSchemaService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.workflow_jobs import (
    JOB_TYPE_EXTRACTION,
    build_job_runtime_context,
    cancel_job,
    clone_job_for_retry,
    create_job,
    dispatch_job,
    get_job,
    list_jobs,
    serialize_job,
)

router = APIRouter()


@router.get("/schemas")
async def get_extraction_schemas(session: Session = Depends(get_db_session)) -> dict[str, Any]:
    return ExtractionSchemaService(session).schemas()


@router.post("/jobs")
async def start_extraction_job(
    payload: ExtractionJobRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    paper = session.get(Paper, payload.paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    job = create_job(
        session,
        job_type=JOB_TYPE_EXTRACTION,
        library_name=paper.library_name,
        payload=payload.model_dump(mode="json"),
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "queued", "paper_id": str(payload.paper_id), "schemas": payload.schemas},
    )
    dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=settings.database_url)
    if dispatch_mode != "celery":
        session.refresh(job)
    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    return data


@router.get("/jobs")
async def list_extraction_jobs(
    library_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    try:
        jobs = list_jobs(session, job_type=JOB_TYPE_EXTRACTION, library_name=library_name, status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [serialize_job(job) for job in jobs]


@router.get("/jobs/{job_id}")
async def get_extraction_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_EXTRACTION:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    return serialize_job(job)


@router.post("/jobs/{job_id}/retry")
async def retry_extraction_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_EXTRACTION:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    try:
        retry_job = clone_job_for_retry(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else None
    dispatch_mode = dispatch_job(retry_job.job_id, background_tasks, control_database_url=db_url)
    if dispatch_mode != "celery":
        session.refresh(retry_job)
    data = serialize_job(retry_job)
    data["dispatch_mode"] = dispatch_mode
    return data


@router.post("/jobs/{job_id}/cancel")
async def cancel_extraction_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_EXTRACTION:
        raise HTTPException(status_code=404, detail="Extraction job not found")
    try:
        cancelled = cancel_job(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_job(cancelled)


@router.get("/results/{paper_id}", response_model=ExtractionResultsResponse)
async def get_extraction_results(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> ExtractionResultsResponse:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return ExtractionSchemaService(session).results(paper_id)


@router.get("/results/{paper_id}/reviews", response_model=list[ExtractionFieldReviewResponse])
async def get_extraction_field_reviews(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[ExtractionFieldReviewResponse]:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return ExtractionReviewService(session).list_reviews(paper_id)


@router.get("/results/{paper_id}/reviews/audit", response_model=ExtractionReviewAuditResponse)
async def audit_extraction_field_reviews(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> ExtractionReviewAuditResponse:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return ExtractionReviewService(session).audit_reviews(paper_id)


@router.post("/results/{paper_id}/reviews/prepare", response_model=list[ExtractionFieldReviewResponse])
async def prepare_extraction_field_reviews(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[ExtractionFieldReviewResponse]:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    return ExtractionReviewService(session).prepare_pending_reviews(paper_id)


@router.post("/results/{paper_id}/reviews/save", response_model=list[ExtractionFieldReviewResponse])
async def save_extraction_field_reviews(
    paper_id: UUID,
    payload: ExtractionFieldReviewSaveRequest,
    session: Session = Depends(get_db_session),
) -> list[ExtractionFieldReviewResponse]:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    try:
        return ExtractionReviewService(session).save_reviews(paper_id, payload.reviews)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/results/{paper_id}/reviews/mark-verified", response_model=list[ExtractionFieldReviewResponse])
async def mark_extraction_fields_verified(
    paper_id: UUID,
    payload: ExtractionReviewMarkVerifiedRequest,
    session: Session = Depends(get_db_session),
) -> list[ExtractionFieldReviewResponse]:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    try:
        return ExtractionReviewService(session).mark_verified(paper_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/results/{paper_id}/validate", response_model=ExtractionValidationResponse)
async def validate_extraction_results(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> ExtractionValidationResponse:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    results = ExtractionSchemaService(session).results(paper_id)
    return ExtractionValidationResponse(
        paper_id=paper_id,
        status=results.validation_status,
        results=results.results,
        field_reviews=results.field_reviews,
        validation_warnings=results.validation_warnings,
    )


@router.get("/results/{paper_id}/evidence-locators", response_model=list[EvidenceLocatorResponse])
async def get_extraction_result_evidence_locators(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[EvidenceLocatorResponse]:
    if not session.get(Paper, paper_id):
        raise HTTPException(status_code=404, detail="Paper not found")
    results = ExtractionSchemaService(session).results(paper_id)
    return EvidenceLocatorService(session).list_extraction_locators(paper_id, results.results)
