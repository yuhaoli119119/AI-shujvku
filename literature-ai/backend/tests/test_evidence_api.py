import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.models import Base, DFTResult, EvidenceClaim, EvidenceLocator, ExtractionFieldReview, Paper
from app.db.session import get_db_session
from app.main import app
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.evidence_locator_service import EvidenceLocatorService


def _override_session(engine):
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    return override_get_db_session


def _clear_engine_cache():
    from app.db.session import _engines, _session_factories

    for eng in list(_engines.values()):
        try:
            eng.dispose()
        except Exception:
            pass
    _engines.clear()
    _session_factories.clear()
    get_settings.cache_clear()


def test_claim_locator_returns_exact_bbox(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_exact.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Exact Locator", pdf_path="exact.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = str(paper.id)

        client = TestClient(app)
        create_response = client.post(
            "/api/evidence/claims",
            json={
                "claim_text": "A figure supports this claim.",
                "source_type": "manual",
                "target_type": "dft_results",
                "target_id": "result-1",
                "evidence": {
                    "paper_id": paper_id,
                    "chunk_id": "chunk-1",
                    "page_span": {"page_start": 4, "page_end": 4, "span_start": 12, "span_end": 48},
                    "evidence_text": "Figure 2 shows the adsorption energy trend.",
                    "confidence": 0.93,
                    "source": "figure",
                    "section_title": "Results",
                    "bbox": {
                        "x0": 10,
                        "y0": 20,
                        "x1": 110,
                        "y1": 140,
                        "width": 600,
                        "height": 800,
                        "coordinate_system": "pdf_points",
                    },
                    "parser_source": "pymupdf",
                },
            },
        )
        assert create_response.status_code == 200
        claim = create_response.json()
        claim_id = claim["id"]

        locator_response = client.get(f"/api/evidence/claims/{claim_id}/locator")
        assert locator_response.status_code == 200
        locator = locator_response.json()
        assert locator["locator_status"] == "exact_page"
        assert locator["provenance_level"] == "exact_pdf_page"
        assert locator["can_jump_to_pdf_page"] is True
        assert locator["can_highlight_in_pdf"] is False
        assert locator["page"] == 4
        assert locator["bbox"]["x0"] == 10.0
        assert locator["bbox"]["y1"] == 140.0
        assert locator["parser_source"] == "pymupdf"

        paper_locators = client.get(f"/api/papers/{paper_id}/evidence/locators")
        assert paper_locators.status_code == 200
        items = paper_locators.json()
        assert any(item["claim_id"] == claim_id and item["locator_status"] == "exact_page" for item in items)

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_claim_locator_page_only_fallback(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_page_only.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Page Only Locator", pdf_path="page-only.pdf", authors=[])
            session.add(paper)
            session.commit()
            paper_id = str(paper.id)

        client = TestClient(app)
        create_response = client.post(
            "/api/evidence/claims",
            json={
                "claim_text": "A paragraph supports this claim.",
                "source_type": "manual",
                "evidence": {
                    "paper_id": paper_id,
                    "chunk_id": "chunk-2",
                    "page_span": {"page_start": 2, "page_end": 2},
                    "evidence_text": "The catalyst remains stable after 200 cycles.",
                    "confidence": 0.88,
                    "source": "text",
                    "section_title": "Discussion",
                    "parser_source": "docling",
                },
            },
        )
        assert create_response.status_code == 200
        claim_id = create_response.json()["id"]

        locator_response = client.get(f"/api/evidence/claims/{claim_id}/locator")
        assert locator_response.status_code == 200
        locator = locator_response.json()
        assert locator["locator_status"] == "exact_page"
        assert locator["bbox"] is None
        assert locator["page"] == 2
        assert locator["can_jump_to_pdf_page"] is True
        assert locator["can_highlight_in_pdf"] is False

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_claim_locator_legacy_missing_or_text_only_fallback(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_legacy.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Legacy Locator", pdf_path="legacy.pdf", authors=[])
            session.add(paper)
            session.flush()
            claim = EvidenceClaim(
                claim_text="Legacy claim",
                source_type="manual",
                paper_id=paper.id,
                chunk_id="legacy-chunk",
                evidence_text="Legacy evidence without page metadata.",
                validation_status="supported",
            )
            session.add(claim)
            session.commit()
            claim_id = str(claim.id)
            paper_id = str(paper.id)

        client = TestClient(app)
        locator_response = client.get(f"/api/evidence/claims/{claim_id}/locator")
        assert locator_response.status_code == 200
        locator = locator_response.json()
        assert locator["locator_status"] in {"text_only", "missing_page"}
        assert locator["page"] is None
        assert locator["can_jump_to_pdf_page"] is False

        paper_locators = client.get(f"/api/papers/{paper_id}/evidence/locators")
        assert paper_locators.status_code == 200
        assert any(item["claim_id"] == claim_id for item in paper_locators.json())

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_extraction_results_include_evidence_locator_and_locator_api(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_extraction.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Extraction Locator", pdf_path="extraction.pdf", authors=[])
            session.add(paper)
            session.flush()
            result = DFTResult(
                paper_id=paper.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.45,
                unit="eV",
                source_section="Results",
                evidence_text="The adsorption energy of Li2S4 is -1.45 eV on Fe-N4.",
                confidence=0.91,
            )
            session.add(result)
            session.flush()
            EvidenceLocatorService(session).create_locator_for_span(
                paper_id=paper.id,
                object_type="dft_result",
                object_id=str(result.id),
                evidence_text=result.evidence_text,
                page=3,
                section="Results",
                figure=None,
                table=None,
                confidence=0.91,
                bbox=None,
                parser_source="docling",
                field_name="value",
            )
            session.commit()
            paper_id = str(paper.id)
            target_id = str(result.id)

        client = TestClient(app)
        results_response = client.get(f"/api/extraction/results/{paper_id}")
        assert results_response.status_code == 200
        payload = results_response.json()["results"]["DFTResult"][0]["value"]
        assert payload["value"] == -1.45
        assert payload["evidence_text"] == "The adsorption energy of Li2S4 is -1.45 eV on Fe-N4."
        assert payload["evidence_locator"]["locator_status"] == "exact_page"
        assert payload["evidence_locator"]["page"] == 3
        assert payload["evidence_locator"]["chunk_id"] == target_id

        locators_response = client.get(f"/api/extraction/results/{paper_id}/evidence-locators")
        assert locators_response.status_code == 200
        locators = locators_response.json()
        assert any(
            item["target_type"] == "dft_results"
            and item["target_id"] == target_id
            and item["field_name"] == "value"
            and item["locator_status"] == "exact_page"
            for item in locators
        )

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_stage2_replace_cleanup_removes_old_extraction_locators_but_keeps_reviews_and_claims(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_stage2_cleanup.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Locator Cleanup", pdf_path="cleanup.pdf", authors=[])
            session.add(paper)
            session.flush()
            old_target_id = str(uuid4())
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=old_target_id,
                field_name="value",
                original_value=-1.0,
                reviewed_value=-1.0,
                reviewer_status="verified",
                target_resolution_status="active",
            )
            old_locator = EvidenceLocator(
                paper_id=paper.id,
                claim_id=None,
                chunk_id=old_target_id,
                target_type="DFTResult",
                target_id=old_target_id,
                field_name="adsorption_energy",
                source_type="text",
                page=2,
                evidence_text="Old extraction locator.",
                locator_status="page_only",
                locator_confidence=0.7,
                parser_source="fallback",
            )
            claim = EvidenceClaim(
                claim_text="Manual claim survives Stage2 cleanup.",
                source_type="manual",
                paper_id=paper.id,
                chunk_id="manual-claim-chunk",
                evidence_text="Manual evidence remains attached to the paper.",
                validation_status="supported",
            )
            session.add_all([review, old_locator, claim])
            session.flush()
            claim_locator = EvidenceLocator(
                paper_id=paper.id,
                claim_id=claim.id,
                chunk_id="manual-claim-chunk",
                target_type="manual_claim",
                target_id="manual-claim",
                source_type="text",
                page=1,
                evidence_text="Manual evidence remains attached to the paper.",
                locator_status="page_only",
                locator_confidence=0.7,
                parser_source="fallback",
            )
            session.add(claim_locator)
            session.commit()
            paper_id = str(paper.id)
            paper_uuid = paper.id

        with Session() as session:
            paper = session.get(Paper, paper_uuid)
            assert paper is not None
            service = ExtractionPipelineService(session, Settings(storage_root=Path(".")))

            def fake_run_stage2(_paper, _document):
                result = DFTResult(
                    paper_id=_paper.id,
                    adsorbate="Li2S4",
                    property_type="adsorption_energy",
                    value=-1.25,
                    unit="eV",
                    source_section="Results",
                    evidence_text="The new adsorption energy is -1.25 eV.",
                    confidence=0.89,
                )
                session.add(result)
                session.flush()
                EvidenceLocatorService(session).create_locator_for_span(
                    paper_id=_paper.id,
                    object_type="dft_result",
                    object_id=str(result.id),
                    evidence_text=result.evidence_text,
                    page=4,
                    section="Results",
                    figure=None,
                    table=None,
                    confidence=0.89,
                    parser_source="docling",
                    field_name="value",
                )
                return {"dft_results": 1}

            service.run_stage2 = fake_run_stage2  # type: ignore[method-assign]
            service.replace_stage2(paper, SimpleNamespace())
            session.commit()
            new_target_id = str(session.query(DFTResult).filter(DFTResult.paper_id == paper.id).one().id)

        client = TestClient(app)
        locators_response = client.get(f"/api/papers/{paper_id}/evidence/locators")
        assert locators_response.status_code == 200
        locators = locators_response.json()
        assert not any(item["target_id"] == old_target_id and item["claim_id"] is None for item in locators)
        assert any(item["target_id"] == new_target_id and item["locator_status"] == "exact_page" for item in locators)
        assert any(item["claim_id"] is not None and item["target_id"] == "manual-claim" for item in locators)

        audit_response = client.get(f"/api/extraction/results/{paper_id}/reviews/audit")
        assert audit_response.status_code == 200
        assert audit_response.json()["total_reviews"] == 1
        reviews_response = client.get(f"/api/extraction/results/{paper_id}/reviews")
        assert reviews_response.status_code == 200
        reviews = reviews_response.json()
        assert reviews[0]["reviewer_status"] == "verified"
        assert reviews[0]["reviewed_value"] == -1.0

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_malformed_bbox_degrades_to_page_only(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_bad_bbox.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        Session = sessionmaker(bind=engine)
        with Session() as session:
            paper = Paper(title="Bad BBox", pdf_path="bad-bbox.pdf", authors=[])
            session.add(paper)
            session.flush()
            EvidenceLocatorService(session).create_locator_for_span(
                paper_id=paper.id,
                object_type="dft_result",
                object_id="bad-bbox-target",
                evidence_text=None,
                page=5,
                section="Results",
                figure=None,
                table=None,
                confidence=0.8,
                bbox={"x0": "bad", "y0": 0, "x1": 1, "y1": 1},
                parser_source="docling",
                field_name="value",
            )
            session.commit()
            paper_id = str(paper.id)

        client = TestClient(app)
        locators_response = client.get(f"/api/papers/{paper_id}/evidence/locators")
        assert locators_response.status_code == 200
        locator = next(item for item in locators_response.json() if item["target_id"] == "bad-bbox-target")
        assert locator["locator_status"] == "exact_page"
        assert locator["page"] == 5
        assert locator["bbox"] is None

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()


def test_missing_claim_locator_returns_404(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_evidence_missing_claim.db"
        db_url = f"sqlite:///{db_path}"
        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db_session] = _override_session(engine)

        client = TestClient(app)
        locator_response = client.get(f"/api/evidence/claims/{uuid4()}/locator")
        assert locator_response.status_code == 404
        assert locator_response.json()["detail"] == "Evidence claim not found"

        app.dependency_overrides.clear()
        engine.dispose()
        _clear_engine_cache()
