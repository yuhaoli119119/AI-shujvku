"""Durable, paused-by-default scheduling in the established workflow_jobs table.

No worker loop, cron, startup dispatch or synthetic native events. An explicit
coordinator/heartbeat claims and reconciles the same attempt through adapters.
Unknown native outcomes retain their paper occupancy even after lease expiry.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.db.models import AuditLog, Paper, WorkflowJob
from app.services.rebuild_acceptance import _digest, current_snapshot, acceptance_status
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.services.rebuild_progress_guard import evidence_signature, initial_guard

TYPE = "rebuild_pipeline_attempt"
CONTROL = "rebuild-pipeline-control-v1"
HOLDING = {"claimed", "dispatch_intent", "running", "unknown", "needs_attention"}
TERMINAL = {"extracted", "accepted", "partial", "needsfix", "review_incomplete", "failed"}
DEFAULT_LIMITS = {"writer_slots": 1, "reviewer_slots": 1, "total_slots": 1, "review_backlog": 2,
                  "writer_tokens": None, "reviewer_tokens": None, "total_tokens": None}


def now():
    return datetime.utcnow()


def lock_scheduler(session):
    # Transaction scoped global scheduler serialization; no extra DB schema.
    session.execute(text("SELECT pg_advisory_xact_lock(72819300420261017)"))


def control(session):
    job = session.get(WorkflowJob, CONTROL)
    return {"paused": True, "limits": dict(DEFAULT_LIMITS), "updated_at": None} if job is None else {
        "paused": bool(job.payload.get("paused", True)), "limits": {**DEFAULT_LIMITS, **job.payload.get("limits", {})},
        "updated_at": job.updated_at.isoformat() if job.updated_at else None}


def capabilities(settings):
    return {"enabled": settings.rebuild_queue_enabled, "automatic_event_execution": False,
            "native_bridge": "existing Codex IPC /__backend/ipc on 8214; configured container URI retained",
            "native_event_forwarding": "unverified_external_dependency",
            "protective_native_stop_enabled": settings.rebuild_native_stop_enabled,
            "stop_semantics": "detection only unless enabled; pause/interrupt requires verified owned handles and durable acknowledgements",
            "fallback": "explicit native heartbeat calls claim/readback adapters; no shell polling or cron",
            "batch_adapter": "workflow_jobs payload.batch_id; baseline literature_batch.cjs absent; no legacy batch revived"}


def set_control(session, *, paused, limits=None, actor, settings):
    if not actor or not actor.strip():
        raise RebuildWorkflowError("queue_actor_required")
    lock_scheduler(session)
    if not paused and not settings.rebuild_queue_enabled:
        raise RebuildWorkflowError("rebuild_queue_disabled")
    before = control(session)
    merged = {**before["limits"], **(limits or {})}
    if set(merged) != set(DEFAULT_LIMITS):
        raise RebuildWorkflowError("queue_unknown_limit")
    for key in ("writer_slots", "reviewer_slots", "total_slots", "review_backlog"):
        if type(merged[key]) is not int or not 1 <= merged[key] <= (4 if key != "review_backlog" else 100):
            raise RebuildWorkflowError("queue_invalid_limit")
    for key in ("writer_tokens", "reviewer_tokens", "total_tokens"):
        if merged[key] is not None and (type(merged[key]) is not int or merged[key] < 1):
            raise RebuildWorkflowError("queue_invalid_token_limit")
    job = session.get(WorkflowJob, CONTROL)
    if job is None:
        job = WorkflowJob(job_id=CONTROL, type="rebuild_pipeline_control", status="paused")
        session.add(job)
    job.status = "paused" if paused else "ready"
    job.payload = {"paused": paused, "limits": merged, "actor": actor}
    session.add(AuditLog(action="rebuild_queue_control", source="rebuild_queue_v1", target_type="workflow_jobs", target_id=CONTROL,
                        payload={"actor": actor, "before": before, "after": job.payload}))
    session.flush()
    return control(session)


def _attempts(session):
    return list(session.scalars(select(WorkflowJob).where(WorkflowJob.type == TYPE)
                                .order_by(WorkflowJob.created_at, WorkflowJob.job_id)))


def paper_busy(session, paper_id, *, excluding=None):
    # Jobs are history and progress, not an admission gate for reading or writing.
    return None


def enqueue(session, paper_id, payload, *, actor, settings):
    if not actor or not actor.strip():
        raise RebuildWorkflowError("queue_actor_required")
    if not settings.rebuild_queue_enabled:
        raise RebuildWorkflowError("rebuild_queue_disabled")
    lock_scheduler(session)
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise RebuildWorkflowError("paper_not_found")
    request_hash = _digest(payload.model_dump(mode="json"))
    job_id = "pipeline-" + _digest([str(paper_id), payload.request_id])[:48]
    existing = session.get(WorkflowJob, job_id)
    if existing:
        if (existing.payload["request_sha256"], existing.payload["actor"]) != (request_hash, actor):
            raise RebuildWorkflowError("queue_request_id_reused")
        return {**serialize(existing), "replayed": True}
    snapshot, _ = current_snapshot(session, paper_id)
    if payload.stage == "reviewer":
        frozen = session.get(WorkflowJob, payload.freeze_id)
        if frozen is None or frozen.type != "rebuild_acceptance" or frozen.payload["paper_id"] != str(paper_id) or frozen.payload["snapshot"]["revision"] != snapshot["revision"]:
            raise RebuildWorkflowError("queue_review_freeze_required")
        if frozen.status == "accepted":
            raise RebuildWorkflowError("queue_already_accepted")
    job = WorkflowJob(job_id=job_id, type=TYPE, status="pending", library_name=paper.library_name,
                      payload={**payload.model_dump(mode="json"), "paper_id": str(paper_id), "request_sha256": request_hash,
                               "actor": actor, "attempt_token": str(uuid4())}, progress={"events": [], "dispatch_started": False})
    session.add(job); session.flush()
    return serialize(job)


def claim_next(session, *, stage, worker_id, settings, lease_seconds=900):
    if stage not in {"writer", "reviewer"} or not worker_id or not worker_id.strip() or not 1 <= lease_seconds <= 3600:
        raise RebuildWorkflowError("queue_invalid_claim")
    if not settings.rebuild_queue_enabled:
        raise RebuildWorkflowError("rebuild_queue_disabled")
    lock_scheduler(session)
    state = control(session)
    if state["paused"]:
        return {"claimed": False, "reason": "paused"}
    attempts = _attempts(session)
    active = [job for job in attempts if job.status in HOLDING]
    for job in active:
        lease = job.progress.get("lease_expires_at")
        if lease and datetime.fromisoformat(lease) <= now():
            if job.status == "claimed" and not job.progress.get("dispatch_started"):
                job.status = "pending"
                job.progress = {**job.progress, "lease_owner": None, "lease_expires_at": None}
            else:
                job.status = "needs_attention"
                job.progress = {**job.progress, "reason": "lease_expired_reconcile_same_native_attempt", "automatic_retry": False}
    for job in attempts:
        if job.status != "pending" or job.payload["stage"] != stage:
            continue
        session.scalar(select(Paper.id).where(Paper.id == UUID(job.payload["paper_id"])).with_for_update())
        snapshot, _ = current_snapshot(session, UUID(job.payload["paper_id"]))
        budget = job.payload["token_allowance"]
        job.status = "claimed"
        job.progress = {**job.progress, "lease_owner": worker_id, "lease_expires_at": (now() + timedelta(seconds=lease_seconds)).isoformat(),
                        "reserved_tokens": budget, "evidence_guard": initial_guard(evidence_signature(session, UUID(job.payload["paper_id"]), role=stage, task_id=job.job_id), role=stage)}
        session.flush()
        return {"claimed": True, "job": serialize(job)}
    session.flush()
    return {"claimed": False, "reason": "no_eligible_pending_attempt"}


def prepare_dispatch(session, job_id, *, worker_id, attempt_token, settings):
    if not settings.rebuild_queue_enabled:
        raise RebuildWorkflowError("rebuild_queue_disabled")
    lock_scheduler(session)
    if control(session)["paused"]:
        raise RebuildWorkflowError("queue_paused")
    job = session.get(WorkflowJob, job_id)
    if job is None or job.type != TYPE or job.status != "claimed" or job.payload["attempt_token"] != attempt_token or job.progress.get("lease_owner") != worker_id:
        raise RebuildWorkflowError("queue_claim_identity_mismatch")
    if datetime.fromisoformat(job.progress["lease_expires_at"]) <= now():
        raise RebuildWorkflowError("queue_claim_expired")
    current, _ = current_snapshot(session, UUID(job.payload["paper_id"]))
    if current["revision"] != job.payload["expected_revision"]:
        job.status = "needs_attention"
        job.progress = {**job.progress, "reason": "inputs_changed_before_dispatch_intent", "automatic_retry": False}
        session.flush()
        return serialize(job)
    job.status = "dispatch_intent"
    job.progress = {**job.progress, "dispatch_intent_at": now().isoformat(), "dispatch_started": False}
    session.flush()
    return serialize(job)  # caller commits this before invoking native adapter


def serialize(job):
    return {"job_id": job.job_id, "status": job.status, "paper_id": job.payload["paper_id"], "stage": job.payload["stage"],
            "batch_id": job.payload.get("batch_id"), "attempt_token": job.payload["attempt_token"],
            "expected_revision": job.payload["expected_revision"], "progress": job.progress, "error": job.error,
            "result": job.result}


def snapshot(session, settings):
    attempts = _attempts(session)
    from app.services.rebuild_acceptance import JOB_TYPE as ACCEPTANCE_TYPE
    reviews = list(session.scalars(select(WorkflowJob).where(WorkflowJob.type == ACCEPTANCE_TYPE)))
    latest_reviews = {}
    for job in sorted(reviews, key=lambda item: (item.created_at, item.job_id)):
        latest_reviews[job.payload["paper_id"]] = job
    projections = {pid: acceptance_status(session, UUID(pid)) for pid in latest_reviews}
    accepted = [job for pid, job in latest_reviews.items() if projections[pid]["status"] == "accepted"]
    oldest = min((job.created_at for job in attempts), default=None)
    hours = (now() - oldest).total_seconds() / 3600 if oldest else None
    unique_papers = len({job.payload["paper_id"] for job in accepted})
    events = list(session.scalars(select(AuditLog).where(AuditLog.action == "independent_review",
                                                       AuditLog.source == "rebuild_acceptance_v1")))
    accepted_job_ids = {job.job_id for job in accepted}
    accepted_in_window = {event.paper_id for event in events if event.payload.get("after") == "accepted"
                          and event.target_id in accepted_job_ids and oldest and event.created_at >= oldest}
    native_usages = [job.progress.get("native_usage", {}).get("tokens_used") for job in attempts]
    known_usage = [value for value in native_usages if type(value) is int]
    return {"control": control(session), "capabilities": capabilities(settings), "items": [serialize(job) for job in attempts],
            "metrics": {"accepted_papers": unique_papers, "accepted_papers_per_hour": len(accepted_in_window) / hours if hours and hours > 0 else None,
                        "measurement_window_hours": hours, "review_backlog": sum(item["status"] != "accepted" for item in projections.values()),
                        "rework_receipts": sum(event.payload.get("after") == "needsfix" for event in events),
                        "reserved_tokens": sum(job.progress.get("reserved_tokens", 0) for job in attempts),
                        "measured_usage_tokens": sum(known_usage) if attempts and len(known_usage) == len(attempts) else None,
                        "known_usage_tokens": sum(known_usage) if known_usage else None,
                        "usage_receipts": len(known_usage), "usage_attempts": len(attempts),
                        "usage_source": "identity-matched thread/goal/get tokensUsed when exposed; unavailable otherwise",
                        "allowance_semantics": "writer/reviewer/total dispatch reservations; target goal tokenBudget requires matched set/get acknowledgements; runtime budget enforcement needs independent native acceptance",
                        "speedup": None}}
