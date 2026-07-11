from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    AuditLog,
    DFTResult,
    DFTSetting,
    EvidenceLocator,
    ExternalAnalysisCandidate,
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
                    figure_role="dft_calculation",
                    content_summary="Adsorption energy profile for Li2S conversion.",
                    key_elements=["Li2S adsorption", "energy profile"],
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


def _create_chart_run(engine, paper_id, *, figure_ids=(), table_ids=(), completed=False, source_label="chart run"):
    with Session(engine) as session:
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="web_ai",
            source_label=source_label,
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        for figure_id in figure_ids:
            session.add(ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper_id,
                candidate_type="figure_review",
                normalized_payload={"target_type": "figure", "figure_id": str(figure_id)},
                status="materialized",
            ))
        for table_id in table_ids:
            session.add(ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper_id,
                candidate_type="table_review",
                normalized_payload={"target_type": "table", "table_id": str(table_id)},
                status="materialized",
            ))
        session.commit()
        run_id = run.id
    if completed:
        with Session(engine) as session:
            task = DFTReviewBundleService(session, get_settings()).get_figure_table_review_state(
                paper_id,
                chart_run_id=run_id,
            )
            session.add(AuditLog(
                paper_id=paper_id,
                action="offline_evidence_review_applied",
                source="test_chart_review",
                target_type="offline_evidence_review",
                target_id=task["current_snapshot_fingerprint"][:32],
                payload={
                    "run_id": str(run_id),
                    "chart_run_id": str(run_id),
                    "stage_status": "completed",
                    "completed_snapshot_fingerprint": task["current_snapshot_fingerprint"],
                    "review_source": {"review_source_type": "local_ai", "reviewer_label": "test"},
                    "applied": [],
                    "skipped": [],
                    "dft_evidence_candidates": [],
                },
            ))
            for figure_id in figure_ids:
                session.add(AuditLog(
                    paper_id=paper_id,
                    action="KEEP",
                    source="test_chart_review",
                    target_type="paper_figure",
                    target_id=str(figure_id),
                    payload={"run_id": str(run_id), "chart_run_id": str(run_id), "action": "KEEP"},
                ))
            for table_id in table_ids:
                session.add(AuditLog(
                    paper_id=paper_id,
                    action="KEEP",
                    source="test_chart_review",
                    target_type="paper_table",
                    target_id=str(table_id),
                    payload={"run_id": str(run_id), "chart_run_id": str(run_id), "action": "KEEP"},
                ))
            session.commit()
    return run_id


def test_offline_dft_review_bundle_requires_completed_figure_table_review(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "figure_table_review_not_completed"


def test_offline_dft_review_bundle_blocks_completed_chart_audit_when_figures_not_rag_ready(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    settings = get_settings()
    with Session(setup_test_db) as session:
        figure = session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).first()
        figure.figure_role = "unknown"
        figure.content_summary = None
        figure.key_elements = None
        session.add(figure)
        session.commit()

    with Session(setup_test_db) as session:
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

    with Session(setup_test_db) as session:
        state = DFTReviewBundleService(session, settings).get_figure_table_review_state(paper_id)

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert state["stage_status"] == "needs_local_ai"
    assert state["rag_quality_status"] == "blocked"
    assert state["rag_quality"]["figures"]["blocked"] == 1
    assert response.status_code == 409
    assert response.json()["detail"]["figure_table_review"]["rag_quality_status"] == "blocked"


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
            "format_examples.json",
            "instructions_for_web_ai.md",
            "return_schema.json",
            "return_template.json",
            "parsed/dft_review_checklist.json",
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
        return_template = json.loads(archive.read("return_template.json"))
        checklist = json.loads(archive.read("parsed/dft_review_checklist.json"))
        examples = json.loads(archive.read("format_examples.json"))
        instructions = archive.read("instructions_for_web_ai.md").decode("utf-8")

    assert manifest["paper"]["paper_code"] == "B0078"
    assert manifest["figure_table_review_status"] == "completed"
    assert len(manifest["figure_table_completed_snapshot_fingerprint"]) == 64
    assert manifest["chart_scope_type"] == "paper_reviewed_aggregate"
    assert manifest["retention_policy"] == "generated_in_memory_not_persisted_on_server"
    assert candidates["existing_candidates"][0]["material_identity"] == "Fe-N-C"
    assert candidates["supporting_si_candidates"][0]["source_document_type"] == "supplementary_information"
    assert {doc["role"] for doc in metadata["source_documents"]} == {"main", "si"}
    assert "gap_discovery" in return_schema["properties"]["overall_status"]["description"]
    assert return_template["coverage_acknowledgement"]["expected_target_ids"] == manifest["target_dft_result_ids"]
    assert checklist["target_ids"] == manifest["target_dft_result_ids"]
    assert checklist["targets"][0]["required_once"] is True
    assert checklist["targets"][0]["field_name"] == "dft_results"
    assert "PASS" in checklist["targets"][0]["allowed_decisions"]
    revise_example = examples["examples"]["revise_existing_candidate"]["object_review_audits"][0]
    assert revise_example["corrected_value"]["value"] == -1.2
    assert revise_example["corrected_value"]["unit"] == "eV"
    assert "format only" in examples["usage"][0]
    assert manifest["expected_dft_review_coverage"]["target_ids"] == manifest["target_dft_result_ids"]
    assert "gap_discovery" in instructions
    assert "format_examples.json" in instructions
    assert "dft_review_checklist.json" in instructions
    assert "不要照抄示例 ID" in instructions
    assert "existing_terminal_context" in instructions
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


def test_offline_review_new_candidate_uses_best_pdf_anchor_from_cited_evidence(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, get_settings())
        materials = service._build_materials(paper_id)
        template = service._return_template(materials)
        text_evidence = next(
            item for item in materials["evidence_map"].values()
            if item.get("evidence_kind") == "text"
        )
        figure_evidence = next(
            item for item in materials["evidence_map"].values()
            if item.get("evidence_kind") == "figure" and item.get("page") is not None
        )
        template.update(
            {
                "overall_status": "uncertain",
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": "new",
                        "temporary_id": "new-dft-001",
                        "field_name": "dft_results",
                        "decision": "new_candidate",
                        "evidence_checked": True,
                            "evidence_ids": [figure_evidence["evidence_id"]],
                        "corrected_value": {
                            "material_identity": "Fe-N-C",
                            "property_type": "pdos_overlap_energy_window",
                            "value": -2.5,
                            "value_upper": -0.5,
                            "unit": "eV",
                            "adsorbate": "Li2S4",
                        },
                        "confidence": 0.9,
                        "reason": "Text and Figure 4 jointly support the DFT energy window.",
                        "recommended_action": "ready_for_ml_export",
                    },
                    {
                        "target_type": "dft_results",
                        "target_id": str(row_id),
                        "field_name": "dft_results",
                        "decision": "PASS",
                        "evidence_checked": True,
                        "evidence_ids": [text_evidence["evidence_id"]],
                        "confidence": 0.9,
                        "reason": "The existing DFT candidate is also covered by this review result.",
                        "recommended_action": "ready_for_ml_export",
                    }
                ],
            }
        )

        result = service.validate_result(paper_id, template)

    audit = result["import_analysis_request"]["raw_payload"]["object_review_audits"][0]
    assert result["valid"] is True
    assert audit["evidence_location"]["page"] == figure_evidence["page"]
    assert audit["evidence_location"]["figure"] == figure_evidence["figure_label"]
    assert audit["evidence_location"]["evidence_ids"] == [figure_evidence["evidence_id"]]


def test_offline_review_validation_allows_multiple_new_dft_candidates_with_temporary_ids(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, get_settings())
        materials = service._build_materials(paper_id)
        template = service._return_template(materials)
        text_evidence = next(
            item for item in materials["evidence_map"].values()
            if item.get("evidence_kind") == "text"
        )
        figure_evidence = next(
            item for item in materials["evidence_map"].values()
            if item.get("evidence_kind") == "figure" and item.get("eligible_for_auto_apply")
        )
        template.update(
            {
                "overall_status": "uncertain",
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": str(row_id),
                        "field_name": "dft_results",
                        "decision": "PASS",
                        "evidence_checked": True,
                        "evidence_ids": [figure_evidence["evidence_id"]],
                        "confidence": 0.9,
                        "reason": "Existing candidate covered.",
                        "recommended_action": "ready_for_ml_export",
                    },
                    {
                        "target_type": "dft_results",
                        "target_id": "new",
                        "temporary_id": "new-dft-001",
                        "field_name": "dft_results",
                        "decision": "new_candidate",
                        "evidence_checked": True,
                        "evidence_ids": [figure_evidence["evidence_id"]],
                        "corrected_value": {
                            "material_identity": "Fe-N-C",
                            "property_type": "work_function",
                            "value": 4.8,
                            "unit": "eV",
                        },
                        "confidence": 0.85,
                        "reason": "First missing DFT descriptor.",
                        "recommended_action": "ready_for_ml_export",
                    },
                    {
                        "target_type": "dft_results",
                        "target_id": "new",
                        "temporary_id": "new-dft-002",
                        "field_name": "dft_results",
                        "decision": "new_candidate",
                        "evidence_checked": True,
                        "evidence_ids": [figure_evidence["evidence_id"]],
                        "corrected_value": {
                            "material_identity": "Fe-N-C",
                            "property_type": "magnetic_moment",
                            "value": 1.2,
                            "unit": "μB",
                        },
                        "confidence": 0.82,
                        "reason": "Second missing DFT descriptor.",
                        "recommended_action": "ready_for_ml_export",
                    },
                ],
            }
        )

        result = service.validate_result(paper_id, template)

    assert result["valid"] is True
    audits = result["import_analysis_request"]["raw_payload"]["object_review_audits"]
    new_audits = [audit for audit in audits if audit["target_id"] == "new"]
    assert {audit["temporary_id"] for audit in new_audits} == {"new-dft-001", "new-dft-002"}


def test_offline_review_existing_dft_uses_locator_pdf_anchor_when_payload_has_no_page(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(
            title="Locator-only DFT paper",
            paper_code="BLOC1",
            paper_type="article",
            pdf_path="locator-only.pdf",
            authors=[],
        )
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Fe-N4")
        session.add(sample)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.2,
            unit="eV",
            evidence_text="Table 2 reports Li2S4 adsorption energy of -1.20 eV on Fe-N4.",
            evidence_payload=None,
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=9,
                evidence_text="Table 2 reports Li2S4 adsorption energy of -1.20 eV on Fe-N4.",
                locator_status="exact_page",
                locator_confidence=0.96,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, get_settings())
        materials = service._build_materials(paper_id)
        row_evidence = next(
            item
            for item in materials["evidence_map"].values()
            if item.get("source_record_id") == str(row_id)
        )
        template = service._return_template(materials)
        template.update(
            {
                "overall_status": "completed",
                "object_review_audits": [
                    {
                        "target_type": "dft_results",
                        "target_id": str(row_id),
                        "field_name": "value",
                        "decision": "PASS",
                        "evidence_checked": True,
                        "evidence_ids": [row_evidence["evidence_id"]],
                        "corrected_value": -1.2,
                        "confidence": 0.9,
                        "reason": "Locator-backed row evidence confirms the value.",
                        "recommended_action": "ready_for_ml_export",
                    }
                ],
            }
        )

        result = service.validate_result(paper_id, template)

    audit = result["import_analysis_request"]["raw_payload"]["object_review_audits"][0]
    assert result["valid"] is True
    assert row_evidence["page"] == 9
    assert audit["evidence_location"]["page"] == 9
    assert audit["evidence_location"]["locator_status"] == "exact_page"


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


def test_offline_review_validation_normalizes_common_web_ai_json_noise(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    client = TestClient(app)
    bundle_response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    with ZipFile(BytesIO(bundle_response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        evidence_id = json.loads(archive.read("evidence/text_snippets.jsonl").decode("utf-8").splitlines()[0])[
            "evidence_id"
        ]
    audit = {
        "target_type": "dft_results",
        "target_id": str(row_id),
        "field_name": "dft_results",
        "decision": "REVISE",
        "evidence_checked": True,
        "evidence_ids": [evidence_id],
        "corrected_value": {
            "material_identity": "Fe-N-C",
            "property_type": "adsorption_energy",
            "value": "−1.20 eV",
            "unit": "eV",
            "adsorbate": "Li2S",
        },
        "confidence": 0.95,
        "reason": "Evidence checked against the quoted sentence.",
        "recommended_action": "ready_for_ml_export",
    }
    template.update(
        {
            "overall_status": "completed",
            "object_review_audits": [dict(audit), dict(audit)],
        }
    )

    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["validated_audit_count"] == 1
    assert any(warning["code"] == "normalized_duplicate_object_review_audit" for warning in body["warnings"])
    normalized_audit = body["import_analysis_request"]["raw_payload"]["object_review_audits"][0]
    assert normalized_audit["corrected_value"]["value"] == -1.2


def test_offline_review_validation_blocks_partial_dft_coverage_even_when_uncertain(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    with Session(setup_test_db) as session:
        sample = session.query(CatalystSample).filter(CatalystSample.paper_id == paper_id).first()
        session.add(
            DFTResult(
                paper_id=paper_id,
                catalyst_sample_id=sample.id,
                adsorbate="Li2S2",
                property_type="adsorption_energy",
                value=-0.8,
                unit="eV",
                evidence_text="A second DFT row requires a separate audit decision.",
                evidence_payload={"page": 7, "quoted_text": "-0.8 eV"},
            )
        )
        session.commit()
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
            "overall_status": "uncertain",
            "object_review_audits": [
                {
                    "target_type": "dft_results",
                    "target_id": str(row_id),
                    "field_name": "dft_results",
                    "decision": "PASS",
                    "evidence_checked": True,
                    "evidence_ids": [evidence_id],
                    "reason": "Only one of two DFT candidates was reviewed.",
                    "recommended_action": "ready_for_ml_export",
                }
            ],
        }
    )

    response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=template)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["import_analysis_request"] is None
    assert body["coverage"]["coverage_complete"] is False
    assert body["coverage"]["reviewed_existing_count"] == 1
    assert body["coverage"]["missing_target_ids"]
    assert any(error["code"] == "incomplete_candidate_coverage" for error in body["errors"])


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
    assert len(excluded) == 2


def test_dft_bundle_aggregates_completed_runs_and_keeps_pending_and_unreviewed_si_separate(setup_test_db):
    figure_root = get_settings().storage_paths["figures"]
    figure_root.mkdir(parents=True, exist_ok=True)
    (figure_root / "aggregate-reviewed.png").write_bytes(b"\x89PNG\r\n\x1a\naggregate")
    with Session(setup_test_db) as session:
        main = Paper(title="Aggregate review", paper_code="B-AGG", authors=[], pdf_path="agg.pdf")
        si = Paper(title="Aggregate SI", paper_code="S-AGG", authors=[], pdf_path="agg-si.pdf")
        session.add_all([main, si])
        session.flush()
        session.add(PaperRelationship(
            source_paper_id=main.id,
            target_paper_id=si.id,
            relationship_type="supplementary",
            created_by="test",
        ))
        figures = [
            PaperFigure(
                paper_id=main.id,
                figure_label=f"Figure {index}",
                caption=f"Reviewed DFT figure {index}",
                page=index,
                figure_role="dft_calculation",
                content_summary=f"DFT result figure {index}",
                    key_elements=["energy", "DFT"],
                    image_path="aggregate-reviewed.png",
                crop_status="approved",
            )
            for index in range(1, 6)
        ]
        tables = [
            PaperTable(
                paper_id=main.id,
                caption=f"Table {index}",
                markdown_content=f"| DFT energy |\n| {index}.0 eV |",
                page=10 + index,
            )
            for index in range(1, 6)
        ]
        si_figure = PaperFigure(
            paper_id=si.id,
            figure_label="Figure S1",
            caption="Reviewed SI figure with DFT energy 0.3 eV",
            page=2,
            figure_role="dft_calculation",
            content_summary="Reviewed supplementary DFT energy evidence.",
            key_elements=["DFT energy", "0.3 eV"],
            image_path="aggregate-reviewed.png",
            crop_status="approved",
        )
        si_table = PaperTable(
            paper_id=si.id,
            caption="Table S1 DFT energies",
            markdown_content="| energy |\n| 0.3 eV |",
            page=3,
        )
        session.add_all([*figures, *tables, si_figure, si_table])
        session.commit()
        paper_id = main.id
        si_id = si.id
        figure_ids = [row.id for row in figures]
        table_ids = [row.id for row in tables]
        si_figure_id = si_figure.id

    _create_chart_run(setup_test_db, paper_id, figure_ids=figure_ids[:4], completed=True, source_label="figures")
    _create_chart_run(setup_test_db, paper_id, figure_ids=figure_ids[:4], completed=True, source_label="figures duplicate")
    _create_chart_run(setup_test_db, paper_id, table_ids=table_ids, completed=True, source_label="tables")
    _create_chart_run(setup_test_db, paper_id, figure_ids=figure_ids[4:], completed=False, source_label="pending figure")
    _create_chart_run(setup_test_db, si_id, figure_ids=[si_figure_id], completed=True, source_label="reviewed SI figure")

    client = TestClient(app)
    state = client.get(f"/api/papers/{paper_id}/dft-review-state")
    assert state.status_code == 200
    summary = state.json()["summary"]
    assert summary["reviewed_figures"] == 5
    assert summary["reviewed_tables"] == 5
    assert summary["reviewed_main_figures"] == 4
    assert summary["reviewed_main_tables"] == 5
    assert summary["pending_main_figures"] == 1
    assert summary["pending_main_tables"] == 0
    assert summary["reviewed_supporting_evidence"] == 1
    assert summary["unreviewed_supporting_context"] == 1

    response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        snapshot = json.loads(archive.read("parsed/curated_figure_table_evidence_snapshot.json"))
        old_template = json.loads(archive.read("return_template.json"))
    assert manifest["review_mode"] == "gap_discovery"
    assert manifest["counts"]["reviewed_main_figures"] == 4
    assert manifest["counts"]["reviewed_main_tables"] == 5
    assert manifest["counts"]["pending_main_figures"] == 1
    assert len(snapshot["reviewed_main_evidence"]["figures"]) == 4
    assert len(snapshot["reviewed_main_evidence"]["tables"]) == 5
    assert len(snapshot["reviewed_supporting_evidence"]) == 1
    assert snapshot["reviewed_supporting_evidence"][0]["source_record_id"] == str(si_figure_id)
    assert len(snapshot["unreviewed_supporting_context"]) == 1
    assert all(item["eligible_for_auto_apply"] is False for item in snapshot["unreviewed_supporting_context"])

    with Session(setup_test_db) as session:
        changed = session.get(PaperFigure, figure_ids[0])
        changed.caption = "Changed after completed review"
        session.add(changed)
        session.commit()
    old_template["overall_status"] = "completed"
    stale = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=old_template)
    assert stale.status_code == 200
    assert stale.json()["valid"] is False
    assert any(item["code"] == "stale_or_mismatched_bundle" for item in stale.json()["errors"])


def test_gap_discovery_exports_terminal_context_and_rejects_duplicate_or_unreviewed_si_new_candidate(setup_test_db):
    figure_root = get_settings().storage_paths["figures"]
    figure_root.mkdir(parents=True, exist_ok=True)
    (figure_root / "gap-reviewed.png").write_bytes(b"\x89PNG\r\n\x1a\ngap")
    with Session(setup_test_db) as session:
        main = Paper(title="Gap discovery", paper_code="B-GAP", authors=[], pdf_path="gap.pdf")
        si = Paper(title="Gap SI", paper_code="S-GAP", authors=[], pdf_path="gap-si.pdf")
        session.add_all([main, si])
        session.flush()
        session.add(PaperRelationship(
            source_paper_id=main.id,
            target_paper_id=si.id,
            relationship_type="supplementary",
            created_by="test",
        ))
        sample = CatalystSample(paper_id=main.id, name="Fe-N-C")
        session.add(sample)
        session.flush()
        rejected_rows = [
            DFTResult(
                paper_id=main.id,
                catalyst_sample_id=sample.id,
                property_type="adsorption_energy",
                value=-1.0 - index,
                unit="eV",
                adsorbate=f"Li2S{index}",
                candidate_status="Rejected",
                evidence_text=f"Rejected value {-1.0-index} eV",
            )
            for index in range(7)
        ]
        main_figure = PaperFigure(
            paper_id=main.id,
            figure_label="Figure 1",
            caption="DFT adsorption energy -1.0 eV",
            page=4,
            figure_role="dft_calculation",
            content_summary="Adsorption energy result",
            key_elements=["-1.0 eV"],
            image_path="gap-reviewed.png",
            crop_status="approved",
        )
        si_table = PaperTable(
            paper_id=si.id,
            caption="Table S1 DFT adsorption energy",
            markdown_content="| adsorbate | energy |\n| Li2S0 | -1.0 eV |",
            page=8,
        )
        session.add_all([*rejected_rows, main_figure, si_table])
        session.commit()
        paper_id = main.id
        figure_id = main_figure.id
    _create_chart_run(setup_test_db, paper_id, figure_ids=[figure_id], completed=True)

    client = TestClient(app)
    response = client.post(f"/api/papers/{paper_id}/dft-review-bundle")
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        template = json.loads(archive.read("return_template.json"))
        candidates = json.loads(archive.read("parsed/initial_dft_candidates.json"))
        figures = json.loads(archive.read("parsed/extracted_figures.json"))
        tables = json.loads(archive.read("parsed/extracted_tables.json"))
    assert template["review_mode"] == "gap_discovery"
    assert template["coverage_acknowledgement"]["expected_target_ids"] == []
    assert len(candidates["existing_terminal_context"]) == 7
    assert all(item["readonly"] and not item["eligible_as_write_target"] for item in candidates["existing_terminal_context"])
    reviewed_id = next(item["evidence_id"] for item in figures if item["eligible_for_auto_apply"])
    unreviewed_si_id = next(item["evidence_id"] for item in tables if not item["eligible_for_auto_apply"])

    duplicate = dict(template)
    duplicate.update({"overall_status": "completed", "object_review_audits": [{
        "target_type": "dft_results", "target_id": "new", "temporary_id": "new-dft-001",
        "field_name": "dft_results", "decision": "new_candidate", "evidence_checked": True,
        "evidence_ids": [reviewed_id],
        "corrected_value": {"material_identity": "Fe-N-C", "property_type": "adsorption_energy", "value": -1.0, "unit": "eV", "adsorbate": "Li2S0"},
        "reason": "Duplicate proposal", "blocking_errors": [],
    }]})
    duplicate_response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=duplicate)
    assert duplicate_response.status_code == 200
    assert any(item["code"] == "duplicate_existing_terminal_candidate" for item in duplicate_response.json()["errors"])

    unreviewed = dict(template)
    unreviewed.update({"overall_status": "completed", "object_review_audits": [{
        "target_type": "dft_results", "target_id": "new", "temporary_id": "new-dft-002",
        "field_name": "dft_results", "decision": "new_candidate", "evidence_checked": True,
        "evidence_ids": [unreviewed_si_id],
        "corrected_value": {"material_identity": "Co-N-C", "property_type": "adsorption_energy", "value": -0.2, "unit": "eV", "adsorbate": "Li2S"},
        "reason": "Unreviewed SI-only proposal", "blocking_errors": [],
    }]})
    unreviewed_response = client.post(f"/api/papers/{paper_id}/dft-review-result/validate", json=unreviewed)
    error_codes = {item["code"] for item in unreviewed_response.json()["errors"]}
    assert "unreviewed_supporting_evidence_requires_human" in error_codes
    assert "new_candidate_requires_reviewed_evidence" in error_codes


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
