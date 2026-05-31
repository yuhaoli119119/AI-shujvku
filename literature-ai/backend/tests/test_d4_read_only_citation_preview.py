import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, Paper, PaperImpactMetadata, PaperCitationEligibility
from app.db.session import get_db_session
from app.main import app

@pytest.fixture
def client_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "d4_preview.db"
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
        yield TestClient(app), Session
        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories
        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_citation_metadata_preview_normal(client_db):
    client, Session = client_db
    paper_id = uuid4()
    with Session() as db_session:
        paper = Paper(
            id=paper_id,
            title="Test Paper Normal",
            authors=["Author A", "Author B"],
            journal="Journal of Testing",
            year=2023,
            doi="10.1234/test",
            pdf_path="",
        )
        db_session.add(paper)
        
        impact = PaperImpactMetadata(paper_id=paper_id, impact_factor=5.5)
        db_session.add(impact)
        
        eligibility = PaperCitationEligibility(paper_id=paper_id, exclude_from_citation=False)
        db_session.add(eligibility)
        db_session.commit()

    resp = client.get(f"/api/library/papers/{paper_id}/citation-metadata-preview")
    assert resp.status_code == 200
    data = resp.json()

    assert data["paper_id"] == str(paper_id)
    assert data["warning_banner"] == "DRAFT METADATA ONLY - Do not use as final citation"
    assert data["citation_safety_status"] == "eligible_for_draft"
    assert data["evidence_status"] == "metadata_only"
    assert data["safety"]["read_only"] is True
    assert data["safety"]["modifies_db"] is False
    
    # Check BibTeX and CSL JSON
    assert "draft_" in data["bibtex_draft"]
    assert "DRAFT METADATA ONLY" in data["bibtex_draft"]
    assert "Author A and Author B" in data["bibtex_draft"]
    
    assert data["csl_json_draft"]["note"] == "DRAFT METADATA ONLY"
    
    # Check missing warnings (volume, issue, pages, publisher are expected missing)
    warnings = data["missing_metadata_warnings"]
    assert len(warnings) == 4
    assert "Missing volume" in warnings
    assert "Missing issue" in warnings
    assert "Missing pages" in warnings
    assert "Missing publisher" in warnings
    assert "Missing title" not in warnings
    assert "Missing impact factor" not in warnings


def test_citation_metadata_preview_missing_metadata(client_db):
    client, Session = client_db
    paper_id = uuid4()
    with Session() as db_session:
        paper = Paper(id=paper_id, pdf_path="")
        db_session.add(paper)
        db_session.commit()

    resp = client.get(f"/api/library/papers/{paper_id}/citation-metadata-preview")
    assert resp.status_code == 200
    data = resp.json()

    warnings = data["missing_metadata_warnings"]
    assert "Missing title" in warnings
    assert "Missing authors" in warnings
    assert "Missing year" in warnings
    assert "Missing DOI" in warnings
    assert "Missing journal" in warnings
    assert "Missing impact factor" in warnings


def test_citation_metadata_preview_excluded_status(client_db):
    client, Session = client_db
    paper_id = uuid4()
    with Session() as db_session:
        paper = Paper(id=paper_id, title="Bad Paper", pdf_path="")
        db_session.add(paper)
        
        eligibility = PaperCitationEligibility(paper_id=paper_id, exclude_from_citation=True)
        db_session.add(eligibility)
        db_session.commit()

    resp = client.get(f"/api/library/papers/{paper_id}/citation-metadata-preview")
    assert resp.status_code == 200
    data = resp.json()

    assert data["citation_safety_status"] == "excluded"


def test_citation_metadata_preview_not_found(client_db):
    client, Session = client_db
    resp = client.get(f"/api/library/papers/{uuid4()}/citation-metadata-preview")
    assert resp.status_code == 404


