from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper
from app.db.session import get_db_session
from app.schemas.workbench import GeminiAuditRequest, HumanConfirmRequest, WorkbenchPrepareRequest
from app.services.gemini_audit_service import GeminiAuditService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.workbench_status import WORKBENCH_SCHEMA_VERSION

router = APIRouter()


@router.get("/review-center")
async def review_center(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return PaperWorkbenchService(session, settings).review_center(limit=limit)


@router.get("/papers/{paper_id}/workspace")
async def get_paper_workspace(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return PaperWorkbenchService(session, settings).workspace_summary(paper_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/prepare")
async def prepare_paper_workspace(
    paper_id: UUID,
    payload: WorkbenchPrepareRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return PaperWorkbenchService(session, settings).prepare_paper_workspace(
            paper_id,
            render_pages=payload.render_pages,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/gemini-audit")
async def submit_gemini_audit(
    paper_id: UUID,
    payload: GeminiAuditRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return GeminiAuditService(session).submit(
            paper_id=paper_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            decision=payload.decision,
            reviewer=payload.reviewer,
            reviewer_note=payload.reviewer_note,
            confidence=payload.confidence,
            field_names=payload.field_names,
            field_name=payload.field_name,
            proposed_value=payload.proposed_value,
            evidence_payload=payload.evidence_payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/human-confirm")
async def human_confirm_workbench_status(
    paper_id: UUID,
    payload: HumanConfirmRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return GeminiAuditService(session).human_confirm(
            paper_id=paper_id,
            target_status=payload.target_status,
            reviewer=payload.reviewer,
            note=payload.note,
            confirm_human_review=payload.confirm_human_review,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/prepare-active-library")
async def prepare_active_library(
    render_pages: bool = False,
    limit: int = Query(default=500, ge=1, le=5000),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    service = PaperWorkbenchService(session, settings)
    papers = session.query(Paper).order_by(Paper.created_at.asc()).limit(limit).all()
    rows = []
    for paper in papers:
        try:
            rows.append(service.prepare_paper_workspace(paper.id, render_pages=render_pages))
        except Exception as exc:
            rows.append({"paper_id": str(paper.id), "status": "failed", "error": str(exc)})
    return {
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "prepared": len(rows),
        "rows": rows,
    }
