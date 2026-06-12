from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import DFTResult, Paper
from app.db.session import get_db_session
from app.schemas.workbench import (
    ConflictAdjudicationActionRequest,
    ConflictAutoAdvanceBatchRequest,
    GeminiAuditRequest,
    HumanConfirmRequest,
    ReviewCenterBatchStage2Request,
    VerificationConflictDecisionRequest,
    VerificationSessionCreateRequest,
    VerificationSessionSettleRequest,
    WorkbenchPrepareRequest,
)
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.gemini_audit_service import GeminiAuditService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.verification_session_service import VerificationSessionService
from app.utils.active_database import get_registered_active_library_info
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.workbench_status import WORKBENCH_SCHEMA_VERSION

router = APIRouter()


@router.get("/review-center")
def review_center(
    limit: int = Query(default=100, ge=1, le=500),
    sort_by: str = Query(default="recent"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return PaperWorkbenchService(session, settings).review_center(
        limit=limit,
        sort_by=sort_by,
        library_name=library_name,
    )


@router.get("/review-conflicts")
def get_review_conflicts(
    paper_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    field_name: str | None = None,
    include_non_conflicts: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return ReviewAdjudicationService(session).list_with_adjudication(
        paper_id=paper_id,
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        include_non_conflicts=include_non_conflicts,
        limit=limit,
    )


@router.post("/review-conflicts/accept-ai")
def accept_ai_adjudication(
    payload: ConflictAdjudicationActionRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return ReviewAdjudicationService(session).accept_recommendation(
            paper_id=payload.paper_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            field_name=payload.field_name,
            reviewer=payload.reviewer,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/review-conflicts/auto-advance")
def auto_advance_review_conflicts(
    payload: ConflictAutoAdvanceBatchRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    return ReviewAdjudicationService(session).auto_advance_batch(
        paper_ids=payload.paper_ids,
        reviewer=payload.reviewer,
        limit=payload.limit,
    )


@router.post("/verification-sessions")
def create_verification_session(
    payload: VerificationSessionCreateRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return VerificationSessionService(session, settings).create_session(
            paper_ids=payload.paper_ids,
            paper_refs=payload.paper_refs,
            scope=payload.scope,
            refresh_materials=payload.refresh_materials,
            reviewer=payload.reviewer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/verification-sessions/{session_id}")
def get_verification_session(
    session_id: str,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return VerificationSessionService(session, settings).get_session(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/verification-sessions/{session_id}/settle")
def settle_verification_session(
    session_id: str,
    payload: VerificationSessionSettleRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return VerificationSessionService(session, settings).settle_session(session_id, reviewer=payload.reviewer)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/review-conflicts/manual-decision")
def manually_resolve_review_conflict(
    payload: VerificationConflictDecisionRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return VerificationSessionService(session, settings).resolve_conflict(
            paper_id=payload.paper_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            field_name=payload.field_name,
            resolution=payload.resolution,
            reviewer=payload.reviewer,
            opinion_source_id=payload.opinion_source_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/artifact-reliability")
def get_artifact_reliability_audit(
    limit: int = Query(default=100, ge=1, le=500),
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return ArtifactReliabilityAuditService(session, settings).audit_library(limit=limit, library_name=library_name)


@router.get("/papers/{paper_id}/artifact-reliability")
def get_paper_artifact_reliability_audit(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return ArtifactReliabilityAuditService(session, settings).audit_paper(paper_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/papers/{paper_id}/workspace")
def get_paper_workspace(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        return PaperWorkbenchService(session, settings).workspace_summary(paper_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/prepare")
def prepare_paper_workspace(
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
def submit_gemini_audit(
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
            agent_role=payload.agent_role,
            model_name=payload.model_name,
            protocol_key=payload.protocol_key,
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
def human_confirm_workbench_status(
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
def prepare_active_library(
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


def _run_review_center_prepare_ai_materials(
    payload: ReviewCenterBatchStage2Request,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    service = PaperReprocessingService(session=session, settings=settings)
    requested_mode = payload.mode
    normalized_mode = {
        "reparse_filtered": "prepare_filtered",
        "deep_parse_suspected_missing": "prepare_suspected_missing",
    }.get(requested_mode, requested_mode)
    paper_ids = list(dict.fromkeys(payload.paper_ids))
    selection_scope = "requested_paper_ids" if paper_ids else "explicit_request"
    if normalized_mode == "prepare_suspected_missing":
        requested_ids = set(paper_ids)
        stmt = select(Paper.id, Paper.workflow_status, Paper.library_name)
        if requested_ids:
            stmt = stmt.where(Paper.id.in_(requested_ids))
        else:
            try:
                active_library = normalize_library_name(get_registered_active_library_info().get("active_library"))
            except Exception:
                active_library = None
            if active_library:
                stmt = stmt.where(build_library_name_clause(Paper.library_name, active_library))
                selection_scope = f"active_library:{active_library}"
            else:
                selection_scope = "all_papers_fallback"
        paper_rows = session.execute(stmt).all()
        candidate_ids = {row.id for row in paper_rows}
        parsed_counts = {
            paper_id: int(count or 0)
            for paper_id, count in session.execute(
                select(DFTResult.paper_id, func.count(DFTResult.id))
                .where(DFTResult.paper_id.in_(candidate_ids))
                .group_by(DFTResult.paper_id)
            ).all()
        } if candidate_ids else {}
        audits = DFTCompletenessAuditor(session).audit_papers(candidate_ids, parsed_counts=parsed_counts)
        paper_ids = [
            row.id
            for row in paper_rows
            if (
                int((audits.get(str(row.id)) or {}).get("suspected_missing_count") or 0) > 0
                or str(row.workflow_status or "") in {"Unparsed", "Suspected_Missing"}
            )
        ]
    results: list[dict[str, Any]] = []
    for paper_id in paper_ids:
        try:
            summary = service.rerun_stage2(paper_id)
            results.append({"paper_id": str(paper_id), "status": "completed", "summary": summary})
        except Exception as exc:
            results.append({"paper_id": str(paper_id), "status": "failed", "error": str(exc)})
    return {
        "mode": normalized_mode,
        "requested_mode": requested_mode,
        "action_label": (
            "prepare_external_ai_materials_for_suspected_missing"
            if normalized_mode == "prepare_suspected_missing"
            else "prepare_external_ai_materials"
        ),
        "llm_required": False,
        "selection_scope": selection_scope,
        "requested": len(paper_ids),
        "completed": len([item for item in results if item["status"] == "completed"]),
        "failed": len([item for item in results if item["status"] == "failed"]),
        "rows": results,
    }


@router.post("/review-center/prepare-ai-materials")
def prepare_review_center_ai_materials(
    payload: ReviewCenterBatchStage2Request,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return _run_review_center_prepare_ai_materials(payload, session, settings)


@router.post("/review-center/batch-stage2", deprecated=True)
def batch_rerun_stage2(
    payload: ReviewCenterBatchStage2Request,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # Compatibility alias for older UI calls. The supported meaning is
    # "prepare AI-readable materials for external IDE/MCP follow-up".
    return _run_review_center_prepare_ai_materials(payload, session, settings)
