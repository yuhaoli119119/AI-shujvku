from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import WorkflowJob
from app.db.session import get_db_session
from app.services.workflow_jobs import (
    JOB_TYPE_AGENT_ACTIVITY,
    JOB_STATUSES,
    clone_job_for_retry_with_status,
    cancel_job,
    delete_job,
    dispatch_job,
    get_job,
    list_jobs,
    serialize_job,
)
from app.utils.library_names import normalize_library_name

router = APIRouter()


class AgentActivityRequest(BaseModel):
    agent: str = Field(default="Codex", min_length=1, max_length=64)
    action: str = Field(..., min_length=1, max_length=128)
    status: str = Field(default="completed")
    library_name: str | None = None
    title: str | None = None
    paper_id: str | None = None
    paper_title: str | None = None
    query: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


@router.get("")
async def list_workflow_jobs(
    job_type: str | None = Query(default=None, alias="type"),
    library_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=80, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    if status == "active":
        statuses = ["queued", "running"]
    elif status:
        if status not in JOB_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unsupported workflow job status: {status}")
        statuses = [status]
    else:
        statuses = [None]

    jobs = []
    try:
        for status_value in statuses:
            jobs.extend(
                list_jobs(
                    session,
                    job_type=job_type,
                    library_name=library_name,
                    status=status_value,
                    limit=limit,
                )
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    jobs.sort(key=lambda job: job.created_at or job.updated_at or datetime.min, reverse=True)
    return [serialize_job(job) for job in jobs[:limit]]


@router.post("/agent-activities")
async def record_agent_activity(
    payload: AgentActivityRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if payload.status not in JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unsupported workflow job status: {payload.status}")

    request_data = payload.model_dump(mode="json")
    title = payload.title or payload.action
    job = WorkflowJob(
        job_id=str(uuid4()),
        type=JOB_TYPE_AGENT_ACTIVITY,
        status=payload.status,
        library_name=normalize_library_name(payload.library_name),
        payload=request_data,
        progress={
            "phase": payload.action,
            "action": payload.action,
            "message": title,
            "agent": payload.agent,
            "paper_id": payload.paper_id,
        },
        result={
            "details": payload.details,
            "metrics": payload.metrics,
            "artifacts": payload.artifacts,
            "success_count": payload.metrics.get("success_count"),
            "failure_count": payload.metrics.get("failure_count"),
        },
        error=payload.error,
        runtime_context={},
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return serialize_job(job)


@router.get("/{job_id}")
async def get_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Workflow job not found")
    return serialize_job(job)


@router.post("/{job_id}/retry")
async def retry_workflow_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    job = get_job(session, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Workflow job not found")
    if job.type == JOB_TYPE_AGENT_ACTIVITY:
        raise HTTPException(status_code=409, detail="Agent activity records are audit logs and cannot be retried")
    try:
        retry_job, reused = clone_job_for_retry_with_status(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else None
    dispatch_mode = "reused_active"
    if not reused:
        dispatch_mode = dispatch_job(retry_job.job_id, background_tasks, control_database_url=db_url)
        if dispatch_mode != "celery":
            session.refresh(retry_job)

    data = serialize_job(retry_job)
    data["dispatch_mode"] = dispatch_mode
    data["deduplicated"] = reused
    return data


@router.post("/{job_id}/cancel")
async def cancel_workflow_job(job_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        job = cancel_job(session, job_id)
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=409, detail=detail) from exc
    return serialize_job(job)


@router.delete("/{job_id}")
async def delete_workflow_job(
    job_id: str,
    force: bool = Query(default=False),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        delete_job(session, job_id, allow_active=force)
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=409, detail=detail) from exc
    return {"status": "deleted", "job_id": job_id}
