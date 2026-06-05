from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    Base,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperCitationEligibility,
    PaperImpactMetadata,
)
from app.db.session import get_db_session
from app.main import app
from app.services.impact_metadata_import_service import (
    ImpactMetadataImportService,
    normalize_journal_name,
    parse_impact_metadata_csv,
    parse_impact_metadata_json,
)


@pytest.fixture
def impact_client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "impact_import.db"
        db_url = f"sqlite:///{db_path}"
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
        seed = _seed(Session)
        yield TestClient(app), Session, seed
        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_csv_import_parses_valid_rows():
    text = (Path(__file__).parent / "fixtures" / "impact_metadata_sample.csv").read_text(encoding="utf-8")
    items, invalid = parse_impact_metadata_csv(text)
    assert invalid == []
    assert items[0].journal == "Advanced Energy Materials"
    assert items[0].impact_factor == 24.4
    assert items[0].impact_factor_year == 2024
    assert items[0].impact_factor_source == "user_imported"


def test_json_import_parses_valid_rows():
    items, invalid = parse_impact_metadata_json(
        {
            "source": "user_imported",
            "year": 2024,
            "items": [{"journal": "Advanced Energy Materials", "impact_factor": 24.4}],
        }
    )
    assert invalid == []
    assert items[0].impact_factor_source == "user_imported"
    assert items[0].impact_factor_year == 2024


def test_journal_exact_normalized_match_works(impact_client):
    client, _, seed = impact_client
    assert normalize_journal_name(" Advanced-Energy  Materials. ") == normalize_journal_name("advanced energy materials")

    response = client.post(
        "/api/library/impact-metadata/import",
        content="journal,impact_factor,impact_factor_year,impact_factor_source\nadvanced energy materials,24.4,2024,user_imported\n",
        headers={"content-type": "text/csv"},
    )
    assert response.status_code == 200
    assert response.json()["matched_paper_count"] == 2
    assert response.json()["imported_count"] == 2

    response = client.get("/api/library/papers/filter", params={"needs_metadata": False})
    assert response.status_code == 200
    assert str(seed["advanced_one"]) in _ids(response)
    assert str(seed["advanced_two"]) in _ids(response)


def test_dry_run_counts_pending_matches_as_planned_metadata(impact_client):
    client, _, seed = impact_client
    response = client.post(
        "/api/library/impact-metadata/import?dry_run=true",
        content="journal,impact_factor,impact_factor_year,impact_factor_source\nadvanced energy materials,24.4,2024,user_imported\n",
        headers={"content-type": "text/csv"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["matched_paper_count"] == 2
    assert payload["active_db_write_performed"] is False
    assert payload["needs_metadata_remaining"] == 1

    response = client.get("/api/library/papers/filter", params={"needs_metadata": False})
    assert str(seed["advanced_one"]) not in _ids(response)
    assert str(seed["advanced_two"]) not in _ids(response)


def test_unmatched_journal_returns_unmatched_items(impact_client):
    client, _, _ = impact_client
    response = client.post(
        "/api/library/impact-metadata/import",
        json={
            "source": "user_imported",
            "year": 2024,
            "items": [{"journal": "No Such Journal", "impact_factor": 9.9}],
        },
    )
    assert response.status_code == 200
    assert response.json()["matched_paper_count"] == 0
    assert response.json()["unmatched_items"][0]["journal"] == "No Such Journal"


def test_invalid_impact_factor_returns_invalid_items(impact_client):
    client, Session, _ = impact_client
    response = client.post(
        "/api/library/impact-metadata/import",
        json={"items": [{"journal": "Advanced Energy Materials", "impact_factor": "not-a-number"}]},
    )
    assert response.status_code == 200
    assert response.json()["invalid_items"][0]["reason"].startswith("impact_factor")
    with Session() as session:
        assert session.scalar(select(func.count(PaperImpactMetadata.paper_id))) == 1


def test_missing_impact_factor_does_not_delete_or_exclude_paper(impact_client):
    client, Session, seed = impact_client
    response = client.get("/api/library/papers/filter", params={"needs_metadata": True})
    assert response.status_code == 200
    assert str(seed["missing_if"]) in _ids(response)

    with Session() as session:
        paper = session.get(Paper, seed["missing_if"])
        assert paper is not None
        assert session.get(PaperCitationEligibility, seed["missing_if"]) is None


def test_upsert_does_not_duplicate_rows(impact_client):
    client, Session, seed = impact_client
    payload = {
        "source": "user_imported",
        "year": 2024,
        "items": [{"journal": "Advanced Energy Materials", "impact_factor": 24.4}],
    }
    assert client.post("/api/library/impact-metadata/import", json=payload).status_code == 200
    response = client.post("/api/library/impact-metadata/import", json=payload)
    assert response.status_code == 200
    assert response.json()["imported_count"] == 0
    assert response.json()["updated_count"] == 0
    with Session() as session:
        assert session.scalar(select(func.count(PaperImpactMetadata.paper_id))) == 3
        assert session.get(PaperImpactMetadata, seed["advanced_one"]).impact_factor == 24.4


def test_impact_factor_min_and_max_filters_work_after_import(impact_client):
    client, _, seed = impact_client
    response = client.post(
        "/api/library/impact-metadata/import",
        json={
            "source": "user_imported",
            "year": 2024,
            "items": [{"journal": "Advanced Energy Materials", "impact_factor": 24.4}],
        },
    )
    assert response.status_code == 200

    response = client.get("/api/library/papers/filter", params={"impact_factor_min": 20})
    assert response.status_code == 200
    assert str(seed["advanced_one"]) in _ids(response)
    assert str(seed["low_if"]) not in _ids(response)

    response = client.get("/api/library/papers/filter", params={"impact_factor_max": 5})
    assert response.status_code == 200
    assert _ids(response) == [str(seed["low_if"])]


def test_year_and_source_fields_are_persisted(impact_client):
    client, Session, seed = impact_client
    response = client.post(
        "/api/library/impact-metadata/import",
        json={"items": [{"journal": "Advanced Energy Materials", "impact_factor": 24.4, "impact_factor_year": 2024, "impact_factor_source": "user_imported"}]},
    )
    assert response.status_code == 200
    with Session() as session:
        row = session.get(PaperImpactMetadata, seed["advanced_one"])
        assert row.impact_factor_year == 2024
        assert row.impact_factor_source == "user_imported"


def test_import_does_not_modify_papers_reviews_locators_or_verified_gates(impact_client):
    client, Session, seed = impact_client
    with Session() as session:
        before = _guard_counts(session)
        paper_before = session.get(Paper, seed["advanced_one"])
        paper_identity = (paper_before.title, paper_before.journal, paper_before.year, paper_before.pdf_path)

    response = client.post(
        "/api/library/impact-metadata/import",
        json={"items": [{"journal": "Advanced Energy Materials", "impact_factor": 24.4, "impact_factor_year": 2024}]},
    )
    assert response.status_code == 200

    with Session() as session:
        assert _guard_counts(session) == before
        paper_after = session.get(Paper, seed["advanced_one"])
        assert (paper_after.title, paper_after.journal, paper_after.year, paper_after.pdf_path) == paper_identity


def test_no_online_fetch_or_scrape_path_exists():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "impact_metadata_import_service.py").read_text(encoding="utf-8")
    assert "requests" not in source
    assert "httpx" not in source
    assert "urllib" not in source


def _seed(Session):
    with Session() as session:
        advanced_one = Paper(title="A", doi="10.1000/a", authors=["A. One"], year=2024, journal="Advanced Energy Materials", abstract="", pdf_path="a.pdf")
        advanced_two = Paper(title="B", doi="10.1000/b", authors=["B. Two"], year=2023, journal="Advanced-Energy Materials", abstract="", pdf_path="b.pdf")
        low_if = Paper(title="C", doi="10.1000/c", authors=["C. Low"], year=2022, journal="Archive Journal", abstract="", pdf_path="c.pdf")
        missing_if = Paper(title="D", doi="10.1000/d", authors=["D. Missing"], year=2021, journal="Unknown Journal", abstract="", pdf_path="d.pdf")
        session.add_all([advanced_one, advanced_two, low_if, missing_if])
        session.flush()
        session.add(PaperImpactMetadata(paper_id=low_if.id, impact_factor=2.0, impact_factor_source="manual", impact_factor_year=2022))
        session.add(
            ExtractionFieldReview(
                paper_id=advanced_one.id,
                target_type="manual",
                target_id=str(uuid4()),
                field_name="value",
                original_value=1,
                reviewed_value=1,
                reviewer_status="pending",
                target_resolution_status="active",
            )
        )
        session.add(
            EvidenceLocator(
                paper_id=advanced_one.id,
                source_type="manual",
                evidence_text="kept",
                locator_status="missing",
                locator_confidence=0.0,
                parser_source="manual",
            )
        )
        session.commit()
        return {
            "advanced_one": advanced_one.id,
            "advanced_two": advanced_two.id,
            "low_if": low_if.id,
            "missing_if": missing_if.id,
        }


def _ids(response):
    return [item["id"] for item in response.json()["items"]]


def _guard_counts(session):
    return {
        "papers": session.scalar(select(func.count(Paper.id))),
        "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
        "locators": session.scalar(select(func.count(EvidenceLocator.id))),
        "verified_reviews": session.scalar(
            select(func.count(ExtractionFieldReview.id)).where(
                func.lower(ExtractionFieldReview.reviewer_status) == "verified"
            )
        ),
        "eligibility": session.scalar(select(func.count(PaperCitationEligibility.paper_id))),
        "included_for_writing": session.scalar(
            select(func.count(PaperCitationEligibility.paper_id)).where(
                PaperCitationEligibility.included_for_writing.is_(True)
            )
        ),
    }
