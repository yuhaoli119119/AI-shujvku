"""Synthetic PostgreSQL fixtures only: real HTTP/auth and transaction rollback."""
from __future__ import annotations

import copy
import hashlib
from uuid import uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import AuditLog, Paper, RebuildVisualAsset, RebuildPaperFile, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.main import app
from app.security.htpasswd import hash_password, write_htpasswd
from app.security.session_auth import create_session
from app.services.rebuild_mutation_audit import EVENT, RECEIPT


@pytest.fixture
def audit_env(setup_test_db, tmp_path, monkeypatch):
    auth_file = tmp_path / "fixture.htpasswd"
    write_htpasswd(auth_file, {"writer": hash_password("fixture-password"), "other": hash_password("fixture-password")})
    for name, value in {"LITAI_AUTH_ENABLED": "true", "LITAI_AUTH_HTPASSWD_FILE": str(auth_file),
                        "LITAI_AUTH_SESSION_SECRET": "isolated-audit-fixture", "LITAI_AUTH_REDIS_URL": "",
                        "LITAI_AUTH_COOKIE_SECURE": "false"}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    settings = get_settings()
    settings.storage_root.mkdir(parents=True)
    factory = sessionmaker(bind=setup_test_db, autoflush=False)
    with factory() as session:
        paper = Paper(title="Synthetic audit", library_name="默认文献库", paper_code="T999", pdf_path="fixture.pdf")
        session.add(paper); session.flush()
        path = settings.storage_root / "fixture.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((30, 40), "Synthetic table: s 1.00s")
            document.new_page().insert_text((30, 40), "Second synthetic page")
            document.save(path)
        file = RebuildPaperFile(paper_id=paper.id, role="si", storage_path="fixture.pdf", original_filename="fixture.pdf",
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
        session.add(file); session.commit()
        ids = {"paper": paper.id, "file": file.id}
    def client_for(actor):
        issued = create_session(actor, settings=settings)
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("litai_session", issued.token)
        client.cookies.set("litai_csrf", issued.csrf)
        client.headers.update({"X-CSRF-Token": issued.csrf, "Origin": "http://testserver"})
        return client
    client = client_for("writer")
    yield {**ids, "client": client, "client_for": client_for, "factory": factory, "engine": setup_test_db, "settings": settings}
    client.close()


def assets(env):
    return f'/api/rebuild/papers/{env["paper"]}/assets'


def row_body(env):
    return {"paper_id": str(env["paper"]), "request_id": str(uuid4()), "task_id": "synthetic-task",
            "rows": [{"row_key": "synthetic-row", "reaction": "SRR", "material": "fixture", "active_site_type": "dual_atom", "data_type": "dft",
                      "values": [{"field_name": "adsorption_energy_ev", "raw_value": "0.0100", "numeric_value": 0.01, "unit": "eV", "precision_digits": 4,
                                  "sources": [{"source_kind": "table", "file_id": str(env["file"]), "page_number": 2, "table_row": 0, "table_column": 1, "quote": "0.0100"}]}]}]}


def logs(env, action=EVENT):
    with env["factory"]() as session:
        return session.scalars(select(AuditLog).where(AuditLog.action == action)).all()


def test_partial_assets_receipts_replay_and_actor_not_from_payload(audit_env):
    env = audit_env
    body = {"request_id": "batch-1", "task_id": "task-1", "assets": [
        {"asset_key": "good", "asset_type": "table", "file_id": str(env["file"]), "context_text": " s 1.00s\nmissing — "},
        {"asset_key": "bad", "asset_type": "table", "file_id": str(uuid4())},
        {"asset_key": "invalid", "asset_type": "not-a-type"}]}
    response = env["client"].post(assets(env) + "/import", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["assets_imported"] == 1 and len(result["errors"]) == 2
    events = logs(env); assert len(events) == 1
    assert events[0].payload["actor"] == "writer"
    assert events[0].payload["before"] is None
    assert events[0].payload["after"]["context_text"] == " s 1.00s\nmissing — "
    assert logs(env, RECEIPT)[0].payload["outcome"] == "partial"
    retry = env["client"].post(assets(env) + "/import", json=body)
    assert retry.json()["audit"]["replayed"] is True
    assert len(logs(env)) == 1 and len(logs(env, RECEIPT)) == 1
    changed = copy.deepcopy(body); changed["assets"][0]["context_text"] = "changed"
    assert env["client"].post(assets(env) + "/import", json=changed).status_code == 409
    other = env["client_for"]("other")
    try:
        assert other.post(assets(env) + "/import", json=body).status_code == 409
    finally:
        other.close()


def test_rows_full_diff_sources_precision_partial_cells_and_idempotency(audit_env):
    env = audit_env; body = row_body(env)
    body["rows"][0]["values"].append({"field_name": "bad", "numeric_value": 3.14, "sources": []})
    response = env["client"].post("/api/rebuild/rows/import", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["rows_partial"] == 1
    snapshots = {entry.target_type: entry.payload["after"] for entry in logs(env)}
    assert set(snapshots) == {"rebuild_data_rows", "rebuild_data_values", "rebuild_value_sources"}
    assert snapshots["rebuild_data_values"]["raw_value"] == "0.0100"
    assert snapshots["rebuild_data_values"]["numeric_value"] == 0.01
    assert snapshots["rebuild_value_sources"]["file_id"] == str(env["file"])
    assert snapshots["rebuild_value_sources"]["table_row"] == 0
    assert env["client"].post("/api/rebuild/rows/import", json=body).json()["audit"]["replayed"]
    assert len(logs(env)) == 3


def test_audit_insert_failure_rolls_back_data_and_no_success_receipt(audit_env):
    env = audit_env
    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO audit_logs"):
            raise RuntimeError("synthetic audit storage failure")
    event.listen(env["engine"], "before_cursor_execute", fail_audit)
    try:
        response = env["client"].post("/api/rebuild/rows/import", json=row_body(env))
        assert response.status_code == 500
    finally:
        event.remove(env["engine"], "before_cursor_execute", fail_audit)
    with env["factory"]() as session:
        for model in (RebuildDataRow, RebuildDataValue, RebuildValueSource):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action.in_([EVENT, RECEIPT]))) == 0


def test_crop_is_immutable_update_diff_and_rollback_keeps_previous_image(audit_env):
    env = audit_env
    body = {"asset_key": "crop", "asset_type": "table", "file_id": str(env["file"]),
            "request_id": "crop-1", "regions": [{"page_number": 1, "bbox": [0.0, 0.0, 1.0, 0.5]}]}
    result = env["client"].post(assets(env) + "/crop", json=body)
    assert result.status_code == 200, result.text
    old = result.json()["asset"]
    original = env["settings"].storage_root / old["image_path"]
    pixels = original.read_bytes()
    assert env["client"].post(assets(env) + "/crop", json=body).json()["audit"]["replayed"]
    body["request_id"] = "crop-2"; body["regions"][0]["bbox"] = [0.0, 0.0, 1.0, 0.8]
    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO audit_logs"):
            raise RuntimeError("synthetic crop audit failure")
    event.listen(env["engine"], "before_cursor_execute", fail_audit)
    try:
        assert env["client"].post(assets(env) + "/crop", json=body).status_code == 500
    finally:
        event.remove(env["engine"], "before_cursor_execute", fail_audit)
    assert original.read_bytes() == pixels
    with env["factory"]() as session:
        assert session.get(RebuildVisualAsset, old["id"]).image_path == old["image_path"]
    update = {"request_id": "update-1", "asset_key": "crop", "asset_type": "table", "context_text": "1.00s"}
    response = env["client"].post(assets(env), json=update)
    assert response.status_code == 200, response.text
    diff = next(entry.payload for entry in logs(env) if entry.payload["operation"] == "assets.upsert")
    assert diff["before"]["context_text"] is None and diff["after"]["context_text"] == "1.00s"
    assert diff["before"]["image_path"] == diff["after"]["image_path"] == old["image_path"]


def test_backward_body_no_new_fields_still_audited_and_auth_required(audit_env):
    env = audit_env
    body = {"asset_key": "legacy", "asset_type": "table", "context_text": "s 1.00s"}
    response = env["client"].post(assets(env), json=body)
    assert response.status_code == 200 and response.json()["audit"]["request_id"]
    with TestClient(app) as anonymous:
        assert anonymous.post(assets(env), json=body).status_code == 401
    headers = dict(env["client"].headers); env["client"].headers.pop("X-CSRF-Token")
    assert env["client"].post(assets(env), json=body).status_code == 403
    env["client"].headers.update(headers)


def test_work_package_paginates_more_than_500_rows_and_exposes_contract(audit_env):
    env = audit_env
    with env["factory"]() as session:
        session.add_all([RebuildDataRow(paper_id=env["paper"], row_key=f"fixture-{i}", reaction="SRR", material=f"fixture-{i}",
                                       active_site_type="dual_atom", data_type="dft") for i in range(503)])
        session.commit()
    response = env["client"].get(f'/api/rebuild/papers/{env["paper"]}/ai-work-package')
    assert response.status_code == 200, response.text
    data = response.json(); rows = data["rows"]
    assert rows["total"] == 503 and len(rows["items"]) == 500 and rows["has_more"]
    assert "table_write_contract" in data
    next_rows = env["client"].get(rows["next_page"]).json()["rows"]
    assert len(next_rows["items"]) == 3 and next_rows["next_page"] is None
    assert not set(row["id"] for row in rows["items"]) & set(row["id"] for row in next_rows["items"])


def test_repeated_entity_in_batch_preserves_each_before_after_and_credentials_are_redacted(audit_env):
    import json
    env = audit_env
    body = {"request_id": "two-changes", "assets": [
        {"asset_key": "same", "asset_type": "table", "context_text": "first 1.00s", "provenance": {"api_key": "synthetic-secret-never-log"}},
        {"asset_key": "same", "asset_type": "table", "context_text": "second 0.0100"}]}
    response = env["client"].post(assets(env) + "/import", json=body)
    assert response.status_code == 200, response.text
    events = sorted(logs(env), key=lambda item: item.payload["item_index"])
    assert len(events) == 2
    assert events[0].payload["before"] is None
    assert events[0].payload["after"]["context_text"] == "first 1.00s"
    assert events[1].payload["before"]["context_text"] == "first 1.00s"
    assert events[1].payload["after"]["context_text"] == "second 0.0100"
    assert all(item["audit_event_ids"] for item in response.json()["results"])
    assert "synthetic-secret-never-log" not in json.dumps([event.payload for event in events] + [event.payload for event in logs(env, RECEIPT)])


def test_same_request_concurrent_retries_create_one_audit_and_one_asset(audit_env):
    from concurrent.futures import ThreadPoolExecutor
    env = audit_env
    body = {"asset_key": "same-retry", "asset_type": "table", "request_id": "same-request", "context_text": "s 1.00s"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env["client"].post(assets(env), json=body), range(2)))
    assert all(response.status_code == 200 for response in results)
    assert sum(response.json()["audit"]["replayed"] for response in results) == 1
    assert len(logs(env)) == len(logs(env, RECEIPT)) == 1


def test_new_structured_s13_body_physical_pages_and_caption_source_are_validated(audit_env):
    from app.db.models import PaperTable
    env = audit_env
    with env["factory"]() as session:
        file = session.get(RebuildPaperFile, env["file"])
        source = env["settings"].storage_root / file.storage_path
        with fitz.open(source) as document:
            for _ in range(70): document.new_page()
            document[70].insert_text((30, 40), "Table S13 synthetic caption")
            document[71].insert_text((30, 40), "s 1.00s; missing is unreported")
            body_bytes = document.tobytes()
        source.write_bytes(body_bytes)
        file.sha256 = hashlib.sha256(body_bytes).hexdigest(); file.file_size = len(body_bytes); session.commit()
    body = {"asset_key": "table-s13", "asset_type": "table", "file_id": str(env["file"]), "request_id": "s13",
            "caption": "Table S13 synthetic caption", "page_numbers": [71, 72], "context_text": "fallback",
            "provenance": {"table_data": {"columns": ["symbol", "time"], "rows": [["s", "1.00s"], ["—", None]], "footnotes": ["Missing is unreported"]},
                           "table_sources": [{"file_id": str(env["file"]), "page_number": 71, "role": "caption", "locator": "S13 caption", "quote": "Table S13 synthetic caption"},
                                             {"file_id": str(env["file"]), "page_number": 72, "role": "body", "locator": "S13 body", "table_row": 0, "table_column": 1, "quote": "s 1.00s"}]}}
    result = env["client"].post(assets(env), json=body)
    assert result.status_code == 200, result.text
    content = result.json()["asset"]["table_content"]
    assert content["format"] == "structured" and content["data"]["rows"] == [["s", "1.00s"], ["—", None]]
    assert content["sources"][0]["page_number"] == 71 and content["sources"][1]["page_number"] == 72
    missing = copy.deepcopy(body); missing["request_id"] = "missing-caption-source"
    missing["provenance"]["table_sources"] = missing["provenance"]["table_sources"][1:]
    assert env["client"].post(assets(env), json=missing).status_code == 400
    bad = copy.deepcopy(body); bad["request_id"] = "out-of-range-source"
    bad["page_numbers"] = [71, 73]; bad["provenance"]["table_sources"][1]["page_number"] = 73
    response = env["client"].post(assets(env), json=bad)
    assert response.status_code == 400 and response.json()["detail"] == "table_source_page_out_of_range"
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(PaperTable)) == 0
