from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Base, Paper, PaperFigure
from app.db.session import get_db_session
from app.schemas.api import ExtractionRunResponse, PaperDetailResponse
from app.schemas.evidence import EvidenceLocatorResponse
from app.services.paper_query import PaperQueryService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.paper_reprocessing import PaperReprocessingService
from app.utils.artifact_paths import resolve_persisted_artifact_path

router = APIRouter()


def _safe_unlink(base_dir: Path, stored_path: str | None, *, category: str, settings: Settings) -> str | None:
    if not stored_path:
        return None
    base = base_dir.resolve()
    target = resolve_persisted_artifact_path(
        stored_path,
        category=category,
        settings=settings,
        must_exist=False,
    )
    if target is None:
        return None
    target = target.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if target.exists() and target.is_file():
        try:
            target.unlink()
            return str(target)
        except OSError:
            return None
    return None


@router.get("/{paper_id}", response_model=PaperDetailResponse)
async def get_paper(paper_id: UUID, session: Session = Depends(get_db_session)) -> PaperDetailResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    return detail


@router.delete("/{paper_id}")
async def delete_paper(
    paper_id: UUID,
    delete_pdf: bool = False,
    delete_derived: bool = False,
    session: Session = Depends(get_db_session),
) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    settings = get_settings()
    files_to_delete: list[tuple[Path, str | None]] = []
    if delete_pdf:
        files_to_delete.append((settings.storage_paths["pdf"], paper.pdf_path))
    if delete_derived:
        files_to_delete.extend(
            [
                (settings.storage_paths["tei"], paper.tei_path),
                (settings.storage_paths["docling_json"], paper.docling_json_path),
                (settings.storage_paths["markdown"], paper.markdown_path),
            ]
        )
        figure_paths = session.scalars(select(PaperFigure.image_path).where(PaperFigure.paper_id == paper_id)).all()
        files_to_delete.extend((settings.storage_paths["figures"], path) for path in figure_paths)

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "papers":
            continue
        conditions = []
        if "paper_id" in table.c:
            conditions.append(table.c.paper_id == paper_id)
        if "source_paper_id" in table.c:
            conditions.append(table.c.source_paper_id == paper_id)
        if "target_paper_id" in table.c:
            conditions.append(table.c.target_paper_id == paper_id)
        if conditions:
            condition = conditions[0]
            for extra in conditions[1:]:
                condition = condition | extra
            session.execute(table.delete().where(condition))
    session.delete(paper)
    session.commit()

    deleted_files = []
    for base_dir, stored_path in files_to_delete:
        category = base_dir.name
        deleted = _safe_unlink(base_dir, stored_path, category=category, settings=settings)
        if deleted:
            deleted_files.append(deleted)
    return {
        "status": "deleted",
        "paper_id": str(paper_id),
        "delete_pdf": delete_pdf,
        "delete_derived": delete_derived,
        "deleted_files": deleted_files,
    }


@router.post("/{paper_id}/extract", response_model=ExtractionRunResponse)
async def rerun_stage2_extraction(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    settings = get_settings()
    summary = PaperReprocessingService(session=session, settings=settings).rerun_stage2(paper_id)
    return ExtractionRunResponse(
        paper_id=paper_id,
        status="completed",
        dft_settings=summary.get("dft_settings", 0),
        catalyst_samples=summary.get("catalyst_samples", 0),
        dft_results=summary.get("dft_results", 0),
        electrochemical_performance=summary.get("electrochemical_performance", 0),
        mechanism_claims=summary.get("mechanism_claims", 0),
        writing_cards=summary.get("writing_cards", 0),
    )


@router.get("/{paper_id}/evidence/locators", response_model=list[EvidenceLocatorResponse])
async def get_paper_evidence_locators(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[EvidenceLocatorResponse]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return EvidenceLocatorService(session).list_locators_for_paper(paper_id)


@router.get("/{paper_id}/pdf")
async def get_paper_pdf(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    """Serve the PDF file for a paper, for in-browser preview."""
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not paper.pdf_path:
        raise HTTPException(status_code=404, detail="PDF not uploaded or unavailable")
    settings = get_settings()
    file_path = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")
    return FileResponse(str(file_path), media_type="application/pdf")
