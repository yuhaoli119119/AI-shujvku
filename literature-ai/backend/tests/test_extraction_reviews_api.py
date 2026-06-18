import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.models import Base, DFTResult, EvidenceSpan, ExtractionFieldReview, Paper
from app.db.session import get_db_session
from app.main import app
from app.services.extraction_pipeline import ExtractionPipelineService


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
    assert data[0]["target_fingerprint"]
    assert data[0]["target_label"] == "adsorption_energy / Li2S4 / -1.23 eV"
    assert data[0]["field_path"] == "dft_results.value.value"
    assert data[0]["target_resolution_status"] == "active"
    assert data[0]["last_resolved_target_id"] == target_id

    list_response = client.get(f"/api/extraction/results/{paper_id}/reviews")
    assert list_response.status_code == 200
    reviews = list_response.json()
    assert len(reviews) == 1
    assert reviews[0]["reviewer"] == "alice"


def test_save_extraction_field_review_rejects_stale_expected_write_version(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Stale Save Paper", pdf_path="stale-save.pdf", authors=[])
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
        paper_uuid = paper.id
        target_id = str(result.id)

    client = TestClient(app)
    first = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "value",
                    "reviewed_value": -1.2,
                    "reviewer_status": "corrected",
                    "reviewer": "ai-1",
                    "expected_write_version": 1,
                }
            ]
        },
    )
    assert first.status_code == 200
    before = first.json()[0]
    stale_version = first.json()[0]["write_version"] - 1

    second = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "value",
                    "reviewed_value": -9.99,
                    "reviewer_status": "corrected",
                    "reviewer": "ai-2",
                    "expected_write_version": stale_version,
                }
            ]
        },
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "write_conflict:extraction_review_version_stale"

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(paper_id=paper_uuid).one()
        assert review.reviewed_value == -1.2
        assert review.reviewer == "ai-1"


def test_existing_review_rejects_missing_expected_write_version_without_mutation(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Missing Version Save", pdf_path="missing-version-save.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            evidence_text="The adsorption energy is -1.23 eV.",
        )
        session.add(result)
        session.commit()
        paper_id, paper_uuid, target_id = str(paper.id), paper.id, str(result.id)

    client = TestClient(app)
    first = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [{
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "value",
            "reviewed_value": -1.2,
            "reviewer_status": "corrected",
            "reviewer": "ai-1",
        }]},
    )
    assert first.status_code == 200
    before = first.json()[0]

    missing = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [{
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "value",
            "reviewed_value": -9.99,
            "reviewer_status": "corrected",
            "reviewer": "ai-2",
        }]},
    )
    assert missing.status_code == 409
    assert missing.json()["detail"] == "write_conflict:extraction_review_version_required"

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(paper_id=paper_uuid).one()
        assert review.reviewed_value == before["reviewed_value"]
        assert review.reviewer == before["reviewer"]
        assert review.reviewer_status == before["reviewer_status"]
        assert review.write_version == before["write_version"]


def test_first_review_creation_allows_missing_version_and_returns_version(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="First Review", pdf_path="first-review.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S6",
            property_type="adsorption_energy",
            value=-0.91,
            unit="eV",
            evidence_text="The adsorption energy is -0.91 eV.",
        )
        session.add(result)
        session.commit()
        paper_id, target_id = str(paper.id), str(result.id)

    response = TestClient(app).post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [{
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "value",
            "reviewed_value": -0.9,
            "reviewer_status": "corrected",
            "reviewer": "ai-1",
        }]},
    )
    assert response.status_code == 200
    assert response.json()[0]["write_version"] >= 1


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
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text or "The adsorption energy of Li2S6 is -0.91 eV.",
        ))
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
    assert data[0]["target_resolution_status"] == "active"


def test_mark_verified_rejects_stale_expected_write_version(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Stale Verify Paper", pdf_path="stale-verify.pdf", authors=[])
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
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text or "The adsorption energy of Li2S6 is -0.91 eV.",
        ))
        session.commit()
        paper_id = str(paper.id)
        paper_uuid = paper.id
        target_id = str(result.id)

    client = TestClient(app)
    first = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": target_id,
                    "field_name": "value",
                    "reviewed_value": -0.9,
                    "reviewer_status": "corrected",
                    "reviewer": "ai-1",
                    "expected_write_version": 1,
                }
            ]
        },
    )
    assert first.status_code == 200
    before = first.json()[0]
    stale_version = first.json()[0]["write_version"] - 1

    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value"],
            "reviewer": "ai-2",
            "expected_write_version": stale_version,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "write_conflict:extraction_review_version_stale"

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(paper_id=paper_uuid).one()
        assert review.reviewed_value == before["reviewed_value"]
        assert review.reviewer_status == "corrected"
        assert review.reviewer == "ai-1"
        assert review.write_version == before["write_version"]


def test_mark_verified_rejects_missing_version_without_mutation(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Missing Verify Version", pdf_path="missing-verify.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S6",
            property_type="adsorption_energy",
            value=-0.91,
            unit="eV",
            evidence_text="The adsorption energy is -0.91 eV.",
        )
        session.add(result)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text,
        ))
        session.commit()
        paper_id, paper_uuid, target_id = str(paper.id), paper.id, str(result.id)

    client = TestClient(app)
    first = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [{
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "value",
            "reviewed_value": -0.9,
            "reviewer_status": "corrected",
            "reviewer": "ai-1",
        }]},
    )
    assert first.status_code == 200
    before = first.json()[0]

    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value"],
            "reviewer": "ai-2",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "write_conflict:extraction_review_version_required"

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(paper_id=paper_uuid).one()
        assert review.reviewed_value == before["reviewed_value"]
        assert review.reviewer == before["reviewer"]
        assert review.reviewer_status == before["reviewer_status"]
        assert review.write_version == before["write_version"]


def test_mark_verified_accepts_distinct_per_field_versions(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Per Field Versions", pdf_path="per-field.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S8",
            property_type="adsorption_energy",
            value=-0.75,
            unit="eV",
            evidence_text="The adsorption energy is -0.75 eV.",
        )
        session.add(result)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text,
        ))
        session.commit()
        paper_id, target_id = str(paper.id), str(result.id)

    client = TestClient(app)
    created = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [
            {"target_type": "dft_results", "target_id": target_id, "field_name": "value", "reviewed_value": -0.74, "reviewer_status": "corrected", "reviewer": "ai-1"},
            {"target_type": "dft_results", "target_id": target_id, "field_name": "adsorbate", "reviewed_value": "Li2S8", "reviewer_status": "corrected", "reviewer": "ai-1"},
        ]},
    )
    assert created.status_code == 200
    versions = {row["field_name"]: row["write_version"] for row in created.json()}
    updated = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [{
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": "value",
            "reviewed_value": -0.73,
            "reviewer_status": "corrected",
            "reviewer": "ai-2",
            "expected_write_version": versions["value"],
        }]},
    )
    assert updated.status_code == 200
    versions["value"] = updated.json()[0]["write_version"]
    assert versions["value"] != versions["adsorbate"]

    verified = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value", "adsorbate"],
            "expected_write_versions": versions,
            "reviewer": "human",
        },
    )
    assert verified.status_code == 200
    assert {row["reviewer_status"] for row in verified.json()} == {"verified"}


def test_mark_verified_stale_field_rolls_back_entire_batch(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Atomic Verify Batch", pdf_path="atomic-verify.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S8",
            property_type="adsorption_energy",
            value=-0.75,
            unit="eV",
            evidence_text="The adsorption energy is -0.75 eV.",
        )
        session.add(result)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text,
        ))
        session.commit()
        paper_id, paper_uuid, target_id = str(paper.id), paper.id, str(result.id)

    client = TestClient(app)
    created = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={"reviews": [
            {"target_type": "dft_results", "target_id": target_id, "field_name": "value", "reviewed_value": -0.74, "reviewer_status": "corrected", "reviewer": "ai-1"},
            {"target_type": "dft_results", "target_id": target_id, "field_name": "adsorbate", "reviewed_value": "Li2S8", "reviewer_status": "corrected", "reviewer": "ai-1"},
        ]},
    )
    assert created.status_code == 200
    versions = {row["field_name"]: row["write_version"] for row in created.json()}

    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value", "adsorbate"],
            "expected_write_versions": {"value": versions["value"], "adsorbate": versions["adsorbate"] - 1},
            "reviewer": "human",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "write_conflict:extraction_review_version_stale"

    with Session() as session:
        reviews = session.query(ExtractionFieldReview).filter_by(paper_id=paper_uuid).all()
        assert {row.reviewer_status for row in reviews} == {"corrected"}
        assert {row.reviewer for row in reviews} == {"ai-1"}
        assert {row.field_name: row.write_version for row in reviews} == versions


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
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text or "The adsorption energy is -1.11 eV.",
        ))
        session.commit()
        paper_id = str(paper.id)
        target_id = str(result.id)

    client = TestClient(app)
    # Use mark-verified API instead of save with verified status (D1 Phase 3 boundary)
    save_response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "field_names": ["value"],
            "reviewer": "carol",
        },
    )
    assert save_response.status_code == 200

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["target_id"] == target_id
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is True
    assert payload["results"]["DFTResult"][0]["value"]["review"]["reviewer_status"] == "verified"
    assert payload["results"]["DFTResult"][0]["value"]["review"]["target_resolution_status"] == "active"
    assert payload["results"]["DFTResult"][0]["value"]["evidence_locator"]["locator_status"] in {"missing_page", "text_only"}
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


def test_replace_stage2_remaps_review_when_semantics_are_unchanged(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Remap Paper", pdf_path="remap.pdf", authors=[])
        session.add(paper)
        session.flush()
        original = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="adsorption",
            source_section="Results",
            evidence_text="The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
            confidence=0.9,
        )
        session.add(original)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(original.id),
            text=original.evidence_text or "The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
        ))
        session.commit()
        paper_id = str(paper.id)
        paper_uuid = paper.id
        old_target_id = str(original.id)

    client = TestClient(app)
    # First save as corrected, then mark verified (D1 Phase 3 boundary)
    save_response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": old_target_id,
                    "field_name": "value",
                    "reviewed_value": -1.2,
                    "reviewer_status": "corrected",
                    "reviewer": "alice",
                    "reviewer_note": "Confirmed against the PDF.",
                }
            ]
        },
    )
    assert save_response.status_code == 200
    saved_version = save_response.json()[0]["write_version"]
    verify_response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": old_target_id,
            "field_names": ["value"],
            "expected_write_versions": {"value": saved_version},
            "reviewer": "alice",
            "reviewer_note": "Confirmed against the PDF.",
        },
    )
    assert verify_response.status_code == 200

    with Session() as session:
        paper = session.get(Paper, paper_uuid)
        assert paper is not None
        service = ExtractionPipelineService(session, Settings(storage_root=Path(".")))

        def fake_run_stage2(_paper, _document):
            replacement = DFTResult(
                paper_id=_paper.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                reaction_step="adsorption",
                source_section="Results",
                evidence_text="The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                confidence=0.77,
            )
            session.add(replacement)
            session.flush()
            return {"dft_results": 1}

        service.run_stage2 = fake_run_stage2  # type: ignore[method-assign]
        service.replace_stage2(paper, SimpleNamespace())
        session.commit()

        review = session.query(ExtractionFieldReview).filter(ExtractionFieldReview.paper_id == paper.id).one()
        new_target_id = session.query(DFTResult).filter(DFTResult.paper_id == paper.id).one().id
        assert review.target_id == str(new_target_id)
        assert review.target_resolution_status == "remapped"
        assert review.remapped_from_target_id == old_target_id
        assert review.last_resolved_target_id == str(new_target_id)
        assert review.reviewer_status == "verified"
        assert review.reviewed_value == -1.2
        assert review.reviewer_note == "Confirmed against the PDF."

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is True
    assert payload["results"]["DFTResult"][0]["value"]["review"]["target_resolution_status"] == "remapped"


def test_replace_stage2_marks_stale_review_and_does_not_apply_it(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Stale Paper", pdf_path="stale.pdf", authors=[])
        session.add(paper)
        session.flush()
        original = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            source_section="Results",
            evidence_text="The adsorption energy of Li2S4 is -1.23 eV.",
            confidence=0.9,
        )
        session.add(original)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(original.id),
            text=original.evidence_text or "The adsorption energy of Li2S4 is -1.23 eV.",
        ))
        session.commit()
        paper_id = str(paper.id)
        paper_uuid = paper.id
        old_target_id = str(original.id)

    client = TestClient(app)
    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": old_target_id,
            "field_names": ["value"],
            "reviewer": "bob",
            "reviewer_note": "Verified old result.",
        },
    )
    assert response.status_code == 200

    with Session() as session:
        paper = session.get(Paper, paper_uuid)
        assert paper is not None
        service = ExtractionPipelineService(session, Settings(storage_root=Path(".")))

        def fake_run_stage2(_paper, _document):
            replacement = DFTResult(
                paper_id=_paper.id,
                adsorbate="Li2S8",
                property_type="reaction_barrier",
                value=0.42,
                unit="eV",
                source_section="Discussion",
                evidence_text="The reaction barrier of Li2S8 is 0.42 eV.",
                confidence=0.8,
            )
            session.add(replacement)
            session.flush()
            return {"dft_results": 1}

        service.run_stage2 = fake_run_stage2  # type: ignore[method-assign]
        service.replace_stage2(paper, SimpleNamespace())
        session.commit()

        review = session.query(ExtractionFieldReview).filter(ExtractionFieldReview.paper_id == paper.id).one()
        assert review.target_id == old_target_id
        assert review.target_resolution_status == "stale"
        assert review.reviewed_value == -1.23
        assert review.reviewer_note == "Verified old result."

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is False
    assert any(warning["code"] == "review_target_stale" for warning in payload["validation_warnings"])
    assert any(
        warning["code"] == "review_target_stale" and warning["value"]["review_resolution_status"] == "stale"
        for warning in payload["validation_warnings"]
    )


def test_replace_stage2_marks_ambiguous_review_and_audit_reports_counts(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Ambiguous Paper", pdf_path="ambiguous.pdf", authors=[])
        session.add(paper)
        session.flush()
        original = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="adsorption",
            source_section="Results",
            evidence_text="The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
            confidence=0.9,
        )
        session.add(original)
        session.commit()
        paper_id = str(paper.id)
        paper_uuid = paper.id
        old_target_id = str(original.id)

    client = TestClient(app)
    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/save",
        json={
            "reviews": [
                {
                    "target_type": "dft_results",
                    "target_id": old_target_id,
                    "field_name": "value",
                    "reviewed_value": -1.21,
                    "reviewer_status": "corrected",
                    "reviewer": "eve",
                }
            ]
        },
    )
    assert response.status_code == 200

    with Session() as session:
        paper = session.get(Paper, paper_uuid)
        assert paper is not None
        service = ExtractionPipelineService(session, Settings(storage_root=Path(".")))

        def fake_run_stage2(_paper, _document):
            for _ in range(2):
                session.add(
                    DFTResult(
                        paper_id=_paper.id,
                        adsorbate="Li2S4",
                        property_type="adsorption_energy",
                        value=-1.23,
                        unit="eV",
                        reaction_step="adsorption",
                        source_section="Results",
                        evidence_text="The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                        confidence=0.8,
                    )
                )
            session.flush()
            return {"dft_results": 2}

        service.run_stage2 = fake_run_stage2  # type: ignore[method-assign]
        service.replace_stage2(paper, SimpleNamespace())
        session.commit()

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is False
    assert payload["results"]["DFTResult"][1]["value"]["verified"] is False
    assert any(
        warning["value"]["review_resolution_status"] == "ambiguous"
        for warning in payload["validation_warnings"]
        if warning["code"] == "review_target_stale"
    )

    audit_response = client.get(f"/api/extraction/results/{paper_id}/reviews/audit")
    assert audit_response.status_code == 200
    audit = audit_response.json()
    assert audit["total_reviews"] == 1
    assert audit["ambiguous"] == 1
    assert audit["active"] == 0


def test_validate_reports_locator_warning_without_overriding_stale_review_state(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Locator Warning Paper", pdf_path="locator-warning.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.05,
            unit="eV",
            source_section="Results",
            evidence_text="The adsorption energy of Li2S4 is -1.05 eV.",
            confidence=0.85,
        )
        session.add(result)
        session.flush()
        session.add(EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(result.id),
            text=result.evidence_text or "The adsorption energy of Li2S4 is -1.05 eV.",
        ))
        session.commit()
        paper_id = str(paper.id)
        paper_uuid = paper.id
        old_target_id = str(result.id)

    client = TestClient(app)
    response = client.post(
        f"/api/extraction/results/{paper_id}/reviews/mark-verified",
        json={
            "target_type": "dft_results",
            "target_id": old_target_id,
            "field_names": ["value"],
            "reviewer": "dana",
            "reviewer_note": "Verified before re-run.",
        },
    )
    assert response.status_code == 200

    with Session() as session:
        paper = session.get(Paper, paper_uuid)
        assert paper is not None
        service = ExtractionPipelineService(session, Settings(storage_root=Path(".")))

        def fake_run_stage2(_paper, _document):
            replacement = DFTResult(
                paper_id=_paper.id,
                adsorbate="Li2S8",
                property_type="reaction_barrier",
                value=0.51,
                unit="eV",
                source_section="Discussion",
                evidence_text="The reaction barrier of Li2S8 is 0.51 eV.",
                confidence=0.83,
            )
            session.add(replacement)
            session.flush()
            return {"dft_results": 1}

        service.run_stage2 = fake_run_stage2  # type: ignore[method-assign]
        service.replace_stage2(paper, SimpleNamespace())
        session.commit()

    validate_response = client.post(f"/api/extraction/results/{paper_id}/validate")
    assert validate_response.status_code == 200
    payload = validate_response.json()
    assert payload["results"]["DFTResult"][0]["value"]["verified"] is False
    assert any(warning["code"] == "review_target_stale" for warning in payload["validation_warnings"])
    assert any(warning["code"] == "evidence_locator_missing_page" for warning in payload["validation_warnings"])


def test_multi_ai_audit_keeps_history_and_marks_conflict(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        paper = Paper(title="Multi AI Paper", pdf_path="multi-ai.pdf", authors=[])
        session.add(paper)
        session.flush()
        result = DFTResult(
            paper_id=paper.id,
            adsorbate="H2O",
            property_type="adsorption_energy",
            value=-0.1,
            unit="eV",
            evidence_text="The adsorption energy of H2O is -0.1 eV.",
            confidence=0.88,
        )
        session.add(result)
        session.flush()
        session.add(
            EvidenceSpan(
                paper_id=paper.id,
                object_type="dft_results",
                object_id=str(result.id),
                text=result.evidence_text or "",
                page=2,
            )
        )
        session.commit()
        paper_uuid = paper.id
        paper_id = str(paper.id)
        target_id = str(result.id)

    client = TestClient(app)
    first = client.post(
        f"/api/workbench/papers/{paper_id}/gemini-audit",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "decision": "PASS",
            "reviewer": "gemini_image_auditor",
            "agent_role": "gemini_image_auditor",
            "model_name": "gemini-test",
            "field_names": ["value"],
            "reviewer_note": "Image evidence looks aligned.",
        },
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/workbench/papers/{paper_id}/gemini-audit",
        json={
            "target_type": "dft_results",
            "target_id": target_id,
            "decision": "FLAG",
            "reviewer": "glm_dft_auditor",
            "agent_role": "glm_dft_auditor",
            "model_name": "glm-5.1-test",
            "field_names": ["value"],
            "reviewer_note": "DFT value is not supported by the cited sentence.",
        },
    )
    assert second.status_code == 200

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(
            paper_id=paper_uuid,
            target_type="dft_results",
            target_id=target_id,
            field_name="value",
        ).one()
        assert review.reviewer_status == "review_conflict"
        payload = review.review_payload or {}
        assert payload["review_conflict"] is True
        assert len(payload["ai_audits"]) == 2
        assert payload["ai_audits"][0]["protocol"]["sha256"]
