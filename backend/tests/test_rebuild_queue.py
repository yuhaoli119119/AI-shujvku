import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Paper, WorkflowJob
from app.schemas.rebuild import RebuildQueueEnqueueRequest
from app.services.codex_web_dispatch import DispatchResult, ThreadStatus
from app.services.rebuild_acceptance import current_snapshot
from app.services.rebuild_queue import (CONTROL, TYPE, claim_next, control, enqueue, now,
                                         prepare_dispatch, set_control, snapshot)
from app.services.rebuild_queue_native import dispatch_once, reconcile_attempt
from app.services.rebuild_workflow_service import RebuildWorkflowError
from test_rebuild_mutation_audit import audit_env


def config(env, monkeypatch):
    monkeypatch.setenv("LITAI_REBUILD_QUEUE_ENABLED", "true")
    get_settings.cache_clear()
    return get_settings()


def enqueue_one(env, settings, *, paper_id=None, request="enqueue-1", stage="writer", freeze=None):
    paper_id = paper_id or env["paper"]
    with env["factory"]() as session:
        revision = current_snapshot(session, paper_id)[0]["revision"]
        payload = RebuildQueueEnqueueRequest(request_id=request, stage=stage, expected_revision=revision,
                                            freeze_id=freeze, token_allowance=100)
        result = enqueue(session, paper_id, payload, actor="writer", settings=settings)
        session.commit()
        return result


def resume(env, settings, limits=None):
    with env["factory"]() as session:
        set_control(session, paused=False, limits=limits, actor="operator", settings=settings); session.commit()


def claim(env, settings, stage="writer", worker="fixture-worker"):
    with env["factory"]() as session:
        result = claim_next(session, stage=stage, worker_id=worker, settings=settings)
        session.commit(); return result


def prepared(env, settings):
    pending = enqueue_one(env, settings)
    resume(env, settings)
    assert claim(env, settings)["claimed"]
    with env["factory"]() as session:
        result = prepare_dispatch(session, pending["job_id"], worker_id="fixture-worker",
                                  attempt_token=pending["attempt_token"], settings=settings)
        session.commit(); return result


def test_disabled_default_read_does_not_create_queue_and_resume_is_denied(audit_env):
    env = audit_env
    response = env["client"].get("/api/rebuild/queue")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["control"]["paused"] and not result["capabilities"]["enabled"]
    assert result["capabilities"]["automatic_event_execution"] is False
    assert result["metrics"]["measured_usage_tokens"] is None
    assert env["client"].put("/api/rebuild/queue/control", json={"paused": False}).status_code == 403
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0


def test_pause_persists_restart_and_request_replay_is_idempotent(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    pending = enqueue_one(env, settings)
    assert enqueue_one(env, settings)["replayed"]
    assert claim(env, settings)["reason"] == "paused"
    resume(env, settings)
    with env["factory"]() as session:
        set_control(session, paused=True, actor="operator", settings=settings); session.commit()
    with env["factory"]() as another_process:
        assert control(another_process)["paused"]
        assert another_process.get(WorkflowJob, pending["job_id"]).status == "pending"
    assert claim(env, settings)["reason"] == "paused"


def test_global_claim_concurrency_one_paper_and_default_total_slot(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    enqueue_one(env, settings); resume(env, settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: claim(env, settings, worker=worker), ["one", "two"]))
    assert sum(item["claimed"] for item in results) == 1
    assert next(item for item in results if not item["claimed"])["reason"] == "slot_limit"
    with env["factory"]() as session:
        assert len([job for job in session.scalars(select(WorkflowJob).where(WorkflowJob.type == TYPE)) if job.status == "claimed"]) == 1


def test_old_native_writer_blocks_queue_and_unknown_expired_lease_never_redispatches(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    pending = enqueue_one(env, settings); resume(env, settings)
    with env["factory"]() as session:
        old = WorkflowJob(job_id="old-fixture", type="ai_extract_rebuild", status="running", payload={"paper_id": str(env["paper"])})
        session.add(old); session.commit()
    assert not claim(env, settings)["claimed"]
    with env["factory"]() as session:
        session.get(WorkflowJob, "old-fixture").status = "completed"; session.commit()
    assert claim(env, settings)["claimed"]
    with env["factory"]() as session:
        job = session.get(WorkflowJob, pending["job_id"])
        job.status = "unknown"
        job.progress = {**job.progress, "dispatch_started": True, "lease_expires_at": (now() - timedelta(seconds=1)).isoformat()}
        session.commit()
    assert not claim(env, settings)["claimed"]
    with env["factory"]() as session:
        job = session.get(WorkflowJob, pending["job_id"])
        assert job.status == "needs_attention" and job.payload["attempt_token"] == pending["attempt_token"]
        assert job.progress["automatic_retry"] is False


def test_independent_slots_total_allowances_and_backpressure(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    enqueue_one(env, settings)
    resume(env, settings, {"writer_tokens": 50, "reviewer_tokens": 200, "total_tokens": 250})
    assert claim(env, settings)["reason"] == "token_allowance_limit"
    from test_rebuild_acceptance import freeze_body, base
    response = env["client"].post(base(env) + "/freeze", json=freeze_body(env))
    assert response.status_code == 200, response.text
    resume(env, settings, {"review_backlog": 1})
    assert claim(env, settings)["reason"] == "review_backpressure"


def test_different_papers_can_pipeline_with_explicit_limits(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    enqueue_one(env, settings)
    with env["factory"]() as session:
        second = Paper(title="Second synthetic paper", paper_code="T998", pdf_path="second.pdf")
        session.add(second); session.commit(); pid = second.id
    enqueue_one(env, settings, paper_id=pid, request="second")
    resume(env, settings, {"writer_slots": 2, "total_slots": 2})
    first, second = claim(env, settings, worker="one"), claim(env, settings, worker="two")
    assert first["claimed"] and second["claimed"] and first["job"]["paper_id"] != second["job"]["paper_id"]


def test_dispatch_unknown_result_persists_same_thread_and_never_recreates(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch); attempt = prepared(env, settings)
    count = 0
    async def lost_reply(**kwargs):
        nonlocal count; count += 1
        kwargs["on_event"]({"method": "thread/start", "phase": "response_received", "response": {"result": {"thread": {"id": "same-native-thread"}}}})
        raise TimeoutError("synthetic lost turn ACK")
    async def dispatch():
        return await dispatch_once(database_url=settings.database_url, job_id=attempt["job_id"], worker_id="fixture-worker",
                                   attempt_token=attempt["attempt_token"], settings=settings, native_dispatch=lost_reply)
    asyncio.run(dispatch())
    with pytest.raises(RebuildWorkflowError):
        asyncio.run(dispatch())
    assert count == 1
    with env["factory"]() as session:
        job = session.get(WorkflowJob, attempt["job_id"])
        assert job.status == "unknown" and job.progress["thread_id"] == "same-native-thread"
        assert job.payload["attempt_token"] == attempt["attempt_token"]


def test_pause_between_intent_and_native_request_prevents_execution(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch); attempt = prepared(env, settings)
    async def paused_dispatch(**kwargs):
        with env["factory"]() as session:
            set_control(session, paused=True, actor="operator", settings=settings); session.commit()
        kwargs["on_event"]({"method": "thread/start", "phase": "request_prepared", "params": {}})
        pytest.fail("native send must not be reached")
    asyncio.run(dispatch_once(database_url=settings.database_url, job_id=attempt["job_id"], worker_id="fixture-worker",
                             attempt_token=attempt["attempt_token"], settings=settings, native_dispatch=paused_dispatch))
    with env["factory"]() as session:
        job = session.get(WorkflowJob, attempt["job_id"])
        assert job.status == "unknown" and job.progress.get("thread_id") is None


def test_native_completed_without_new_evidence_cannot_release_as_success(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch); attempt = prepared(env, settings)
    async def dispatched(**kwargs):
        return DispatchResult(thread_id="thread", turn_id="turn", model="cn:deepseek-v4.1-flash", goal_status="active")
    asyncio.run(dispatch_once(database_url=settings.database_url, job_id=attempt["job_id"], worker_id="fixture-worker",
                             attempt_token=attempt["attempt_token"], settings=settings, native_dispatch=dispatched))
    async def completed(*args, **kwargs):
        return ThreadStatus("completed", "complete", None, None, turn_id="turn", thread_id="thread", item_count=0)
    with env["factory"]() as session:
        result = asyncio.run(reconcile_attempt(session, attempt["job_id"], attempt_token=attempt["attempt_token"], settings=settings, native_read=completed))
        assert not result["reconciled"] and result["reason"] == "no_new_business_evidence"
        session.commit()
        assert session.get(WorkflowJob, attempt["job_id"]).status == "needs_attention"


def test_writer_and_reviewer_for_same_paper_cannot_overlap_even_with_two_slots(audit_env, monkeypatch):
    from test_rebuild_acceptance import freeze_body, base
    env = audit_env; settings = config(env, monkeypatch)
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    enqueue_one(env, settings)
    enqueue_one(env, settings, stage="reviewer", request="reviewer", freeze=frozen["job_id"])
    resume(env, settings, {"total_slots": 2})
    assert claim(env, settings, stage="writer")["claimed"]
    assert not claim(env, settings, stage="reviewer")["claimed"]


def test_concurrent_claims_for_same_paper_have_one_winner_even_with_two_slots(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    first = enqueue_one(env, settings, request="first")
    second = enqueue_one(env, settings, request="second")
    resume(env, settings, {"writer_slots": 2, "total_slots": 2})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda worker: claim(env, settings, worker=worker), ["one", "two"]
        ))
    assert sum(item["claimed"] for item in results) == 1, results
    with env['factory']() as session:
        statuses = {
            first["job_id"]: session.get(WorkflowJob, first["job_id"]).status,
            second["job_id"]: session.get(WorkflowJob, second["job_id"]).status,
        }
        assert sorted(statuses.values()) == ["claimed", "pending"]


@pytest.mark.parametrize("status,progress", [
    ("unknown", {}),
    ("needs_attention", {}),
    ("failed", {}),
    ("failed", {"native_terminal_verified": False}),
])
def test_unresolved_legacy_state_blocks_queue_claim(audit_env, monkeypatch, status, progress):
    env = audit_env; settings = config(env, monkeypatch)
    enqueue_one(env, settings)
    resume(env, settings)
    with env['factory']() as session:
        session.add(WorkflowJob(
            job_id="legacy-blocker", type="ai_extract_rebuild", status=status,
            payload={"paper_id": str(env['paper']), "actor": "writer"}, progress=progress,
        ))
        session.commit()
    assert not claim(env, settings)["claimed"]


def test_verified_legacy_failure_releases_queue_claim(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch)
    enqueue_one(env, settings)
    resume(env, settings)
    with env['factory']() as session:
        session.add(WorkflowJob(
            job_id="legacy-verified-failure", type="ai_extract_rebuild", status="failed",
            payload={"paper_id": str(env['paper']), "actor": "writer"},
            progress={"native_terminal_verified": True},
        ))
        session.commit()
    assert claim(env, settings)["claimed"]


def test_identity_matched_native_readback_renews_lease_and_records_reported_usage(audit_env, monkeypatch):
    env = audit_env; settings = config(env, monkeypatch); attempt = prepared(env, settings)
    async def dispatched(**kwargs):
        return DispatchResult(thread_id="thread", turn_id="turn", model="cn:deepseek-v4.1-flash", goal_status="active")
    asyncio.run(dispatch_once(database_url=settings.database_url, job_id=attempt["job_id"], worker_id="fixture-worker",
                             attempt_token=attempt["attempt_token"], settings=settings, native_dispatch=dispatched))
    async def active(*args, **kwargs):
        return ThreadStatus("inProgress", "active", "working", None, turn_id="turn", thread_id="thread", item_count=2, tokens_used=50)
    with env["factory"]() as session:
        job = session.get(WorkflowJob, attempt["job_id"])
        job.progress = {**job.progress, "lease_expires_at": (now() - timedelta(seconds=1)).isoformat()}; session.commit()
        result = asyncio.run(reconcile_attempt(session, attempt["job_id"], attempt_token=attempt["attempt_token"], settings=settings, native_read=active))
        assert not result["reconciled"]
        session.commit()
        job = session.get(WorkflowJob, attempt["job_id"])
        assert job.status == "running" and job.progress["native_usage"]["tokens_used"] == 50
        assert snapshot(session, settings)["metrics"]["measured_usage_tokens"] == 50


def test_existing_extract_empty_completed_stops_native_readback_after_three_observations(audit_env, monkeypatch):
    from datetime import datetime
    from app.services import ai_extract_service, rebuild_progress_guard
    env = audit_env
    clock = [datetime(2026, 10, 17)]
    class ClockDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return clock[0]
    monkeypatch.setattr(rebuild_progress_guard, "datetime", ClockDatetime)
    monkeypatch.setattr(ai_extract_service, "_utcnow", lambda: clock[0])
    calls = []
    async def empty(*args, **kwargs):
        calls.append(args)
        return ThreadStatus("completed", "active", None, None, turn_id="turn", thread_id="thread", item_count=0)
    monkeypatch.setattr("app.services.codex_web_dispatch.read_thread_status", empty)
    with env["factory"]() as session:
        signature = rebuild_progress_guard.evidence_signature(session, env["paper"])
        job = WorkflowJob(job_id="extract-fixture", type="ai_extract_rebuild", status="running", payload={"paper_id": str(env["paper"])},
                          progress={"thread_id": "thread", "turn_id": "turn", "dispatch_mode": "codex_web_target",
                                    "evidence_guard": rebuild_progress_guard.initial_guard(signature, now=clock[0])})
        session.add(job); session.commit()
        for i in range(4):
            clock[0] = datetime(2026, 10, 17) + timedelta(minutes=i)
            asyncio.run(ai_extract_service.sync_job_status(session, job, env["settings"])); session.commit()
        assert len(calls) == 3 and job.status == "running"
        assert job.progress["needs_attention"] and job.progress["automatic_retry"] is False


def test_external_writer_cannot_start_while_reviewer_owns_paper(audit_env, monkeypatch):
    from test_rebuild_acceptance import freeze_body, base
    from app.services.paper_processing_status import report_processing
    from app.schemas.rebuild import ProcessingStatusReport
    env = audit_env; settings = config(env, monkeypatch)
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    enqueue_one(env, settings, stage="reviewer", freeze=frozen["job_id"])
    resume(env, settings)
    assert claim(env, settings, stage="reviewer")["claimed"]
    with env["factory"]() as session:
        with pytest.raises(ValueError, match="other_unresolved"):
            report_processing(session, session.get(Paper, env["paper"]), ProcessingStatusReport(run_id="unrelated-writer", status="parsing"))
        session.rollback()
        assert session.scalar(select(func.count()).select_from(WorkflowJob).where(WorkflowJob.type == "rebuild_external_extract")) == 0


def test_changed_inputs_between_claim_and_intent_require_attention(audit_env, monkeypatch):
    from app.db.models import PaperSection
    env = audit_env; settings = config(env, monkeypatch)
    pending = enqueue_one(env, settings); resume(env, settings)
    assert claim(env, settings)["claimed"]
    with env["factory"]() as session:
        session.add(PaperSection(paper_id=env["paper"], section_title="Synthetic new output", text="Change after frozen claim"))
        session.commit()
        result = prepare_dispatch(session, pending["job_id"], worker_id="fixture-worker", attempt_token=pending["attempt_token"], settings=settings)
        session.commit()
        assert result["status"] == "needs_attention" and result["progress"]["dispatch_started"] is False
