import os
import base64
import tempfile
import pytest
import asyncio
import io
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.config import get_settings
from app.db.models import AuditLog, Base, DFTResult, EvidenceLocator, ExtractionFieldReview, MechanismClaim, Paper, PaperCorrection, PaperFigure, PaperNote, PaperSection, WorkflowJob
from app.db.session import get_db_session
from app.schemas.documents import UnifiedPaperDocument, UnifiedSection
from app.services.paper_ingestion import PaperIngestionService
import app.api.papers as papers_api

@pytest.fixture
def setup_test_db(monkeypatch):
    # Create temp DB file
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        db_path = tmp_root / "test_api.db"
        db_url = f"sqlite:///{db_path}"
        storage_root = tmp_root / "storage"
        
        # Keep API tests from writing uploads into the real active library.
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
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


def test_list_papers_api_supports_year_serial_sorting(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add_all(
            [
                Paper(title="2019-003", year=2019, serial_number=3, pdf_path="a.pdf"),
                Paper(title="2018-010", year=2018, serial_number=10, pdf_path="b.pdf"),
                Paper(title="2019-001", year=2019, serial_number=1, pdf_path="c.pdf"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/papers")
    assert response.status_code == 200
    payload = response.json()
    assert [item["title"] for item in payload] == ["2018-010", "2019-001", "2019-003"]

    response = client.get("/api/papers", params={"sort_by": "year_serial", "sort_order": "desc"})
    assert response.status_code == 200
    payload = response.json()
    assert [item["title"] for item in payload] == ["2019-003", "2019-001", "2018-010"]


def test_unified_jobs_endpoint_lists_and_reuses_active_retry(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    payload = {"paper_id": "paper-1", "schemas": ["DFTResult"]}
    with Session() as session:
        session.add_all(
            [
                WorkflowJob(
                    job_id="failed-extraction",
                    type="extraction",
                    status="failed",
                    library_name="默认文献库",
                    payload=payload,
                    progress={"phase": "failed", "paper_id": "paper-1"},
                    runtime_context={},
                ),
                WorkflowJob(
                    job_id="active-extraction",
                    type="extraction",
                    status="running",
                    library_name="默认文献库",
                    payload=payload,
                    progress={"phase": "extraction", "paper_id": "paper-1"},
                    runtime_context={},
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/jobs?type=extraction")
    assert response.status_code == 200
    jobs = response.json()
    assert {job["job_id"] for job in jobs} >= {"failed-extraction", "active-extraction"}
    assert jobs[0]["summary"]["source_label"] == "论文结构化解析"

    retry_response = client.post("/api/jobs/failed-extraction/retry")
    assert retry_response.status_code == 200
    retry_data = retry_response.json()
    assert retry_data["job_id"] == "active-extraction"
    assert retry_data["deduplicated"] is True
    assert retry_data["dispatch_mode"] == "reused_active"
def test_delete_paper_default_keeps_files(setup_test_db, tmp_path, monkeypatch):
    engine = setup_test_db
    storage_root = tmp_path / "storage"
    for name in ["pdf", "tei", "docling_json", "markdown", "figures"]:
        (storage_root / name).mkdir(parents=True)
    pdf_file = storage_root / "pdf" / "paper.pdf"
    tei_file = storage_root / "tei" / "paper.tei.xml"
    json_file = storage_root / "docling_json" / "paper.json"
    md_file = storage_root / "markdown" / "paper.md"
    figure_file = storage_root / "figures" / "figure.png"
    for path in [pdf_file, tei_file, json_file, md_file, figure_file]:
        path.write_text("fixture", encoding="utf-8")

    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(
            title="Delete Safety Paper",
            pdf_path="paper.pdf",
            tei_path="paper.tei.xml",
            docling_json_path="paper.json",
            markdown_path="paper.md",
        )
        session.add(paper)
        session.flush()
        session.add(PaperFigure(paper_id=paper.id, caption="Figure 1. Real caption", image_path="figure.png"))
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    response = client.delete(f"/api/papers/{paper_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["delete_pdf"] is False
    assert payload["delete_derived"] is False
    assert payload["deleted_files"] == []
    assert pdf_file.exists()
    assert tei_file.exists()
    assert json_file.exists()
    assert md_file.exists()
    assert figure_file.exists()


def test_agent_guide_endpoint_exposes_connection_instructions(setup_test_db):
    client = TestClient(app)
    response = client.get("/api/system/agent-guide")
    assert response.status_code == 200
    data = response.json()
    assert data["system_name"] == "Literature AI"
    assert data["recommended_entrypoint"]["mode"] == "codex_mcp_first"
    assert data["recommended_entrypoint"]["path"] == "/mcp"
    assert "get_codex_context" in data["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "get_codex_item" in data["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "get_paper_knowledge" in data["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "get_dft_review_queue" in data["recommended_entrypoint"]["json_schema_hint"]["read_tools"]
    assert "insert_word_citation" in data["recommended_entrypoint"]["json_schema_hint"]["writing_tools"]
    assert data["mcp"]["url"] == "/mcp"
    assert "get_codex_context" in data["mcp"]["common_tools"]
    assert "get_codex_item" in data["mcp"]["common_tools"]
    assert "get_paper_knowledge" in data["mcp"]["common_tools"]
    assert "get_dft_review_queue" in data["mcp"]["common_tools"]
    assert "verify_dft_result" in data["mcp"]["common_tools"]
    assert "reject_dft_result" in data["mcp"]["common_tools"]
    assert "propose_dft_result_correction" in data["mcp"]["common_tools"]
    assert "retrieve_evidence" in data["mcp"]["common_tools"]
    assert "insert_word_citation" in data["mcp"]["common_tools"]


def test_codex_context_endpoint_returns_candidate_aware_bundle(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    figure_dir = get_settings().storage_paths["figures"]
    figure_dir.mkdir(parents=True, exist_ok=True)
    (figure_dir / "fig1.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
    )
    with Session() as session:
        paper = Paper(
            title="Graphene vacancy defect DFT test paper",
            doi="10.1000/codex-context",
            year=2026,
            journal="Codex Test Journal",
            authors=["A. Researcher"],
            abstract="DFT calculations show that vacancy defects modify adsorption on graphene.",
            pdf_path="graphene-vacancy.pdf",
            paper_type="A",
            type_confidence=0.91,
            classification_source="test",
        )
        session.add(paper)
        session.flush()
        section = PaperSection(
            paper_id=paper.id,
            section_title="Computational Methods",
            section_type="methods",
            text="VASP and PBE were used to model a graphene single-vacancy defect supercell.",
            page_start=2,
            page_end=4,
        )
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 1. Optimized graphene vacancy defect structure.",
            image_path="fig1.png",
            page=3,
            figure_role="structure",
            role_confidence=0.8,
            content_summary="Vacancy defect model.",
            key_elements=["graphene", "vacancy"],
            prov=[
                {
                    "page_no": 3,
                    "bbox": {
                        "l": 10,
                        "t": 200,
                        "r": 210,
                        "b": 40,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ],
        )
        dft_result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="Li adsorption",
            source_section="Computational Methods",
            evidence_text="The Li adsorption energy on the vacancy defect is -1.23 eV.",
            confidence=0.82,
        )
        session.add_all(
            [
                section,
                figure,
                dft_result,
                MechanismClaim(
                    paper_id=paper.id,
                    claim_type="defect_adsorption",
                    claim_text="Vacancy defects strengthen Li adsorption on graphene.",
                    evidence_types=["DFT"],
                    evidence_text="The vacancy defect strengthens Li adsorption.",
                    confidence=0.76,
                ),
                PaperNote(
                    paper_id=paper.id,
                    source="codex_test",
                    content="Treat this as an unverified DFT candidate until checked against the PDF.",
                    field_name="dft_results",
                ),
            ]
        )
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(dft_result.id),
                field_name="value",
                page=4,
                evidence_text="The Li adsorption energy on the vacancy defect is -1.23 eV.",
                locator_status="exact_page",
                locator_confidence=0.9,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = paper.id
        figure_id = figure.id
        dft_result_id = dft_result.id

    client = TestClient(app)
    response = client.get(f"/api/papers/{paper_id}/codex-context")
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "codex_context_v1"
    assert data["paper_id"] == str(paper_id)
    assert data["context"]["reliability_policy"]["automatic_outputs_are_candidates"] is True
    assert data["context"]["reliability_policy"]["figure_crops_are_candidates"] is True
    assert data["context"]["content"]["sections"][0]["title"] == "Computational Methods"
    figure = data["context"]["content"]["figures"][0]
    assert figure["prov"][0]["bbox"]["l"] == 10
    assert figure["image_review"]["bbox_size_points"] == {"width": 200.0, "height": 160.0}
    assert figure["image_review"]["pixel_size"] == {"width": 1, "height": 1}
    assert figure["image_review"]["review_required"] is True
    assert "small_crop_or_subfigure" in figure["image_review"]["flags"]
    assert data["context"]["structured_candidates"]["dft_results"][0]["candidate_status"] == "candidate_unverified"
    dft_safety = data["context"]["structured_candidates"]["dft_results"][0]["export_safety"]
    assert dft_safety["is_exportable"] is False
    assert dft_safety["blocked_reasons"] == ["missing_review"]
    readiness = data["context"]["dft_export_readiness"]
    assert readiness["safety_gate"] == "safe_verified_with_required_evidence"
    assert readiness["total_candidates"] == 1
    assert readiness["eligible_count"] == 0
    assert readiness["blocked_count"] == 1
    assert readiness["blocked_reasons"] == {"missing_review": 1}
    assert data["context"]["evidence_locators"]["status_counts"]["exact_page"] == 1
    assert any(item["code"] == "dft_unverified" for item in data["context"]["warnings"])
    assert any(item["code"] == "dft_export_blocked" for item in data["context"]["warnings"])
    assert any(item["code"] == "figure_crop_review" for item in data["context"]["warnings"])
    assert "Graphene vacancy defect DFT test paper" in data["markdown"]
    assert "Automatic parser, extraction, and external analysis outputs are candidates" in data["markdown"]
    assert "crop=needs_review" in data["markdown"]
    assert "DFT Export Readiness" in data["markdown"]

    item_response = client.get(f"/api/papers/{paper_id}/codex-item/dft_result/{dft_result_id}")
    assert item_response.status_code == 200
    item_data = item_response.json()
    assert item_data["schema_version"] == "codex_item_context_v1"
    assert item_data["item_type"] == "dft_result"
    assert item_data["context"]["item"]["export_safety"]["blocked_reasons"] == ["missing_review"]
    assert item_data["context"]["evidence_locators"]["items"][0]["page"] == 4
    assert item_data["context"]["nearby_context"]["related_sections"][0]["title"] == "Computational Methods"
    assert "Codex Item: dft_result" in item_data["markdown"]

    figure_response = client.get(f"/api/papers/{paper_id}/codex-item/figure/{figure_id}")
    assert figure_response.status_code == 200
    figure_data = figure_response.json()
    assert figure_data["context"]["item"]["image_review"]["review_required"] is True
    assert figure_data["context"]["nearby_context"]["related_sections"][0]["title"] == "Computational Methods"

    invalid_response = client.get(f"/api/papers/{paper_id}/codex-item/not_supported/{figure_id}")
    assert invalid_response.status_code == 400


def test_paper_knowledge_context_uses_section_fallback_and_external_candidates(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(
            title="Graphene defect knowledge fallback paper",
            year=2026,
            journal="Codex Test Journal",
            abstract="Vacancy and Stone-Wales defects alter graphene reactivity in density functional theory calculations.",
            pdf_path="graphene-knowledge.pdf",
        )
        session.add(paper)
        session.flush()
        session.add_all(
            [
                PaperSection(
                    paper_id=paper.id,
                    section_title="Introduction",
                    section_type="introduction",
                    text="However, the origin of defect-driven graphene reactivity remains difficult to organize across studies.",
                    page_start=1,
                    page_end=1,
                ),
                PaperSection(
                    paper_id=paper.id,
                    section_title="Computational Methods",
                    section_type="methods",
                    text="DFT calculations used PBE to model vacancy defects and Stone-Wales defects in graphene supercells.",
                    page_start=2,
                    page_end=3,
                ),
                PaperSection(
                    paper_id=paper.id,
                    section_title="Results and Discussion",
                    section_type="results",
                    text="The Stone-Wales defect changes adsorption and charge density around the defect site, suggesting a mechanism context for reactivity.",
                    page_start=4,
                    page_end=5,
                ),
                PaperSection(
                    paper_id=paper.id,
                    section_title="Conclusions",
                    section_type="conclusion",
                    text="The study concludes that graphene defect topology controls the calculated reactivity trends.",
                    page_start=8,
                    page_end=8,
                ),
                PaperNote(
                    paper_id=paper.id,
                    source="codex_test",
                    field_name="mechanism",
                    content="Codex note: compare defect topology, charge redistribution, and adsorption evidence.",
                    quoted_text="defect topology controls the calculated reactivity trends",
                    page=8,
                    section_title="Conclusions",
                ),
            ]
        )
        session.commit()
        paper_id = paper.id

    client = TestClient(app)
    imported = client.post(
        "/api/external-analysis/import",
        json={
            "paper_id": str(paper_id),
            "source": "web_ai",
            "source_label": "Web AI parsed summary",
            "raw_payload": {
                "review_notes": [
                    {
                        "content": "Web AI summary candidate: Stone-Wales strain and vacancy charge redistribution explain reactivity differences.",
                        "field_name": "mechanism",
                        "page": 4,
                        "section_title": "Results and Discussion",
                        "quoted_text": "Stone-Wales defect changes adsorption and charge density",
                        "confidence": 0.72,
                    }
                ]
            },
        },
    )
    assert imported.status_code == 200
    assert imported.json()["candidates"][0]["candidate_type"] == "note"

    response = client.get(f"/api/papers/{paper_id}/knowledge-context", params={"max_candidates": 20})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "paper_knowledge_context_v1"
    assert payload["metadata"]["has_mechanism_claims"] is False
    assert payload["reliability_policy"]["section_fallbacks_are_not_final_claims"] is True
    categories = {item["category"] for item in payload["candidates"]}
    source_types = {item["source_type"] for item in payload["candidates"]}
    assert "mechanism_context" in categories
    assert "computational_method" in categories
    assert "conclusion" in categories
    assert "external_analysis_candidate" in source_types
    assert "paper_note" in source_types
    assert any(item["candidate_status"] == "section_candidate_unverified" for item in payload["candidates"])
    assert "Paper Knowledge Candidates" in payload["markdown"]

    codex_response = client.get(f"/api/papers/{paper_id}/codex-context", params={"max_candidates": 20})
    assert codex_response.status_code == 200
    codex = codex_response.json()["context"]
    assert codex["knowledge_candidates"]["items"]
    assert codex["structured_candidates"]["knowledge_candidates"]


def test_verify_dft_result_promotes_candidate_to_exportable(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(
            title="DFT verification paper",
            doi="10.1000/dft-verify",
            year=2026,
            journal="Codex Test Journal",
            authors=["A. Curator"],
            abstract="DFT verification test.",
            pdf_path="dft-verify.pdf",
            oa_status="arxiv_pdf",
        )
        session.add(paper)
        session.flush()
        dft_result = DFTResult(
            paper_id=paper.id,
            adsorbate="Li",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="Li adsorption",
            source_section="Results",
            evidence_text="The Li adsorption energy on the vacancy defect is -1.23 eV.",
            confidence=0.9,
        )
        missing_locator_result = DFTResult(
            paper_id=paper.id,
            adsorbate="Na",
            property_type="adsorption_energy",
            value=-0.5,
            unit="eV",
            source_section="Results",
            evidence_text="The Na adsorption energy is -0.5 eV.",
            confidence=0.7,
        )
        session.add_all([dft_result, missing_locator_result])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(dft_result.id),
                field_name="value",
                page=4,
                evidence_text="The Li adsorption energy on the vacancy defect is -1.23 eV.",
                locator_status="exact_page",
                locator_confidence=0.95,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = paper.id
        dft_result_id = dft_result.id
        missing_locator_result_id = missing_locator_result.id

    client = TestClient(app)
    not_confirmed = client.post(
        f"/api/papers/{paper_id}/dft-results/{dft_result_id}/verify",
        json={"confirm_reviewed_against_pdf": False},
    )
    assert not_confirmed.status_code == 400

    missing_locator = client.post(
        f"/api/papers/{paper_id}/dft-results/{missing_locator_result_id}/verify",
        json={"confirm_reviewed_against_pdf": True, "reviewer": "codex_test"},
    )
    assert missing_locator.status_code == 400
    assert "missing_evidence_reference" in missing_locator.json()["detail"]

    verified = client.post(
        f"/api/papers/{paper_id}/dft-results/{dft_result_id}/verify",
        json={
            "confirm_reviewed_against_pdf": True,
            "reviewer": "codex_test",
            "reviewer_note": "Checked PDF page and evidence text.",
        },
    )
    assert verified.status_code == 200
    payload = verified.json()
    assert payload["dft_result_id"] == str(dft_result_id)
    assert payload["export_safety"]["is_exportable"] is True
    assert payload["export_safety"]["review_status"] == "verified"
    assert payload["export_safety"]["locator_status"] == "exact_page"
    assert payload["field_names"] == ["value", "adsorbate", "energy_type", "reaction_step"]
    assert all(item["verified"] is True for item in payload["reviews"])

    context_response = client.get(
        f"/api/papers/{paper_id}/codex-context",
        params={"max_candidates": 10},
    )
    assert context_response.status_code == 200
    readiness = context_response.json()["context"]["dft_export_readiness"]
    assert readiness["total_candidates"] == 2
    assert readiness["eligible_count"] == 1
    assert readiness["blocked_count"] == 1
    assert readiness["blocked_reasons"] == {"missing_review": 1, "missing_evidence": 1}

    dataset_response = client.get("/api/papers/export/dft-dataset")
    assert dataset_response.status_code == 200
    dataset = dataset_response.json()
    assert dataset["metadata"]["eligible_count"] == 1
    assert dataset["metadata"]["blocked_count"] == 1
    assert len(dataset["records"]) == 1
    assert dataset["records"][0]["record_id"] == str(dft_result_id)

    queue_response = client.get("/api/papers/export/dft-review-queue")
    assert queue_response.status_code == 200
    queue = queue_response.json()
    assert queue["metadata"]["schema_version"] == "dft_review_queue_v1"
    assert queue["metadata"]["eligible_count"] == 1
    assert queue["metadata"]["blocked_count"] == 1
    assert len(queue["rows"]) == 1
    assert queue["rows"][0]["record_id"] == str(missing_locator_result_id)
    assert queue["rows"][0]["recommended_action"] == "repair_evidence_reference"
    assert queue["rows"][0]["can_mark_verified"] is False
    assert "verify_url" in queue["rows"][0]

    exportable_queue_response = client.get("/api/papers/export/dft-review-queue", params={"status": "exportable"})
    assert exportable_queue_response.status_code == 200
    exportable_rows = exportable_queue_response.json()["rows"]
    assert len(exportable_rows) == 1
    assert exportable_rows[0]["record_id"] == str(dft_result_id)
    assert exportable_rows[0]["recommended_action"] == "ready_for_ml_export"

    with Session() as session:
        reviews = session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.target_id == str(dft_result_id))
        ).all()
        assert {review.field_name for review in reviews} == {"value", "adsorbate", "energy_type", "reaction_step"}
        assert all(review.reviewer_status == "verified" for review in reviews)
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "verify_dft_result"))
        assert audit is not None
        assert audit.target_id == str(dft_result_id)


def test_dft_review_queue_flags_suspicious_real_world_candidates(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    with Session() as session:
        paper = Paper(title="Suspicious DFT candidate paper", year=2025, pdf_path="suspicious.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="limiting_potential",
            adsorbate="[22]",
            value=436.0,
            unit="e",
            evidence_text="A sentence near reference [22] was incorrectly parsed as a DFT value.",
            confidence=0.8,
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_result",
                target_id=str(row.id),
                field_name="value",
                page=5,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.9,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    client = TestClient(app)
    response = client.get("/api/papers/export/dft-review-queue")
    assert response.status_code == 200
    queue_row = response.json()["rows"][0]
    assert queue_row["blocked_reasons"] == ["missing_review"]
    assert queue_row["can_mark_verified"] is False
    assert queue_row["recommended_action"] == "inspect_suspicious_candidate"
    assert "adsorbate_looks_like_reference" in queue_row["sanity_flags"]
    assert "unexpected_potential_unit:e" in queue_row["sanity_flags"]
    assert queue_row["correction_url"].endswith(f"/api/papers/{paper_id}/dft-results/{row_id}/corrections")

    unconfirmed_correction = client.post(
        f"/api/papers/{paper_id}/dft-results/{row_id}/corrections",
        json={
            "confirm_correction_proposal": False,
            "field_name": "unit",
            "proposed_value": "V",
            "reason": "The source table uses potential units, not charge units.",
        },
    )
    assert unconfirmed_correction.status_code == 400

    correction_response = client.post(
        f"/api/papers/{paper_id}/dft-results/{row_id}/corrections",
        json={
            "confirm_correction_proposal": True,
            "field_name": "unit",
            "proposed_value": "V",
            "reason": "The source table uses potential units, not charge units.",
            "reviewer": "codex_test",
            "evidence_payload": {"page": 5, "field": "unit"},
        },
    )
    assert correction_response.status_code == 200
    correction = correction_response.json()["correction"]
    assert correction["status"] == "pending"
    assert correction["field_name"] == "dft_results"
    assert correction["target_path"] == f"dft_results:{row_id}:unit"
    assert correction["proposed_value"] == "V"

    not_confirmed = client.post(
        f"/api/papers/{paper_id}/dft-results/{row_id}/reject",
        json={"confirm_reject_candidate": False},
    )
    assert not_confirmed.status_code == 400

    rejected = client.post(
        f"/api/papers/{paper_id}/dft-results/{row_id}/reject",
        json={
            "confirm_reject_candidate": True,
            "reviewer": "codex_test",
            "reviewer_note": "Looks like a reference/citation artifact rather than a DFT row.",
        },
    )
    assert rejected.status_code == 200
    rejected_payload = rejected.json()
    assert rejected_payload["dft_result_id"] == str(row_id)
    assert rejected_payload["export_safety"]["is_exportable"] is False
    assert rejected_payload["export_safety"]["review_status"] == "rejected"
    assert rejected_payload["export_safety"]["blocked_reasons"] == ["unsafe_review"]
    assert all(item["reviewer_status"] == "rejected" for item in rejected_payload["reviews"])

    active_queue = client.get("/api/papers/export/dft-review-queue")
    assert active_queue.status_code == 200
    assert active_queue.json()["rows"] == []

    rejected_queue = client.get("/api/papers/export/dft-review-queue", params={"status": "rejected"})
    assert rejected_queue.status_code == 200
    rejected_row = rejected_queue.json()["rows"][0]
    assert rejected_row["record_id"] == str(row_id)
    assert rejected_row["decision_status"] == "rejected"
    assert rejected_row["recommended_action"] == "rejected_candidate"

    dataset = client.get("/api/papers/export/dft-dataset").json()
    assert dataset["metadata"]["eligible_count"] == 0
    assert dataset["metadata"]["blocked_count"] == 1
    assert dataset["metadata"]["blocked_reasons"] == {"unsafe_review": 1}

    with Session() as session:
        saved_correction = session.scalar(select(PaperCorrection).where(PaperCorrection.target_path == f"dft_results:{row_id}:unit"))
        assert saved_correction is not None
        assert saved_correction.status == "pending"
        correction_audit = session.scalar(select(AuditLog).where(AuditLog.action == "propose_dft_result_correction"))
        assert correction_audit is not None
        assert correction_audit.target_id == str(saved_correction.id)
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "reject_dft_result"))
        assert audit is not None
        assert audit.target_id == str(row_id)


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


def test_default_library_aliases_are_merged_in_listing(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add_all(
            [
                Paper(title="Default Paper", pdf_path="a.pdf", library_name="默认文献库"),
                Paper(title="Alias Paper", pdf_path="b.pdf", library_name="?????"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/papers", params={"library_name": "默认文献库"})
    assert response.status_code == 200
    data = response.json()
    assert {item["title"] for item in data} == {"Default Paper", "Alias Paper"}
    assert {item["library_name"] for item in data} == {"默认文献库"}

    response = client.get("/api/papers/libraries")
    assert response.status_code == 200
    libraries = {item["name"]: item["paper_count"] for item in response.json()}
    assert libraries["默认文献库"] == 2


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


def test_metadata_only_same_doi_upsert_reuses_paper(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        service = PaperIngestionService(session=session, settings=get_settings())
        first = service.ingest_metadata_only(
            {
                "title": "Stable Metadata Paper",
                "doi": "10.1000/stable-meta",
                "year": 2024,
                "journal": "Metadata Journal",
            },
            library_name="MetaDedup",
        )
        first_id = first.id
        first_serial = first.serial_number
        second = service.ingest_metadata_only(
            {
                "title": "Stable Metadata Paper",
                "doi": "10.1000/stable-meta",
                "year": 2024,
                "abstract": "New abstract should fill a missing field",
            },
            library_name="MetaDedup",
        )
        assert second.id == first_id
        assert second.serial_number == first_serial
        assert second.abstract == "New abstract should fill a missing field"
        papers = session.scalars(select(Paper).where(Paper.library_name == "MetaDedup")).all()
        assert len(papers) == 1


def test_metadata_only_doi_url_variants_normalize_to_same_paper(setup_test_db):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        service = PaperIngestionService(session=session, settings=get_settings())
        first = service.ingest_metadata_only(
            {"title": "DOI Variant Paper", "doi": "https://doi.org/10.1000/variant", "year": 2025},
            library_name="VariantLibrary",
        )
        second = service.ingest_metadata_only(
            {"title": "DOI Variant Paper", "doi": "doi:10.1000/variant", "year": 2025},
            library_name="VariantLibrary",
        )
        assert second.id == first.id
        assert second.doi == "10.1000/variant"
        papers = session.scalars(select(Paper).where(Paper.library_name == "VariantLibrary")).all()
        assert len(papers) == 1


def test_ai_workflow_metadata_only_fallback_does_not_duplicate(setup_test_db, monkeypatch):
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
                "identifier": "https://doi.org/10.1000/workflow-meta",
                "title": "Workflow Metadata Fallback",
                "doi": "https://doi.org/10.1000/workflow-meta",
                "year": 2025,
                "url": "https://example.com/workflow-meta",
                "databases": ["openalex"],
            }
        ]

    def fake_fetch_metadata(self, identifier, providers=None):
        return DummyPaper(), {
            "identifier": identifier,
            "title": "Workflow Metadata Fallback",
            "doi": "doi:10.1000/workflow-meta",
            "year": 2025,
            "journal": "Workflow Journal",
            "url": "https://example.com/workflow-meta",
        }

    def fake_download_pdf(self, paper, dest_dir):
        raise ValueError("primary download failed")

    def fake_download_pdf_url(self, pdf_url, dest_dir, filename=None):
        raise ValueError("direct download failed")

    monkeypatch.setattr(papers_api.DiscoveryService, "search", fake_search)
    monkeypatch.setattr(papers_api.DiscoveryService, "fetch_metadata", fake_fetch_metadata)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf", fake_download_pdf)
    monkeypatch.setattr(papers_api.DiscoveryService, "download_pdf_url", fake_download_pdf_url)

    client = TestClient(app)
    payload = {"query": "workflow metadata", "library_name": "WorkflowMeta", "max_results": 1, "max_downloads": 1}
    first = client.post("/api/papers/ai_workflow", json=payload)
    second = client.post("/api/papers/ai_workflow", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200

    with Session() as session:
        papers = session.scalars(select(Paper).where(Paper.library_name == "WorkflowMeta")).all()
        assert len(papers) == 1
        assert papers[0].doi == "10.1000/workflow-meta"
        assert papers[0].oa_status == "metadata_only"


def _install_ingest_document_stubs(monkeypatch, metadata: dict[str, object], section_text: str = "Parsed section text"):
    async def fake_grobid_parse(self, stored_pdf):
        return None

    async def fake_docling_parse(self, stored_pdf):
        return None

    async def fake_build_unified_document(self, stored_pdf, grobid_result, docling_result):
        return UnifiedPaperDocument(
            metadata=metadata,
            abstract=str(metadata.get("abstract") or ""),
            sections=[UnifiedSection(section_title="Body", section_type="body", text=section_text, page_start=1, page_end=1)],
            tables=[],
            figures=[],
            references=[],
            markdown="# Parsed",
            tei_xml="<TEI/>",
            docling_json={"title": metadata.get("title")},
            source_pdf_path=stored_pdf,
            tei_path=stored_pdf.with_suffix(".tei.xml"),
            markdown_path=stored_pdf.with_suffix(".md"),
            docling_json_path=stored_pdf.with_suffix(".json"),
        )

    def fake_run_stage2(self, paper, document):
        return {
            "dft_settings": 0,
            "catalyst_samples": 0,
            "dft_results": 0,
            "electrochemical_performance": 0,
            "mechanism_claims": 0,
            "writing_cards": 0,
            "comprehensive_analysis": 0,
        }

    monkeypatch.setattr("app.services.paper_ingestion.GrobidParser.parse_pdf", fake_grobid_parse)
    monkeypatch.setattr("app.services.paper_ingestion.DoclingParser.parse_pdf", fake_docling_parse)
    monkeypatch.setattr(papers_api.PaperIngestionService, "_build_unified_document", fake_build_unified_document)
    monkeypatch.setattr("app.services.extraction_pipeline.ExtractionPipelineService.run_stage2", fake_run_stage2)
    monkeypatch.setattr("app.services.extraction_pipeline.ExtractionPipelineService.replace_stage2", fake_run_stage2)


def test_upload_pdf_merges_metadata_only_placeholder_by_doi(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Merged By DOI",
            "doi": "10.1000/merged-doi",
            "year": 2024,
            "journal": "Merge Journal",
            "authors": ["Alice"],
            "abstract": "Merged abstract",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="MergeLibrary",
            doi="10.1000/merged-doi",
            title="Merged By DOI",
            year=2024,
            journal="Merge Journal",
            authors=["Alice"],
            abstract="Metadata only",
            pdf_path="",
            source_path="https://doi.org/10.1000/merged-doi",
            oa_status="metadata_only",
            serial_number=7,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ingest/upload",
        data={"library_name": "MergeLibrary"},
        files={"file": ("merged.pdf", io.BytesIO(b"%PDF-1.4 merged"), "application/pdf")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["paper_id"] == placeholder_id
    assert data["status"] == "merged"

    with Session() as session:
        papers = session.scalars(select(Paper).where(Paper.library_name == "MergeLibrary")).all()
        assert len(papers) == 1
        paper = papers[0]
        assert str(paper.id) == placeholder_id
        assert paper.serial_number == 7
        assert paper.oa_status == "uploaded"
        assert paper.pdf_path.endswith(".pdf")
        assert paper.tei_path.endswith(".tei.xml")


def test_upload_pdf_merges_metadata_only_placeholder_by_title_and_year(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Exact Title Match For Merge",
            "year": 2025,
            "journal": "Match Journal",
            "authors": ["Alice", "Bob"],
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="MergeLibrary",
            doi=None,
            title="Exact Title Match For Merge",
            year=2025,
            journal="Match Journal",
            authors=["Alice", "Bob"],
            abstract="Metadata only",
            pdf_path="",
            source_path="https://example.com/placeholder",
            oa_status="metadata_only",
            serial_number=3,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ingest/upload",
        data={"library_name": "MergeLibrary"},
        files={"file": ("title-match.pdf", io.BytesIO(b"%PDF-1.4 title match"), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json()["paper_id"] == placeholder_id

    with Session() as session:
        papers = session.scalars(select(Paper).where(Paper.library_name == "MergeLibrary")).all()
        assert len(papers) == 1
        assert str(papers[0].id) == placeholder_id
        assert papers[0].oa_status == "uploaded"


def test_upload_pdf_with_existing_full_doi_returns_conflict(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Existing DOI Paper",
            "doi": "10.1000/existing-full",
            "year": 2022,
        },
    )

    with Session() as session:
        existing = Paper(
            library_name="ConflictLibrary",
            doi="10.1000/existing-full",
            title="Existing DOI Paper",
            year=2022,
            pdf_path="existing.pdf",
            oa_status="downloaded",
            serial_number=1,
        )
        session.add(existing)
        session.commit()
        session.refresh(existing)
        existing_id = str(existing.id)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ingest/upload",
        data={"library_name": "ConflictLibrary"},
        files={"file": ("existing.pdf", io.BytesIO(b"%PDF-1.4 conflict"), "application/pdf")},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "already_exists"
    assert detail["paper_id"] == existing_id

    with Session() as session:
        papers = session.scalars(select(Paper).where(Paper.library_name == "ConflictLibrary")).all()
        assert len(papers) == 1
        assert papers[0].pdf_path == "existing.pdf"


def test_low_confidence_title_does_not_auto_merge(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Distinct catalyst reconstruction pathway",
            "year": 2025,
            "journal": "Different Journal",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="LowConfidenceLibrary",
            title="Single atom catalyst design study",
            year=2025,
            journal="Match Journal",
            pdf_path="",
            source_path="https://example.com/metadata-only",
            oa_status="metadata_only",
            serial_number=2,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        "/api/papers/ingest/upload",
        data={"library_name": "LowConfidenceLibrary"},
        files={"file": ("low-confidence.pdf", io.BytesIO(b"%PDF-1.4 low confidence"), "application/pdf")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["paper_id"] != placeholder_id
    assert data["status"] == "completed"

    with Session() as session:
        papers = session.scalars(select(Paper).where(Paper.library_name == "LowConfidenceLibrary")).all()
        assert len(papers) == 2
        assert any(str(paper.id) == placeholder_id for paper in papers)


def test_attach_pdf_low_confidence_requires_confirmation_by_default(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Forced Attach Paper",
            "year": 2024,
            "journal": "Attach Journal",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="AttachLibrary",
            title="Placeholder Title That Does Not Match",
            year=2024,
            pdf_path="",
            source_path="https://example.com/attach-me",
            oa_status="metadata_only",
            serial_number=5,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{placeholder_id}/attach-pdf",
        files={"file": ("attach.pdf", io.BytesIO(b"%PDF-1.4 attach"), "application/pdf")},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "needs_confirmation"
    assert detail["target_paper_id"] == placeholder_id
    assert detail["incoming"]["title"] == "Forced Attach Paper"

    with Session() as session:
        paper = session.get(Paper, UUID(placeholder_id))
        assert paper is not None
        assert paper.oa_status == "metadata_only"
        assert paper.pdf_path == ""


def test_attach_pdf_low_confidence_confirmed_binds_and_preserves_identity(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Forced Attach Paper",
            "year": 2024,
            "journal": "Attach Journal",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="AttachLibrary",
            title="Placeholder Title That Does Not Match",
            year=2024,
            pdf_path="",
            source_path="https://example.com/attach-me",
            oa_status="metadata_only",
            serial_number=5,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)
        placeholder_serial = placeholder.serial_number

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{placeholder_id}/attach-pdf",
        data={"confirm_identity_mismatch": "true"},
        files={"file": ("attach.pdf", io.BytesIO(b"%PDF-1.4 attach"), "application/pdf")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["paper_id"] == placeholder_id
    assert data["status"] == "merged_confirmed"

    with Session() as session:
        paper = session.get(Paper, UUID(placeholder_id))
        assert paper is not None
        assert paper.serial_number == placeholder_serial
        assert paper.oa_status == "uploaded"
        assert paper.pdf_path.endswith(".pdf")


def test_attach_pdf_doi_conflict_rejected_even_when_confirmed(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Incoming DOI Conflict",
            "doi": "10.1000/incoming-conflict",
            "year": 2024,
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="AttachLibrary",
            doi="10.1000/target-conflict",
            title="Target DOI Conflict",
            year=2024,
            pdf_path="",
            oa_status="metadata_only",
            serial_number=9,
        )
        session.add(placeholder)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{placeholder_id}/attach-pdf",
        data={"confirm_identity_mismatch": "true"},
        files={"file": ("doi-conflict.pdf", io.BytesIO(b"%PDF-1.4 conflict"), "application/pdf")},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "identity_mismatch"
    assert detail["target"]["doi"] == "10.1000/target-conflict"
    assert detail["incoming"]["doi"] == "10.1000/incoming-conflict"

    with Session() as session:
        paper = session.get(Paper, UUID(placeholder_id))
        assert paper is not None
        assert paper.pdf_path == ""
        assert paper.oa_status == "metadata_only"


def test_attach_pdf_existing_full_paper_returns_already_exists(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={"title": "Already Has PDF", "doi": "10.1000/already-attached", "year": 2024},
    )

    with Session() as session:
        existing = Paper(
            library_name="AttachLibrary",
            doi="10.1000/already-attached",
            title="Already Has PDF",
            year=2024,
            pdf_path="existing.pdf",
            oa_status="uploaded",
            serial_number=4,
        )
        session.add(existing)
        session.commit()
        session.refresh(existing)
        existing_id = str(existing.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{existing_id}/attach-pdf",
        files={"file": ("already.pdf", io.BytesIO(b"%PDF-1.4 already"), "application/pdf")},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "already_exists"
    assert detail["paper_id"] == existing_id

    with Session() as session:
        paper = session.get(Paper, UUID(existing_id))
        assert paper.pdf_path == "existing.pdf"
        assert paper.oa_status == "uploaded"


def test_attach_pdf_preserves_verified_field_reviews_by_paper_id(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Reviewed Metadata Placeholder",
            "year": 2025,
            "journal": "Review Journal",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="ReviewAttachLibrary",
            title="Reviewed Metadata Placeholder",
            year=2025,
            pdf_path="",
            oa_status="metadata_only",
            serial_number=11,
        )
        session.add(placeholder)
        session.flush()
        review = ExtractionFieldReview(
            paper_id=placeholder.id,
            target_type="dft_results",
            target_id="legacy-target-id",
            field_name="adsorption_energy",
            original_value="-1.0",
            reviewed_value="-1.1",
            unit="eV",
            evidence_text="Legacy evidence",
            reviewer_status="verified",
            reviewer="qa",
            reviewer_note="checked before attach",
        )
        session.add(review)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)
        placeholder_serial = placeholder.serial_number

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{placeholder_id}/attach-pdf",
        files={"file": ("reviewed.pdf", io.BytesIO(b"%PDF-1.4 reviewed"), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json()["paper_id"] == placeholder_id

    reviews_response = client.get(f"/api/extraction/results/{placeholder_id}/reviews")
    assert reviews_response.status_code == 200
    reviews = reviews_response.json()
    assert len(reviews) == 1
    assert reviews[0]["reviewer_status"] == "verified"
    assert reviews[0]["reviewed_value"] == "-1.1"

    with Session() as session:
        paper = session.get(Paper, UUID(placeholder_id))
        assert paper.serial_number == placeholder_serial
        review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper.id))
        assert review is not None
        assert review.reviewer_status == "verified"


def test_attach_pdf_preserves_evidence_locators_on_original_paper_id(setup_test_db, monkeypatch):
    engine = setup_test_db
    Session = sessionmaker(bind=engine)
    _install_ingest_document_stubs(
        monkeypatch,
        metadata={
            "title": "Locator Placeholder",
            "year": 2025,
            "journal": "Locator Journal",
        },
    )

    with Session() as session:
        placeholder = Paper(
            library_name="LocatorAttachLibrary",
            title="Locator Placeholder",
            year=2025,
            pdf_path="",
            oa_status="metadata_only",
            serial_number=12,
        )
        session.add(placeholder)
        session.flush()
        locator = EvidenceLocator(
            paper_id=placeholder.id,
            chunk_id="placeholder-chunk",
            source_type="text",
            page=1,
            section="Body",
            evidence_text="Metadata placeholder evidence.",
            locator_status="page_only",
            locator_confidence=0.7,
            parser_source="fallback",
        )
        session.add(locator)
        session.commit()
        session.refresh(placeholder)
        placeholder_id = str(placeholder.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{placeholder_id}/attach-pdf",
        files={"file": ("locator.pdf", io.BytesIO(b"%PDF-1.4 locator"), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json()["paper_id"] == placeholder_id

    locators_response = client.get(f"/api/papers/{placeholder_id}/evidence/locators")
    assert locators_response.status_code == 200
    locators = locators_response.json()
    assert any(item["chunk_id"] == "placeholder-chunk" and item["paper_id"] == placeholder_id for item in locators)

    with Session() as session:
        paper = session.get(Paper, UUID(placeholder_id))
        assert paper is not None
        locators = session.scalars(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper.id)).all()
        assert len(locators) == 1
        assert locators[0].chunk_id == "placeholder-chunk"
