import os
import pytest
from uuid import UUID
from fastapi.testclient import TestClient

from app.db.models import ExtractionFieldReview, AuditLog, Paper, EvidenceLocator, DFTResult, MechanismClaim
from app.main import app

import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
from app.db.models import Base
from app.db.session import get_db_session
from app.services.dft_review_service import DFTResultReviewService
from app.utils.review_safety import content_object_gate, is_safe_verified_review

OWNER_HEADERS = {"X-LitAI-Owner-Token": "owner-secret"}

@pytest.fixture
def setup_test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]

        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "owner-secret")
        monkeypatch.setenv("LITAI_STORAGE_ROOT", tmpdir)
        Path(tmpdir, "test.pdf").write_bytes(b"%PDF-1.4\nreview test\n%%EOF\n")
        Path(tmpdir, "paper.pdf").write_bytes(b"%PDF-1.4\nmechanism test\n%%EOF\n")
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

client = TestClient(app, headers=OWNER_HEADERS)


def test_anonymous_loopback_cannot_promote_final_verification(setup_test_db):
    review_id = create_mock_data(setup_test_db)

    response = TestClient(app).post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": True,
    })

    assert response.status_code == 401


def test_owner_session_cookie_is_httponly_and_can_promote(setup_test_db):
    review_id = create_mock_data(setup_test_db)
    browser = TestClient(app)

    opened = browser.post("/api/settings/owner-session", json={"token": "owner-secret"})

    assert opened.status_code == 200
    set_cookie = opened.headers["set-cookie"]
    assert "owner-secret" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    promoted = browser.post(
        f"/api/reviews/{review_id}/promote",
        json={
            "target_status": "verified",
            "reviewed_value": 2.0,
            "confirm_human_review": True,
        },
    )
    assert promoted.status_code == 200

    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        audit = session.query(AuditLog).filter_by(target_id=review_id).order_by(AuditLog.created_at.desc()).first()
        assert audit is not None
        assert audit.source == "owner_session"


def test_owner_session_rejects_invalid_token_with_403(setup_test_db):
    response = TestClient(app).post("/api/settings/owner-session", json={"token": "wrong"})
    assert response.status_code == 403


def test_ai_service_call_cannot_write_final_dft_verification(setup_test_db):
    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        paper = Paper(title="AI cannot finalize", pdf_path="test.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(paper_id=paper.id, value=-1.0, evidence_text="Exact DFT evidence.")
        session.add(row)
        session.flush()
        with pytest.raises(ValueError, match="authenticated_human_actor_required"):
            DFTResultReviewService(session).verify_result(
                paper_id=paper.id,
                result_id=row.id,
                confirm_reviewed_against_pdf=True,
                verification_actor_type="ai",
                actor_name="external_ai",
                source_label="mcp",
                evidence_payload={"page": 1, "quoted_text": row.evidence_text},
                commit=False,
            )
        assert session.query(ExtractionFieldReview).filter_by(target_id=str(row.id)).count() == 0

def create_mock_data(
    engine,
    pdf_path="test.pdf",
    has_locator=True,
    has_evidence_text=True,
    target_resolution_status="active",
    oa_status=None,
    is_orphan=False,
    locator_status="exact_page",
    locator_page=1,
):
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Test Paper", pdf_path=pdf_path, authors=[], oa_status=oa_status)
        session.add(paper)
        session.flush()

        target_id = UUID(int=1)
        if not is_orphan:
            target = DFTResult(
                paper_id=paper.id,
                value=1.0,
                evidence_text="mock evidence" if has_evidence_text else "",
            )
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
                locator_status=locator_status,
                page=locator_page,
            )
            session.add(locator)

        session.commit()
        return str(review.id)


def test_mark_verified_requires_owner_and_records_server_identity(setup_test_db):
    review_id = create_mock_data(setup_test_db)
    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        review = session.get(ExtractionFieldReview, UUID(review_id))
        paper_id = review.paper_id
        target_id = review.target_id
        write_version = review.write_version

    payload = {
        "target_type": "dft_results",
        "target_id": target_id,
        "field_names": ["value"],
        "expected_write_version": write_version,
        "reviewer": "forged-ai-name",
    }
    path = f"/api/extraction/results/{paper_id}/reviews/mark-verified"

    assert TestClient(app).post(path, json=payload).status_code == 401
    response = client.post(path, json=payload)

    assert response.status_code == 200
    stored = response.json()[0]
    assert stored["reviewer"] == "owner"
    assert stored["review_payload"]["human_verification"]["identity_verified"] is True
    with Session() as session:
        audit = session.query(AuditLog).filter_by(action="mark_extraction_fields_verified").one()
        assert audit.source == "owner_api_token"
        assert audit.payload["actor"] == "owner"


def test_mark_verified_owner_session_ignores_client_reviewer_and_records_server_identity(setup_test_db):
    review_id = create_mock_data(setup_test_db)
    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        review = session.get(ExtractionFieldReview, UUID(review_id))
        paper_id = review.paper_id
        target_id = review.target_id
        write_version = review.write_version

    browser = TestClient(app)
    opened = browser.post("/api/settings/owner-session", json={"token": "owner-secret"})
    assert opened.status_code == 200
    response = browser.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value"],
            "expected_write_version": write_version,
            "reviewer": "client-supplied-human",
        },
    )

    assert response.status_code == 200
    stored = response.json()[0]
    assert stored["reviewer"] == "owner"
    with Session() as session:
        audit = session.query(AuditLog).filter_by(action="mark_extraction_fields_verified").one()
        assert audit.source == "owner_session"
        assert audit.payload["actor"] == "owner"


def test_all_final_verification_entrypoints_reject_anonymous_loopback_before_lookup(setup_test_db):
    missing_id = "00000000-0000-0000-0000-000000000000"
    requests = [
        (
            f"/api/reviews/{missing_id}/promote",
            {"target_status": "verified", "reviewed_value": "sentinel", "confirm_human_review": True},
        ),
        (
            f"/api/extraction/results/{missing_id}/reviews/mark-verified",
            {
                "target_type": "mechanism_claims",
                "target_id": missing_id,
                "field_names": ["claim_text"],
                "reviewer": "human",
            },
        ),
        (
            f"/api/papers/{missing_id}/dft-results/{missing_id}/verify",
            {"confirm_reviewed_against_pdf": True, "field_names": ["value"]},
        ),
        (
            f"/api/workbench/papers/{missing_id}/human-confirm",
            {"confirm_human_review": True, "target_status": "Human_Confirmed"},
        ),
    ]

    anonymous = TestClient(app)
    for path, payload in requests:
        response = anonymous.post(path, json=payload)
        assert response.status_code in {401, 403}, (path, response.status_code, response.text)

    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        assert session.query(AuditLog).count() == 0
        assert session.query(ExtractionFieldReview).count() == 0


def test_promotion_requires_explicit_confirmation(setup_test_db):
    """Requires explicit confirmation to proceed, otherwise 400."""
    engine = setup_test_db
    review_id = create_mock_data(engine)
    
    # Missing confirmation.
    response_no_confirm = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": False
    })
    assert response_no_confirm.status_code == 400
    assert "Explicit confirmation is required" in response_no_confirm.json()["detail"]

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
    assert "locator_not_exact_page:missing_locator" in response.json()["detail"]
    
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
        assert audit.source == "owner_api_token"
        assert audit.payload["actor"] == "owner"
        assert audit.payload["identity_verified"] is True
        assert audit.target_id == review_id
        assert audit.payload["before_state"]["reviewed_value"] is None
        assert audit.payload["after_state"]["reviewed_value"] == 2.0


def test_exact_mechanism_promotion_matches_final_content_object_gate(setup_test_db):
    Session = sessionmaker(bind=setup_test_db)
    with Session() as session:
        paper = Paper(title="Mechanism promotion", pdf_path="paper.pdf", authors=[])
        session.add(paper)
        session.flush()
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="conversion",
            claim_text="Exact claim",
            evidence_text="Exact mechanism evidence.",
        )
        session.add(claim)
        session.flush()
        review = ExtractionFieldReview(
            paper_id=paper.id,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            reviewer_status="pending",
            target_resolution_status="active",
            evidence_text=claim.evidence_text,
        )
        locator = EvidenceLocator(
            paper_id=paper.id,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            evidence_text=claim.evidence_text,
            locator_status="exact_page",
            page=2,
        )
        session.add_all([review, locator])
        session.commit()
        review_id = review.id
        claim_id = claim.id

    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": "Exact claim",
        "confirm_human_review": True,
    })

    assert response.status_code == 200
    with Session() as session:
        claim = session.get(MechanismClaim, claim_id)
        gate = content_object_gate(session, "mechanism_claims", claim)
        assert gate.can_use_for_writing is True
        assert gate.can_use_for_citation is True

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


@pytest.mark.parametrize(
    ("locator_status", "locator_page", "expected_reason"),
    [
        ("approximate", 1, "locator_not_exact_page:approximate"),
        ("text_only", 1, "locator_not_exact_page:text_only"),
        ("exact_page", None, "locator_not_exact_page:missing_page"),
    ],
)
def test_promotion_rejects_non_exact_or_missing_page_locator(
    setup_test_db,
    locator_status,
    locator_page,
    expected_reason,
):
    review_id = create_mock_data(
        setup_test_db,
        locator_status=locator_status,
        locator_page=locator_page,
    )

    response = client.post(f"/api/reviews/{review_id}/promote", json={
        "target_status": "verified",
        "reviewed_value": 2.0,
        "confirm_human_review": True,
    })

    assert response.status_code == 400
    assert expected_reason in response.json()["detail"]
