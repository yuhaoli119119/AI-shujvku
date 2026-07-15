from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.mcp.auth import get_optional_request_mcp_auth
from app.mcp.context import MCPAuthInfo
from app.schemas.content_knowledge import ContentKnowledgeReviewRequest
from app.services.content_knowledge_review_service import (
    ContentKnowledgeReviewError,
    ContentKnowledgeReviewService,
)
from app.services.content_knowledge_service import ContentKnowledgeService
from app.services.content_review_bundle_service import ContentReviewBundleService
from app.services.content_writing_plan_service import ContentWritingPlanService

router = APIRouter()


@router.get("")
def list_content_knowledge(
    paper_id: str | None = Query(default=None),
    run_id: UUID | None = Query(default=None),
    library_name: str | None = Query(default=None),
    category: str | None = Query(
        default=None,
        pattern="^(mechanism_evidence|performance_evidence|dft_evidence|figure_table_evidence|material_evidence|method_evidence|writing_material|review_viewpoint|uncertainty_note|draft_evidence_check)$",
    ),
    query: str | None = Query(default=None),
    include_candidates: bool = Query(default=True),
    include_blocked: bool = Query(default=False),
    review_status: str | None = Query(
        default=None,
        pattern="^(needs_review|needs_human|content_ready|validated|approved|safe_verified|rejected)$",
    ),
    citation_status: str | None = Query(default=None, pattern="^(citable|writing_only|needs_review|blocked)$"),
    source_trust: str | None = Query(default=None, pattern="^(verified|unverified)$"),
    problem_status: str | None = Query(default=None, pattern="^(has_risk)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> dict:
    return ContentKnowledgeService(session).list_items(
        paper_id=paper_id,
        run_id=run_id,
        library_name=library_name,
        category=category,
        query=query,
        include_candidates=include_candidates,
        include_blocked=include_blocked,
        review_status=review_status,
        citation_status=citation_status,
        source_trust=source_trust,
        problem_status=problem_status,
        offset=offset,
        limit=limit,
    )


@router.get("/items/{item_id}")
def get_content_knowledge_item(
    item_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ContentKnowledgeReviewService(session).get_item(item_id)
    except ContentKnowledgeReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/items/{item_id}/review")
def review_content_knowledge_item(
    item_id: UUID,
    payload: ContentKnowledgeReviewRequest,
    session: Session = Depends(get_db_session),
    auth: MCPAuthInfo | None = Depends(get_optional_request_mcp_auth),
) -> dict:
    if auth is not None:
        raise HTTPException(status_code=403, detail={"code": "human_review_requires_non_mcp_session"})
    try:
        result = ContentKnowledgeReviewService(session).review_item(
            item_id,
            decision=payload.decision,
            reviewer=payload.reviewer,
            reason=payload.reason,
            expected_updated_at=payload.expected_updated_at,
        )
        session.commit()
        return result
    except ContentKnowledgeReviewError as exc:
        session.rollback()
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.get("/papers/{paper_id}/review-summary")
def get_content_knowledge_review_summary(
    paper_id: str,
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ContentKnowledgeReviewService(session).paper_summary(paper_id)
    except ContentKnowledgeReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/sync")
def sync_content_knowledge(
    paper_id: str | None = Query(default=None),
    library_name: str | None = Query(default=None),
    include_candidates: bool = Query(default=True),
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        result = ContentKnowledgeService(session).sync_items(
            paper_id=paper_id, library_name=library_name, include_candidates=include_candidates
        )
        session.commit()
        return {"synced": True, **result}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/review-bundles")
def generate_content_review_bundle(
    payload: dict = Body(...), session: Session = Depends(get_db_session)
) -> dict:
    try:
        result = ContentReviewBundleService(session).generate(
            paper_id=UUID(str(payload.get("paper_id"))),
            run_id=UUID(str(payload["run_id"])) if payload.get("run_id") else None,
            created_by=str(payload.get("created_by") or "user"),
        )
        session.commit()
        return result
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/review-bundles/{bundle_id}/validate")
def validate_content_review_bundle(
    bundle_id: UUID,
    payload: dict = Body(...),
    session: Session = Depends(get_db_session),
    auth: MCPAuthInfo | None = Depends(get_optional_request_mcp_auth),
) -> dict:
    try:
        identity_verified = bool(auth and auth.identity_verified and auth.source_identity)
        result = ContentReviewBundleService(session).validate_result(
            bundle_id,
            payload,
            authenticated_identity=str(auth.source_identity) if identity_verified else None,
            authenticated_identity_verified=identity_verified,
        )
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review-bundles/{bundle_id}/apply")
def apply_content_review_bundle(
    bundle_id: UUID, payload: dict = Body(...), session: Session = Depends(get_db_session)
) -> dict:
    try:
        result = ContentReviewBundleService(session).apply_result(bundle_id, reviewer=str(payload.get("reviewer") or "human"))
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review-bundles/{bundle_id}/finalize")
def finalize_content_review_bundle(
    bundle_id: UUID, payload: dict = Body(default={}), session: Session = Depends(get_db_session)
) -> dict:
    try:
        result = ContentReviewBundleService(session).finalize_review(bundle_id, reviewer=str(payload.get("reviewer") or "human"))
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/writing-plan")
def content_writing_plan(payload: dict = Body(...), session: Session = Depends(get_db_session)) -> dict:
    query = str(payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="query must not be blank")
    try:
        paper_ids = [UUID(str(value)) for value in (payload.get("paper_ids") or [])]
        # Plans are intentionally read-only: citations retain links to existing
        # ContentEvidenceItem records and cannot become a new evidence source.
        result = ContentWritingPlanService(session).build(query=query, paper_ids=paper_ids or None)
        return result
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
