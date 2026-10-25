"""C5/C6 transport compatibility, without DB or production credentials."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import rebuild as api
from app.schemas.rebuild import RebuildVisualAssetRequest
from app.services import paper_processing_status, rebuild_workflow_service as workflow

pytestmark = pytest.mark.no_test_database


@pytest.fixture
def status_api(monkeypatch):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/rebuild")
    session = MagicMock()
    app.dependency_overrides[api.get_db_session] = lambda: session
    runtime_settings = object()
    monkeypatch.setattr(api, "get_settings", lambda: runtime_settings)
    auth_calls = []

    async def auth(request, *, settings=None):
        assert settings is runtime_settings
        auth_calls.append(request)
        if request.headers.get("x-test-auth") != "valid":
            raise HTTPException(401, "not_authenticated")
        return NS(username="fixture")

    monkeypatch.setattr(api, "require_session", auth)
    monkeypatch.setattr(workflow, "_required_paper", lambda session, paper_id: NS(id=paper_id))
    report = MagicMock(return_value={"status": "partial"})
    monkeypatch.setattr(paper_processing_status, "report_processing", report)

    async def worker_boundary(request_session, paper_id, payload):
        """The production boundary owns a real Session; this fixture makes its
        commit/rollback contract observable without putting SQL on the loop."""
        try:
            result = report(request_session, NS(id=paper_id), payload)
            request_session.commit()
            return result
        except Exception:
            request_session.rollback()
            raise

    monkeypatch.setattr(api, "_processing_status_write", worker_boundary)
    return TestClient(app), session, report, auth_calls


@pytest.mark.parametrize("embedded", [False, True])
def test_status_accepts_flat_and_legacy_envelope(status_api, embedded):
    client, session, report, auth_calls = status_api
    data = {"run_id": "stable-attempt", "status": "partial", "file_ids": [str(uuid4())]}
    response = client.put(f"/api/rebuild/papers/{uuid4()}/processing-status",
                          json={"payload": data} if embedded else data,
                          headers={"x-test-auth": "valid"})
    assert response.status_code == 200, response.text
    assert report.call_args.args[2].model_dump(mode="json")["file_ids"] == data["file_ids"]
    assert report.call_args.args[2].run_id == data["run_id"]
    assert len(auth_calls) == 1
    session.commit.assert_called_once()


@pytest.mark.parametrize("data", [
    {"run_id": "x", "status": "partial"},
    {"payload": {"run_id": "x", "status": "partial"}},
])
def test_status_still_requires_authenticated_session(status_api, data):
    client, session, report, _ = status_api
    response = client.put(f"/api/rebuild/papers/{uuid4()}/processing-status", json=data)
    assert response.status_code == 401
    report.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("data", [
    {"payload": {"run_id": "x", "status": "partial"}, "status": "parsed"},
    {"payload": {"run_id": "x", "status": "partial"}, "settings": {}},
    {"run_id": "x", "status": "partial", "settings": {}},
    {"payload": {"run_id": "x", "status": "partial", "unexpected": True}},
    {"payload": None},
    {"run_id": "x", "status": "unsupported"},
])
def test_status_rejects_mixed_unknown_and_invalid_bodies(status_api, data):
    client, session, report, _ = status_api
    response = client.put(f"/api/rebuild/papers/{uuid4()}/processing-status", json=data,
                          headers={"x-test-auth": "valid"})
    assert response.status_code == 422
    report.assert_not_called()
    session.commit.assert_not_called()


def test_status_business_conflicts_still_roll_back(status_api):
    client, session, report, _ = status_api
    report.side_effect = ValueError("terminal_attempt_cannot_resume")
    response = client.put(f"/api/rebuild/papers/{uuid4()}/processing-status",
                          json={"run_id": "x", "status": "parsing"}, headers={"x-test-auth": "valid"})
    assert response.status_code == 409
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_status_openapi_has_no_settings_body(status_api):
    client, *_ = status_api
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/rebuild/papers/{paper_id}/processing-status"]["put"]["requestBody"]
    choices = body["content"]["application/json"]["schema"]["anyOf"]
    assert {item["$ref"].split("/")[-1] for item in choices} == {
        "ProcessingStatusReport", "ProcessingStatusReportEnvelope"}
    envelope = schema["components"]["schemas"]["ProcessingStatusReportEnvelope"]
    assert set(envelope["properties"]) == {"payload"}
    assert envelope["additionalProperties"] is False


@pytest.fixture
def asset_env(monkeypatch):
    paper_id, file_id = uuid4(), uuid4()
    session = MagicMock()
    session.scalar.return_value = None
    monkeypatch.setattr(workflow, "_required_paper", lambda *args: NS(id=paper_id))
    monkeypatch.setattr(workflow, "get_file", lambda *args: NS(id=file_id, paper_id=paper_id, sha256="a" * 64))
    base = dict(asset_key="figure-1", asset_type="figure", file_id=file_id, page_numbers=[1])

    def prepare(**changes):
        return workflow.prepare_visual_asset(session, paper_id=paper_id,
            payload=RebuildVisualAssetRequest(**{**base, **changes}), settings=NS())

    return session, base, prepare


def rich_reading():
    return {"summary_zh": "Summary", "detailed_explanation_zh": "Full detail",
            "evidence_locators": [{"page": 1, "quote": "Exact source"}]}


def test_new_asset_null_equals_omitted_and_no_mutation(asset_env):
    session, _, prepare = asset_env
    assert prepare(reading_explanation=None) == prepare()
    for method in ("add", "flush", "commit", "delete"):
        getattr(session, method).assert_not_called()


@pytest.mark.parametrize("key", ["reading_explanation", "scientific_reading"])
def test_null_preserves_existing_rich_reading(asset_env, key):
    session, base, prepare = asset_env
    reading = rich_reading()
    existing = NS(id=uuid4(), provenance={key: reading, "custom": "keep"}, **base)
    before = deepcopy(existing.provenance)
    session.scalar.return_value = existing
    assert prepare(reading_explanation=None) == prepare()
    assert existing.provenance == before
    _, data = prepare(reading_explanation=None, provenance={"new": "preserved"})
    assert data["provenance"] == {**before, "new": "preserved"}


def test_null_cannot_hide_source_change_staleness(asset_env):
    session, base, prepare = asset_env
    session.scalar.return_value = NS(id=uuid4(), provenance={"reading_explanation": rich_reading()}, **base)
    _, data = prepare(reading_explanation=None, page_numbers=[2])
    assert data["provenance"]["reading_explanation"]["is_stale"] is True


@pytest.mark.parametrize("changes", [
    {"reading_explanation": {}},
    {"provenance": {"reading_explanation": None}},
    {"reading_explanation": None, "provenance": {"scientific_reading": {}}},
    {"reading_explanation": rich_reading(), "provenance": {"scientific_reading": {"summary": "conflict"}}},
])
def test_empty_or_conflicting_explicit_readings_still_rejected(asset_env, changes):
    _, _, prepare = asset_env
    with pytest.raises(workflow.RebuildWorkflowError, match="empty_or_conflicting_declarations"):
        prepare(**changes)


def test_null_allows_valid_provenance_reading_but_still_validates(asset_env):
    _, _, prepare = asset_env
    _, data = prepare(reading_explanation=None, provenance={"reading_explanation": rich_reading()})
    assert data["provenance"]["reading_explanation"]["is_stale"] is False
    bad = rich_reading()
    bad["file_id"] = str(uuid4())
    with pytest.raises(workflow.RebuildWorkflowError, match="reading_file_id_mismatch"):
        prepare(reading_explanation=None, provenance={"reading_explanation": bad})
