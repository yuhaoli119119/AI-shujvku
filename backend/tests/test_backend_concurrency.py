"""Isolated real HTTP+MCP concurrency acceptance for cross-paper isolation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
import fitz
import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Paper, RebuildPaperFile, RebuildVisualAsset, WorkflowJob
from app.main import app
from app.security.session_auth import SessionState, require_session
from app.services.codex_web_dispatch import ThreadStatus
from app.services.paper_native_models import DEFAULT as DS_MODEL
from app.services.rebuild_file_lifecycle import file_set


LIBRARIES = ("锂硫双原子", "硫中毒")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * percentile) - 1))]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _thread_status(thread_id: str, turn_id: str) -> ThreadStatus:
    return ThreadStatus(
        turn_status="completed", goal_status="complete", latest_progress=None,
        final_report=None, turn_id=turn_id, thread_id=thread_id,
        turn_ids=("audit", turn_id), cwd="/opt/literature-ai", thread_status="idle",
        model_provider="workbuddy", goal_observed=True, model=DS_MODEL,
    )


def _result_data(result):
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    for item in getattr(result, "content", []):
        text_value = getattr(item, "text", None)
        if text_value:
            return json.loads(text_value)
    raise AssertionError(f"MCP result has no data: {result!r}")


async def _mcp_begin(base_url: str, paper_id: str, file_id: str, revision: str, index: int):
    async with streamablehttp_client(
        f"{base_url}/mcp/", headers={"Authorization": "Bearer load-writer"}, timeout=20
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("begin_paper_work", {"request": {
                "paper_id": paper_id,
                "request_id": f"load-begin-{index}",
                "file_ids": [file_id],
                "file_set_revision": revision,
            }})
            assert not result.isError, result
            return _result_data(result)


async def _stale_revision_probe(base_url: str, args: dict) -> bool:
    async with streamablehttp_client(
        f"{base_url}/mcp/", headers={"Authorization": "Bearer load-writer"}, timeout=20
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("report_paper_work", {"request": args})
            return bool(result.isError)


@pytest.mark.slow
@pytest.mark.integration
def test_sixteen_cross_library_http_mcp_are_isolated_and_cancel_releases(
    setup_test_db, tmp_path, monkeypatch
):
    """Run real ASGI HTTP and Streamable MCP against one isolated PostgreSQL schema."""
    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "load|Load writer|load-writer|read_papers,write_rebuild;"
        "reader|Load reader|load-reader|read_papers",
    )
    get_settings.cache_clear()
    settings = get_settings()
    storage = settings.storage_root
    storage.mkdir(parents=True, exist_ok=True)
    image = storage / "load-fixture.png"
    with fitz.open() as document:
        page = document.new_page(width=100, height=100)
        page.draw_rect(fitz.Rect(5, 5, 95, 95), color=(0, 0, 0), fill=(0.94, 0.94, 0.94))
        pixmap = page.get_pixmap(alpha=False)
        pixmap.save(str(image))

    factory = lambda: Session(bind=setup_test_db, autoflush=False, expire_on_commit=False)
    paper_rows = []
    for index in range(18):
        pdf = storage / f"load-{index}.pdf"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((30, 40), f"Synthetic isolated paper {index}")
            document.save(pdf)
        with factory() as session:
            paper = Paper(
                title=f"Concurrency fixture {index}",
                library_name=LIBRARIES[index % 2],
                paper_code=f"L{index:03d}",
                pdf_path=str(pdf.relative_to(storage)),
            )
            session.add(paper); session.flush()
            record = RebuildPaperFile(
                paper_id=paper.id, role="main", storage_path=str(pdf.relative_to(storage)),
                original_filename=pdf.name, sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                file_size=pdf.stat().st_size,
            )
            session.add(record); session.flush()
            asset = RebuildVisualAsset(
                paper_id=paper.id, file_id=record.id, asset_key="load-image", asset_type="figure",
                image_path=image.name, page_numbers=[1], status="accepted",
            )
            session.add(asset); session.flush()
            revision = file_set(session, paper.id)["file_set_revision"]
            session.commit()
            paper_rows.append({
                "paper_id": str(paper.id), "file_id": str(record.id), "revision": revision,
                "asset_id": str(asset.id), "library": paper.library_name,
            })

    # Two HTTP papers have an owned completed source thread. The native read is
    # deliberately slow and therefore must happen after the database phase ends.
    for index in (0, 1, 16, 17):
        with factory() as session:
            session.add(WorkflowJob(
                job_id=f"load-source-{index}", type="paper_native_thread_binding",
                status="completed", library_name=paper_rows[index]["library"],
                payload={"paper_id": paper_rows[index]["paper_id"], "actor": "tester",
                         "chosen_model": DS_MODEL},
                progress={"thread_id": f"load-thread-{index}", "turn_id": "audit",
                          "native_terminal_verified": True},
            ))
            session.commit()

    dispatch_lock = threading.Lock()
    dispatched = []

    async def fake_dispatch(**kwargs):
        await asyncio.sleep(0.02)
        with dispatch_lock:
            dispatched.append(kwargs["job_id"])

    monkeypatch.setattr("app.services.ai_extract_service._background_dispatch", fake_dispatch)

    async def slow_native(thread_id, settings=None, include_goal=True):
        del settings, include_goal
        await asyncio.sleep(0.45)
        index = int(thread_id.rsplit("-", 1)[-1])
        return _thread_status(thread_id, "audit")

    monkeypatch.setattr("app.services.codex_web_dispatch.read_thread_status", slow_native)

    auth = SessionState(
        username="tester", sid="load", csrf="load", remember=False,
        issued_at=0, expires_at=9999999999, ttl=3600,
    )
    app.dependency_overrides[require_session] = lambda: auth
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

        async def run_concurrency():
            limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
            clients = [httpx.AsyncClient(base_url=base_url, limits=limits, timeout=20) for _ in range(4)]

            def http_job(client, index, request_id):
                body = {"request_id": request_id, "model": DS_MODEL}
                if index in (0, 1, 16, 17):
                    body["source_job_id"] = f"load-source-{index}"
                return client.post(f"/api/rebuild/papers/{paper_rows[index]['paper_id']}/ai-extract/jobs", json=body)

            http_starts = time.perf_counter()
            http_tasks = [http_job(clients[index % len(clients)], index, f"load-http-{index}") for index in range(8)]
            mcp_tasks = [
                _mcp_begin(base_url, row["paper_id"], row["file_id"], row["revision"], index + 8)
                for index, row in enumerate(paper_rows[8:16])
            ]
            # Same-paper race uses the exact same idempotency key on the owned
            # source thread. Different-paper calls proceed at the same time.
            same_paper = [
                http_job(clients[0], 16, "load-same-paper"),
                http_job(clients[1], 16, "load-same-paper"),
            ]
            all_tasks = [*http_tasks, *mcp_tasks, *same_paper]

            probe_tasks = []
            for _ in range(20):
                probe_tasks.extend([
                    clients[0].get("/api/health"),
                    clients[1].get("/api/auth/me"),
                    clients[2].get(f"/api/rebuild/assets/{paper_rows[2]['asset_id']}"),
                    clients[3].get(f"/api/rebuild/papers/{paper_rows[3]['paper_id']}/ai-extract/jobs/latest"),
                ])
            probe_start = time.perf_counter()
            async def timed(awaitable):
                started_at = time.perf_counter()
                try:
                    return await awaitable, time.perf_counter() - started_at
                except Exception as exc:
                    return exc, time.perf_counter() - started_at

            measured_results = await asyncio.gather(*(timed(task) for task in all_tasks))
            measured_probes = await asyncio.gather(*(timed(task) for task in probe_tasks))
            results = [item for item, _ in measured_results]
            operation_latencies = [latency for _, latency in measured_results]
            probes = [item for item, _ in measured_probes]
            probe_latencies = [latency for _, latency in measured_probes]
            total_elapsed = time.perf_counter() - http_starts
            probe_elapsed = time.perf_counter() - probe_start

            await asyncio.sleep(0.1)

            for item in clients:
                await item.aclose()
            return (results, probes, total_elapsed, probe_elapsed,
                    operation_latencies, probe_latencies)

        started = time.perf_counter()
        (results, probes, elapsed, probe_elapsed, operation_latencies,
         probe_latencies) = asyncio.run(run_concurrency())
        operation_errors = sum(isinstance(result, Exception) for result in results)
        http_results = results[:8]
        mcp_results = results[8:16]
        same_results = results[16:]
        assert operation_errors == 0, [(index, value) for index, value in enumerate(results) if isinstance(value, Exception)]
        assert all(response.status_code == 200 for response in http_results), [response.text for response in http_results]
        assert all(not item.get("replayed") for item in mcp_results)
        assert same_results[0].status_code == same_results[1].status_code == 200
        assert same_results[0].json()["job_id"] == same_results[1].json()["job_id"]
        assert sum(1 for item in same_results if item.json()["job_id"] == same_results[0].json()["job_id"]) == 2

        probe_errors = sum(isinstance(item, Exception) for item in probes)
        assert probe_errors == 0
        health = probes[0::4]; login = probes[1::4]; images = probes[2::4]; unrelated = probes[3::4]
        assert all(item.status_code == 200 for item in health)
        assert all(item.status_code in (401, 503) for item in login)
        assert all(item.status_code == 200 and item.content.startswith(b"\x89PNG") for item in images)
        assert all(item.status_code == 200 for item in unrelated)

        # Active native I/O does not hold a paper row transaction. This competing
        # lock acquisition must succeed inside 250 ms.
        with factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            locked = session.execute(
                text("SELECT id FROM papers WHERE id = :paper_id FOR UPDATE"),
                {"paper_id": paper_rows[0]["paper_id"]},
            ).scalar_one()
            assert str(locked) == paper_rows[0]["paper_id"]
            session.rollback()

        # New requests no longer wait for an old native thread. Delay file-plan
        # construction for this one fixture to exercise a genuine disconnect.
        from app.services import ordinary_parsing
        original_file_plan = ordinary_parsing.file_plan
        def delayed_file_plan(session, paper_id, *args, **kwargs):
            if str(paper_id) == paper_rows[17]['paper_id']:
                time.sleep(0.45)
            return original_file_plan(session, paper_id, *args, **kwargs)
        monkeypatch.setattr(ordinary_parsing, 'file_plan', delayed_file_plan)

        # Client disconnect during the database phase cannot strand a commit.
        async def cancel_request():
            limits = httpx.Limits(max_connections=8)
            async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=20) as client:
                task = asyncio.create_task(client.post(
                    f"/api/rebuild/papers/{paper_rows[17]['paper_id']}/ai-extract/jobs",
                    json={"request_id": "load-cancel", "model": DS_MODEL,
                          "source_job_id": "load-source-17"},
                ))
                await asyncio.sleep(0.12)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0.55)
                replay = await client.post(
                    f"/api/rebuild/papers/{paper_rows[17]['paper_id']}/ai-extract/jobs",
                    json={"request_id": "load-cancel", "model": DS_MODEL,
                          "source_job_id": "load-source-17"},
                )
                return replay

        replay = asyncio.run(cancel_request())
        assert replay.status_code in (200, 409), replay.text
        if replay.status_code == 200:
            with factory() as session:
                jobs = list(session.scalars(select(WorkflowJob).where(
                    WorkflowJob.type == "ai_extract_rebuild",
                    WorkflowJob.payload.op("->>") ("request_id") == "load-cancel",
                )))
                assert len(jobs) == 1

        # A stale work revision is rejected through the actual MCP transport.
        first = mcp_results[0]
        stale = {
            "paper_id": paper_rows[8]["paper_id"], "work_id": first["work_id"],
            "request_id": "load-stale", "expected_revision": "0" * 64,
            "status": "running", "message": "stale probe", "file_stages": [], "missing_items": [],
        }
        assert asyncio.run(_stale_revision_probe(base_url, stale))

        with factory() as session:
            http_jobs = list(session.scalars(select(WorkflowJob).where(
                WorkflowJob.type == "ai_extract_rebuild",
                WorkflowJob.payload.op("->>") ("request_id").like("load-http-%"),
            )))
            mcp_jobs = list(session.scalars(select(WorkflowJob).where(
                WorkflowJob.type == "rebuild_external_extract",
                WorkflowJob.job_id.in_([item["work_id"] for item in mcp_results]),
            )))
            assert len(http_jobs) == 8
            assert len(mcp_jobs) == 8
            assert {job.payload["paper_id"] for job in http_jobs}.isdisjoint(
                {job.payload["paper_id"] for job in mcp_jobs})
            assert len(set(dispatched)) == len(dispatched)
            assert len(dispatched) == 10  # 8 HTTP + same-paper + cancelled/retried job.
            assert len({job.payload["paper_id"] for job in http_jobs}) == 8
            assert all(job.status == "running" for job in mcp_jobs)

        # Preserve numeric evidence for the independent acceptance window.
        evidence = {
            "operations": 18,
            "operation_errors": operation_errors,
            "operation_elapsed_seconds": round(elapsed, 4),
            "operation_latency_ms_p95": round(_percentile(operation_latencies, 0.95) * 1000, 2),
            "operation_latency_ms_max": round(max(operation_latencies) * 1000, 2),
            "probe_requests": 80,
            "probe_errors": probe_errors,
            "probe_elapsed_seconds": round(probe_elapsed, 4),
            "probe_latency_ms_p95": round(_percentile(probe_latencies, 0.95) * 1000, 2),
            "probe_latency_ms_max": round(max(probe_latencies) * 1000, 2),
            "total_elapsed_seconds": round(time.perf_counter() - started, 4),
            "health_status": [item.status_code for item in health],
            "login_status": [item.status_code for item in login],
            "image_bytes_min": min(len(item.content) for item in images),
            "unrelated_image_status": [item.status_code for item in images],
            "libraries": list(LIBRARIES),
            "http_jobs": len(http_jobs),
            "mcp_jobs": len(mcp_jobs),
            "unique_dispatch_count": len(dispatched),
            "stale_revision_rejected": True,
            "paper_lock_after_ipc": "acquired_within_250ms",
            "cancel_retry_status": replay.status_code,
        }
        evidence_path = tmp_path / "backend-concurrency-evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        assert evidence_path.is_file()
    finally:
        app.dependency_overrides.pop(require_session, None)
        server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()
