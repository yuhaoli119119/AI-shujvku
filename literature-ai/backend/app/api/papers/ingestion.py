from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import IngestFromPathRequest, IngestResponse
from app.services.discovery_service import DiscoveryService
from app.services.paper_ingestion import PaperIngestionService
from app.services.workflow_jobs import normalize_library_name

router = APIRouter()


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
    paper = await service.ingest_pdf(
        source_path=source_path,
        original_filename=source_path.name,
        external_metadata=external_meta,
        source_reference=str(source_path.resolve()),
        library_name=normalize_library_name(payload.library_name),
    )
    return IngestResponse(paper_id=paper.id, title=paper.title, status="completed")


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
    paper = await service.ingest_upload(
        file=file,
        external_metadata=external_metadata,
        library_name=normalize_library_name(library_name),
    )
    return IngestResponse(paper_id=paper.id, title=paper.title, status="completed")
