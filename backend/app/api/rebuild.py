from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
import csv
import io
from sqlalchemy.orm import Session
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.db.models import RebuildValueSource, RebuildDataValue
from app.schemas.ordinary_parsing import ParseCheckpoint
from app.schemas.rebuild import (
    ProcessingStatusReport,
    ProcessingStatusReportEnvelope,
    RebuildAssetsImportRequest,
    RebuildAssetMetadataCorrection, RebuildSectionMetadataCorrection,
    RebuildAnalysisRequest,
    RebuildRowsImportRequest,
    RebuildDataRowInput,
    RebuildVisualAssetCropRequest,
    RebuildVisualAssetRequest,
    RebuildValueCorrectionRequest,
    RebuildSourceCorrectionRequest, RebuildSourceLifecycleRequest, RebuildSourceAssetBindingRequest, RebuildSectionOrderRequest,
    RebuildDedupPreviewRequest, RebuildDedupApplyRequest,
    RebuildRowIdentityCorrectionRequest,
    RebuildSectionsImportRequest,
    RebuildSectionCorrectionsRequest,
)
from app.services.rebuild_metadata_cas import metadata_state, correct_metadata, validate_material_mapping
from app.services.rebuild_value_dedup import preview_dedup, apply_dedup, dedup_contract
from app.services.rebuild_source_correction import source_state, correct_source, source_correction_contract, deactivate_source, bind_source_asset
from app.services.artifact_store import ArtifactStore
from app.services.batch_catalog import batch_catalog
from app.services.legacy_data_transfer import LegacyTransferRequest
from app.services.rebuild_file_preview import resolve_original_download, resolve_reading_pdf
from app.services.rebuild_workflow_service import (
    RebuildWorkflowError,
    analyze,
    associate_pdf_file,
    get_asset,
    get_analysis_run,
    get_file,
    get_paper,
    import_data_row,
    list_assets,
    list_files,
    list_papers,
    list_rows,
    reaction_templates,
    resolve_asset_image_path,
    resolve_file_path,
    resolve_paper_source_pdf,
    serialize_asset,
    serialize_file,
    serialize_value,
    _required_paper,
    upsert_visual_asset,
    prepare_visual_asset,
    update_data_value,
    correct_row_identity,
)
from app.security.session_auth import require_session, SessionState, origin_allowed, csrf_token_valid
from app.services.ai_extract_service import (
    JOB_TYPE, AiExtractError,
    create_ai_extract_job,
    get_latest_job,
    has_running_job,
    serialize_job,
    sync_job_status,
)


from app.schemas.rebuild import (ExtractionFreezeRequest, ExtractionReviewRequest, ReviewProgressRequest, RebuildQueueControlRequest,
                                RebuildQueueEnqueueRequest, RebuildQueueClaimRequest, RebuildQueueAttemptRequest)


router = APIRouter(tags=["rebuild"])
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
logger = logging.getLogger(__name__)


def _http_error(exc: RebuildWorkflowError) -> HTTPException:
    status = 404 if str(exc) in {"paper_not_found", "file_not_found", "asset_not_found", "value_not_found", "row_not_found", "source_not_found", "immutable_inventory_not_cached", "coverage_linked_output_not_found", "stage_import_receipt_not_found"} else (
        409 if str(exc) in {"paper_has_other_unresolved_writer_or_reviewer", "work_package_revision_changed", "stage_request_id_reused", "history_revision_changed", "history_binding_changed", "history_request_id_reused", "history_page_revision_required", "stage_check_revision_changed", "stage_binding_changed", "stage_page_revision_required", "stage_page_revision_changed", "coverage_request_id_reused", "coverage_plan_revision_changed", "coverage_output_revision_changed", "coverage_read_revision_changed", "derived_pdf_preview_unavailable", "source_sha256_mismatch", "value_revision_conflict", "value_request_id_reused", "source_revision_conflict", "source_key_conflict", "source_inactive", "source_request_id_reused", "mutation_request_id_reused", "acceptance_request_id_reused", "acceptance_frozen_revision_changed", "independent_reviewer_required", "acceptance_gates_not_satisfied", "accepted_receipt_is_immutable_freeze_new_revision", "immutable_read_source_changed", "immutable_cache_content_mismatch", "independent_task_authorization_required", "independent_task_authorization_invalid", "independent_task_authority_not_configured", "independent_task_authority_unavailable"} else 400
    )
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/papers/{paper_id}/ai-extract/jobs")
async def create_ai_extract_job_endpoint(
    paper_id: UUID,
    request: Request,
    model: Literal["gpt-6-luna", "cn:deepseek-v4.1-flash", "cn:glm-5.3-flash", "cn:glm-5.2"] | None = Body(default=None, embed=True),
    request_id: str | None = Body(default=None, min_length=1, max_length=128),
    file_ids: list[UUID] | None = Body(default=None, max_length=30),
    retry: bool = Body(default=False),
    source_job_id: str | None = Body(default=None, min_length=1, max_length=128),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(require_session),
) -> dict[str, Any]:
    """Create and dispatch a one-click AI extract job for one paper.

    Requires an authenticated workbench session. Creates a WorkflowJob of
    type ``ai_extract_rebuild`` and dispatches a Codex-web autonomous agent.
    If a job is already running for this paper, returns that job instead of
    creating a duplicate.
    """
    try:
        return await create_ai_extract_job(session, paper_id, settings, model=model, request_id=request_id,
                                           file_ids=[str(fid) for fid in file_ids] if file_ids else None, retry=retry,
                                           source_job_id=source_job_id, actor=_auth.username)
    except AiExtractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ai_extract_job_failed: {type(exc).__name__}: {exc}",
        ) from exc


@router.get("/papers/{paper_id}/ai-extract/jobs/latest")
async def get_latest_ai_extract_job_endpoint(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the most recent AI extract job for this paper.

    If the job is still running, performs a read-through check of the
    Codex-web thread status and updates the job accordingly.
    """
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        job = await run_phase(session, lambda worker: get_latest_job(worker, paper_id))
    except LockConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        return {
            "job_id": None,
            "type": JOB_TYPE,
            "status": "not_started",
            "paper_id": str(paper_id),
            "result": None,
            "error": None,
        }
    # Read-through: sync status from Codex-web if running
    if job.status in ("queued", "dispatching", "running"):
        try:
            with Session(bind=session.get_bind(), autoflush=False,
                         expire_on_commit=False) as phase_session:
                job = await sync_job_status(phase_session, job, settings)
        except Exception:
            pass
    return await run_phase(session, lambda worker: serialize_job(job, worker))


@router.get("/library-progress")
def current_library_progress(
    library_name: str = Query(..., min_length=1),
    session: Session = Depends(get_db_session),
):
    from app.services.library_progress import library_progress
    return library_progress(session, library_name)


@router.get("/processing-statuses")
async def current_processing_statuses(
    library_name: str | None = None,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
):
    from app.db.models import Paper, WorkflowJob
    from app.services.paper_list_readonly import readonly_library_clause
    from app.services.paper_processing_status import processing_statuses
    import asyncio
    from datetime import datetime
    from app.services.paper_db_phase import LockConflictError, job_pin, run_phase
    query = select(Paper.id)
    if library_name is not None:
        query = query.where(readonly_library_clause(library_name))
    def load(worker):
        paper_ids = list(worker.scalars(query))
        jobs = list(worker.scalars(select(WorkflowJob).where(
            WorkflowJob.type == JOB_TYPE, WorkflowJob.status.in_(['queued','dispatching','running']),
            WorkflowJob.payload.op('->>')('paper_id').in_([str(pid) for pid in paper_ids])
        ).order_by(WorkflowJob.updated_at.asc()).limit(4))) if paper_ids else []
        return paper_ids, jobs
    try:
        ids, jobs = await run_phase(session, load)
    except LockConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for job in jobs:
        job_id = job.job_id
        pin = job_pin(job)
        old_read_at = ((job.progress or {}).get('readback') or {}).get('read_at')
        try:
            with Session(bind=session.get_bind(), autoflush=False,
                         expire_on_commit=False) as phase_session:
                job = await asyncio.wait_for(sync_job_status(phase_session, job, settings), timeout=5)
            if (job.status in ('queued','dispatching','running') and
                    ((job.progress or {}).get('readback') or {}).get('read_at') == old_read_at):
                def mark(worker):
                    current = worker.scalar(select(WorkflowJob).where(
                        WorkflowJob.job_id == job_id).with_for_update())
                    if current and job_pin(current) == pin and current.status in ('queued','dispatching','running'):
                        current.progress = {**(current.progress or {}), 'needs_check': True,
                                            'phase': 'needs_check',
                                            'status_checked_at': datetime.utcnow().isoformat()}
                    return current
                await run_phase(session, mark)
        except Exception:
            def mark_unknown(worker):
                current = worker.scalar(select(WorkflowJob).where(
                    WorkflowJob.job_id == job_id).with_for_update())
                if current and current.status in ('queued','dispatching','running'):
                    current.progress = {**(current.progress or {}), 'needs_check': True,
                                        'phase': 'needs_check',
                                        'status_checked_at': datetime.utcnow().isoformat()}
                return current
            try:
                await run_phase(session, mark_unknown)
            except Exception:
                pass
    statuses = await run_phase(session, lambda worker: processing_statuses(worker, ids))
    return {'items':[{'paper_id':str(pid),'processing_status':status} for pid,status in statuses.items()]}


async def _require_work_package_session(request: Request) -> SessionState:
    return await require_session(request, settings=get_settings())


def _guard_request_work_phase(session, request, auth, body=None):
    from app.db.models import RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildVisualAsset
    from app.services.external_paper_work import guard_workbench_write
    from app.services.paper_db_phase import LockConflictError
    from app.services.rebuild_mutation_audit import audit_ids
    from app.services.rebuild_queue import writer_task
    # Creation services check the existing request identity under the paper lock
    # before testing occupancy, so exact replays must reach those services.
    if request.method == 'POST' and request.url.path.endswith(('/reading-assistance/jobs', '/ai-extract/jobs')):
        return
    paper_id = request.path_params.get("paper_id") or (body.get("paper_id") if isinstance(body, dict) else None)
    if not paper_id:
        for key, model in (("row_id", RebuildDataRow), ("value_id", RebuildDataValue),
                           ("source_id", RebuildValueSource), ("asset_id", RebuildVisualAsset)):
            if request.path_params.get(key):
                try:
                    record = session.get(model, UUID(str(request.path_params[key])))
                except ValueError:
                    raise HTTPException(422, "invalid_target_id")
                if record:
                    paper_id = record.paper_id
                break
    if paper_id:
        try:
            _, task_id = audit_ids(request, body if isinstance(body, dict) else None)
            request.state.admitted_task_id = task_id
            task = writer_task(session, paper_id, auth.username, task_id)
            task_id = task.job_id if task else None
            guard_workbench_write(session, UUID(str(paper_id)), auth.username, task_id)
        except LockConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RebuildWorkflowError as exc:
            status = 409 if str(exc) == 'paper_has_other_unresolved_writer_or_reviewer' else 400
            raise HTTPException(status, str(exc)) from exc
        except ValueError:
            raise HTTPException(422, "invalid_paper_id")


async def _guard_request_work(session, request, auth, body=None):
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, lambda worker: _guard_request_work_phase(
            worker, request, auth, body))
    except LockConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _request_task_id(request: Request, fallback: str | None = None):
    return getattr(request.state, 'admitted_task_id', None) or fallback


def _guard_sync_write(session: Session, paper_id: UUID, actor: str, task_id: str | None = None):
    """Atomic occupancy guard for routes already running in FastAPI's threadpool."""
    from app.services.external_paper_work import guard_workbench_write
    from app.services.paper_db_phase import BUSY, LockConflictError, is_lock_conflict
    try:
        guard_workbench_write(session, paper_id, actor, task_id)
    except (LockConflictError, OperationalError) as exc:
        session.rollback()
        if isinstance(exc, LockConflictError) or is_lock_conflict(exc):
            raise HTTPException(status_code=409, detail=BUSY) from exc
        raise
    except RebuildWorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _processing_status_write(session: Session, paper_id: UUID, payload):
    """Injectable worker-session boundary for status transport compatibility."""
    from app.services.paper_db_phase import run_phase
    from app.services.paper_processing_status import report_processing
    from app.services.rebuild_workflow_service import _required_paper
    return await run_phase(session, lambda worker: report_processing(
        worker, _required_paper(worker, paper_id), payload))


@router.put("/papers/{paper_id}/processing-status")
async def report_paper_processing_status(
    paper_id: UUID,
    payload: ProcessingStatusReport | ProcessingStatusReportEnvelope,
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(_require_work_package_session),
):
    if isinstance(payload, ProcessingStatusReportEnvelope):
        payload = payload.payload
    from app.services.paper_db_phase import LockConflictError
    try:
        result = await _processing_status_write(session, paper_id, payload)
        return {'paper_id':str(paper_id),'processing_status':result}
    except (ValueError, RebuildWorkflowError, LockConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/templates")
async def rebuild_templates() -> dict[str, Any]:
    return reaction_templates()


@router.get("/batch-catalog")
async def rebuild_batch_catalog(
    scope: str = Query(..., pattern="^(all|selected)$"),
    library: list[str] | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    snapshot: str | None = None,
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(require_session),
) -> dict[str, Any]:
    try:
        return batch_catalog(session, scope=scope, libraries=library,
                             limit=limit, offset=offset, snapshot=snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "catalog_changed_restart_selection" else 400,
                            detail=str(exc)) from exc


@router.get("/papers")
async def rebuild_papers(
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return list_papers(session, query=q, limit=limit, offset=offset)


@router.get("/papers/{paper_id}")
async def rebuild_paper(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return get_paper(session, paper_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


async def _require_section_authenticated_session(request: Request) -> SessionState:
    """Authenticate a replay-aware section writer without taking occupancy."""
    auth = await _require_work_package_session(request)
    if not origin_allowed(request, get_settings()):
        raise HTTPException(status_code=403, detail="cross_origin_request_blocked")
    if not csrf_token_valid(request, auth):
        raise HTTPException(status_code=403, detail="csrf_token_invalid")
    try:
        body = await request.json()
        json.dumps(body, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="section_body_must_be_finite_json") from exc
    return auth


async def _require_section_write_session(request: Request, session: Session = Depends(get_db_session)) -> SessionState:
    auth = await _require_section_authenticated_session(request)
    body = await request.json()
    # Boundary guard runs in a short worker transaction. Audited/full writers
    # recheck occupancy while holding the Paper row in their worker transaction.
    await _guard_request_work(session, request, auth, body)
    return auth


@router.get("/papers/{paper_id}/ai-extract/jobs/{job_id}")
async def ordinary_job(paper_id: UUID, job_id: str, session: Session = Depends(get_db_session),
                       _auth: SessionState = Depends(_require_work_package_session)):
    from app.db.models import WorkflowJob
    from app.services.paper_db_phase import run_phase
    def load(worker):
        job = worker.get(WorkflowJob, job_id)
        if not job or job.type != JOB_TYPE or (job.payload or {}).get("paper_id") != str(paper_id):
            raise HTTPException(404, "parse_job_not_found")
        return serialize_job(job, worker)
    return await run_phase(session, load)


@router.put("/papers/{paper_id}/ai-extract/jobs/{job_id}/checkpoints")
async def ordinary_checkpoint(paper_id: UUID, job_id: str, payload: ParseCheckpoint,
                              session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
                              _auth: SessionState = Depends(_require_section_write_session)):
    from app.services.ordinary_parsing import checkpoint
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, lambda worker: checkpoint(
            worker, paper_id, job_id, payload.model_dump(mode="json"), settings))
    except (ValueError, RebuildWorkflowError, LockConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/papers/{paper_id}/files/{file_id}/reading-text")
async def ordinary_reading_text(paper_id: UUID, file_id: UUID, offset: int = Query(0, ge=0),
                                limit: int = Query(25, ge=1, le=100),
                                session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
                                _auth: SessionState = Depends(_require_work_package_session)):
    from app.services.ordinary_parsing import reading_text
    from app.services.paper_db_phase import run_phase
    try:
        return await run_phase(session, lambda worker: reading_text(
            worker, paper_id, file_id, settings, offset, limit))
    except (ValueError, RebuildWorkflowError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/papers/{paper_id}/sections")
async def rebuild_sections(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(_require_work_package_session),
) -> dict[str, Any]:
    from app.services.rebuild_sections import list_sections, section_coverage
    try:
        files = list_files(session, paper_id)
        sections = list_sections(session, paper_id)
        return {"items": sections, "section_coverage": section_coverage(sections, files)}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


def _queue_error(exc):
    detail = str(exc)
    return HTTPException(status_code=403 if detail == "rebuild_queue_disabled" else 404 if detail == "paper_not_found" else 409, detail=detail)


@router.get("/queue")
async def rebuild_queue_snapshot(session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
                                 _auth: SessionState = Depends(_require_work_package_session)):
    from app.services.rebuild_queue import snapshot
    return snapshot(session, settings)


@router.put("/queue/control")
async def rebuild_queue_control(payload: RebuildQueueControlRequest, session: Session = Depends(get_db_session),
                                 settings: Settings = Depends(get_settings), auth: SessionState = Depends(_require_section_write_session)):
    from app.services.rebuild_queue import set_control
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, lambda worker: set_control(
            worker, paused=payload.paused, limits=payload.limits, actor=auth.username, settings=settings))
    except (RebuildWorkflowError, LockConflictError) as exc:
        raise _queue_error(exc) from exc
    except Exception:
        raise


@router.post("/papers/{paper_id}/queue")
async def enqueue_rebuild_attempt(paper_id: UUID, payload: RebuildQueueEnqueueRequest,
                                 session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
                                 auth: SessionState = Depends(_require_section_write_session)):
    from app.services.rebuild_queue import enqueue
    from app.services.paper_db_phase import LockConflictError, run_phase
    from app.services.external_paper_work import guard_workbench_write
    def execute(worker):
        guard_workbench_write(worker, paper_id, auth.username)
        return enqueue(worker, paper_id, payload, actor=auth.username, settings=settings)
    try:
        return await run_phase(session, execute)
    except (RebuildWorkflowError, LockConflictError) as exc:
        raise _queue_error(exc) from exc
    except Exception:
        raise


@router.post("/queue/claim")
async def claim_rebuild_attempt(payload: RebuildQueueClaimRequest, session: Session = Depends(get_db_session),
                               settings: Settings = Depends(get_settings), _auth: SessionState = Depends(_require_section_write_session)):
    from app.services.rebuild_queue import claim_next
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, lambda worker: claim_next(
            worker, stage=payload.stage, worker_id=payload.worker_id, settings=settings))
    except (RebuildWorkflowError, LockConflictError) as exc:
        raise _queue_error(exc) from exc
    except Exception:
        raise


@router.post("/queue/{job_id}/dispatch-intent")
async def prepare_rebuild_dispatch(job_id: str, payload: RebuildQueueAttemptRequest, session: Session = Depends(get_db_session),
                                   settings: Settings = Depends(get_settings), _auth: SessionState = Depends(_require_section_write_session)):
    from app.services.rebuild_queue import prepare_dispatch
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, lambda worker: prepare_dispatch(
            worker, job_id, worker_id=payload.worker_id,
            attempt_token=str(payload.attempt_token), settings=settings))
    except (RebuildWorkflowError, LockConflictError) as exc:
        raise _queue_error(exc) from exc
    except Exception:
        raise


@router.post("/queue/{job_id}/readback")
async def readback_rebuild_attempt(job_id: str, payload: RebuildQueueAttemptRequest, session: Session = Depends(get_db_session),
                                   settings: Settings = Depends(get_settings), _auth: SessionState = Depends(_require_section_write_session)):
    from app.services.rebuild_queue_native import reconcile_attempt
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        result = await reconcile_attempt(session, job_id, attempt_token=str(payload.attempt_token), settings=settings,
                                         manual_recheck=payload.manual_recheck)
        if settings.rebuild_native_stop_enabled:
            from app.db.models import WorkflowJob
            current = await run_phase(session, lambda worker: worker.get(WorkflowJob, job_id))
            if current and current.status == "needs_attention":
                from app.services.rebuild_native_stop import protective_stop
                result["native_stop"] = await protective_stop(settings.database_url, job_id, settings=settings)
        return result
    except (RebuildWorkflowError, LockConflictError) as exc:
        raise _queue_error(exc) from exc
    except Exception:
        raise


@router.post("/native-attempts/{job_id}/stop")
async def controlled_native_attempt_stop(
    job_id: str, payload: RebuildQueueAttemptRequest,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(_require_section_write_session),
):
    from app.db.models import WorkflowJob
    from app.services.rebuild_native_stop import protective_stop
    from app.services.paper_db_phase import run_phase
    job = await run_phase(session, lambda worker: worker.get(WorkflowJob, job_id))
    if job is None or (job.payload or {}).get("attempt_token") != str(payload.attempt_token):
        raise HTTPException(status_code=409, detail="native_stop_attempt_identity_mismatch")
    try:
        return await protective_stop(settings.database_url, job_id, settings=settings)
    except RebuildWorkflowError as exc:
        raise _queue_error(exc) from exc


@router.get("/papers/{paper_id}/acceptance")
async def get_rebuild_acceptance(
    paper_id: UUID, session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(_require_work_package_session),
):
    from app.services.rebuild_acceptance import acceptance_status, contract
    try:
        _required_paper(session, paper_id)
        from app.services.rebuild_acceptance import current_snapshot
        return {"acceptance": acceptance_status(session, paper_id), "contract": contract(),
                "current_revision": current_snapshot(session, paper_id)[0]["revision"]}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/acceptance/freeze")
def freeze_rebuild_extraction(
    paper_id: UUID, payload: ExtractionFreezeRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session),
):
    from app.services.rebuild_acceptance import freeze
    try:
        _guard_sync_write(session, paper_id, auth.username,
                          _request_task_id(request, payload.task_id))
        result = freeze(session, paper_id, payload, settings, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/acceptance/{freeze_id}/review-progress")
def report_independent_review_progress(
    paper_id: UUID, freeze_id: str, payload: ReviewProgressRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session),
):
    from app.services.rebuild_review_progress import record_progress
    try:
        _guard_sync_write(session, paper_id, auth.username,
                          _request_task_id(request, payload.reviewer_task_id))
        result = record_progress(session, paper_id, freeze_id, payload, settings, actor=auth.username,
                                 authorization=request.headers.get("X-Reviewer-Authorization"))
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/acceptance/{freeze_id}/reviews")
def review_rebuild_extraction(
    paper_id: UUID, freeze_id: str, payload: ExtractionReviewRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session),
):
    from app.services.rebuild_acceptance import review
    try:
        _guard_sync_write(session, paper_id, auth.username,
                          _request_task_id(request, payload.reviewer_task_id))
        result = review(session, paper_id, freeze_id, payload, settings, actor=auth.username,
                        authorization=request.headers.get("X-Reviewer-Authorization"))
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/sections/import")
def import_rebuild_sections(
    paper_id: UUID,
    payload: RebuildSectionsImportRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session),
) -> dict[str, Any]:
    from app.services.rebuild_sections import import_sections
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        result = import_sections(session, paper_id, payload, settings, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        error = str(exc)
        status = 404 if error in {"paper_not_found", "file_not_found"} else (
            409 if error in {"section_request_id_reused", "section_key_conflict", "section_id_collision",
                             "section_stored_content_conflict", "source_sha256_mismatch",
                             "section_reading_pdf_sha256_mismatch", "derived_pdf_preview_unavailable"} else 400)
        raise HTTPException(status_code=status, detail=error) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/sections/order")
def declare_rebuild_section_order(paper_id: UUID, payload: RebuildSectionOrderRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    from app.services.rebuild_sections import declare_section_order
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        result = declare_section_order(session, paper_id, payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        error = str(exc)
        raise HTTPException(status_code=409 if error in {
            'section_order_revision_conflict', 'section_order_request_id_reused'} else
            404 if error == 'paper_not_found' else 400, detail=error) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/sections/corrections")
def declare_rebuild_section_corrections(
    paper_id: UUID,
    payload: RebuildSectionCorrectionsRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session),
) -> dict[str, Any]:
    from app.services.rebuild_sections import declare_section_corrections
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        result = declare_section_corrections(session, paper_id, payload, settings, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        error = str(exc)
        status = 404 if error in {"paper_not_found", "file_not_found"} else (
            409 if error in {"section_correction_request_id_reused", "section_correction_relation_conflict",
                             "section_correction_content_mismatch", "section_correction_target_is_history",
                             "section_correction_reading_source_mismatch", "source_sha256_mismatch",
                             "derived_pdf_preview_unavailable"} else 400)
        raise HTTPException(status_code=status, detail=error) from exc
    except Exception:
        session.rollback()
        raise


@router.get("/papers/{paper_id}/delivery-package")
def read_delivery_package(
    paper_id: UUID,
    view: Literal["summary", "checkpoints", "gaps", "receipts", "jobs"] = "summary",
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    expected_revision: str | None = Query(None, pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_work_package_session),
):
    """Read existing delivery records without native calls or status reconciliation."""
    from app.services.paper_delivery import delivery_package
    from app.services.rebuild_acceptance import current_snapshot
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session, paper_id)
        return delivery_package(session, paper_id, current_snapshot(session, paper_id)[0], get_settings(),
                                view=view, offset=offset, limit=limit, expected_revision=expected_revision)
    except RebuildWorkflowError as exc:
        if str(exc) in {"delivery_revision_changed", "delivery_page_revision_required"}:
            raise HTTPException(409, str(exc)) from exc
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/ai-work-package")
async def rebuild_ai_work_package(
    paper_id: UUID,
    rows_limit: int = Query(default=500, ge=1, le=500),
    rows_offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(_require_work_package_session),
    view: str = Query(default="full", pattern="^(full|compact|rows|assets|sections|legacy_tables)$"),
    expected_revision: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
) -> dict[str, Any]:
    """Give an external AI one read call for paper context, assets and rows."""
    try:
        # One response must describe one PostgreSQL snapshot, including its
        # revision. Authentication uses the separate workbench session store.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        from app.services.rebuild_sections import list_sections, section_coverage, section_write_contract
        from app.services.rebuild_acceptance import acceptance_status, current_snapshot, contract as acceptance_contract
        from app.services.rebuild_table_content import table_write_contract
        from app.services.rebuild_work_package import bounded_view, compact_package
        from app.services.rebuild_quality import producer_contract
        _required_paper(session, paper_id)
        snapshot = current_snapshot(session, paper_id)[0]
        if expected_revision is not None and expected_revision != snapshot["revision"]:
            raise RebuildWorkflowError("work_package_revision_changed")
        if view == "compact":
            return compact_package(session, paper_id, snapshot)
        if view in {"rows", "assets", "sections", "legacy_tables"}:
            return bounded_view(session, paper_id, view=view, snapshot=snapshot, limit=rows_limit, offset=rows_offset)
        from app.services.rebuild_file_lifecycle import file_set
        from app.services.rebuild_work_package import mark_scientific_file_bindings
        lifecycle = file_set(session, paper_id)
        files = [f for f in lifecycle['items'] if f['id'] in lifecycle['active_file_ids']]
        inactive = {f['id'] for f in lifecycle['items']} - set(lifecycle['active_file_ids'])
        sections = list_sections(session, paper_id)
        mark_scientific_file_bindings(sections, inactive)
        rows = list_rows(session, paper_id=paper_id, limit=rows_limit, offset=rows_offset)
        mark_scientific_file_bindings(rows, inactive)
        assets = list_assets(session, paper_id)
        mark_scientific_file_bindings(assets, inactive)
        rows["next_page"] = (f"/api/rebuild/papers/{paper_id}/ai-work-package?rows_limit={rows_limit}&rows_offset={rows['next_offset']}&expected_revision={snapshot['revision']}"
                             if rows["has_more"] else None)
        rows["continuation_page"] = (f"/api/rebuild/papers/{paper_id}/ai-work-package?view=rows&rows_limit={rows_limit}&rows_offset={rows['next_offset']}&expected_revision={snapshot['revision']}" if rows["has_more"] else None)
        return {
            "table_write_contract": table_write_contract(),
            "output_revision": snapshot["revision"],
            "acceptance": acceptance_status(session, paper_id, snapshot=snapshot),
            "producer_contract": producer_contract(),
            "quality_endpoint": f"/api/rebuild/papers/{paper_id}/quality?expected_revision={snapshot['revision']}",
            "acceptance_contract": acceptance_contract(),
            "paper": get_paper(session, paper_id),
            "sections": sections,
            "section_coverage": section_coverage(sections, files),
            "section_write_contract": section_write_contract(paper_id),
            "templates": reaction_templates(),
            "files": files,
            "file_history": lifecycle['items'],
            "file_lifecycle": lifecycle,
            "assets": assets,
            "rows": rows,
            "coordinator_tools": {"python_module": "app.services.literature_tools", "read_only_cli": "scripts/literature_tools.py",
                                  "pdf_chunk_rules": "/api/rebuild/files/{file_id}/text-pages", "automatic_scientific_decisions": False},
            "read_endpoints": {"text_pages": "/api/rebuild/files/{file_id}/text-pages?offset=0&limit=5",
                               "render_page": "/api/rebuild/files/{file_id}/render-page?page=1&dpi=120",
                               "asset_inventory": "/api/rebuild/files/{file_id}/asset-inventory", "queue": "/api/rebuild/queue"},
            "write_endpoints": {
                "asset_explanations": "/api/rebuild/papers/{paper_id}/assets/import",
                "asset_crop": "/api/rebuild/papers/{paper_id}/assets/crop",
                "data_rows": "/api/rebuild/rows/import",
                "data_rows_preflight": "/api/rebuild/rows/import/preflight",
                "coverage_plan": f"/api/rebuild/papers/{paper_id}/coverage-plan",
                "row_identity_correction": "/api/rebuild/rows/{row_id}/identity",
                "source_correction": "/api/rebuild/sources/{source_id}",
                "source_asset_binding": "/api/rebuild/sources/{source_id}/asset-binding",
                "source_lifecycle": "/api/rebuild/sources/{source_id}/lifecycle",
                "section_order": f"/api/rebuild/papers/{paper_id}/sections/order",
                "value_dedup_preview": "/api/rebuild/papers/{paper_id}/value-dedup/preview",
                "value_dedup_apply": "/api/rebuild/papers/{paper_id}/value-dedup/apply",
                "processing_status": "/api/rebuild/papers/{paper_id}/processing-status",
                "sections": "/api/rebuild/papers/{paper_id}/sections/import",
            },
            "source_correction_contract": source_correction_contract(),
            "value_dedup_contract": dedup_contract(),
            "processing_status_contract": {
                "method": "PUT", "authentication": "workbench session",
                "body": {"run_id": "stable id for this attempt", "agent": "AI name", "status": "parsing then parsed/partial/failed", "message": "completion or remaining work", "file_ids": "IDs of files covered by this completed attempt"},
                "rule": "Report parsing before work and parsed/partial/failed after committed writes, using the same run_id. Terminal runs are immutable; retry identical terminal status, use a new run_id for a new attempt. Specify file_ids for files fully processed; newly attached SI remains unparsed until covered. Empty completion requires no_extractable_content_reason. Processing completion does not mean scientific acceptance.",
            },
            "row_identity_contract": {
                "dimensions": ["adsorbate", "intermediate", "reaction_step", "adsorption_site", "identity_context"],
                "rule": "A dimension declared in row top-level, condition, properties or a non-missing value must match exactly. Omit redundant declarations or make every declaration identical from evidence. Contradictions are rejected; no synonym normalization is applied.",
                "pending_recovery": "Use the returned canonical row_key at rows/import to supply all pending dimension values; keep existing identity and conditions unchanged.",
                "confirmed_correction": "PUT row_identity_correction with paper_id, canonical row_key, fresh identity_revision as expected_revision, new request_id, whitelisted changes, reason and same-paper registered file/asset evidence. condition replaces the entire condition object. Requires workbench session and X-CSRF-Token.",
                "retry": "Persist the exact request before sending; retry identical body and request_id after unknown outcomes. request_id is scoped to row_id. GET and compare the current row after a replay; applied_revision may be older than current_revision.",
            },
        }
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/quality")
async def rebuild_quality_report(
    paper_id: UUID,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200),
    include_asset_mechanics: bool = Query(default=False),
    asset_offset: int = Query(default=0, ge=0), asset_limit: int = Query(default=32, ge=1, le=32),
    expected_coverage_revision: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    expected_revision: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session),
    _auth: SessionState = Depends(_require_work_package_session),
):
    from app.services.rebuild_quality import quality_report
    from app.services.rebuild_acceptance import current_snapshot
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session, paper_id)
        snapshot = current_snapshot(session, paper_id)[0]
        if include_asset_mechanics and asset_offset and expected_revision is None:
            raise RebuildWorkflowError("work_package_revision_required")
        if expected_revision is not None and snapshot["revision"] != expected_revision:
            raise RebuildWorkflowError("work_package_revision_changed")
        result = quality_report(session, paper_id, get_settings(), snapshot, offset=offset, limit=limit,
                                include_asset_mechanics=include_asset_mechanics,
                                asset_offset=asset_offset, asset_limit=asset_limit)
        if offset and result['coverage_plan']['plan_revision'] is not None and expected_coverage_revision is None:
            raise RebuildWorkflowError('coverage_read_revision_required')
        if expected_coverage_revision is not None and result['quality_revision'] != expected_coverage_revision:
            raise RebuildWorkflowError('coverage_read_revision_changed')
        return result
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


from app.schemas.extraction_coverage import CoveragePlanRequest


@router.get("/papers/{paper_id}/coverage-plan")
async def read_coverage_plan(paper_id: UUID,
    offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
    expected_revision: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.services.extraction_coverage import report
    from app.services.rebuild_acceptance import current_snapshot
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session, paper_id)
        return report(session, paper_id, current_snapshot(session, paper_id)[0], offset=offset, limit=limit,
                      expected_revision=expected_revision)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/coverage-plan/items/{item_key}/output")
async def read_coverage_output(paper_id: UUID, item_key: str,
    expected_revision: str = Query(pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.services.extraction_coverage import output_detail
    from app.services.rebuild_acceptance import current_snapshot
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session,paper_id)
        return output_detail(session,paper_id,current_snapshot(session,paper_id)[0],item_key,expected_revision)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/coverage-plan/preview")
async def preview_coverage_plan(paper_id: UUID, payload: CoveragePlanRequest,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_section_write_session)):
    from app.services.extraction_coverage import evaluate, latest
    from app.services.rebuild_acceptance import current_snapshot
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session, paper_id)
        snapshot = current_snapshot(session, paper_id)[0]; previous = latest(session, paper_id)
        if payload.expected_plan_revision != (previous.payload['plan_revision'] if previous else None):
            raise RebuildWorkflowError('coverage_plan_revision_changed')
        if payload.expected_output_revision != snapshot['revision']:
            raise RebuildWorkflowError('coverage_output_revision_changed')
        plan = payload.model_dump(mode='json')
        return evaluate(session, paper_id, plan, snapshot, previous=previous.payload['plan'] if previous else None)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.put("/papers/{paper_id}/coverage-plan")
def write_coverage_plan(paper_id: UUID, payload: CoveragePlanRequest, request: Request,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_section_write_session)):
    from app.services.extraction_coverage import save_plan
    from app.services.rebuild_acceptance import current_snapshot, _locked_paper
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        _locked_paper(session, paper_id)
        receipt, report = save_plan(session, paper_id, payload, current_snapshot(session, paper_id, force_physical=True)[0], actor=auth.username)
        if receipt is None:
            raise HTTPException(status_code=422, detail={'code':'coverage_plan_invalid', 'errors':report['errors']})
        session.commit()
        return receipt
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


from app.schemas.staged_extraction import Stage, StageCheckRequest


def _stage_context(session, paper_id, actor, *, force=False):
    from app.services.rebuild_acceptance import current_snapshot
    from app.services.staged_extraction import context
    _required_paper(session, paper_id)
    return context(session, paper_id, current_snapshot(session, paper_id, force_physical=force)[0], actor)


@router.get("/papers/{paper_id}/staged-work-package")
async def read_staged_package(paper_id: UUID, stage: Stage = "source_scope",
    view: Literal["items", "receipts", "rows_contract", "table_contract", "section_contract"] = "items", offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    expected_revision: str | None = Query(None, pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.services.staged_extraction import package
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return package(session, paper_id, _stage_context(session,paper_id,auth.username), stage,view,offset,limit,expected_revision)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/extraction-import-receipts/{receipt_id}")
async def read_extraction_import_receipt(paper_id: UUID, receipt_id: UUID,
    offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    expected_revision: str | None = Query(None, pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.services.staged_extraction import receipt_errors
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        _required_paper(session,paper_id)
        return receipt_errors(session,paper_id,receipt_id,offset=offset,limit=limit,expected_revision=expected_revision)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/extraction-checks")
async def read_extraction_checks(paper_id: UUID,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.services.staged_extraction import record_summary, contract
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        ctx=_stage_context(session,paper_id,auth.username)
        return {"paper_id":str(paper_id),"binding":ctx['binding'],"check_revision":ctx['check_revision'],
                "records":[record_summary(e,ctx) for e in ctx['records']],"contract":contract(paper_id),
                "read_only":True,"scientific_acceptance":"not_implied"}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/extraction-checks/{record_id}")
async def read_extraction_check(paper_id: UUID, record_id: UUID,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_work_package_session)):
    from app.db.models import AuditLog
    from app.services.staged_extraction import record_summary, EVENT, VERSION
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        ctx=_stage_context(session,paper_id,auth.username)
        record=session.scalar(select(AuditLog).where(AuditLog.id==record_id,AuditLog.paper_id==paper_id,AuditLog.action==EVENT,AuditLog.source==VERSION))
        if not record:raise HTTPException(404,detail="stage_check_not_found")
        return record_summary(record,ctx,full=True)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/extraction-checks/preview")
async def preview_extraction_check(paper_id: UUID, payload: StageCheckRequest,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_section_write_session)):
    from app.services.staged_extraction import assess
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        ctx=_stage_context(session,paper_id,auth.username)
        if payload.expected_check_revision!=ctx['check_revision']:raise RebuildWorkflowError('stage_check_revision_changed')
        if payload.expected_binding.model_dump(mode='json')!=ctx['binding']:raise RebuildWorkflowError('stage_binding_changed')
        body=payload.model_dump(mode='json')
        assessment,binding=assess(session,paper_id,ctx,source_scope=body['source_scope'],item_keys=body['item_keys'],
            receipt_ids=body['receipt_ids'],unknown_requests=body['unknown_requests'],statement=body['review_statement'])
        return {"assessment":assessment,"binding_if_saved":binding,"read_only":True,"scientific_acceptance":"not_implied"}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.put("/papers/{paper_id}/extraction-checks")
def write_extraction_check(paper_id: UUID, payload: StageCheckRequest, request: Request,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_section_write_session)):
    from app.services.staged_extraction import save_check
    from app.services.rebuild_acceptance import _locked_paper
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        _locked_paper(session,paper_id)
        receipt,assessment=save_check(session,paper_id,payload,_stage_context(session,paper_id,auth.username,force=True),auth.username)
        if receipt is None:raise HTTPException(422,detail={"code":"stage_check_references_invalid","errors":assessment['reference_errors']})
        session.commit()
        return receipt
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


from app.schemas.history_reconciliation import HistoryRegistration


def _history_context(session, paper_id, settings, actor, force=False):
    from app.services.history_reconciliation import context
    from app.services.rebuild_acceptance import current_snapshot
    _required_paper(session,paper_id)
    return context(session,paper_id,settings,current_snapshot(session,paper_id,force_physical=force)[0],actor)


@router.get("/papers/{paper_id}/history-reconciliation")
async def read_history_reconciliation(paper_id: UUID, view: Literal["attempts","reports"] = "attempts",
    offset: int = Query(0,ge=0), limit: int = Query(25,ge=1,le=100),
    expected_revision: str | None = Query(None,pattern="^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_work_package_session)):
    from app.services.history_reconciliation import package
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return package(paper_id,_history_context(session,paper_id,settings,auth.username),view,offset,limit,expected_revision)
    except RebuildWorkflowError as exc:raise _http_error(exc) from exc


@router.get("/papers/{paper_id}/history-reconciliation/records/{record_id}")
async def read_history_record(paper_id: UUID, record_id: UUID,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_work_package_session)):
    from app.services.history_reconciliation import record_summary
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        ctx=_history_context(session,paper_id,settings,auth.username)
        record=next((e for e in ctx['records'] if e.id==record_id),None)
        if record is None:raise HTTPException(404,detail="history_record_not_found")
        return record_summary(record,ctx,full=True)
    except RebuildWorkflowError as exc:raise _http_error(exc) from exc


@router.post("/papers/{paper_id}/history-reconciliation/preview")
async def preview_history_registration(paper_id: UUID, payload: HistoryRegistration,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_authenticated_session)):
    from app.services.history_reconciliation import preview
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return preview(payload,_history_context(session,paper_id,settings,auth.username))
    except RebuildWorkflowError as exc:raise _http_error(exc) from exc


@router.put("/papers/{paper_id}/history-reconciliation")
def save_history_registration(paper_id: UUID, payload: HistoryRegistration, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_authenticated_session)):
    from app.services.history_reconciliation import save
    from app.services.rebuild_acceptance import _locked_paper
    try:
        # Historical supersession is an append-only audit declaration. It never
        # releases current occupancy or mutates scientific results, so an unknown
        # native outcome must not prevent recording the evidence-bound audit.
        _locked_paper(session,paper_id)
        receipt,result=save(session,paper_id,payload,_history_context(session,paper_id,settings,auth.username,force=True),auth.username)
        if receipt is None:raise HTTPException(422,detail={'code':'history_registration_invalid','errors':result['errors']})
        session.commit()
        return receipt
    except (RebuildWorkflowError, ValueError) as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


async def _require_file_write_session(request: Request, session: Session = Depends(get_db_session)) -> SessionState:
    auth = await _require_work_package_session(request)
    if not origin_allowed(request, get_settings()):
        raise HTTPException(403, "cross_origin_request_blocked")
    if not csrf_token_valid(request, auth):
        raise HTTPException(403, "csrf_token_invalid")
    await _guard_request_work(session, request, auth)
    return auth


from app.schemas.rebuild import RebuildFileLifecycleRequest


@router.post("/papers/{paper_id}/files/lifecycle")
def select_rebuild_file(paper_id: UUID, payload: RebuildFileLifecycleRequest, request: Request,
    session: Session = Depends(get_db_session), auth: SessionState = Depends(_require_section_authenticated_session)):
    from app.services.rebuild_file_lifecycle import _hash, locked_replay, validate_change, apply_change
    body = payload.model_dump(mode='json')
    try:
        fingerprint = _hash({'body': payload.model_dump(mode='json', exclude_unset=True), 'operation': 'select_existing'})
        replay = locked_replay(session, paper_id, body['request_id'], fingerprint, auth.username,
                               _request_task_id(request))
        if replay is not None:
            return replay
        before = validate_change(session, paper_id, body)
        record = get_file(session, payload.file_id)
        if record.paper_id != paper_id or record.role != payload.role:
            raise HTTPException(422, 'selected_file_not_in_paper_role')
        result = apply_change(session, paper_id, body, auth.username, fingerprint, record, before, created=False)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except Exception:
        session.rollback()
        raise


@router.post("/papers/{paper_id}/files")
async def associate_rebuild_pdf(
    paper_id: UUID, request: Request, role: Literal['main', 'si'] = Form(...), file: UploadFile = File(...),
    request_id: UUID = Form(...), expected_revision: str = Form(..., pattern=r'^[0-9a-f]{64}$'),
    action: Literal['add', 'supersede'] = Form(...), reason: str = Form(..., min_length=1, max_length=2000),
    supersede_file_ids: str = Form(default='[]'),
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_file_write_session),
) -> dict[str, Any]:
    from app.services.rebuild_file_lifecycle import _hash, locked_replay, validate_change, apply_change
    form = await request.form()
    allowed = {'role', 'file', 'request_id', 'expected_revision', 'action', 'reason', 'supersede_file_ids'}
    if set(form.keys()) - allowed or any(len(form.getlist(key)) != 1 for key in form.keys()):
        raise HTTPException(422, 'unexpected_or_duplicate_file_fields')
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, 'only_pdf_uploads_are_supported')
    try:
        targets = json.loads(supersede_file_ids)
        if not isinstance(targets, list) or len(targets) > 100:
            raise ValueError()
        targets = [str(UUID(item)) for item in targets]
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'invalid_supersede_file_ids')
    if not reason.strip():
        raise HTTPException(422, 'reason_required')
    digest = hashlib.sha256(); size = 0
    while chunk := await file.read(1024 * 1024):
        digest.update(chunk); size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, 'file_too_large')
    await file.seek(0)
    safe_name = Path(file.filename).name
    body = {'request_id': str(request_id), 'expected_revision': expected_revision, 'role': role,
            'action': action, 'reason': reason, 'supersede_file_ids': targets,
            'original_filename': safe_name, 'sha256': digest.hexdigest(), 'file_size': size}
    fingerprint = _hash({'body': body, 'provided_fields': sorted(form.keys()),
                         'supersede_file_ids_raw': supersede_file_ids, 'operation': 'upload'})
    def persist(worker):
        replay = locked_replay(worker, paper_id, str(request_id), fingerprint, auth.username)
        if replay is not None:
            return replay
        before = validate_change(worker, paper_id, body)
        # Validate CAS/ownership and duplicates before persisting any upload.
        from app.db.models import RebuildPaperFile
        record = worker.scalar(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id,
            RebuildPaperFile.role == role, RebuildPaperFile.sha256 == digest.hexdigest()))
        created = record is None
        if record is not None:
            if str(record.id) in targets:
                raise HTTPException(409, 'file_cannot_supersede_itself')
            if any(f['id'] == str(record.id) and f['lifecycle_status'] == 'superseded' for f in before['items']):
                raise HTTPException(409, 'superseded_file_cannot_be_reactivated')
        if role == 'main' and any(f['role'] == 'main' and f['lifecycle_status'] == 'active'
            and f['id'] not in targets and (record is None or f['id'] != str(record.id)) for f in before['items']):
            raise HTTPException(409, 'main_source_requires_explicit_supersede')
        if created:
            # A content-addressed immutable destination avoids orphan duplicates on retry.
            relative = f'rebuild_sources/{paper_id}/{role}/{digest.hexdigest()}.pdf'
            path = settings.storage_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest.hexdigest():
                    raise HTTPException(409, 'immutable_upload_content_mismatch')
            else:
                with path.open('xb') as handle:
                    while chunk := file.file.read(1024 * 1024):
                        handle.write(chunk)
            record = RebuildPaperFile(paper_id=paper_id, role=role, storage_path=relative,
                original_filename=safe_name, sha256=digest.hexdigest(), file_size=size)
            worker.add(record); worker.flush()
        result = apply_change(worker, paper_id, body, auth.username, fingerprint, record, before, created=created)
        return result

    # Upload I/O above is async. The completed spool is handed exclusively to
    # this worker until run_phase finishes (also on cancellation); the request
    # Session never enters the worker and no DB transaction crosses an await.
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        return await run_phase(session, persist)
    except LockConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/papers/{paper_id}/files")
async def rebuild_files(paper_id: UUID, session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_work_package_session)) -> dict[str, Any]:
    from app.services.rebuild_file_lifecycle import file_set
    try:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        return file_set(session, paper_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}/text-pages")
async def read_rebuild_text_pages(
    file_id: UUID, offset: int = Query(default=0, ge=0), limit: int = Query(default=5, ge=1, le=20),
    source_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    reading_pdf_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(_require_work_package_session),
):
    from app.services.rebuild_read_cache import paginated_text, chunk_packet_contract
    try:
        result = paginated_text(get_file(session, file_id), settings, offset=offset, limit=limit,
                                expected_source=source_sha256, expected_reading=reading_pdf_sha256)
        result["next_page"] = (f"/api/rebuild/files/{file_id}/text-pages?offset={result['next_offset']}&limit={limit}&source_sha256={result['source_sha256']}&reading_pdf_sha256={result['reading_pdf_sha256']}"
                               if result["has_more"] else None)
        return {**result, "chunk_contract": chunk_packet_contract()}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}/render-page")
async def read_rebuild_rendered_page(
    file_id: UUID, page: int = Query(ge=1), dpi: int = Query(default=120, ge=72, le=160),
    source_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    reading_pdf_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(_require_work_package_session),
):
    from app.services.rebuild_read_cache import rendered_page
    try:
        path, identity = rendered_page(get_file(session, file_id), settings, page=page, dpi=dpi,
                                       expected_source=source_sha256, expected_reading=reading_pdf_sha256)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store",
                            "X-Source-SHA256": identity["source_sha256"], "X-Reading-PDF-SHA256": identity["reading_pdf_sha256"],
                            "ETag": '"' + identity["render_sha256"] + '"'})
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}/asset-inventory")
async def read_rebuild_asset_inventory(
    file_id: UUID, inventory_revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    _auth: SessionState = Depends(_require_work_package_session),
):
    from app.services.rebuild_read_cache import asset_inventory
    try:
        return asset_inventory(session, get_file(session, file_id), settings, inventory_revision=inventory_revision)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}/preview")
async def rebuild_file_preview(
    file_id: UUID,
    source_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    reading_pdf_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    page_basis: str | None = Query(default=None, pattern=r"^(source_pdf|derived_pdf)$"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        record = get_file(session, file_id)
        from app.services.rebuild_file_preview import reading_identity
        path, identity = reading_identity(record, settings)
        for key, expected in (("source_sha256", source_sha256), ("reading_pdf_sha256", reading_pdf_sha256), ("page_basis", page_basis)):
            if expected is not None and expected != identity[key]:
                raise RebuildWorkflowError(("source_sha256_mismatch" if key == "source_sha256" else "source_" + key + "_mismatch"))
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{Path(record.original_filename).stem}.pdf",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/files/{file_id}/download")
async def rebuild_file_download(
    file_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        record = get_file(session, file_id)
        path, media_type = resolve_original_download(record, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type=media_type,
        filename=record.original_filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/papers/{paper_id}/source-pdf/preview")
async def rebuild_paper_source_pdf(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        paper = _required_paper(session, paper_id)
        from app.services.rebuild_file_lifecycle import file_set
        state = file_set(session, paper_id)
        if state['ambiguous_main']:
            raise HTTPException(409, 'preferred_main_source_ambiguous')
        if state['preferred_main_id']:
            path = resolve_reading_pdf(get_file(session, UUID(state['preferred_main_id'])), settings)
        elif any(f['role'] == 'main' for f in state['items']):
            raise HTTPException(409, 'preferred_main_source_unavailable')
        else:
            path = resolve_paper_source_pdf(paper, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    filename = f"{paper.paper_code or paper.id}.pdf"
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="inline",
    )


@router.get("/papers/{paper_id}/assets")
async def rebuild_assets(paper_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return {"items": list_assets(session, paper_id)}
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


async def _audited_response(session, request, payload, paper_id, operation, actor, mutate, *, receipt_serializer=None):
    from app.services.rebuild_mutation_audit import audit_ids, run_audited
    from app.services.paper_db_phase import LockConflictError, run_phase
    try:
        request_id, task_id = audit_ids(request, payload)
        def execute(worker):
            return run_audited(worker, paper_id=paper_id, operation=operation, payload=payload,
                               actor=actor, request_id=request_id, task_id=task_id,
                               mutate=lambda: mutate(worker),
                               **({"receipt_serializer": receipt_serializer} if receipt_serializer else {}))
        return await run_phase(session, execute)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    except LockConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        raise


@router.post("/papers/{paper_id}/assets/import")
async def import_rebuild_assets(
    paper_id: UUID, payload: RebuildAssetsImportRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_authenticated_session),
) -> dict[str, Any]:
    return await _audited_response(session, request, payload, paper_id, "assets.import", auth.username,
                                   lambda worker: _import_assets(worker, paper_id, payload, settings))


def _import_assets(session, paper_id, payload, settings):
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.assets):
        try:
            with session.begin_nested():
                asset_payload = RebuildVisualAssetRequest.model_validate(raw)
                asset, created = upsert_visual_asset(
                    session,
                    paper_id=paper_id,
                    payload=asset_payload,
                    settings=settings,
                )
            from app.services.rebuild_mutation_audit import capture_item
            item_events = capture_item(session, index=index, entity_id=asset.id)
            results.append(
                {
                    "index": index,
                    "audit_event_ids": item_events,
                    "asset_key": asset.asset_key,
                    "asset_id": str(asset.id),
                    "created": created,
                    "outcome": "created" if created else "updated",
                }
            )
        except (ValidationError, RebuildWorkflowError, DataError, IntegrityError) as exc:
            reason = ("database_constraint_rejected" if isinstance(exc, (DataError, IntegrityError)) else
                      json.dumps(exc.errors(include_input=False, include_context=False), ensure_ascii=False) if isinstance(exc, ValidationError) else str(exc))
            errors.append({"index": index, "outcome": "rejected", "error": reason})
    return {
        "paper_id": str(paper_id),
        "assets_submitted": len(payload.assets),
        "assets_imported": len(results),
        "results": results,
        "errors": errors,
    }


@router.post("/papers/{paper_id}/assets")
async def create_or_update_rebuild_asset(
    paper_id: UUID, payload: RebuildVisualAssetRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_authenticated_session),
) -> dict[str, Any]:
    def mutate(worker):
        asset, created = upsert_visual_asset(worker, paper_id=paper_id, payload=payload, settings=settings)
        from app.services.rebuild_mutation_audit import capture_item
        events = capture_item(worker, index=None, entity_id=asset.id)
        return {"asset": serialize_asset(asset), "created": created, "audit_event_ids": events}
    return await _audited_response(session, request, payload, paper_id, "assets.upsert", auth.username, mutate)


@router.post("/papers/{paper_id}/assets/crop")
async def create_rebuild_asset_from_pdf_crop(
    paper_id: UUID, payload: RebuildVisualAssetCropRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_authenticated_session),
) -> dict[str, Any]:
    return await _audited_response(session, request, payload, paper_id, "assets.crop", auth.username,
                                   lambda worker: _crop_asset(worker, paper_id, payload, settings))


def _crop_asset(session, paper_id, payload, settings):
    try:
        if payload.file_id is None:
            raise HTTPException(status_code=400, detail="file_id_required_for_crop")
        file_record = get_file(session, payload.file_id)
        if file_record.paper_id != paper_id:
            raise HTTPException(status_code=400, detail="file_belongs_to_other_paper")
        from app.services.rebuild_file_preview import reading_identity
        pdf_path, page_identity = reading_identity(file_record, settings)
        # Validate complete metadata/reading before rendering or touching the fixed PNG.
        base_payload = payload.model_dump(exclude={"regions"})
        if "reading_explanation" not in payload.model_fields_set:
            base_payload.pop("reading_explanation", None)
        base_payload["page_numbers"] = sorted({region.page_number for region in payload.regions})
        # Persist the exact ordered render regions, rather than a caller-supplied
        # optional bbox that can disagree with the pixels (or be absent).
        base_payload["bbox"] = {"units": "normalized_page_fraction",
                               "regions": [region.model_dump() for region in payload.regions]}
        supplied_identity = (payload.provenance or {}).get("page_identity")
        if supplied_identity is not None and supplied_identity != page_identity:
            raise RebuildWorkflowError("source_page_identity_mismatch")
        base_payload["provenance"] = {**(payload.provenance or {}), "page_identity": page_identity}
        source_payload = RebuildVisualAssetRequest(**base_payload)
        prepare_visual_asset(session, paper_id=paper_id, payload=source_payload, settings=settings)

        import fitz
        from PIL import Image

        images: list[Image.Image] = []
        with fitz.open(pdf_path) as document:
            for region in payload.regions:
                if region.page_number > document.page_count:
                    raise HTTPException(status_code=400, detail="crop_page_out_of_range")
                page = document[region.page_number - 1]
                x0, y0, x1, y1 = region.bbox
                if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                    raise HTTPException(status_code=400, detail="crop_bbox_must_be_normalized")
                rectangle = fitz.Rect(
                    page.rect.x0 + x0 * page.rect.width,
                    page.rect.y0 + y0 * page.rect.height,
                    page.rect.x0 + x1 * page.rect.width,
                    page.rect.y0 + y1 * page.rect.height,
                )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), clip=rectangle, alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                images.append(image.convert("RGB"))

        if len(images) == 1:
            output_image = images[0]
        else:
            width = max(image.width for image in images)
            total_height = sum(image.height for image in images) + 12 * (len(images) - 1)
            output_image = Image.new("RGB", (width, total_height), "white")
            offset = 0
            for image in images:
                output_image.paste(image, (0, offset))
                offset += image.height + 12

        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", payload.asset_key).strip(".-") or "asset"
        target_dir = settings.storage_paths["figures"] / "rebuild" / str(paper_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        output_image.save(buffer, format="PNG", optimize=True)
        png_bytes = buffer.getvalue()
        png_sha256 = hashlib.sha256(png_bytes).hexdigest()
        image_path = target_dir / f"{safe_key[:96]}-{png_sha256}.png"
        relative_path = image_path.resolve().relative_to(settings.storage_root.resolve()).as_posix()
        pixels_changed = not image_path.is_file() or image_path.read_bytes() != png_bytes
        base_payload["image_path"] = relative_path
        source_payload = RebuildVisualAssetRequest(**base_payload)
        prepared = prepare_visual_asset(session, paper_id=paper_id, payload=source_payload,
                                        settings=settings, force_source_changed=pixels_changed)
        # Source/reading rejection above leaves original PNG and DB untouched.
        # Immutable content paths keep the previous image intact on DB/audit rollback.
        # A failed commit can leave an unreferenced file, never a false success.
        if not image_path.exists():
            with image_path.open("xb") as handle:
                handle.write(png_bytes)
        elif image_path.read_bytes() != png_bytes:
            raise RebuildWorkflowError("crop_content_hash_collision")
        asset, created = upsert_visual_asset(session, paper_id=paper_id, payload=source_payload,
                                            settings=settings, prepared=prepared)
    except HTTPException:
        raise
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"crop_failed:{type(exc).__name__}") from exc
    from app.services.rebuild_mutation_audit import capture_item
    events = capture_item(session, index=None, entity_id=asset.id)
    return {"asset": serialize_asset(asset), "created": created, "rendered_pages": len(payload.regions), "audit_event_ids": events}



@router.get("/assets/{asset_id}")
async def rebuild_asset_image(
    asset_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    try:
        asset = get_asset(session, asset_id)
        path = resolve_asset_image_path(asset, settings)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="asset_image_not_available")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.get("/rows")
async def rebuild_data_rows(
    reaction: str | None = None,
    material: str | None = None,
    material_family: str | None = None,
    active_site_type: str | None = None,
    data_type: str | None = None,
    paper_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return list_rows(
        session,
        reaction=reaction,
        material=material,
        material_family=material_family,
        active_site_type=active_site_type,
        data_type=data_type,
        paper_id=paper_id,
        limit=limit,
        offset=offset,
    )


@router.post("/rows/import")
async def import_rebuild_rows(
    payload: RebuildRowsImportRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_authenticated_session),
) -> dict[str, Any]:
    return await _audited_response(session, request, payload, payload.paper_id, "rows.import", auth.username,
                                   lambda worker: _import_rows(worker, payload))


@router.post("/rows/import/preflight")
async def preflight_rebuild_rows(
    payload: RebuildRowsImportRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session),
) -> dict[str, Any]:
    # Separate session owns its entire transaction; never flush/rollback the
    # dependency session or reuse its audit context. No receipt/id reservation.
    from app.services.rebuild_import_recovery import simulated_result
    from app.services.rebuild_mutation_audit import audit_ids
    try:
        audit_ids(request, payload)  # Syntax/header consistency only; no reservation.
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    # get_bind may be a Connection in dependency overrides. Always acquire a
    # separate Engine connection, never join an enclosing caller transaction.
    from app.services.paper_db_phase import LockConflictError, run_phase
    def simulate(worker):
        try:
            return simulated_result(_import_rows(worker, payload))
        finally:
            # Fixture-like simulation: no audit/scientific writes may commit.
            worker.rollback()
    try:
        return await run_phase(session, simulate)
    except LockConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _import_rows(session, payload):
    try:
        _required_paper(session, payload.paper_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.rows):
        try:
            with session.begin_nested():
                row_payload = RebuildDataRowInput.model_validate(raw)
                result = import_data_row(session, payload.paper_id, row_payload)
            from app.services.rebuild_mutation_audit import capture_item
            result["audit_event_ids"] = capture_item(session, index=index, entity_id=result["row_id"])
            result["row_index"] = index
            results.append(result)
            for part in ("values", "properties"):
                for detail in result[part]:
                    if detail["outcome"] in {"rejected", "conflict", "pending"}:
                        errors.append({"row_index": index, "row_id": result["row_id"], "row_key": result["row_key"],
                                       "part": part, **detail})
            if result.get("identity_status") == "pending":
                reason = {
                    "row_index": index, "row_id": result["row_id"], "row_key": result["row_key"], "part": "row",
                    "outcome": "pending", "error": "identity_pending:correct_dimension_using_returned_row_key",
                    "pending_identity_fields": result["pending_identity_fields"],
                }
                result["pending_reason"] = reason["error"]
                errors.append(reason)
        except (ValidationError, RebuildWorkflowError, DataError, IntegrityError) as exc:
            reason = ("database_constraint_rejected" if isinstance(exc, (DataError, IntegrityError)) else
                      json.dumps(exc.errors(include_input=False, include_context=False), ensure_ascii=False) if isinstance(exc, ValidationError) else str(exc))
            detail = {"row_index": index, "row_key": raw.get("row_key") if isinstance(raw, dict) else None,
                      "outcome": "rejected", "persisted": False, "complete": False, "error": reason}
            results.append(detail)
            errors.append(detail)
    counts = {name: sum(r["outcome"] == name for r in results)
              for name in ("written", "duplicate", "partial", "rejected")}
    from app.services.rebuild_import_recovery import import_summary, recovery_advice
    summary = import_summary(session, results)
    return {
        "outcome": summary["outcome"], "summary": summary,
        "recovery": recovery_advice(session, payload, results),
        "paper_id": str(payload.paper_id),
        "rows_submitted": len(payload.rows),
        "rows_imported": counts["written"],
        "rows_duplicate": counts["duplicate"],
        "rows_partial": counts["partial"],
        "rows_rejected": counts["rejected"],
        "complete": all(result["complete"] for result in results),
        "results": results,
        "errors": errors,
    }



async def _require_identity_correction_session(request: Request, session: Session = Depends(get_db_session)) -> SessionState:
    # Adapt the helper's optional Settings argument explicitly. Exposing it
    # directly to Depends would make FastAPI treat Settings as a second body.
    auth = await require_session(request, settings=get_settings())
    # Python's JSON parser accepts NaN/Infinity. Reject them before FastAPI's
    # validation error handler tries to serialize a non-finite error input.
    try:
        body = await request.json()
        json.dumps(body, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="identity_body_must_be_finite_json") from exc
    # Authenticate the mutation before holding the shared paper occupancy lock.
    if not origin_allowed(request, get_settings()):
        raise HTTPException(403, "cross_origin_request_blocked")
    if not csrf_token_valid(request, auth):
        raise HTTPException(403, "csrf_token_invalid")
    await _guard_request_work(session, request, auth, body)
    return auth


@router.put("/rows/{row_id}/identity")
def correct_rebuild_row_identity(
    row_id: UUID,
    payload: RebuildRowIdentityCorrectionRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_identity_correction_session),
) -> dict[str, Any]:
    """Workbench owners may correct one confirmed row with evidence and CAS.

    Explicit Origin/CSRF checks are required even where the application has
    not enabled global session middleware. Share/MCP-only callers are denied.
    """
    if not origin_allowed(request, get_settings()):
        raise HTTPException(status_code=403, detail="cross_origin_request_blocked")
    if not csrf_token_valid(request, auth):
        raise HTTPException(status_code=403, detail="csrf_token_invalid")
    try:
        _guard_sync_write(session, payload.paper_id, auth.username, _request_task_id(request))
        result = correct_row_identity(session, row_id=row_id, payload=payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        error = str(exc)
        if error in {"paper_not_found", "row_not_found", "file_not_found", "asset_not_found"}:
            status = 404
        elif error.startswith(("identity_revision_conflict", "identity_target_collision:",
                               "identity_target_ambiguous_collision:",
                               "identity_request_id_reused", "canonical_row_key_mismatch",
                               "identity_pending:")):
            status = 409
        else:
            status = 400
        raise HTTPException(status_code=status, detail=error) from exc
    except ValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail="invalid_corrected_identity") from exc


@router.post("/analysis")
def rebuild_analysis(
    payload: RebuildAnalysisRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = analyze(session, payload)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    session.commit()
    return result


@router.post("/papers/{paper_id}/history-transfer")
async def transfer_history(paper_id: UUID, payload: LegacyTransferRequest,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_identity_correction_session)) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="旧提取数据已停用，请从原 PDF 通过新 AI 提取流程重新取数。")


@router.get("/sources/{source_id}")
async def read_rebuild_source(source_id: UUID, session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_work_package_session)) -> dict[str, Any]:
    try:
        return source_state(session, source_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc


@router.put("/sources/{source_id}")
def correct_rebuild_source(source_id: UUID, payload: RebuildSourceCorrectionRequest, request: Request,
    session: Session = Depends(get_db_session), settings: Settings = Depends(get_settings),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    try:
        _guard_sync_write(session, payload.paper_id, auth.username, _request_task_id(request))
        result = correct_source(session, source_id, payload, settings, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except (IntegrityError, DataError) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="source_database_constraint_conflict") from exc
    except Exception:
        session.rollback()
        raise


@router.post("/sources/{source_id}/asset-binding")
def bind_rebuild_source_asset(source_id: UUID, payload: RebuildSourceAssetBindingRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    try:
        _guard_sync_write(session, payload.paper_id, auth.username, _request_task_id(request))
        result = bind_source_asset(session, source_id, payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except (IntegrityError, DataError) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="source_database_constraint_conflict") from exc
    except Exception:
        session.rollback()
        raise


@router.post("/sources/{source_id}/lifecycle")
def change_rebuild_source_lifecycle(source_id: UUID, payload: RebuildSourceLifecycleRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    try:
        _guard_sync_write(session, payload.paper_id, auth.username, _request_task_id(request))
        result = deactivate_source(session, source_id, payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    except (IntegrityError, DataError) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="source_database_constraint_conflict") from exc
    except Exception:
        session.rollback()
        raise


@router.get("/values/{value_id}")
async def read_rebuild_value(value_id: UUID, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    value = session.get(RebuildDataValue, value_id)
    if value is None:
        raise HTTPException(status_code=404, detail="value_not_found")
    sources = session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)).all()
    return serialize_value(value, sources)


async def _require_value_write_session(request: Request, session: Session = Depends(get_db_session)) -> SessionState:
    try:
        return await _require_section_write_session(request, session)
    except HTTPException as exc:
        # Keep the value endpoint's existing finite-JSON error contract.
        if exc.status_code == 422 and exc.detail == "section_body_must_be_finite_json":
            raise HTTPException(status_code=422, detail="identity_body_must_be_finite_json") from exc
        raise


@router.put("/values/{value_id}")
def correct_rebuild_value(
    value_id: UUID,
    payload: RebuildValueCorrectionRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_value_write_session),
) -> dict[str, Any]:
    try:
        paper_id = session.execute(select(RebuildDataValue.paper_id).where(
            RebuildDataValue.id == value_id)).scalar_one_or_none()
        if paper_id is None:
            raise RebuildWorkflowError("value_not_found")
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        value = update_data_value(session, value_id=value_id, payload=payload, actor=auth.username)
    except RebuildWorkflowError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    sources = session.scalars(
        select(RebuildValueSource).where(RebuildValueSource.value_id == value.id)
    ).all()
    session.commit()
    return serialize_value(value, sources)


@router.get("/analysis/{run_id}/csv")
async def rebuild_analysis_csv(run_id: UUID, session: Session = Depends(get_db_session)) -> StreamingResponse:
    try:
        run = get_analysis_run(session, run_id)
    except RebuildWorkflowError as exc:
        raise _http_error(exc) from exc
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "paper_id",
            "material",
            "reaction",
            "data_type",
            "condition_json",
            run.result.get("x_field"),
            run.result.get("x_unit") or "",
            run.result.get("y_field"),
            run.result.get("y_unit") or "",
            "x_value_type",
            "y_value_type",
        ]
    )
    for point in run.result.get("points", []):
        writer.writerow(
            [
                point["paper_id"],
                point["material"],
                point["reaction"],
                point["data_type"],
                json.dumps(point.get("condition") or {}, ensure_ascii=False, sort_keys=True),
                point["x"],
                point["y"],
                point["x_value_type"],
                point["y_value_type"],
            ]
        )
    filename = f"literature-ai-analysis-{run_id}.csv"
    content = buffer.getvalue()
    buffer.seek(0)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Analysis-Sample-Count": str(run.sample_count),
            "X-Analysis-Run-Id": str(run.id),
        },
    )


@router.post("/papers/{paper_id}/value-dedup/preview")
async def preview_rebuild_value_dedup(paper_id: UUID, payload: RebuildDedupPreviewRequest,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    try:
        return preview_dedup(session, paper_id, payload)
    except RebuildWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/value-dedup/apply")
def apply_rebuild_value_dedup(paper_id: UUID, payload: RebuildDedupApplyRequest, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_write_session)) -> dict[str, Any]:
    try:
        _guard_sync_write(session, paper_id, auth.username, _request_task_id(request))
        result = apply_dedup(session, paper_id, payload, actor=auth.username)
        session.commit()
        return result
    except RebuildWorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (IntegrityError, DataError, OperationalError) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="dedup_database_constraint_or_lock_conflict") from exc
    except Exception:
        session.rollback()
        raise


# Deliberately separate from broad imports: each request changes one target.


@router.get("/papers/{paper_id}/assets/{target_id}/metadata")
async def asset_metadata_state(paper_id: UUID, target_id: UUID,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_work_package_session)):
    return metadata_state(session, paper_id, "assets", target_id)[1]


@router.get("/papers/{paper_id}/sections/{target_id}/metadata")
async def section_metadata_state(paper_id: UUID, target_id: UUID,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_work_package_session)):
    return metadata_state(session, paper_id, "sections", target_id)[1]


@router.put("/papers/{paper_id}/assets/{target_id}/metadata")
async def correct_asset_metadata(paper_id: UUID, target_id: UUID,
    payload: RebuildAssetMetadataCorrection, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_authenticated_session)):
    # Reject invalid JSON values before audit fingerprint serialization; otherwise
    # NaN/unencodable strings could fail outside the metadata validation gate.
    if "material_mapping" in payload.changes.model_fields_set:
        validate_material_mapping(payload.changes.material_mapping)
    return await _audited_response(session, request, payload, paper_id,
        f"assets.metadata.{target_id}", auth.username,
        lambda worker: correct_metadata(worker, paper_id, "assets", target_id, payload),
        receipt_serializer=lambda value: value)


@router.put("/papers/{paper_id}/sections/{target_id}/metadata")
async def correct_section_metadata(paper_id: UUID, target_id: UUID,
    payload: RebuildSectionMetadataCorrection, request: Request,
    session: Session = Depends(get_db_session),
    auth: SessionState = Depends(_require_section_authenticated_session)):
    return await _audited_response(session, request, payload, paper_id,
        f"sections.metadata.{target_id}", auth.username,
        lambda worker: correct_metadata(worker, paper_id, "sections", target_id, payload),
        receipt_serializer=lambda value: value)


@router.post('/papers/{paper_id}/ai-extract/jobs/{job_id}/resume')
async def resume_same_paper_extract(
    paper_id:UUID,job_id:str,request_id:str=Body(...,min_length=1,max_length=128),
    model:str | None=Body(default=None),session:Session=Depends(get_db_session),
    settings:Settings=Depends(get_settings),auth:SessionState=Depends(_require_section_write_session),
):
    from app.services.paper_native_recovery import resume_job
    from app.services.ai_extract_service import serialize_job
    from app.services.paper_db_phase import run_phase
    try:
        job=await resume_job(session,paper_id,job_id,settings,request_id=request_id,actor=auth.username,model=model,expected_type="ai_extract_rebuild")
        return await run_phase(session, lambda worker: serialize_job(job, worker))
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    except Exception:
        logger.exception("Same-job extract resume failed for %s", job_id)
        raise
