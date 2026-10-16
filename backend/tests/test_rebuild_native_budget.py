import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.codex_web_dispatch import (CodexWebDispatchError, ThreadStatus, dispatch_agent, matches_owned_attempt)

pytestmark = pytest.mark.no_test_database


@pytest.mark.parametrize("ack_budget,expected_success", [(100, True), (None, False), (101, False)])
def test_native_budget_is_explicit_and_checked_before_turn_start(monkeypatch, ack_budget, expected_success):
    settings = SimpleNamespace(codex_web_default_model="cn:deepseek-v4.1-flash", codex_web_cwd="/fixture",
                               codex_web_ipc_uri="ws://fixture.invalid", codex_web_dispatch_timeout_seconds=1)
    calls = []
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield object()
    async def call(ws, method, params, *args):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "owned", "turns": []}, "model": "cn:deepseek-v4.1-flash", "modelProvider": "workbuddy"}
        if method in {"thread/goal/set", "thread/goal/get"}:
            return {"goal": {"threadId": "owned", "status": "active", "objective": "bounded task", "tokenBudget": ack_budget}}
        if method == "thread/read":
            return {"thread": {"id": "owned", "modelProvider": "workbuddy", "status": "idle", "turns": []}}
        if method == "turn/start":
            return {"threadId": "owned", "turn": {"id": "first"}}
        raise AssertionError(method)
    monkeypatch.setattr("app.services.codex_web_dispatch.websockets.connect", connect)
    monkeypatch.setattr("app.services.codex_web_dispatch._call", call)
    task = dispatch_agent(prompt="bounded task", objective="bounded task", settings=settings, token_budget=100, on_event=lambda event: None)
    if expected_success:
        assert asyncio.run(task).thread_id == "owned"
        assert any(method == "turn/start" for method, _ in calls)
    else:
        with pytest.raises(CodexWebDispatchError, match="token budget"):
            asyncio.run(task)
        assert not any(method == "turn/start" for method, _ in calls)
    assert next(params for method, params in calls if method == "thread/goal/set")["tokenBudget"] == 100


def test_goal_continuation_requires_original_and_latest_turn_in_same_owned_thread():
    owned = ThreadStatus("completed", "complete", None, None, turn_id="later", thread_id="owned", turn_ids=("original", "later"))
    assert matches_owned_attempt(owned, "owned", "original", allow_goal_continuations=True)
    assert not matches_owned_attempt(owned, "owned", "original", allow_goal_continuations=False)
    assert not matches_owned_attempt(owned, "foreign", "original", allow_goal_continuations=True)
    missing = ThreadStatus("completed", "complete", None, None, turn_id="later", thread_id="owned", turn_ids=("later",))
    assert not matches_owned_attempt(missing, "owned", "original", allow_goal_continuations=True)


@pytest.mark.parametrize("turn_items,expected", [(None, None), ([], 0)])
def test_native_items_missing_is_unknown_not_a_verified_empty_completion(monkeypatch, turn_items, expected):
    from app.services.codex_web_dispatch import read_thread_status
    settings = SimpleNamespace(codex_web_ipc_uri="ws://fixture.invalid", codex_web_dispatch_timeout_seconds=1)
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield object()
    async def call(ws, method, params, *args):
        turn = {"id": "turn", "status": "completed"}
        if turn_items is not None:
            turn["items"] = turn_items
        return {"thread": {"id": "owned", "turns": [turn]}}
    monkeypatch.setattr("app.services.codex_web_dispatch.websockets.connect", connect)
    monkeypatch.setattr("app.services.codex_web_dispatch._call", call)
    assert asyncio.run(read_thread_status("owned", settings, include_goal=False)).item_count == expected
