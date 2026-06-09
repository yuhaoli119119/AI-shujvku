import pytest
from uuid import UUID
from fastapi.testclient import TestClient
from app.main import app
from app.db.models import Paper, Base
import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
from app.db.session import get_db_session


@pytest.fixture
def setup_test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_d5_4_export.db"
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


client = TestClient(app)


def test_export_successful_verified_drafts(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        paper1 = Paper(id=UUID(int=1), title="Paper 1", pdf_path="", authors=["Alice"], year=2021)
        paper2 = Paper(id=UUID(int=2), title="Paper 2", pdf_path="", authors=["Bob"], year=2022)
        session.add_all([paper1, paper2])
        session.commit()

    payload = {
        "cards": [
            {
                "draft_text": "Alice said X.",
                "paper_id": str(UUID(int=1)),
                "evidence_status": "safe_verified"
            },
            {
                "draft_text": "Bob said Y.",
                "paper_id": str(UUID(int=2)),
                "evidence_status": "safe_verified"
            }
        ],
        "export_format": "markdown",
        "include_bibliography": True
    }
    
    response = client.post("/api/writing/export", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    # Assert safety status
    assert data["safety"]["contains_unverified"] is False
    assert data["safety"]["generates_bibliography"] is True
    
    # Assert markdown output
    md = data["compiled_markdown"]
    assert "Alice said X." in md
    assert "Bob said Y." in md
    assert "UNVERIFIED" not in md
    
    # Assert bibtex output contains the references
    bibtex = data["bibliography"]["bibtex"]
    assert "Paper 1" in bibtex
    assert "Paper 2" in bibtex
    assert "Draft reference preview from safe_verified evidence" in bibtex


def test_export_flags_unverified_drafts(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        paper1 = Paper(id=UUID(int=1), title="Paper 1", pdf_path="", authors=["Alice"], year=2021)
        session.add(paper1)
        session.commit()

    payload = {
        "cards": [
            {
                "draft_text": "Alice might have said X.",
                "paper_id": str(UUID(int=1)),
                "evidence_status": "unverified"
            }
        ]
    }
    
    response = client.post("/api/writing/export", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    # Assert safety status flags it
    assert data["safety"]["contains_unverified"] is True
    
    # Assert markdown output has warnings
    md = data["compiled_markdown"]
    assert "[!WARNING]" in md
    assert "contains unverified draft citations" in md
    assert "**[UNVERIFIED]** Alice might have said X." in md
    
    # Assert unverified cards do not generate bibliography entries
    bibtex = data["bibliography"]["bibtex"]
    assert bibtex == ""
    assert data["safety"]["generates_bibliography"] is False
    assert data["safety"]["skips_unverified_bibliography"] is True
