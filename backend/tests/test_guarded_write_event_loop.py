"""Real Uvicorn regression for synchronous lock waits in async write routes.

The row case preserves the independent acceptance probe's 450 ms health bound.
All three routes must conflict while the holder still owns the paper, leave no
writes, and release their transactions before the independent lock readback.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api import rebuild as route_module
from app.db.models import AuditLog, Paper, RebuildPaperFile
from app.main import app
from app.security.session_auth import SessionState
from test_sync_write_lock_contract import PaperLock, _seed_paper


@pytest.mark.parametrize("route", ["row_identity", "extraction_check", "file_upload"])
def test_guarded_write_does_not_block_health(setup_test_db, monkeypatch, route):
    # Isolate endpoint effects from the unrelated startup recovery monitor.
    async def no_monitor(settings):
        await asyncio.Event().wait()
    monkeypatch.setattr("app.services.ordinary_parsing.monitor", no_monitor)
    paper_id = _seed_paper(setup_test_db)
    with Session(bind=setup_test_db) as session:
        initial_audits = [(str(a.id), a.action, a.payload) for a in session.scalars(
            select(AuditLog).where(AuditLog.paper_id == paper_id))]
    auth = SessionState(username="acceptance-user", sid="acceptance", csrf="acceptance",
                        remember=False, issued_at=0, expires_at=9999999999, ttl=3600)
    dependencies = (route_module._require_identity_correction_session,
                    route_module._require_section_write_session,
                    route_module._require_file_write_session)
    for dependency in dependencies:
        app.dependency_overrides[dependency] = lambda: auth
    monkeypatch.setattr(route_module, "origin_allowed", lambda *_: True)
    monkeypatch.setattr(route_module, "csrf_token_valid", lambda *_: True)
    if route == "row_identity":
        method, url = "PUT", f"/api/rebuild/rows/{uuid4()}/identity"
        kwargs = {"json": {
            "paper_id": paper_id, "row_key": "fixture-row", "request_id": str(uuid4()),
            "expected_revision": "0" * 64, "changes": {"configuration": "fixture"},
            "correction_reason": "Independent event-loop lock probe",
            "evidence": [{"file_id": str(uuid4()), "source_kind": "text",
                          "page_number": 1, "quote": "fixture evidence"}],
        }}
    elif route == "extraction_check":
        method, url = "PUT", f"/api/rebuild/papers/{paper_id}/extraction-checks"
        kwargs = {"json": {
            "request_id": str(uuid4()), "expected_check_revision": None,
            "expected_binding": {"output_revision": "0" * 64, "source_revision": "0" * 64,
                "receipt_revision": "0" * 64, "plan_revision": None, "plan_generation": None,
                "known_unknown_revision": "0" * 64},
            "stage": "source_scope", "review_statement": "Independent event-loop lock probe",
        }}
    else:
        method, url = "POST", f"/api/rebuild/papers/{paper_id}/files"
        kwargs = {"data": {"role": "si", "request_id": str(uuid4()),
            "expected_revision": "0" * 64, "action": "add", "reason": "lock probe",
            "supersede_file_ids": "[]"}, "files": {"file": ("fixture.pdf", b"%PDF-1.4\n", "application/pdf")}}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                         log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    base = f"http://127.0.0.1:{port}"
    try:
        with PaperLock(setup_test_db, paper_id):
            thread.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if httpx.get(base + "/api/health", timeout=0.25).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.05)
            else:
                raise AssertionError("isolated event-loop probe server did not start")

            async def scenario():
                async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                    started_write = time.perf_counter()
                    write_task = asyncio.create_task(client.request(method, url, **kwargs))
                    await asyncio.sleep(0.15)
                    assert not write_task.done(), "write must still be waiting on the holder"
                    started = time.perf_counter()
                    health = await client.get("/api/health")
                    latency = time.perf_counter() - started
                    assert not write_task.done(), "health must finish before the lock timeout"
                    write = await write_task
                    return write, health, latency, time.perf_counter() - started_write

            write, health, latency, write_elapsed = asyncio.run(scenario())
            assert write.status_code == 409, write.text
            assert "paper_lock_busy_retry_same_request" in write.text
            assert health.status_code == 200
            assert latency < 0.45, latency
            assert 0.8 <= write_elapsed < 1.8, write_elapsed
        # No transaction or partial record survives the failed request.
        with Session(bind=setup_test_db) as session:
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            assert str(session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update())) == paper_id
            assert [(str(a.id), a.action, a.payload) for a in session.scalars(
                select(AuditLog).where(AuditLog.paper_id == paper_id))] == initial_audits
            assert session.scalar(select(func.count()).select_from(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id)) == 0
            session.rollback()
        evidence = {"route": route, "health_seconds": latency,
                    "write_seconds": write_elapsed, "write_status": write.status_code}
        print(json.dumps(evidence))
        if destination := os.getenv("LITAI_GUARD_PROBE_EVIDENCE"):
            with Path(destination).open("a") as handle:
                handle.write(json.dumps(evidence) + "\n")
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive(), "fixture Uvicorn did not stop"
        for dependency in dependencies:
            app.dependency_overrides.pop(dependency, None)
