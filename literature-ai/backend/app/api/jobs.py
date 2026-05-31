from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.services.workflow_jobs import (
    JOB_STATUSES,
    clone_job_for_retry_with_status,
    dispatch_job,
    get_job,
    list_jobs,
    serialize_job,
)

router = APIRouter()


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
