from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.db.models import Paper
from app.db.session import session_scope
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService
from app.workers.celery_app import celery_app


@celery_app.task(name="papers.ingest_path")
def ingest_pdf_path_task(pdf_path: str) -> str:
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        service = PaperIngestionService(session=session, settings=settings)
        paper = service.ingest_pdf_sync(source_path=Path(pdf_path), original_filename=Path(pdf_path).name)
        return str(paper.id)


@celery_app.task(name="papers.rerun_stage2")
def rerun_stage2_task(paper_id: str) -> dict[str, int | str]:
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        paper = session.get(Paper, UUID(paper_id))
        if not paper:
            return {"paper_id": paper_id, "status": "not_found"}
        summary = PaperReprocessingService(session=session, settings=settings).rerun_stage2(UUID(paper_id))
        return {
            "paper_id": paper_id,
            "status": "completed",
            **summary,
        }
