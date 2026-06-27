from __future__ import annotations

import csv
import io
import json

import pytest
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
    catalyst: CatalystSample | None = None,
    property_type: str = "adsorption_energy",
    adsorbate: str | None = "Li2S4",
    reaction_step: str = "Li2S4 adsorption",
    evidence_text: str = "Li2S4 adsorption is measured in the table.",
    value: float = -1.20,
    unit: str = "eV",
    evidence_payload: dict | None = None,
    with_setting: bool = True,
) -> DFTResult:
    if catalyst is None:
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
        adsorbate=adsorbate,
        property_type=property_type,
        value=value if complete else -1.35,
        unit=unit,
        reaction_step=reaction_step,
        evidence_text=evidence_text,
        evidence_payload=evidence_payload,
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
    assert payload["sample_quality"]["sample_unit"] == "active_site_instance"
    assert payload["sample_quality"]["counts"]["total_sample_count"] == 2
    assert payload["sample_quality"]["counts"]["missing_li2s_adsorption_sample_count"] == 2
    assert payload["sample_quality"]["counts"]["missing_li2s_barrier_sample_count"] == 2
    assert payload["sample_quality"]["counts"]["missing_rds_sample_count"] == 2
    assert payload["sample_quality"]["counts"]["missing_bader_or_charge_transfer_sample_count"] == 2
    assert "missing_li2s_adsorption_sample_count" in payload["sample_quality"]["gap_examples"]
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


def test_project_library_bundles_and_v4_export_are_read_only_with_energy_kind(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    active_site_key = "B0097:Fe-Co-N-C:site-1"
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S v4 ready", library_name="锂硫双原子", parsed=True)
        adsorption = _seed_dft(
            session,
            paper=paper,
            complete=True,
            adsorbate="Li2S",
            evidence_text="Table 1 reports Li2S adsorption energy of -1.10 eV.",
            value=-1.10,
            evidence_payload={
                "active_site_instance_key": active_site_key,
                "support_raw": "N-doped carbon",
                "support_normalized": "N-C",
                "support_confidence": 0.91,
                "source_text": "Li2S adsorption energy of -1.10 eV",
            },
        )
        catalyst = session.get(CatalystSample, adsorption.catalyst_sample_id)
        catalyst.name = "Fe-Co-N-C"
        catalyst.catalyst_type = "dual_atom"
        catalyst.metal_centers = ["Fe", "Co"]
        barrier = _seed_dft(
            session,
            paper=paper,
            complete=True,
            catalyst=catalyst,
            property_type="li2s_decomposition_barrier",
            adsorbate="Li2S",
            reaction_step="Li2S decomposition",
            evidence_text="Table 1 reports Li2S decomposition barrier of 0.65 eV.",
            value=0.65,
            evidence_payload={
                "active_site_instance_key": active_site_key,
                "support_raw": "N-doped carbon",
                "support_normalized": "N-C",
                "support_confidence": 0.91,
                "source_text": "Li2S decomposition barrier of 0.65 eV",
            },
            with_setting=False,
        )
        session.commit()
        adsorption_id = str(adsorption.id)
        barrier_id = str(barrier.id)

    client = TestClient(app)
    with SessionLocal() as session:
        before = _row_counts(session)

    bundles_response = client.get("/api/dft/project-library-bundles")
    export_response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "li2s_barrier"},
    )
    csv_response = client.get(
        "/api/dft/project-library-ml-export-v4.csv",
        params={"task": "li2s_barrier"},
    )
    record_csv_response = client.get(
        "/api/dft/project-library-ml-export-v4.csv",
        params={"task": "li2s_barrier", "unit": "record"},
    )

    with SessionLocal() as session:
        after = _row_counts(session)

    assert before == after
    assert bundles_response.status_code == 200
    bundles = bundles_response.json()
    assert bundles["read_only"] is True
    assert bundles["database_write_authority"] == "user_submit_only"
    assert bundles["ai_review_policy"]["ai_consensus_auto_adopt_allowed"] is False
    assert bundles["counts"]["catalyst_sample_count"] == 1
    assert bundles["counts"]["active_site_instance_count"] == 1
    assert bundles["counts"]["sample_with_li2s_adsorption_energy_count"] == 1
    assert bundles["counts"]["sample_with_li2s_decomposition_barrier_count"] == 1
    assert bundles["counts"]["energy_kind_thermodynamic_energy_count"] == 1
    assert bundles["counts"]["energy_kind_activation_barrier_count"] == 1

    instance = bundles["bundles"][0]["active_site_instances"][0]
    assert instance["active_site_instance_key"] == active_site_key
    assert instance["blockers"] == []
    assert {
        item["record_id"] for item in instance["properties"]["adsorbate_properties"]
    } == {adsorption_id}
    assert {
        item["record_id"] for item in instance["properties"]["reaction_step_properties"]
    } == {barrier_id}

    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["schema_version"] == "project_library_ml_export_v4"
    assert export_payload["read_only"] is True
    assert export_payload["manifest"]["source_export_versions_unchanged"] == [
        "dft_results_ml_v2",
        "dft_results_ml_v3",
    ]
    assert export_payload["manifest"]["task"] == "li2s_barrier"
    assert export_payload["manifest"]["ai_consensus_auto_adopt_allowed"] is False
    assert export_payload["manifest"]["sample_unit"] == "active_site_instance"
    assert export_payload["manifest"]["candidate_sample_count"] == 1
    assert export_payload["manifest"]["returned_sample_count"] == 1
    assert [row["record_id"] for row in export_payload["records"]] == [barrier_id]
    record = export_payload["records"][0]
    assert record["task"] == "li2s_barrier"
    assert record["label_name"] == "li2s_barrier_eV"
    assert record["label_value"] == 0.65
    assert record["label_unit"] == "eV"
    assert record["label_energy_kind"] == "activation_barrier"
    assert record["label_property_subtype"] == "li2s_decomposition_barrier"
    assert record["feature_scope"] == "reaction_step_property"
    assert record["active_site_instance_key"] == active_site_key
    assert record["energy_kind"] == "activation_barrier"
    assert record["property_subtype"] == "li2s_decomposition_barrier"
    assert record["support_raw"] == "N-doped carbon"
    assert record["support_normalized"] == "N-C"
    assert record["database_write_authority"] == "user_submit_only"
    assert record["ai_consensus_auto_adopt_allowed"] is False
    sample_record = export_payload["sample_records"][0]
    assert sample_record["sample_unit"] == "active_site_instance"
    assert sample_record["sample_id"] == active_site_key
    assert sample_record["active_site_instance_key"] == active_site_key
    assert sample_record["task"] == "li2s_barrier"
    assert sample_record["ml_ready"] is True
    assert sample_record["task_record_ids"] == [barrier_id]
    assert set(sample_record["source_record_ids"]) == {adsorption_id, barrier_id}
    assert sample_record["wide_properties"]["adsorption_energy_li2s_ev"] == -1.10
    assert sample_record["wide_properties"]["li2s_decomposition_barrier_ev"] == 0.65
    assert sample_record["property_group_counts"]["adsorbate_properties"] == 1
    assert sample_record["property_group_counts"]["reaction_step_properties"] == 1
    assert sample_record["property_groups"]["adsorbate_properties"][0]["record_id"] == adsorption_id
    assert sample_record["property_groups"]["reaction_step_properties"][0]["record_id"] == barrier_id
    assert sample_record["task_labels"] == [
        {
            "record_id": barrier_id,
            "label_name": "li2s_barrier_eV",
            "label_value": 0.65,
            "label_unit": "eV",
            "label_energy_kind": "activation_barrier",
            "label_property_subtype": "li2s_decomposition_barrier",
            "adsorbate": "Li2S",
            "reaction_step": "Li2S decomposition",
            "ml_ready": True,
            "blockers": [],
        }
    ]

    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert rows[0]["sample_id"] == active_site_key
    assert rows[0]["sample_unit"] == "active_site_instance"
    assert rows[0]["task"] == "li2s_barrier"
    assert json.loads(rows[0]["task_record_ids"]) == [barrier_id]
    assert set(json.loads(rows[0]["source_record_ids"])) == {adsorption_id, barrier_id}
    assert rows[0]["li2s_barrier_eV"] == "0.65"
    assert rows[0]["adsorption_energy_li2s_ev"] == "-1.1"
    assert rows[0]["li2s_decomposition_barrier_ev"] == "0.65"

    assert record_csv_response.status_code == 200
    record_rows = list(csv.DictReader(io.StringIO(record_csv_response.text)))
    assert record_csv_response.headers["content-disposition"] == (
        'attachment; filename="project_library_ml_export_v4_record_li2s_barrier.csv"'
    )
    assert [row["record_id"] for row in record_rows] == [barrier_id]
    assert record_rows[0]["label_name"] == "li2s_barrier_eV"
    assert record_rows[0]["label_value"] == "0.65"
    assert "sample_id" not in record_rows[0]


def test_project_library_v4_allows_generated_key_when_setting_binding_is_unambiguous(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S generated key ready", library_name="锂硫双原子", parsed=True)
        ready_row = _seed_dft(
            session,
            paper=paper,
            complete=True,
            adsorbate="Li2S",
            evidence_text="Li2S adsorption energy is -1.10 eV on the catalyst.",
            value=-1.10,
            evidence_payload={"source_text": "Li2S adsorption energy is -1.10 eV on the catalyst."},
        )
        missing_setting_paper = _seed_paper(
            session,
            title="Li-S generated key missing setting",
            library_name="锂硫双原子",
            parsed=True,
        )
        blocked_row = _seed_dft(
            session,
            paper=missing_setting_paper,
            complete=True,
            adsorbate="Li2S",
            evidence_text="Li2S adsorption energy is -1.35 eV on the catalyst.",
            value=-1.35,
            evidence_payload={"source_text": "Li2S adsorption energy is -1.35 eV on the catalyst."},
            with_setting=False,
        )
        session.commit()
        ready_row_id = str(ready_row.id)
        blocked_row_id = str(blocked_row.id)

    client = TestClient(app)
    response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "adsorption_energy", "ready_only": "false"},
    )
    assert response.status_code == 200, response.text
    records = {record["record_id"]: record for record in response.json()["records"]}

    ready_record = records[ready_row_id]
    assert ready_record["active_site_instance_key"].startswith("paper:")
    assert ready_record["active_site_ref"]["binding_source"] == "generated_read_only_bundle_key"
    assert ready_record["active_site_ref"]["dft_setting_ref"]["source"] == "singleton_paper_setting"
    assert ready_record["ml_ready"] is True
    assert "generated_active_site_instance_key" not in ready_record["blockers"]

    blocked_record = records[blocked_row_id]
    assert blocked_record["ml_ready"] is False
    assert "missing_result_setting_link" in blocked_record["blockers"]
    assert "generated_active_site_instance_key" in blocked_record["blockers"]


def test_project_library_v4_export_excludes_user_decision_candidates_from_ml_ready(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S v4 pending user", library_name="锂硫双原子", parsed=True)
        row = _seed_dft(
            session,
            paper=paper,
            complete=True,
            evidence_payload={
                "active_site_instance_key": "pending-user-site",
                "needs_user_decision": True,
                "source_text": "Li2S4 adsorption is -1.20 eV.",
            },
        )
        session.commit()
        row_id = str(row.id)

    client = TestClient(app)
    ready = client.get("/api/dft/project-library-ml-export-v4")
    all_rows = client.get("/api/dft/project-library-ml-export-v4", params={"ready_only": False})

    assert ready.status_code == 200
    assert ready.json()["records"] == []
    assert all_rows.status_code == 200
    payload = all_rows.json()
    assert [record["record_id"] for record in payload["records"]] == [row_id]
    assert payload["records"][0]["ml_ready"] is False
    assert "needs_user_decision" in payload["records"][0]["blockers"]


def test_project_library_v4_export_task_taxonomy_and_blockers(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S v4 taxonomy", library_name="锂硫双原子", parsed=True)
        adsorption_missing_adsorbate = _seed_dft(
            session,
            paper=paper,
            complete=True,
            adsorbate=None,
            evidence_text="Adsorption energy is -1.21 eV but adsorbate is omitted.",
            value=-1.21,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:ads",
                "source_text": "Adsorption energy is -1.21 eV.",
            },
        )
        li2s_dissociation = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="li2s_dissociation_energy",
            adsorbate="Li2S",
            reaction_step="Li2S dissociation",
            evidence_text="Li2S dissociation energy is 0.42 eV.",
            value=0.42,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:rxn",
                "energy_kind": "thermodynamic_energy",
                "source_text": "Li2S dissociation energy is 0.42 eV.",
            },
            with_setting=False,
        )
        li2s_barrier_mismatch = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="li2s_decomposition_barrier",
            adsorbate="Li2S",
            reaction_step="Li2S decomposition",
            evidence_text="Li2S decomposition barrier is 0.61 eV.",
            value=0.61,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:barrier",
                "energy_kind": "thermodynamic_energy",
                "source_text": "Li2S decomposition barrier is 0.61 eV.",
            },
            with_setting=False,
        )
        li2s_reaction_missing_step = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="reaction_energy",
            adsorbate=None,
            reaction_step=None,
            evidence_text="A generic reaction energy entry is present but the reaction step is missing.",
            value=0.55,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:reaction-missing-step",
                "energy_kind": "thermodynamic_energy",
                "source_text": "Reaction energy is 0.55 eV.",
            },
            with_setting=False,
        )
        li2s_barrier_missing_step = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="reaction_barrier",
            adsorbate=None,
            reaction_step=None,
            evidence_text="A generic barrier entry is present but the reaction step is missing.",
            value=0.73,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:barrier-missing-step",
                "energy_kind": "activation_barrier",
                "source_text": "Barrier is 0.73 eV.",
            },
            with_setting=False,
        )
        rds_free_energy = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="gibbs_free_energy_change",
            adsorbate=None,
            reaction_step="RDS",
            evidence_text="The RDS free energy is 0.33 eV.",
            value=0.33,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:rds",
                "energy_kind": "free_energy_change",
                "source_text": "The RDS free energy is 0.33 eV.",
            },
            with_setting=False,
        )
        rds_missing_step = _seed_dft(
            session,
            paper=paper,
            complete=True,
            property_type="gibbs_free_energy_change",
            adsorbate=None,
            reaction_step=None,
            evidence_text="Free energy is 0.29 eV but reaction step is missing.",
            value=0.29,
            evidence_payload={
                "active_site_instance_key": "taxonomy:site:rds-missing-step",
                "energy_kind": "free_energy_change",
                "source_text": "Free energy is 0.29 eV.",
            },
            with_setting=False,
        )
        session.commit()
        adsorption_missing_adsorbate_id = str(adsorption_missing_adsorbate.id)
        li2s_dissociation_id = str(li2s_dissociation.id)
        li2s_barrier_mismatch_id = str(li2s_barrier_mismatch.id)
        li2s_reaction_missing_step_id = str(li2s_reaction_missing_step.id)
        li2s_barrier_missing_step_id = str(li2s_barrier_missing_step.id)
        rds_free_energy_id = str(rds_free_energy.id)
        rds_missing_step_id = str(rds_missing_step.id)

    client = TestClient(app)

    adsorption_ready = client.get("/api/dft/project-library-ml-export-v4", params={"task": "adsorption_energy"})
    adsorption_all = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "adsorption_energy", "ready_only": "false"},
    )
    assert adsorption_ready.status_code == 200
    assert adsorption_ready.json()["records"] == []
    assert adsorption_all.status_code == 200
    adsorption_payload = adsorption_all.json()
    assert [record["record_id"] for record in adsorption_payload["records"]] == [adsorption_missing_adsorbate_id]
    assert adsorption_payload["records"][0]["task"] == "adsorption_energy"
    assert adsorption_payload["records"][0]["label_name"] == "adsorption_energy_eV"
    assert adsorption_payload["records"][0]["feature_scope"] == "adsorbate_property"
    assert "missing_adsorbate" in adsorption_payload["records"][0]["blockers"]
    assert adsorption_payload["manifest"]["candidate_count"] == 1
    assert adsorption_payload["manifest"]["returned_count"] == 1
    assert adsorption_payload["manifest"]["blocked_count"] == 1

    reaction_ready = client.get("/api/dft/project-library-ml-export-v4", params={"task": "li2s_reaction_energy"})
    reaction_all = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "li2s_reaction_energy", "ready_only": "false"},
    )
    assert reaction_ready.status_code == 200
    reaction_ready_payload = reaction_ready.json()
    assert [record["record_id"] for record in reaction_ready_payload["records"]] == [li2s_dissociation_id]
    assert reaction_ready_payload["records"][0]["label_energy_kind"] == "thermodynamic_energy"
    assert reaction_ready_payload["records"][0]["label_property_subtype"] == "li2s_dissociation_energy"
    reaction_all_payload = reaction_all.json()
    assert {record["record_id"] for record in reaction_all_payload["records"]} == {
        li2s_dissociation_id,
        li2s_barrier_mismatch_id,
        li2s_reaction_missing_step_id,
        li2s_barrier_missing_step_id,
    }
    mismatch_record = next(record for record in reaction_all_payload["records"] if record["record_id"] == li2s_barrier_mismatch_id)
    assert mismatch_record["ml_ready"] is False
    assert "energy_kind_task_mismatch" in mismatch_record["blockers"]
    missing_step_record = next(
        record for record in reaction_all_payload["records"] if record["record_id"] == li2s_reaction_missing_step_id
    )
    assert missing_step_record["ml_ready"] is False
    assert "missing_reaction_step" in missing_step_record["blockers"]
    barrier_missing_step_in_reaction = next(
        record for record in reaction_all_payload["records"] if record["record_id"] == li2s_barrier_missing_step_id
    )
    assert barrier_missing_step_in_reaction["ml_ready"] is False
    assert "missing_reaction_step" in barrier_missing_step_in_reaction["blockers"]
    assert "energy_kind_task_mismatch" in barrier_missing_step_in_reaction["blockers"]
    assert reaction_all_payload["manifest"]["blocker_counts"]["missing_reaction_step"] >= 1

    barrier_ready = client.get("/api/dft/project-library-ml-export-v4", params={"task": "li2s_barrier"})
    barrier_all = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "li2s_barrier", "ready_only": "false"},
    )
    assert barrier_ready.status_code == 200
    assert barrier_ready.json()["records"] == []
    barrier_all_payload = barrier_all.json()
    assert {record["record_id"] for record in barrier_all_payload["records"]} == {
        li2s_dissociation_id,
        li2s_barrier_mismatch_id,
        li2s_reaction_missing_step_id,
        li2s_barrier_missing_step_id,
    }
    barrier_mismatch_record = next(
        record for record in barrier_all_payload["records"] if record["record_id"] == li2s_dissociation_id
    )
    assert "energy_kind_task_mismatch" in barrier_mismatch_record["blockers"]
    barrier_missing_step_record = next(
        record for record in barrier_all_payload["records"] if record["record_id"] == li2s_barrier_missing_step_id
    )
    assert barrier_missing_step_record["ml_ready"] is False
    assert "missing_reaction_step" in barrier_missing_step_record["blockers"]
    reaction_missing_step_in_barrier = next(
        record for record in barrier_all_payload["records"] if record["record_id"] == li2s_reaction_missing_step_id
    )
    assert reaction_missing_step_in_barrier["ml_ready"] is False
    assert "missing_reaction_step" in reaction_missing_step_in_barrier["blockers"]
    assert "energy_kind_task_mismatch" in reaction_missing_step_in_barrier["blockers"]

    multitask = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "rds_srr_multitask", "ready_only": "false"},
    )
    assert multitask.status_code == 200
    multitask_payload = multitask.json()
    returned_ids = {record["record_id"] for record in multitask_payload["records"]}
    assert rds_free_energy_id in returned_ids
    rds_record = next(record for record in multitask_payload["records"] if record["record_id"] == rds_free_energy_id)
    assert rds_record["task"] == "rds_srr_multitask"
    assert rds_record["reaction_step"] == "RDS"
    assert rds_record["label_energy_kind"] == "free_energy_change"
    assert rds_record["label_property_subtype"] == "gibbs_free_energy_change"
    rds_missing_step_record = next(
        record for record in multitask_payload["records"] if record["record_id"] == rds_missing_step_id
    )
    assert rds_missing_step_record["ml_ready"] is False
    assert "missing_reaction_step" in rds_missing_step_record["blockers"]


def test_project_library_v4_user_submit_record_respects_task_contract(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S submit task contract", library_name="锂硫双原子", parsed=True)
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-N-C",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="N-doped carbon",
        )
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="reaction_energy",
            adsorbate=None,
            value=0.58,
            unit="eV",
            reaction_step="Li2S dissociation",
            reaction_type="SRR_LiS",
            evidence_text="Legacy reaction energy entry.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        run = _seed_external_run(session, paper=paper)
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "schema_version": "project_library_ml_export_v4",
                "project_library_context": "li_s_sac_dac",
                "database_write_authority": "user_submit_only",
                "ai_consensus_auto_adopt_allowed": False,
                "target_type": "dft_results",
                "target_id": str(row.id),
            },
            status="pending",
        )
        session.add(candidate)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)
        candidate_id = str(candidate.id)
        catalyst_id = str(catalyst.id)

    client = TestClient(app)
    submit_response = client.post(
        "/api/dft/project-library-v4/user-submit",
        json={
            "schema_version": "project_library_ml_export_v4",
            "context_key": "li_s_sac_dac",
            "paper_id": paper_id,
            "record_id": row_id,
            "database_write_authority": "user_submit_only",
            "ai_consensus_auto_adopt_allowed": False,
            "active_site_instance_key": f"paper:{paper_id}|catalyst:{catalyst_id}|site:li2s",
            "active_site_ref": {
                "paper_id": paper_id,
                "catalyst_sample_id": catalyst_id,
                "active_site_instance_key": f"paper:{paper_id}|catalyst:{catalyst_id}|site:li2s",
            },
            "catalyst_sample_id": catalyst_id,
            "property_type": "reaction_energy",
            "adsorbate": None,
            "reaction_step": "Li2S dissociation",
            "energy_kind": "activation_barrier",
            "value": 0.58,
            "unit": "eV",
            "source_text": "User confirmed this is a barrier-like record pending taxonomy cleanup.",
            "source_location": {"page": 7},
            "submitted_by": "human_reviewer",
            "source_candidate_ids": [candidate_id],
            "decision_status": "ready_for_submission",
        },
    )
    assert submit_response.status_code == 200, submit_response.text

    ready = client.get("/api/dft/project-library-ml-export-v4", params={"task": "li2s_reaction_energy", "paper_id": paper_id})
    all_rows = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "li2s_reaction_energy", "paper_id": paper_id, "ready_only": "false"},
    )
    assert ready.status_code == 200
    assert ready.json()["records"] == []
    assert all_rows.status_code == 200
    payload = all_rows.json()
    assert [record["record_id"] for record in payload["records"]] == [row_id]
    assert payload["records"][0]["ml_ready"] is False
    assert "energy_kind_task_mismatch" in payload["records"][0]["blockers"]


def test_project_library_v4_descriptors_and_structure_fields_are_postprocessed(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = _seed_paper(session, title="Li-S v4 descriptors", library_name="锂硫双原子", parsed=True)
        fe_adsorption = _seed_dft(
            session,
            paper=paper,
            complete=True,
            evidence_payload={
                "active_site_instance_key": "descriptor:site:fe",
                "source_text": "Li2S4 adsorption energy is -1.20 eV.",
                "adsorption_site": "Fe-top",
                "adsorption_mode": "end-on",
                "metal_ligand_distance_A": 1.92,
            },
        )
        fe_catalyst = session.get(CatalystSample, fe_adsorption.catalyst_sample_id)
        assert fe_catalyst is not None
        fe_co_catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-Co-N-C",
            catalyst_type="dual_atom",
            metal_centers=["Fe", "Co"],
            coordination="Fe-Co-N6",
            support="N-doped carbon",
        )
        session.add(fe_co_catalyst)
        session.flush()
        fe_co_barrier = _seed_dft(
            session,
            paper=paper,
            complete=True,
            catalyst=fe_co_catalyst,
            property_type="li2s_decomposition_barrier",
            adsorbate="Li2S",
            reaction_step="Li2S decomposition",
            evidence_text="Li2S decomposition barrier is 0.61 eV.",
            value=0.61,
            evidence_payload={
                "active_site_instance_key": "descriptor:site:feco",
                "source_text": "Li2S decomposition barrier is 0.61 eV.",
                "energy_kind": "activation_barrier",
                "metal_metal_distance_A": 2.37,
                "coordination_environment": "Fe-Co-N6",
                "adsorption_site": "bridge",
                "adsorption_mode": "bidentate",
            },
            with_setting=False,
        )
        _seed_dft(
            session,
            paper=paper,
            complete=True,
            catalyst=fe_co_catalyst,
            property_type="bader_charge",
            adsorbate="Li2S",
            reaction_step="Bader charge after Li2S adsorption",
            evidence_text="Bader charges of Fe and Co are 0.21 e and 0.18 e after Li2S adsorption.",
            value=0.21,
            unit="e",
            evidence_payload={
                "active_site_instance_key": "descriptor:site:feco",
                "source_text": "Bader charges of Fe and Co are 0.21 e and 0.18 e after Li2S adsorption.",
                "bader_charge_M1": 0.21,
                "bader_charge_M2": 0.18,
                "state_context": "after_Li2S_adsorption",
                "site_label": "M1",
            },
            with_setting=False,
        )
        _seed_dft(
            session,
            paper=paper,
            complete=True,
            catalyst=fe_co_catalyst,
            property_type="charge_transfer",
            adsorbate="Li2S",
            reaction_step="Charge transfer after Li2S adsorption",
            evidence_text="Charge transfer from catalyst to Li2S is 0.42 e.",
            value=0.42,
            unit="e",
            evidence_payload={
                "active_site_instance_key": "descriptor:site:feco",
                "source_text": "Charge transfer from catalyst to Li2S is 0.42 e.",
                "charge_transfer_e": 0.42,
                "charge_transfer_direction": "catalyst_to_adsorbate",
                "state_context": "after_Li2S_adsorption",
            },
            with_setting=False,
        )
        unknown_catalyst = CatalystSample(
            paper_id=paper.id,
            name="Xx-N-C",
            catalyst_type="single_atom",
            metal_centers=["Xx"],
            coordination="Xx-N4",
            support="carbon",
        )
        session.add(unknown_catalyst)
        session.flush()
        unknown_adsorption = _seed_dft(
            session,
            paper=paper,
            complete=True,
            catalyst=unknown_catalyst,
            evidence_payload={
                "active_site_instance_key": "descriptor:site:unknown",
                "source_text": "Li2S4 adsorption energy is -1.30 eV.",
                "adsorption_site": "Xx-top",
            },
            with_setting=False,
        )
        session.commit()
        paper_id = str(paper.id)
        fe_adsorption_id = str(fe_adsorption.id)
        fe_co_barrier_id = str(fe_co_barrier.id)
        unknown_adsorption_id = str(unknown_adsorption.id)

    client = TestClient(app)
    adsorption_response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "adsorption_energy", "ready_only": "false", "paper_id": paper_id},
    )
    assert adsorption_response.status_code == 200, adsorption_response.text
    adsorption_records = {record["record_id"]: record for record in adsorption_response.json()["records"]}
    fe_record = adsorption_records[fe_adsorption_id]
    assert fe_record["metal_1_descriptors"]["element_symbol"] == "Fe"
    assert fe_record["metal_1_descriptors"]["atomic_number"] == 26
    assert fe_record["metal_1_descriptors"]["electronegativity"] == 1.83
    assert fe_record["metal_1_descriptors"]["valence_electron_count"] == 8
    assert fe_record["metal_2_descriptors"] is None
    assert fe_record["dac_combined_descriptors"] is None
    assert fe_record["descriptor_blockers"] == []
    assert fe_record["metal_metal_distance_A"] is None
    assert fe_record["coordination_environment"] == "Fe-N4"
    assert fe_record["metal_ligand_distance_A"] == 1.92
    assert fe_record["adsorption_site"] == "Fe-top"
    assert fe_record["adsorption_mode"] == "end-on"
    assert "missing_metal_metal_distance" in fe_record["structure_blockers"]
    assert fe_record["ml_ready"] is True

    unknown_record = adsorption_records[unknown_adsorption_id]
    assert unknown_record["metal_1_descriptors"]["element_symbol"] == "Xx"
    assert unknown_record["metal_1_descriptors"]["atomic_number"] is None
    assert unknown_record["metal_1_descriptors"]["electronegativity"] is None
    assert unknown_record["metal_1_descriptors"]["valence_electron_count"] is None
    assert "unknown_metal_descriptor" in unknown_record["descriptor_blockers"]
    assert unknown_record["ml_ready"] is True

    barrier_response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"task": "li2s_barrier", "ready_only": "false", "paper_id": paper_id},
    )
    assert barrier_response.status_code == 200, barrier_response.text
    barrier_record = next(record for record in barrier_response.json()["records"] if record["record_id"] == fe_co_barrier_id)
    assert barrier_record["metal_1_descriptors"]["element_symbol"] == "Fe"
    assert barrier_record["metal_2_descriptors"]["element_symbol"] == "Co"
    assert barrier_record["dac_combined_descriptors"]["metal_pair_canonical"] == "Fe-Co"
    assert barrier_record["dac_combined_descriptors"]["atomic_number_delta"] == 1
    assert barrier_record["dac_combined_descriptors"]["atomic_number_mean"] == 26.5
    assert barrier_record["dac_combined_descriptors"]["electronegativity_delta"] == pytest.approx(0.05)
    assert barrier_record["dac_combined_descriptors"]["electronegativity_mean"] == pytest.approx(1.855)
    assert barrier_record["dac_combined_descriptors"]["valence_electron_count_delta"] == 1
    assert barrier_record["dac_combined_descriptors"]["valence_electron_count_mean"] == 8.5
    assert barrier_record["metal_metal_distance_A"] == 2.37
    assert barrier_record["coordination_environment"] == "Fe-Co-N6"
    assert barrier_record["adsorption_site"] == "bridge"
    assert barrier_record["adsorption_mode"] == "bidentate"
    assert barrier_record["structure_blockers"] == []
    assert barrier_record["bader_charge_M1"] is None
    assert barrier_record["ml_ready"] is True
    barrier_sample = barrier_response.json()["sample_records"][0]
    assert barrier_sample["wide_properties"]["bader_charge_M1_e"] == 0.21
    assert barrier_sample["wide_properties"]["bader_charge_M2_e"] == 0.18
    assert barrier_sample["wide_properties"]["charge_transfer_e"] == 0.42
    assert barrier_sample["property_group_counts"]["electronic_properties"] == 2

    v3_response = client.get(
        "/api/dft/ml-dataset-v3",
        params={"task": "adsorption_energy", "paper_id": paper_id},
    )
    assert v3_response.status_code == 200, v3_response.text
    assert v3_response.json()["manifest"]["schema_version"] == "dft_results_ml_v3"
