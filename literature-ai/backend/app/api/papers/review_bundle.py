from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.services.dft_review_bundle_service import DFTReviewBundleService
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService


router = APIRouter()


@router.post("/{paper_id}/dft-review-bundle")
def export_dft_review_bundle(
    paper_id: UUID,
    include_figure_files: bool = Query(default=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Generate an offline review ZIP in memory and stream it to the owner client."""

    try:
        bundle = DFTReviewBundleService(session, settings).build_zip(
            paper_id,
            include_figure_files=include_figure_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = bundle["content"]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle["filename"]}"',
            "Content-Length": str(len(content)),
            "Cache-Control": "no-store",
            "X-LitAI-Bundle-Fingerprint": bundle["manifest"]["bundle_fingerprint"],
        },
    )


@router.post("/{paper_id}/evidence-review-bundle")
def export_evidence_review_bundle(
    paper_id: UUID,
    include_pdf_files: bool = Query(default=True),
    include_figure_files: bool = Query(default=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Generate an offline figure/table evidence review ZIP and stream it to the owner client."""

    try:
        bundle = EvidenceReviewBundleService(session, settings).build_zip(
            paper_id,
            include_pdf_files=include_pdf_files,
            include_figure_files=include_figure_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = bundle["content"]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle["filename"]}"',
            "Content-Length": str(len(content)),
            "Cache-Control": "no-store",
            "X-LitAI-Bundle-Fingerprint": bundle["manifest"]["bundle_fingerprint"],
        },
    )


@router.post("/{paper_id}/evidence-review-result/validate")
def validate_evidence_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate a returned figure/table evidence review proposal without writing records."""

    try:
        return EvidenceReviewBundleService(session, settings).validate_result(paper_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/evidence-review-result/apply")
def apply_evidence_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Apply auto-eligible figure/table evidence actions after local AI confirmation."""

    try:
        return EvidenceReviewBundleService(session, settings).apply_result(paper_id, payload, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



@router.get("/{paper_id}/chart-review-task")
def get_chart_review_task(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Return the current figure/table review task, including unresolved actions from the latest run.

    try:
        return EvidenceReviewBundleService(session, settings).get_review_task(paper_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/chart-review-result/resolve")
def resolve_chart_review_actions(
    paper_id: UUID,
    payload: dict[str, Any],
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Batch-resolve figure/table review actions through the same guarded apply path.

    try:
        return EvidenceReviewBundleService(session, settings).resolve_review_actions(paper_id, payload, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/chart-review-result/finalize")
def finalize_chart_review(
    paper_id: UUID,
    payload: dict[str, Any] | None = None,
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Finalize chart review only after re-reading current figures/tables and finding no unresolved actions.

    try:
        return EvidenceReviewBundleService(session, settings).finalize_review(paper_id, payload, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/dft-review-result/validate")
def validate_dft_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate an offline proposal without writing any database records."""

    try:
        return DFTReviewBundleService(session, settings).validate_result(paper_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
