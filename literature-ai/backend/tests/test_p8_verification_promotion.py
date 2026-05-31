import pytest
from uuid import UUID
from fastapi.testclient import TestClient

from app.db.models import ExtractionFieldReview, AuditLog, Paper, EvidenceLocator, DFTResult
from app.main import app

import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
from app.db.models import Base
from app.db.session import get_db_session
from app.utils.review_safety import is_safe_verified_review

@pytest.fixture
def setup_test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_verification.db"
        db_url = f"sqlite:///{db_path}"

        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        yield engine

        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories
        for eng in list(_engines.values()):
            try:
                eng.dispose()
            except Exception:
                pass
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()

client = TestClient(app)

def create_mock_data(engine, pdf_path="test.pdf", has_locator=True, has_evidence_text=True, target_resolution_status="active", oa_status=None, is_orphan=False):
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Test Paper", pdf_path=pdf_path, authors=[], oa_status=oa_status)
        session.add(paper)
        session.flush()

        target_id = UUID(int=1)
        if not is_orphan:
            target = DFTResult(paper_id=paper.id)
            session.add(target)
            session.flush()
            target_id = target.id

        review = ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(target_id),
            field_name="value",
            original_value=1.0,
            reviewer_status="pending",
            target_resolution_status=target_resolution_status,
            evidence_text="mock evidence" if has_evidence_text else "",
        )
        session.add(review)
        session.flush()

        if has_locator:
            locator = EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=review.target_id,
                evidence_text="mock evidence" if has_evidence_text else "",
                locator_status="exact_page",
                page=1,
            )
            session.add(locator)

        session.commit()
        return str(review.id)

def test_promotion_requires_explicit_confirmation(setup_test_db):
    """Requires explicit confirm_human_review=True to proceed, otherwise 400"""
    engine = setup_test_db
    review_id = create_mock_data(engine)
    
    # Missing confirm_human_review
    response_no_confirm = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": False
    })
    assert response_no_confirm.status_code == 400
    assert "Explicit human confirmation is required" in response_no_confirm.json()["detail"]

    # Invalid target_status
    response_bad_status = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "something_else",
        "reviewed_value": 2.0,
        "confirm_human_review": True
    })
    assert response_bad_status.status_code == 400
    assert "must be 'verified' or 'safe_verified'" in response_bad_status.json()["detail"]

def test_promotion_blocked_if_locator_insufficient(setup_test_db):
    """If there's no exact_page locator or no evidence_text, refuse promotion even if target_status='verified'"""
    engine = setup_test_db
    review_id = create_mock_data(engine, has_locator=False)
    
    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": True
    })
    assert response.status_code == 400
    assert "missing explicit evidence text or exact locator" in response.json()["detail"]
    
    Session = sessionmaker(bind=engine)
    with Session() as session:
        review = session.get(ExtractionFieldReview, UUID(review_id))
        assert review.reviewer_status == "pending", "Reviewer status must not be modified if locator is missing"
        assert not is_safe_verified_review(review), "Review must not be considered safe_verified"

def test_metadata_only_paper_cannot_be_verified(setup_test_db):
    """If the paper is metadata_only with no PDF, it cannot be verified"""
    engine = setup_test_db
    review_id = create_mock_data(engine, pdf_path="", oa_status="metadata_only")
    
    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": True
    })
    assert response.status_code == 400
    assert "metadata-only" in response.json()["detail"]

def test_audit_log_persisted_after_promotion(setup_test_db):
    """After successful promotion, audit_logs table must have a record"""
    engine = setup_test_db
    review_id = create_mock_data(engine, has_locator=True, has_evidence_text=True)
    
    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "safe_verified",
        "reviewed_value": 2.0,
        "reviewer": "test_user",
        "confirm_human_review": True
    })
    assert response.status_code == 200
    audit_id = response.json()["audit_log_id"]
    
    Session = sessionmaker(bind=engine)
    with Session() as session:
        audit = session.get(AuditLog, UUID(audit_id))
        assert audit is not None
        assert audit.action == "promote_to_verified"
        assert audit.source == "test_user"
        assert audit.target_id == review_id
        assert audit.payload["before_state"]["reviewed_value"] is None
        assert audit.payload["after_state"]["reviewed_value"] == 2.0

def test_bulk_operation_not_implemented():
    """Bulk promote endpoint does not exist"""
    response = client.post("/api/reviews/promote-all", json={"review_ids": []})
    assert response.status_code == 404

def test_orphan_review_blocked(setup_test_db):
    """Orphan reviews (where the target target_id does not exist) cannot be promoted"""
    engine = setup_test_db
    review_id = create_mock_data(engine, is_orphan=True)

    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": True
    })
    assert response.status_code in {400, 404}
    assert "Target not found" in response.json()["detail"]

    Session = sessionmaker(bind=engine)
    with Session() as session:
        review = session.get(ExtractionFieldReview, UUID(review_id))
        assert review.reviewer_status == "pending"
        assert not is_safe_verified_review(review)
        assert session.query(AuditLog).count() == 0
