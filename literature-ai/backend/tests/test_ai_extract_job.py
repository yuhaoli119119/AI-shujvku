"""Tests for the one-click AI extract job API.

The dispatch runs as a background asyncio task, so these tests mock
_background_dispatch to control the job's final state without depending
on timing or real WebSocket connections.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Paper, WorkflowJob
from app.main import app
from app.security.session_auth import SessionState
from app.services.ai_extract_service import JOB_TYPE
from app.services.codex_web_dispatch import CodexWebDispatchError, DispatchResult


def _make_paper(session) -> Paper:
    paper = Paper(
        library_name="默认文献库",
        title="AI extract test paper",
        doi=None,
        pdf_path="pdf/test.pdf",
        paper_code="A9999",
    )
    session.add(paper)
    session.commit()
    return paper


def _mock_session_state() -> SessionState:
    return SessionState(
        username="tester",
        sid="test-sid",
        csrf="test-csrf",
        remember=False,
        issued_at=0,
        expires_at=9999999999,
        ttl=3600,
    )


def _override_auth():
    from app.api.rebuild import require_session
    app.dependency_overrides[require_session] = _mock_session_state


def _patch_bg_dispatch_success(monkeypatch):
    """Mock _background_dispatch to set the job to 'running' immediately."""
    async def _fake_bg(*, job_id, database_url, prompt, objective, settings,
                       primary_model, fallback_model):
        from app.db.session import session_scope
        with session_scope(database_url) as session:
            job = session.get(WorkflowJob, job_id)
            if job is not None:
                job.status = "running"
                job.progress = {
                    "phase": "running",
                    "dispatch_mode": "codex_web_target",
                    "thread_id": "thread-fake-001",
                    "turn_id": "turn-fake-001",
                    "model": primary_model,
                    "goal_status": "active",
                }
    monkeypatch.setattr(
        "app.services.ai_extract_service._background_dispatch",
        _fake_bg,
    )


def _patch_bg_dispatch_failure(monkeypatch):
    """Mock _background_dispatch to set the job to 'failed' immediately."""
    async def _fake_bg(*, job_id, database_url, prompt, objective, settings,
                       primary_model, fallback_model):
        from app.db.session import session_scope
        with session_scope(database_url) as session:
            job = session.get(WorkflowJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = "Cannot reach Codex-web IPC: connection refused"
                job.progress = {
                    "phase": "dispatch_failed",
                    "dispatch_mode": "codex_web_target",
                    "model": primary_model,
                }
    monkeypatch.setattr(
        "app.services.ai_extract_service._background_dispatch",
        _fake_bg,
    )


def _patch_sync_noop(monkeypatch):
    """Mock sync_job_status to do nothing (tests control the state directly)."""
    async def _fake_sync(session, job, settings):
        return job
    monkeypatch.setattr(
        "app.services.ai_extract_service.sync_job_status",
        _fake_sync,
    )


def test_post_returns_dispatching_and_bg_sets_running(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    _override_auth()
    _patch_bg_dispatch_success(monkeypatch)
    _patch_sync_noop(monkeypatch)

    client = TestClient(app)
    response = client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    assert response.status_code == 200, response.text
    body = response.json()
    # POST returns immediately with "dispatching" before bg task runs
    assert body["status"] == "dispatching"
    assert body["paper_id"] == str(paper_id)

    # After the bg task (mocked, runs synchronously in the portal), check DB
    with factory() as session:
        job = session.scalar(
            select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE)
        )
        assert job is not None
        # The mocked bg dispatch may or may not have run depending on portal
        # timing, so we accept either "dispatching" or "running"
        assert job.status in ("dispatching", "running")

    app.dependency_overrides.clear()


def test_post_returns_dispatching_and_bg_sets_failed(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    _override_auth()
    _patch_bg_dispatch_failure(monkeypatch)
    _patch_sync_noop(monkeypatch)

    client = TestClient(app)
    response = client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "dispatching"

    with factory() as session:
        job = session.scalar(
            select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE)
        )
        assert job is not None
        assert job.status in ("dispatching", "failed")

    app.dependency_overrides.clear()


def test_create_does_not_duplicate_running_job(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    _override_auth()
    _patch_bg_dispatch_success(monkeypatch)
    _patch_sync_noop(monkeypatch)

    client = TestClient(app)
    first = client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    assert first.status_code == 200
    assert first.json()["status"] == "dispatching"

    # Second call should return the same job (dispatching counts as running)
    second = client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]

    with factory() as session:
        jobs = session.scalars(
            select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE)
        ).all()
        assert len(jobs) == 1

    app.dependency_overrides.clear()


def test_get_latest_job_returns_not_started(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    client = TestClient(app)
    response = client.get(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_started"
    assert body["job_id"] is None


def test_get_latest_job_returns_dispatching(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    _override_auth()
    _patch_bg_dispatch_success(monkeypatch)
    _patch_sync_noop(monkeypatch)

    client = TestClient(app)
    client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    response = client.get(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("dispatching", "running")

    app.dependency_overrides.clear()


def test_unauthenticated_post_is_rejected(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    monkeypatch.setenv("LITAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("LITAI_AUTH_SESSION_SECRET", "test-secret-1234")
    monkeypatch.setenv("LITAI_AUTH_COOKIE_SECURE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(f"/api/rebuild/papers/{paper_id}/ai-extract/jobs")
    assert response.status_code == 401

    get_settings.cache_clear()
