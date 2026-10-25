"""Explicit adapters to the existing native IPC bridge, with durable attempts.

The upstream bridge has not exposed a verified event subscription contract.
These adapters deliberately require an explicit coordinator/heartbeat invocation.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.db.models import WorkflowJob
from app.db.session import session_scope
from app.services.codex_web_dispatch import dispatch_agent, read_thread_status, matches_owned_attempt
from app.services.rebuild_acceptance import acceptance_status, current_snapshot
from app.services.rebuild_mutation_audit import safe_json
from app.services.rebuild_progress_guard import evidence_signature, observe
from app.services.rebuild_queue import TYPE, control, lock_scheduler, now
from app.services.rebuild_workflow_service import RebuildWorkflowError


async def dispatch_once(*, database_url, job_id, worker_id, attempt_token, settings, native_dispatch=None):
    if not settings.rebuild_queue_enabled:
        raise RebuildWorkflowError("rebuild_queue_disabled")
    with session_scope(database_url) as session:
        lock_scheduler(session)
        job = session.get(WorkflowJob, job_id)
        if control(session)["paused"]:
            raise RebuildWorkflowError("queue_paused")
        if job is None or job.type != TYPE or job.status != "dispatch_intent" or job.payload["attempt_token"] != attempt_token or job.progress.get("lease_owner") != worker_id:
            raise RebuildWorkflowError("queue_dispatch_identity_mismatch")
        if job.progress.get("dispatch_started"):
            raise RebuildWorkflowError("queue_dispatch_outcome_unknown_reconcile_same_attempt")
        from datetime import datetime
        if datetime.fromisoformat(job.progress["lease_expires_at"]) <= now():
            raise RebuildWorkflowError("queue_claim_expired")
        intent = dict(job.payload)
        current, _ = current_snapshot(session, UUID(intent["paper_id"]))
        if current["revision"] != intent["expected_revision"]:
            job.status = "needs_attention"
            job.progress = {**job.progress, "reason": "inputs_changed_before_native_dispatch", "automatic_retry": False}
            return {"dispatched": False, "reason": "inputs_changed_before_native_dispatch"}
        if intent["stage"] == "writer":
            from app.services.ai_extract_service import _build_prompt
            prompt, objective = _build_prompt(session, UUID(intent["paper_id"]), settings)
        else:
            prompt = (f"独立验收 paper_id={intent['paper_id']}，reviewer_task_id={job_id}，冻结回执={intent['freeze_id']}，冻结 revision={intent['expected_revision']}。"
                      f"工作包 /api/rebuild/papers/{intent['paper_id']}/ai-work-package。读取真实 PDF、SI 和数据库，核对冻结覆盖、来源和精度。"
                      "只提交独立验收回执，不修改科学值、来源、图表或章节，不重解析。未知诚实 review_incomplete，原文冲突 partial，不能猜数。"
                      "同账号可用独立协调者预先签发并绑定原生任务的授权，不能仅自填不同task_id。按 review-progress 接口提交物理页原句和报告manifest作为只读进展；未配置授权时诚实报门禁未集成，勿要求另建账号。")
            objective = prompt
        prompt = (f"持久任务 {job_id}，attempt_token={attempt_token}，仅处理 paper_id={intent['paper_id']}。"
                  f"如回报 processing-status，run_id 固定为 {job_id}；独立 reviewer 不写解析状态。"
                  "不派子任务、不换模型；连续三次没有新证据、覆盖或回执，保存诊断后 needs_attention 并停止，不按 turn completed 自称通过。\n" + prompt)
        if len(prompt) > 4000:
            job.status = "needs_attention"
            job.progress = {**job.progress, "reason": "queue_context_chunk_required_before_dispatch", "automatic_retry": False}
            return {"dispatched": False, "reason": "queue_context_chunk_required_before_dispatch"}
        job.progress = {**job.progress, "dispatch_started": True, "dispatch_started_at": now().isoformat()}
    # Durable started marker precedes even a connection. A crash/lost ACK never
    # authorizes dispatching another thread after restart.
    def persist_event(event):
        with session_scope(database_url) as session:
            lock_scheduler(session)
            job = session.get(WorkflowJob, job_id)
            if job is None or job.payload["attempt_token"] != attempt_token:
                raise RebuildWorkflowError("queue_attempt_token_changed")
            if event.get("phase") == "request_prepared" and control(session)["paused"]:
                raise RebuildWorkflowError("queue_paused_before_native_request")
            progress = dict(job.progress)
            progress["events"] = [*progress.get("events", []), safe_json({**event, "recorded_at": now().isoformat()})]
            response = event.get("response") or {}
            result = response.get("result") if isinstance(response, dict) else None
            if isinstance(result, dict):
                thread = result.get("thread")
                turn = result.get("turn")
                if event.get("method") == "thread/start" and isinstance(thread, dict) and thread.get("id"):
                    progress["thread_id"] = thread["id"]
                if event.get("method") == "turn/start" and isinstance(turn, dict) and turn.get("id"):
                    progress["turn_id"] = turn["id"]
            job.progress = progress
    try:
        result = await (native_dispatch or dispatch_agent)(prompt=prompt, objective=objective, settings=settings,
                                                          model=intent["model"], token_budget=intent["token_allowance"], on_event=persist_event)
        with session_scope(database_url) as session:
            lock_scheduler(session)
            job = session.get(WorkflowJob, job_id)
            if job.payload["attempt_token"] != attempt_token:
                raise RebuildWorkflowError("queue_attempt_token_changed")
            job.status = "running"
            job.progress = {**job.progress, "thread_id": result.thread_id, "turn_id": result.turn_id,
                            "dispatch_mode": result.dispatch_mode, "actual_inference_verified": False}
    except Exception as exc:
        with session_scope(database_url) as session:
            lock_scheduler(session)
            job = session.get(WorkflowJob, job_id)
            job.status = "unknown"
            job.error = f"native_dispatch_requires_readback:{type(exc).__name__}"
            job.progress = {**job.progress, "automatic_retry": False, "needs_check": True}
        # Preserve identity/diagnostics; no fallback, recreation or auto resume.


async def reconcile_attempt(session, job_id, *, attempt_token, settings, native_read=None, manual_recheck=False):
    from app.services.paper_db_phase import job_pin, run_phase

    def snapshot(worker):
        job = worker.get(WorkflowJob, job_id)
        if job is None or job.type != TYPE or job.payload["attempt_token"] != attempt_token:
            raise RebuildWorkflowError("queue_attempt_identity_mismatch")
        if (job.progress.get("evidence_guard") or {}).get("needs_attention") and not manual_recheck:
            return {"early": {"reconciled": False, "reason": "needs_attention_requires_explicit_manual_readback"}}
        thread_id = job.progress.get("thread_id")
        if not thread_id:
            return {"early": {"reconciled": False, "reason": "unknown_thread_identity_no_redispatch"}}
        if job.status not in {"running", "unknown", "needs_attention", "dispatch_intent"}:
            return {"early": {"reconciled": False, "reason": "attempt_is_not_awaiting_native_readback"}}
        return {"early": None, "pin": job_pin(job), "thread_id": thread_id,
                "model": job.payload["model"]}

    pinned = await run_phase(session, snapshot)
    if pinned["early"] is not None:
        return pinned["early"]
    thread_id = pinned["thread_id"]
    # The snapshot transaction is closed before this IPC read.
    observed = await (native_read or read_thread_status)(
        thread_id, settings=settings, include_goal=pinned["model"] != "gpt-6-luna")

    def apply(worker):
        job = worker.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
        if job is None or job_pin(job) != pinned["pin"]:
            raise RebuildWorkflowError("queue_attempt_identity_changed_during_readback")
        native = {key: getattr(observed, key, None) for key in ("thread_id", "turn_id", "turn_status", "goal_status", "item_count")}
        guard = observe(job.progress.get("evidence_guard"), evidence_signature(
            worker, UUID(job.payload["paper_id"]), role=job.payload["stage"], task_id=job.job_id), native)
        job.progress = {**job.progress, "readback": {**native, "at": now().isoformat()}, "evidence_guard": guard}
        if guard["needs_attention"]:
            job.status = "needs_attention"
        if not matches_owned_attempt(observed, thread_id, job.progress.get("turn_id"), allow_goal_continuations=True):
            job.status = "needs_attention" if guard["needs_attention"] else "unknown"
            return {"reconciled": False, "reason": "native_identity_or_outcome_unconfirmed"}
        if type(observed.tokens_used) is int and observed.tokens_used >= 0:
            previous_usage = job.progress.get("native_usage", {}).get("tokens_used")
            if previous_usage is None or observed.tokens_used >= previous_usage:
                job.progress = {**job.progress, "native_usage": {"tokens_used": observed.tokens_used,
                                "source": "thread/goal/get", "thread_id": thread_id, "observed_at": now().isoformat()}}
            if observed.tokens_used > job.payload["token_allowance"]:
                job.status = "needs_attention"
                job.progress = {**job.progress, "reason": "reported_native_usage_exceeded_reserved_allowance", "automatic_retry": False}
                return {"reconciled": False, "reason": "native_token_allowance_exceeded"}
        terminal = observed.turn_status in {"completed", "failed", "interrupted"} and (
            job.payload["model"] == "gpt-6-luna" or observed.goal_status in {"complete", "completed", "paused", "blocked", "usageLimited", "budgetLimited"})
        if not terminal:
            if observed.turn_status in {"inProgress", "inprogress", "queued"} and not guard["needs_attention"]:
                from datetime import timedelta
                job.status = "running"
                job.progress = {**job.progress, "lease_expires_at": (now() + timedelta(seconds=900)).isoformat(),
                                "lease_renewal_source": "identity_matched_native_readback"}
            return {"reconciled": False, "reason": "same_native_attempt_not_confirmed_terminal"}
        paper_id = UUID(job.payload["paper_id"])
        acceptance = acceptance_status(worker, paper_id)
        if observed.turn_status in {"failed", "interrupted"}:
            job.status = "failed"
        elif job.payload["stage"] == "reviewer":
            if (acceptance.get("job_id") != job.payload["freeze_id"] or acceptance.get("status") not in {"accepted", "needsfix", "partial", "review_incomplete"}
                    or not acceptance.get("review") or acceptance["review"].get("reviewer_task_id") != job_id):
                job.status = "needs_attention"
                return {"reconciled": False, "reason": "explicit_independent_review_receipt_missing"}
            job.status = acceptance["status"]
        elif not guard.get("business_advanced"):
            job.status = "needs_attention"
            return {"reconciled": False, "reason": "no_new_business_evidence"}
        else:
            job.status = "extracted"
        job.progress = {**job.progress, "lease_owner": None, "lease_expires_at": None,
                        "native_terminal_verified": True, "automatic_retry": False}
        job.result = {"execution_ended": True, "scientific_acceptance": acceptance["status"],
                      "evidence_signature": guard["latest"], "terminal_turn_id": observed.turn_id}
        return {"reconciled": True, "status": job.status}

    return await run_phase(session, apply)
