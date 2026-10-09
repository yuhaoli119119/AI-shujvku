from __future__ import annotations

import logging
import hashlib
from pathlib import Path
import re
from uuid import UUID, uuid4

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import select, text

from app.config import Settings, get_settings
from app.db.models import Paper, PaperRelationship, WorkflowJob, RebuildPaperFile
from app.db.session import get_db_session
from app.schemas.api import IngestFromPathRequest, IngestResponse
from app.security.files import UnsafeLocalPDF, validate_local_ingest_pdf
from app.services.artifact_store import ArtifactStore
from app.services.paper_codes import ensure_paper_codes, next_supplementary_paper_code
from app.services.rebuild_workflow_service import associate_pdf_file
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference
from app.utils.library_names import normalize_library_name

router = APIRouter()
logger = logging.getLogger(__name__)

_DOCLING_PAPER_ID_PATTERN = re.compile(
    r"docling_parse_failed:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _record_ingest_failure(session: Session, job_id: str, exc: Exception) -> str:
    """Recover a failed transaction and persist the original ingest failure."""
    error_text = str(exc)
    paper_id_match = _DOCLING_PAPER_ID_PATTERN.search(error_text)
    try:
        session.rollback()
        job = session.get(WorkflowJob, job_id)
        if job is None:
            logger.error(
                "Cannot record ingest failure because workflow job %s no longer exists; original error: %s",
                job_id,
                error_text,
            )
            return error_text

        payload = dict(job.payload or {})
        if paper_id_match is not None:
            payload["paper_id"] = paper_id_match.group(1)
        job.payload = payload
        job.status = "failed"
        job.error = error_text
        job.progress = {
            **dict(job.progress or {}),
            "phase": "failed",
            **({"paper_id": paper_id_match.group(1)} if paper_id_match is not None else {}),
        }
        session.add(job)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "Failed to persist workflow job %s failure; preserving original ingest error: %s",
            job_id,
            error_text,
        )
    return error_text


def _validated_local_pdf(path: str, settings: Settings) -> Path:
    try:
        return validate_local_ingest_pdf(Path(path), settings)
    except UnsafeLocalPDF as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_upload_request(file: UploadFile) -> None:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
    if file.size and file.size > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 30MB.")


async def _stage_uploaded_pdf(file: UploadFile, settings: Settings) -> Path:
    store = ArtifactStore(settings)
    suffix_name = Path(file.filename or "upload.pdf").name
    return await store.save_upload(file, f"{uuid4()}_{suffix_name}")


async def _uploaded_digest_and_size(file: UploadFile) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if size > 30 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large. Maximum size is 30MB.")
    await file.seek(0)
    return digest.hexdigest(), size


def _file_digest_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def _pdf_reference(path: Path, settings: Settings) -> str:
    return canonicalize_persisted_artifact_reference(path, category="pdf", settings=settings) or str(path)


def _upload_only_job(
    session: Session,
    *,
    job_type: str,
    library_name: str | None,
    payload: dict[str, Any],
    runtime_context: dict[str, Any],
    progress: dict[str, Any],
) -> WorkflowJob:
    # Upload-only records are completed synchronously; no legacy parser/queue.
    job = WorkflowJob(
        job_id=str(uuid4()), type=job_type, status="completed",
        library_name=library_name, payload=payload,
        runtime_context=runtime_context,
        progress={**progress, "automatic_parsing_started": False},
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _lock_upload(session: Session, scope: str, digest: str) -> None:
    # Same content/scope serializes even when the first response is lost.
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256((scope + ":" + digest).encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _existing_upload_response(session: Session, record: RebuildPaperFile, paper: Paper) -> dict[str, Any]:
    job = session.scalar(select(WorkflowJob).where(
        WorkflowJob.payload["supplementary_for_paper_id" if record.role == "si" else "paper_id"].as_string() == str(paper.id),
        WorkflowJob.payload["pdf_path"].as_string().endswith(record.storage_path),
        WorkflowJob.payload["upload_only"].as_boolean() == True,
    ).order_by(WorkflowJob.created_at))
    if job is not None:
        data = _serialize_upload_job(job)
    else:
        data = {"status": "completed", "payload": {"upload_only": True, "paper_id": str(paper.id), "file_id": str(record.id)},
                "progress": {"phase": "completed", "automatic_parsing_started": False}}
    return {**data, "paper_id": str(paper.id), "file_id": str(record.id),
            "automatic_parsing_started": False, "duplicate": True}


def _serialize_upload_job(job: WorkflowJob) -> dict[str, Any]:
    return {name: getattr(job, name) for name in (
        "job_id", "type", "status", "library_name", "payload", "progress", "result",
        "error", "created_at", "updated_at",
    )}


def _raise_already_exists(exc: PaperConflictError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "status": "already_exists",
            "paper_id": str(exc.paper.id),
            "title": exc.paper.title,
            "message": str(exc),
        },
    ) from exc


def _raise_identity_guard(exc: PaperIdentityMismatchError) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "status": exc.status,
            "target_paper_id": str(exc.target_paper.id),
            "target": {
                "title": exc.target_paper.title,
                "doi": exc.target_paper.doi,
                "year": exc.target_paper.year,
            },
            "incoming": {
                "title": exc.incoming.get("title"),
                "doi": exc.incoming.get("doi"),
                "year": exc.incoming.get("year"),
            },
            "match_score": exc.match_report.get("score", 0.0),
            "match_reason": exc.match_report.get("reason", ""),
        },
    ) from exc


@router.post("/ingest/path/jobs")
async def queue_ingest_from_path(
    payload: IngestFromPathRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    source_path = _validated_local_pdf(payload.pdf_path, settings)

    target_library = normalize_library_name(payload.library_name)
    job_payload = {
        "pdf_path": str(source_path),
        "title": payload.title,
        "doi": payload.doi,
        "authors": payload.authors,
        "year": payload.year,
        "journal": payload.journal,
        "abstract": payload.abstract,
        "library_name": target_library,
    }
    job, reused = create_job_or_reuse_active(
        session,
        job_type=JOB_TYPE_LOCAL_PDF_PATH_INGEST,
        library_name=target_library,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Local PDF ingest is queued in the worker.",
            "source_path": str(source_path),
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


@router.post("/ingest/path", response_model=IngestResponse)
async def ingest_from_path(
    payload: IngestFromPathRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    source_path = _validated_local_pdf(payload.pdf_path, settings)

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
    job = create_job(
        session=session,
        job_type="local_pdf_path_ingest",
        library_name=normalize_library_name(payload.library_name),
        payload={
            "pdf_path": str(source_path),
            "title": payload.title,
            "doi": payload.doi,
            "year": payload.year,
            "journal": payload.journal,
        },
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在解析本地 PDF 文件"},
    )
    job_id = str(job.job_id)
    try:
        paper = await service.ingest_pdf(
            source_path=source_path,
            original_filename=source_path.name,
            external_metadata=external_meta,
            source_reference=str(source_path.resolve()),
            library_name=normalize_library_name(payload.library_name),
            ingest_source="local_pdf",
        )
        update_job(
            session,
            job.job_id,
            status="completed",
            progress={
                "phase": "completed",
                "message": "本地 PDF 收录成功",
                "paper_id": str(paper.id),
                "ingested": 1,
            },
        )
    except PaperConflictError as exc:
        update_job(session, job.job_id, status="failed", error=f"doi_conflict: {exc}")
        _raise_already_exists(exc)
    except Exception as exc:
        err_str = _record_ingest_failure(session, job_id, exc)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    _validate_upload_request(file)

    service = PaperIngestionService(session=session, settings=settings)
    job = create_job(
        session=session,
        job_type="local_pdf_upload",
        library_name=normalize_library_name(library_name),
        payload={"filename": file.filename},
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在解析上传的 PDF 文件"},
    )
    job_id = str(job.job_id)
    
    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=None,
            library_name=normalize_library_name(library_name),
        )
        update_job(session, job.job_id, status="completed", progress={"phase": "completed", "message": "PDF 收录成功", "ingested": 1})
    except PaperConflictError as exc:
        update_job(session, job.job_id, status="failed", error=f"doi_conflict: {exc}")
        _raise_already_exists(exc)
    except Exception as exc:
        err_str = _record_ingest_failure(session, job_id, exc)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
        
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/ingest/upload/jobs")
async def queue_ingest_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _validate_upload_request(file)

    target_library = normalize_library_name(library_name)
    sha256, file_size = await _uploaded_digest_and_size(file)
    _lock_upload(session, "main:" + target_library, sha256)
    existing = session.scalar(select(RebuildPaperFile).join(Paper, Paper.id == RebuildPaperFile.paper_id).where(
        RebuildPaperFile.role == "main", RebuildPaperFile.sha256 == sha256,
        Paper.library_name == target_library,
    ))
    if existing is not None:
        return _existing_upload_response(session, existing, session.get(Paper, existing.paper_id))
    staged_pdf = await _stage_uploaded_pdf(file, settings)
    pdf_reference = _pdf_reference(staged_pdf, settings)
    title = Path(file.filename or "未命名 PDF").stem or "未命名 PDF"
    paper = Paper(
        library_name=target_library,
        title=title,
        pdf_path=pdf_reference,
        workflow_status="Imported",
        oa_status="uploaded_only",
    )
    session.add(paper)
    session.flush()
    ensure_paper_codes(session, [paper])
    associate_pdf_file(
        session,
        paper_id=paper.id,
        role="main",
        saved_path=staged_pdf,
        original_filename=file.filename or "upload.pdf",
        sha256=sha256,
        file_size=file_size,
        settings=settings,
    )
    job_payload = {
        "pdf_path": pdf_reference,
        "library_name": target_library,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
        "paper_id": str(paper.id),
        "upload_only": True,
    }
    job = _upload_only_job(
        session=session,
        job_type="local_pdf_path_ingest",
        library_name=target_library,
        payload=job_payload,
        runtime_context={},
        progress={
            "phase": "completed",
            "message": "PDF 已保存，未启动自动解析。",
            "source_path": pdf_reference,
            "paper_id": str(paper.id),
        },
    )

    data = _serialize_upload_job(job)
    data["paper_id"] = str(paper.id)
    data["automatic_parsing_started"] = False
    return data


@router.post("/{paper_id}/supplementary/upload", response_model=IngestResponse)
async def upload_supplementary_pdf(
    paper_id: UUID,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    target = session.get(Paper, paper_id)
    if not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    _validate_upload_request(file)

    service = PaperIngestionService(session=session, settings=settings)
    job = create_job(
        session=session,
        job_type="supplementary_pdf_upload",
        library_name=target.library_name,
        payload={"filename": file.filename, "supplementary_for_paper_id": str(target.id)},
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在上传支撑文献 PDF"},
    )
    job_id = str(job.job_id)

    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=None,
            library_name=target.library_name,
            supplementary_for_paper_id=target.id,
        )
        update_job(session, job.job_id, status="completed", progress={"phase": "completed", "message": "支撑文献上传成功", "ingested": 1})
    except Exception as exc:
        err_str = _record_ingest_failure(session, job_id, exc)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/{paper_id}/supplementary/upload/jobs")
async def queue_upload_supplementary_pdf(
    paper_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    target = session.get(Paper, paper_id)
    if not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    _validate_upload_request(file)

    sha256, file_size = await _uploaded_digest_and_size(file)
    _lock_upload(session, "si:" + str(target.id), sha256)
    session.refresh(target, with_for_update=True)
    existing = session.scalar(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id == target.id, RebuildPaperFile.role == "si", RebuildPaperFile.sha256 == sha256))
    if existing is not None:
        data = _existing_upload_response(session, existing, target)
        # SI may originate from a separately uploaded paper rather than this endpoint.
        relation = session.scalar(select(PaperRelationship).join(Paper, Paper.id == PaperRelationship.target_paper_id).where(
            PaperRelationship.source_paper_id == target.id, PaperRelationship.relationship_type == "supplementary",
            Paper.pdf_path.endswith(existing.storage_path)))
        if relation is not None:
            data["supplementary_paper_id"] = str(relation.target_paper_id)
        return data
    staged_pdf = await _stage_uploaded_pdf(file, settings)
    pdf_reference = _pdf_reference(staged_pdf, settings)
    ensure_paper_codes(session, [target])
    supplementary = Paper(
        library_name=target.library_name,
        title=f"{target.title or target.paper_code or '未命名文献'} · SI",
        year=target.year,
        journal=target.journal,
        pdf_path=pdf_reference,
        workflow_status="Imported",
        oa_status="uploaded_only",
        paper_type="supplementary",
    )
    session.add(supplementary)
    session.flush()
    supplementary.paper_code = next_supplementary_paper_code(
        session,
        main_paper_code=target.paper_code,
        serial_number=target.serial_number,
        exclude_paper_id=supplementary.id,
    )
    session.add(
        PaperRelationship(
            source_paper_id=target.id,
            target_paper_id=supplementary.id,
            relationship_type="supplementary",
            created_by="supplementary_upload",
            note="Upload-only supplementary association.",
        )
    )
    associate_pdf_file(
        session,
        paper_id=target.id,
        role="si",
        saved_path=staged_pdf,
        original_filename=file.filename or "supplementary.pdf",
        sha256=sha256,
        file_size=file_size,
        settings=settings,
    )
    job_payload = {
        "pdf_path": pdf_reference,
        "library_name": target.library_name,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
        "supplementary_for_paper_id": str(target.id),
        "supplementary_paper_id": str(supplementary.id),
        "upload_only": True,
    }

    job = _upload_only_job(
        session=session,
        job_type="local_pdf_path_ingest",
        library_name=target.library_name,
        payload=job_payload,
        runtime_context={},
        progress={
            "phase": "completed",
            "message": "SI 已保存并关联，未启动自动解析。",
            "source_path": pdf_reference,
            "supplementary_for_paper_id": str(target.id),
            "supplementary_paper_id": str(supplementary.id),
        },
    )

    data = _serialize_upload_job(job)
    data["paper_id"] = str(target.id)
    data["supplementary_paper_id"] = str(supplementary.id)
    data["automatic_parsing_started"] = False
    return data


@router.post("/{paper_id}/attach-pdf", response_model=IngestResponse)
async def attach_pdf_to_existing_paper(
    paper_id: UUID,
    file: UploadFile = File(...),
    confirm_identity_mismatch: bool = Form(
        default=False,
        description="Allow low-confidence title/year binding. Explicit DOI conflicts are still rejected.",
    ),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    target = session.get(Paper, paper_id)
    if not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
    if file.size and file.size > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 30MB.")

    service = PaperIngestionService(session=session, settings=settings)
    job = create_job(
        session=session,
        job_type="local_pdf_upload",
        library_name=target.library_name,
        payload={"filename": file.filename, "attach_to_paper_id": str(target.id)},
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在附加 PDF 文件"},
    )
    job_id = str(job.job_id)
    
    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=None,
            library_name=target.library_name,
            attach_to_paper_id=target.id,
            confirm_identity_mismatch=confirm_identity_mismatch,
        )
        update_job(session, job.job_id, status="completed", progress={"phase": "completed", "message": "PDF 附加成功", "ingested": 1})
    except PaperIdentityMismatchError as exc:
        update_job(session, job.job_id, status="failed", error=f"identity_mismatch: {exc}")
        _raise_identity_guard(exc)
    except PaperConflictError as exc:
        update_job(session, job.job_id, status="failed", error=f"doi_conflict: {exc}")
        _raise_already_exists(exc)
    except Exception as exc:
        err_str = _record_ingest_failure(session, job_id, exc)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
        
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/{paper_id}/attach-pdf/jobs")
async def queue_attach_pdf_to_existing_paper(
    paper_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    confirm_identity_mismatch: bool = Form(
        default=False,
        description="Allow low-confidence title/year binding. Explicit DOI conflicts are still rejected.",
    ),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    target = session.get(Paper, paper_id)
    if not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    _validate_upload_request(file)

    staged_pdf = await _stage_uploaded_pdf(file, settings)
    pdf_reference = _pdf_reference(staged_pdf, settings)
    sha256, file_size = _file_digest_and_size(staged_pdf)
    ensure_paper_codes(session, [target])
    target.pdf_path = pdf_reference
    session.add(target)
    associate_pdf_file(
        session,
        paper_id=target.id,
        role="main",
        saved_path=staged_pdf,
        original_filename=file.filename or "main.pdf",
        sha256=sha256,
        file_size=file_size,
        settings=settings,
    )
    job_payload = {
        "pdf_path": pdf_reference,
        "library_name": target.library_name,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
        "attach_to_paper_id": str(target.id),
        "confirm_identity_mismatch": bool(confirm_identity_mismatch),
        "upload_only": True,
    }
    job = _upload_only_job(
        session=session,
        job_type=JOB_TYPE_LOCAL_PDF_PATH_INGEST,
        library_name=target.library_name,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "completed",
            "message": "正文 PDF 已保存并关联，未启动自动解析。",
            "source_path": pdf_reference,
            "attach_to_paper_id": str(target.id),
        },
    )

    data = serialize_job(job)
    data["paper_id"] = str(target.id)
    data["automatic_parsing_started"] = False
    return data
