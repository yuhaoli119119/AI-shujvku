from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.main import app
from app.mcp.context import mcp_auth_context
from app.mcp.server import get_dft_review_task, mcp_server
from app.services.ide_prompt_service import build_ide_review_prompt


def _fake_review_task(paper_id):
    return {"paper_id": str(paper_id), "mode": "local_ai", "targets": []}


def test_get_dft_review_task_mcp_requires_read_capability_and_forwards_to_service(setup_test_db, monkeypatch):
    calls = []

    class FakeReviewBundleService:
        def __init__(self, session, settings):
            calls.append((session, settings))

        def get_review_task(self, paper_id, **kwargs):
            calls.append((paper_id, kwargs))
            return _fake_review_task(paper_id)

    monkeypatch.setattr("app.mcp.server.DFTReviewBundleService", FakeReviewBundleService)
    paper_id = uuid4()

    with mcp_auth_context("test-reader-key"):
        assert get_dft_review_task(str(paper_id)) == _fake_review_task(paper_id)

    assert calls[-1] == (
        paper_id,
        {"catalyst_sample_id": None, "dft_result_ids": []},
    )

    monkeypatch.setenv("LITAI_MCP_API_KEYS", "no_reader|No Reader|no-reader-key|append_notes")
    from app.config import get_settings

    get_settings.cache_clear()
    with mcp_auth_context("no-reader-key"), pytest.raises(PermissionError, match="read_papers"):
        get_dft_review_task(str(paper_id))


def test_get_dft_review_task_api_returns_json_without_writing_or_zip(setup_test_db, monkeypatch):
    calls = []

    class FakeReviewBundleService:
        def __init__(self, session, settings):
            calls.append((session, settings))

        def get_review_task(self, paper_id, **kwargs):
            calls.append((paper_id, kwargs))
            return _fake_review_task(paper_id)

        def build_zip(self, *args, **kwargs):
            raise AssertionError("read-only task endpoint must not build a ZIP")

    monkeypatch.setattr("app.api.papers.review_bundle.DFTReviewBundleService", FakeReviewBundleService)
    paper_id = uuid4()
    with Session(setup_test_db) as session:
        before = session.scalar(select(func.count()).select_from(AuditLog))

    response = TestClient(app).get(f"/api/papers/{paper_id}/dft-review-task")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in response.headers
    assert response.json() == _fake_review_task(paper_id)
    assert calls[-1] == (
        paper_id,
        {"catalyst_sample_id": None, "dft_result_ids": None},
    )
    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(AuditLog)) == before


def test_dft_live_review_tool_list_and_prompt_describe_local_ai_workflow(setup_test_db):
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "get_dft_review_task" in by_name
    assert set(by_name["get_dft_review_task"].inputSchema["required"]) == {"paper_id"}

    guide = TestClient(app).get("/api/system/agent-guide").json()
    assert "get_dft_review_task" in guide["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "get_ai_verification_tasks" in guide["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "submit_ai_verification_batch" in guide["recommended_entrypoint"]["json_schema_hint"]["curation_tools"]
    assert "verify_dft_results_batch" in guide["recommended_entrypoint"]["json_schema_hint"]["compatibility_tools"]
    endpoint = next(item for item in guide["http_endpoints"] if item["name"] == "get_dft_review_task")
    assert endpoint["method"] == "GET"
    assert endpoint["path"] == "/api/papers/{paper_id}/dft-review-task"
    assert "JSON only" in endpoint["purpose"]
    assert "get_dft_review_task -> get_codex_item/read_paper_page" in guide["legacy_suggested_client_prompt"]
    assert "offline DFT review ZIP is only for web AI, third-party, or offline review" in guide["legacy_suggested_client_prompt"]
    legacy_prompt = guide["legacy_suggested_client_prompt"]
    assert "get_ai_verification_tasks -> dedicated ai_verify_content identity -> submit_ai_verification_batch" in legacy_prompt
    assert "accept/correct/reject or exception" in legacy_prompt
    assert "Only exception enters Owner-session human handling" in legacy_prompt
    assert "There is no second model, vote, consensus, or third-AI adjudication" in legacy_prompt
    assert "compatibility endpoints only" in legacy_prompt
    assert "A dft_results lock plus import_analysis is not an authoritative verification path" in legacy_prompt
    assert "acquire dft_results lock -> import_analysis" not in legacy_prompt
    assert "one evidence-backed AI opinion may" not in legacy_prompt
    assert "later AI writes may overwrite earlier AI writes" not in legacy_prompt
    assert "one-call DFT repair/finalization" not in legacy_prompt
    assert "can enter the fast processing path" not in legacy_prompt

    prompt = build_ide_review_prompt("dft")
    assert "get_dft_review_task -> get_codex_item/read_paper_page" in prompt
    assert "离线 DFT 审阅 ZIP 只产生 proposal/candidate" in prompt
