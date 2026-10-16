import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.services.literature_tools import AdapterError, BrowserAdapter, ReadonlyDatabase, WorkbenchAPI
from app.services.rebuild_read_cache import paginated_text, rendered_page
from app.db.models import AuditLog, RebuildDataRow, RebuildPaperFile, WorkflowJob
from sqlalchemy import func, select
from test_rebuild_mutation_audit import audit_env, assets


def test_hash_bound_text_render_inventory_cache_and_no_scientific_mutation(audit_env, monkeypatch):
    env = audit_env; path = f'/api/rebuild/files/{env["file"]}'
    first = env["client"].get(path + "/text-pages?limit=1")
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["total_pages"] == 2 and first["has_more"] and len(first["items"]) == 1
    assert "1.00s" in first["items"][0]["text"]
    second = env["client"].get(first["next_page"]).json()
    assert second["items"][0]["physical_page"] == 2 and not second["has_more"]
    cached_files = list((env["settings"].storage_root / "rebuild_read_cache").rglob("text-1.json"))
    assert len(cached_files) == 1
    stamp = cached_files[0].stat().st_mtime_ns
    assert env["client"].get(path + "/text-pages?limit=1").json() == first
    assert cached_files[0].stat().st_mtime_ns == stamp
    render = env["client"].get(path + "/render-page?page=1&dpi=72")
    assert render.status_code == 200 and render.content.startswith(b"\x89PNG")
    assert render.headers["x-source-sha256"] == first["source_sha256"]
    assert render.headers["etag"].strip('"') == hashlib.sha256(render.content).hexdigest()
    assert env["client"].get(path + "/render-page?page=99").status_code == 400
    assert env["client"].get(path + "/text-pages?source_sha256=" + "f" * 64).status_code == 409
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 0
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0
    inventory = env["client"].get(path + "/asset-inventory").json()
    assert inventory["items"] == []
    response = env["client"].post(assets(env), json={"asset_key": "cache-test", "asset_type": "table", "file_id": str(env["file"]), "context_text": "1.00s"})
    assert response.status_code == 200
    changed = env["client"].get(path + "/asset-inventory").json()
    assert changed["source_sha256"] == inventory["source_sha256"]
    assert changed["inventory_revision"] != inventory["inventory_revision"]
    restored_snapshot = env["client"].get(path + "/asset-inventory?inventory_revision=" + inventory["inventory_revision"]).json()
    assert restored_snapshot == inventory
    assert len(list((env["settings"].storage_root / "rebuild_read_cache").rglob("inventory-*.json"))) == 2


def test_corrupt_cached_text_is_rejected_and_original_is_preserved(audit_env):
    env = audit_env
    with env["factory"]() as session:
        file = session.get(RebuildPaperFile, env["file"])
        source = env["settings"].storage_root / file.storage_path
        before = source.read_bytes()
        paginated_text(file, env["settings"], limit=1)
        cached = next((env["settings"].storage_root / "rebuild_read_cache").rglob("text-1.json"))
        cached.write_text('{"physical_page":1,"text":"wrong","text_sha256":"wrong"}')
    response = env["client"].get(f'/api/rebuild/files/{env["file"]}/text-pages?limit=1')
    assert response.status_code == 409
    assert source.read_bytes() == before


def test_db_adapter_uses_readonly_transaction_and_collects_all_pages(audit_env):
    env = audit_env
    with env["factory"]() as session:
        session.add_all([RebuildDataRow(paper_id=env["paper"], row_key=f"r{i}", reaction="SRR", material="fixture",
                                       active_site_type="dual_atom", data_type="dft") for i in range(502)])
        session.commit()
    adapter = ReadonlyDatabase(env["settings"].database_url)
    try:
        result = adapter.paper_package(env["paper"])
        assert result["rows"]["total"] == len(result["rows"]["items"]) == 502
        assert not result["rows"]["has_more"]
    finally:
        adapter.close()


def pages(pid):
    return [{"paper": {"id": pid}, "output_revision": "a" * 64, "rows": {
        "total": 2, "offset": i, "items": [{"id": str(i), "paper_id": pid}], "has_more": i == 0,
        "next_page": f"/api/rebuild/papers/{pid}/ai-work-package?rows_offset=1" if i == 0 else None}} for i in range(2)]


@pytest.mark.parametrize("breakage", ["none", "revision", "duplicate", "truncated", "foreign", "foreign_url"])
def test_api_adapter_validates_complete_stable_pagination(audit_env, breakage):
    pid = str(uuid4()); packet = pages(pid)
    if breakage == "revision": packet[1]["output_revision"] = "b" * 64
    if breakage == "duplicate": packet[1]["rows"]["items"][0]["id"] = "0"
    if breakage == "truncated": packet[1]["rows"]["items"] = []
    if breakage == "foreign": packet[1]["rows"]["items"][0]["paper_id"] = str(uuid4())
    if breakage == "foreign_url": packet[0]["rows"]["next_page"] = "https://foreign.test/api/steal"
    index = 0
    def handler(request):
        nonlocal index
        result = packet[index]; index += 1
        return httpx.Response(200, json=result)
    client = httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(handler))
    adapter = WorkbenchAPI("https://fixture.test", client=client)
    try:
        if breakage == "none":
            assert len(adapter.work_package(pid)["rows"]["items"]) == 2
        else:
            with pytest.raises(AdapterError): adapter.work_package(pid)
    finally:
        adapter.close()


def test_write_transport_failure_does_not_retry_or_output_credentials(audit_env):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic private network detail", request=request)
    client = httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(handler))
    client.cookies.set("litai_csrf", "test-only-csrf")
    adapter = WorkbenchAPI("https://fixture.test", client=client)
    try:
        with pytest.raises(AdapterError, match="outcome_unknown") as error:
            adapter.mutate("/api/rebuild/rows/import", {}, request_id="stable", task_id="task")
        assert len(calls) == 1 and calls[0].headers["X-Request-ID"] == "stable"
        assert "test-only-csrf" not in str(error.value) and "private network" not in str(error.value)
        with pytest.raises(AdapterError): adapter.get("//foreign.test/api/")
    finally:
        adapter.close()


def test_read_429_backoff_is_finite_and_bounded(audit_env, monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr("app.services.literature_tools.time.sleep", waits.append)
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    client = httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(handler))
    adapter = WorkbenchAPI("https://fixture.test", client=client)
    try:
        with pytest.raises(AdapterError, match="429"):
            adapter.get("/api/rebuild/templates")
    finally:
        adapter.close()
    assert len(calls) == 4 and waits == [5, 10, 20]


def test_stdlib_host_client_logs_in_locally_keeps_cookies_private_and_follows_all_pages(audit_env):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    pid = str(uuid4()); packet = pages(pid); received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/auth/login"
            assert payload == {"username": "fixture-user", "password": "fixture-only-password"}
            self.send_response(200)
            self.send_header("Set-Cookie", "litai_session=fixture-session; Path=/; HttpOnly")
            self.send_header("Set-Cookie", "litai_csrf=fixture-csrf; Path=/")
            self.end_headers(); self.wfile.write(b'{}')
        def do_GET(self):
            received.append(self.path)
            assert "litai_session=fixture-session" in self.headers.get("Cookie", "")
            index = 1 if "rows_offset=1" in self.path else 0
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(packet[index]).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True); worker.start()
    credentials = audit_env["settings"].storage_root / "synthetic-login.json"
    credentials.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}", "username": "fixture-user", "password": "fixture-only-password"}))
    try:
        adapter = WorkbenchAPI.from_local_credentials(credentials)
        try:
            result = adapter.work_package(pid)
            assert len(result["rows"]["items"]) == 2 and len(received) == 2
            encoded = json.dumps(result)
            assert "fixture-session" not in encoded and "fixture-only-password" not in encoded
        finally:
            adapter.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)
