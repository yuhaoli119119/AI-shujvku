"""Real socket HTTP and Streamable HTTP transport contracts."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select

from app.db.models import RebuildDataRow, WorkflowJob
from app.security.session_auth import create_session
from test_dataset_write_resume import dataset_row, existing_dataset
from test_rebuild_mutation_audit import audit_env
from test_mcp_v2 import mcp_env


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(mcp_env):
    from app.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/api/health", timeout=0.25).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError("isolated uvicorn did not become ready")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _result_data(result):
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    for item in getattr(result, "content", []):
        if getattr(item, "text", None):
            return json.loads(item.text)
    return None


async def _mcp_call(base_url, key, name, arguments):
    async with streamablehttp_client(
        f"{base_url}/mcp/", headers={"Authorization": f"Bearer {key}"}, timeout=20
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


def test_real_http_and_streamable_mcp_identity_replay_and_occupancy(live_server, mcp_env):
    env = mcp_env
    fixture = existing_dataset(env)
    with env["factory"]() as session:
        session.add(WorkflowJob(
            job_id="transport-owned-parser", type="ai_extract_rebuild", status="running",
            payload={"paper_id": str(env["paper"]), "actor": "writer"},
        ))
        session.commit()

    writer = create_session("writer", settings=env["settings"])
    other = create_session("other", settings=env["settings"])
    row_payload = dataset_row(env, fixture, row_key="transport-header-row")
    row_payload.pop("task_id", None)

    async def http_race():
        async def attempt(issued, request_id):
            body = {**row_payload, "request_id": request_id}
            async with httpx.AsyncClient(
                base_url=live_server, timeout=10,
                cookies={"litai_session": issued.token, "litai_csrf": issued.csrf},
            ) as client:
                return await client.post(
                    "/api/rebuild/rows/import", json=body,
                    headers={"X-CSRF-Token": issued.csrf, "Origin": live_server,
                             "X-Task-ID": "transport-owned-parser"},
                )
        return await asyncio.gather(
            attempt(writer, row_payload["request_id"]),
            attempt(other, "transport-other-request"),
        )

    responses = asyncio.run(http_race())
    assert sorted(response.status_code for response in responses) == [200, 409], [
        (response.status_code, response.text) for response in responses
    ]
    accepted = next(response for response in responses if response.status_code == 200)
    assert accepted.json()["audit"]["task_id"] == "transport-owned-parser"
    rejected = next(response for response in responses if response.status_code == 409)
    assert "paper_has_other_unresolved_writer_or_reviewer" in rejected.text

    with env["factory"]() as session:
        original = session.get(WorkflowJob, "transport-owned-parser")
        original.status = "failed"
        original.progress = {"native_terminal_verified": True}
        session.add(WorkflowJob(
            job_id="transport-new-http-writer", type="ai_extract_rebuild", status="running",
            payload={"paper_id": str(env["paper"]), "actor": "other"},
        ))
        session.commit()

    async def http_mutation(issued, body):
        async with httpx.AsyncClient(
            base_url=live_server, timeout=10,
            cookies={"litai_session": issued.token, "litai_csrf": issued.csrf},
        ) as client:
            return await client.post(
                "/api/rebuild/rows/import", json=body,
                headers={"X-CSRF-Token": issued.csrf, "Origin": live_server,
                         "X-Task-ID": "transport-owned-parser"},
            )

    replay = asyncio.run(http_mutation(writer, row_payload))
    assert replay.status_code == 200, replay.text
    assert replay.json()["audit"]["replayed"] is True
    assert replay.json()["audit"]["receipt_is_historical"] is True
    changed = json.loads(json.dumps(row_payload))
    changed["rows"][0]["material"] = "changed-transport-body"
    conflict = asyncio.run(http_mutation(writer, changed))
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "mutation_request_id_reused"

    with env["factory"]() as session:
        new_writer = session.get(WorkflowJob, "transport-new-http-writer")
        new_writer.status = "completed"
        new_writer.progress = {"native_terminal_verified": True}
        from app.services.rebuild_file_lifecycle import file_set
        file_set_revision = file_set(session, env["paper"])["file_set_revision"]
        session.commit()

    begin_arguments = {
        "request": {
            "paper_id": str(env["paper"]), "request_id": "transport-mcp-begin",
            "file_ids": [str(env["file"])], "file_set_revision": file_set_revision,
        }
    }

    async def mcp_begin_race():
        return await asyncio.gather(
            _mcp_call(live_server, "writer-fixture", "begin_paper_work", begin_arguments),
            _mcp_call(live_server, "other-fixture", "begin_paper_work", begin_arguments),
        )

    results = asyncio.run(mcp_begin_race())
    assert sum(not result.isError for result in results) == 1, results
    winner = next(result for result in results if not result.isError)
    winner_key = "writer-fixture" if results[0] is winner else "other-fixture"
    begun = _result_data(winner)

    context_result = asyncio.run(_mcp_call(
        live_server, winner_key, "get_reading_context",
        {"request": {"paper_id": str(env["paper"])}},
    ))
    assert not context_result.isError, context_result
    context = _result_data(context_result)
    reading_request = {
        "paper_id": str(env["paper"]), "work_id": begun["work_id"],
        "request_id": "transport-mcp-reading", "expected_revision": begun["work_revision"],
        "reading": {
            "request_id": "transport-mcp-reading", "input_revision": context["input_revision"],
            "abstract_zh": "合成摘要说明催化剂改善硫转化。",
            "one_sentence": "合成文献比较了催化剂对硫转化的作用。",
            "guide": {"research_objective": "比较反应表现"},
            "content_scope": "partial_text", "missing": ["合成测试范围"],
        },
    }
    saved_result = asyncio.run(_mcp_call(
        live_server, winner_key, "save_reading_assistance", {"request": reading_request},
    ))
    assert not saved_result.isError, saved_result
    saved = _result_data(saved_result)
    assert saved["work_id"] == begun["work_id"]

    finish_result = asyncio.run(_mcp_call(
        live_server, winner_key, "finish_paper_work",
        {"request": {
            "paper_id": str(env["paper"]), "work_id": begun["work_id"],
            "request_id": "transport-mcp-finish", "expected_revision": saved["work_revision"],
            "status": "partial", "message": "Synthetic transport scope is partial",
            "file_stages": [], "missing_items": ["Synthetic transport scope"],
        }},
    ))
    assert not finish_result.isError, finish_result

    newer = asyncio.run(_mcp_call(
        live_server, "writer-fixture" if winner_key == "other-fixture" else "other-fixture",
        "begin_paper_work", {"request": {
            "paper_id": str(env["paper"]), "request_id": "transport-mcp-new-work",
            "file_ids": [str(env["file"])], "file_set_revision": file_set_revision,
        }},
    ))
    assert not newer.isError, newer

    historical = asyncio.run(_mcp_call(
        live_server, winner_key, "save_reading_assistance", {"request": reading_request},
    ))
    assert not historical.isError, historical
    historical_data = _result_data(historical)
    assert historical_data["replayed"] is True
    assert historical_data["receipt_is_historical"] is True

    changed_mcp = json.loads(json.dumps(reading_request))
    changed_mcp["reading"]["one_sentence"] = "同请求标识不能覆盖已保存的 MCP 内容。"
    changed_result = asyncio.run(_mcp_call(
        live_server, winner_key, "save_reading_assistance", {"request": changed_mcp},
    ))
    assert changed_result.isError, changed_result
    assert "request_id_reused" in changed_result.model_dump_json()

    cross_actor = asyncio.run(_mcp_call(
        live_server, "writer-fixture" if winner_key == "other-fixture" else "other-fixture",
        "save_reading_assistance", {"request": reading_request},
    ))
    assert cross_actor.isError, cross_actor
    with env["factory"]() as session:
        assert session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.row_key == "transport-header-row")) is not None
