"""Synchronous web-write lock bounds and worker-session commit semantics."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import rebuild as rebuild_api
from app.db.models import Paper
from app.main import app
from app.security.session_auth import SessionState
from app.services.paper_db_phase import LockConflictError, is_lock_conflict


class PaperLock:
    def __init__(self, engine, paper_id):
        self.engine = engine
        self.paper_id = paper_id
        self.ready = threading.Event()
        self.release = threading.Event()
        self.failure = None
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        try:
            with Session(bind=self.engine) as session:
                session.execute(select(Paper.id).where(
                    Paper.id == self.paper_id).with_for_update()).scalar_one()
                self.ready.set()
                self.release.wait(15)
                session.rollback()
        except BaseException as exc:
            self.failure = exc
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        if self.failure:
            raise self.failure
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release.set()
        self.thread.join(5)
        if self.failure and exc is None:
            raise self.failure


def _seed_paper(engine):
    with Session(bind=engine, expire_on_commit=False) as session:
        paper = Paper(title="sync lock fixture", library_name="acceptance",
                      pdf_path=f"{uuid4()}.pdf")
        session.add(paper)
        session.commit()
        return str(paper.id)


def _override_section_auth():
    auth = SessionState(
        username="acceptance-user", sid="acceptance", csrf="acceptance", remember=False,
        issued_at=0, expires_at=9999999999, ttl=3600,
    )
    app.dependency_overrides[
        rebuild_api._require_section_write_session] = lambda: auth


def test_sync_reading_save_maps_lock_timeout_to_409(setup_test_db):
    paper_id = _seed_paper(setup_test_db)
    from app.api import reading_assistance as route_module
    auth = SessionState(username="acceptance-user", sid="acceptance", csrf="acceptance",
                        remember=False, issued_at=0, expires_at=9999999999, ttl=3600)
    app.dependency_overrides[route_module._require_section_write_session] = lambda: auth
    body = {
        "save_mode": "abstract_only", "request_id": "sync-save-lock",
        "job_id": "acceptance-job", "input_revision": "0" * 64,
        "abstract_source_revision": "1" * 64,
        "abstract_zh": "独立锁争用测试摘要",
    }
    try:
        with PaperLock(setup_test_db, paper_id):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    f"/api/rebuild/papers/{paper_id}/reading-assistance", json=body)
        assert response.status_code == 409, response.text
        assert "paper_lock_busy_retry_same_request" in response.text
    finally:
        app.dependency_overrides.pop(
            route_module._require_section_write_session, None)


def test_history_write_uses_database_bounded_lock_timeout(setup_test_db):
    paper_id = _seed_paper(setup_test_db)
    _override_section_auth()
    body = {
        "request_id": "history-lock", "expected_revision": "0" * 64,
        "expected_binding": {
            "output_revision": "0" * 64, "source_revision": "0" * 64,
            "receipt_revision": "0" * 64, "plan_revision": None,
            "plan_generation": None, "known_unknown_revision": "0" * 64,
        },
        "report_id": "history-report", "expected_report_sha256": "0" * 64,
        "kind": "report_index", "note": "bounded lock probe",
    }
    holder = PaperLock(setup_test_db, paper_id)
    timer = None
    started = time.perf_counter()
    try:
        with holder:
            timer = threading.Timer(2.2, holder.release.set)
            timer.start()
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.put(
                    f"/api/rebuild/papers/{paper_id}/history-reconciliation", json=body)
        elapsed = time.perf_counter() - started
        assert response.status_code == 409, (response.status_code, response.text, elapsed)
        assert elapsed < 1.8, elapsed
        assert "paper_lock_busy_retry_same_request" in response.text
    finally:
        if timer is not None:
            timer.cancel()
        app.dependency_overrides.pop(
            rebuild_api._require_section_write_session, None)


def test_direct_locked_paper_has_database_bounded_timeout(setup_test_db):
    from app.services.rebuild_acceptance import _locked_paper
    paper_id = _seed_paper(setup_test_db)
    holder = PaperLock(setup_test_db, paper_id)
    timer = None
    started = time.perf_counter()
    try:
        with holder:
            timer = threading.Timer(2.2, holder.release.set)
            timer.start()
            with Session(bind=setup_test_db) as session, pytest.raises(LockConflictError):
                _locked_paper(session, paper_id)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.8, elapsed
    finally:
        if timer is not None:
            timer.cancel()


def test_processing_status_worker_commit_and_rollback(setup_test_db, monkeypatch):
    paper_id = _seed_paper(setup_test_db)

    def commit(worker, paper, payload):
        del paper, payload
        worker.get(Paper, paper_id).title = "worker committed"
        return {"status": "partial"}

    monkeypatch.setattr("app.services.paper_processing_status.report_processing", commit)
    with Session(bind=setup_test_db) as request_session:
        result = asyncio.run(rebuild_api._processing_status_write(
            request_session, paper_id, SimpleNamespace()))
    assert result == {"status": "partial"}
    with Session(bind=setup_test_db) as session:
        assert session.get(Paper, paper_id).title == "worker committed"

    def rollback(worker, paper, payload):
        del paper, payload
        worker.get(Paper, paper_id).title = "must rollback"
        raise ValueError("terminal_attempt_cannot_resume")

    monkeypatch.setattr("app.services.paper_processing_status.report_processing", rollback)
    with Session(bind=setup_test_db) as request_session:
        with pytest.raises(ValueError, match="terminal_attempt_cannot_resume"):
            asyncio.run(rebuild_api._processing_status_write(
                request_session, paper_id, SimpleNamespace()))
    with Session(bind=setup_test_db) as session:
        assert session.get(Paper, paper_id).title == "worker committed"


@pytest.mark.no_test_database
@pytest.mark.parametrize("sqlstate", ["55P03", "40P01"])
def test_lock_sqlstates_are_classified_as_retryable_conflicts(sqlstate):
    original = Exception("database lock conflict")
    original.sqlstate = sqlstate
    assert is_lock_conflict(OperationalError("SELECT", {}, original))
