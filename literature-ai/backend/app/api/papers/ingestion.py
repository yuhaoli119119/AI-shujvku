from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.api import IngestFromPathRequest, IngestResponse
from app.services.discovery_service import DiscoveryService
from app.services.paper_ingestion import PaperConflictError, PaperIdentityMismatchError, PaperIngestionService
from app.services.workflow_jobs import create_job, update_job, build_job_runtime_context, normalize_library_name

router = APIRouter()


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


@router.post("/ingest/path", response_model=IngestResponse)
async def ingest_from_path(
    payload: IngestFromPathRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
    source_path = Path(payload.pdf_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="PDF path does not exist")

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
    try:
        paper = await service.ingest_pdf(
            source_path=source_path,
            original_filename=source_path.name,
            external_metadata=external_meta,
            source_reference=str(source_path.resolve()),
            library_name=normalize_library_name(payload.library_name),
            ingest_source="local_pdf",
        )
    except PaperConflictError as exc:
        _raise_already_exists(exc)
    return IngestResponse(paper_id=paper.id, title=paper.title, status=getattr(paper, "_ingest_status", "completed"))


@router.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: UploadFile = File(...),
    identifier: str | None = Form(default=None, description="Optional DOI or identifier to fetch metadata"),
    library_name: str | None = Form(default=None, description="Target literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestResponse:
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
