from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.services.dft_review_bundle_service import (
    ChartReviewScopeSelectionRequiredError,
    DFTReviewBundleService,
    FigureTableReviewNotCompletedError,
)
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService


router = APIRouter()


@router.get("/{paper_id}/dft-review-state")
def get_dft_review_state(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Preview the paper-level reviewed-evidence aggregate without exporting or writing."""

    try:
        return DFTReviewBundleService(session, settings).get_review_state(paper_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{paper_id}/dft-review-task")
def get_dft_review_task(
    paper_id: UUID,
    catalyst_sample_id: UUID | None = Query(default=None),
    dft_result_ids: list[UUID] | None = Query(default=None),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return a pending-only or UUID-scoped local-AI DFT task without exporting or writing."""

    try:
        return DFTReviewBundleService(session, settings).get_review_task(
            paper_id,
            catalyst_sample_id=catalyst_sample_id,
            dft_result_ids=dft_result_ids,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/dft-review-bundle")
def export_dft_review_bundle(
    paper_id: UUID,
    chart_run_id: UUID | None = Query(default=None),
    chart_scope: str | None = Query(default=None, pattern="^(paper|external_analysis_run)$"),
    include_figure_files: bool = Query(default=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Generate an offline review ZIP in memory and stream it to the owner client."""

    try:
        bundle = DFTReviewBundleService(session, settings).build_zip(
            paper_id,
            chart_run_id=chart_run_id,
            explicit_paper_scope=chart_scope == "paper",
            include_figure_files=include_figure_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FigureTableReviewNotCompletedError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ChartReviewScopeSelectionRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            "X-LitAI-DFT-Review-Mode": bundle["manifest"]["review_mode"],
            "X-LitAI-Reviewed-Figures": str(bundle["manifest"]["counts"]["reviewed_figures"]),
            "X-LitAI-Reviewed-Tables": str(bundle["manifest"]["counts"]["reviewed_tables"]),
            "X-LitAI-Pending-Main-Figures": str(bundle["manifest"]["counts"]["pending_main_figures"]),
            "X-LitAI-Unreviewed-Supporting-Context": str(bundle["manifest"]["counts"]["unreviewed_supporting_context"]),
        },
    )


@router.post("/{paper_id}/evidence-review-bundle")
def export_evidence_review_bundle(
    paper_id: UUID,
    run_id: UUID | None = Query(default=None),
    include_pdf_files: bool = Query(default=True),
    include_figure_files: bool = Query(default=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Generate an offline figure/table evidence review ZIP and stream it to the owner client."""

    try:
        bundle = EvidenceReviewBundleService(session, settings).build_zip(
            paper_id,
            run_id=run_id,
            include_pdf_files=include_pdf_files,
            include_figure_files=include_figure_files,
        )
        session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FigureTableReviewNotCompletedError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            "X-LitAI-Review-Scope": str(bundle["manifest"].get("scope_type") or "paper"),
            "X-LitAI-Review-Run-Id": str(bundle["manifest"].get("run_id") or ""),
            "X-LitAI-Review-Figure-Count": str(len(bundle["manifest"].get("expected_coverage", {}).get("figure_ids", []))),
            "X-LitAI-Review-Table-Count": str(len(bundle["manifest"].get("expected_coverage", {}).get("table_ids", []))),
            "X-LitAI-Bundle-Id": str(bundle.get("bundle_id") or bundle["manifest"].get("bundle_id") or ""),
        },
    )


@router.post("/{paper_id}/evidence-review-result/validate")
def validate_evidence_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    run_id: UUID | None = Query(default=None),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate a returned figure/table evidence review proposal without writing records."""

    try:
        return EvidenceReviewBundleService(session, settings).validate_result(paper_id, payload, run_id=run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/evidence-review-result/apply")
def apply_evidence_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    run_id: UUID | None = Query(default=None),
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Apply safe web-AI figure/table suggestions; this endpoint cannot satisfy local-AI verification."""

    try:
        return EvidenceReviewBundleService(session, settings).apply_result(paper_id, payload, run_id=run_id, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



@router.get("/{paper_id}/chart-review-task")
def get_chart_review_task(
    paper_id: UUID,
    run_id: UUID | None = Query(default=None),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Return the current figure/table review task, including unresolved actions from the latest run.

    try:
        return EvidenceReviewBundleService(session, settings).get_review_task(paper_id, run_id=run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{paper_id}/chart-review-scopes")
def get_chart_review_scopes(
    paper_id: UUID,
    chart_run_id: UUID | None = Query(default=None),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return EvidenceReviewBundleService(session, settings).get_review_scope_options(
            paper_id,
            selected_run_id=chart_run_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/chart-review-result/resolve")
def resolve_chart_review_actions(
    paper_id: UUID,
    payload: dict[str, Any],
    run_id: UUID | None = Query(default=None),
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # HTTP callers may resolve safe actions, but only the authenticated MCP tool
    # grants local-AI verification authority for chart-stage completion.

    try:
        return EvidenceReviewBundleService(session, settings).resolve_review_actions(paper_id, payload, run_id=run_id, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/chart-review-result/finalize")
def finalize_chart_review(
    paper_id: UUID,
    payload: dict[str, Any] | None = None,
    run_id: UUID | None = Query(default=None),
    dry_run: bool = Query(default=False),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Finalize chart review only after re-reading current figures/tables and finding no unresolved actions.

    try:
        return EvidenceReviewBundleService(session, settings).finalize_review(paper_id, payload, run_id=run_id, dry_run=dry_run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/dft-review-result/validate")
def validate_dft_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    chart_run_id: UUID | None = Query(default=None),
    chart_scope: str | None = Query(default=None, pattern="^(paper|external_analysis_run)$"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate an offline proposal without writing any database records."""

    try:
        return DFTReviewBundleService(session, settings).validate_result(
            paper_id,
            payload,
            chart_run_id=chart_run_id,
            explicit_paper_scope=chart_scope == "paper",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FigureTableReviewNotCompletedError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ChartReviewScopeSelectionRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
