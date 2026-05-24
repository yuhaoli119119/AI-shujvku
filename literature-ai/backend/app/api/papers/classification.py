from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import ClassifyBatchPayload
from app.services.workflow_jobs import (
    JOB_TYPE_CLASSIFY_BATCH,
    build_job_runtime_context,
    create_job,
    dispatch_job,
    run_classify_batch_sync as run_classify_batch_sync_service,
    serialize_job,
)

router = APIRouter()


@router.post("/classify-batch/jobs")
async def start_classify_batch_job(
    payload: ClassifyBatchPayload,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    job = create_job(
        session,
        job_type=JOB_TYPE_CLASSIFY_BATCH,
        library_name=payload.library_name,
        payload=payload.model_dump(mode="json"),
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Classifying batch task is queued.",
            "completed": 0,
            "total": 0,
            "failed": 0,
        },
    )
    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_job(job.job_id, background_tasks)
    return data


@router.post("/classify-batch")
async def run_classify_batch_sync(
    payload: ClassifyBatchPayload,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return run_classify_batch_sync_service(payload, session=session, settings=settings)
