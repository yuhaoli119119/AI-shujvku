import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, DFTResult, Paper
from app.db.session import get_db_session
from app.main import app


@pytest.fixture
def setup_test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_extraction_reviews.db"
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


def test_save_extraction_field_review(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Reviewable Paper", pdf_path="reviewable.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            evidence_text="The adsorption energy of Li2S4 is -1.23 eV.",
            confidence=0.88,
        )
        session.add(result)
        session.commit()
        paper_id = str(paper.id)
        target_id = str(result.id)

    client = TestClient(app)
    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "value",
                    "reviewed_value": -1.2,
                    "reviewer_status": "corrected",
                    "reviewer": "alice",
                    "reviewer_note": "Rounded to the reported significant digits.",
                }
            ]
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["target_type"] == "dft_results"
    assert data[0]["field_name"] == "value"
    assert data[0]["original_value"] == -1.23
    assert data[0]["reviewed_value"] == -1.2
    assert data[0]["reviewer_status"] == "corrected"

    list_response = client.get(f"/api/extraction/results/{paper_id}/reviews")
    assert list_response.status_code == 200
    reviews = list_response.json()
    assert len(reviews) == 1
    assert reviews[0]["reviewer"] == "alice"


def test_mark_verified_persists_verified_review(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Verify Paper", pdf_path="verify.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S6",
            property_type="adsorption_energy",
            value=-0.91,
            unit="eV",
            evidence_text="The adsorption energy of Li2S6 is -0.91 eV.",
            confidence=0.8,
        )
        session.add(result)
        session.commit()
        paper_id = str(paper.id)
        target_id = str(result.id)

    client = TestClient(app)
    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value"],
            "reviewer": "bob",
            "reviewer_note": "Source PDF double-checked.",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data[0]["reviewer_status"] == "verified"
    assert data[0]["verified"] is True
    assert data[0]["reviewed_value"] == -0.91


def test_validate_returns_review_state(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Validate Paper", pdf_path="validate.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.11,
            unit="eV",
            evidence_text="The adsorption energy is -1.11 eV.",
            confidence=0.92,
        )
        session.add(result)
        session.commit()
        paper_id = str(paper.id)
        target_id = str(result.id)

    client = TestClient(app)
    save_response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "value",
                    "reviewed_value": -1.11,
                    "reviewer_status": "verified",
                    "reviewer": "carol",
                }
            ]
        },
    )
    assert save_response.status_code == 200

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["target_id"] == target_id
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is True
    assert payload["results"]["DFTResult"][0]["value"]["review"]["reviewer_status"] == "verified"
    assert payload["field_reviews"][0]["target_id"] == target_id


def test_extraction_review_api_returns_errors_for_missing_paper_or_target(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Missing Target Paper", pdf_path="missing.pdf", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)

    missing_paper_response = client.get(f"/api/extraction/results/{uuid4()}/reviews")
    assert missing_paper_response.status_code == 404
    assert missing_paper_response.json()["detail"] == "Paper not found"

    missing_target_response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": str(uuid4()),
                    "field_name": "value",
                    "reviewed_value": -0.5,
                }
            ]
        },
    )
    assert missing_target_response.status_code == 404
    assert "Target not found" in missing_target_response.json()["detail"]
