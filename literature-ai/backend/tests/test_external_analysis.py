import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.models import AuditLog, Base, CatalystSample, DFTAuditIssue, DFTResult, ElectrochemicalPerformance, EvidenceLocator, ExternalAnalysisCandidate, ExternalAnalysisRun, ExtractionFieldReview, MechanismClaim, Paper, PaperCorrection, PaperFigure, PaperNote, PaperRelationship, PaperSection, PaperTable, WorkflowJob, WritingCard
from app.db.session import get_db_session
from app.main import app
from app.services.external_analysis_service import ExternalAnalysisNormalizedModel, ExternalAnalysisService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.verification_session_service import VerificationSessionService
from app.utils.review_safety import is_export_eligible_extraction


def _make_external_audit_ready(paper: Paper, root: Path) -> None:
    pdf_path = root / f"{paper.id}.pdf"
    markdown_path = root / f"{paper.id}.md"
    docling_path = root / f"{paper.id}.docling.json"
    workspace_path = root / "workspace" / str(paper.id)
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    markdown_path.write_text("# Ready paper\n\nDFT evidence is available.", encoding="utf-8")
    docling_path.write_text('{"texts": [{"text": "DFT evidence is available."}]}', encoding="utf-8")
    package_path = workspace_path / "extraction" / "ai_reading_package.json"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text('{"sections": [{"title": "Results"}]}', encoding="utf-8")
    paper.pdf_path = str(pdf_path)
    paper.markdown_path = str(markdown_path)
    paper.docling_json_path = str(docling_path)
    paper.workspace_path = str(workspace_path)


def _acquire_write_lock(client: TestClient, paper_id: Any, module_name: str = "all_non_dft", locked_by: str = "codex") -> str:
    response = client.post(
        "/api/module-locks/acquire",
        json={
            "paper_id": str(paper_id),
            "module_name": module_name,
            "locked_by": locked_by,
            "ttl_minutes": 30,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["lock_token"]


def test_dual_ai_consensus_signature_accepts_structured_corrected_values():
    first = VerificationSessionService._value_key(["Mo", "Ni"])
    second = VerificationSessionService._value_key(["Mo", "Ni"])
    grouped = {("REVISE", first): "accepted"}

    assert grouped[("REVISE", second)] == "accepted"


def test_import_warning_decision_aliases_match_dft_queue_semantics():
    assert ExternalAnalysisService._normalize_dft_review_decision_for_warning("confirmed_with_corrections") == "PROPOSED"
    assert ExternalAnalysisService._normalize_dft_review_decision_for_warning("corrected") == "PROPOSED"
    assert ExternalAnalysisService._normalize_dft_review_decision_for_warning("revision") == "PROPOSED"
    assert ExternalAnalysisService._normalize_dft_review_decision_for_warning("needs_user_decision") == "NEEDS_HUMAN"
    assert ExternalAnalysisService._normalize_dft_review_decision_for_warning("ambiguous") == "NEEDS_HUMAN"


@pytest.mark.parametrize(
    ("operation", "target_path", "expected_tool"),
    [
        ("replace", "tables:00000000-0000-0000-0000-000000000001:caption", "update_table"),
        ("create", "tables:new:create", "create_table"),
        ("delete", "tables:00000000-0000-0000-0000-000000000001:delete", "delete_table"),
    ],
)
def test_import_analysis_rejects_table_correction_operations(operation, target_path, expected_tool):
    normalized = ExternalAnalysisNormalizedModel.model_validate(
        {
            "correction_proposals": [
                {
                    "field_name": "tables",
                    "target_path": target_path,
                    "operation": operation,
                    "proposed_value": {} if operation == "create" else "value",
                    "reason": "Table mutations must use direct tools.",
                    "evidence_payload": {"page": 1, "table": "Table 1"},
                }
            ]
        }
    )

    with pytest.raises(ValueError, match=expected_tool):
        ExternalAnalysisService._reject_direct_tool_only_corrections(normalized)


def test_heuristic_import_maps_legacy_candidates_notes():
    service = ExternalAnalysisService.__new__(ExternalAnalysisService)

    normalized = service._heuristic_normalize(
        {
            "candidates": [
                {
                    "candidate_type": "paper_note",
                    "field_name": "figures",
                    "content": "Figure extraction appears incomplete.",
                    "page": 3,
                    "quoted_text": "Fig. 1.",
                },
                {
                    "candidate_type": "unknown_custom_shape",
                    "content": "This should not disappear.",
                },
            ]
        }
    )

    assert len(normalized.review_notes) == 1
    assert normalized.review_notes[0].field_name == "figures"
    assert normalized.review_notes[0].quoted_text == "Fig. 1."
    assert len(normalized.unmapped_items) == 1


def test_import_analysis_warns_on_unrecognized_dft_audit_container():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Container warning paper", pdf_path="warning.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            response = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "legacy_dft_key",
                    "raw_payload": {
                        "dft_result_audits": [
                            {
                                "target_type": "dft_results",
                                "target_id": "existing-row",
                                "field_name": "value",
                                "decision": "PROPOSED",
                                "corrected_value": 1.23,
                            }
                        ]
                    },
                },
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["candidates"] == []
            assert [warning["code"] for warning in body["warnings"]] == [
                "unrecognized_object_review_container"
            ]
            assert body["warnings"][0]["key"] == "dft_result_audits"
            assert body["warnings"][0]["expected_key"] == "object_review_audits"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_import_and_materialize_flow():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                main_paper = Paper(
                    title="Main Paper",
                    doi="10.1000/main",
                    pdf_path="main.pdf",
                    authors=["Author A"],
                )
                support_paper = Paper(
                    title="Support Paper",
                    doi="10.1000/support",
                    pdf_path="support.pdf",
                    authors=["Author B"],
                )
                session.add_all([main_paper, support_paper])
                session.commit()
                session.refresh(main_paper)
                session.refresh(support_paper)

            client = TestClient(app)

            import_payload = {
                "paper_id": str(main_paper.id),
                "source": "chatgpt_web",
                "source_label": "ChatGPT web upload",
                "raw_payload": {
                    "review_notes": [
                        {
                            "content": "The abstract wording may overstate the mechanism claim.",
                            "field_name": "abstract",
                            "page": 1,
                            "quoted_text": "This catalyst proves complete sulfur immobilization.",
                        }
                    ],
                    "correction_proposals": [
                        {
                            "field_name": "abstract",
                            "target_path": "abstract",
                            "operation": "replace",
                            "proposed_value": "A more conservative abstract rewrite.",
                            "reason": "The original sentence is too strong compared with the source evidence.",
                            "evidence_payload": {"page": 1},
                        }
                    ],
                    "supporting_papers": [
                        {
                            "relationship_type": "supports",
                            "target_doi": "10.1000/support",
                            "target_title": "Support Paper",
                            "note": "This paper provides complementary mechanism evidence.",
                        }
                    ],
                },
            }

            imported = client.post("/api/external-analysis/import", json=import_payload)
            assert imported.status_code == 200
            run_payload = imported.json()
            assert run_payload["mapping_status"] in {"normalized", "heuristic", "normalized_with_llm"}
            assert len(run_payload["candidates"]) == 3

            run_id = run_payload["id"]
            materialized = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"explicit_all": True, "created_by": "reviewer_ai"},
            )
            assert materialized.status_code == 200
            assert materialized.json()["created_notes"] == 1
            assert materialized.json()["created_corrections"] == 1
            assert materialized.json()["created_relationships"] == 1
            assert materialized.json()["deferred_review_candidates"] == 0
            assert "next_action" not in materialized.json()

            detail = client.get(f"/api/papers/{main_paper.id}")
            assert detail.status_code == 200
            detail_payload = detail.json()
            assert detail_payload["relationship_summary"]["supports"] == 1
            assert len(detail_payload["outgoing_relationships"]) == 1
            assert detail_payload["outgoing_relationships"][0]["related_paper_title"] == "Support Paper"

            listing = client.get("/api/papers")
            assert listing.status_code == 200
            listing_payload = listing.json()
            listed_main = next(item for item in listing_payload if item["id"] == str(main_paper.id))
            assert listed_main["relationship_summary"]["supports"] == 1

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 1
                assert session.query(PaperCorrection).count() == 1
                assert session.query(PaperRelationship).count() == 1
                jobs = session.scalars(select(WorkflowJob)).all()
                assert len(jobs) == 1
                assert jobs[0].type == "agent_activity"
                assert jobs[0].payload["source_label"] == "ChatGPT web upload"
                assert jobs[0].payload["paper_id"] == str(main_paper.id)
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_auto_applies_non_dft_corrections():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Original title",
                    abstract="Original abstract.",
                    doi="10.1000/non-dft-auto",
                    pdf_path="paper.pdf",
                    authors=["Author A"],
                )
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    caption="Wrong caption",
                    page=2,
                    figure_label="fig_1",
                )
                session.add(figure)
                session.commit()
                session.refresh(paper)
                session.refresh(figure)
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, locked_by="codex")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "codex_overall_test",
                    "auto_apply_review_rules": True,
                    "reviewer": "codex",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "review_notes": [
                            {
                                "content": "[AI_REVIEWED] metadata and figures checked.",
                                "field_name": "overall",
                                "page": 1,
                                "quoted_text": "Abstract evidence.",
                            }
                        ],
                        "correction_proposals": [
                            {
                                "field_name": "abstract",
                                "target_path": "abstract",
                                "operation": "replace",
                                "proposed_value": "AI corrected abstract.",
                                "reason": "The abstract was missing the verified summary.",
                                "evidence_payload": {"page": 1, "quoted_text": "Abstract evidence."},
                            },
                            {
                                "field_name": "figures",
                                "target_path": f"figures:{figure_id}:caption",
                                "operation": "replace",
                                "proposed_value": "Fig. 1. AI corrected caption.",
                                "reason": "Caption text on page 2 identifies Fig. 1.",
                                "evidence_payload": {"page": 2, "quoted_text": "Fig. 1. AI corrected caption."},
                            },
                        ],
                    },
                },
            )
            assert imported.status_code == 200, imported.text
            statuses = {item["status"] for item in imported.json()["candidates"]}
            assert statuses == {"ai_reviewed", "ai_applied"}

            with Session(engine) as session:
                stored_paper = session.get(Paper, paper_id)
                stored_figure = session.get(PaperFigure, figure_id)
                corrections = session.scalars(select(PaperCorrection)).all()
                note = session.scalar(select(PaperNote))

                assert stored_paper.abstract == "AI corrected abstract."
                assert stored_figure.caption == "Fig. 1. AI corrected caption."
                assert note is not None
                assert note.content.startswith("[AI_REVIEWED]")
                assert len(corrections) == 2
                assert {row.status for row in corrections} == {"approved"}
                assert {row.reviewed_by for row in corrections} == {"codex"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_auto_creates_non_table_structured_objects():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Create missing objects",
                    doi="10.1000/non-dft-create",
                    pdf_path="paper.pdf",
                    authors=["Author A"],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, locked_by="codex")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "codex_create_test",
                    "auto_apply_review_rules": True,
                    "reviewer": "codex",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "correction_proposals": [
                            {
                                "field_name": "figures",
                                "target_path": "figures:new:create",
                                "operation": "create",
                                "proposed_value": {
                                    "caption": "Fig. 2. Missing band structure.",
                                    "page": 4,
                                    "figure_label": "fig_2",
                                    "figure_role": "electronic_property",
                                    "content_summary": "Band structure panel missing from parser.",
                                    "key_elements": ["band structure"],
                                    "crop_status": "needs_recrop",
                                    "crop_source": "ide_ai_locator",
                                },
                                "reason": "The PDF contains Fig. 2 but parser did not create a figure object.",
                                "evidence_payload": {
                                    "page": 4,
                                    "quoted_text": "Fig. 2. Missing band structure.",
                                    "source_pdf": "paper.pdf",
                                },
                            },
                            {
                                "field_name": "sections",
                                "target_path": "sections:new:create",
                                "operation": "create",
                                "proposed_value": {
                                    "section_title": "Methods",
                                    "section_type": "methods",
                                    "text": "Methods section recovered from PDF text.",
                                    "page_start": 2,
                                    "page_end": 3,
                                },
                                "reason": "The parser missed the logical methods section.",
                                "evidence_payload": {
                                    "page": 2,
                                    "quoted_text": "Methods section recovered from PDF text.",
                                    "source_pdf": "paper.pdf",
                                },
                            },
                        ]
                    },
                },
            )

            assert imported.status_code == 200, imported.text
            statuses = {item["status"] for item in imported.json()["candidates"]}
            assert statuses == {"ai_applied"}

            with Session(engine) as session:
                figure = session.scalars(select(PaperFigure)).one()
                section = session.scalars(select(PaperSection)).one()
                corrections = session.scalars(select(PaperCorrection)).all()

                assert figure.caption == "Fig. 2. Missing band structure."
                assert figure.figure_label == "fig_2"
                assert figure.crop_status == "needs_recrop"
                assert section.section_title == "Methods"
                assert section.text == "Methods section recovered from PDF text."
                assert session.scalars(select(PaperTable)).all() == []
                assert len(corrections) == 2
                assert {row.status for row in corrections} == {"approved"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_auto_applies_figure_delete_correction():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Delete duplicate figure", pdf_path="paper.pdf", authors=["Author A"])
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    figure_label="fig_4a",
                    caption="Duplicate right-column fragment of Fig. 4.",
                    page=6,
                    crop_status="needs_recrop",
                    figure_role="experimental_evidence",
                    content_summary="Duplicate crop fragment.",
                )
                session.add(figure)
                session.commit()
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="figures", locked_by="codex")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "codex_delete_test",
                    "auto_apply_review_rules": True,
                    "reviewer": "codex",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "correction_proposals": [
                            {
                                "field_name": "figures",
                                "target_path": f"figures:{figure_id}:delete",
                                "operation": "delete",
                                "proposed_value": None,
                                "reason": "Duplicate parser fragment of Fig. 4 should be removed after full-figure recrop.",
                                "evidence_payload": {
                                    "page": 6,
                                    "figure_label": "fig_4a",
                                    "quoted_text": "Duplicate right-column fragment of Fig. 4.",
                                },
                            }
                        ]
                    },
                },
            )

            assert imported.status_code == 200, imported.text
            statuses = {item["status"] for item in imported.json()["candidates"]}
            assert statuses == {"ai_applied"}

            with Session(engine) as session:
                stored_figure = session.get(PaperFigure, figure_id)
                corrections = session.scalars(select(PaperCorrection)).all()
                delete_logs = session.scalars(
                    select(AuditLog).where(AuditLog.action == "delete_structured_object")
                ).all()

                assert stored_figure is None
                assert len(corrections) == 1
                assert corrections[0].operation == "delete"
                assert corrections[0].status == "approved"
                assert len(delete_logs) == 1
                assert delete_logs[0].target_type == "figures"
                assert delete_logs[0].target_id == str(figure_id)
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_rejects_figure_recrop_submission(monkeypatch):
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(root / "storage"))
        get_settings.cache_clear()
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        import fitz

        pdf_path = root / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page(width=300, height=300)
        page.draw_rect(fitz.Rect(50, 60, 250, 220), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        page.insert_text((70, 140), "Figure 2 panel")
        doc.save(str(pdf_path))
        doc.close()

        try:
            with Session(engine) as session:
                paper = Paper(title="Recrop Paper", pdf_path=str(pdf_path), authors=["A"])
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    figure_label="fig_2",
                    caption="Figure 2. Test panel.",
                    page=1,
                    crop_status="needs_recrop",
                    crop_source="ide_ai_locator",
                    figure_role="property_data",
                    content_summary="Test panel with plotted property data.",
                    key_elements=["test panel"],
                )
                session.add(figure)
                session.commit()
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="figures", locked_by="gemini")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "gemini_recrop_test",
                    "auto_apply_review_rules": True,
                    "reviewer": "gemini",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "correction_proposals": [
                            {
                                "field_name": "figures",
                                "target_path": f"figures:{figure_id}:image_path",
                                "operation": "recrop_figure",
                                "proposed_value": {"bbox": {"l": 45, "t": 55, "r": 255, "b": 225}},
                                "reason": "Precise bbox for Figure 2.",
                                "evidence_payload": {
                                    "page": 1,
                                    "figure_label": "fig_2",
                                    "bbox": {"l": 45, "t": 55, "r": 255, "b": 225},
                                },
                            }
                        ]
                    },
                },
            )

            assert imported.status_code == 400
            assert imported.json()["detail"].startswith("direct_mcp_tool_required:recrop_figure")

            with Session(engine) as session:
                stored = session.get(PaperFigure, figure_id)
                assert stored is not None
                assert stored.image_path is None
                assert stored.crop_status == "needs_recrop"
                assert session.scalars(select(PaperCorrection)).all() == []
        finally:
            app.dependency_overrides.clear()
            get_settings.cache_clear()
            engine.dispose()


def test_import_analysis_preserves_si_new_dft_candidate_source_and_signature():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="SI DFT Paper", pdf_path="main.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            lock = _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="IDE AI SI rescan")
            response = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "IDE AI SI rescan",
                    "auto_apply_review_rules": True,
                    "write_lock_token": lock,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "target_type": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material": "Co-GDY",
                                    "adsorbate": "O2",
                                    "property_type": "adsorption_energy",
                                    "value": -1.23,
                                    "unit": "eV",
                                    "reaction_step": "O2 adsorption",
                                },
                                "evidence_location": {
                                    "source_document_type": "supplementary_information",
                                    "source_document_label": "SI",
                                    "source_locator": "Table S2, row Co-GDY, column E_ads",
                                    "page": 14,
                                    "quoted_text": "Co-GDY O2 -1.23 eV",
                                },
                            }
                        ]
                    },
                },
            )

            assert response.status_code == 200
            with Session(engine) as session:
                candidate = session.scalars(select(ExternalAnalysisCandidate)).one()
                payload = candidate.normalized_payload
                assert candidate.paper_id == paper_id
                assert candidate.candidate_type == "object_review_audit"
                assert payload["target_id"] == "new"
                assert payload["decision"] == "new_candidate"
                assert payload["evidence_location"]["source_document_type"] == "supplementary_information"
                assert payload["dedupe_signature"].startswith("dft:")
                assert candidate.evidence_payload["source_document_type"] == "supplementary_information"
                assert candidate.status == "materialized"
                assert candidate.materialized_target_type == "dft_results"
                assert candidate.materialized_target_id is not None
                stored_row = session.get(DFTResult, UUID(candidate.materialized_target_id))
                assert stored_row is not None
                assert stored_row.candidate_status == "new_candidate"
                assert stored_row.property_type == "adsorption_energy"
                assert stored_row.value == -1.23
                assert stored_row.unit == "eV"
                assert stored_row.evidence_payload["material_identity"] == "Co-GDY"
                locator = session.scalar(select(EvidenceLocator).where(EvidenceLocator.target_id == str(stored_row.id)))
                assert locator is not None
                assert locator.target_type == "dft_results"
                assert locator.page == 14
                gate = is_export_eligible_extraction(session, stored_row, target_type="dft_results")
                assert not gate.eligible
                assert "missing_review" in gate.reasons
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_marks_supporting_reference_candidate_as_borrowed():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                paper = Paper(title="Main Paper", pdf_path="main.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                settings = get_settings()
                service = ExternalAnalysisService(session=session, settings=settings)
                run = service.import_run(
                    paper_id=paper.id,
                    source="ide_ai",
                    source_label="IDE AI reference check",
                    raw_text=None,
                    raw_payload={
                        "object_review_audits": [
                            {
                                "target_type": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material": "Pt",
                                    "adsorbate": "H",
                                    "property_type": "adsorption_energy",
                                    "value": -0.2,
                                    "unit": "eV",
                                },
                                "evidence_location": {
                                    "source_document_type": "supporting_reference",
                                    "source_document_label": "Ref. 32",
                                    "page": 3,
                                    "quoted_text": "The value was reported in a prior paper.",
                                },
                            }
                        ]
                    },
                )
                candidate = service.list_candidates(run.id)[0]

                assert candidate.normalized_payload["borrowed_from_reference"] is True
                assert candidate.evidence_payload["borrowed_from_reference"] is True
                assert candidate.evidence_payload["source_document_type"] == "supporting_reference"
        finally:
            engine.dispose()


def test_external_analysis_relationship_resolution_stays_in_source_library():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                source = Paper(title="Source A", doi="10.1000/source-a", library_name="LibraryA", pdf_path="source.pdf", authors=[])
                wrong_library_target = Paper(title="Support B", doi="10.1000/shared-support", library_name="LibraryB", pdf_path="b.pdf", authors=[])
                right_library_target = Paper(title="Support A", doi="10.1000/shared-support", library_name="LibraryA", pdf_path="a.pdf", authors=[])
                session.add_all([source, wrong_library_target, right_library_target])
                session.commit()
                session.refresh(source)
                session.refresh(wrong_library_target)
                session.refresh(right_library_target)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(source.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "supporting_papers": [
                            {
                                "relationship_type": "supports",
                                "target_doi": "https://doi.org/10.1000/SHARED-SUPPORT",
                                "target_title": "Support A",
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200
            run_id = imported.json()["id"]

            materialized = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"explicit_all": True, "created_by": "reviewer_ai"},
            )
            assert materialized.status_code == 200
            assert materialized.json()["created_relationships"] == 1

            with Session(engine) as session:
                relationship = session.scalars(select(PaperRelationship)).one()
                assert relationship.target_paper_id == right_library_target.id
                assert relationship.target_paper_id != wrong_library_target.id
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_relationship_resolution_blocks_ambiguous_same_library_title():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                source = Paper(title="Source A", doi="10.1000/source-ambiguous", library_name="LibraryA", pdf_path="source.pdf", authors=[])
                first_target = Paper(title="Shared Support Title", library_name="LibraryA", pdf_path="first.pdf", authors=[])
                second_target = Paper(title="Shared Support Title", library_name="LibraryA", pdf_path="second.pdf", authors=[])
                session.add_all([source, first_target, second_target])
                session.commit()
                session.refresh(source)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(source.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "supporting_papers": [
                            {
                                "relationship_type": "supports",
                                "target_title": "Shared Support Title",
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200
            run_id = imported.json()["id"]
            assert imported.json()["candidates"][0]["action_mode"] == "readonly"
            assert imported.json()["candidates"][0]["action_scope"] == "candidate"

            with Session(engine) as session:
                candidate = session.scalars(select(ExternalAnalysisCandidate)).one()
                assert candidate.status == "requires_resolution"
                assert candidate.normalized_payload["target_paper_id"] is None
                assert "ambiguous" in candidate.mapping_reason

            materialized = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"explicit_all": True, "created_by": "reviewer_ai"},
            )
            assert materialized.status_code == 200
            assert materialized.json()["created_relationships"] == 0
            assert materialized.json()["skipped_candidates"] == 1

            with Session(engine) as session:
                assert session.query(PaperRelationship).count() == 0
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_materialize_rejects_empty_or_implicit_all():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Contract Paper", pdf_path="contract.pdf", authors=[])
                session.add(paper)
                session.commit()
                session.refresh(paper)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "review_notes": [{"content": "Candidate note.", "field_name": "abstract"}],
                    },
                },
            )
            assert imported.status_code == 200
            run_id = imported.json()["id"]

            empty_selection = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"candidate_ids": [], "created_by": "reviewer_ai"},
            )
            assert empty_selection.status_code == 400
            assert "candidate_ids=[]" in empty_selection.json()["detail"]

            implicit_all = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"created_by": "reviewer_ai"},
            )
            assert implicit_all.status_code == 400
            assert "explicit_all=true" in implicit_all.json()["detail"]

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 0
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_paper_level_audit_payload_creates_unverified_candidate():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Gemini Audit Paper", pdf_path="paper.pdf", authors=[])
                session.add(paper)
                session.flush()
                _make_external_audit_ready(paper, Path(tmpdir))
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "gemini_external_audit",
                    "raw_payload": {
                        "paper_id": str(paper_id),
                        "verdict": "WARN",
                        "recommended_action": "needs_dft_review",
                        "suspected_missing": ["dft_result"],
                        "metadata_status": "ok",
                        "section_structure_status": "warn",
                        "table_status": "warn",
                        "figure_status": "ok",
                        "dft_status": "warn",
                        "evidence_examples": [{"text": "DFT was mentioned but no DFT result was found."}],
                    },
                },
            )

            assert imported.status_code == 200
            payload = imported.json()
            assert len(payload["candidates"]) == 1
            candidate = payload["candidates"][0]
            assert candidate["candidate_type"] == "external_audit_opinion"
            assert candidate["status"] == "candidate"
            assert candidate["normalized_payload"]["source"] == "gemini_external_audit"
            assert candidate["normalized_payload"]["verdict"] == "WARN"
            assert candidate["normalized_payload"]["recommended_action"] == "needs_dft_review"
            assert candidate["normalized_payload"]["verification_status"] == "unverified"

            center = client.get("/api/workbench/review-center")
            assert center.status_code == 200
            row = next(item for item in center.json()["rows"] if item["paper_id"] == str(paper_id))
            assert row["external_audit_count"] == 1
            assert row["external_audit_source_counts"] == {"gemini_external_audit": 1}
            assert row["external_audit_opinions"][0]["candidate_type"] == "external_audit_opinion"
            assert row["external_audit_opinions"][0]["verification_status"] == "unverified"

            with Session(engine) as session:
                candidate_row = session.query(ExternalAnalysisCandidate).one()
                assert candidate_row.status == "candidate"
                assert candidate_row.candidate_type == "external_audit_opinion"
                assert candidate_row.materialized_target_type is None
                assert candidate_row.materialized_target_id is None
                assert candidate_row.normalized_payload["verification_status"] == "unverified"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_object_level_dft_audit_payload_creates_unverified_candidate():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Object DFT Audit Paper", pdf_path="object-dft.pdf", authors=[])
                session.add(paper)
                session.flush()
                row = DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    adsorbate="Li2S4",
                    value=-1.20,
                    unit="eV",
                    evidence_text="The adsorption energy is reported in Table 1.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            raw_item = {
                "paper_id": str(paper_id),
                "target_type": "dft_results",
                "target_id": str(row_id),
                "field_name": "value",
                "decision": "REVISE",
                "evidence_checked": True,
                "evidence_location": {"page": 7, "section": "Results", "table": "Table 1"},
                "blocking_errors": ["value_mismatch"],
                "recommended_action": "propose_correction",
                "corrected_value": -1.35,
                "confidence": 0.72,
                "source": "glm_dft_audit",
                "source_label": "GLM DFT audit",
                "agent_role": "dft_auditor",
                "model_name": "glm-test",
                "reason": "Table 1 reports -1.35 eV.",
                "writes_final_truth": False,
                "human_confirmation_required": True,
            }
            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "glm_dft_audit",
                    "source_label": "GLM DFT audit",
                    "raw_payload": {"object_review_audits": [raw_item]},
                },
            )

            assert imported.status_code == 200
            payload = imported.json()
            assert len(payload["candidates"]) == 1
            candidate = payload["candidates"][0]
            assert candidate["candidate_type"] == "object_review_audit"
            assert candidate["status"] == "candidate"
            assert candidate["normalized_payload"]["target_type"] == "dft_results"
            assert candidate["normalized_payload"]["target_id"] == str(row_id)
            assert candidate["normalized_payload"]["field_name"] == "value"
            assert candidate["normalized_payload"]["decision"] == "REVISE"
            assert candidate["normalized_payload"]["verification_status"] == "unverified"
            assert candidate["normalized_payload"]["writes_final_truth"] is False
            assert candidate["normalized_payload"]["human_confirmation_required"] is True
            assert candidate["normalized_payload"]["raw_payload"]["corrected_value"] == -1.35

            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                stored_candidate = session.query(ExternalAnalysisCandidate).one()
                assert stored_row.value == -1.20
                assert stored_row.candidate_status == "system_candidate"
                assert stored_candidate.materialized_target_type is None
                assert stored_candidate.materialized_target_id is None
                assert stored_candidate.evidence_payload["verification_status"] == "unverified"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_object_level_writing_card_audit_payload_is_candidate_only():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Object Writing Audit Paper", pdf_path="object-writing.pdf", authors=[])
                session.add(paper)
                session.flush()
                card = WritingCard(
                    paper_id=paper.id,
                    paper_type="research",
                    research_gap="Lithium sulfur mechanism gap.",
                    proposed_solution="Single atom catalyst.",
                    core_hypothesis="Polar sites anchor polysulfides.",
                )
                session.add(card)
                session.commit()
                paper_id = paper.id
                card_id = card.id

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "assigned_writing_card_audit",
                    "source_label": "Assigned AI writing-card audit",
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "writing_cards",
                                "target_id": str(card_id),
                                "field_name": "core_hypothesis",
                                "decision": "FLAG",
                                "evidence_checked": True,
                                "evidence_location": {"page": 3, "section": "Discussion"},
                                "blocking_errors": ["unsupported_causality"],
                                "recommended_action": "needs_human_review",
                                "confidence": 0.64,
                                "agent_role": "writing_card_auditor",
                                "model_name": "claude-test",
                                "reason": "The causal claim needs a qualifier.",
                            }
                        ]
                    },
                },
            )

            assert imported.status_code == 200
            candidate = imported.json()["candidates"][0]
            assert candidate["candidate_type"] == "object_review_audit"
            assert candidate["normalized_payload"]["target_type"] == "writing_cards"
            assert candidate["normalized_payload"]["field_name"] == "core_hypothesis"
            assert candidate["normalized_payload"]["verification_status"] == "unverified"
            assert candidate["normalized_payload"]["writes_final_truth"] is False

            with Session(engine) as session:
                stored_card = session.get(WritingCard, card_id)
                assert stored_card.core_hypothesis == "Polar sites anchor polysulfides."
                assert session.query(PaperCorrection).count() == 0
                assert session.query(ExternalAnalysisCandidate).one().status == "candidate"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_materializes_single_ai_anchored_content():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Single AI Auto Apply Paper",
                    abstract="Original abstract.",
                    pdf_path="single-auto.pdf",
                    authors=[],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="content", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "ide-ai-main",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "review_notes": [
                            {
                                "content": "The abstract should be softened.",
                                "field_name": "abstract",
                                "page": 1,
                                "quoted_text": "Original abstract.",
                            }
                        ],
                        "correction_proposals": [
                            {
                                "field_name": "abstract",
                                "target_path": "abstract",
                                "operation": "replace",
                                "proposed_value": "Updated abstract from IDE AI.",
                                "reason": "The original wording is too strong.",
                                "evidence_payload": {"page": 1, "quoted_text": "Original abstract."},
                            }
                        ],
                    },
                },
            )

            assert imported.status_code == 200
            with Session(engine) as session:
                stored_paper = session.get(Paper, paper_id)
                notes = session.query(PaperNote).all()
                corrections = session.query(PaperCorrection).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            assert stored_paper is not None
            assert stored_paper.abstract == "Updated abstract from IDE AI."
            assert len(notes) == 1
            assert notes[0].quoted_text == "Original abstract."
            assert len(corrections) == 1
            assert corrections[0].status == "approved"
            assert {candidate.status for candidate in candidates} == {"ai_reviewed", "ai_applied"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_applies_non_dft_structured_modules():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Non-DFT Structured Auto Apply Paper",
                    abstract="Original abstract.",
                    pdf_path="non-dft-auto.pdf",
                    authors=[],
                )
                session.add(paper)
                session.flush()
                sample = CatalystSample(paper_id=paper.id, name="Fe-N-C", catalyst_type="old")
                claim = MechanismClaim(
                    paper_id=paper.id,
                    claim_type="old",
                    claim_text="Old mechanism claim.",
                    evidence_types=[],
                    confidence=0.4,
                )
                performance = ElectrochemicalPerformance(
                    paper_id=paper.id,
                    capacity_value=600.0,
                    cycle_number=100,
                    rate="1 C",
                    evidence_text="old performance",
                )
                session.add_all([sample, claim, performance])
                session.commit()
                paper_id = paper.id
                sample_id = sample.id
                claim_id = claim.id
                performance_id = performance.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="content", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "ide-ai-main",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "correction_proposals": [
                            {
                                "field_name": "mechanism_claims",
                                "target_path": f"mechanism_claims:{claim_id}:claim_text",
                                "operation": "replace",
                                "proposed_value": "Fe-N-C promotes polysulfide conversion under the reported conditions.",
                                "reason": "The revised claim matches the quoted discussion.",
                                "evidence_payload": {"page": 4, "section": "Results", "quoted_text": "Fe-N-C promotes conversion."},
                            },
                            {
                                "field_name": "electrochemical_performance",
                                "target_path": f"electrochemical_performance:{performance_id}:capacity_value",
                                "operation": "replace",
                                "proposed_value": 720.0,
                                "reason": "The table reports 720 mAh g-1.",
                                "evidence_payload": {"page": 5, "table": "Table 1", "quoted_text": "720 mAh g-1"},
                            },
                            {
                                "field_name": "catalyst_samples",
                                "target_path": f"catalyst_samples:{sample_id}:catalyst_type",
                                "operation": "replace",
                                "proposed_value": "single_atom_catalyst",
                                "reason": "The material is described as an atomically dispersed Fe catalyst.",
                                "evidence_payload": {"page": 3, "section": "Synthesis", "quoted_text": "atomically dispersed Fe sites"},
                            },
                        ]
                    },
                },
            )

            assert imported.status_code == 200, imported.text
            with Session(engine) as session:
                stored_sample = session.get(CatalystSample, sample_id)
                stored_claim = session.get(MechanismClaim, claim_id)
                stored_performance = session.get(ElectrochemicalPerformance, performance_id)
                corrections = session.query(PaperCorrection).order_by(PaperCorrection.created_at.asc()).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            assert stored_sample is not None
            assert stored_sample.catalyst_type == "single_atom_catalyst"
            assert stored_claim is not None
            assert stored_claim.claim_text == "Fe-N-C promotes polysulfide conversion under the reported conditions."
            assert stored_performance is not None
            assert stored_performance.capacity_value == 720.0
            assert len(corrections) == 3
            assert {correction.status for correction in corrections} == {"approved"}
            assert {candidate.status for candidate in candidates} == {"ai_applied"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_import_rejects_empty_payload():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Empty Import Paper", pdf_path="empty.pdf", authors=[])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "empty-import",
                    "raw_text": "",
                    "raw_payload": None,
                    "auto_apply_review_rules": True,
                },
            )

            assert imported.status_code == 422
            assert "import_analysis requires non-empty raw_text or raw_payload" in imported.text

            with Session(engine) as session:
                assert session.query(ExternalAnalysisRun).count() == 0
                assert session.query(WorkflowJob).count() == 0
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_requires_dual_ai_for_dft():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Dual AI DFT Paper", pdf_path="dual-dft.pdf", authors=[])
                session.add(paper)
                session.flush()
                catalyst = CatalystSample(
                    paper_id=paper.id,
                    name="Li2S4 on graphdiyne",
                    catalyst_type="graphdiyne",
                    support="graphdiyne",
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
                    source_section="Results",
                    evidence_text="Table 1 reports -1.20 eV.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            client = TestClient(app)
            lock = _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="ide_ai")
            base_payload = {
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "auto_apply_review_rules": True,
                "reviewer": "ide_ai",
                "write_lock_token": lock,
                "raw_payload": {
                    "object_review_audits": [
                        {
                            "paper_id": str(paper_id),
                            "target_type": "dft_results",
                            "target_id": str(row_id),
                            "field_name": "value",
                            "decision": "PASS",
                            "corrected_value": -1.20,
                            "confidence": 0.91,
                            "reason": "Table 1 confirms the value.",
                            "evidence_location": {"page": 7, "section": "Results", "table": "Table 1", "quoted_text": "-1.20 eV"},
                        }
                    ]
                },
            }
            first = client.post(
                "/api/external-analysis/import",
                json={**base_payload, "source_label": "ide-ai-1"},
            )
            second = client.post(
                "/api/external-analysis/import",
                json={**base_payload, "source_label": "ide-ai-2"},
            )
            assert first.status_code == 200
            assert second.status_code == 200
            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            assert stored_row is not None
            assert stored_row.candidate_status != "system_candidate"
            assert {candidate.status for candidate in candidates} == {"materialized"}

            conflict = client.post(
                "/api/external-analysis/import",
                json={
                    **base_payload,
                    "source_label": "ide-ai-3",
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "dft_results",
                                "target_id": str(row_id),
                                "field_name": "value",
                                "decision": "REVISE",
                                "corrected_value": -1.35,
                                "confidence": 0.88,
                                "reason": "Conflicting audit after settlement.",
                                "evidence_location": {"page": 7, "table": "Table 1", "quoted_text": "-1.35 eV"},
                            }
                        ]
                    },
                },
            )
            assert conflict.status_code == 200
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_dft_dual_ai_missing_material_identity_stays_pending():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="DFT missing identity paper", pdf_path="dft-missing-identity.pdf", authors=[])
                session.add(paper)
                session.flush()
                row = DFTResult(
                    paper_id=paper.id,
                    property_type="band_gap",
                    value=1.20,
                    unit="eV",
                    source_section="Results",
                    evidence_text="Table 1 reports a 1.20 eV band gap.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            client = TestClient(app)
            write_lock_tokens = [
                _acquire_write_lock(client, paper_id, module_name="catalyst_samples", locked_by="ide_ai"),
                _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="ide_ai"),
            ]
            payload = {
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "auto_apply_review_rules": True,
                "reviewer": "ide_ai",
                "write_lock_tokens": write_lock_tokens,
                "raw_payload": {
                    "object_review_audits": [
                        {
                            "paper_id": str(paper_id),
                            "target_type": "dft_results",
                            "target_id": str(row_id),
                            "field_name": "value",
                            "decision": "PASS",
                            "corrected_value": 1.20,
                            "confidence": 0.9,
                            "reason": "Table 1 confirms the value, but the material identity is not bound.",
                            "normalized_energy_type": "band_gap",
                            "evidence_location": {"page": 4, "table": "Table 1", "quoted_text": "1.20 eV"},
                        }
                    ]
                },
            }

            first = client.post("/api/external-analysis/import", json={**payload, "source_label": "ide-ai-1"})
            second = client.post("/api/external-analysis/import", json={**payload, "source_label": "ide-ai-2"})

            assert first.status_code == 200
            assert second.status_code == 200
            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()
                reviews = session.query(ExtractionFieldReview).all()

            assert stored_row is not None
            assert stored_row.candidate_status == "human_reviewed_needs_evidence"
            assert {candidate.status for candidate in candidates} == {"materialized"}
            assert reviews
            assert {review.reviewer_status for review in reviews} == {"verified"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_dft_dual_ai_requires_same_material_identity():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="DFT identity conflict paper", pdf_path="dft-identity.pdf", authors=[])
                session.add(paper)
                session.flush()
                catalyst = CatalystSample(
                    paper_id=paper.id,
                    name="alpha-GDY",
                    catalyst_type="graphdiyne",
                    support="graphdiyne",
                )
                session.add(catalyst)
                session.flush()
                row = DFTResult(
                    paper_id=paper.id,
                    catalyst_sample_id=catalyst.id,
                    property_type="band_gap",
                    value=1.20,
                    unit="eV",
                    source_section="Results",
                    evidence_text="Table 1 reports a 1.20 eV band gap.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            client = TestClient(app)
            write_lock_tokens = [
                _acquire_write_lock(client, paper_id, module_name="catalyst_samples", locked_by="ide_ai"),
                _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="ide_ai"),
            ]

            def payload_for(source_label: str, material: str) -> dict[str, Any]:
                return {
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": source_label,
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_tokens": write_lock_tokens,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "dft_results",
                                "target_id": str(row_id),
                                "field_name": "value",
                                "decision": "PASS",
                                "corrected_value": 1.20,
                                "confidence": 0.9,
                                "reason": "Table 1 confirms the value.",
                                "normalized_energy_type": "band_gap",
                                "normalized_material": material,
                                "evidence_location": {"page": 4, "table": "Table 1", "quoted_text": "1.20 eV"},
                            }
                        ]
                    },
                }

            first = client.post("/api/external-analysis/import", json=payload_for("ide-ai-1", "alpha-GDY"))
            second = client.post("/api/external-analysis/import", json=payload_for("ide-ai-2", "beta-GDY"))

            assert first.status_code == 200
            assert second.status_code == 200
            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()
                conflicts = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper_id)

            assert stored_row is not None
            assert stored_row.candidate_status == "system_candidate"
            assert {candidate.status for candidate in candidates} == {"candidate"}
            assert conflicts["conflict_count"] == 1
            assert "identity_conflict" in conflicts["rows"][0]["conflict_types"]
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_new_dft_candidates_do_not_create_target_new_conflicts():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                paper = Paper(title="New Candidate Conflict Paper", pdf_path="new-candidates.pdf", authors=[])
                session.add(paper)
                session.flush()
                run = ExternalAnalysisRun(
                    paper_id=paper.id,
                    source="ide_ai",
                    source_label="gemini_table_review",
                    raw_payload={},
                    normalized_payload={},
                    mapping_status="mapped",
                )
                session.add(run)
                session.flush()
                materialized_ids = [UUID(int=1), UUID(int=2)]
                for value, materialized_id in zip((1.2, 3.4), materialized_ids, strict=True):
                    session.add(
                        ExternalAnalysisCandidate(
                            run_id=run.id,
                            paper_id=paper.id,
                            candidate_type="object_review_audit",
                            normalized_payload={
                                "paper_id": str(paper.id),
                                "target_type": "dft_results",
                                "target_id": "new",
                                "field_name": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material": "GDY membrane",
                                    "property": "permeance",
                                    "value": value,
                                    "unit": "GPU",
                                    "source_table": "Table 1",
                                },
                                "evidence_location": {"page": 6, "table": "Table 1"},
                            },
                            status="materialized",
                            materialized_target_type="dft_results",
                            materialized_target_id=str(materialized_id),
                        )
                    )
                session.commit()

                conflicts = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id)

            assert conflicts["conflict_count"] == 0
        finally:
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_single_ai_applies_figures():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Dual AI Figure Paper", pdf_path="dual-figure.pdf", authors=[])
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    caption="Figure 1",
                    content_summary="Old summary",
                    figure_label="Figure 1",
                    page=5,
                )
                session.add(figure)
                session.commit()
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="figures", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "ide-ai-1",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "figure",
                                "target_id": str(figure_id),
                                "field_name": "content_summary",
                                "decision": "REVISE",
                                "corrected_value": "Updated figure summary from a single AI review.",
                                "confidence": 0.83,
                                "reason": "The original summary missed the main comparison.",
                                "evidence_location": {"page": 5, "figure": "Figure 1", "quoted_text": "Figure 1 compares..."},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                stored_figure = session.get(PaperFigure, figure_id)
                corrections = session.query(PaperCorrection).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            assert stored_figure is not None
            assert stored_figure.content_summary == "Updated figure summary from a single AI review."
            assert len(corrections) == 1
            assert corrections[0].status == "approved"
            assert {candidate.status for candidate in candidates} == {"ai_applied"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_figure_summary_strips_caption_echo_prefix():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Caption echo repair", pdf_path="dual-figure.pdf", authors=[])
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    caption="Fig. 2 | Structural characterization of HEASA-Pt2.3%, Pt1-NiCoMgBiSn.",
                    content_summary="Old summary",
                    figure_label="fig_2",
                    figure_role="characterization",
                    page=5,
                    image_path="figures/fig2.png",
                    key_elements=["HAADF-STEM", "EXAFS"],
                )
                session.add(figure)
                session.commit()
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="figures", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "ide-ai-caption-echo-fix",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "figure",
                                "target_id": str(figure_id),
                                "field_name": "content_summary",
                                "decision": "REVISE",
                                "corrected_value": (
                                    "Fig. 2 | Structural characterization of HEASA-Pt2.3%, Pt1-NiCoMgBiSn. "
                                    "(a) HAADF-STEM image with EDS elemental maps for Pt, Ni, Co, Mg, Bi, and Sn. "
                                    "(b-f) XANES/EXAFS comparisons and Pt-Pt coordination number chart."
                                ),
                                "confidence": 0.9,
                                "reason": "The summary should describe the panels rather than repeat the caption.",
                                "evidence_location": {"page": 5, "figure": "Fig. 2", "quoted_text": "a Aberration-corrected HAADF-STEM image"},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                stored_figure = session.get(PaperFigure, figure_id)

            assert stored_figure is not None
            assert stored_figure.content_summary == (
                "(a) HAADF-STEM image with EDS elemental maps for Pt, Ni, Co, Mg, Bi, and Sn. "
                "(b-f) XANES/EXAFS comparisons and Pt-Pt coordination number chart."
            )
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_figure_key_elements_normalizes_stringified_dicts():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Figure key elements cleanup", pdf_path="figure.pdf", authors=[])
                session.add(paper)
                session.flush()
                figure = PaperFigure(
                    paper_id=paper.id,
                    caption="Figure 2",
                    content_summary="(a) STEM image and (b) EXAFS fitting.",
                    figure_label="fig_2",
                    figure_role="characterization",
                    page=5,
                    image_path="figures/fig2.png",
                    key_elements=["old"],
                )
                session.add(figure)
                session.commit()
                paper_id = paper.id
                figure_id = figure.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="figures", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "ide-ai-key-elements-fix",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "figure",
                                "target_id": str(figure_id),
                                "field_name": "key_elements",
                                "decision": "REVISE",
                                "corrected_value": [
                                    "{'description': 'Panel (a): HAADF-STEM image with atomically dispersed Pt'}",
                                    "{'description': 'Panel (b): EXAFS fitting and coordination-number comparison'}",
                                ],
                                "confidence": 0.89,
                                "reason": "Normalize historical stringified dict entries into plain list items.",
                                "evidence_location": {"page": 5, "figure": "Figure 2", "quoted_text": "EXAFS fitting"},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                stored_figure = session.get(PaperFigure, figure_id)

            assert stored_figure is not None
            assert stored_figure.key_elements == [
                "Panel (a): HAADF-STEM image with atomically dispersed Pt",
                "Panel (b): EXAFS fitting and coordination-number comparison",
            ]
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_single_ai_accepts_tables():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Single AI Table Accept", pdf_path="table-accept.pdf", authors=[])
                session.add(paper)
                session.flush()
                table = PaperTable(
                    paper_id=paper.id,
                    caption="Table 1. Original caption",
                    markdown_content="| col | value |\n| --- | --- |\n| A | 1 |",
                    page=4,
                    extraction_source="docling",
                )
                session.add(table)
                session.commit()
                paper_id = paper.id
                table_id = table.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="tables", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "table-accept-ai",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "table",
                                "target_id": str(table_id),
                                "field_name": "table_review",
                                "decision": "PASS",
                                "confidence": 0.88,
                                "reason": "The table caption, page, and markdown content match the PDF.",
                                "evidence_location": {"page": 4, "table": "Table 1", "quoted_text": "Table 1. Original caption"},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            detail = client.get(f"/api/papers/{paper_id}")
            assert detail.status_code == 200
            table_payload = detail.json()["tables"][0]

            assert {candidate.status for candidate in candidates} == {"ai_reviewed"}
            assert table_payload["table_review_status"] == "verified"
            assert table_payload["object_review_audit_count"] == 1
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_single_ai_rejects_tables():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Single AI Table Reject", pdf_path="table-reject.pdf", authors=[])
                session.add(paper)
                session.flush()
                table = PaperTable(
                    paper_id=paper.id,
                    caption="Table X. Duplicate parser artifact",
                    markdown_content="| bad | row |",
                    page=6,
                    extraction_source="docling",
                )
                session.add(table)
                session.commit()
                paper_id = paper.id
                table_id = table.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="tables", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "table-reject-ai",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "tables",
                                "target_id": str(table_id),
                                "field_name": "table_review",
                                "decision": "REJECT",
                                "confidence": 0.84,
                                "reason": "This parser artifact is not a valid source table and should be rejected.",
                                "evidence_location": {"page": 6, "quoted_text": "No corresponding source table exists on the page."},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            detail = client.get(f"/api/papers/{paper_id}")
            assert detail.status_code == 200
            table_payload = detail.json()["tables"][0]

            assert {candidate.status for candidate in candidates} == {"ai_reviewed"}
            assert table_payload["table_review_status"] == "rejected"
            assert table_payload["object_review_audit_count"] == 1
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_table_audit_corrected_value_requires_direct_tool():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Single AI Table Revise", pdf_path="table-revise.pdf", authors=[])
                session.add(paper)
                session.flush()
                table = PaperTable(
                    paper_id=paper.id,
                    caption="Table 2. Truncated caption",
                    markdown_content="| item | number |\n| --- | --- |\n| B | 2 |",
                    page=5,
                    extraction_source="docling",
                )
                session.add(table)
                session.commit()
                paper_id = paper.id
                table_id = table.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="tables", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "table-revise-ai",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "table",
                                "target_id": str(table_id),
                                "field_name": "caption",
                                "decision": "REVISE",
                                "corrected_value": "Table 2. Corrected full caption from the PDF",
                                "confidence": 0.9,
                                "reason": "The parser truncated the table caption.",
                                "evidence_location": {"page": 5, "table": "Table 2", "quoted_text": "Table 2. Corrected full caption from the PDF"},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 200

            with Session(engine) as session:
                stored_table = session.get(PaperTable, table_id)
                corrections = session.query(PaperCorrection).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            detail = client.get(f"/api/papers/{paper_id}")
            assert detail.status_code == 200
            table_payload = detail.json()["tables"][0]

            assert stored_table is not None
            assert stored_table.caption == "Table 2. Truncated caption"
            assert corrections == []
            assert {candidate.status for candidate in candidates} == {"requires_resolution"}
            assert table_payload["table_review_status"] != "verified"
            assert table_payload["object_review_audit_count"] == 1
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_import_analysis_rejects_legacy_codex_item_table_correction():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Legacy Codex Item Table", pdf_path="legacy-table.pdf", authors=[])
                session.add(paper)
                session.flush()
                table = PaperTable(
                    paper_id=paper.id,
                    caption="Table legacy",
                    markdown_content="broken markdown",
                    page=8,
                    extraction_source="docling",
                )
                session.add(table)
                session.commit()
                paper_id = paper.id
                table_id = table.id

            client = TestClient(app)
            write_lock_token = _acquire_write_lock(client, paper_id, module_name="tables", locked_by="ide_ai")
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "legacy-codex-item-table",
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "write_lock_token": write_lock_token,
                    "raw_payload": {
                        "correction_proposals": [
                            {
                                "field_name": "markdown_content",
                                "target_path": f"codex_item:{table_id}",
                                "operation": "replace",
                                "proposed_value": "fixed markdown table",
                                "reason": "Legacy codex_item correction should map back to the table markdown_content field.",
                                "evidence_payload": {"page": 8, "table": "Table legacy", "quoted_text": "fixed markdown table"},
                            }
                        ]
                    },
                },
            )
            assert imported.status_code == 400
            assert "direct_mcp_tool_required:update_table" in imported.text

            with Session(engine) as session:
                stored_table = session.get(PaperTable, table_id)
                corrections = session.query(PaperCorrection).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()

            assert stored_table is not None
            assert stored_table.markdown_content == "broken markdown"
            assert corrections == []
            assert candidates == []
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_auto_apply_review_rules_can_bind_dft_to_catalyst_sample():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Dual AI DFT Binding Paper", pdf_path="dual-binding.pdf", authors=[])
                session.add(paper)
                session.flush()
                catalyst = CatalystSample(
                    paper_id=paper.id,
                    name="Vacancy graphene",
                    catalyst_type="defective_graphene",
                    coordination="single vacancy",
                    support="graphene",
                )
                row = DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    adsorbate="Li2S4",
                    value=-1.20,
                    unit="eV",
                    source_section="Results",
                    evidence_text="Table 2 reports adsorption on vacancy graphene.",
                    candidate_status="system_candidate",
                )
                session.add_all([catalyst, row])
                session.commit()
                paper_id = paper.id
                row_id = row.id
                catalyst_id = catalyst.id

            client = TestClient(app)
            lock = _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="ide_ai")
            base_payload = {
                "paper_id": str(paper_id),
                "source": "ide_ai",
                "auto_apply_review_rules": True,
                "reviewer": "ide_ai",
                "write_lock_token": lock,
                "raw_payload": {
                    "object_review_audits": [
                        {
                            "paper_id": str(paper_id),
                            "target_type": "dft_results",
                            "target_id": str(row_id),
                            "field_name": "catalyst_sample_id",
                            "decision": "REVISE",
                            "corrected_value": str(catalyst_id),
                            "confidence": 0.92,
                            "reason": "Table 2 and the surrounding paragraph both attribute this adsorption energy to vacancy graphene.",
                            "normalized_material": "vacancy graphene",
                            "structure_name": "single vacancy graphene",
                            "adsorbate": "Li2S4",
                            "reaction_step": "adsorption",
                            "evidence_location": {"page": 6, "section": "Results", "table": "Table 2", "quoted_text": "vacancy graphene"},
                        }
                    ]
                },
            }
            first = client.post("/api/external-analysis/import", json={**base_payload, "source_label": "ide-ai-1"})
            second = client.post("/api/external-analysis/import", json={**base_payload, "source_label": "ide-ai-2"})

            assert first.status_code == 200
            assert second.status_code == 200

            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                corrections = session.query(PaperCorrection).all()
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()
                gate = is_export_eligible_extraction(session, stored_row, target_type="dft_results")

            assert stored_row is not None
            assert stored_row.catalyst_sample_id == catalyst_id
            assert len(corrections) == 1
            assert corrections[0].status == "approved"
            assert corrections[0].target_path == f"dft_results:{row_id}:catalyst_sample_id"
            assert corrections[0].evidence_payload["review_source_label"] in {"ide-ai-1", "ide-ai-2"}
            assert {candidate.status for candidate in candidates} == {"ai_applied"}
            assert "missing_material_identity" not in gate.reasons
            assert "missing_review" in gate.reasons
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_object_level_audit_payloads_participate_in_conflict_aggregation():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Object Conflict Paper", pdf_path="object-conflict.pdf", authors=[])
                session.add(paper)
                session.flush()
                row = DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    adsorbate="Li2S8",
                    value=-0.50,
                    unit="eV",
                    evidence_text="DFT evidence.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            client = TestClient(app)
            for source, decision, corrected_value in [
                ("gemini_dft_audit", "PASS", -0.50),
                ("glm_dft_audit", "REVISE", -0.70),
            ]:
                imported = client.post(
                    "/api/external-analysis/import",
                    json={
                        "paper_id": str(paper_id),
                        "source": source,
                        "source_label": source,
                        "raw_payload": {
                            "object_review_audits": [
                                {
                                    "paper_id": str(paper_id),
                                    "target_type": "dft_results",
                                    "target_id": str(row_id),
                                    "field_name": "value",
                                    "decision": decision,
                                    "evidence_checked": True,
                                    "evidence_location": {"page": 5},
                                    "recommended_action": "review_candidate",
                                    "corrected_value": corrected_value,
                                    "confidence": 0.7,
                                    "source": source,
                                    "agent_role": "dft_auditor",
                                    "normalized_energy_type": "adsorption_energy",
                                }
                            ]
                        },
                    },
                )
                assert imported.status_code == 200

            with Session(engine) as session:
                payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper_id)
                stored_row = session.get(DFTResult, row_id)

            assert payload["conflict_count"] == 1
            conflict = payload["rows"][0]
            assert conflict["target_type"] == "dft_results"
            assert conflict["field_name"] == "value"
            assert "value_conflict" in conflict["conflict_types"]
            assert "decision_conflict" in conflict["conflict_types"]
            assert conflict["target_summary"]["property_type"] == "adsorption_energy"
            assert conflict["target_summary"]["adsorbate"] == "Li2S8"
            assert conflict["anchor_summary"]["page"] == 5
            assert conflict["opinions"][0]["identity"]["normalized_energy_type"] == "adsorption_energy"
            assert {item["source_type"] for item in conflict["opinions"]} == {"object_review_audit"}
            assert stored_row.candidate_status == "system_candidate"
            assert stored_row.value == -0.50
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_third_ai_can_adjudicate_dual_ai_disagreement():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Third AI DFT Paper", pdf_path="third-ai.pdf", authors=[])
                session.add(paper)
                session.flush()
                row = DFTResult(
                    paper_id=paper.id,
                    property_type="adsorption_energy",
                    adsorbate="Li2S6",
                    value=-1.10,
                    unit="eV",
                    source_section="Results",
                    evidence_text="Table 3 reports the adsorption energies.",
                    candidate_status="system_candidate",
                )
                session.add(row)
                session.commit()
                paper_id = paper.id
                row_id = row.id

            client = TestClient(app)
            write_lock_tokens = [
                _acquire_write_lock(client, paper_id, module_name="catalyst_samples", locked_by="ide_ai"),
                _acquire_write_lock(client, paper_id, module_name="dft_results", locked_by="ide_ai"),
            ]

            def payload_for(
                source_label: str,
                decision: str,
                corrected_value: float,
                confidence: float,
                adjudication_role: str | None = None,
                write_lock_tokens: list[str] | None = None,
            ):
                body = {
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": source_label,
                    "auto_apply_review_rules": True,
                    "reviewer": "ide_ai",
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "paper_id": str(paper_id),
                                "target_type": "dft_result",
                                "target_id": str(row_id),
                                "field_name": "value",
                                "decision": decision,
                                "corrected_value": corrected_value,
                                "confidence": confidence,
                                "reason": "Compare against Table 3 and the surrounding paragraph.",
                                "normalized_energy_type": "adsorption_energy",
                                "normalized_material": "Co-N-C host",
                                "structure_name": "CoN4 single-atom site",
                                "adsorbate": "Li2S6",
                                "reaction_step": "adsorption",
                                "selected_source_ids": ["ide-ai-1", "ide-ai-2"],
                                "evidence_location": {"page": 8, "section": "Results", "table": "Table 3", "quoted_text": "-1.26 eV"},
                                **({"adjudication_role": adjudication_role, "adjudication_scope": "conflict_resolution"} if adjudication_role else {}),
                            }
                        ]
                    },
                }
                if write_lock_tokens:
                    body["write_lock_tokens"] = write_lock_tokens
                return body

            first = client.post(
                "/api/external-analysis/import",
                json=payload_for("ide-ai-1", "PASS", -1.10, 0.81, write_lock_tokens=write_lock_tokens),
            )
            second = client.post(
                "/api/external-analysis/import",
                json=payload_for("ide-ai-2", "REVISE", -1.26, 0.84, write_lock_tokens=write_lock_tokens),
            )
            third = client.post(
                "/api/external-analysis/import",
                json=payload_for(
                    "ide-ai-3",
                    "REVISE",
                    -1.26,
                    0.92,
                    adjudication_role="third_ai",
                    write_lock_tokens=write_lock_tokens,
                ),
            )

            assert first.status_code == 200
            assert second.status_code == 200
            assert third.status_code == 200

            with Session(engine) as session:
                stored_row = session.get(DFTResult, row_id)
                candidates = session.query(ExternalAnalysisCandidate).order_by(ExternalAnalysisCandidate.created_at.asc()).all()
                corrections = session.query(PaperCorrection).all()

            assert stored_row is not None
            assert stored_row.value == -1.26
            value_corrections = [
                item
                for item in corrections
                if item.field_name == "dft_results" and item.target_path == f"dft_results:{row_id}:value"
            ]
            catalyst_corrections = [item for item in corrections if item.field_name == "catalyst_samples"]
            binding_corrections = [
                item
                for item in corrections
                if item.field_name == "dft_results" and item.target_path == f"dft_results:{row_id}:catalyst_sample_id"
            ]
            assert len(value_corrections) == 1
            assert catalyst_corrections
            assert binding_corrections
            assert all(item.status == "approved" for item in corrections)
            assert value_corrections[0].evidence_payload["adjudication_role"] == "third_ai"
            assert value_corrections[0].evidence_payload["selected_source_ids"] == ["ide-ai-1", "ide-ai-2"]
            assert {candidate.status for candidate in candidates} == {"materialized"}
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_external_analysis_delete_post_alias_and_utc_created_at():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Delete Contract Paper", pdf_path="delete.pdf", authors=[])
                session.add(paper)
                session.commit()
                session.refresh(paper)

            client = TestClient(app)
            imported = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper.id),
                    "source": "chatgpt_web",
                    "raw_payload": {
                        "review_notes": [{"content": "Candidate note.", "field_name": "abstract"}],
                    },
                },
            )
            assert imported.status_code == 200
            payload = imported.json()
            assert payload["created_at"].endswith(("Z", "+00:00"))
            assert payload["candidates"][0]["created_at"].endswith(("Z", "+00:00"))
            assert datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00")).tzinfo == UTC

            run_id = payload["id"]
            deleted = client.post(f"/api/external-analysis/runs/{run_id}/delete")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True, "run_id": run_id}

            listed = client.get(f"/api/external-analysis/runs?paper_id={paper.id}")
            assert listed.status_code == 200
            assert listed.json() == []
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_internal_ai_parse_endpoint_auto_materializes(monkeypatch):
    with TemporaryDirectory():
        monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
        get_settings.cache_clear()
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                main_paper = Paper(
                    title="Internal Parse Paper",
                    doi="10.1000/internal",
                    pdf_path="internal.pdf",
                    abstract="A parsed paper ready for internal AI review.",
                    authors=["Reviewer A"],
                )
                support_paper = Paper(
                    title="Known Support Paper",
                    doi="10.1000/known-support",
                    pdf_path="support.pdf",
                    authors=["Reviewer B"],
                )
                session.add_all([main_paper, support_paper])
                session.commit()
                session.refresh(main_paper)
                session.refresh(support_paper)

            monkeypatch.setattr(
                "app.services.llm_service.LLMService.is_configured",
                lambda self: True,
            )
            monkeypatch.setattr(
                "app.services.llm_service.LLMService.structured_extract",
                lambda self, system_prompt, user_prompt, response_format: ExternalAnalysisNormalizedModel.model_validate(
                    {
                        "review_notes": [
                            {
                                "content": "Internal AI thinks the abstract should be softened.",
                                "field_name": "abstract",
                                "page": 1,
                                "quoted_text": "A parsed paper ready for internal AI review.",
                            }
                        ],
                        "correction_proposals": [
                            {
                                "field_name": "abstract",
                                "target_path": "abstract",
                                "operation": "replace",
                                "proposed_value": "A softened abstract generated by internal AI.",
                                "reason": "The abstract wording is too generic.",
                            }
                        ],
                        "supporting_papers": [
                            {
                                "relationship_type": "supports",
                                "target_doi": "10.1000/known-support",
                                "target_title": "Known Support Paper",
                                "note": "The known support paper complements this record.",
                            }
                        ],
                    }
                ),
            )

            client = TestClient(app)
            health = client.get("/api/health")
            assert health.status_code == 200
            health_payload = health.json()
            assert health_payload["db_kind"] == "postgresql"
            assert health_payload["db_url_masked"]
            assert "db_path" not in health_payload

            response = client.post(
                f"/api/external-analysis/papers/{main_paper.id}/internal-parse",
                json={"source_label": "内部AI解析", "auto_apply": True},
            )
            assert response.status_code == 410
            assert "prepare-ai-context" in response.json()["detail"]

            with Session(engine) as session:
                assert session.query(PaperNote).count() == 0
                assert session.query(PaperCorrection).count() == 0
                assert session.query(PaperRelationship).count() == 0
                stored_paper = session.get(Paper, main_paper.id)
                assert stored_paper.abstract == "A parsed paper ready for internal AI review."
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()


def test_internal_ai_parse_uses_persisted_writer_settings(monkeypatch):
    with TemporaryDirectory():
        monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
        monkeypatch.delenv("LITAI_WRITER_API_KEY", raising=False)
        monkeypatch.delenv("LITAI_WRITER_API_BASE", raising=False)
        monkeypatch.delenv("LITAI_WRITER_MODEL", raising=False)
        get_settings.cache_clear()

        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            connection.execute(
                text(
                    "CREATE TABLE app_settings ("
                    "  key   VARCHAR(255) PRIMARY KEY,"
                    "  value TEXT"
                    ")"
                )
            )
            connection.execute(
                text("INSERT INTO app_settings (key, value) VALUES (:key, :value)"),
                [
                    {"key": "writer_api_key", "value": "persisted-secret-key"},
                    {"key": "writer_api_base", "value": "https://writer.example.test"},
                    {"key": "writer_model", "value": "persisted-model"},
                ],
            )

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(
                    title="Persisted Settings Paper",
                    doi="10.1000/persisted",
                    pdf_path="persisted.pdf",
                    abstract="A parsed paper that relies on persisted writer settings.",
                    authors=["Reviewer A"],
                )
                session.add(paper)
                session.commit()
                session.refresh(paper)

            monkeypatch.setattr(
                "app.services.llm_service.LLMService.structured_extract",
                lambda self, system_prompt, user_prompt, response_format: ExternalAnalysisNormalizedModel.model_validate(
                    {
                        "review_notes": [{"content": "Persisted config note.", "field_name": "abstract"}],
                        "correction_proposals": [],
                        "supporting_papers": [],
                    }
                ),
            )

            client = TestClient(app)
            response = client.post(
                f"/api/external-analysis/papers/{paper.id}/internal-parse",
                json={"source_label": "持久化配置解析", "auto_apply": False},
            )

            assert response.status_code == 410
            assert "import_analysis" in response.json()["detail"]

        finally:
            app.dependency_overrides.clear()
            engine.dispose()
            get_settings.cache_clear()


def test_http_import_dft_new_candidate_auto_acquires_lock_without_token():
    """Regression: HTTP /import with auto_apply_review_rules=True and no write_lock_token
    must auto-acquire a dft_results lock, materialize the candidate, and release the lock.
    This was the original bug report's core failure (module_write_lock_required:dft_results).
    """
    with TemporaryDirectory():
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="HTTP DFT auto-lock paper", pdf_path="dft.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            response = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "codex_http_dft",
                    "auto_apply_review_rules": True,
                    "reviewer": "codex_http_dft",
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "target_type": "dft_results",
                                "target_id": "new",
                                "field_name": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material_identity": "Fe-N4",
                                    "property_type": "adsorption_energy",
                                    "value": -1.23,
                                    "unit": "eV",
                                    "adsorbate": "Li2S4",
                                    "reaction_step": "adsorption",
                                },
                                "evidence_location": {
                                    "page": 3,
                                    "quoted_text": "Fe-N4 -1.23 eV",
                                },
                                "confidence": 0.9,
                            }
                        ]
                    },
                },
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["candidates"][0]["status"] == "materialized"
            assert body["candidates"][0]["normalized_payload"]["target_type"] == "dft_results"

            with Session(engine) as session:
                dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
                assert len(dft_rows) == 1
                assert dft_rows[0].candidate_status == "new_candidate"
                assert dft_rows[0].value == -1.23
                from app.db.models import ModuleWriteLock
                active_locks = session.scalars(
                    select(ModuleWriteLock).where(
                        ModuleWriteLock.paper_id == paper_id,
                        ModuleWriteLock.module_name == "dft_results",
                        ModuleWriteLock.status == "active",
                    )
                ).all()
                assert active_locks == [], "Auto-acquired dft_results lock was not released"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_http_apply_review_rules_endpoint_materializes_deferred_dft_candidates():
    """Regression: a run imported with auto_apply_review_rules=False must be
    materializable later via POST /runs/{run_id}/apply-review-rules.
    """
    with TemporaryDirectory():
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Deferred DFT paper", pdf_path="deferred.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            import_response = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "deferred_dft",
                    "auto_apply_review_rules": False,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "target_type": "dft_results",
                                "target_id": "new",
                                "field_name": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material_identity": "Co-N3",
                                    "property_type": "adsorption_energy",
                                    "value": -0.95,
                                    "unit": "eV",
                                    "adsorbate": "H",
                                    "reaction_step": "adsorption",
                                },
                                "evidence_location": {
                                    "page": 5,
                                    "quoted_text": "Co-N3 H -0.95 eV",
                                },
                                "confidence": 0.88,
                            }
                        ]
                    },
                },
            )
            assert import_response.status_code == 200, import_response.text
            run_id = import_response.json()["id"]
            assert import_response.json()["candidates"][0]["status"] == "candidate"
            assert import_response.json()["candidates"][0]["action_mode"] == "apply_review_rules"
            assert import_response.json()["candidates"][0]["action_scope"] == "run"
            with Session(engine) as session:
                dft_row = session.scalar(
                    select(DFTResult).where(DFTResult.paper_id == paper_id)
                )
                assert dft_row is None

            apply_response = client.post(
                f"/api/external-analysis/runs/{run_id}/apply-review-rules",
                json={"reviewer": "deferred_dft"},
            )
            assert apply_response.status_code == 200, apply_response.text
            apply_body = apply_response.json()
            assert apply_body["candidate_count"] == 1
            assert apply_body["candidates"][0]["status"] == "materialized"
            assert apply_body["candidates"][0]["materialized_target_type"] == "dft_results"

            with Session(engine) as session:
                dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
                assert len(dft_rows) == 1
                assert dft_rows[0].candidate_status == "new_candidate"
                assert dft_rows[0].value == -0.95
                from app.db.models import ModuleWriteLock
                active_locks = session.scalars(
                    select(ModuleWriteLock).where(
                        ModuleWriteLock.paper_id == paper_id,
                        ModuleWriteLock.module_name == "dft_results",
                        ModuleWriteLock.status == "active",
                    )
                ).all()
                assert active_locks == [], "apply-review-rules leaked an active dft_results lock"
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_apply_review_rules_only_materializes_new_dft_candidates_from_requested_run():
    with TemporaryDirectory():
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Run-isolated DFT paper", pdf_path="run-isolated.pdf", authors=["A"])
                session.add(paper)
                session.flush()
                old_run = ExternalAnalysisRun(
                    paper_id=paper.id,
                    source="ide_ai",
                    source_label="old-run",
                    raw_payload={},
                    normalized_payload={},
                    mapping_status="mapped",
                )
                new_run = ExternalAnalysisRun(
                    paper_id=paper.id,
                    source="ide_ai",
                    source_label="new-run",
                    raw_payload={},
                    normalized_payload={},
                    mapping_status="mapped",
                )
                session.add_all([old_run, new_run])
                session.flush()

                def new_candidate(run: ExternalAnalysisRun, *, material: str, value: float) -> ExternalAnalysisCandidate:
                    return ExternalAnalysisCandidate(
                        run_id=run.id,
                        paper_id=paper.id,
                        candidate_type="object_review_audit",
                        status="candidate",
                        normalized_payload={
                            "target_type": "dft_results",
                            "target_id": "new",
                            "field_name": "dft_results",
                            "decision": "new_candidate",
                            "corrected_value": {
                                "material_identity": material,
                                "property_type": "adsorption_energy",
                                "value": value,
                                "unit": "eV",
                                "adsorbate": "H",
                                "reaction_step": "adsorption",
                            },
                            "evidence_location": {
                                "page": 3,
                                "quoted_text": f"{material} H {value} eV",
                            },
                            "confidence": 0.9,
                        },
                    )

                old_candidate = new_candidate(old_run, material="Fe-N4", value=-1.1)
                current_candidate = new_candidate(new_run, material="Co-N3", value=-0.9)
                session.add_all([old_candidate, current_candidate])
                session.commit()
                paper_id = paper.id
                old_run_id = old_run.id
                new_run_id = new_run.id
                old_candidate_id = old_candidate.id
                current_candidate_id = current_candidate.id

            client = TestClient(app)
            applied = client.post(
                f"/api/external-analysis/runs/{new_run_id}/apply-review-rules",
                json={"reviewer": "new-run"},
            )
            assert applied.status_code == 200, applied.text
            assert applied.json()["auto_apply_summary"]["new_dft_candidates"]["materialized_count"] == 1

            with Session(engine) as session:
                assert session.get(ExternalAnalysisCandidate, old_candidate_id).status == "candidate"
                assert session.get(ExternalAnalysisCandidate, current_candidate_id).status == "materialized"
                rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
                assert len(rows) == 1
                assert rows[0].value == -0.9
                issues = session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper_id)).all()
                assert len(issues) == 1
                assert str(current_candidate_id) in issues[0].source_candidate_ids

            applied_old = client.post(
                f"/api/external-analysis/runs/{old_run_id}/apply-review-rules",
                json={"reviewer": "old-run"},
            )
            assert applied_old.status_code == 200, applied_old.text
            with Session(engine) as session:
                assert session.get(ExternalAnalysisCandidate, old_candidate_id).status == "materialized"
                assert session.query(DFTResult).filter(DFTResult.paper_id == paper_id).count() == 2
                assert session.query(DFTAuditIssue).filter(DFTAuditIssue.paper_id == paper_id).count() == 2
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_materialize_endpoint_defers_object_review_audit_without_consuming_it():
    """POST /materialize must preserve active object-review candidates so the
    run-scoped apply-review-rules endpoint can process them later.
    """
    with TemporaryDirectory():
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        try:
            with Session(engine) as session:
                paper = Paper(title="Materialize skip DFT paper", pdf_path="skip.pdf", authors=["A"])
                session.add(paper)
                session.commit()
                session.refresh(paper)
                paper_id = paper.id

            client = TestClient(app)
            import_response = client.post(
                "/api/external-analysis/import",
                json={
                    "paper_id": str(paper_id),
                    "source": "ide_ai",
                    "source_label": "skip_dft",
                    "auto_apply_review_rules": False,
                    "raw_payload": {
                        "object_review_audits": [
                            {
                                "target_type": "dft_results",
                                "target_id": "new",
                                "field_name": "dft_results",
                                "decision": "new_candidate",
                                "corrected_value": {
                                    "material_identity": "Ni-S4",
                                    "property_type": "reaction_barrier",
                                    "value": 0.72,
                                    "unit": "eV",
                                    "adsorbate": "S",
                                    "reaction_step": "migration",
                                },
                                "evidence_location": {"page": 2, "quoted_text": "Ni-S4 0.72 eV"},
                                "confidence": 0.8,
                            }
                        ]
                    },
                },
            )
            assert import_response.status_code == 200
            run_id = import_response.json()["id"]
            candidate_id = import_response.json()["candidates"][0]["id"]
            assert import_response.json()["candidates"][0]["action_mode"] == "apply_review_rules"
            assert import_response.json()["candidates"][0]["action_scope"] == "run"

            # Exercise the legacy active status that previously fell through to
            # the generic unsupported-candidate branch and became "skipped".
            with Session(engine) as session:
                candidate = session.get(ExternalAnalysisCandidate, UUID(candidate_id))
                candidate.status = "pending"
                session.add(candidate)
                session.commit()

            materialize_response = client.post(
                f"/api/external-analysis/runs/{run_id}/materialize",
                json={"explicit_all": True, "created_by": "test"},
            )
            assert materialize_response.status_code == 200
            body = materialize_response.json()
            assert body["skipped_candidates"] >= 1
            assert body["deferred_review_candidates"] == 1
            assert body["next_action"] == "apply-review-rules"
            assert body["created_notes"] == 0
            assert body["created_corrections"] == 0

            with Session(engine) as session:
                dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
                assert dft_rows == []
                assert session.get(ExternalAnalysisCandidate, UUID(candidate_id)).status == "pending"

            apply_response = client.post(
                f"/api/external-analysis/runs/{run_id}/apply-review-rules",
                json={"reviewer": "test"},
            )
            assert apply_response.status_code == 200, apply_response.text
            with Session(engine) as session:
                assert session.get(ExternalAnalysisCandidate, UUID(candidate_id)).status == "materialized"
                assert session.query(DFTResult).filter(DFTResult.paper_id == paper_id).count() == 1
        finally:
            app.dependency_overrides.clear()
            engine.dispose()


def test_dft_results_create_correction_returns_clear_error_message():
    """Regression: PaperCorrection with operation=create and field_name=dft_results
    must return a clear error pointing to import_analysis, not the generic message.
    """
    from app.services.review_service import ReviewService

    with TemporaryDirectory():
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)
        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        try:
            with Session(engine) as session:
                paper = Paper(title="DFT create error paper", pdf_path="err.pdf", authors=["A"])
                session.add(paper)
                session.flush()
                correction = PaperCorrection(
                    paper_id=paper.id,
                    source="ide_ai",
                    field_name="dft_results",
                    target_path="dft_results:new:create",
                    operation="create",
                    proposed_value={"value": -1.0},
                    reason="attempting forbidden create path",
                    status="pending",
                )
                session.add(correction)
                session.flush()
                correction_id = correction.id
                session.commit()

            with Session(engine) as session:
                service = ReviewService(session)
                try:
                    service.approve_correction(correction_id, reviewer="ide_ai")
                    raise AssertionError("Expected ValueError for dft_results:create")
                except ValueError as exc:
                    assert "DFT results cannot be created via PaperCorrection" in str(exc)
                    assert "import_analysis" in str(exc)
                    assert "object_review_audit" in str(exc)
        finally:
            engine.dispose()


def test_apply_review_rules_logs_auto_lock_release_failure_when_apply_rolls_back(monkeypatch, caplog):
    """Release failures must remain observable even when the outer apply later rolls back."""

    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        try:
            with Session(engine) as session:
                paper = Paper(title="DFT release log paper", pdf_path="release-log.pdf", authors=["A"])
                session.add(paper)
                session.flush()
                run = ExternalAnalysisRun(
                    paper_id=paper.id,
                    source="ide_ai",
                    source_label="release_log",
                    raw_payload={},
                    normalized_payload={},
                    mapping_status="mapped",
                )
                session.add(run)
                session.flush()
                session.add(
                    ExternalAnalysisCandidate(
                        run_id=run.id,
                        paper_id=paper.id,
                        candidate_type="object_review_audit",
                        status="candidate",
                        normalized_payload={
                            "target_type": "dft_results",
                            "target_id": "new",
                            "field_name": "dft_results",
                            "decision": "new_candidate",
                            "corrected_value": {
                                "material_identity": "Fe-N4",
                                "property_type": "adsorption_energy",
                                "value": -1.23,
                                "unit": "eV",
                                "adsorbate": "Li2S4",
                                "reaction_step": "adsorption",
                            },
                            "evidence_location": {
                                "page": 3,
                                "quoted_text": "Fe-N4 -1.23 eV",
                            },
                            "confidence": 0.9,
                        },
                    )
                )
                session.commit()
                run_id = run.id

            from app.services.module_write_lock_service import ModuleWriteLockService

            def fail_apply_import_rules(self, **kwargs):
                raise RuntimeError("apply boom")

            def fail_release(self, **kwargs):
                raise RuntimeError("release boom")

            monkeypatch.setattr(
                VerificationSessionService,
                "apply_import_rules_for_paper",
                fail_apply_import_rules,
            )
            monkeypatch.setattr(ModuleWriteLockService, "release", fail_release)
            caplog.set_level(logging.ERROR, logger="app.services.external_analysis_service")

            with Session(engine) as session:
                service = ExternalAnalysisService(session, Settings(storage_root=Path(tmpdir)))
                try:
                    service.apply_review_rules_for_run(run_id, reviewer="ide_ai")
                    raise AssertionError("Expected apply_review_rules_for_run to raise")
                except RuntimeError as exc:
                    assert str(exc) == "apply boom"
                session.rollback()

            assert any(
                record.name == "app.services.external_analysis_service"
                and "Failed to release auto-acquired DFT module write lock" in record.getMessage()
                and record.exc_info
                and str(record.exc_info[1]) == "release boom"
                for record in caplog.records
            )
        finally:
            engine.dispose()
