from pathlib import Path
from uuid import UUID

from app.api.settings import apply_persisted_settings_to_runtime
from app.config import get_settings
from app.db.models import Paper
from app.db.session import session_scope
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.workflow_jobs import run_workflow_job_by_id
from app.workers.celery_app import celery_app


@celery_app.task(name="papers.ingest_path")
def ingest_pdf_path_task(pdf_path: str) -> str:
    apply_persisted_settings_to_runtime()
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        service = PaperIngestionService(session=session, settings=settings)
        paper = service.ingest_pdf_sync(source_path=Path(pdf_path), original_filename=Path(pdf_path).name)
        return str(paper.id)


def _prepare_external_ai_context_task(paper_id: str) -> dict[str, int | str | bool | list[str] | dict]:
    apply_persisted_settings_to_runtime()
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


@celery_app.task(name="papers.prepare_external_ai_context")
def prepare_external_ai_context_task(paper_id: str) -> dict[str, int | str | bool | list[str] | dict]:
    return _prepare_external_ai_context_task(paper_id)


@celery_app.task(name="papers.rerun_stage2")
def rerun_stage2_task(paper_id: str) -> dict[str, int | str | bool | list[str] | dict]:
    # Compatibility alias for older jobs. This no longer implies backend LLM deep extraction.
    return _prepare_external_ai_context_task(paper_id)


@celery_app.task(name="papers.run_workflow_job")
def run_workflow_job_task(job_id: str, control_database_url: str | None = None) -> None:
    run_workflow_job_by_id(job_id, control_database_url)
