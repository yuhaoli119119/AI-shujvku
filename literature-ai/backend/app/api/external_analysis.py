from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.settings import sync_writer_settings_from_session
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import InternalAIParseRequest, InternalAIParseResponse
from app.schemas.external_analysis import (
    ExternalAnalysisApplyReviewRulesRequest,
    ExternalAnalysisCandidateResponse,
    ExternalAnalysisImportRequest,
    ExternalAnalysisMaterializeRequest,
    ExternalAnalysisRunResponse,
)
from app.services.external_analysis_service import ExternalAnalysisService

router = APIRouter()

ACTIVE_REVIEW_RULE_STATUSES = {"candidate", "pending", "requires_resolution"}
MATERIALIZABLE_CANDIDATE_TYPES = {"note", "correction", "relationship"}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _candidate_action(candidate) -> tuple[str, str]:
    candidate_type = str(candidate.candidate_type or "").strip().lower()
    status = str(candidate.status or "").strip().lower()
    if candidate_type == "object_review_audit" and status in ACTIVE_REVIEW_RULE_STATUSES:
        return "apply_review_rules", "run"
    if candidate_type in MATERIALIZABLE_CANDIDATE_TYPES and status == "pending":
        return "materialize", "candidate"
    return "readonly", "candidate"


def _serialize_run(service: ExternalAnalysisService, run) -> ExternalAnalysisRunResponse:
    base = ExternalAnalysisRunResponse.model_validate(run).model_dump(exclude={"candidates", "warnings"})
    candidates = service.list_candidates(run.id)
    warnings = service.diagnose_import_warnings(run, candidates=candidates)
    return ExternalAnalysisRunResponse(
        **{**base, "created_at": _as_utc(run.created_at)},
        candidates=[
            ExternalAnalysisCandidateResponse.model_validate(item).model_copy(update={
                "created_at": _as_utc(item.created_at),
                "action_mode": _candidate_action(item)[0],
                "action_scope": _candidate_action(item)[1],
            })
            for item in candidates
        ],
        warnings=warnings,
    )


@router.post("/import", response_model=ExternalAnalysisRunResponse)
async def import_external_analysis(
    payload: ExternalAnalysisImportRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ExternalAnalysisRunResponse:
    try:
        effective_reviewer = (
            str(payload.reviewer or payload.source_label or payload.source or "ide_ai").strip() or "ide_ai"
        )
        service = ExternalAnalysisService(session=session, settings=settings)
        run = service.import_run(
            paper_id=payload.paper_id,
            source=payload.source,
            source_label=payload.source_label,
            raw_text=payload.raw_text,
            raw_payload=payload.raw_payload,
        )
        if payload.auto_apply_review_rules:
            write_lock_tokens = [*payload.write_lock_tokens]
            if payload.write_lock_token:
                write_lock_tokens.append(payload.write_lock_token)
            service.apply_review_rules_for_run(
                run.id,
                reviewer=effective_reviewer,
                write_lock_tokens=write_lock_tokens or None,
                write_lock_owner=effective_reviewer,
                auto_lock_owner=effective_reviewer,
                lock_meta_source="http_import_analysis",
            )
        session.commit()
        return _serialize_run(service, run)
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith(("module_write_lock_required", "module_write_lock_conflict")) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/runs", response_model=list[ExternalAnalysisRunResponse])
async def list_external_analysis_runs(
    paper_id: UUID | None = None,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[ExternalAnalysisRunResponse]:
    service = ExternalAnalysisService(session=session, settings=settings)
    runs = service.list_runs(paper_id=paper_id)
    return [_serialize_run(service, run) for run in runs]


@router.get("/runs/{run_id}", response_model=ExternalAnalysisRunResponse)
async def get_external_analysis_run(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ExternalAnalysisRunResponse:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        run = service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_run(service, run)


@router.delete("/runs/{run_id}")
async def delete_external_analysis_run(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        run = service.delete_run(run_id)
        session.commit()
        return {"deleted": True, "run_id": str(run.id)}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/delete")
async def delete_external_analysis_run_via_post(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    return await delete_external_analysis_run(run_id, session, settings)


@router.post("/runs/{run_id}/materialize")
async def materialize_external_analysis_run(
    run_id: UUID,
    payload: ExternalAnalysisMaterializeRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        result = service.materialize_candidates(
            run_id=run_id,
            candidate_ids=payload.candidate_ids,
            explicit_all=payload.explicit_all,
            created_by=payload.created_by,
        )
        session.commit()
        response = {
            "run_id": str(run_id),
            "created_notes": result.created_notes,
            "created_corrections": result.created_corrections,
            "created_relationships": result.created_relationships,
            "idempotent_noops": result.idempotent_noops,
            "skipped_candidates": result.skipped_candidates,
            "deferred_review_candidates": result.deferred_review_candidates,
        }
        if result.deferred_review_candidates > 0:
            response["next_action"] = "apply-review-rules"
        return response
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/apply-review-rules")
async def apply_review_rules_for_run(
    run_id: UUID,
    payload: ExternalAnalysisApplyReviewRulesRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Apply IDE-AI review rules to an existing external analysis run.

    This is the follow-up entry point for runs that were imported with
    ``auto_apply_review_rules=False``.  DFT ``object_review_audit`` candidates
    that were left in the candidate pool can be materialized and settled
    through this endpoint.  When the caller does not supply a write lock
    token, the service auto-acquires a ``dft_results`` lock for the duration
    of the apply step.
    """
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        run = service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        effective_reviewer = (
            str(payload.reviewer or run.source_label or run.source or "ide_ai").strip() or "ide_ai"
        )
        write_lock_tokens = [*payload.write_lock_tokens]
        if payload.write_lock_token:
            write_lock_tokens.append(payload.write_lock_token)
        auto_apply_summary = service.apply_review_rules_for_run(
            run_id=run_id,
            reviewer=effective_reviewer,
            write_lock_tokens=write_lock_tokens or None,
            write_lock_owner=effective_reviewer,
            auto_lock_owner=effective_reviewer,
            lock_meta_source="http_apply_review_rules",
        )
        session.commit()
        candidates = service.list_candidates(run_id)
        warnings = service.diagnose_import_warnings(
            run,
            candidates=candidates,
            auto_apply_summary=auto_apply_summary,
        )
        return {
            "run_id": str(run_id),
            "reviewer": effective_reviewer,
            "auto_apply_summary": auto_apply_summary,
            "candidate_count": len(candidates),
            "warnings": warnings,
            "candidates": [
                {
                    "id": str(c.id),
                    "type": c.candidate_type,
                    "status": c.status,
                    "target_type": (c.normalized_payload or {}).get("target_type"),
                    "target_id": (c.normalized_payload or {}).get("target_id"),
                    "field_name": (c.normalized_payload or {}).get("field_name"),
                    "decision": (c.normalized_payload or {}).get("decision") or (c.normalized_payload or {}).get("verdict"),
                    "materialized_target_type": c.materialized_target_type,
                    "materialized_target_id": c.materialized_target_id,
                }
                for c in candidates
            ],
        }
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith(("module_write_lock_required", "module_write_lock_conflict")) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/internal-parse", response_model=InternalAIParseResponse)
def internal_ai_parse_paper(
    paper_id: UUID,
    payload: InternalAIParseRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> InternalAIParseResponse:
    raise HTTPException(
        status_code=410,
        detail="网页端解析已停用；请通过 IDE/MCP AI 使用 prepare-ai-context、codex-item 和 import_analysis。",
    )
