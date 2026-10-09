from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import CatalystSample, DFTResult, DFTSetting, EvidenceSpan, ExtractionFieldReview, Paper
from app.main import app
from app.domain.project_library_context import get_project_library_context
from app.services.project_library_queue_service import ProjectLibraryQueueService


def _seed_paper(session: Session, *, title: str, library_name: str = "锂硫双原子", parsed: bool = False) -> Paper:
    paper = Paper(
        title=title,
        library_name=library_name,
        pdf_path=f"{title}.pdf",
        markdown_path=f"{title}.md" if parsed else None,
        docling_json_path=f"{title}.json" if parsed else None,
    )
    session.add(paper)
    session.flush()
    return paper


def _seed_dft(
    session: Session,
    *,
    paper: Paper,
    complete: bool = True,
    with_setting: bool = True,
    property_type: str = "adsorption_energy",
) -> DFTResult:
    catalyst = CatalystSample(
        paper_id=paper.id,
        name="Fe-N-C",
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        coordination="Fe-N4" if complete else None,
        support="carbon",
    )
    session.add(catalyst)
    session.flush()
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id,
        property_type=property_type,
        adsorbate="Li2S4",
        value=-1.20,
        unit="eV",
        reaction_step="Li2S4 adsorption",
        evidence_text="Li2S4 adsorption is -1.20 eV.",
        reaction_type="SRR_LiS",
        reaction_profile_version="reaction_profiles_v1",
        reaction_validation_status="valid",
    )
    session.add(row)
    session.flush()
    session.add(
        ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="active",
            evidence_text=row.evidence_text,
        )
    )
    session.add(
        EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_result",
            object_id=str(row.id),
            text=row.evidence_text,
            page=5,
        )
    )
    if with_setting:
        session.add(DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"))
    return row


def test_project_library_queue_service_is_read_only_and_classifies_states(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        imported = _seed_paper(session, title="Imported only", parsed=False)
        parsed = _seed_paper(session, title="Parsed only", parsed=True)
        needs_fields = _seed_paper(session, title="Needs fields", parsed=True)
        training_ready = _seed_paper(session, title="Training ready", parsed=True)
        _seed_dft(session, paper=needs_fields, complete=False)
        _seed_dft(session, paper=training_ready, complete=True)
        session.commit()

    with SessionLocal() as session:
        before = {
            "papers": session.scalar(select(func.count(Paper.id))) or 0,
            "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))) or 0,
        }
        payload = ProjectLibraryQueueService(session).build_queue(context_key="li_s_sac_dac", library_name="锂硫双原子")
        after = {
            "papers": session.scalar(select(func.count(Paper.id))) or 0,
            "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))) or 0,
        }

    assert before == after
    assert payload["read_only"] is True
    assert payload["auto_verification_applied"] is False
    assert payload["counts"]["paper_count"] == 4
    assert payload["counts"]["parsed_count"] == 3
    assert payload["counts"]["with_dft_count"] == 2
    assert payload["counts"]["pending_review_count"] == 1
    assert payload["counts"]["export_ready_count"] == 2
    assert payload["counts"]["training_ready_count"] == 1
    assert payload["counts"]["needs_fields_count"] == 1

    by_title = {item["title"]: item for item in payload["papers"]}
    assert by_title["Imported only"]["dominant_state"] == "imported"
    assert by_title["Parsed only"]["dominant_state"] == "parsed"
    assert by_title["Needs fields"]["needs_fields"] is True
    assert by_title["Needs fields"]["training_ready"] is False
    assert "missing_coordination" in by_title["Needs fields"]["blocker_counts"]
    assert by_title["Training ready"]["training_ready"] is True
    assert by_title["Training ready"]["pending_review"] is False


def test_project_library_context_exposes_default_library_name() -> None:
    context = get_project_library_context("li_s_sac_dac")

    assert context.default_library_name == "锂硫双原子"


def test_project_library_queue_defaults_to_li_s_library_when_library_name_is_omitted(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        li_s_paper = _seed_paper(session, title="Li-S ready", library_name="锂硫双原子", parsed=True)
        other_paper = _seed_paper(session, title="Other ready", library_name="其它文献库", parsed=True)
        _seed_dft(session, paper=li_s_paper, complete=True)
        _seed_dft(session, paper=other_paper, complete=True)
        session.commit()
        before = {
            "papers": session.scalar(select(func.count(Paper.id))) or 0,
            "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))) or 0,
        }

    client = TestClient(app)
    default_response = client.get("/api/dft/project-library-queue", params={"context_key": "li_s_sac_dac"})
    assert default_response.status_code == 200
    default_payload = default_response.json()
    assert default_payload["context_key"] == "li_s_sac_dac"
    assert default_payload["library_name"] == "锂硫双原子"
    assert default_payload["read_only"] is True
    assert default_payload["auto_verification_applied"] is False
    assert default_payload["counts"]["training_ready_count"] == 1
    assert [item["title"] for item in default_payload["papers"]] == ["Li-S ready"]

    explicit_response = client.get(
        "/api/dft/project-library-queue",
        params={"context_key": "li_s_sac_dac", "library_name": "其它文献库"},
    )
    assert explicit_response.status_code == 200
    explicit_payload = explicit_response.json()
    assert explicit_payload["library_name"] == "其它文献库"
    assert explicit_payload["read_only"] is True
    assert explicit_payload["auto_verification_applied"] is False
    assert explicit_payload["counts"]["training_ready_count"] == 1
    assert [item["title"] for item in explicit_payload["papers"]] == ["Other ready"]

    post_response = client.post("/api/dft/project-library-queue", json={})
    assert post_response.status_code == 405

    with SessionLocal() as session:
        after = {
            "papers": session.scalar(select(func.count(Paper.id))) or 0,
            "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))) or 0,
        }
    assert before == after


def test_project_library_queue_api_rejects_unknown_context(setup_test_db):
    response = TestClient(app).get(
        "/api/dft/project-library-queue",
        params={"context_key": "unknown-context"},
    )
    assert response.status_code == 422
    assert "Unknown project library context" in response.json()["detail"]
