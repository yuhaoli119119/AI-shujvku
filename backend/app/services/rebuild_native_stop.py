"""Durable, idempotent protective stop of an already-owned native attempt.

Never creates/resumes a thread. Before each goal pause/turn interrupt the native
readback must prove the original owned thread/turn relationship. Lost replies
are reconciled by readback, never by blindly issuing the mutation again.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import websockets

from app.db.models import WorkflowJob
from app.db.session import session_scope
from app.services.codex_web_dispatch import _call, read_thread_status, matches_owned_attempt
from app.services.rebuild_workflow_service import RebuildWorkflowError


async def _native_command(thread_id, turn_id, *, goal_mode, settings, on_event, native_read=None):
    read = native_read or read_thread_status
    observed = await read(thread_id, settings=settings, include_goal=goal_mode)
    if not matches_owned_attempt(observed, thread_id, turn_id, allow_goal_continuations=goal_mode):
        raise RebuildWorkflowError("native_stop_owned_handle_not_verified")
    timeout = min(settings.codex_web_dispatch_timeout_seconds, 15)
    async with websockets.connect(settings.codex_web_ipc_uri, open_timeout=timeout) as ws:
        if goal_mode and observed.goal_status not in {"paused", "complete", "completed"}:
            response = await _call(ws, "thread/goal/set", {"threadId": thread_id, "status": "paused"}, timeout, on_event)
            goal = response.get("goal")
            if not isinstance(goal, dict) or goal.get("threadId") != thread_id or goal.get("status") != "paused":
                raise RebuildWorkflowError("native_goal_pause_unconfirmed")
        # A target goal can race with an automatic new turn. Re-read the owned
        # thread after pausing and interrupt the actual active turn, not a guess.
        current = await read(thread_id, settings=settings, include_goal=goal_mode)
        if not matches_owned_attempt(current, thread_id, turn_id, allow_goal_continuations=goal_mode):
            raise RebuildWorkflowError("native_stop_owned_handle_not_verified")
        if current.turn_status in {"inProgress", "inprogress", "queued"}:
            await _call(ws, "turn/interrupt", {"threadId": thread_id, "turnId": current.turn_id}, timeout, on_event)


async def protective_stop(database_url, job_id, *, settings, native_read=None, native_command=None):
    if not settings.rebuild_native_stop_enabled:
        return {"verified_stop": False, "status": "disabled_detection_only"}
    read = native_read or read_thread_status
    with session_scope(database_url) as session:
        job = session.get(WorkflowJob, job_id, with_for_update=True)
        if job is None or job.type not in {"ai_extract_rebuild", "rebuild_pipeline_attempt"}:
            raise RebuildWorkflowError("native_stop_attempt_not_found")
        progress = dict(job.progress or {})
        thread_id, turn_id = progress.get("thread_id"), progress.get("turn_id")
        if not thread_id or not turn_id:
            raise RebuildWorkflowError("native_stop_missing_owned_handle")
        existing = progress.get("native_stop") or {}
        if existing.get("verified_stop"):
            return existing
        # Reserve once. A concurrent/restarted caller may only reconcile.
        may_send = not existing or existing.get("phase") == "identity_unverified_no_mutation"
        stop = existing or {"request_id": str(uuid4()), "thread_id": thread_id, "start_turn_id": turn_id,
                            "phase": "intent", "events": [], "verified_stop": False,
                            "requested_at": datetime.utcnow().isoformat(), "automatic_retry": False}
        if may_send:
            stop = {**stop, "phase": "intent", "execution_claim_id": str(uuid4())}
        job.progress = {**progress, "native_stop": stop}
        goal_mode = progress.get("dispatch_mode") != "codex_web_ordinary" and (job.payload or {}).get("model") != "gpt-6-luna"
    # First reconcile an uncertain prior stop without any mutation replay.
    observed = await read(thread_id, settings=settings, include_goal=goal_mode)
    owned = matches_owned_attempt(observed, thread_id, turn_id, allow_goal_continuations=goal_mode)
    ended = owned and observed.turn_status in {"completed", "failed", "interrupted"}
    halted = ended and (not goal_mode or observed.goal_status in {"paused", "complete", "completed"})
    def journal(event):
        with session_scope(database_url) as session:
            job = session.get(WorkflowJob, job_id, with_for_update=True)
            if job.progress.get("thread_id") != thread_id or job.progress.get("turn_id") != turn_id:
                raise RebuildWorkflowError("native_stop_attempt_changed")
            old = dict(job.progress["native_stop"])
            # Store only protocol identity/phase, not arbitrary prompt or secrets.
            old["events"] = [*old.get("events", []), {"method": event.get("method"), "phase": event.get("phase"),
                            "rpc_id": event.get("rpc_id"), "at": datetime.utcnow().isoformat()}]
            old["phase"] = "request_sent_or_unknown" if event.get("phase") == "request_prepared" else "ack_received"
            job.progress = {**job.progress, "native_stop": old}
    error = None
    if not halted and may_send and owned:
        try:
            command = native_command or _native_command
            await command(thread_id, turn_id, goal_mode=goal_mode, settings=settings, on_event=journal, native_read=read)
        except Exception as exc:
            error = type(exc).__name__
        observed = await read(thread_id, settings=settings, include_goal=goal_mode)
        owned = matches_owned_attempt(observed, thread_id, turn_id, allow_goal_continuations=goal_mode)
        halted = bool(owned and observed.turn_status in {"completed", "failed", "interrupted"} and
                      (not goal_mode or observed.goal_status in {"paused", "complete", "completed"}))
    with session_scope(database_url) as session:
        job = session.get(WorkflowJob, job_id, with_for_update=True)
        if job.progress.get("thread_id") != thread_id or job.progress.get("turn_id") != turn_id:
            raise RebuildWorkflowError("native_stop_attempt_changed")
        if job.progress["native_stop"].get("verified_stop"):
            return job.progress["native_stop"]
        stop = {**job.progress["native_stop"], "verified_stop": bool(halted),
                "phase": "verified_stopped" if halted else "identity_unverified_no_mutation" if not owned and not job.progress["native_stop"].get("events") else "needs_manual_reconciliation",
                "last_checked_at": datetime.utcnow().isoformat(), "error_type": error,
                "observed_turn_status": getattr(observed, "turn_status", None),
                "observed_goal_status": getattr(observed, "goal_status", None)}
        job.progress = {**job.progress, "native_stop": stop}
        return stop
