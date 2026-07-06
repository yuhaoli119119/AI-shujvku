from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    AuditLog,
    DFTResult,
    DFTSetting,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.main import app
from app.services.dft_review_bundle_service import DFTReviewBundleService


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


def _mark_figure_table_review_completed(engine, paper_id):
    settings = get_settings()
    with Session(engine) as session:
        state = DFTReviewBundleService(session, settings).get_figure_table_review_state(paper_id)
        session.add(
            AuditLog(
                paper_id=paper_id,
                action="offline_evidence_review_applied",
                source="test_chart_review",
                target_type="offline_evidence_review",
                target_id=state["current_snapshot_fingerprint"][:32],
                payload={
                    "stage_status": "completed",
                    "completed_snapshot_fingerprint": state["current_snapshot_fingerprint"],
                    "review_source": {"review_source_type": "local_ai", "reviewer_label": "test"},
                    "applied": [],
                    "skipped": [],
                    "dft_evidence_candidates": [],
                },
            )
        )
        session.commit()
        return state["current_snapshot_fingerprint"]


def test_offline_dft_review_bundle_requires_completed_figure_table_review(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "figure_table_review_not_completed"


def test_offline_dft_review_bundle_streams_compact_zip(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    completed_snapshot = _mark_figure_table_review_completed(setup_test_db, paper_id)
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
        return_schema = json.loads(archive.read("return_schema.json"))
        instructions = archive.read("instructions_for_web_ai.md").decode("utf-8")

    assert manifest["paper"]["paper_code"] == "B0078"
    assert manifest["figure_table_review_status"] == "completed"
    assert manifest["figure_table_completed_snapshot_fingerprint"] == completed_snapshot
    assert manifest["retention_policy"] == "generated_in_memory_not_persisted_on_server"
    assert candidates["existing_candidates"][0]["material_identity"] == "Fe-N-C"
    assert candidates["supporting_si_candidates"][0]["source_document_type"] == "supplementary_information"
    assert {doc["role"] for doc in metadata["source_documents"]} == {"main", "si"}
    assert "every current main-paper DFT candidate" in return_schema["properties"]["overall_status"]["description"]
    assert '`overall_status="completed"`' in instructions
    assert "未覆盖全部已有候选" in instructions
    assert "不得声称已写数据库" in instructions


def test_offline_review_validation_returns_import_request_without_writing(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
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
    assert request["source"] == "local_ai"
    assert request["raw_payload"]["review_metadata"]["local_ai_verification_required"] is True
    assert request["raw_payload"]["object_review_audits"][0]["writes_final_truth"] is False
    assert request["raw_payload"]["object_review_audits"][0]["local_ai_verification"]["verified_against_pdf"] is False
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
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))

    template["paper_code"] = "B9999"
    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(error["code"] == "paper_code_mismatch" for error in response.json()["errors"])


def test_offline_review_validation_rejects_stale_figure_table_snapshot(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_id = json.loads(archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()[0])[
            "evidence_id"
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
                    "evidence_ids": [evidence_id],
                    "reason": "Current evidence supports the row.",
                    "recommended_action": "ready_for_ml_export",
                }
            ],
        }
    )
    with Session(setup_test_db) as session:
        figure = session.query(PaperFigure).filter(PaperFigure.paper_id == paper_id).first()
        figure.caption = "Figure 4. DFT adsorption-energy profile with changed caption."
        session.add(figure)
        session.commit()

    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "figure_table_review_not_completed"


def test_offline_review_validation_rejects_conflicting_duplicate_target_field(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_id = json.loads(archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()[0])[
            "evidence_id"
        ]
    base = {
        "target_type": "dft_results",
        "target_id": str(row_id),
        "field_name": "dft_results",
        "evidence_checked": True,
        "evidence_ids": [evidence_id],
        "reason": "Evidence checked.",
        "recommended_action": "ready_for_ml_export",
    }
    template.update(
        {
            "overall_status": "completed",
            "object_review_audits": [
                {**base, "decision": "PASS"},
                {
                    **base,
                    "decision": "REVISE",
                    "corrected_value": {
                        "material_identity": "Fe-N-C",
                        "property_type": "adsorption_energy",
                        "value": -1.1,
                        "unit": "eV",
                        "adsorbate": "Li2S",
                    },
                },
            ],
        }
    )

    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(error["code"] == "conflicting_target_field_review" for error in response.json()["errors"])


def test_offline_review_validation_rejects_unrelated_or_missing_evidence_ids(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_lines = [
            json.loads(line)
            for line in archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
    unrelated = next(
        item["evidence_id"]
        for item in evidence_lines
        if item["source_document_type"] == "supplementary_information"
        and item["source_record_type"] == "dft_result_candidate_evidence"
    )
    audit = {
        "target_type": "dft_results",
        "target_id": str(row_id),
        "field_name": "dft_results",
        "decision": "PASS",
        "evidence_checked": True,
        "evidence_ids": [unrelated],
        "reason": "This incorrectly cites SI evidence for the main-paper target.",
        "recommended_action": "ready_for_ml_export",
    }
    template.update({"overall_status": "completed", "object_review_audits": [audit]})
    unrelated_response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert unrelated_response.status_code == 200
    assert unrelated_response.json()["valid"] is False
    assert any(error["code"] == "unrelated_evidence_id" for error in unrelated_response.json()["errors"])

    audit_without_evidence = dict(audit)
    audit_without_evidence.pop("evidence_ids")
    template["object_review_audits"] = [audit_without_evidence]
    missing_response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert missing_response.status_code == 200
    assert missing_response.json()["valid"] is False
    assert any(error["code"] == "schema_validation_error" for error in missing_response.json()["errors"])


def test_offline_dft_review_bundle_reincludes_stale_ml_ready_and_skips_rejected(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    with Session(setup_test_db) as session:
        sample = session.query(CatalystSample).filter(CatalystSample.paper_id == paper_id).first()
        session.add_all(
            [
                DFTResult(
                    paper_id=paper_id,
                    catalyst_sample_id=sample.id,
                    adsorbate="Li2S2",
                    property_type="adsorption_energy",
                    value=-0.8,
                    unit="eV",
                    evidence_text="ML-ready row without a currently eligible export gate must be reviewed again.",
                    candidate_status="ML_Ready",
                ),
                DFTResult(
                    paper_id=paper_id,
                    catalyst_sample_id=sample.id,
                    adsorbate="Li2S6",
                    property_type="adsorption_energy",
                    value=-0.4,
                    unit="eV",
                    evidence_text="Rejected row should not be resent.",
                    candidate_status="Rejected",
                ),
            ]
        )
        session.commit()
    _mark_figure_table_review_completed(setup_test_db, paper_id)

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        candidates = json.loads(archive.read("parsed/initial_dft_candidates.json"))
    included_ids = [item["target_id"] for item in candidates["existing_candidates"]]
    assert str(row_id) in included_ids
    assert len(included_ids) == 2


def test_dft_candidate_filter_skips_only_currently_eligible_ml_ready(setup_test_db, monkeypatch):
    with Session(setup_test_db) as session:
        paper = Paper(title="DFT filter", paper_code="B-FILTER", authors=[], pdf_path="filter.pdf")
        session.add(paper)
        session.flush()
        eligible = DFTResult(
            paper_id=paper.id,
            property_type="band_gap",
            value=1.0,
            unit="eV",
            candidate_status="ML_Ready",
        )
        stale = DFTResult(
            paper_id=paper.id,
            property_type="band_gap",
            value=2.0,
            unit="eV",
            candidate_status="ML_Ready",
        )
        rejected = DFTResult(
            paper_id=paper.id,
            property_type="band_gap",
            value=3.0,
            unit="eV",
            candidate_status="Rejected",
        )
        session.add_all([eligible, stale, rejected])
        session.flush()
        gates = {
            str(eligible.id): SimpleNamespace(eligible=True, review_status="verified"),
            str(stale.id): SimpleNamespace(eligible=False, review_status="verified"),
            str(rejected.id): SimpleNamespace(eligible=False, review_status="rejected"),
        }
        monkeypatch.setattr(
            "app.services.dft_review_bundle_service.bulk_export_gate_results",
            lambda *args, **kwargs: gates,
        )

        selected, excluded = DFTReviewBundleService(
            session,
            get_settings(),
        )._dft_rows_for_review_bundle(
            [eligible, stale, rejected],
            main_paper_id=paper.id,
        )

    assert [row.id for row in selected] == [stale.id]
    assert excluded == 2


def test_dft_import_requires_local_ai_verification_and_records_ai_identity(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_id = json.loads(archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()[0])[
            "evidence_id"
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
                    "evidence_ids": [evidence_id],
                    "corrected_value": {
                        "material_identity": "Fe-N-C",
                        "property_type": "adsorption_energy",
                        "value": -1.2,
                        "unit": "eV",
                        "adsorbate": "Li2S",
                    },
                    "reason": "The quoted sentence directly reports the candidate value.",
                    "recommended_action": "ready_for_ml_export",
                }
            ],
        }
    )
    validation = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)
    assert validation.status_code == 200
    request = validation.json()["import_analysis_request"]

    blocked = client.post("/api/external-analysis/import", json=request)
    assert blocked.status_code == 409
    assert "local_ai_pdf_verification_required" in blocked.json()["detail"]

    audit = request["raw_payload"]["object_review_audits"][0]
    audit["source"] = "local_ai"
    audit["source_label"] = "local_codex_pdf_check"
    audit["agent_role"] = "local_ai_pdf_verifier"
    audit["local_ai_verification"] = {
        "verified_against_pdf": True,
        "used_tools": ["get_codex_item", "read_paper_page"],
        "checked_evidence_ids": [evidence_id],
    }
    applied = client.post("/api/external-analysis/import", json=request)

    assert applied.status_code == 200
    summary = applied.json()["auto_apply_summary"]
    assert summary["object_reviews"]["applied_count"] == 1
    readback = summary["dft_readback"]
    assert str(row_id) in readback["object_versions"]
    assert str(row_id) in readback["candidate_status"]
    assert str(row_id) in readback["export_safety"]
    assert "conflicts" in readback
    assert "unfinished_items" in readback
    with Session(setup_test_db) as session:
        review = session.query(ExtractionFieldReview).filter(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_id == str(row_id),
            ExtractionFieldReview.field_name == "value",
        ).one()
    assert "ai_verification" in review.review_payload
    assert "human_verification" not in review.review_payload
    assert review.review_payload["ai_verification"]["verification_actor_type"] == "ai"
    assert review.reviewer_status == "verified"
    with Session(setup_test_db) as session:
        audit = session.query(AuditLog).filter(
            AuditLog.action == "verify_dft_result",
            AuditLog.target_id == str(row_id),
        ).order_by(AuditLog.created_at.desc()).first()
    assert audit.payload["actor_type"] == "ai"


def test_dft_import_without_review_metadata_cannot_bypass_gates(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    payload = {
        "paper_id": str(paper_id),
        "source": "ordinary_caller",
        "source_label": "ordinary_caller",
        "auto_apply_review_rules": True,
        "raw_payload": {
            "object_review_audits": [
                {
                    "target_type": "dft_results",
                    "target_id": str(row_id),
                    "field_name": "dft_results",
                    "decision": "PASS",
                    "evidence_checked": True,
                    "evidence_location": {
                        "page": 6,
                        "quoted_text": "-1.20 eV",
                    },
                    "reason": "Caller intentionally omitted the DFT review contract.",
                    "local_ai_verification": {
                        "verified_against_pdf": True,
                        "used_tools": ["get_codex_item", "read_paper_page"],
                    },
                }
            ]
        },
    }

    response = TestClient(app).post("/api/external-analysis/import", json=payload)

    assert response.status_code in {400, 409}
    assert "dft_json_validation_failed" in response.json()["detail"] or "figure_table_review_not_completed" in response.json()["detail"]
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
    assert row.candidate_status != "ML_Ready"


def test_loopback_owner_can_export_review_bundle_when_bulk_exports_are_disabled(
    setup_test_db,
    monkeypatch,
):
    paper_id, _ = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    get_settings.cache_clear()

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
