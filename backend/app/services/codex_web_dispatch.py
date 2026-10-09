"""Python port of the Codex-web IPC dispatch protocol.

Reuses the same WebSocket channel (``ws://…/__backend/ipc``) and message
format as ``scripts/codex_web_dispatch.cjs``. The backend calls this to
dispatch a self-contained autonomous agent thread for one-click AI extract,
without depending on Node.js inside the container.

Protocol (target mode):

1. ``thread/start`` — create a new thread (cwd, approvalPolicy, sandbox, model)
2. ``thread/goal/set`` — set an active objective
3. ``turn/start`` — send the prompt and begin work

Failures preserve an unknown attempt for readback. They never trigger fallback
or another thread creation. Luna ordinary mode skips goal creation entirely.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

import websockets
from websockets.exceptions import WebSocketException

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

CHANNEL_FROM_VIEW = "codex_desktop:message-from-view"
CHANNEL_FOR_VIEW = "codex_desktop:message-for-view"
HOST_ID = "local"


class CodexWebDispatchError(Exception):
    """Raised when dispatch to Codex-web fails (connection, RPC, or timeout)."""


@dataclass(frozen=True)
class DispatchResult:
    thread_id: str
    turn_id: str | None
    model: str
    goal_status: str | None
    dispatch_mode: str = "codex_web_target"
    start_effective_model: str | None = None
    start_effective_provider: str | None = None
    start_collaboration_verification: str = "not_exposed_by_start_ack"
    actual_inference_verified: bool = False


def _rpc_id() -> str:
    return f"rpc-{secrets.token_hex(8)}"


def _outer_id() -> str:
    return f"dispatch-{secrets.token_hex(8)}"


MODEL_PROVIDERS = {"gpt-6-luna": "openai", "cn:deepseek-v4.1-flash": "workbuddy"}

ALLOWED_MODEL_MODES = {"gpt-6-luna": "codex_web_ordinary", "cn:deepseek-v4.1-flash": "codex_web_target"}


def dispatch_mode_for_model(model: str) -> str:
    if model not in ALLOWED_MODEL_MODES:
        raise CodexWebDispatchError("unsupported_extraction_model: choose gpt-6-luna or cn:deepseek-v4.1-flash")
    return ALLOWED_MODEL_MODES[model]


async def _call(ws, method: str, params: dict[str, Any], timeout: float = 30.0,
                on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Persist a request before sending and its exact matched reply before proceeding.

    A single deadline bounds unrelated IPC traffic as well as send/receive.
    Lost replies are unknown outcomes, never permission to recreate a thread.
    """
    rpc_id, outer = _rpc_id(), _outer_id()
    message = {"type": "ipc-renderer-invoke", "requestId": outer, "channel": CHANNEL_FROM_VIEW,
               "args": [{"type": "mcp-request", "hostId": HOST_ID,
                         "request": {"id": rpc_id, "method": method, "params": params}}]}
    if on_event:
        on_event({"method": method, "phase": "request_prepared", "rpc_id": rpc_id,
                  "outer_id": outer, "params": params})
    deadline = time.monotonic() + timeout
    try:
        await asyncio_wait_for(ws.send(json.dumps(message)), max(0.001, deadline - time.monotonic()))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            raw = await asyncio_wait_for(ws.recv(), remaining)
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
            if not isinstance(msg, dict):
                continue
            if msg.get("requestId") == outer and msg.get("error"):
                if on_event:
                    on_event({"method":method,"phase":"response_received","response":msg})
                raise CodexWebDispatchError(f"IPC invoke error for {method}")
            if msg.get("type") != "ipc-main-event" or msg.get("channel") != CHANNEL_FOR_VIEW:
                continue
            for item in msg.get("args") or []:
                if not isinstance(item, dict) or item.get("type") != "mcp-response":
                    continue
                inner = item.get("message") or {}
                if not isinstance(inner,dict) or inner.get("id") != rpc_id:
                    continue
                if on_event:
                    on_event({"method":method,"phase":"response_received","response":inner})
                if inner.get("error"):
                    raise CodexWebDispatchError(f"RPC error for {method}: {json.dumps(inner['error'],ensure_ascii=False)[:800]}")
                result = inner.get("result")
                if not isinstance(result,dict):
                    raise CodexWebDispatchError(f"Invalid RPC result for {method}; readback required")
                return result
    except TimeoutError as exc:
        raise CodexWebDispatchError(f"RPC timeout for {method}; outcome unknown, readback required") from exc


async def asyncio_wait_for(coro, timeout: float):
    """Wrapper around asyncio.wait_for that raises TimeoutError."""
    import asyncio

    return await asyncio.wait_for(coro, timeout=timeout)


def _verify_start_effective(started: dict[str, Any], model: str, provider: str) -> str:
    """Check contract fields; a start ACK does not verify an inference turn."""
    if started.get("model") != model or started.get("modelProvider") != provider:
        raise CodexWebDispatchError("thread/start effective model/provider missing or mismatched; readback required")
    thread = started.get("thread")
    if isinstance(thread, dict) and (any(key in thread and thread[key] != expected for key, expected in
            (("model", model), ("modelProvider", provider)))):
        raise CodexWebDispatchError("thread/start contradictory thread model/provider; readback required")
    if "reasoningEffort" in started and started["reasoningEffort"] != "high":
        raise CodexWebDispatchError("thread/start effective effort mismatched; readback required")
    # Native ThreadStartResponse does not require this bridge extension.
    # Missing extension is unobserved, never filled from requested settings.
    if "collaborationMode" not in started:
        return "not_exposed_by_start_ack"
    collaboration = started["collaborationMode"]
    settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
    if (not isinstance(settings, dict) or collaboration.get("mode") != "default" or
            settings.get("model") != model or settings.get("reasoning_effort") != "high"):
        raise CodexWebDispatchError("thread/start effective collaboration mismatched; readback required")
    return "matched_start_ack_extension"


def _verify_goal(result: dict[str, Any], thread_id: str, *, active: bool,
                 objective: str | None = None) -> None:
    if result.get("threadId", thread_id) != thread_id or "goal" not in result:
        raise CodexWebDispatchError("goal identity missing or mismatched; readback required")
    goal = result["goal"]
    if active:
        if not isinstance(goal, dict) or goal.get("threadId") != thread_id or goal.get("status") != "active":
            raise CodexWebDispatchError("goal not confirmed active for original thread; readback required")
        if objective is not None and goal.get("objective") != objective:
            raise CodexWebDispatchError("goal objective missing or mismatched; readback required")
    elif goal is not None:
        raise CodexWebDispatchError("ordinary thread goal is not confirmed null; readback required")


async def dispatch_agent(*, prompt: str, objective: str, settings: Settings | None = None,
                         model: str | None = None,
                         on_event: Callable[[dict[str, Any]], None] | None = None) -> DispatchResult:
    """One attempt only: Luna ordinary or DeepSeek active-goal mode.

    The caller durably stores an attempt intent and each stage via on_event.
    No fallback, reconnect dispatch, resume mutation or request retry is performed.
    """
    runtime = settings or get_settings()
    chosen_model = model or runtime.codex_web_default_model
    mode = dispatch_mode_for_model(chosen_model)
    chosen_provider = MODEL_PROVIDERS[chosen_model]
    if on_event is None:
        raise CodexWebDispatchError("durable_dispatch_journal_required")
    if mode == "codex_web_target":
        if not prompt.strip() or len(prompt) > 4000:
            raise CodexWebDispatchError("target objective empty or exceeds 4000 characters; no native request sent")
        objective = prompt
    timeout = runtime.codex_web_dispatch_timeout_seconds
    try:
        async with websockets.connect(runtime.codex_web_ipc_uri, open_timeout=timeout) as ws:
            started = await _call(ws,"thread/start",{"cwd":runtime.codex_web_cwd,
                "approvalPolicy":"never","sandbox":"danger-full-access","model":chosen_model,
                "modelProvider":chosen_provider,"allowProviderModelFallback":False,
                "config":{"model_reasoning_effort":"high"}},timeout,on_event)
            thread = started.get("thread")
            thread_id = thread.get("id") if isinstance(thread,dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexWebDispatchError("thread/start missing thread.id; outcome unknown, readback required")
            collaboration_verification = _verify_start_effective(started, chosen_model, chosen_provider)
            on_event({"method":"thread/start","phase":"effective_verified",
                      "start_effective_model":started["model"],
                      "start_effective_provider":started["modelProvider"],
                      "start_collaboration_verification":collaboration_verification,
                      "actual_inference_verified":False})
            goal_status = None
            if mode == "codex_web_target":
                result = await _call(ws,"thread/goal/set",{"threadId":thread_id,"objective":objective,"status":"active"},timeout,on_event)
                _verify_goal(result, thread_id, active=True, objective=objective)
                goal_status = "active"
            goal_readback = await _call(ws,"thread/goal/get",{"threadId":thread_id},timeout,on_event)
            _verify_goal(goal_readback, thread_id, active=mode == "codex_web_target",
                         objective=objective if mode == "codex_web_target" else None)
            if mode == "codex_web_target":
                # Active goals may auto-wake. Never add a turn after observing that.
                # This read is not a compare-and-swap with turn/start.
                snapshot = await _call(ws,"thread/read",{"threadId":thread_id,"includeTurns":True},timeout,on_event)
                observed = snapshot.get("thread")
                status = observed.get("status") if isinstance(observed,dict) else None
                status_type = status.get("type") if isinstance(status,dict) else status
                if (not isinstance(observed,dict) or observed.get("id") != thread_id or
                        observed.get("modelProvider") != chosen_provider or
                        status_type not in {"idle","notLoaded"} or
                        (isinstance(status,dict) and status.get("activeFlags")) or
                        not isinstance(observed.get("turns"),list) or observed["turns"] or
                        not isinstance(thread.get("turns"),list) or thread["turns"]):
                    raise CodexWebDispatchError("target goal may have auto-started or thread state unknown; readback required")
            result = await _call(ws,"turn/start",{"threadId":thread_id,"input":[{"type":"text","text":prompt}],
                "approvalPolicy":"never","sandboxPolicy":{"type":"dangerFullAccess"},
                "model":chosen_model,"effort":"high",
                "collaborationMode":{"mode":"default","settings":{"model":chosen_model,
                    "reasoning_effort":"high","developer_instructions":None}}},timeout,on_event)
            if result.get("threadId") and result["threadId"] != thread_id:
                raise CodexWebDispatchError("turn response thread identity mismatch; readback required")
            turn = result.get("turn")
            turn_id = turn.get("id") if isinstance(turn,dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise CodexWebDispatchError("turn/start missing turn.id; outcome unknown, readback required")
            return DispatchResult(thread_id=thread_id,turn_id=turn_id,model=chosen_model,
                                  goal_status=goal_status,dispatch_mode=mode,
                                  start_effective_model=started["model"],start_effective_provider=started["modelProvider"],
                                  start_collaboration_verification=collaboration_verification,
                                  actual_inference_verified=False)
    except CodexWebDispatchError:
        raise
    except Exception as exc:
        raise CodexWebDispatchError(f"Dispatch stopped: {type(exc).__name__}; outcome requires readback") from exc


@dataclass(frozen=True)
class ThreadStatus:
    turn_status: str | None  # inProgress, queued, completed, failed, interrupted
    goal_status: str | None
    latest_progress: str | None
    final_report: str | None
    turn_id: str | None = None
    thread_id: str | None = None


async def read_thread_status(
    thread_id: str,
    settings: Settings | None = None,
    *, include_goal: bool = True,
) -> ThreadStatus | None:
    """Read the current status of a Codex-web thread.

    Returns ``ThreadStatus`` or ``None`` if the thread cannot be read
    (connection failure, etc.) — the caller should NOT fail the job
    just because a status read failed.
    """
    runtime = settings or get_settings()
    uri = runtime.codex_web_ipc_uri
    timeout = min(runtime.codex_web_dispatch_timeout_seconds, 15.0)

    try:
        async with websockets.connect(uri, open_timeout=timeout) as ws:
            # Read the thread
            thread_result = await _call(
                ws, "thread/read", {"threadId": thread_id, "includeTurns": True}, timeout
            )
            thread = (
                thread_result.get("thread") if isinstance(thread_result, dict) else None
            ) or {}

            if thread.get("id") != thread_id:
                raise CodexWebDispatchError("thread/read identity mismatch")
            goal_status = None
            if include_goal:
                try:
                    goal_result = await _call(ws,"thread/goal/get",{"threadId":thread_id},timeout)
                    goal = goal_result.get("goal")
                    goal_status = goal.get("status") if isinstance(goal,dict) else None
                except CodexWebDispatchError:
                    pass

            turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
            last_turn = turns[-1] if turns else {}
            turn_status = last_turn.get("status") if isinstance(last_turn, dict) else None

            # Extract progress text from agent messages
            items = (
                last_turn.get("items") if isinstance(last_turn.get("items"), list) else []
            )
            agent_messages = [
                item.get("text", "")
                for item in items
                if isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and isinstance(item.get("text"), str)
                and item.get("text", "").strip()
            ]
            latest_progress = agent_messages[-1] if agent_messages else None

            final_report = None
            if turn_status == "completed" and agent_messages:
                final_report = (
                    agent_messages[-2]
                    if len(agent_messages) > 1
                    else agent_messages[-1]
                )

            return ThreadStatus(
                turn_status=turn_status,
                goal_status=goal_status,
                latest_progress=latest_progress,
                final_report=final_report,
                turn_id=last_turn.get("id") if isinstance(last_turn,dict) else None,
                thread_id=thread_id,
            )
    except (CodexWebDispatchError, OSError, WebSocketException) as exc:
        logger.debug(
            "read_thread_status(%s) failed (job stays running): %s",
            thread_id, exc,
        )
        return None
    except Exception:
        logger.debug(
            "read_thread_status(%s) unexpected error (job stays running)",
            thread_id,
            exc_info=True,
        )
        return None
