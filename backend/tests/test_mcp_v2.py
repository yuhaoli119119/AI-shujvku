"""MCP v2 contract and synthetic PostgreSQL integration; no production writes."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from mcp.types import ImageContent

from app.config import get_settings
from app.mcp.context import mcp_auth_context
from test_rebuild_mutation_audit import audit_env, row_body  # isolated fixture PDF/database


EXPECTED_TOOLS = {
    "list_libraries_and_papers", "get_paper_workspace", "list_paper_files",
    "read_source_text", "view_source_page", "view_figure_image", "list_data_rows",
    "get_reading_context", "get_quality_report", "get_data_templates",
    "begin_paper_work", "save_figure_assets", "crop_figure", "preview_data_rows",
    "save_data_rows", "save_sections", "save_reading_assistance",
    "report_paper_work", "finish_paper_work", "get_write_receipt",
}


@pytest.fixture
def mcp_env(audit_env, monkeypatch):
    monkeypatch.setenv("LITAI_MCP_ENABLED", "true")
    monkeypatch.setenv("LITAI_MCP_API_KEYS", "reader|Reader|reader-fixture|read_papers;"
                       "writer|Writer|writer-fixture|read_papers,write_rebuild;"
                       "other|Other|other-fixture|read_papers,write_rebuild;"
                       "legacy|Legacy|legacy-fixture|read_papers,propose_corrections,request_parse")
    get_settings.cache_clear()
    yield audit_env
    get_settings.cache_clear()


@pytest.mark.no_test_database
def test_tools_list_is_exactly_new_contract_and_forbids_unknown_fields():
    from app.mcp.v2_server import mcp_server
    tools = asyncio.run(mcp_server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert len(tools) == 20
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        definitions = tool.inputSchema.get("$defs", {})
        for definition in definitions.values():
            if definition.get("type") == "object" and "properties" in definition:
                assert definition.get("additionalProperties") is False, tool.name


def test_http_anonymous_initialization_and_tool_calls_are_denied(mcp_env):
    from app.main import app
    client = TestClient(app)
    try:
        for path in ("/mcp", "/mcp/"):
            for method, params in (("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                   "clientInfo": {"name": "fixture", "version": "1"}}),
                                   ("tools/list", {}),
                                   ("tools/call", {"name": "get_data_templates", "arguments": {}})):
                response = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                assert response.status_code == 401, response.text
                assert "resource_metadata=" in response.headers["WWW-Authenticate"]

    finally:
        client.close()


def test_http_authenticated_tools_list_and_native_image(mcp_env):
    from app.main import app
    env = mcp_env
    path = env["settings"].storage_root / "fixture.pdf"
    headers = {"Authorization": "Bearer reader-fixture", "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        response = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert response.status_code == 200, response.text
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == EXPECTED_TOOLS
        response = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "view_source_page", "arguments": {"request": {"paper_id": str(env["paper"]), "file_id": str(env["file"]),
            "page": 1, "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}}})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        images = [item for item in result["content"] if item["type"] == "image"]
        assert len(images) == 1
        assert base64.b64decode(images[0]["data"]).startswith(b"\x89PNG\r\n\x1a\n")


def test_reader_key_cannot_begin_write(mcp_env):
    from app.mcp.v2_server import begin_paper_work
    from app.mcp.v2_contracts import BeginWork
    for key in ("reader-fixture", "legacy-fixture"):
        with mcp_auth_context(key), pytest.raises(PermissionError, match="write_rebuild"):
            begin_paper_work(BeginWork(paper_id=mcp_env["paper"], request_id="unauthorized",
                                      file_ids=[mcp_env["file"]], file_set_revision="0" * 64))


def test_disabled_mcp_rejects_even_valid_oauth_identity(mcp_env, monkeypatch):
    from app.main import app
    from app.mcp.context import MCPAuthInfo
    monkeypatch.setenv("LITAI_MCP_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr("app.oauth.verify_oauth_access_token", lambda token: MCPAuthInfo(
        "oauth-fixture", "fixture", frozenset({"read_papers", "write_rebuild"}), "", "mcp:oauth-fixture", True))
    client = TestClient(app)
    try:
        response = client.post("/mcp/", headers={"Authorization": "Bearer fixture-oauth"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert response.status_code == 503
    finally:
        client.close()


def test_reader_can_fetch_current_workspace_sources_rows_quality_and_templates(mcp_env):
    from app.mcp import v2_server as server
    from app.mcp.v2_contracts import Catalog, Workspace, PaperRequest, ListRows, Quality, ReadSourceText
    env = mcp_env
    digest = hashlib.sha256((env["settings"].storage_root / "fixture.pdf").read_bytes()).hexdigest()
    with mcp_auth_context("reader-fixture"):
        assert server.list_libraries_and_papers(Catalog())["items"]
        assert server.get_paper_workspace(Workspace(paper_id=env["paper"]))
        assert server.list_paper_files(PaperRequest(paper_id=env["paper"]))["file_set_revision"]
        assert server.read_source_text(ReadSourceText(paper_id=env["paper"], file_id=env["file"], source_sha256=digest))
        assert server.list_data_rows(ListRows(paper_id=env["paper"]))
        assert server.get_reading_context(PaperRequest(paper_id=env["paper"]))
        assert server.get_quality_report(Quality(paper_id=env["paper"]))
        assert server.get_data_templates()


def test_source_page_is_real_native_png_and_hash_change_rejected(mcp_env):
    from app.mcp.v2_server import view_source_page
    from app.mcp.v2_contracts import ViewSourcePage
    env = mcp_env
    path = env["settings"].storage_root / "fixture.pdf"
    request = ViewSourcePage(paper_id=env["paper"], file_id=env["file"], page=1,
                             source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with mcp_auth_context("reader-fixture"):
        content = view_source_page(request)
    images = [item for item in content if isinstance(item, ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n")
    from app.mcp.v2_server import mcp_server
    with mcp_auth_context("reader-fixture"):
        called = asyncio.run(mcp_server.call_tool("view_source_page", {"request": request.model_dump(mode="json")}))
    blocks = called[0] if isinstance(called, tuple) else called
    assert any(isinstance(block, ImageContent) for block in blocks)
    path.write_bytes(path.read_bytes() + b"\nchanged-source")
    with mcp_auth_context("reader-fixture"), pytest.raises(Exception, match="sha|hash|changed"):
        view_source_page(request)


def begin_fixture(env, *, actor="mcp:writer", request_id="begin-fixture"):
    from app.services import external_paper_work as work
    from app.services.rebuild_file_lifecycle import file_set
    with env["factory"]() as session:
        revision = file_set(session, env["paper"])["file_set_revision"]
        result = work.begin(session, env["paper"], actor, request_id, [env["file"]], revision)
        session.commit()
        return result


def test_same_paper_two_concurrent_actors_only_one_can_begin(mcp_env):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    def attempt(actor):
        try:
            return begin_fixture(env, actor=actor, request_id=actor.replace(":", "-"))
        except RebuildWorkflowError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["mcp:writer", "mcp:other"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum("paper_has_other_unresolved_writer_or_reviewer" in str(result) for result in results) == 1


def test_work_ownership_revisions_replay_terminal_readback_and_resume(mcp_env):
    from app.services import external_paper_work as work
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    begun = begin_fixture(env)
    with env["factory"]() as session:
        assert work.state(session, env["paper"], "mcp:writer", begun["work_id"])["work_revision"] == begun["work_revision"]
        with pytest.raises(RebuildWorkflowError, match="work_not_owned"):
            work.require_work(session, env["paper"], begun["work_id"], "mcp:other", begun["work_revision"])
        session.rollback()
        with pytest.raises(RebuildWorkflowError, match="revision_conflict"):
            work.report(session, env["paper"], begun["work_id"], "mcp:writer", "stale", "0" * 64, message="stale")
        session.rollback()
        result = work.report(session, env["paper"], begun["work_id"], "mcp:writer", "report", begun["work_revision"],
                             status="needs_check", message="Disconnected; resume same work")
        session.commit()
    with env["factory"]() as session:
        replay = work.report(session, env["paper"], begun["work_id"], "mcp:writer", "report", begun["work_revision"],
                             status="needs_check", message="Disconnected; resume same work")
        assert replay["replayed"] and replay["receipt_is_historical"]
        with pytest.raises(RebuildWorkflowError, match="request_id_reused"):
            work.report(session, env["paper"], begun["work_id"], "mcp:writer", "report", begun["work_revision"], message="changed")
        session.rollback()
        ended = work.finish(session, env["paper"], begun["work_id"], "mcp:writer", "finish", result["work_revision"],
                            status="partial", message="Missing SI cells", missing_items=["SI cells"])
        session.commit()
    with env["factory"]() as session:
        assert work.receipt(session, env["paper"], begun["work_id"], "mcp:writer", "report")["replayed"]
        with pytest.raises(RebuildWorkflowError, match="work_terminal"):
            work.report(session, env["paper"], begun["work_id"], "mcp:writer", "new", ended["work_revision"], message="new")
    path = env["settings"].storage_root / "fixture.pdf"
    path.write_bytes(path.read_bytes() + b"\nchanged-after-terminal")
    with env["factory"]() as session:
        assert work.receipt(session, env["paper"], begun["work_id"], "mcp:writer", "report")["receipt_is_historical"]
        assert work.finish(session, env["paper"], begun["work_id"], "mcp:writer", "finish", result["work_revision"],
                           status="partial", message="Missing SI cells", missing_items=["SI cells"])["replayed"]


def test_workbench_write_cannot_bypass_external_occupancy(mcp_env):
    env = mcp_env
    begun = begin_fixture(env)
    # Even self-reported task_id and actor cannot bypass the internal service context.
    response = env["client"].post(f'/api/rebuild/papers/{env["paper"]}/assets',
        json={"asset_key": "blocked", "asset_type": "table", "context_text": "fixture",
              "request_id": "bypass", "task_id": begun["work_id"]})
    assert response.status_code == 409, response.text


def test_http_existing_value_source_and_row_identity_writes_are_blocked(mcp_env):
    from app.db.models import RebuildDataValue, RebuildDataRow, RebuildValueSource
    from sqlalchemy import select
    env = mcp_env
    imported = env["client"].post("/api/rebuild/rows/import", json=row_body(env))
    assert imported.status_code == 200, imported.text
    with env["factory"]() as session:
        row = session.scalar(select(RebuildDataRow)).id
        value = session.scalar(select(RebuildDataValue)).id
        source = session.scalar(select(RebuildValueSource)).id
    begin_fixture(env)
    for endpoint in (f"/rows/{row}/identity", f"/values/{value}", f"/sources/{source}"):
        response = env["client"].put("/api/rebuild" + endpoint, json={})
        assert response.status_code == 409, (endpoint, response.text)
        assert "paper_has_other_unresolved_writer_or_reviewer" in response.text
    with env["factory"]() as session:
        assert session.get(RebuildDataValue, value).raw_value == "0.0100"


def test_physical_source_change_invalidates_existing_work(mcp_env):
    from app.services import external_paper_work as work
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    begun = begin_fixture(env)
    path = env["settings"].storage_root / "fixture.pdf"
    path.write_bytes(path.read_bytes() + b"\nfixture-changed")
    with env["factory"]() as session, pytest.raises(RebuildWorkflowError, match="sha256_mismatch"):
        work.require_work(session, env["paper"], begun["work_id"], "mcp:writer", begun["work_revision"])


def test_data_write_preserves_valid_cells_replays_without_duplicates_and_conflicts_on_changed_body(mcp_env):
    from app.mcp.v2_server import save_data_rows, preview_data_rows
    from app.mcp.v2_contracts import SaveDataRows, PreviewRows
    from app.db.models import RebuildDataValue, RebuildDataRow
    from sqlalchemy import func, select
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    begun = begin_fixture(env)
    rows = row_body(env)["rows"]
    rows[0]["values"].append({"field_name": "invalid-field", "numeric_value": 5, "sources": []})
    with mcp_auth_context("writer-fixture"):
        simulated = preview_data_rows(PreviewRows(paper_id=env["paper"], rows=rows))
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 0
    request = SaveDataRows(paper_id=env["paper"], work_id=begun["work_id"], request_id="data-partial",
                           expected_revision=begun["work_revision"], rows=rows)
    with mcp_auth_context("writer-fixture"):
        result = save_data_rows(request)
        assert result["rows_partial"] == 1
        assert save_data_rows(request)["replayed"]
        changed = request.model_copy(deep=True)
        changed.rows[0]["material"] = "changed"
        with pytest.raises(RebuildWorkflowError, match="request_id_reused"):
            save_data_rows(changed)
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 1
        values = list(session.scalars(select(RebuildDataValue)))
        assert len(values) == 1
        assert values[0].raw_value == "0.0100"


def test_audit_failure_rolls_back_mcp_rows_and_receipts(mcp_env):
    from app.mcp.v2_server import save_data_rows
    from app.mcp.v2_contracts import SaveDataRows
    from app.db.models import AuditLog, RebuildDataRow
    from sqlalchemy import event, func, select
    env = mcp_env
    begun = begin_fixture(env)
    request = SaveDataRows(paper_id=env["paper"], work_id=begun["work_id"], request_id="rollback-data",
                           expected_revision=begun["work_revision"], rows=row_body(env)["rows"])
    # Observe SQLAlchemy Engine class: MCP opens its own real database session.
    from sqlalchemy.engine import Engine
    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO audit_logs"):
            raise RuntimeError("synthetic audit storage failure")
    event.listen(Engine, "before_cursor_execute", fail_audit)
    try:
        with mcp_auth_context("writer-fixture"), pytest.raises(RuntimeError, match="audit storage"):
            save_data_rows(request)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_audit)
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.target_id == "rollback-data")) == 0


def test_backup_assets_crop_guide_and_completion_use_current_schema(mcp_env):
    from app.mcp import v2_server as server
    from app.mcp.v2_contracts import SaveAssets, CropFigure, SaveReading, PaperRequest, FinishWork, ViewFigureImage
    from app.db.models import WorkflowJob, Paper
    env = mcp_env
    with env["factory"]() as session:
        paper = session.get(Paper, env["paper"])
        paper.abstract = "A synthetic catalyst improves sulfur conversion."
        paper.reading_guide = {"legacy": "preserve"}
        paper.comprehensive_analysis = {"retained": {"value": 42}}
        session.commit()
    begun = begin_fixture(env)
    with env["factory"]() as session:
        backup = session.get(WorkflowJob, begun["work_id"]).payload["backup"]
    path = env["settings"].storage_root / "mcp-v2-backups" / str(env["paper"]) / backup["backup_id"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == backup["sha256"]
    assert path.stat().st_size == backup["bytes"]
    preimage = json.loads(path.read_bytes())["preimage"]
    assert preimage["papers"][0]["reading_guide"] == {"legacy": "preserve"}
    assert preimage["rebuild_visual_assets"] == []
    digest = hashlib.sha256((env["settings"].storage_root / "fixture.pdf").read_bytes()).hexdigest()
    base = {"paper_id": env["paper"], "work_id": begun["work_id"]}
    with mcp_auth_context("writer-fixture"):
        saved = server.save_figure_assets(SaveAssets(**base, request_id="assets", expected_revision=begun["work_revision"],
            assets=[{"asset_key": "synthetic-figure", "asset_type": "figure", "file_id": env["file"],
                     "source_sha256": digest, "page_numbers": [1], "explanation": "合成测试示意图。"}]))
        assert saved["assets_imported"] == 1, saved
        cropped = server.crop_figure(CropFigure(**base, request_id="crop", expected_revision=saved["work_revision"],
            crop={"asset_key": "synthetic-figure", "asset_type": "figure", "file_id": env["file"],
                  "source_sha256": digest, "regions": [{"page_number": 1, "bbox": [0, 0, 1, 0.5]}]}))
        assert cropped["asset"]["image_path"]
        image = server.view_figure_image(ViewFigureImage(paper_id=env["paper"], asset_id=cropped["asset"]["id"]))
        assert any(isinstance(item, ImageContent) for item in image)
        context = server.get_reading_context(PaperRequest(paper_id=env["paper"]))
        reading = {"request_id": "guide", "job_id": begun["work_id"], "input_revision": context["input_revision"],
                   "abstract_zh": "合成催化剂改善硫转化。", "one_sentence": "合成文献比较催化剂对硫转化的作用。",
                   "guide": {"research_objective": "比较反应动力学"}, "content_scope": "partial_text", "missing": ["合成测试"]}
        guide = server.save_reading_assistance(SaveReading(**base, request_id="guide", expected_revision=cropped["work_revision"], reading=reading))
        stages = [{"file_id": env["file"], "stage": "read", "status": "completed"},
                  {"file_id": env["file"], "stage": "assets", "status": "completed"},
                  {"file_id": env["file"], "stage": "rows", "status": "not_applicable", "message": "原文无可提取数值表。"}]
        finished = server.finish_paper_work(FinishWork(**base, request_id="done", expected_revision=guide["work_revision"],
            status="completed", message="合成测试已读回", file_stages=stages))
        assert finished["scientific_acceptance"] == "not_implied"
    with env["factory"]() as session:
        paper = session.get(Paper, env["paper"])
        assert paper.abstract.startswith("A synthetic")
        assert paper.reading_guide["legacy"] == "preserve"
        assert paper.comprehensive_analysis["retained"] == {"value": 42}


def test_completion_rejects_missing_stages_false_counts_and_empty_not_applicable_reason(mcp_env):
    from app.services import external_paper_work as work
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    begun = begin_fixture(env)
    for stages, expected in (([], "file_stage_coverage"),
            ([{"file_id": str(env["file"]), "stage": stage, "status": "completed"} for stage in ("read", "assets", "rows")], "file_results_or_reason"),
            ([{"file_id": str(env["file"]), "stage": "read", "status": "completed"},
              {"file_id": str(env["file"]), "stage": "assets", "status": "not_applicable", "message": ""},
              {"file_id": str(env["file"]), "stage": "rows", "status": "not_applicable", "message": "No data"}], "file_results_or_reason")):
        with env["factory"]() as session, pytest.raises(RebuildWorkflowError, match=expected):
            work.finish(session, env["paper"], begun["work_id"], "mcp:writer", "bad-finish", begun["work_revision"],
                        status="completed", message="premature", file_stages=stages)


def test_asset_key_cannot_rebind_existing_asset_from_unselected_source(mcp_env):
    from app.mcp import v2_server as server
    from app.mcp.v2_contracts import SaveAssets
    from app.db.models import RebuildPaperFile, RebuildVisualAsset
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env = mcp_env
    with env["factory"]() as session:
        source = session.get(RebuildPaperFile, env["file"])
        other = RebuildPaperFile(paper_id=env["paper"], role="main", storage_path=source.storage_path,
            original_filename="other-main.pdf", sha256=source.sha256, file_size=source.file_size)
        session.add(other); session.flush()
        asset = RebuildVisualAsset(paper_id=env["paper"], file_id=other.id, asset_key="occupied-key",
            asset_type="figure", explanation="preserve existing unrelated source")
        session.add(asset); session.commit()
        original_file, asset_id, digest = other.id, asset.id, source.sha256
    begun = begin_fixture(env)
    request = SaveAssets(paper_id=env["paper"], work_id=begun["work_id"], request_id="collision",
        expected_revision=begun["work_revision"], assets=[{"asset_key": "occupied-key", "asset_type": "figure",
            "file_id": env["file"], "source_sha256": digest, "page_numbers": [1], "explanation": "overwrite"}])
    with mcp_auth_context("writer-fixture"):
        try:
            result = server.save_figure_assets(request)
        except RebuildWorkflowError as exc:
            assert "scope" in str(exc)
        else:
            assert result["assets_imported"] == 0 and result["errors"]
    with env["factory"]() as session:
        asset = session.get(RebuildVisualAsset, asset_id)
        assert asset.file_id == original_file and asset.explanation == "preserve existing unrelated source"
