"""intake.py — Literature Intake MVP API

路由前缀: /api/intake
所有候选入口点，实现"搜索即候选，确认才入库"的核心门控逻辑。

端点列表:
  POST   /api/intake/search                               — 发起检索，生成候选，不入库
  GET    /api/intake/sessions/{session_id}                — 查询会话及候选列表
  POST   /api/intake/candidates/{candidate_id}/approve   — 标记 approved
  POST   /api/intake/candidates/{candidate_id}/reject    — 标记 rejected
  POST   /api/intake/candidates/{candidate_id}/ingest    — 仅 approved 可触发入库 job
  POST   /api/intake/sessions/{session_id}/ingest-approved — 批量触发已确认候选入库
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LiteratureIntakeCandidate, LiteratureIntakeSession
from app.db.session import get_db_session
from app.services.discovery_service import DiscoveryService
from app.services.intake_screening_service import IntakeScreeningService
from app.services.workflow_jobs import (
    JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
    create_job,
    dispatch_job,
    get_job,
    serialize_job,
)
from app.utils.library_names import normalize_library_name

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 请求 / 响应 Schema
# ---------------------------------------------------------------------------

class IntakeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="检索关键词或研究需求描述")
    user_need: str | None = Field(None, max_length=2000, description="研究需求的自然语言描述（可选，比 query 更详细）")
    library_name: str | None = Field(None, description="目标文献库名称")
    providers: list[str] = Field(default_factory=list, description="检索数据源，空列表使用默认")
    max_results: int = Field(default=20, ge=1, le=100, description="检索上限")
    target_types: list[str] | None = Field(None, description="目标文献类型")


class RejectRequest(BaseModel):
    reason: str | None = Field(None, max_length=500, description="拒绝理由（可选）")


class SingleIngestRequest(BaseModel):
    library_name: str | None = None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

_INGEST_GATE_STATUSES = {"ingesting", "ingested", "metadata_only"}
_INGESTABLE_STATUS = "approved"


def _serialize_candidate(c: LiteratureIntakeCandidate) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "session_id": str(c.session_id),
        "title": c.title,
        "doi": c.doi,
        "year": c.year,
        "journal": c.journal,
        "authors": c.authors or [],
        "abstract": c.abstract,
        "identifier": c.identifier,
        "url": c.url,
        "pdf_url": c.pdf_url,
        "providers": c.providers or [],
        "relevance_score": c.relevance_score,
        "screening_tier": c.screening_tier,
        "screening_reason": c.screening_reason,
        "risk_flags": c.risk_flags or [],
        "status": c.status,
        "reject_reason": c.reject_reason,
        "duplicate_paper_id": str(c.duplicate_paper_id) if c.duplicate_paper_id else None,
        "ingest_job_id": c.ingest_job_id,
        "ingested_paper_id": str(c.ingested_paper_id) if c.ingested_paper_id else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _serialize_session(
    s: LiteratureIntakeSession,
    candidates: list[LiteratureIntakeCandidate],
) -> dict[str, Any]:
    candidate_list = [_serialize_candidate(c) for c in candidates]
    counts: dict[str, int] = {}
    for c in candidates:
        counts[c.status] = counts.get(c.status, 0) + 1
    return {
        "id": str(s.id),
        "library_name": s.library_name,
        "user_need": s.user_need,
        "original_query": s.original_query,
        "rewritten_query": s.rewritten_query,
        "providers": s.providers or [],
        "target_types": s.target_types,
        "max_results": s.max_results,
        "status": s.status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "candidate_count": len(candidates),
        "candidate_counts_by_status": counts,
        "candidates": candidate_list,
        # UI 提示：候选未入库、不可引用
        "intake_notice": (
            "⚠️ 以下候选文献尚未入库，未经解析，不可引用。"
            "请逐一确认或拒绝后，点击'确认收录'才会触发下载和入库。"
        ),
    }


def _get_candidate_or_404(session: Session, candidate_id: UUID) -> LiteratureIntakeCandidate:
    c = session.get(LiteratureIntakeCandidate, candidate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return c


def _trigger_ingest_job(
    candidate: LiteratureIntakeCandidate,
    session: Session,
    background_tasks: BackgroundTasks,
    library_name: str | None = None,
) -> dict[str, Any]:
    """为一个已 approved 的候选创建 ingest job 并派发。"""
    # 构建与现有 discovery_download_ingest 兼容的 payload
    target_library = normalize_library_name(library_name or candidate.session.library_name if hasattr(candidate, "session") else None)
    # 直接从候选字段获取 library_name（通过 session_id 反查开销较大，改用 JOIN）
    stmt = select(LiteratureIntakeSession.library_name).where(
        LiteratureIntakeSession.id == candidate.session_id
    )
    sess_lib = session.scalar(stmt)
    if library_name is None:
        library_name = normalize_library_name(sess_lib)

    identifier = candidate.doi or candidate.identifier or candidate.url or candidate.title or ""
    if not identifier:
        raise HTTPException(status_code=422, detail="Candidate has no usable identifier for download")

    payload = {
        "identifier": identifier,
        "providers": candidate.providers or [],
        "library_name": library_name,
        # 传递候选原始元数据，供 ingest_metadata_only 降级时使用
        "title": candidate.title,
        "doi": candidate.doi,
        "authors": candidate.authors or [],
        "year": candidate.year,
        "journal": candidate.journal,
        "abstract": candidate.abstract,
        # intake 溯源
        "_intake_candidate_id": str(candidate.id),
    }
    bind = session.get_bind()
    db_url = bind.engine.url.render_as_string(hide_password=False) if bind else None

    job = create_job(
        session,
        job_type=JOB_TYPE_DISCOVERY_DOWNLOAD_INGEST,
        library_name=library_name,
        payload=payload,
        runtime_context={},
        progress={
            "phase": "queued",
            "message": f"Intake 候选入库：{candidate.title or identifier}",
            "identifier": identifier,
            "_intake_candidate_id": str(candidate.id),
        },
    )

    # 更新候选状态
    candidate.status = "ingesting"
    candidate.ingest_job_id = job.job_id
    session.add(candidate)
    session.commit()
    session.refresh(candidate)

    dispatch_job(job.job_id, background_tasks, control_database_url=db_url)
    return serialize_job(job)


# ---------------------------------------------------------------------------
# 端点实现
# ---------------------------------------------------------------------------

@router.post("/search", summary="发起 AI 检索，生成候选（不入库）")
async def intake_search(
    payload: IntakeSearchRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """
    接受研究需求 + 检索关键词，调用 DiscoveryService 检索，
    使用 IntakeScreeningService 进行规则筛选，
    将结果写入 literature_intake_candidates 表。

    **不会**写入 papers 表。用户必须通过 /approve 后再 /ingest 才能入库。
    """
    library_name = normalize_library_name(payload.library_name)
    user_need = payload.user_need or payload.query

    # 1. 创建会话记录
    s = LiteratureIntakeSession(
        library_name=library_name,
        user_need=user_need,
        original_query=payload.query,
        providers=payload.providers or DiscoveryService.DEFAULT_SEARCH_PROVIDERS,
        target_types=payload.target_types,
        max_results=payload.max_results,
        status="searching",
    )
    session.add(s)
    session.flush()
    session_id = s.id

    try:
        # 2. 调用检索服务（同步但包含 HTTP，用 run_in_threadpool 避免阻塞）
        disc = DiscoveryService()
        active_providers = payload.providers or disc.DEFAULT_SEARCH_PROVIDERS
        raw_results: list[dict] = await run_in_threadpool(
            disc.search,
            payload.query,
            active_providers,
            payload.max_results,
            payload.target_types,
        )

        # 3. AI/规则筛选
        screener = IntakeScreeningService(session, library_name=library_name)
        screening_results = await run_in_threadpool(
            screener.screen,
            raw_results,
            user_need=user_need,
            query=payload.query,
            target_types=payload.target_types,
        )

        # 4. 写入候选（不入 papers 表）
        raw_by_idx = {r.get("doi") or r.get("identifier") or r.get("title"): r for r in raw_results}
        candidates: list[LiteratureIntakeCandidate] = []
        for sr, raw in zip(screening_results, raw_results):
            status = "duplicate" if sr.is_duplicate else "pending_review"
            c = LiteratureIntakeCandidate(
                session_id=session_id,
                title=raw.get("title"),
                doi=raw.get("doi"),
                year=raw.get("year"),
                journal=raw.get("journal"),
                authors=raw.get("authors") or [],
                abstract=raw.get("abstract"),
                identifier=raw.get("identifier") or raw.get("doi") or raw.get("url"),
                url=raw.get("url"),
                pdf_url=raw.get("pdf_url"),
                providers=raw.get("databases") or [],
                relevance_score=sr.relevance_score,
                screening_tier=sr.screening_tier,
                screening_reason=sr.screening_reason,
                risk_flags=sr.risk_flags,
                status=status,
                duplicate_paper_id=(
                    UUID(sr.duplicate_paper_id) if sr.duplicate_paper_id else None
                ),
            )
            session.add(c)
            candidates.append(c)

        # 5. 更新会话状态
        s.status = "pending_review"
        s.rewritten_query = payload.query  # 如有 LLM 改写可在此覆盖
        session.commit()

        # 刷新所有候选
        for c in candidates:
            session.refresh(c)
        session.refresh(s)

        return _serialize_session(s, candidates)

    except Exception as exc:
        logger.exception("Intake search failed for session %s", session_id)
        s.status = "cancelled"
        session.commit()
        raise HTTPException(status_code=500, detail=f"检索失败：{exc}") from exc


@router.get("/sessions/{session_id}", summary="获取会话详情及候选列表")
async def get_intake_session(
    session_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    s = session.get(LiteratureIntakeSession, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Intake session not found")

    candidates = list(
        session.scalars(
            select(LiteratureIntakeCandidate)
            .where(LiteratureIntakeCandidate.session_id == session_id)
            .order_by(
                LiteratureIntakeCandidate.relevance_score.desc().nulls_last(),
                LiteratureIntakeCandidate.created_at.asc(),
            )
        ).all()
    )
    return _serialize_session(s, candidates)


@router.get("/sessions", summary="列出近期检索会话")
async def list_intake_sessions(
    library_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = (
        select(LiteratureIntakeSession)
        .order_by(LiteratureIntakeSession.created_at.desc())
        .limit(limit)
    )
    if library_name:
        stmt = stmt.where(
            LiteratureIntakeSession.library_name == normalize_library_name(library_name)
        )
    if status:
        stmt = stmt.where(LiteratureIntakeSession.status == status)

    sessions = list(session.scalars(stmt).all())
    result = []
    for s in sessions:
        candidate_count = session.scalar(
            select(LiteratureIntakeCandidate.id)
            .where(LiteratureIntakeCandidate.session_id == s.id)
            .limit(1)
        )
        result.append({
            "id": str(s.id),
            "library_name": s.library_name,
            "original_query": s.original_query,
            "user_need": s.user_need,
            "status": s.status,
            "max_results": s.max_results,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "has_candidates": candidate_count is not None,
        })
    return result


@router.post("/candidates/{candidate_id}/approve", summary="确认候选（不入库）")
async def approve_candidate(
    candidate_id: UUID,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """标记候选为 approved。仍不触发任何下载或入库操作。"""
    c = _get_candidate_or_404(session, candidate_id)

    if c.status in _INGEST_GATE_STATUSES | {"ingested"}:
        raise HTTPException(
            status_code=409,
            detail=f"候选已进入入库流程，不可重复确认（当前状态：{c.status}）",
        )

    c.status = "approved"
    c.reject_reason = None
    session.add(c)
    session.commit()
    session.refresh(c)
    return _serialize_candidate(c)


@router.post("/candidates/{candidate_id}/reject", summary="拒绝候选")
async def reject_candidate(
    candidate_id: UUID,
    payload: RejectRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """标记候选为 rejected，保存拒绝理由。已入库的候选不可拒绝。"""
    c = _get_candidate_or_404(session, candidate_id)

    if c.status in _INGEST_GATE_STATUSES | {"ingested"}:
        raise HTTPException(
            status_code=409,
            detail=f"候选已进入入库流程，无法拒绝（当前状态：{c.status}）",
        )

    c.status = "rejected"
    c.reject_reason = payload.reason
    session.add(c)
    session.commit()
    session.refresh(c)
    return _serialize_candidate(c)


@router.post("/candidates/{candidate_id}/ingest", summary="触发候选入库（仅 approved）")
async def ingest_candidate(
    candidate_id: UUID,
    background_tasks: BackgroundTasks,
    payload: SingleIngestRequest | None = None,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """
    **核心门控**：只有 status == 'approved' 的候选才能触发入库 job。

    - pending_review → 400 candidate_not_approved
    - rejected       → 400 candidate_rejected
    - ingesting/ingested → 409 already_ingesting
    """
    c = _get_candidate_or_404(session, candidate_id)

    # ---- 门控检查 ----
    if c.status == "rejected":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "candidate_rejected",
                "message": "该候选已被拒绝，无法触发入库。如需收录，请先重置为 pending_review。",
                "current_status": c.status,
            },
        )
    if c.status == "pending_review":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "candidate_not_approved",
                "message": "候选尚未确认（pending_review），必须先点击'确认收录'后才能触发入库。",
                "current_status": c.status,
            },
        )
    if c.status in _INGEST_GATE_STATUSES | {"ingested"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_ingesting",
                "message": f"候选已处于入库流程中（当前状态：{c.status}）。",
                "current_status": c.status,
                "ingest_job_id": c.ingest_job_id,
            },
        )
    if c.status == "duplicate":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "candidate_duplicate",
                "message": "系统检测到该文献已存在于库中。如仍需收录，请先将状态重置为 approved。",
                "duplicate_paper_id": str(c.duplicate_paper_id) if c.duplicate_paper_id else None,
            },
        )

    # ---- 触发 job ----
    library_name = (payload.library_name if payload else None)
    job_data = _trigger_ingest_job(c, session, background_tasks, library_name=library_name)

    return {
        "candidate": _serialize_candidate(c),
        "job": job_data,
    }


@router.post("/sessions/{session_id}/ingest-approved", summary="批量触发已确认候选入库")
async def batch_ingest_approved(
    session_id: UUID,
    background_tasks: BackgroundTasks,
    library_name: str | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """
    批量将本 session 内所有 status == 'approved' 的候选触发入库 job。
    每个候选单独创建一个 WorkflowJob，互相独立，失败不影响其他条目。
    """
    s = session.get(LiteratureIntakeSession, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Intake session not found")

    approved_candidates = list(
        session.scalars(
            select(LiteratureIntakeCandidate)
            .where(LiteratureIntakeCandidate.session_id == session_id)
            .where(LiteratureIntakeCandidate.status == "approved")
        ).all()
    )

    if not approved_candidates:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_approved_candidates",
                "message": "本次检索会话中没有已确认的候选。请先逐一点击'确认收录'。",
            },
        )

    triggered = []
    failed = []
    for c in approved_candidates:
        try:
            job_data = _trigger_ingest_job(c, session, background_tasks, library_name=library_name)
            triggered.append({
                "candidate_id": str(c.id),
                "title": c.title,
                "job_id": job_data.get("job_id"),
            })
        except Exception as exc:
            logger.warning("Failed to trigger ingest for candidate %s: %s", c.id, exc)
            failed.append({
                "candidate_id": str(c.id),
                "title": c.title,
                "error": str(exc),
            })

    # 更新 session 状态
    if triggered:
        s.status = "reviewing"
        session.add(s)
        session.commit()

    return {
        "session_id": str(session_id),
        "triggered_count": len(triggered),
        "failed_count": len(failed),
        "triggered": triggered,
        "failed": failed,
        "message": (
            f"已为 {len(triggered)} 篇候选创建入库任务"
            + (f"，{len(failed)} 篇失败" if failed else "")
            + "。入库过程在后台进行，可通过任务中心查看进度。"
        ),
    }
