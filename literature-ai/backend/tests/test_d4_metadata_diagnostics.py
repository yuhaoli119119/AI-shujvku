import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, Paper, PaperImpactMetadata
from app.db.session import get_db_session
from app.main import app

@pytest.fixture
def client_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "d4_diagnostics.db"
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


def test_metadata_diagnostics_all_complete(client_db):
    client, Session = client_db
    paper_id = uuid4()
    with Session() as db_session:
        paper = Paper(
            id=paper_id,
            title="Complete Paper",
            authors=["Author Complete"],
            journal="Nature",
            year=2024,
            doi="10.1000/complete",
            pdf_path=""
        )
        db_session.add(paper)
        impact = PaperImpactMetadata(paper_id=paper_id, impact_factor=50.0)
        db_session.add(impact)
        db_session.commit()

    resp = client.get("/api/library/papers/metadata-diagnostics")
    assert resp.status_code == 200
    data = resp.json()

    item = next((i for i in data["items"] if i["paper_id"] == str(paper_id)), None)
    assert item is not None
    missing = item["missing_fields"]
    assert "title" not in missing
    assert "authors" not in missing
    assert "journal" not in missing
    assert "year" not in missing
    assert "DOI" not in missing
    assert "impact factor" not in missing
    assert "volume" in missing


def test_metadata_diagnostics_missing_standard_fields(client_db):
    client, Session = client_db
    paper_id = uuid4()
    with Session() as db_session:
        paper = Paper(id=paper_id, pdf_path="")
        db_session.add(paper)
        db_session.commit()

    resp = client.get("/api/library/papers/metadata-diagnostics")
    assert resp.status_code == 200
    data = resp.json()

    item = next((i for i in data["items"] if i["paper_id"] == str(paper_id)), None)
    assert item is not None
    missing = item["missing_fields"]
    assert "title" in missing
    assert "authors" in missing
    assert "journal" in missing
    assert "year" in missing
    assert "DOI" in missing
    assert "impact factor" in missing


def test_metadata_diagnostics_guardrails(client_db):
    client, Session = client_db
    resp = client.get("/api/library/papers/metadata-diagnostics")
    assert resp.status_code == 200
    data = resp.json()

    guards = data["safety_guardrails"]
    assert guards["online_scraping_enabled"] is False
    assert guards["auto_completion_enabled"] is False
    assert guards["safety_upgrade_on_completion"] is False
