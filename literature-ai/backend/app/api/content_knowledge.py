from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from io import BytesIO
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
from app.services.content_review_bundle_service import (
    CONTENT_REVIEW_BUNDLE_V1_DEPRECATED_CODE,
    ContentReviewBundleService,
)
from app.services.content_web_review_bundle_v2_service import ContentWebReviewBundleV2Service
from app.services.content_writing_plan_service import ContentWritingPlanService

router = APIRouter()


def _raise_content_review_bundle_v1_deprecated() -> None:
    raise HTTPException(
        status_code=410,
        detail={
            "code": CONTENT_REVIEW_BUNDLE_V1_DEPRECATED_CODE,
            "message": "content review bundle v1 is read-only; use canonical object review gates",
            "read_only": True,
        },
    )


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
    _raise_content_review_bundle_v1_deprecated()


@router.get("/review-bundles/{bundle_id}")
def get_content_review_bundle_readonly(
    bundle_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict:
    try:
        return ContentReviewBundleService(session).get_readonly(bundle_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/review-bundles/v2")
def generate_content_web_review_bundle_v2(
    payload: dict = Body(...), session: Session = Depends(get_db_session)
) -> dict:
    """Create a proposal-only content web-AI review package; it has no apply path."""
    try:
        result = ContentWebReviewBundleV2Service(session).generate(
            paper_id=UUID(str(payload.get("paper_id"))),
            module=str(payload["module"]) if payload.get("module") is not None else None,
            modules=payload.get("modules"),
            created_by=str(payload.get("created_by") or "user"),
        )
        session.commit()
        return result
    except (ValueError, TypeError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/review-bundles/{bundle_id}/download")
def download_content_web_review_bundle_v2(
    bundle_id: UUID, session: Session = Depends(get_db_session)
) -> StreamingResponse:
    try:
        result = ContentWebReviewBundleV2Service(session).download(bundle_id)
        return StreamingResponse(
            BytesIO(result["content"]), media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{result["filename"]}"',
                "Content-Length": str(len(result["content"])),
                "Cache-Control": "no-store",
                "X-LitAI-Bundle-Fingerprint": result["fingerprint"],
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/review-bundles/{bundle_id}/web-proposal/validate")
def validate_content_web_review_proposal_v2(
    bundle_id: UUID, payload: dict = Body(...), session: Session = Depends(get_db_session)
) -> dict:
    try:
        result = ContentWebReviewBundleV2Service(session).validate_web_proposal(bundle_id, payload)
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/review-bundles/{bundle_id}/local-verification-plan")
def content_web_review_local_verification_plan_v2(
    bundle_id: UUID, session: Session = Depends(get_db_session)
) -> dict:
    try:
        result = ContentWebReviewBundleV2Service(session).local_verification_plan(bundle_id)
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review-bundles/{bundle_id}/validate")
def validate_content_review_bundle(
    bundle_id: UUID,
    payload: dict = Body(...),
    session: Session = Depends(get_db_session),
    auth: MCPAuthInfo | None = Depends(get_optional_request_mcp_auth),
) -> dict:
    _raise_content_review_bundle_v1_deprecated()


@router.post("/review-bundles/{bundle_id}/apply")
def apply_content_review_bundle(
    bundle_id: UUID, payload: dict = Body(...), session: Session = Depends(get_db_session)
) -> dict:
    _raise_content_review_bundle_v1_deprecated()


@router.post("/review-bundles/{bundle_id}/finalize")
def finalize_content_review_bundle(
    bundle_id: UUID, payload: dict = Body(default={}), session: Session = Depends(get_db_session)
) -> dict:
    _raise_content_review_bundle_v1_deprecated()


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
