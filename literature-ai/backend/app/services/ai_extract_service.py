"""One-click AI extract job orchestration.

Creates a ``WorkflowJob`` of type ``ai_extract_rebuild``, builds a
self-contained agent prompt, dispatches via the Codex-web IPC bridge,
and provides status / result queries. Does NOT call OpenAI directly —
the actual extraction work is done by a Codex-web autonomous agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Paper,
    RebuildDataRow,
    RebuildDataValue,
    RebuildPaperFile,
    RebuildValueSource,
    RebuildVisualAsset,
    WorkflowJob,
)
from app.services.codex_web_dispatch import (
    CodexWebDispatchError,
    DispatchResult,
    dispatch_agent,
    dispatch_mode_for_model,
)
from app.services.rebuild_workflow_service import (
    RebuildWorkflowError,
    _required_paper,
    get_paper,
    list_assets,
    list_rows,
    resolve_paper_source_pdf,
    reading_coverage,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "ai_extract_rebuild"
RUNNING_STATUSES = {"queued", "dispatching", "running"}
TERMINAL_STATUSES = {"completed", "failed"}


class AiExtractError(Exception):
    """Business-level error for AI extract job operations."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _job_id() -> str:
    return f"aiext-{uuid4().hex[:24]}"


def get_latest_job(session: Session, paper_id: UUID) -> WorkflowJob | None:
    """Return the most recent AI extract job for this paper, or None."""
    return session.scalar(
        select(WorkflowJob)
        .where(
            WorkflowJob.type == JOB_TYPE,
            WorkflowJob.payload.op("->>")(  # type: ignore[union-attr]
                "paper_id"
            )
            == str(paper_id),
        )
        .order_by(WorkflowJob.created_at.desc())
    )


def has_running_job(session: Session, paper_id: UUID) -> WorkflowJob | None:
    """Return a running/dispatching job for this paper, if any."""
    # Do not let an older unresolved attempt disappear behind a newer row.
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE,
        WorkflowJob.payload.op("->>")("paper_id") == str(paper_id),
        WorkflowJob.status.in_(RUNNING_STATUSES)).order_by(WorkflowJob.created_at.desc()))
    if job is not None:
        return job
    latest = get_latest_job(session, paper_id)
    return latest if latest is not None and latest.status == "failed" else None


def _build_prompt(
    session: Session,
    paper_id: UUID,
    settings: Settings,
) -> tuple[str, str]:
    """Build the self-contained agent prompt and objective.

    Returns ``(prompt, objective)``.
    """
    paper = _required_paper(session, paper_id)
    paper_detail = get_paper(session, paper_id)

    # Resolve source PDF path for the agent to read.
    pdf_path: str | None = None
    try:
        resolved = resolve_paper_source_pdf(paper, settings)
        pdf_path = str(resolved)
    except RebuildWorkflowError:
        pdf_path = None

    work_package_url = f"/api/rebuild/papers/{paper_id}/ai-work-package"
    origin = getattr(settings, "oauth_issuer", "") or "https://dft.researchlife.top"

    title = paper.title or "(no title)"
    paper_code = paper.paper_code or paper_detail.get("paper_code") or "(no code)"

    objective = (
        f"Read paper {paper_code} ({title}), organise its figures into sub-figure "
        f"assets with Chinese explanations, then fill the reaction-template data "
        f"table for paper_id={paper_id}. Only process this paper."
    )

    prompt = f"""# AI 文献提取任务（paper_id={paper_id}）

## 论文信息
- paper_id: {paper_id}
- 编号: {paper_code}
- 标题: {title}
- DOI: {paper.doi or "(无)"}
- 期刊: {paper.journal or "(无)"}

## 源 PDF
- {'路径: ' + pdf_path if pdf_path else '未关联源 PDF；请先通过工作包接口查看已关联文件。'}
- 如果有 SI PDF，也在工作包中列出。

## 工作包地址（一次读取全部上下文）
GET {origin}{work_package_url}
该接口返回论文信息、反应模板、已关联文件列表、已有图表对象、已有数据行，
以及写入口说明。

## 写入口（逐条调用，不要批量）
1. 图表对象写入: POST {origin}/api/rebuild/papers/{paper_id}/assets/import
2. 图表裁图: POST {origin}/api/rebuild/papers/{paper_id}/assets/crop
3. 数据行写入: POST {origin}/api/rebuild/rows/import

## 工作要求（严格遵守）
1. **只处理 paper_id={paper_id} 这一篇论文**，不得跨论文合并来源。
2. **不启动旧解析链**（不要调用 /api/papers/*/parse 或 docling 等接口）。
3. **不改数据库 schema**；只通过上述写入口写入图表对象和数据行。
4. **不删除任何已有数据**。
5. **逐图解释**：从真实 PDF 裁图，保留页码与来源。短 explanation 仅补充；完整结果用 assets/import 或 crop 的 reading_explanation 对象保存：summary_zh、detailed_explanation_zh、subfigures（label/description/methods/findings）、evidence_locators（自身 file_id、page、quote；visual 可用 visual_observation）、uncertainties_zh。只写已核实内容，缺项留待补，禁止固定句冒充详解；不覆盖旧 PaperFigure 或移植不明来源解读。
6. **每个数值必须有来源**：标注页码、表号/图号、或引文；无法确定来源的留空。
7. **缺失留空**：不要编造数据；缺失字段写 missing_reason 说明原因。
8. **不同条件分行**：同一材料在不同测试条件下的数据应分成不同的数据行。
9. **实验与 DFT 分开**：data_type 分别用 experimental 和 dft。
10. **按反应模板填表**：SRR / HER / OER / ORR / CO2RR，每个反应的专属字段按模板填写。

## 完成标志
当所有图表对象和数据行写入完成后，在最终回复中输出：
EXTRACT_DONE: assets=<数量> rows=<数量> values=<数量> sources=<数量>
另逐项报告 reading_coverage 与未完成内容；执行结束不等于内容完整或独立科学验收。
"""

    return prompt, objective


def _result_summary(session: Session, paper_id: UUID) -> dict[str, Any]:
    """Compute current asset / row / value / source counts for a paper."""
    assets_count = session.scalar(
        select(func.count()).select_from(RebuildVisualAsset).where(
            RebuildVisualAsset.paper_id == paper_id
        )
    ) or 0
    rows_count = session.scalar(
        select(func.count()).select_from(RebuildDataRow).where(
            RebuildDataRow.paper_id == paper_id
        )
    ) or 0
    values_count = session.scalar(
        select(func.count()).select_from(RebuildDataValue).where(
            RebuildDataValue.paper_id == paper_id
        )
    ) or 0
    sources_count = session.scalar(
        select(func.count()).select_from(RebuildValueSource).where(
            RebuildValueSource.paper_id == paper_id
        )
    ) or 0
    return {
        "assets": assets_count,
        "rows": rows_count,
        "values": values_count,
        "sources": sources_count,
        "reading_coverage": reading_coverage(list_assets(session, paper_id)),
        "scientific_acceptance": "pending",
    }


def serialize_job(job: WorkflowJob, session: Session | None = None) -> dict[str, Any]:
    """Serialize a WorkflowJob for the API response."""
    payload = job.payload or {}
    paper_id_str = payload.get("paper_id")
    progress = job.progress or {}

    result: dict[str, Any] = {
        "job_id": job.job_id,
        "type": job.type,
        "status": job.status,
        "model": progress.get("model"),  # Chosen model, not an inference-verification claim.
        "chosen_model": payload.get("chosen_model"),
        "start_effective_model": progress.get("start_effective_model"),
        "start_effective_provider": progress.get("start_effective_provider"),
        "start_collaboration_verification": progress.get("start_collaboration_verification"),
        "actual_inference_verified": progress.get("actual_inference_verified", False),
        "model_verification_status": progress.get("model_verification_status", "unverified"),
        "thread_id": progress.get("thread_id"),
        "turn_id": progress.get("turn_id"),
        "dispatch_mode": progress.get("dispatch_mode"),
        "phase": progress.get("phase"),
        "attempt_token": payload.get("attempt_token"),
        "needs_check": progress.get("needs_check", False),
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "paper_id": paper_id_str,
    }

    # Include result summary for completed jobs or when session is available.
    if job.status == "completed" and isinstance(job.result, dict):
        result["result"] = job.result
    elif session is not None and paper_id_str:
        try:
            paper_id = UUID(paper_id_str)
            summary = _result_summary(session, paper_id)
            result["result"] = summary
        except (ValueError, Exception):
            result["result"] = job.result if isinstance(job.result, dict) else None
    else:
        result["result"] = job.result if isinstance(job.result, dict) else None

    if isinstance(result.get("result"), dict):
        result["result"] = {**result["result"], "execution_status": job.status}
    return result


async def create_ai_extract_job(
    session: Session,
    paper_id: UUID,
    settings: Settings,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Create and dispatch an AI extract job for one paper.

    Returns the serialized job. If a running job exists, returns it instead
    of creating a duplicate.
    """
    # Same-paper requests serialize before checking or inserting any job intent.
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise AiExtractError("paper_not_found")
    existing = has_running_job(session, paper_id)
    if existing is not None:
        result = serialize_job(existing, session)
        session.commit()
        return result
    chosen_model = model or settings.codex_web_default_model
    chosen_mode = dispatch_mode_for_model(chosen_model)
    attempt_token = str(uuid4())
    prompt, objective = _build_prompt(session, paper_id, settings)
    mode_instruction = ("Luna 普通任务：没有目标；禁止 create_goal、goal/set、设置或创建目标。"
                        if chosen_mode == "codex_web_ordinary" else
                        "DeepSeek V4.1 flash 目标任务：派发器已设置 active 目标，必须保留目标模式，不得另建目标。")
    prompt = (f"本轮派发边界：model={chosen_model}；mode={chosen_mode}；attempt_token={attempt_token}。\n"
              f"{mode_instruction} 不创建子任务、子代理，不 fork，不换模型，不部署或重启服务。\n"
              "EXTRACT_DONE 与产物总数只表示本任务结束待科学验收，不表示用户或协调者科学验收通过。\n\n" + prompt)
    if chosen_mode == "codex_web_target":
        if not prompt.strip() or len(prompt) > 4000:
            raise AiExtractError("目标任务说明超过 4000 字符或为空，未创建任务；请调整输入后重试。")
        # An active goal can wake before turn/start: give both paths identical scope.
        objective = prompt
    job = WorkflowJob(job_id=_job_id(),type=JOB_TYPE,status="dispatching",library_name=paper.library_name,
        payload={"paper_id":str(paper_id),"attempt_token":attempt_token,"chosen_model":chosen_model,
                 "chosen_mode":chosen_mode,"dispatch_intent_at":_utcnow().isoformat()},
        progress={"phase":"dispatching","dispatch_mode":chosen_mode,"model":chosen_model,
                  "dispatch_started":False,"events":[],"actual_inference_verified":False,
                  "model_verification_status":"pending_start_ack"})
    session.add(job)
    session.commit()  # Durable intent precedes scheduling and every native request.
    asyncio.create_task(_background_dispatch(job_id=job.job_id,database_url=settings.database_url,
        prompt=prompt,objective=objective,settings=settings,primary_model=chosen_model,
        attempt_token=attempt_token))
    return serialize_job(job, session)


async def _background_dispatch(*, job_id: str, database_url: str, prompt: str, objective: str,
                               settings: Settings, primary_model: str,
                               attempt_token: str | None = None,
                               fallback_model: str | None = None) -> None:
    """Execute exactly one persisted attempt. fallback_model is ignored compatibility input."""
    from app.db.session import session_scope
    with session_scope(database_url) as session:
        job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
        if job is None:
            return
        intent, progress = job.payload or {}, dict(job.progress or {})
        if (not attempt_token or intent.get("attempt_token") != attempt_token or
            intent.get("chosen_model") != primary_model or job.status != "dispatching" or
            progress.get("dispatch_started")):
            return  # A restart/second worker must never replay an intent.
        progress.update(dispatch_started=True,dispatch_started_at=_utcnow().isoformat())
        job.progress = progress

    def persist_event(event: dict[str, Any]) -> None:
        with session_scope(database_url) as session:
            job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
            if job is None or (job.payload or {}).get("attempt_token") != attempt_token:
                raise AiExtractError("dispatch_intent_identity_changed")
            progress = dict(job.progress or {})
            progress["events"] = [*(progress.get("events") or []), {**event,"recorded_at":_utcnow().isoformat()}]
            progress["last_method"] = event["method"]
            progress["last_rpc_phase"] = event["phase"]
            response = event.get("response") or {}
            result = response.get("result") if isinstance(response,dict) else {}
            if not isinstance(result,dict):
                result = {}
            if event["method"] == "thread/start" and isinstance(result.get("thread"),dict):
                if result["thread"].get("id"):
                    progress["thread_id"] = result["thread"]["id"]
                    progress["host_id"] = "local"
            elif event["method"] == "thread/goal/set" and isinstance(result.get("goal"),dict):
                progress["goal_status"] = result["goal"].get("status")
            elif event["method"] == "turn/start" and isinstance(result.get("turn"),dict):
                if result["turn"].get("id"):
                    progress["turn_id"] = result["turn"]["id"]
            if event["method"] == "thread/start" and event["phase"] == "effective_verified":
                for key in ("start_effective_model","start_effective_provider","start_collaboration_verification"):
                    progress[key] = event[key]
                progress["actual_inference_verified"] = False
                progress["model_verification_status"] = "pending_rollout_verification"
            job.progress = progress  # Each context commits before the next RPC.

    try:
        result = await dispatch_agent(prompt=prompt,objective=objective,settings=settings,
                                      model=primary_model,on_event=persist_event)
        with session_scope(database_url) as session:
            job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
            if job is not None:
                job.status = "running"
                job.error = None
                job.progress = {**(job.progress or {}),"phase":"running","needs_check":False,
                    "dispatch_mode":result.dispatch_mode,"thread_id":result.thread_id,"turn_id":result.turn_id,
                    "model":result.model,"goal_status":result.goal_status,
                    "start_effective_model":result.start_effective_model,
                    "start_effective_provider":result.start_effective_provider,
                    "start_collaboration_verification":result.start_collaboration_verification,
                    "actual_inference_verified":False,"model_verification_status":"pending_rollout_verification"}
    except Exception as exc:
        # RPC rejection, lost response, transport error, and persistence uncertainty all
        # preserve the same identity; none authorize fallback or another create.
        with session_scope(database_url) as session:
            job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
            if job is not None:
                job.status = "dispatching"
                job.error = f"{type(exc).__name__}: {exc}"[:1600]
                job.progress = {**(job.progress or {}),"phase":"needs_check","needs_check":True,
                                "automatic_retry":False}
        logger.warning("AI extract attempt %s needs readback; no task retried", job_id)


def _paper_library_name(session: Session, paper_id: UUID) -> str:
    paper = session.get(Paper, paper_id)
    return paper.library_name if paper else "默认文献库"


async def sync_job_status(
    session: Session,
    job: WorkflowJob,
    settings: Settings,
) -> WorkflowJob:
    """Read-through: if the job is running, check Codex-web thread status.

    Updates the job status in the database if the agent has completed or
    failed. If the thread cannot be read, the job stays running (no false
    failure).
    """
    if job.status not in RUNNING_STATUSES:
        return job
    progress = dict(job.progress or {})
    thread_id = progress.get("thread_id")
    if not thread_id:
        # Missing identity after restart/unknown start cannot be recovered by creating.
        session.refresh(job, with_for_update=True)
        job.progress = {**(job.progress or {}),"phase":"needs_check","needs_check":True,
                        "automatic_retry":False}
        session.flush()
        return job
    from app.services.codex_web_dispatch import read_thread_status
    mode = progress.get("dispatch_mode") or (job.payload or {}).get("chosen_mode")
    status = await read_thread_status(thread_id,settings=settings,include_goal=mode != "codex_web_ordinary")
    # Do not hold a DB lock while awaiting IPC; merge fresh progress after readback.
    session.refresh(job, with_for_update=True)
    progress = dict(job.progress or {})
    if status is None:
        return job
    observed = {"thread_id":status.thread_id,"turn_id":status.turn_id,"turn_status":status.turn_status,
                "goal_status":status.goal_status,"read_at":_utcnow().isoformat()}
    progress["readback"] = observed
    if (status.thread_id != thread_id or not progress.get("turn_id") or
        status.turn_id != progress.get("turn_id") or not mode):
        job.progress = {**progress,"phase":"needs_check","needs_check":True,"automatic_retry":False}
        session.flush()
        return job  # Unknown turn reply stays unknown; no turn mutation/replay.
    turn_status = (status.turn_status or "").lower()
    if turn_status == "completed" and (mode == "codex_web_ordinary" or status.goal_status in {"complete","completed"}):
        paper_id_str = (job.payload or {}).get("paper_id")
        summary = _result_summary(session, UUID(paper_id_str)) if paper_id_str else {}
        if status.final_report:
            summary["final_report"] = status.final_report[:2000]
        job.status = "completed"
        job.result = summary
        job.progress = {**progress,"phase":"completed","needs_check":False}
        session.flush()
        # Original detail reads both stores directly. Never overwrite legacy figures on job polling.
    elif turn_status in {"failed","interrupted"}:
        # Preserve a failed task for review, and block button-driven recreation.
        job.status = "failed"
        job.error = f"Codex-web turn {turn_status}"
        job.progress = {**progress,"phase":turn_status,"needs_check":True,"automatic_retry":False}
        session.flush()
    else:
        # A completed DS turn with an active/null goal is not task completion.
        phase = "running" if turn_status in {"inprogress","queued"} else "needs_check"
        job.progress = {**progress,"phase":phase,"needs_check":phase == "needs_check",
                        "latest_progress":(status.latest_progress or "")[:1000]}
        session.flush()
    return job


def _sync_rebuild_assets_to_paper_figures(session: Session, paper_id_str: str | None) -> None:
    """Compatibility no-op: current detail reads both stores, never synthesise legacy readings."""
    logger.info("Legacy writeback disabled; original figure images/readings remain unchanged")
