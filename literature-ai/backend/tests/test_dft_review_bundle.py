from __future__ import annotations

from io import BytesIO
import json
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ExternalAnalysisRun,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.main import app


def _seed_review_materials(engine):
    settings = get_settings()
    figure_root = settings.storage_paths["figures"]
    figure_root.mkdir(parents=True, exist_ok=True)
    (figure_root / "dft-profile.png").write_bytes(b"\x89PNG\r\n\x1a\nreview-image")

    with Session(engine) as session:
        main = Paper(
            title="Main DFT paper",
            paper_code="B0078",
            paper_type="article",
            pdf_path="main.pdf",
            authors=["A. Author"],
            abstract="A catalyst study with density functional theory.",
        )
        si = Paper(
            title="Main DFT paper supporting information",
            paper_code="B0078-SI1",
            paper_type="supplementary",
            pdf_path="main-si.pdf",
            authors=["A. Author"],
        )
        session.add_all([main, si])
        session.flush()
        session.add(
            PaperRelationship(
                source_paper_id=main.id,
                target_paper_id=si.id,
                relationship_type="supplementary",
                created_by="test",
            )
        )
        sample = CatalystSample(paper_id=main.id, name="Fe-N-C", metal_centers=["Fe"])
        session.add(sample)
        session.flush()
        row = DFTResult(
            paper_id=main.id,
            catalyst_sample_id=sample.id,
            adsorbate="Li2S",
            property_type="adsorption_energy",
            value=-1.2,
            unit="eV",
            source_section="Computational details",
            source_figure="Figure 4",
            evidence_text="The adsorption energy of Li2S on Fe-N-C is -1.20 eV.",
            evidence_payload={"page": 6, "figure": "Figure 4", "quoted_text": "-1.20 eV"},
        )
        si_row = DFTResult(
            paper_id=si.id,
            adsorbate="LiS",
            property_type="free_energy",
            value=0.42,
            unit="eV",
            source_section="DFT data",
            evidence_text="Table S3 reports a free energy of 0.42 eV.",
            evidence_payload={"page": 12, "table": "Table S3"},
        )
        session.add_all(
            [
                row,
                si_row,
                DFTSetting(
                    paper_id=main.id,
                    software="VASP",
                    functional="PBE",
                    cutoff_energy_ev=450.0,
                    k_points="3x3x1",
                ),
                PaperSection(
                    paper_id=main.id,
                    section_title="Computational details",
                    section_type="methods",
                    text="DFT calculations used VASP with the PBE functional and a 450 eV cutoff.",
                    page_start=5,
                    page_end=5,
                ),
                PaperSection(
                    paper_id=si.id,
                    section_title="DFT data",
                    section_type="methods",
                    text="The SI lists k-point convergence and adsorption energy values.",
                    page_start=10,
                    page_end=12,
                ),
                PaperSection(
                    paper_id=main.id,
                    section_title="Introduction",
                    section_type="introduction",
                    text="This unrelated introductory paragraph is not part of the DFT evidence package.",
                    page_start=1,
                    page_end=1,
                ),
                PaperTable(
                    paper_id=si.id,
                    caption="Table S3. DFT free energies",
                    markdown_content="| species | energy (eV) |\n| LiS | 0.42 |",
                    page=12,
                ),
                PaperFigure(
                    paper_id=main.id,
                    figure_label="Figure 4",
                    caption="Figure 4. DFT adsorption-energy profile.",
                    content_summary="Adsorption energy profile for Li2S conversion.",
                    image_path="dft-profile.png",
                    page=6,
                ),
            ]
        )
        session.commit()
        return main.id, row.id


def test_offline_dft_review_bundle_streams_compact_zip(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["cache-control"] == "no-store"
    assert "B0078_dft_review_bundle.zip" in response.headers["content-disposition"]

    with ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "instructions_for_web_ai.md",
            "return_schema.json",
            "return_template.json",
            "parsed/paper_metadata.json",
            "parsed/initial_dft_candidates.json",
            "parsed/extracted_tables.json",
            "parsed/extracted_figures.json",
            "evidence/text_snippets.jsonl",
        } <= names
        assert not any(name.lower().endswith(".pdf") for name in names)
        assert any(name.startswith("evidence/tables/si_table_") for name in names)
        assert any(name.startswith("evidence/figures/main_fig_") for name in names)

        manifest = json.loads(archive.read("manifest.json"))
        candidates = json.loads(archive.read("parsed/initial_dft_candidates.json"))
        metadata = json.loads(archive.read("parsed/paper_metadata.json"))
        instructions = archive.read("instructions_for_web_ai.md").decode("utf-8")

    assert manifest["paper"]["paper_code"] == "B0078"
    assert manifest["retention_policy"] == "generated_in_memory_not_persisted_on_server"
    assert candidates["existing_candidates"][0]["material_identity"] == "Fe-N-C"
    assert candidates["supporting_si_candidates"][0]["source_document_type"] == "supplementary_information"
    assert {doc["role"] for doc in metadata["source_documents"]} == {"main", "si"}
    assert "不得声称已写数据库" in instructions


def test_offline_review_validation_returns_import_request_without_writing(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    assert bundle_response.status_code == 200

    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_lines = [
            json.loads(line)
            for line in archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]

    template.update(
        {
            "overall_status": "completed",
            "object_review_audits": [
                {
                    "target_type": "dft_results",
                    "target_id": str(row_id),
                    "field_name": "dft_results",
                    "decision": "PASS",
                    "evidence_checked": True,
                    "evidence_ids": [evidence_lines[0]["evidence_id"]],
                    "corrected_value": {
                        "material_identity": "Fe-N-C",
                        "property_type": "adsorption_energy",
                        "value": -1.2,
                        "unit": "eV",
                        "adsorbate": "Li2S",
                    },
                    "confidence": 0.95,
                    "reason": "The quoted sentence directly reports the candidate value.",
                    "blocking_errors": [],
                    "recommended_action": "Keep as an unverified review candidate.",
                }
            ],
        }
    )

    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["validated_audit_count"] == 1
    assert payload["safety"]["writes_database"] is False
    request = payload["import_analysis_request"]
    assert request["source"] == "web_ai"
    assert request["raw_payload"]["object_review_audits"][0]["writes_final_truth"] is False
    assert request["raw_payload"]["object_review_audits"][0]["evidence_location"]["evidence_ids"]

    with Session(setup_test_db) as session:
        assert session.query(ExternalAnalysisRun).count() == 0

    template["object_review_audits"][0]["evidence_ids"] = ["main:text:999"]
    invalid = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert invalid.status_code == 200
    assert invalid.json()["valid"] is False
    assert invalid.json()["errors"][0]["code"] == "unknown_evidence_id"


def test_offline_review_validation_rejects_wrong_paper_code(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))

    template["paper_code"] = "B9999"
    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(error["code"] == "paper_code_mismatch" for error in response.json()["errors"])


def test_loopback_owner_can_export_review_bundle_when_bulk_exports_are_disabled(
    setup_test_db,
    monkeypatch,
):
    paper_id, _ = _seed_review_materials(setup_test_db)
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    get_settings.cache_clear()

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
