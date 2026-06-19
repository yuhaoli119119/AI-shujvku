from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditLog, Base, ModuleWriteLock, Paper, utcnow
from app.db.session import get_db_session
from app.main import app
from app.services.module_write_lock_service import ModuleWriteLockService


def _session(tmp_path, name: str = "module_locks.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def test_module_write_lock_blocks_same_module_and_allows_different_modules(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with Session(engine) as session:
            paper = Paper(title="Lock Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = paper.id

            service = ModuleWriteLockService(session)
            first = service.acquire(paper_id=paper_id, module_name="sections", locked_by="ai_1")

            try:
                service.acquire(paper_id=paper_id, module_name="sections", locked_by="ai_2")
                raise AssertionError("same-module lock should conflict")
            except ValueError as exc:
                assert str(exc).startswith("module_write_lock_conflict")

            figure_lock = service.acquire(paper_id=paper_id, module_name="figures", locked_by="ai_2")
            check = service.validate_write(
                paper_id=paper_id,
                module_names=["sections", "figures"],
                lock_tokens=[first.lock_token, figure_lock.lock_token],
            )

            assert check.valid is True
            assert check.covered_modules == ["figures", "sections"]
            assert session.scalar(select(AuditLog).where(AuditLog.action == "acquire_module_write_lock")) is not None
    finally:
        engine.dispose()


def test_module_write_lock_blocks_parent_child_scope(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_parent_child.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Parent Child Lock Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = paper.id

            service = ModuleWriteLockService(session)
            service.acquire(paper_id=paper_id, module_name="content", locked_by="ai_1")

            with pytest.raises(ValueError, match="module_write_lock_conflict"):
                service.acquire(paper_id=paper_id, module_name="sections", locked_by="ai_2")
    finally:
        engine.dispose()


def test_module_write_lock_scope_and_release(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_scope.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Scope Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = paper.id

            service = ModuleWriteLockService(session)
            lock = service.acquire(paper_id=paper_id, module_name="content", locked_by="ai_writer")
            check = service.validate_write(
                paper_id=paper_id,
                module_names=[
                    "sections",
                    "writing_cards",
                    "metadata",
                    "mechanism_claims",
                    "electrochemical_performance",
                    "catalyst_samples",
                ],
                lock_tokens=[lock.lock_token],
                locked_by="ai_writer",
            )
            assert check.valid is True

            service.release(lock_token=lock.lock_token, released_by="ai_writer")
            failed = service.validate_write(
                paper_id=paper_id,
                module_names=["sections"],
                lock_tokens=[lock.lock_token],
                locked_by="ai_writer",
            )
            assert failed.valid is False
            assert failed.missing_modules == ["sections"]
    finally:
        engine.dispose()


def test_module_write_lock_audit_truncates_long_owner_source(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_long_owner.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Long Owner Lock Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()

            owner = f"paper_operation:prepare_workspace:{uuid4().hex}"
            lock = ModuleWriteLockService(session).acquire(
                paper_id=paper.id,
                module_name="all_non_dft",
                locked_by=owner,
            )
            ModuleWriteLockService(session).release(lock_token=lock.lock_token, released_by=owner)
            session.commit()

            logs = session.scalars(select(AuditLog).where(AuditLog.target_id == str(lock.id)).order_by(AuditLog.created_at.asc())).all()
            assert [item.action for item in logs] == ["acquire_module_write_lock", "release_module_write_lock"]
            assert all(len(item.source) <= ModuleWriteLockService.AUDIT_SOURCE_MAX_LENGTH for item in logs)
            assert all(item.source != owner for item in logs)
    finally:
        engine.dispose()


def test_module_write_lock_expired_lock_can_be_reacquired(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_expiry.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Expiry Paper", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.flush()
            expired = ModuleWriteLock(
                paper_id=paper.id,
                module_name="writing_cards",
                locked_by="ai_1",
                expires_at=utcnow() - timedelta(minutes=1),
            )
            session.add_all([paper, expired])
            session.commit()
            paper_id = paper.id

            lock = ModuleWriteLockService(session).acquire(
                paper_id=paper_id,
                module_name="writing_cards",
                locked_by="ai_2",
            )

            assert lock.locked_by == "ai_2"
            assert session.get(ModuleWriteLock, expired.id).status == "expired"
    finally:
        engine.dispose()


def test_module_write_lock_uses_non_blocking_postgres_scope_lock():
    session = Mock()
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    session.execute.return_value.scalar.return_value = False

    service = ModuleWriteLockService(session)

    with pytest.raises(ValueError, match="module_write_lock_conflict:paper_transaction_busy"):
        service._lock_paper_scope(uuid4())


def test_module_write_lock_api_requires_token_for_direct_auto_apply(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_api.db")

    def override_get_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        with Session(engine) as session:
            paper = Paper(title="API Lock Paper", abstract="Old", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = paper.id

        client = TestClient(app)
        blocked = client.post(
            "/api/external-analysis/import",
            json={
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "source_label": "ai_writer",
                "auto_apply_review_rules": True,
                "reviewer": "ai_writer",
                "raw_payload": {
                    "correction_proposals": [
                        {
                            "field_name": "abstract",
                            "target_path": "abstract",
                            "operation": "replace",
                            "proposed_value": "New abstract",
                            "reason": "Evidence-backed rewrite.",
                            "evidence_payload": {"page": 1, "quoted_text": "Old"},
                        }
                    ]
                },
            },
        )
        assert blocked.status_code == 409
        assert "module_write_lock_required" in blocked.text

        acquired = client.post(
            "/api/module-locks/acquire",
            json={"paper_id": str(paper_id), "module_name": "content", "locked_by": "ai_writer"},
        )
        assert acquired.status_code == 200
        allowed = client.post(
            "/api/external-analysis/import",
            json={
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "source_label": "ai_writer",
                "auto_apply_review_rules": True,
                "reviewer": "ai_writer",
                "write_lock_token": acquired.json()["lock_token"],
                "raw_payload": {
                    "correction_proposals": [
                        {
                            "field_name": "abstract",
                            "target_path": "abstract",
                            "operation": "replace",
                            "proposed_value": "New abstract",
                            "reason": "Evidence-backed rewrite.",
                            "evidence_payload": {"page": 1, "quoted_text": "Old"},
                        }
                    ]
                },
            },
        )
        assert allowed.status_code == 200, allowed.text
        with Session(engine) as session:
            assert session.get(Paper, paper_id).abstract == "New abstract"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_external_analysis_api_defaults_reviewer_from_source_label_for_lock_validation(tmp_path):
    engine, SessionLocal = _session(tmp_path, "module_lock_api_default_reviewer.db")

    def override_get_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        with Session(engine) as session:
            paper = Paper(title="API Default Reviewer Lock Paper", abstract="Old", pdf_path="paper.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = paper.id

        client = TestClient(app)
        acquired = client.post(
            "/api/module-locks/acquire",
            json={"paper_id": str(paper_id), "module_name": "content", "locked_by": "api_writer"},
        )
        assert acquired.status_code == 200

        allowed = client.post(
            "/api/external-analysis/import",
            json={
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "source_label": "api_writer",
                "auto_apply_review_rules": True,
                "write_lock_token": acquired.json()["lock_token"],
                "raw_payload": {
                    "correction_proposals": [
                        {
                            "field_name": "abstract",
                            "target_path": "abstract",
                            "operation": "replace",
                            "proposed_value": "New abstract via default reviewer",
                            "reason": "Evidence-backed rewrite.",
                            "evidence_payload": {"page": 1, "quoted_text": "Old"},
                        }
                    ]
                },
            },
        )
        assert allowed.status_code == 200, allowed.text
        with Session(engine) as session:
            assert session.get(Paper, paper_id).abstract == "New abstract via default reviewer"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
