from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.api import IngestFromPathRequest, IngestResponse
from app.security.files import UnsafeLocalPDF, validate_local_ingest_pdf
from app.services.artifact_store import ArtifactStore
from app.services.discovery_service import DiscoveryService
from app.services.paper_ingestion import PaperConflictError, PaperIdentityMismatchError, PaperIngestionService
from app.services.workflow_jobs import (
    JOB_TYPE_LOCAL_PDF_PATH_INGEST,
    build_job_runtime_context,
    create_job,
    create_job_or_reuse_active,
    dispatch_job,
    normalize_library_name,
    serialize_job,
    update_job,
)

router = APIRouter()


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
        err_str = str(exc)
        if "docling_parse_failed:" in err_str:
            try:
                parts = err_str.split(":", 2)
                paper_id_str = parts[1].split()[0].strip()
                job.payload = {**job.payload, "paper_id": paper_id_str}
                session.add(job)
                session.commit()
            except Exception:
                pass
        update_job(session, job.job_id, status="failed", error=err_str)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    _validate_upload_request(file)

    external_metadata = None
    if identifier:
        try:
            svc = DiscoveryService()
            _, external_metadata = await run_in_threadpool(svc.fetch_metadata, identifier)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Failed to fetch metadata for %s: %s", identifier, exc)

    service = PaperIngestionService(session=session, settings=settings)
    job = create_job(
        session=session,
        job_type="local_pdf_upload",
        library_name=normalize_library_name(library_name),
        payload={"filename": file.filename, "identifier": identifier},
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在解析上传的 PDF 文件"},
    )
    
    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=external_metadata,
            library_name=normalize_library_name(library_name),
        )
        update_job(session, job.job_id, status="completed", progress={"phase": "completed", "message": "PDF 收录成功", "ingested": 1})
    except PaperConflictError as exc:
        update_job(session, job.job_id, status="failed", error=f"doi_conflict: {exc}")
        _raise_already_exists(exc)
    except Exception as exc:
        err_str = str(exc)
        if "docling_parse_failed:" in err_str:
            try:
                parts = err_str.split(":", 2)
                paper_id_str = parts[1].split()[0].strip()
                job.payload = {**job.payload, "paper_id": paper_id_str}
                session.add(job)
                session.commit()
            except Exception:
                pass
        update_job(session, job.job_id, status="failed", error=err_str)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
        
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/ingest/upload/jobs")
async def queue_ingest_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _validate_upload_request(file)

    external_metadata = None
    if identifier:
        try:
            svc = DiscoveryService()
            _, external_metadata = await run_in_threadpool(svc.fetch_metadata, identifier)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Failed to fetch metadata for %s: %s", identifier, exc)

    staged_pdf = await _stage_uploaded_pdf(file, settings)
    target_library = normalize_library_name(library_name)
    job_payload = {
        "pdf_path": str(staged_pdf.resolve()),
        "library_name": target_library,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
    }
    if external_metadata:
        job_payload.update(external_metadata)

    job = create_job(
        session=session,
        job_type=JOB_TYPE_LOCAL_PDF_PATH_INGEST,
        library_name=target_library,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Uploaded PDF is queued for background parsing.",
            "source_path": str(staged_pdf.resolve()),
        },
    )

    db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else settings.database_url
    dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=db_url)
    if dispatch_mode != "celery":
        session.refresh(job)

    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
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

    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=None,
            library_name=target.library_name,
            supplementary_for_paper_id=target.id,
        )
        update_job(session, job.job_id, status="completed", progress={"phase": "completed", "message": "支撑文献上传成功", "ingested": 1})
    except Exception as exc:
        err_str = str(exc)
        update_job(session, job.job_id, status="failed", error=err_str)
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

    staged_pdf = await _stage_uploaded_pdf(file, settings)
    job_payload = {
        "pdf_path": str(staged_pdf.resolve()),
        "library_name": target.library_name,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
        "supplementary_for_paper_id": str(target.id),
    }

    job = create_job(
        session=session,
        job_type=JOB_TYPE_LOCAL_PDF_PATH_INGEST,
        library_name=target.library_name,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Supplementary PDF upload is queued for background parsing.",
            "source_path": str(staged_pdf.resolve()),
            "supplementary_for_paper_id": str(target.id),
        },
    )

    db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else settings.database_url
    dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=db_url)
    if dispatch_mode != "celery":
        session.refresh(job)

    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    return data


@router.post("/{paper_id}/attach-pdf", response_model=IngestResponse)
async def attach_pdf_to_existing_paper(
    paper_id: UUID,
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
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

    external_metadata = None
    if identifier:
        try:
            svc = DiscoveryService()
            _, external_metadata = await run_in_threadpool(svc.fetch_metadata, identifier)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Failed to fetch metadata for %s: %s", identifier, exc)

    service = PaperIngestionService(session=session, settings=settings)
    job = create_job(
        session=session,
        job_type="local_pdf_upload",
        library_name=target.library_name,
        payload={"filename": file.filename, "identifier": identifier, "attach_to_paper_id": str(target.id)},
        runtime_context=build_job_runtime_context(settings),
        progress={"phase": "running", "message": "正在附加 PDF 文件"},
    )
    
    try:
        paper = await service.ingest_upload(
            file=file,
            external_metadata=external_metadata,
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
        err_str = str(exc)
        if "docling_parse_failed:" in err_str:
            try:
                parts = err_str.split(":", 2)
                paper_id_str = parts[1].split()[0].strip()
                job.payload = {**job.payload, "paper_id": paper_id_str}
                session.add(job)
                session.commit()
            except Exception:
                pass
        update_job(session, job.job_id, status="failed", error=err_str)
        raise HTTPException(status_code=500, detail={"message": err_str, "status": "job_error"}) from exc
        
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/{paper_id}/attach-pdf/jobs")
async def queue_attach_pdf_to_existing_paper(
    paper_id: UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
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

    external_metadata = None
    if identifier:
        try:
            svc = DiscoveryService()
            _, external_metadata = await run_in_threadpool(svc.fetch_metadata, identifier)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Failed to fetch metadata for %s: %s", identifier, exc)

    staged_pdf = await _stage_uploaded_pdf(file, settings)
    job_payload = {
        "pdf_path": str(staged_pdf.resolve()),
        "library_name": target.library_name,
        "original_filename": file.filename,
        "trusted_staged_upload": True,
        "attach_to_paper_id": str(target.id),
        "confirm_identity_mismatch": bool(confirm_identity_mismatch),
    }
    if external_metadata:
        job_payload.update(external_metadata)

    job = create_job(
        session=session,
        job_type=JOB_TYPE_LOCAL_PDF_PATH_INGEST,
        library_name=target.library_name,
        payload=job_payload,
        runtime_context=build_job_runtime_context(settings),
        progress={
            "phase": "queued",
            "message": "Uploaded PDF attach is queued for background parsing.",
            "source_path": str(staged_pdf.resolve()),
            "attach_to_paper_id": str(target.id),
        },
    )

    db_url = session.bind.url.render_as_string(hide_password=False) if session.bind is not None else settings.database_url
    dispatch_mode = dispatch_job(job.job_id, background_tasks, control_database_url=db_url)
    if dispatch_mode != "celery":
        session.refresh(job)

    data = serialize_job(job)
    data["dispatch_mode"] = dispatch_mode
    return data
