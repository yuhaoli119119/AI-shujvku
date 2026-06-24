from __future__ import annotations

import copy
import csv
import io

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import CatalystSample, DFTResult, DFTSetting, EvidenceSpan, ExtractionFieldReview, Paper
from app.main import app
from app.schemas.dft_export import DFTMLDatasetExportV2, DFTMLDatasetExportV3, select_training_records_v3
from app.services.dft_export_service import build_dft_ml_dataset, build_dft_ml_dataset_v3


def _seed(
    session: Session,
    *,
    complete: bool = True,
    year: int = 2025,
    property_type: str = "adsorption_energy",
    reaction_step: str = "Li2S4 adsorption",
    evidence_text: str = "Li2S4 adsorption is -1.2 eV.",
    value: float = -1.2,
) -> DFTResult:
    paper = Paper(title=f"V3 API paper {year} {complete}", year=year, pdf_path="paper.pdf", authors=["A"])
    session.add(paper)
    session.flush()
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
        value=value,
        unit="eV",
        reaction_step=reaction_step,
        evidence_text=evidence_text,
        reaction_type="SRR_LiS",
        reaction_profile_version="reaction_profiles_v1",
        reaction_validation_status="valid",
    )
    session.add(row)
    session.flush()
    session.add_all(
        [
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                reviewer_status="verified",
                target_resolution_status="active",
                evidence_text=row.evidence_text,
            ),
            EvidenceSpan(
                paper_id=paper.id,
                object_type="dft_result",
                object_id=str(row.id),
                text=row.evidence_text,
                page=4,
            ),
            DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"),
        ]
    )
    return row


def test_v3_api_empty_dataset_and_required_task(setup_test_db):
    client = TestClient(app)
    missing = client.get("/api/dft/ml-dataset-v3")
    assert missing.status_code == 422

    response = client.get("/api/dft/ml-dataset-v3", params={"task": "adsorption_energy"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["records"] == []
    assert payload["manifest"]["schema_version"] == "dft_results_ml_v3"
    assert payload["manifest"]["task_status"] == "candidate"
    assert payload["manifest"]["property_type_fields"] == [
        "property_type",
        "normalized_property_type",
        "canonical_property_type",
        "property_subtype",
    ]
    DFTMLDatasetExportV3.model_validate(payload)
    barrier = client.get("/api/dft/ml-dataset-v3", params={"task": "reaction_barrier"})
    assert barrier.status_code == 200
    assert barrier.json()["manifest"]["task_status"] == "candidate"
    rds = client.get("/api/dft/ml-dataset-v3", params={"task": "rds_gibbs_free_energy"})
    assert rds.status_code == 200
    assert rds.json()["manifest"]["task"] == "SRR_LiS:rds_gibbs_free_energy"


def test_v3_api_valid_record_schema_selector_and_v2_regression(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        row = _seed(session)
        session.commit()
        service_payload = build_dft_ml_dataset_v3(session, task="adsorption_energy")
        parsed = DFTMLDatasetExportV3.model_validate(service_payload)
        assert [record.record_id for record in select_training_records_v3(parsed)] == [str(row.id)]
        unexpected = copy.deepcopy(service_payload)
        unexpected["records"][0]["unexpected"] = True
        with pytest.raises(ValidationError):
            DFTMLDatasetExportV3.model_validate(unexpected)
        mismatched = copy.deepcopy(service_payload)
        mismatched["metadata"]["task"] = "SRR_LiS:reaction_barrier"
        mismatched["manifest"]["task"] = "SRR_LiS:reaction_barrier"
        assert select_training_records_v3(mismatched) == []
        DFTMLDatasetExportV2.model_validate(build_dft_ml_dataset(session))

    client = TestClient(app)
    response = client.get("/api/dft/ml-dataset-v3", params={"task": "adsorption_energy"})
    assert response.status_code == 200
    record = response.json()["records"][0]
    assert record["provenance"]["page_locators"] == [4]
    assert record["task_profile"] == "SRR_LiS:adsorption_energy"
    assert record["target"]["property_type"] == "adsorption_energy"
    assert record["target"]["normalized_property_type"] == "adsorption_energy"
    assert record["target"]["property_subtype"] == "adsorption"

    v2_response = client.get("/api/papers/export/dft-dataset")
    assert v2_response.status_code == 200
    assert v2_response.json()["metadata"]["schema_version"] == "dft_results_ml_v2"


def test_v3_api_unknown_task_and_parameter_bounds_are_422(setup_test_db):
    client = TestClient(app)
    unknown = client.get("/api/dft/ml-dataset-v3", params={"task": "not-a-task"})
    assert unknown.status_code == 422
    assert "Unknown tabular task" in unknown.json()["detail"]
    assert client.get(
        "/api/dft/ml-dataset-v3", params={"task": "adsorption_energy", "limit": -1}
    ).status_code == 422
    assert client.get(
        "/api/dft/ml-dataset-v3", params={"task": "adsorption_energy", "limit": 10001}
    ).status_code == 422


def test_v3_api_ready_only_and_limit_apply_before_response(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        _seed(session, complete=False, year=2026)
        _seed(session, complete=True, year=2025)
        _seed(session, complete=True, year=2024)
        session.commit()

    response = TestClient(app).get(
        "/api/dft/ml-dataset-v3",
        params={"task": "adsorption_energy", "ready_only": True, "limit": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["records"]) == 1
    assert payload["records"][0]["tabular_ml_ready"] is True
    assert payload["manifest"]["task_candidate_count"] == 2
    assert payload["manifest"]["returned_count"] == 1
    assert payload["manifest"]["filters"]["limit"] == 1


def test_v3_csv_and_manifest_download_endpoints(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        blocked = _seed(session, complete=False, year=2026)
        ready = _seed(session, complete=True, year=2025)
        session.commit()
        blocked_id = str(blocked.id)
        ready_id = str(ready.id)

    client = TestClient(app)
    csv_response = client.get("/api/dft/ml-dataset-v3.csv", params={"task": "adsorption_energy"})
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in csv_response.headers["content-type"]
    assert csv_response.headers["content-disposition"] == (
        'attachment; filename="dft_ml_dataset_v3_adsorption_energy.csv"'
    )
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert [row["record_id"] for row in rows] == [ready_id]
    assert rows[0]["property_type"] == "adsorption_energy"
    assert rows[0]["normalized_property_type"] == "adsorption_energy"
    assert rows[0]["property_subtype"] == "adsorption"
    assert rows[0]["label_ready"] == "true"
    assert rows[0]["tabular_ml_ready"] == "true"
    assert rows[0]["page_locators"] == "[4]"

    all_csv_response = client.get(
        "/api/dft/ml-dataset-v3.csv",
        params={"task": "adsorption_energy", "ready_only": False},
    )
    all_rows = list(csv.DictReader(io.StringIO(all_csv_response.text)))
    assert {row["record_id"] for row in all_rows} == {blocked_id, ready_id}

    manifest_response = client.get("/api/dft/ml-dataset-v3/manifest", params={"task": "adsorption_energy"})
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert "records" not in manifest
    assert manifest["schema_version"] == "dft_results_ml_v3"
    assert manifest["filters"]["ready_only"] is True
    assert manifest["property_type_display_priority"][0] == "property_subtype"
    assert manifest["returned_count"] == 1
    assert manifest["task_candidate_count"] == 1


def test_v3_api_accepts_rds_gibbs_free_energy_task_and_preserves_subtype(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rds = _seed(
            session,
            property_type="gibbs_free_energy_change",
            reaction_step="RDS",
            evidence_text="The Gibbs free energy of the rate-determining step is 0.28 eV.",
            value=0.28,
        )
        _seed(
            session,
            property_type="gibbs_free_energy_change",
            reaction_step="S8 -> Li2S",
            evidence_text="The overall SRR from S8 to Li2S shows a Gibbs free energy change of -1.20 eV.",
            value=-1.20,
        )
        session.commit()
        expected_id = str(rds.id)

    client = TestClient(app)
    response = client.get("/api/dft/ml-dataset-v3", params={"task": "rds_gibbs_free_energy"})
    assert response.status_code == 200
    payload = response.json()
    assert [record["record_id"] for record in payload["records"]] == [expected_id]
    assert payload["records"][0]["target"]["canonical_property_type"] == "gibbs_free_energy_change"
    assert payload["records"][0]["target"]["property_subtype"] == "gibbs_free_energy_change"
    assert payload["records"][0]["target"]["reaction_step"] == "RDS"
    assert payload["manifest"]["task"] == "SRR_LiS:rds_gibbs_free_energy"
    assert select_training_records_v3(payload)[0].record_id == expected_id


def test_v3_csv_and_manifest_unknown_task_and_parameter_bounds_are_422(setup_test_db):
    client = TestClient(app)
    for path in ("/api/dft/ml-dataset-v3.csv", "/api/dft/ml-dataset-v3/manifest"):
        unknown = client.get(path, params={"task": "not-a-task"})
        assert unknown.status_code == 422
        assert "Unknown tabular task" in unknown.json()["detail"]
        assert client.get(path, params={"task": "adsorption_energy", "limit": -1}).status_code == 422
        assert client.get(path, params={"task": "adsorption_energy", "limit": 10001}).status_code == 422
