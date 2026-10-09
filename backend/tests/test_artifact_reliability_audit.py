from __future__ import annotations

import os

import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, EvidenceLocator, Paper, PaperFigure, PaperTable
from app.db.session import get_db_session
from app.main import app
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService


@pytest.fixture
def audit_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        storage_root = root / "storage"
        monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        get_settings.cache_clear()

        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

        def override_get_db_session():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        yield root, storage_root, Session

        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def _write_png(path: Path, size: tuple[int, int]) -> None:
    Image = pytest.importorskip("PIL.Image")
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color=(32, 64, 96))
    image.save(path)


def _seed_reliability_cases(storage_root: Path, Session) -> tuple[str, dict[str, dict]]:
    figure_dir = storage_root / "figures"
    _write_png(figure_dir / "small.png", (120, 80))
    _write_png(figure_dir / "extreme.png", (1300, 180))
    _write_png(figure_dir / "normal.png", (420, 260))
    _write_png(figure_dir / "nobbox.png", (420, 260))
    _write_png(figure_dir / "nofull.png", (420, 260))
    _write_png(figure_dir / "page_003.png", (900, 1200))
    _write_png(figure_dir / "page_004.png", (900, 1200))
    _write_png(figure_dir / "page_005.png", (900, 1200))

    with Session() as session:
        paper = Paper(title="Artifact reliability paper", pdf_path="paper.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        figures = [
            PaperFigure(
                paper_id=paper.id,
                caption="Figure missing image.",
                page=1,
                image_path=None,
                crop_status="candidate_crop",
                prov=[{"bbox": {"l": 1, "t": 1, "r": 200, "b": 180}}],
            ),
            PaperFigure(
                paper_id=paper.id,
                caption="Figure caption only.",
                page=2,
                image_path=None,
                crop_status="caption_only",
                prov=[],
            ),
            PaperFigure(
                paper_id=paper.id,
                caption="Figure small crop.",
                page=3,
                image_path="small.png",
                crop_status="candidate_crop",
                crop_confidence=0.91,
                crop_source="docling_bbox",
                prov=[{
                    "bbox": {"l": 1, "t": 1, "r": 40, "b": 30},
                    "full_page_image_path": "page_003.png",
                    "pixel_size": {"width": 120, "height": 80},
                }],
            ),
            PaperFigure(
                paper_id=paper.id,
                caption="Figure extreme aspect.",
                page=4,
                image_path="extreme.png",
                crop_status="candidate_crop",
                prov=[{
                    "bbox": {"l": 1, "t": 1, "r": 320, "b": 90},
                    "full_page_image_path": "page_004.png",
                    "pixel_size": {"width": 1300, "height": 180},
                }],
            ),
            PaperFigure(
                paper_id=paper.id,
                caption="Figure missing bbox.",
                page=5,
                image_path="nobbox.png",
                crop_status="candidate_crop",
                prov=[{"full_page_image_path": "page_005.png"}],
            ),
            PaperFigure(
                paper_id=paper.id,
                caption="Figure missing full page snapshot.",
                page=6,
                image_path="nofull.png",
                crop_status="candidate_crop",
                prov=[{"bbox": {"l": 1, "t": 1, "r": 320, "b": 220}}],
            ),
        ]
        session.add_all(figures)
        table = PaperTable(
            paper_id=paper.id,
            caption="Table missing page and bbox.",
            markdown_content="| A | B |\n| - | - |",
            page=None,
            extraction_source="docling",
            prov=[],
        )
        session.add(table)
        locators = [
            EvidenceLocator(
                paper_id=paper.id,
                source_type="text",
                target_type="dft_results",
                target_id="row-text-only",
                field_name="value",
                evidence_text="Text-only evidence.",
                page=None,
                locator_status="text_only",
                locator_confidence=0.3,
                parser_source="test",
            ),
            EvidenceLocator(
                paper_id=paper.id,
                source_type="text",
                target_type="dft_results",
                target_id="row-missing-page",
                field_name="value",
                evidence_text="Missing page evidence.",
                page=None,
                locator_status="missing_page",
                locator_confidence=0.2,
                parser_source="test",
            ),
        ]
        session.add_all(locators)
        session.commit()
        paper_id = str(paper.id)
        snapshot = {
            "paper": {"workflow_status": paper.workflow_status},
            "figures": {
                str(item.id): {
                    "crop_status": item.crop_status,
                    "crop_confidence": item.crop_confidence,
                    "crop_source": item.crop_source,
                    "image_path": item.image_path,
                    "prov": item.prov,
                }
                for item in figures
            },
            "tables": {str(table.id): {"page": table.page, "prov": table.prov}},
            "locators": {
                str(item.id): {
                    "page": item.page,
                    "bbox": item.bbox,
                    "locator_status": item.locator_status,
                    "locator_confidence": item.locator_confidence,
                    "warning_reason": item.warning_reason,
                }
                for item in locators
            },
        }
        return paper_id, snapshot


def _current_snapshot(Session, paper_id: str) -> dict[str, dict]:
    paper_uuid = UUID(paper_id)
    with Session() as session:
        paper = session.get(Paper, paper_uuid)
        figures = session.query(PaperFigure).filter(PaperFigure.paper_id == paper_uuid).all()
        tables = session.query(PaperTable).filter(PaperTable.paper_id == paper_uuid).all()
        locators = session.query(EvidenceLocator).filter(EvidenceLocator.paper_id == paper_uuid).all()
        return {
            "paper": {"workflow_status": paper.workflow_status},
            "figures": {
                str(item.id): {
                    "crop_status": item.crop_status,
                    "crop_confidence": item.crop_confidence,
                    "crop_source": item.crop_source,
                    "image_path": item.image_path,
                    "prov": item.prov,
                }
                for item in figures
            },
            "tables": {str(item.id): {"page": item.page, "prov": item.prov} for item in tables},
            "locators": {
                str(item.id): {
                    "page": item.page,
                    "bbox": item.bbox,
                    "locator_status": item.locator_status,
                    "locator_confidence": item.locator_confidence,
                    "warning_reason": item.warning_reason,
                }
                for item in locators
            },
        }


def test_artifact_reliability_audit_counts_known_issues_and_is_read_only(audit_env):
    _, storage_root, Session = audit_env
    paper_id, before = _seed_reliability_cases(storage_root, Session)

    with Session() as session:
        service = ArtifactReliabilityAuditService(session, get_settings())
        report = service.audit_paper(UUID(paper_id))
        figure_summary = service.paper_figure_reliability_summary(UUID(paper_id))

    assert report["report_policy"]["read_only"] is True
    assert report["figure_count"] == 6
    assert report["table_count"] == 1
    assert report["locator_count"] == 2
    assert report["figure_issue_counts"]["missing_image"] == 2
    assert report["figure_issue_counts"]["caption_only"] == 2
    assert report["figure_issue_counts"]["small_crop"] == 1
    assert report["figure_issue_counts"]["extreme_aspect_ratio"] == 1
    assert report["figure_issue_counts"]["missing_bbox"] == 2
    assert report["figure_issue_counts"]["missing_full_page_snapshot"] == 3
    assert report["table_issue_counts"]["missing_page"] == 1
    assert report["table_issue_counts"]["missing_bbox"] == 1
    assert report["locator_issue_counts"]["text_only_locator"] == 1
    assert report["locator_issue_counts"]["missing_page"] == 1
    assert report["examples"]["small_crop"][0]["object_type"] == "figure"
    assert report["examples"]["text_only_locator"][0]["status"] == "text_only"
    assert figure_summary["status"] == "needs_review"
    assert figure_summary["issue_counts"]["missing_full_page_snapshot"] == 3
    assert figure_summary["issue_counts"]["small_crop"] == 1
    assert figure_summary["issue_count"] == sum(figure_summary["issue_counts"].values())
    assert _current_snapshot(Session, paper_id) == before


def test_artifact_reliability_audit_api_is_read_only(audit_env):
    _, storage_root, Session = audit_env
    paper_id, before = _seed_reliability_cases(storage_root, Session)

    client = TestClient(app)
    response = client.get(f"/api/workbench/papers/{paper_id}/artifact-reliability")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "artifact_reliability_audit_v1"
    assert payload["summary"]["status"] == "needs_review"
    assert payload["figure_issue_counts"]["missing_image"] == 2

    list_response = client.get("/api/workbench/artifact-reliability")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["metadata"]["read_only"] is True
    assert list_payload["metadata"]["search_scope"] == "all_libraries"
    assert list_payload["summary"]["paper_count"] == 1
    assert list_payload["summary"]["locator_issue_counts"]["missing_page"] == 1
    assert _current_snapshot(Session, paper_id) == before


def test_artifact_reliability_audit_filters_by_library_name(audit_env):
    _, _, Session = audit_env
    with Session() as session:
        paper_a = Paper(title="Library A artifact paper", pdf_path="a.pdf", library_name="库A")
        paper_b = Paper(title="Library B artifact paper", pdf_path="b.pdf", library_name="库B")
        session.add_all([paper_a, paper_b])
        session.flush()
        session.add_all(
            [
                EvidenceLocator(
                    paper_id=paper_a.id,
                    source_type="text",
                    target_type="dft_results",
                    target_id="a-row",
                    field_name="value",
                    evidence_text="Library A missing page evidence.",
                    page=None,
                    locator_status="missing_page",
                    locator_confidence=0.2,
                    parser_source="test",
                ),
                EvidenceLocator(
                    paper_id=paper_b.id,
                    source_type="text",
                    target_type="dft_results",
                    target_id="b-row",
                    field_name="value",
                    evidence_text="Library B missing page evidence.",
                    page=None,
                    locator_status="missing_page",
                    locator_confidence=0.2,
                    parser_source="test",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/workbench/artifact-reliability", params={"library_name": "库A"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["read_only"] is True
    assert payload["metadata"]["library_name"] == "库A"
    assert payload["metadata"]["search_scope"] == "library"
    assert payload["summary"]["paper_count"] == 1
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["title"] == "Library A artifact paper"
    assert payload["summary"]["locator_issue_counts"]["missing_page"] == 1
    assert all(row["title"] != "Library B artifact paper" for row in payload["rows"])


def test_locator_reliability_summary_classifies_core_statuses():
    exact = ArtifactReliabilityAuditService.locator_reliability_from_payload(
        {
            "page": 4,
            "bbox": {"l": 1, "t": 2, "r": 3, "b": 4},
            "locator_status": "exact_page",
            "locator_confidence": 0.94,
            "evidence_text": "Exact evidence.",
        }
    )
    assert exact["status"] == "reliable"
    assert exact["warnings"] == []
    assert exact["primary_locator"] == {
        "page": 4,
        "bbox": {"l": 1, "t": 2, "r": 3, "b": 4},
        "status": "exact_page",
        "confidence": 0.94,
    }

    text_only = ArtifactReliabilityAuditService.locator_reliability_from_payload(
        {"page": None, "bbox": None, "locator_status": "text_only", "evidence_text": "Text-only evidence."}
    )
    assert text_only["status"] == "text_only"
    assert text_only["warnings"] == ["text_only_locator"]

    missing_page = ArtifactReliabilityAuditService.locator_reliability_from_payload(
        {"page": None, "bbox": None, "locator_status": "missing_page", "evidence_text": "Missing page evidence."}
    )
    assert missing_page["status"] == "missing"
    assert missing_page["warnings"] == ["missing_page"]

    missing_bbox = ArtifactReliabilityAuditService.locator_reliability_from_payload(
        {"page": 5, "bbox": None, "locator_status": "exact_page", "evidence_text": "Page-only evidence."}
    )
    assert missing_bbox["status"] == "weak"
    assert missing_bbox["warnings"] == ["missing_bbox"]
