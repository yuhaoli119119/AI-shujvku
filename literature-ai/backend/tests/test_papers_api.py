import os
import tempfile
import pytest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.config import get_settings
from app.db.models import Base, Paper, WorkflowJob
from app.db.session import get_db_session
import app.api.papers as papers_api

@pytest.fixture
def setup_test_db(monkeypatch):
    # Create temp DB file
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_api.db"
        db_url = f"sqlite:///{db_path}"
        
        # Patch environment
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()
        
        # Setup tables
        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        
        # Override dependency for standard FastAPI injection
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()
        
        app.dependency_overrides[get_db_session] = override_get_db_session
        
        yield engine
        
        # Clean up
        app.dependency_overrides.clear()
        engine.dispose()
        
        # Clean up global engines cache to unlock file on Windows
        from app.db.session import _engines, _session_factories
        for eng in list(_engines.values()):
            try:
                eng.dispose()
            except Exception:
                pass
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()

def test_papers_status_and_stream(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    
    # 1. Initially database is empty
    client = TestClient(app)
    response = client.get("/api/papers/status")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["last_added"] is None
    
    # 2. Add a paper
    with Session() as session:
        paper1 = Paper(title="First Test Paper", pdf_path="test1.pdf")
        session.add(paper1)
        session.commit()
        paper1_id = str(paper1.id)
        
    # Check status again
    response = client.get("/api/papers/status")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["last_added"]["title"] == "First Test Paper"
    assert data["last_added"]["id"] == paper1_id
    
    # 3. Test SSE stream (iterating over the stream)
    # Mock is_disconnected to return False on first call and True on second call to terminate loop
    is_disconnected_calls = 0
    async def mock_is_disconnected(self):
        nonlocal is_disconnected_calls
        is_disconnected_calls += 1
        if is_disconnected_calls > 1:
            return True
        return False
    
    monkeypatch.setattr(Request, "is_disconnected", mock_is_disconnected)
    
    # Mock sleep to be very fast and avoid infinite recursion
    original_sleep = asyncio.sleep
    async def mock_sleep(delay):
        await original_sleep(0.001)
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    
    with client.stream("GET", "/api/papers/stream") as stream_response:
        assert stream_response.status_code == 200
        lines = []
        for line in stream_response.iter_lines():
            if line:
                lines.append(line)
            
        full_output = "\n".join(lines)
        assert "event: papers_update" in full_output
        assert "First Test Paper" in full_output
        assert "event: heartbeat" in full_output


def test_agent_guide_endpoint_exposes_connection_instructions(setup_test_db):
    client = TestClient(app)
    response = client.get("/api/system/agent-guide")
    assert response.status_code == 200
    data = response.json()
    assert data["system_name"] == "Literature AI"
    assert data["recommended_entrypoint"]["path"] == "/api/papers/ai_workflow"
    assert data["mcp"]["url"] == "/mcp"


def test_ai_search_falls_back_to_raw_query_when_llm_unconfigured(setup_test_db, monkeypatch):
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "")
    get_settings.cache_clear()

    captured = {}

    def fake_search(self, query, providers=None, limit=10, target_types=None):
        captured["query"] = query
        captured["providers"] = providers
        captured["limit"] = limit
        return [
            {
                "identifier": "10.1000/test-doi",
                "title": "Test Search Result",
                "doi": "10.1000/test-doi",
                "year": 2024,
                "journal": "Journal of Testing",
                "authors": ["Alice", "Bob"],
                "abstract": "Abstract",
                "url": "https://example.com/paper",
                "pdf_url": "https://example.com/paper.pdf",
                "is_open_access": True,
                "databases": ["openalex"],
            }
        ]

    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ai_search",
        json={"query": "CO2 reduction catalyst", "max_results": 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["prompt_used"] == "CO2 reduction catalyst"
    assert data["llm_status"] == "missing_configuration"
    assert data["llm_diagnostics"]["missing_configuration"] == ["writer_api_base", "writer_api_key"]
    assert data["providers"] == ["openalex", "arxiv"]
    assert data["result_annotation_status"] == "not_applicable"
    assert data["papers"][0]["guard_status"] == "not_applicable"
    assert captured["query"] == "CO2 reduction catalyst"
    assert captured["providers"] == ["openalex", "arxiv"]
    assert captured["limit"] == 3


def test_ai_search_uses_rewritten_query_when_llm_available(setup_test_db, monkeypatch):
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "https://llm.example/v1")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "secret")
    get_settings.cache_clear()

    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "[CO2] AND [single atom catalyst]",
                        }
                    }
                ]
            }

    class DummyClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return DummyResponse()

    def fake_search(self, query, providers=None, limit=10, target_types=None):
        captured["query"] = query
        captured["providers"] = providers
        captured["limit"] = limit
        return []

    monkeypatch.setattr("httpx.Client", DummyClient)
    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ai_search",
        json={
            "query": "find CO2 reduction SAC papers",
            "model": "deepseek-chat",
            "providers": ["pubmed"],
            "max_results": 5,
            "skip_guard": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["prompt_used"] == "[CO2] AND [single atom catalyst]"
    assert data["llm_status"] == "ok"
    assert data["providers"] == ["pubmed"]
    assert data["result_annotation_status"] == "skipped_by_request"
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["query"] == "[CO2] AND [single atom catalyst]"
    assert captured["providers"] == ["pubmed"]
    assert captured["limit"] == 5


def test_list_papers_filters_by_source_path(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add_all(
            [
                Paper(title="Matched Paper", pdf_path="matched.pdf", source_path=r"D:\papers\matched.pdf"),
                Paper(title="Other Paper", pdf_path="other.pdf", source_path=r"D:\papers\other.pdf"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/papers", params={"source_path": r"D:\papers\matched.pdf"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["title"] == "Matched Paper"
    assert data[0]["pdf_path"] == "matched.pdf"


def test_list_papers_filters_by_library_and_lists_libraries(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add_all(
            [
                Paper(title="Library A Paper", pdf_path="a.pdf", library_name="库A"),
                Paper(title="Library B Paper", pdf_path="b.pdf", library_name="库B"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/papers", params={"library_name": "库A"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["title"] == "Library A Paper"
    assert data[0]["library_name"] == "库A"

    response = client.get("/api/papers/libraries")
    assert response.status_code == 200
    libraries = {item["name"]: item["paper_count"] for item in response.json()}
    assert libraries["库A"] == 1
    assert libraries["库B"] == 1


def test_ai_workflow_downloads_and_ingests_results(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "")
    get_settings.cache_clear()

    class DummyPaper:
        pass

    def fake_search(self, query, providers=None, limit=10, target_types=None):
        return [
            {
                "identifier": "10.1000/test-doi",
                "title": "Workflow Paper",
                "doi": "10.1000/test-doi",
                "year": 2024,
                "journal": "Journal of Testing",
                "authors": ["Alice", "Bob"],
                "abstract": "Abstract",
                "url": "https://example.com/paper",
                "pdf_url": "https://example.com/paper.pdf",
                "is_open_access": True,
                "databases": ["openalex"],
            }
        ]

    def fake_fetch_metadata(self, identifier, providers=None):
        paper = DummyPaper()
        return paper, {
            "identifier": identifier,
            "title": "Workflow Paper",
            "doi": "10.1000/test-doi",
            "year": 2024,
            "journal": "Journal of Testing",
            "authors": ["Alice", "Bob"],
            "abstract": "Abstract",
        }

    def fake_download_pdf(self, paper, dest_dir):
        pdf_path = Path(dest_dir) / "workflow.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 workflow test")
        return pdf_path

    async def fake_ingest_pdf(
        self,
        source_path,
        original_filename,
        copy_pdf=True,
        external_metadata=None,
        source_reference=None,
        library_name=None,
    ):
        with Session() as session:
            paper = Paper(
                library_name=library_name or "默认文献库",
                title=external_metadata.get("title"),
                doi=external_metadata.get("doi"),
                pdf_path=str(source_path),
                authors=external_metadata.get("authors") or [],
                year=external_metadata.get("year"),
                journal=external_metadata.get("journal"),
                abstract=external_metadata.get("abstract"),
            )
            session.add(paper)
            session.commit()
            session.refresh(paper)
            return paper

    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)
    monkeypatch.setattr(papers_api.DiscoveryService, "fetch_metadata", fake_fetch_metadata)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf", fake_download_pdf)
    monkeypatch.setattr(papers_api.PaperIngestionService, "ingest_pdf", fake_ingest_pdf)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ai_workflow",
        json={"query": "find workflow papers", "library_name": "AI库", "max_results": 3, "max_downloads": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["prompt_used"] == "find workflow papers"
    assert data["searched_total"] == 1
    assert data["attempted_downloads"] == 1
    assert len(data["ingested"]) == 1
    assert data["ingested"][0]["title"] == "Workflow Paper"
    assert data["failed"] == []
    with Session() as session:
        paper = session.scalar(select(Paper).where(Paper.title == "Workflow Paper"))
        assert paper.library_name == "AI库"


def test_ai_workflow_job_endpoint_runs_without_blocking_request(setup_test_db, monkeypatch):
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "")
    get_settings.cache_clear()

    captured = {}

    def fake_search(self, query, providers=None, limit=10, target_types=None):
        captured["query"] = query
        captured["providers"] = providers
        captured["limit"] = limit
        return []

    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ai_workflow/jobs",
        json={"query": "background workflow", "library_name": "JobLibrary", "max_results": 100, "max_downloads": 100},
    )
    assert response.status_code == 200
    job = response.json()
    assert job["job_id"]
    assert job["status"] in {"queued", "running", "completed"}

    poll = client.get(f"/api/papers/ai_workflow/jobs/{job['job_id']}")
    assert poll.status_code == 200
    data = poll.json()
    assert data["job_id"] == job["job_id"]
    assert data["status"] in {"queued", "running", "completed"}
    if data["status"] == "completed":
        assert data["result"]["searched_total"] == 0
    assert captured["query"] == "background workflow"
    assert captured["limit"] == 100


def test_ai_workflow_job_list_retry_and_cancel_endpoints(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "")
    get_settings.cache_clear()

    captured = {}

    def fake_search(self, query, providers=None, limit=10, target_types=None):
        captured["query"] = query
        return []

    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)

    queued_job_id = str(uuid4())
    failed_job_id = str(uuid4())
    created_base = datetime.utcnow()
    with Session() as session:
        session.add_all(
            [
                WorkflowJob(
                    job_id=queued_job_id,
                    type="ai_workflow",
                    status="queued",
                    library_name="JobLibrary",
                    payload={"query": "queued workflow", "library_name": "JobLibrary", "max_results": 5, "max_downloads": 2},
                    runtime_context={"database_url": str(engine.url), "storage_root": str(Path.cwd())},
                    progress={"phase": "queued"},
                    created_at=created_base,
                    updated_at=created_base,
                ),
                WorkflowJob(
                    job_id=failed_job_id,
                    type="ai_workflow",
                    status="failed",
                    library_name="JobLibrary",
                    payload={"query": "retry workflow", "library_name": "JobLibrary", "max_results": 3, "max_downloads": 1},
                    runtime_context={"database_url": str(engine.url), "storage_root": str(Path.cwd())},
                    progress={"phase": "failed"},
                    error="boom",
                    created_at=created_base + timedelta(seconds=1),
                    updated_at=created_base + timedelta(seconds=1),
                ),
            ]
        )
        session.commit()

    client = TestClient(app)

    response = client.get("/api/papers/ai_workflow/jobs", params={"library_name": "JobLibrary", "limit": 10})
    assert response.status_code == 200
    jobs = response.json()
    assert [job["job_id"] for job in jobs] == [failed_job_id, queued_job_id]

    response = client.get("/api/papers/ai_workflow/jobs", params={"status": "failed", "limit": 10})
    assert response.status_code == 200
    failed_jobs = response.json()
    assert len(failed_jobs) == 1
    assert failed_jobs[0]["job_id"] == failed_job_id

    response = client.post(f"/api/papers/ai_workflow/jobs/{queued_job_id}/cancel")
    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_mode"] == "soft"

    response = client.post(f"/api/papers/ai_workflow/jobs/{failed_job_id}/retry")
    assert response.status_code == 200
    retried = response.json()
    assert retried["job_id"] != failed_job_id
    assert retried["status"] in {"queued", "running", "completed"}
    assert retried["dispatch_mode"] in {"celery", "background_tasks"}
    assert captured["query"] == "retry workflow"

    with Session() as session:
        retried_job = session.get(WorkflowJob, retried["job_id"])
        assert retried_job is not None
        assert retried_job.library_name == "JobLibrary"
        assert retried_job.type == "ai_workflow"


def test_discovery_download_falls_back_to_direct_pdf_url(setup_test_db, monkeypatch):
    class DummyPaper:
        pass

    def fake_fetch_metadata(self, identifier, providers=None):
        return DummyPaper(), {
            "identifier": identifier,
            "title": "Fallback Paper",
            "doi": "10.1000/fallback-doi",
            "year": 2024,
            "journal": "Fallback Journal",
            "authors": ["Alice"],
            "abstract": "Fallback abstract",
            "pdf_url": "https://example.com/fallback.pdf",
        }

    def fake_download_pdf(self, paper, dest_dir):
        raise ValueError("primary download failed")

    def fake_download_pdf_url(self, pdf_url, dest_dir, filename=None):
        pdf_path = Path(dest_dir) / (filename or "fallback.pdf")
        pdf_path.write_bytes(b"%PDF-1.4 fallback")
        return pdf_path

    async def fake_ingest_pdf(
        self,
        source_path,
        original_filename,
        copy_pdf=True,
        external_metadata=None,
        source_reference=None,
        library_name=None,
    ):
        return Paper(
            id=uuid4(),
            library_name=library_name or "默认文献库",
            title=external_metadata.get("title"),
            doi=external_metadata.get("doi"),
            pdf_path=str(source_path),
            authors=external_metadata.get("authors") or [],
            year=external_metadata.get("year"),
            journal=external_metadata.get("journal"),
            abstract=external_metadata.get("abstract"),
        )

    monkeypatch.setattr(papers_api.DiscoveryService, "fetch_metadata", fake_fetch_metadata)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf", fake_download_pdf)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf_url", fake_download_pdf_url)
    monkeypatch.setattr(papers_api.PaperIngestionService, "ingest_pdf", fake_ingest_pdf)

    client = TestClient(app)
    response = client.post(
        "/api/papers/discovery/download",
        json={"identifier": "10.1000/fallback-doi", "library_name": "下载库", "providers": ["openalex"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Fallback Paper"
    assert data["status"] == "completed"


def test_discovery_download_falls_back_to_metadata_only_ingest(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    class DummyPaper:
        pass

    def fake_fetch_metadata(self, identifier, providers=None):
        return DummyPaper(), {
            "identifier": identifier,
            "title": "Metadata Only Paper",
            "doi": "10.1000/metadata-only",
            "year": 2025,
            "journal": "Metadata Journal",
            "authors": ["Alice", "Bob"],
            "abstract": "Metadata fallback abstract",
            "url": "https://example.com/metadata-only",
        }

    def fake_download_pdf(self, paper, dest_dir):
        raise ValueError("primary download failed")

    def fake_download_pdf_url(self, pdf_url, dest_dir, filename=None):
        raise ValueError("direct pdf fallback failed")

    monkeypatch.setattr(papers_api.DiscoveryService, "fetch_metadata", fake_fetch_metadata)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf", fake_download_pdf)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf_url", fake_download_pdf_url)

    client = TestClient(app)
    response = client.post(
        "/api/papers/discovery/download",
        json={"identifier": "10.1000/metadata-only", "library_name": "MetaLibrary", "providers": ["openalex"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Metadata Only Paper"
    assert data["status"] == "metadata_only"

    with Session() as session:
        paper = session.scalar(select(Paper).where(Paper.doi == "10.1000/metadata-only"))
        assert paper is not None
        assert paper.library_name == "MetaLibrary"
        assert paper.pdf_path == ""
        assert paper.oa_status == "metadata_only"
