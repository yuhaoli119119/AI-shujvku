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

    prompt = f"""# 有界文献提取 paper_id={paper_id}
编号 {paper_code}；标题 {title}；原始PDF {pdf_path or '通过工作包读取'}。
只处理本篇，保留已有正确结果、不删除，不启动旧解析链，不改schema，不派子任务或自行部署。
工作包：GET {origin}/api/rebuild/papers/{paper_id}/staged-work-package?stage=source_scope&limit=25。
按stage_index读取当前合同、文件清单和需要的分页；rows/assets/sections/legacy_tables至has_more=false，保持output_revision一致，409后重新只读收集。复用未变来源与已存成果。
按file_id和真实PDF物理页阅读正文/SI，DOCX可使用哈希绑定派生PDF或已有OOXML合同，page=null时保留合法ooxml_locator，不能伪造页码。
图表写入 POST /api/rebuild/papers/{paper_id}/assets/import，裁图用assets/crop，数据先POST /api/rebuild/rows/import/preflight再rows/import，章节用POST /api/rebuild/papers/{paper_id}/sections/import。所有写入遵守工作包当前合同、登录会话与CSRF；读取逐项回执，有界小批，同篇串行。
请求保存原始字节、稳定request_id/task_id。未知结果重放原体原ID查回，不能重建身份、覆盖冲突或因HTTP200当全成功。有限退避后保留诊断。
实验与DFT分开、不同条件分行；每值保留自身file_id/页/图表/原句；未报告留空并注明missing_reason，不猜对象、组合或步骤。按field_semantics使用反应模板，允许有依据估读并标记。表格按table_write_contract保留原文结构、精度及跨页caption/body来源；图表用reading_explanation保存有依据的中文解读和不确定性。
科学章节由DS负责；完整text与有依据explanation_zh分存，保留file-local section_key、源hash、原文定位；未知写结果重放原请求。非DS仅报告章节缺口。
结束前读quality_endpoint，只修真实缺陷，局部失败保存可用成果、明确partial/unknown和未完成文件。已存计数、metadata、模型completed或空errors均不代表全篇覆盖或科学accepted。不要启动队列或另行独立验收；按上级指定范围交接。
最终报告 EXTRACT_DONE: assets=<数量> rows=<数量> values=<数量> sources=<数量>，同时列各file_id已读范围、sections数量、未完成原因；执行结束不是科学验收。
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
        "needs_attention": progress.get("needs_attention", False),
        "evidence_guard": progress.get("evidence_guard"),
        "native_stop": progress.get("native_stop"),
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "paper_id": paper_id_str,
    }

    if payload.get("parse_contract"):
        from app.services.ordinary_parsing import progress_summary
        result.update(parse_contract=payload["parse_contract"], checkpoint_revision=progress.get("checkpoint_revision", 0),
                      file_progress=progress_summary(progress.get("file_plan", {})),
                      native_terminal_verified=progress.get("native_terminal_verified", False),
                      can_retry=job.status in TERMINAL_STATUSES and bool(progress.get("native_terminal_verified")),
                      latest_progress=progress.get("latest_progress"))
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
    if session is not None and paper_id_str:
        from app.services.paper_processing_status import processing_statuses
        result['processing_status'] = processing_statuses(session,[UUID(paper_id_str)])[UUID(paper_id_str)]
    return result


async def create_ai_extract_job(
    session: Session,
    paper_id: UUID,
    settings: Settings,
    *,
    model: str | None = None,
    request_id: str | None = None,
    file_ids: list[str] | None = None,
    retry: bool = False,
    source_job_id: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Create and dispatch an AI extract job for one paper.

    Returns the serialized job. If a running job exists, returns it instead
    of creating a duplicate.
    """
    # Same-paper requests serialize before checking or inserting any job intent.
    from app.services.rebuild_queue import lock_scheduler, paper_busy, TYPE as PIPELINE_TYPE
    lock_scheduler(session)
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise AiExtractError("paper_not_found")
    from app.services.ordinary_parsing import VERSION, file_plan, build_prompt
    if request_id:
        replay = session.scalar(select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE,
            WorkflowJob.payload.op("->>")("paper_id") == str(paper_id),
            WorkflowJob.payload.op("->>")("request_id") == request_id))
        if replay:
            if ((replay.payload or {}).get("request_scope") != {"model": model, "file_ids": file_ids, "retry": retry} or
                    (replay.payload or {}).get("requested_source_job_id") != source_job_id or
                    (replay.payload or {}).get("actor") is not None and replay.payload["actor"] != actor):
                raise AiExtractError("parse_request_id_reused")
            return serialize_job(replay, session)
    occupied = paper_busy(session, paper_id)
    if occupied is not None and occupied.type == PIPELINE_TYPE:
        raise AiExtractError("paper_has_unresolved_pipeline_attempt")
    if occupied is not None and occupied.type == "rebuild_external_extract":
        raise AiExtractError("paper_has_unresolved_external_writer_attempt")
    if occupied is not None and occupied.type == "paper_reading_assistance":
        raise AiExtractError("paper_has_unresolved_reading_attempt")
    existing = has_running_job(session, paper_id)
    if existing is not None and retry and existing.status == "failed" and (existing.progress or {}).get("native_terminal_verified"):
        existing = None
    if existing is not None:
        result = serialize_job(existing, session)
        session.commit()
        return result
    chosen_model = model or settings.codex_web_default_model
    chosen_mode = dispatch_mode_for_model(chosen_model)
    from app.services.reading_thread_continuation import confirmed_source
    try:
        continuation = await confirmed_source(session, paper_id, settings, source_job_id, actor)
    except ValueError as exc:
        raise AiExtractError(str(exc)) from exc
    if continuation and model is None:
        chosen_model=continuation['source_model']
        chosen_mode=dispatch_mode_for_model(chosen_model)
    if continuation and chosen_model != continuation.get('source_model', 'cn:deepseek-v4.1-flash'):
        raise AiExtractError('same_thread_requires_deepseek')
    attempt_token = str(uuid4())
    from app.services.ordinary_parsing import saved_file_plan
    old_plan = saved_file_plan(session, paper_id) if retry else {}
    try:
        plan = file_plan(session, paper_id, settings, previous=old_plan, selected=file_ids)
    except ValueError as exc:
        raise AiExtractError(str(exc)) from exc
    jid = _job_id()
    prompt = build_prompt(paper_id, plan, jid, getattr(settings, "oauth_issuer", "") or "https://47.94.252.147:49004")
    mode_instruction = ("Luna 普通任务：没有目标，不创建目标。" if chosen_mode == "codex_web_ordinary" else
                        "DeepSeek 目标任务：目标已设置；文件各阶段处理完（允许partial/failed）即结束普通解析目标。")
    prompt = f"{mode_instruction} attempt_token={attempt_token}。不通过processing-status另建外部任务。\n" + prompt
    objective = prompt
    if chosen_mode == "codex_web_target" and len(prompt) > 4000:
        raise AiExtractError("ordinary_prompt_exceeds_native_limit")
    from app.services.rebuild_progress_guard import initial_guard, evidence_signature
    evidence_guard = initial_guard(evidence_signature(session, paper_id))
    job = WorkflowJob(job_id=jid,type=JOB_TYPE,status="dispatching",library_name=paper.library_name,
        payload={"paper_id":str(paper_id),"attempt_token":attempt_token,"chosen_model":chosen_model,
                 "parse_contract":VERSION,"request_id":request_id,
                 "actor":actor,"requested_source_job_id":source_job_id,
                 **({"continuation":continuation} if continuation else {}),
                 "request_scope":{"model":model,"file_ids":file_ids,"retry":retry},
                 "dispatch_prompt":prompt,"dispatch_objective":objective,
                 "chosen_mode":chosen_mode,"file_ids":list(plan),"dispatch_intent_at":_utcnow().isoformat()},
        progress={"file_plan":plan,"checkpoint_revision":0,"evidence_guard": evidence_guard, "phase":"dispatching","dispatch_mode":chosen_mode,"model":chosen_model,
                  "dispatch_started":False,"events":[],"actual_inference_verified":False,
                  **({"thread_id":continuation["thread_id"],"previous_turn_id":continuation["turn_id"]} if continuation else {}),
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
            if event["method"] == "thread/resume" and event["phase"] == "effective_verified":
                progress.update(effective_model=event['effective_model'],effective_provider=event['effective_provider'],
                    effective_permissions={'approval_policy':event['approval_policy'],'sandbox_type':event['sandbox_type']})
            if event["phase"] == "auto_start_adopted":
                progress["turn_id"] = event["turn_id"]
            if event["method"] == "thread/start" and event["phase"] == "effective_verified":
                for key in ("start_effective_model","start_effective_provider","start_collaboration_verification"):
                    progress[key] = event[key]
                progress["actual_inference_verified"] = False
                progress["model_verification_status"] = "pending_rollout_verification"
            job.progress = progress  # Each context commits before the next RPC.

    try:
        result = await dispatch_agent(prompt=prompt,objective=objective,settings=settings,
                                      model=primary_model,on_event=persist_event,
                                      **({"continuation": intent["continuation"]} if intent.get("continuation") else {}))
        with session_scope(database_url) as session:
            job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
            if job is not None and job.status in RUNNING_STATUSES and (job.payload or {}).get("attempt_token") == attempt_token:
                job.status = "running"
                job.error = None
                job.progress = {**(job.progress or {}),"phase":"running","needs_check":False,
                    "dispatch_mode":result.dispatch_mode,"thread_id":result.thread_id,"turn_id":result.turn_id,
                    "model":result.model,"goal_status":result.goal_status,
                    "effective_model":result.start_effective_model,"effective_provider":result.start_effective_provider,
                    "start_effective_model":result.start_effective_model,
                    "start_effective_provider":result.start_effective_provider,
                    "start_collaboration_verification":result.start_collaboration_verification,
                    "actual_inference_verified":False,"model_verification_status":"pending_rollout_verification"}
    except Exception as exc:
        # RPC rejection, lost response, transport error, and persistence uncertainty all
        # preserve the same identity; none authorize fallback or another create.
        with session_scope(database_url) as session:
            job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
            if job is not None and job.status in RUNNING_STATUSES and (job.payload or {}).get("attempt_token") == attempt_token:
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
    ordinary = (job.payload or {}).get("parse_contract") == "ordinary_parse_v1"
    if progress.get("needs_attention") and not ordinary:
        return job  # Explicit reconciliation is required; no perpetual native reads.
    thread_id = progress.get("thread_id")
    if not thread_id:
        # Missing identity after restart/unknown start cannot be recovered by creating.
        session.refresh(job, with_for_update=True)
        job.progress = {**(job.progress or {}),"phase":"needs_check","needs_check":True,
                        "automatic_retry":False}
        session.flush()
        return job
    from app.services.codex_web_dispatch import read_thread_status, matches_owned_attempt
    mode = progress.get("dispatch_mode") or (job.payload or {}).get("chosen_mode")
    status = await read_thread_status(thread_id,settings=settings,include_goal=mode != "codex_web_ordinary")
    # Do not hold a DB lock while awaiting IPC; merge fresh progress after readback.
    session.refresh(job, with_for_update=True)
    if job.status not in RUNNING_STATUSES:
        return job
    progress = dict(job.progress or {})
    observed = {"thread_id": getattr(status, "thread_id", None), "turn_id": getattr(status, "turn_id", None),
                "turn_status": getattr(status, "turn_status", None), "goal_status": getattr(status, "goal_status", None),
                "item_count": getattr(status, "item_count", None), "thread_status": getattr(status, "thread_status", None),
                "goal_observed": getattr(status, "goal_observed", False), "read_at": _utcnow().isoformat()}
    progress["readback"] = observed
    progress["status_checked_at"] = _utcnow().isoformat()
    from app.services.rebuild_progress_guard import observe, evidence_signature
    paper_id_str = (job.payload or {}).get("paper_id")
    if paper_id_str:
        progress["evidence_guard"] = observe(progress.get("evidence_guard"), evidence_signature(session, UUID(paper_id_str)), observed)
        if progress["evidence_guard"]["needs_attention"] and not ordinary:
            job.progress = {**progress, "phase": "needs_attention", "needs_attention": True,
                            "needs_check": True, "automatic_retry": False}
            session.flush()
            if getattr(settings, "rebuild_native_stop_enabled", False):
                # This read-through path owns only job-status metadata. Release
                # its row lock before the durable stop adapter journals RPCs.
                session.commit()
                from app.services.rebuild_native_stop import protective_stop
                await protective_stop(settings.database_url, job.job_id, settings=settings)
                session.refresh(job)
            return job
    if status is None:
        job.progress = {**progress, "phase": "needs_check", "needs_check": True, "automatic_retry": False}
        session.flush()
        return job
    if ordinary and not progress.get("turn_id") and (job.payload or {}).get('continuation'):
        from app.services.reading_thread_continuation import recover_turn
        recovered = recover_turn(job, status)
        if recovered: progress['turn_id'] = recovered
        else:
            job.progress = {**progress,'phase':'needs_check','needs_check':True,'automatic_retry':False}
            session.flush()
            return job
    if ordinary and not progress.get("turn_id"):
        from app.services.ordinary_parsing import recover_auto_started_turn
        recovered = recover_auto_started_turn(job.payload or {}, progress, status, settings)
        if recovered:
            progress.update(turn_id=recovered["turn_id"], identity_recovery=recovered)
            job.error = None
            job.status = "running"
    from app.services.paper_native_models import stage_fallback
    if (ordinary and status.goal_objective==(job.payload or {}).get('dispatch_objective') and
            matches_owned_attempt(status,thread_id,progress.get('turn_id'),allow_goal_continuations=True)):
        progress.update(start_turn_id=progress.get('start_turn_id') or progress.get('turn_id'),turn_id=status.turn_id)
        job.progress=progress
    if ordinary and stage_fallback(job,status):
        session.flush();return job
    plan = progress.get("file_plan", {})
    stages_processed = bool(plan) and all(part.get("status") in {"completed", "partial", "failed"}
        for file in plan.values() for part in file.get("stages", {}).values())
    confirmed_absent_idle = (ordinary and stages_processed and getattr(status, "goal_observed", False)
                            and status.goal_status is None and getattr(status, "thread_status", None) in {"idle", "notLoaded"})
    if not mode or not matches_owned_attempt(status, thread_id, progress.get("turn_id"),
                                             allow_goal_continuations=mode == "codex_web_target"):
        job.progress = {**progress,"phase":"needs_check","needs_check":True,"automatic_retry":False}
        session.flush()
        return job  # Unknown turn reply stays unknown; no turn mutation/replay.
    turn_status = (status.turn_status or "").lower()
    if turn_status == "completed" and (mode == "codex_web_ordinary" or status.goal_status in {"complete","completed"} or confirmed_absent_idle):
        if not ordinary and not (progress.get("evidence_guard") or {}).get("business_advanced"):
            job.progress = {**progress, "phase": "awaiting_evidence", "needs_check": True,
                            "automatic_retry": False, "completion_blocked": "no_new_business_evidence"}
            session.flush()
            return job
        paper_id_str = (job.payload or {}).get("paper_id")
        summary = _result_summary(session, UUID(paper_id_str)) if paper_id_str else {}
        if status.final_report:
            summary["final_report"] = status.final_report[:2000]
        if ordinary:
            from app.services.ordinary_parsing import progress_summary
            ordinary_summary = progress_summary(progress.get("file_plan", {}))
            progress.update(native_terminal_verified=True, native_terminal=observed,
                            content_status=ordinary_summary["content_status"],
                            goal_terminal_evidence="confirmed_absent_idle_after_processed_stages" if confirmed_absent_idle else status.goal_status)
            summary["file_progress"] = ordinary_summary
        job.status = "completed"
        observed_files = set(session.scalars(select(RebuildVisualAsset.file_id).where(RebuildVisualAsset.paper_id==UUID(paper_id_str),RebuildVisualAsset.file_id.is_not(None)))) | set(session.scalars(select(RebuildValueSource.file_id).where(RebuildValueSource.paper_id==UUID(paper_id_str),RebuildValueSource.file_id.is_not(None)))) if paper_id_str else set()
        job.result = {**summary,"file_ids":([f["file_id"] for f in ordinary_summary["files"] if all(s["status"] == "completed" for s in f["stages"].values())] if ordinary else [str(fid) for fid in observed_files if str(fid) in (job.payload or {}).get("file_ids",[])])}
        job.progress = {**progress,"phase":"completed","needs_check":False}
        session.flush()
        # Original detail reads both stores directly. Never overwrite legacy figures on job polling.
    elif turn_status in {"failed","interrupted"} and (mode == "codex_web_ordinary" or status.goal_status in {"complete", "completed", "paused", "blocked"}):
        # Preserve a failed task for review, and block button-driven recreation.
        job.status = "failed"
        job.error = job.error if job.error and "fallback_exhausted" in job.error else f"Codex-web turn {turn_status}: {status.native_error or ''}"[:1600]
        job.progress = {**progress,"phase":turn_status,"needs_check":True,"automatic_retry":False,
                        "native_terminal_verified":True,"native_terminal":observed}
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
