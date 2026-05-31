from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import AIWorkflowPayload, AIWorkflowResponse
from app.services.workflow_jobs import (
    JOB_TYPE_AI_WORKFLOW,
    build_job_runtime_context,
    cancel_job,
    delete_job,
    clone_job_for_retry_with_status,
    create_job_or_reuse_active,
    dispatch_job,
    execute_ai_workflow,
    get_job,
    list_jobs,
    serialize_job,
)

router = APIRouter()


@router.post("/ai_workflow", response_model=AIWorkflowResponse)
async def ai_workflow(
    payload: AIWorkflowPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AIWorkflowResponse:
    return await execute_ai_workflow(payload, session=session, settings=settings)


@router.post("/ai_workflow/jobs")
async def start_ai_workflow_job(
    payload: AIWorkflowPayload,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    job, reused = create_job_or_reuse_active(
        session,
        job_type=JOB_TYPE_AI_WORKFLOW,
        library_name=payload.library_name,
        payload=payload.model_dump(mode="json"),
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "max_results": payload.max_results,
            "max_downloads": payload.max_downloads,
        },
    )
    dispatch_mode = "reused_active"
    if not reused:
        dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=settings.database_url)
        if dispatch_mode != "celery":
            session.refresh(job)
    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    data["deduplicated"] = reused
    return data


@router.get("/ai_workflow/jobs")
async def list_ai_workflow_jobs(
    library_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    try:
        jobs = list_jobs(
            session,
            job_type=JOB_TYPE_AI_WORKFLOW,
            library_name=library_name,
            status=status,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [serialize_job(job) for job in jobs]


@router.get("/ai_workflow/jobs/{job_id}")
async def get_ai_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_AI_WORKFLOW:
        raise HTTPException(status_code=404, detail="AI workflow job not found")
    return serialize_job(job)


@router.post("/ai_workflow/jobs/{job_id}/retry")
async def retry_ai_workflow_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_AI_WORKFLOW:
        raise HTTPException(status_code=404, detail="AI workflow job not found")
    try:
        retry_job, reused = clone_job_for_retry_with_status(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    dispatch_mode = "reused_active"
    if not reused:
        dispatch_mode = dispatch_job(retry_job.job_id, background_tasks, control_database_url=session.bind.url.render_as_string(hide_password=False) if session.bind is not None else None)
        if dispatch_mode != "celery":
            session.refresh(retry_job)
    data = serialize_job(retry_job)
    data["dispatch_mode"] = dispatch_mode
    data["deduplicated"] = reused
    return data


@router.post("/ai_workflow/jobs/{job_id}/cancel")
async def cancel_ai_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_AI_WORKFLOW:
        raise HTTPException(status_code=404, detail="AI workflow job not found")
    try:
        cancelled = cancel_job(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    data = serialize_job(cancelled)
    data["cancel_mode"] = "soft"
    return data


@router.delete("/ai_workflow/jobs/{job_id}")
async def delete_ai_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job or job.type != JOB_TYPE_AI_WORKFLOW:
        raise HTTPException(status_code=404, detail="AI workflow job not found")
    try:
        delete_job(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job_id": job_id}
