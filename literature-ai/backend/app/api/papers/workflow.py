from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import AIWorkflowPayload, AIWorkflowResponse
from app.services.workflow_jobs import (
    JOB_TYPE_AI_WORKFLOW,
    build_job_runtime_context,
    create_job,
    dispatch_job,
    execute_ai_workflow,
    get_job,
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
    job = create_job(
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
    dispatch_mode = dispatch_job(job.job_id, background_tasks)
    if dispatch_mode != "celery":
        session.refresh(job)
    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    return data


@router.get("/ai_workflow/jobs/{job_id}")
async def get_ai_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="AI workflow job not found")
    return serialize_job(job)
