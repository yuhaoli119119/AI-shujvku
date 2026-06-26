from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditLog, CatalystSample, DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.main import app


def _seed_project_library_paper(session: Session) -> dict[str, str]:
    paper = Paper(
        title="Li-S submission paper",
        library_name="锂硫双原子",
        pdf_path="li-s-submission.pdf",
        workflow_status="Initial_Parsed",
    )
    session.add(paper)
    session.flush()
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
        property_type="adsorption_energy",
        adsorbate="Li2S4",
        value=-1.20,
        unit="eV",
        reaction_step="Li2S4 adsorption",
        reaction_type="SRR_LiS",
        evidence_text="Table 1 reports Li2S4 adsorption energy of -1.20 eV.",
        candidate_status="system_candidate",
    )
    session.add(row)
    session.flush()
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="ide_ai",
        source_label="li-s-submit-source",
        raw_payload={},
        normalized_payload={},
        mapping_status="mapped",
    )
    session.add(run)
    session.flush()
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
            "field_name": "value",
            "decision": "REVISE",
            "corrected_value": -1.55,
            "evidence_location": {"page": 5, "quoted_text": "-1.55 eV"},
        },
        status="pending",
    )
    session.add(candidate)
    session.commit()
    return {
        "paper_id": str(paper.id),
        "catalyst_id": str(catalyst.id),
        "row_id": str(row.id),
        "candidate_id": str(candidate.id),
    }


def _submit_payload(*, paper_id: str, catalyst_id: str, row_id: str, candidate_id: str) -> dict:
    return {
        "schema_version": "project_library_ml_export_v4",
        "context_key": "li_s_sac_dac",
        "paper_id": paper_id,
        "record_id": row_id,
        "database_write_authority": "user_submit_only",
        "ai_consensus_auto_adopt_allowed": False,
        "active_site_instance_key": f"paper:{paper_id}|catalyst:{catalyst_id}|site:fetop",
        "active_site_ref": {
            "paper_id": paper_id,
            "catalyst_sample_id": catalyst_id,
            "active_site_instance_key": f"paper:{paper_id}|catalyst:{catalyst_id}|site:fetop",
            "site_label": "Fe-top",
        },
        "catalyst_sample_id": catalyst_id,
        "property_type": "adsorption_energy",
        "adsorbate": "Li2S4",
        "reaction_step": "Li2S4 adsorption",
        "energy_kind": "thermodynamic_energy",
        "value": -1.55,
        "unit": "eV",
        "source_text": "Table 1 reports Li2S4 adsorption energy of -1.55 eV after user confirmation.",
        "source_location": {"page": 5, "table": "Table 1", "quoted_text": "-1.55 eV"},
        "submitted_by": "human_reviewer",
        "user_edits": {"value": {"from": -1.2, "to": -1.55}},
        "resolved_conflicts": [{"field_name": "value", "resolution": "user_selected_final_value"}],
        "source_candidate_ids": [candidate_id],
        "decision_status": "ready_for_submission",
        "confidence_level": 0.98,
        "support_raw": "N-doped carbon",
        "support_normalized": "N-doped carbon",
        "support_confidence": "high",
    }


def _add_candidate(
    session: Session,
    *,
    run_id,
    paper_id,
    candidate_type: str,
    normalized_payload: dict,
    status: str = "pending",
) -> str:
    candidate = ExternalAnalysisCandidate(
        run_id=run_id,
        paper_id=paper_id,
        candidate_type=candidate_type,
        normalized_payload=normalized_payload,
        status=status,
    )
    session.add(candidate)
    session.flush()
    return str(candidate.id)


def _counts(session: Session) -> dict[str, int]:
    return {
        "dft_results": session.scalar(select(func.count(DFTResult.id))) or 0,
        "external_candidates": session.scalar(select(func.count(ExternalAnalysisCandidate.id))) or 0,
        "audit_logs": session.scalar(select(func.count(AuditLog.id))) or 0,
    }


def test_project_library_v4_user_submit_preview_does_not_write(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        before = _counts(session)

    client = TestClient(app)
    response = client.post(
        "/api/dft/project-library-v4/user-submit/preview",
        json=_submit_payload(**seeded),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["action"] == "update_existing_dft_result"
    assert payload["can_submit"] is True
    assert payload["writes_to_database"] is False
    assert payload["database_write_authority"] == "user_submit_only"
    assert payload["hard_blockers"] == []

    with SessionLocal() as session:
        after = _counts(session)
    assert before == after


def test_project_library_v4_user_submit_writes_dft_result_and_audit_log(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        paper_id = seeded["paper_id"]
        row_id = seeded["row_id"]
        candidate_id = seeded["candidate_id"]

    client = TestClient(app)
    submit_response = client.post(
        "/api/dft/project-library-v4/user-submit",
        json=_submit_payload(**seeded),
    )
    assert submit_response.status_code == 200, submit_response.text
    submit_payload = submit_response.json()
    assert submit_payload["record_id"] == row_id
    assert submit_payload["action"] == "update_existing_dft_result"
    assert submit_payload["writes_to_database"] is True
    assert submit_payload["candidate_status"] == "final_user_submitted"
    assert submit_payload["visible_in_v4_export"] is True
    assert submit_payload["ready_only_export_eligible"] is False
    assert submit_payload["consumed_source_candidate_ids"] == [candidate_id]

    export_response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"paper_id": paper_id, "ready_only": "false"},
    )
    assert export_response.status_code == 200, export_response.text
    export_payload = export_response.json()
    export_record = next(item for item in export_payload["records"] if item["record_id"] == row_id)
    assert export_record["value"] == -1.55
    assert export_record["active_site_instance_key"].endswith("site:fetop")
    assert export_record["database_write_authority"] == "user_submit_only"
    assert export_record["energy_kind"] == "thermodynamic_energy"
    assert export_record["metal_1_descriptors"]["element_symbol"] == "Fe"
    assert export_record["metal_1_descriptors"]["atomic_number"] == 26
    assert export_record["metal_2_descriptors"] is None
    assert export_record["dac_combined_descriptors"] is None
    assert export_record["descriptor_blockers"] == []
    assert export_record["coordination_environment"] == "Fe-N4"
    assert export_record["adsorption_site"] == "Fe-top"
    assert "missing_metal_metal_distance" in export_record["structure_blockers"]

    v3_response = client.get(
        "/api/dft/ml-dataset-v3",
        params={"task": "adsorption_energy", "paper_id": paper_id},
    )
    assert v3_response.status_code == 200, v3_response.text
    v3_payload = v3_response.json()
    assert v3_payload["manifest"]["schema_version"] == "dft_results_ml_v3"
    assert v3_payload["manifest"]["filters"]["paper_id"] == paper_id

    with SessionLocal() as session:
        stored_row = session.get(DFTResult, UUID(row_id))
        assert stored_row is not None
        assert stored_row.value == -1.55
        assert stored_row.candidate_status == "final_user_submitted"
        assert stored_row.evidence_payload["submitted_by_user"] is True
        assert stored_row.evidence_payload["schema_version"] == "project_library_ml_export_v4"
        assert stored_row.evidence_payload["database_write_authority"] == "user_submit_only"
        stored_candidate = session.get(ExternalAnalysisCandidate, UUID(candidate_id))
        assert stored_candidate is not None
        assert stored_candidate.status == "user_submitted"
        assert stored_candidate.materialized_target_type == "dft_results"
        assert stored_candidate.materialized_target_id == row_id
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.paper_id == UUID(paper_id),
                AuditLog.action == "project_library_v4_user_submit",
                AuditLog.target_id == row_id,
            )
        )
        assert audit is not None


def test_project_library_v4_user_submit_rejects_ambiguous_status(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        before = _counts(session)
        payload = _submit_payload(**seeded)
        payload["decision_status"] = "ambiguous"

    client = TestClient(app)
    preview = client.post("/api/dft/project-library-v4/user-submit/preview", json=payload)
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["can_submit"] is False
    assert "needs_user_decision" in preview_payload["hard_blockers"]

    submit = client.post("/api/dft/project-library-v4/user-submit", json=payload)
    assert submit.status_code == 422, submit.text
    detail = submit.json()["detail"]
    assert detail["code"] == "project_library_v4_submit_blocked"
    assert "needs_user_decision" in detail["hard_blockers"]

    with SessionLocal() as session:
        after = _counts(session)
        stored_row = session.get(DFTResult, UUID(seeded["row_id"]))
        assert stored_row is not None
        assert stored_row.candidate_status == "system_candidate"
    assert before == after


def test_project_library_v4_export_prefers_user_submitted_energy_kind(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        row = session.get(DFTResult, UUID(seeded["row_id"]))
        assert row is not None
        row.property_type = "reaction_energy"
        row.adsorbate = None
        row.reaction_step = "Li2S dissociation"
        session.commit()
        payload = _submit_payload(**seeded)
        payload["property_type"] = "reaction_energy"
        payload["adsorbate"] = None
        payload["reaction_step"] = "Li2S dissociation"
        payload["energy_kind"] = "activation_barrier"

    client = TestClient(app)
    submit = client.post("/api/dft/project-library-v4/user-submit", json=payload)
    assert submit.status_code == 200, submit.text

    export_response = client.get(
        "/api/dft/project-library-ml-export-v4",
        params={"paper_id": seeded["paper_id"], "task": "srr_multitask", "ready_only": "false"},
    )
    assert export_response.status_code == 200, export_response.text
    export_payload = export_response.json()
    export_record = next(item for item in export_payload["records"] if item["record_id"] == seeded["row_id"])
    assert export_record["property_type"] == "reaction_energy"
    assert export_record["energy_kind"] == "activation_barrier"


def test_project_library_v4_user_submit_rejects_invalid_source_candidate(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        run_id = session.scalar(
            select(ExternalAnalysisRun.id).where(ExternalAnalysisRun.paper_id == UUID(seeded["paper_id"]))
        )
        assert run_id is not None
        invalid_candidate_id = _add_candidate(
            session,
            run_id=run_id,
            paper_id=UUID(seeded["paper_id"]),
            candidate_type="object_review_audit",
            normalized_payload={
                "schema_version": "external_analysis_v1",
                "project_library_context": "li_s_sac_dac",
                "database_write_authority": "user_submit_only",
                "ai_consensus_auto_adopt_allowed": False,
                "target_type": "paper",
                "target_id": seeded["paper_id"],
            },
        )
        session.commit()
        before = _counts(session)
        payload = _submit_payload(**seeded)
        payload["source_candidate_ids"] = [invalid_candidate_id]

    client = TestClient(app)
    preview = client.post("/api/dft/project-library-v4/user-submit/preview", json=payload)
    assert preview.status_code == 422, preview.text
    preview_detail = preview.json()["detail"]
    assert preview_detail["code"] == "project_library_v4_submit_blocked"
    assert "invalid_source_candidate_for_project_library_v4" in preview_detail["hard_blockers"]

    submit = client.post("/api/dft/project-library-v4/user-submit", json=payload)
    assert submit.status_code == 422, submit.text
    detail = submit.json()["detail"]
    assert detail["code"] == "project_library_v4_submit_blocked"
    assert "invalid_source_candidate_for_project_library_v4" in detail["hard_blockers"]

    with SessionLocal() as session:
        after = _counts(session)
        stored_row = session.get(DFTResult, UUID(seeded["row_id"]))
        invalid_candidate = session.get(ExternalAnalysisCandidate, UUID(invalid_candidate_id))
        assert stored_row is not None
        assert stored_row.candidate_status == "system_candidate"
        assert invalid_candidate is not None
        assert invalid_candidate.status == "pending"
        assert invalid_candidate.materialized_target_type is None
        assert invalid_candidate.materialized_target_id is None
    assert before == after


def test_project_library_v4_user_submit_requires_source_text(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        seeded = _seed_project_library_paper(session)
        before = _counts(session)
        payload = _submit_payload(**seeded)
        payload["source_text"] = ""

    client = TestClient(app)
    preview = client.post("/api/dft/project-library-v4/user-submit/preview", json=payload)
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["can_submit"] is False
    assert "missing_source_text" in preview_payload["hard_blockers"]

    submit = client.post("/api/dft/project-library-v4/user-submit", json=payload)
    assert submit.status_code == 422, submit.text
    detail = submit.json()["detail"]
    assert "missing_source_text" in detail["hard_blockers"]

    with SessionLocal() as session:
        after = _counts(session)
        stored_row = session.get(DFTResult, UUID(seeded["row_id"]))
        assert stored_row is not None
        assert stored_row.evidence_text == "Table 1 reports Li2S4 adsorption energy of -1.20 eV."
    assert before == after
