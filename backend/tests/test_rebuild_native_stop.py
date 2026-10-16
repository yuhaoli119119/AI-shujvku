import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.config import get_settings
from app.db.models import WorkflowJob
from app.services.codex_web_dispatch import ThreadStatus
from app.services.rebuild_native_stop import protective_stop, _native_command
from app.services.rebuild_workflow_service import RebuildWorkflowError
from test_rebuild_mutation_audit import audit_env


def attempt(env, monkeypatch, *, enabled=True):
    monkeypatch.setenv("LITAI_REBUILD_NATIVE_STOP_ENABLED", str(enabled).lower())
    get_settings.cache_clear(); settings = get_settings()
    with env["factory"]() as session:
        session.add(WorkflowJob(job_id="owned-attempt", type="ai_extract_rebuild", status="running",
                                payload={"paper_id": str(env["paper"]), "attempt_token": "fixture-attempt"},
                                progress={"thread_id": "owned-thread", "turn_id": "first-turn", "dispatch_mode": "codex_web_target"}))
        session.commit()
    return settings


def status(turn="first-turn", *, goal="active", state="inProgress", thread="owned-thread"):
    return ThreadStatus(state, goal, None, None, turn_id=turn, thread_id=thread, turn_ids=("first-turn", turn))


def test_protective_stop_pauses_goal_interrupts_owned_turn_and_is_idempotent(audit_env, monkeypatch):
    env = audit_env; settings = attempt(env, monkeypatch); halted = False; calls = []
    async def read(*args, **kwargs):
        return status(goal="paused", state="interrupted") if halted else status()
    async def command(thread_id, turn_id, **kwargs):
        nonlocal halted
        calls.append((thread_id, turn_id))
        kwargs["on_event"]({"method": "thread/goal/set", "phase": "request_prepared", "rpc_id": "r1"})
        kwargs["on_event"]({"method": "thread/goal/set", "phase": "response_received"})
        kwargs["on_event"]({"method": "turn/interrupt", "phase": "request_prepared", "rpc_id": "r2"})
        halted = True
    for _ in range(2):
        result = asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=read, native_command=command))
        assert result["verified_stop"] and result["phase"] == "verified_stopped"
    assert calls == [("owned-thread", "first-turn")]
    with env["factory"]() as session:
        result = session.get(WorkflowJob, "owned-attempt").progress["native_stop"]
        assert len(result["events"]) == 3 and not result["automatic_retry"]


def test_lost_pause_ack_requires_readback_and_does_not_replay_mutation(audit_env, monkeypatch):
    env = audit_env; settings = attempt(env, monkeypatch); calls = []
    async def read(*args, **kwargs): return status()
    async def lost(*args, **kwargs):
        calls.append(1)
        kwargs["on_event"]({"method": "thread/goal/set", "phase": "request_prepared", "rpc_id": "unknown-rpc"})
        raise TimeoutError("lost pause ACK")
    for _ in range(2):
        result = asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=read, native_command=lost))
        assert not result["verified_stop"] and result["phase"] == "needs_manual_reconciliation"
    assert calls == [1]
    async def stopped(*args, **kwargs): return status(goal="paused", state="interrupted")
    result = asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=stopped, native_command=lost))
    assert result["verified_stop"] and calls == [1]


def test_unknown_or_foreign_handle_never_pauses_someone_elses_task(audit_env, monkeypatch):
    env = audit_env; settings = attempt(env, monkeypatch)
    async def foreign(*args, **kwargs): return status(thread="another-thread")
    async def forbidden(*args, **kwargs): pytest.fail("foreign native command must not execute")
    result = asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=foreign, native_command=forbidden))
    assert not result["verified_stop"] and result["phase"] == "identity_unverified_no_mutation"


def test_native_stop_disabled_is_explicit_detection_only(audit_env, monkeypatch):
    settings = attempt(audit_env, monkeypatch, enabled=False)
    async def forbidden(*args, **kwargs): pytest.fail("disabled native stop must not connect")
    result = asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=forbidden, native_command=forbidden))
    assert result == {"verified_stop": False, "status": "disabled_detection_only"}


def test_actual_native_command_pauses_then_interrupts_latest_verified_owned_turn(monkeypatch):
    calls = []; observations = [status("auto-turn"), status("latest-auto-turn", goal="paused")]
    async def read(*args, **kwargs): return observations.pop(0)
    @asynccontextmanager
    async def connect(*args, **kwargs): yield object()
    async def call(ws, method, params, timeout, on_event):
        calls.append((method, params))
        if method == "thread/goal/set": return {"goal": {"threadId": "owned-thread", "status": "paused"}}
        return {}
    monkeypatch.setattr("app.services.rebuild_native_stop.websockets.connect", connect)
    monkeypatch.setattr("app.services.rebuild_native_stop._call", call)
    from types import SimpleNamespace
    settings = SimpleNamespace(codex_web_ipc_uri="ws://fixture", codex_web_dispatch_timeout_seconds=1)
    asyncio.run(_native_command("owned-thread", "first-turn", goal_mode=True, settings=settings, on_event=lambda event: None, native_read=read))
    assert calls == [("thread/goal/set", {"threadId": "owned-thread", "status": "paused"}),
                     ("turn/interrupt", {"threadId": "owned-thread", "turnId": "latest-auto-turn"})]


def test_concurrent_protective_stop_has_one_mutation_claim(audit_env, monkeypatch):
    env = audit_env; settings = attempt(env, monkeypatch); barrier = Barrier(2); calls = []
    async def read(*args, **kwargs):
        # Each caller's initial read reaches the barrier after its DB intent.
        if len(calls) == 0:
            try: barrier.wait(timeout=5)
            except Exception: pass
        return status()
    async def command(*args, **kwargs):
        calls.append(1)
        kwargs["on_event"]({"method": "thread/goal/set", "phase": "request_prepared"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: asyncio.run(protective_stop(settings.database_url, "owned-attempt", settings=settings, native_read=read, native_command=command)), range(2)))
    assert len(calls) == 1 and not any(item["verified_stop"] for item in results)
