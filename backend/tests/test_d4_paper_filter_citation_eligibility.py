from __future__ import annotations

import os

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    Base,
    DFTResult,
    EvidenceClaim,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
    PaperSection,
)
from app.db.session import get_db_session
from app.main import app


@pytest.fixture
def d4_client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(Path(tmpdir) / "storage"))
        get_settings.cache_clear()
        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

        def override_get_db_session():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        seed = _seed_fixture(Session)
        yield TestClient(app), Session, seed
        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_filter_by_year_journal_and_impact_factor(d4_client):
    client, _, seed = d4_client

    response = client.get("/api/library/papers/filter", params={"year_min": 2021, "year_max": 2024})
    assert response.status_code == 200
    titles = _titles(response)
    assert "Recent High IF" in titles
    assert "Old Paper" not in titles

    response = client.get("/api/library/papers/filter", params={"journal_includes": "Energy"})
    assert response.status_code == 200
    assert _titles(response) == ["Recent High IF"]

    response = client.get("/api/library/papers/filter", params={"impact_factor_min": 10})
    assert response.status_code == 200
    assert _ids(response) == [str(seed["high_id"])]


def test_missing_impact_factor_is_not_deleted_and_can_be_screened_as_needs_metadata(d4_client):
    client, Session, seed = d4_client

    response = client.get("/api/library/papers/filter", params={"impact_factor_min": 5})
    assert response.status_code == 200
    assert str(seed["missing_if_id"]) not in _ids(response)

    response = client.get("/api/library/papers/filter", params={"needs_metadata": True})
    assert response.status_code == 200
    assert str(seed["missing_if_id"]) in _ids(response)
    assert response.json()["items"][0]["impact_factor_status"] == "needs_metadata"

    with Session() as session:
        assert session.scalar(select(func.count(Paper.id))) == 5
        assert session.get(Paper, seed["missing_if_id"]) is not None


def test_excluded_papers_do_not_enter_default_candidate_set_and_can_be_requested(d4_client):
    client, _, seed = d4_client

    response = client.get("/api/library/papers/filter")
    assert response.status_code == 200
    assert str(seed["excluded_id"]) not in _ids(response)

    response = client.get("/api/library/papers/filter", params={"exclude_from_citation": True})
    assert response.status_code == 200
    assert _ids(response) == [str(seed["excluded_id"])]


def test_high_priority_papers_are_sorted_first(d4_client):
    client, _, seed = d4_client

    response = client.get("/api/library/papers/filter")
    assert response.status_code == 200
    ids = _ids(response)
    assert ids[0] == str(seed["high_id"])

    response = client.get("/api/library/papers/filter", params={"citation_priority": "high"})
    assert response.status_code == 200
    assert _ids(response) == [str(seed["high_id"])]


def test_bulk_exclude_only_updates_citation_eligibility_and_keeps_papers(d4_client):
    client, Session, seed = d4_client

    response = client.post(
        "/api/library/papers/citation-eligibility/bulk",
        json={
            "paper_ids": [str(seed["low_id"]), str(seed["missing_if_id"])],
            "exclude_from_citation": True,
            "exclude_reason": "outside scope",
            "citation_priority": "exclude",
            "user_note": "keep record",
        },
    )
    assert response.status_code == 200
    assert response.json()["updated_count"] == 2

    with Session() as session:
        assert session.scalar(select(func.count(Paper.id))) == 5
        rows = session.scalars(select(PaperCitationEligibility)).all()
        changed = {row.paper_id: row for row in rows}
        assert changed[seed["low_id"]].exclude_from_citation is True
        assert changed[seed["missing_if_id"]].citation_priority == "exclude"
        assert session.get(Paper, seed["low_id"]) is not None


def test_filter_does_not_modify_database(d4_client):
    client, Session, _ = d4_client
    with Session() as session:
        before = _counts(session)

    response = client.get("/api/library/papers/filter", params={"keyword": "catalyst", "has_pdf": True})
    assert response.status_code == 200

    with Session() as session:
        assert _counts(session) == before


def test_eligibility_write_does_not_modify_verified_review_extraction_or_materialized_facts(d4_client):
    client, Session, seed = d4_client
    with Session() as session:
        review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == seed["safe_id"]))
        result = session.scalar(select(DFTResult).where(DFTResult.paper_id == seed["safe_id"]))
        candidate = session.scalar(select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == seed["safe_id"]))
        before = (review.reviewer_status, review.target_resolution_status, result.value, candidate.status, candidate.materialized_target_id)

    response = client.post(
        f"/api/library/papers/{seed['safe_id']}/citation-eligibility",
        json={"exclude_from_citation": True, "exclude_reason": "not relevant", "citation_priority": "exclude"},
    )
    assert response.status_code == 200

    with Session() as session:
        review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == seed["safe_id"]))
        result = session.scalar(select(DFTResult).where(DFTResult.paper_id == seed["safe_id"]))
        candidate = session.scalar(select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == seed["safe_id"]))
        after = (review.reviewer_status, review.target_resolution_status, result.value, candidate.status, candidate.materialized_target_id)
        assert after == before


def test_nonexistent_paper_id_returns_clear_error(d4_client):
    client, _, _ = d4_client
    missing = uuid4()
    response = client.post(
        f"/api/library/papers/{missing}/citation-eligibility",
        json={"exclude_from_citation": True},
    )
    assert response.status_code == 404
    assert "Paper not found" in response.json()["detail"]


def test_asset_and_evidence_filters_work_without_forging_safe_verified(d4_client):
    client, _, seed = d4_client

    response = client.get("/api/library/papers/filter", params={"has_pdf": True, "has_parsed_text": True})
    assert response.status_code == 200
    assert str(seed["high_id"]) in _ids(response)
    assert str(seed["low_id"]) not in _ids(response)

    response = client.get("/api/library/papers/filter", params={"has_extraction_output": True})
    assert response.status_code == 200
    assert str(seed["safe_id"]) in _ids(response)

    response = client.get("/api/library/papers/filter", params={"has_verified_evidence": True})
    assert response.status_code == 200
    assert str(seed["verified_only_id"]) in _ids(response)

    response = client.get("/api/library/papers/filter", params={"has_safe_verified_evidence": True})
    assert response.status_code == 200
    ids = _ids(response)
    assert str(seed["safe_id"]) in ids
    assert str(seed["verified_only_id"]) not in ids


def test_mark_not_cite_keeps_paper_in_database(d4_client):
    client, Session, seed = d4_client

    response = client.post(
        f"/api/library/papers/{seed['missing_if_id']}/citation-eligibility",
        json={
            "included_for_writing": False,
            "exclude_from_citation": True,
            "exclude_reason": "missing impact factor",
            "user_note": "ask user later",
        },
    )
    assert response.status_code == 200
    assert response.json()["exclude_from_citation"] is True

    with Session() as session:
        paper = session.get(Paper, seed["missing_if_id"])
        assert paper is not None
        row = session.get(PaperCitationEligibility, seed["missing_if_id"])
        assert row.exclude_reason == "missing impact factor"


def _seed_fixture(Session):
    with Session() as session:
        high = Paper(
            title="Recent High IF",
            year=2023,
            journal="Energy Materials",
            abstract="Catalyst paper with parsed text",
            pdf_path="high.pdf",
            markdown_path="high.md",
        )
        low = Paper(title="Old Paper", year=2018, journal="Archive Journal", abstract="Old catalyst", pdf_path="")
        missing_if = Paper(title="Unknown Impact", year=2022, journal="Manual Review", abstract="Needs metadata", pdf_path="unknown.pdf")
        excluded = Paper(title="Excluded Candidate", year=2024, journal="Energy Materials", abstract="Exclude me", pdf_path="excluded.pdf")
        verified_only = Paper(title="Verified But Unsafe", year=2024, journal="Claims Journal", abstract="Has verified claim", pdf_path="claim.pdf")
        session.add_all([high, low, missing_if, excluded, verified_only])
        session.flush()
        session.add_all(
            [
                PaperImpactMetadata(
                    paper_id=high.id,
                    impact_factor=12.5,
                    impact_factor_source="manual",
                    impact_factor_year=2023,
                ),
                PaperImpactMetadata(
                    paper_id=low.id,
                    impact_factor=2.1,
                    impact_factor_source="manual",
                    impact_factor_year=2018,
                ),
                PaperCitationEligibility(paper_id=high.id, citation_priority="high"),
                PaperCitationEligibility(
                    paper_id=excluded.id,
                    included_for_writing=False,
                    exclude_from_citation=True,
                    exclude_reason="scope mismatch",
                    citation_priority="exclude",
                ),
                PaperSection(paper_id=high.id, section_title="Intro", section_type="body", text="Parsed"),
                EvidenceClaim(
                    paper_id=verified_only.id,
                    claim_text="supported claim",
                    evidence_text="verified text",
                    validation_status="verified",
                ),
            ]
        )
        result = DFTResult(
            paper_id=high.id,
            property_type="binding_energy",
            value=-1.2,
            unit="eV",
            evidence_text="DFT evidence",
        )
        session.add(result)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=high.id,
                target_type="dft_results",
                target_id=str(result.id),
                field_name="value",
                original_value=-1.2,
                reviewed_value=-1.2,
                evidence_text="DFT evidence",
                reviewer_status="verified",
                target_resolution_status="active",
            )
        )
        run = ExternalAnalysisRun(paper_id=high.id, source="manual", mapping_status="completed")
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=high.id,
                candidate_type="dft_result",
                status="materialized",
                materialized_target_type="dft_results",
                materialized_target_id=str(result.id),
            )
        )
        session.commit()
        return {
            "high_id": high.id,
            "safe_id": high.id,
            "low_id": low.id,
            "missing_if_id": missing_if.id,
            "excluded_id": excluded.id,
            "verified_only_id": verified_only.id,
        }


def _titles(response):
    return [item["title"] for item in response.json()["items"]]


def _ids(response):
    return [item["id"] for item in response.json()["items"]]


def _counts(session):
    return {
        "papers": session.scalar(select(func.count(Paper.id))),
        "eligibility": session.scalar(select(func.count(PaperCitationEligibility.paper_id))),
        "impact": session.scalar(select(func.count(PaperImpactMetadata.paper_id))),
        "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
        "dft": session.scalar(select(func.count(DFTResult.id))),
    }
