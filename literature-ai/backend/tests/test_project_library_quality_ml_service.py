from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    Paper,
)
from app.main import app


def _seed_paper(session: Session, *, title: str, library_name: str, parsed: bool = True) -> Paper:
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
    property_type: str = "adsorption_energy",
    reaction_step: str = "Li2S4 adsorption",
    evidence_text: str = "Li2S4 adsorption is measured in the table.",
    value: float = -1.20,
) -> DFTResult:
    catalyst = CatalystSample(
        paper_id=paper.id,
        name="Fe-N-C" if complete else "Co-N-C",
        catalyst_type="single_atom",
        metal_centers=["Fe"] if complete else ["Co"],
        coordination="Fe-N4" if complete else None,
        support="carbon",
    )
    session.add(catalyst)
    session.flush()
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id,
        adsorbate="Li2S4",
        property_type=property_type,
        value=value if complete else -1.35,
        unit="eV",
        reaction_step=reaction_step,
        evidence_text=evidence_text,
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
    session.add(DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"))
    return row


def _seed_external_run(session: Session, *, paper: Paper) -> ExternalAnalysisRun:
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="pytest",
        source_label="project_library_quality_ml_service_test",
        normalized_payload={},
        mapping_status="pending",
    )
    session.add(run)
    session.flush()
    return run


def _row_counts(session: Session) -> dict[str, int]:
    return {
        "papers": session.scalar(select(func.count(Paper.id))) or 0,
        "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
        "external_candidates": session.scalar(select(func.count(ExternalAnalysisCandidate.id))) or 0,
        "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))) or 0,
    }


def test_project_library_quality_defaults_to_li_s_library_and_reports_blockers(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        li_s_ready = _seed_paper(session, title="Li-S ready", library_name="锂硫双原子", parsed=True)
        li_s_blocked = _seed_paper(session, title="Li-S blocked", library_name="锂硫双原子", parsed=True)
        other_ready = _seed_paper(session, title="Other ready", library_name="其它文献库", parsed=True)
        _seed_dft(session, paper=li_s_ready, complete=True)
        _seed_dft(session, paper=li_s_blocked, complete=False)
        _seed_dft(session, paper=other_ready, complete=True)
        li_s_run = _seed_external_run(session, paper=li_s_blocked)
        other_run = _seed_external_run(session, paper=other_ready)
        session.add_all(
            [
                ExternalAnalysisCandidate(
                    run_id=li_s_run.id,
                    paper_id=li_s_blocked.id,
                    candidate_type="experimental_performance",
                    normalized_payload={
                        "experimental_performance": {
                            "specific_capacity": {"value": 4.2, "unit": "mAh cm^-2"},
                            "rate_c_value": "500 mA g^-1",
                        }
                    },
                    status="pending",
                ),
                ExternalAnalysisCandidate(
                    run_id=other_run.id,
                    paper_id=other_ready.id,
                    candidate_type="experimental_performance",
                    normalized_payload={
                        "experimental_performance": {
                            "specific_capacity": {"value": 1300, "unit": "mAh g^-1"},
                            "rate_c_value": {"value": 0.2, "unit": "C"},
                        }
                    },
                    status="pending",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    with SessionLocal() as session:
        before = _row_counts(session)

    response = client.get("/api/dft/project-library-quality")
    assert response.status_code == 200
    payload = response.json()

    with SessionLocal() as session:
        after = _row_counts(session)

    assert before == after
    assert payload["context_key"] == "li_s_sac_dac"
    assert payload["library_name"] == "锂硫双原子"
    assert payload["read_only"] is True
    assert payload["auto_verification_applied"] is False
    assert payload["counts"]["paper_count"] == 2
    assert payload["counts"]["parsed_count"] == 2
    assert payload["counts"]["with_dft_count"] == 2
    assert payload["counts"]["srr_lis_task_candidate_count"] == 2
    assert payload["counts"]["label_ready_count"] == 2
    assert payload["counts"]["training_ready_count"] == 1
    assert payload["counts"]["needs_fields_count"] == 1
    assert payload["counts"]["feature_candidate_blocked_paper_count"] == 1
    assert payload["blocker_counts"]["missing_coordination"] == 1
    assert payload["feature_candidate_blocker_counts"]["unsupported_specific_capacity_unit"] == 1
    assert payload["feature_candidate_blocker_counts"]["rate_requires_conversion"] == 1
    assert [item["title"] for item in payload["needs_fields_papers"]] == ["Li-S blocked"]
    assert payload["needs_fields_papers"][0]["feature_candidate_blocker_counts"]["unsupported_specific_capacity_unit"] == 1


def test_project_library_ml_export_defaults_to_li_s_library_and_reports_insufficient_baseline(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        li_s_ready = _seed_paper(session, title="Li-S ready", library_name="锂硫双原子", parsed=True)
        other_ready_a = _seed_paper(session, title="Other ready A", library_name="其它文献库", parsed=True)
        other_ready_b = _seed_paper(session, title="Other ready B", library_name="其它文献库", parsed=True)
        _seed_dft(session, paper=li_s_ready, complete=True)
        _seed_dft(session, paper=other_ready_a, complete=True)
        _seed_dft(session, paper=other_ready_b, complete=True)
        session.commit()

    client = TestClient(app)
    with SessionLocal() as session:
        before = _row_counts(session)

    summary = client.get("/api/dft/project-library-ml-export")
    assert summary.status_code == 200
    payload = summary.json()

    csv_response = client.get("/api/dft/project-library-ml-export.csv")
    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))

    with SessionLocal() as session:
        after = _row_counts(session)

    assert before == after
    assert payload["context_key"] == "li_s_sac_dac"
    assert payload["library_name"] == "锂硫双原子"
    assert payload["task"] == "SRR_LiS:adsorption_energy"
    assert payload["read_only"] is True
    assert payload["auto_verification_applied"] is False
    assert payload["status"] == "not_ready"
    assert payload["ready_for_baseline"] is False
    assert "insufficient_data" in payload["blockers"]
    assert payload["candidate_manifest"]["task_candidate_count"] == 1
    assert payload["training_manifest"]["returned_count"] == 1
    assert payload["baseline"]["status"] == "insufficient"
    assert payload["baseline"]["n_rows"] == 1
    assert [row["title"] for row in rows] == ["Li-S ready"]

    explicit = client.get(
        "/api/dft/project-library-ml-export",
        params={"library_name": "其它文献库"},
    )
    assert explicit.status_code == 200
    explicit_payload = explicit.json()
    assert explicit_payload["library_name"] == "其它文献库"
    assert explicit_payload["candidate_manifest"]["task_candidate_count"] == 2


def test_project_library_ml_export_accepts_rds_gibbs_free_energy_task(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        li_s_ready = _seed_paper(session, title="Li-S RDS ready", library_name="锂硫双原子", parsed=True)
        _seed_dft(
            session,
            paper=li_s_ready,
            complete=True,
            property_type="gibbs_free_energy_change",
            reaction_step="RDS",
            evidence_text="The Gibbs free energy of the rate-determining step is 0.34 eV.",
            value=0.34,
        )
        session.commit()

    client = TestClient(app)
    summary = client.get(
        "/api/dft/project-library-ml-export",
        params={"task": "rds_gibbs_free_energy"},
    )
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["task"] == "SRR_LiS:rds_gibbs_free_energy"
    assert payload["candidate_manifest"]["task"] == "SRR_LiS:rds_gibbs_free_energy"
    assert payload["candidate_manifest"]["task_candidate_count"] == 1

    csv_response = client.get(
        "/api/dft/project-library-ml-export.csv",
        params={"task": "rds_gibbs_free_energy"},
    )
    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert [row["title"] for row in rows] == ["Li-S RDS ready"]
    assert rows[0]["canonical_property_type"] == "gibbs_free_energy_change"
    assert rows[0]["property_subtype"] == "gibbs_free_energy_change"
    assert rows[0]["reaction_step"] == "RDS"
