from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    AuditLog,
    Base,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperSection,
    WorkflowJob,
    WritingCard,
)
from app.db.session import get_db_session
from app.main import app
from app.services.gemini_audit_service import GeminiAuditService
from app.services.paper_query import PaperQueryService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.workbench_status import workflow_needs_human_confirmation


@pytest.fixture
def workbench_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        db_path = root / "workbench.db"
        storage_root = root / "storage"
        monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        monkeypatch.setenv("LITAI_DOCLING_DO_OCR", "false")
        get_settings.cache_clear()

        engine = create_engine(f"sqlite:///{db_path}", future=True)
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


def test_pdf_quality_missing_file_is_blocked(workbench_env):
    _, storage_root, _ = workbench_env
    report = PaperWorkbenchService.assess_pdf_path(storage_root / "missing.pdf", get_settings())
    assert report["quality_status"] == "Broken"
    assert report["parse_allowed"] is False
    assert report["needs_human_confirmation"] is True
    assert report["markdown_trust"] == "unavailable"


def test_pdf_quality_text_pdf_is_parseable(workbench_env):
    fitz = pytest.importorskip("fitz")
    _, storage_root, _ = workbench_env
    pdf_path = storage_root / "pdf" / "text.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for index in range(3):
        page = doc.new_page()
        text = "\n".join(
            f"Readable DFT text page {index}, line {line}: adsorption energy and band gap evidence."
            for line in range(55)
        )
        page.insert_textbox(fitz.Rect(72, 72, 520, 760), text, fontsize=8)
    doc.save(pdf_path)
    doc.close()

    report = PaperWorkbenchService.assess_pdf_path(pdf_path, get_settings())

    assert report["quality_status"] in {"A_text_readable", "B_text_partial"}
    assert report["parse_allowed"] is True
    assert report["needs_human_confirmation"] is False
    assert report["metrics"]["page_count"] == 3


def test_prepare_workspace_writes_standard_materials(workbench_env):
    _, storage_root, Session = workbench_env
    pdf_path = storage_root / "pdf" / "paper.pdf"
    md_path = storage_root / "markdown" / "paper.md"
    figure_path = storage_root / "figures" / "paper_fig_1.png"
    for path, body in [
        (pdf_path, b"%PDF-1.4\n% test fixture"),
        (md_path, b"# Paper\n\nDFT evidence"),
        (figure_path, b"PNG fixture"),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    with Session() as session:
        paper = Paper(title="Graphdiyne evidence paper", year=2024, journal="Test Journal", pdf_path="paper.pdf", markdown_path="paper.md")
        session.add(paper)
        session.flush()
        dft = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            evidence_text="The adsorption energy is -1.23 eV.",
            source_section="Results",
            source_figure="Figure 2",
        )
        session.add_all(
            [
                PaperSection(paper_id=paper.id, section_title="Results", text="The adsorption energy is -1.23 eV.", page_start=2, page_end=2),
                PaperFigure(paper_id=paper.id, caption="Figure 1. Band structure.", image_path="paper_fig_1.png", page=3),
                dft,
            ]
        )
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="text",
                page=2,
                target_type="dft_results",
                target_id=str(dft.id),
                field_name="value",
                evidence_text="The adsorption energy is -1.23 eV.",
                locator_status="candidate",
                locator_confidence=0.77,
                parser_source="test",
            )
        )
        session.commit()
        paper_id = paper.id

        summary = PaperWorkbenchService(session, get_settings()).prepare_paper_workspace(paper_id)

        assert summary["workflow_status"] == "Needs_Human_Confirmation"
        assert summary["workspace_path"].endswith(f"by_id/{paper_id}")
        workspace_root = storage_root / "by_id" / str(paper_id)
        assert (workspace_root / "metadata.json").exists()
        assert (workspace_root / "quality_report.json").exists()
        assert (workspace_root / "markdown" / "source.md").exists()
        assert (workspace_root / "evidence" / "locators.json").exists()
        assert (workspace_root / "extraction" / "dft_candidates.json").exists()
        with (workspace_root / "quality_report.json").open("r", encoding="utf-8") as handle:
            quality = json.load(handle)
        assert quality["quality_status"] == "Broken"


def test_paper_detail_exposes_figure_object_review_summary_read_only(workbench_env):
    _, storage_root, Session = workbench_env
    Image = pytest.importorskip("PIL.Image")
    figure_asset = storage_root / "figures" / "figure-2.png"
    figure_asset.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (420, 260), color=(32, 64, 96)).save(figure_asset)
    with Session() as session:
        paper = Paper(title="Figure audit paper", pdf_path="paper.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 2. Reaction pathway image.",
            image_path="figures/figure-2.png",
            page=4,
            crop_status="candidate_crop",
            crop_confidence=0.82,
            prov=[{
                "bbox": {"l": 10, "t": 20, "r": 300, "b": 200},
                "full_page_image_path": "page_004.png",
                "pixel_size": {"width": 420, "height": 260},
            }],
        )
        session.add(figure)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="figure_object_review",
            source_label="Figure object review",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        for source, decision, corrected_value in [
            ("glm_figure_audit", "PASS", "usable_crop"),
            ("codex_figure_audit", "REVISE", "needs_manual_crop_check"),
        ]:
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    status="candidate",
                    confidence=0.74,
                    mapping_reason="figure object review",
                    normalized_payload={
                        "target_type": "figure",
                        "target_id": str(figure.id),
                        "field_name": "crop_status",
                        "decision": decision,
                        "corrected_value": corrected_value,
                        "confidence": 0.74,
                        "source": source,
                        "source_label": source,
                        "agent_role": "figure_reviewer",
                        "verification_status": "unverified",
                        "evidence_checked": True,
                        "evidence_location": {"page": 4},
                        "reason": "Figure crop reviewed as a candidate only.",
                    },
                )
            )
        session.commit()
        paper_id = paper.id
        figure_id = figure.id

        detail = PaperQueryService(session).get_paper_detail(paper_id)
        conflict_payload = ReviewConflictAggregationService(session).list_conflicts(
            paper_id=paper_id,
            target_type="figure",
            target_id=str(figure_id),
            include_non_conflicts=True,
        )
        stored_figure = session.get(PaperFigure, figure_id)

        assert detail is not None
        figure_payload = detail.figures[0]
        assert figure_payload.page == 4
        assert figure_payload.asset_url == "/api/papers/assets/figures/figure-2.png"
        assert figure_payload.image_review["crop_status"] == "candidate_crop"
        assert figure_payload.review_required is False
        assert figure_payload.figure_reliability_status == "reliable"
        assert figure_payload.figure_reliability_warnings == []
        assert figure_payload.object_review_audit_count == 2
        assert figure_payload.latest_object_review_audit["source"] == "codex_figure_audit"
        assert figure_payload.latest_object_review_audit["decision"] == "REVISE"
        assert figure_payload.latest_object_review_audit["verification_status"] == "unverified"
        assert figure_payload.conflict_count == 1
        assert figure_payload.field_conflicts[0]["field_name"] == "crop_status"
        assert conflict_payload["conflict_count"] == 1
        assert stored_figure.crop_status == "candidate_crop"
        assert session.get(Paper, paper_id).workflow_status == "Parsed_Material_Ready"


def test_paper_detail_exposes_writing_card_object_review_summary_read_only(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Writing audit paper", pdf_path="paper.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        card = WritingCard(
            paper_id=paper.id,
            paper_type="research",
            research_gap="Catalyst writing gap.",
            proposed_solution="Use a stronger evidence chain.",
            core_hypothesis="Polar sites anchor polysulfides.",
            evidence_chain=[
                {
                    "reviewer_status": "pending",
                    "locator_status": "exact_page",
                    "page": 3,
                    "evidence_text": "Polar sites anchor polysulfides.",
                }
            ],
        )
        session.add(card)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="writing_card_object_review",
            source_label="Writing-card object review",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        for source, decision, corrected_value in [
            ("codex_writing_audit", "PASS", "supported_with_qualifier"),
            ("glm_writing_audit", "FLAG", "unsupported_causality"),
        ]:
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    status="candidate",
                    confidence=0.68,
                    mapping_reason="writing card object review",
                    normalized_payload={
                        "target_type": "writing_cards",
                        "target_id": str(card.id),
                        "field_name": "core_hypothesis",
                        "decision": decision,
                        "corrected_value": corrected_value,
                        "confidence": 0.68,
                        "source": source,
                        "source_label": source,
                        "agent_role": "writing_card_auditor",
                        "verification_status": "unverified",
                        "evidence_checked": True,
                        "evidence_location": {"page": 3, "section": "Discussion"},
                        "reason": "Writing card claim reviewed as a candidate only.",
                    },
                )
            )
        session.commit()
        paper_id = paper.id
        card_id = card.id

        detail = PaperQueryService(session).get_paper_detail(paper_id)
        conflict_payload = ReviewConflictAggregationService(session).list_conflicts(
            paper_id=paper_id,
            target_type="writing_cards",
            target_id=str(card_id),
            include_non_conflicts=True,
        )
        stored_card = session.get(WritingCard, card_id)

        assert detail is not None
        card_payload = detail.writing_cards_items[0]
        assert card_payload.evidence_status == "present"
        assert card_payload.safety_status == "blocked"
        assert card_payload.safe_verified is False
        assert card_payload.object_review_audit_count == 2
        assert card_payload.latest_object_review_audit["source"] == "glm_writing_audit"
        assert card_payload.latest_object_review_audit["decision"] == "FLAG"
        assert card_payload.latest_object_review_audit["verification_status"] == "unverified"
        assert card_payload.conflict_count == 1
        assert card_payload.field_conflicts[0]["field_name"] == "core_hypothesis"
        assert conflict_payload["conflict_count"] == 1
        assert stored_card.core_hypothesis == "Polar sites anchor polysulfides."
        assert session.get(Paper, paper_id).workflow_status == "Parsed_Material_Ready"
        assert session.query(PaperCorrection).count() == 0


def test_paper_detail_exposes_mechanism_claim_object_review_summary_read_only(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Mechanism audit paper", pdf_path="paper.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="adsorption_mechanism",
            claim_text="Defect sites strengthen polysulfide adsorption through charge redistribution.",
            evidence_types=["section_text"],
            confidence=0.71,
            evidence_text="Charge redistribution around defect sites strengthens polysulfide adsorption.",
        )
        session.add(claim)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="mechanism_object_review",
            source_label="Mechanism object review",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        for source, decision, corrected_value in [
            ("codex_mechanism_audit", "PASS", "supported_charge_redistribution"),
            ("glm_mechanism_audit", "FLAG", "overstated_causality"),
        ]:
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    status="candidate",
                    confidence=0.7,
                    mapping_reason="mechanism claim object review",
                    normalized_payload={
                        "target_type": "mechanism_claims",
                        "mechanism_claim_id": str(claim.id),
                        "field_name": "claim_text",
                        "decision": decision,
                        "corrected_value": corrected_value,
                        "confidence": 0.7,
                        "source": source,
                        "source_label": source,
                        "agent_role": "mechanism_claim_auditor",
                        "verification_status": "unverified",
                        "evidence_checked": True,
                        "evidence_location": {"page": 6, "section": "Discussion"},
                        "reason": "Mechanism claim reviewed as a candidate only.",
                    },
                )
            )
        session.commit()
        paper_id = paper.id
        claim_id = claim.id

        detail = PaperQueryService(session).get_paper_detail(paper_id)
        conflict_payload = ReviewConflictAggregationService(session).list_conflicts(
            paper_id=paper_id,
            target_type="mechanism_claims",
            target_id=str(claim_id),
            include_non_conflicts=True,
        )
        stored_claim = session.get(MechanismClaim, claim_id)

        assert detail is not None
        claim_payload = detail.mechanism_claims_items[0]
        assert claim_payload.evidence_status == "present"
        assert claim_payload.locator_status == "text_only"
        assert claim_payload.confidence_status == "medium"
        assert claim_payload.object_review_audit_count == 2
        assert claim_payload.latest_object_review_audit["source"] == "glm_mechanism_audit"
        assert claim_payload.latest_object_review_audit["decision"] == "FLAG"
        assert claim_payload.latest_object_review_audit["verification_status"] == "unverified"
        assert claim_payload.conflict_count == 1
        assert claim_payload.field_conflicts[0]["field_name"] == "claim_text"
        assert conflict_payload["conflict_count"] == 1
        assert stored_claim.claim_text == "Defect sites strengthen polysulfide adsorption through charge redistribution."
        assert session.get(Paper, paper_id).workflow_status == "Parsed_Material_Ready"
        assert session.query(PaperCorrection).count() == 0


def test_gemini_audit_flags_candidate_without_human_confirmation(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Gemini audit paper", pdf_path="paper.pdf", workflow_status="Codex_Candidate")
        session.add(paper)
        session.flush()
        dft = DFTResult(
            paper_id=paper.id,
            adsorbate="Li",
            property_type="band_gap",
            value=1.2,
            unit="eV",
            evidence_text="The band gap is 1.2 eV.",
        )
        session.add(dft)
        session.commit()
        paper_id = paper.id
        result_id = dft.id

        response = GeminiAuditService(session).submit(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=str(result_id),
            decision="PASS",
            reviewer="gemini",
            reviewer_note="Evidence matches the candidate.",
            confidence=0.91,
            field_names=["value"],
        )

        assert response["decision"] == "PASS"
        assert response["safety"]["requires_human_confirmation_for_final_library"] is True
        refreshed_paper = session.get(Paper, paper_id)
        refreshed_dft = session.get(DFTResult, result_id)
        assert refreshed_paper.workflow_status == "Gemini_Verified"
        assert refreshed_dft.candidate_status == "Gemini_Verified"
        review = session.scalar(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id))
        assert review.reviewer_status == "gemini_pass"
        assert review.reviewer != "human"
        assert session.scalar(select(AuditLog).where(AuditLog.action == "gemini_audit")) is not None


def test_human_confirm_requires_explicit_acknowledgement(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Human gate paper", pdf_path="paper.pdf", workflow_status="Gemini_Verified")
        session.add(paper)
        session.commit()
        paper_id = paper.id

        with pytest.raises(ValueError):
            GeminiAuditService(session).human_confirm(
                paper_id=paper_id,
                target_status="Human_Confirmed",
                reviewer="human",
                note=None,
                confirm_human_review=False,
            )

        payload = GeminiAuditService(session).human_confirm(
            paper_id=paper_id,
            target_status="Human_Confirmed",
            reviewer="human",
            note="Checked against PDF.",
            confirm_human_review=True,
        )

        assert payload["workflow_status"] == "Human_Confirmed"
        assert session.get(Paper, paper_id).workflow_status == "Human_Confirmed"
        assert session.scalar(select(AuditLog).where(AuditLog.action == "human_confirm_workbench_status")) is not None


def test_workflow_human_confirmation_gate_covers_candidate_statuses():
    assert workflow_needs_human_confirmation("Codex_Candidate") is True
    assert workflow_needs_human_confirmation("Gemini_Verified") is True
    assert workflow_needs_human_confirmation("Gemini_Revised") is True
    assert workflow_needs_human_confirmation("Gemini_Flagged") is True
    assert workflow_needs_human_confirmation("Evidence_Insufficient") is True
    assert workflow_needs_human_confirmation("Needs_Human_Confirmation") is True

    assert workflow_needs_human_confirmation("Human_Confirmed") is False
    assert workflow_needs_human_confirmation("ML_Ready") is False
    assert workflow_needs_human_confirmation("Citation_Ready") is False

    assert workflow_needs_human_confirmation("Quality_Checked", {"needs_human_confirmation": True}) is True
    assert workflow_needs_human_confirmation("Quality_Checked", {"needs_human_confirmation": False}) is False


def test_review_center_api_exposes_quality_and_candidate_counts(workbench_env):
    _, storage_root, Session = workbench_env
    pdf_path = storage_root / "pdf" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4\n% review center fixture\n")
    with Session() as session:
        paper = Paper(
            title="Review center paper",
            pdf_path="paper.pdf",
            workflow_status="Needs_Human_Confirmation",
            pdf_quality_status="D_scan_unclear",
            pdf_quality_score=0.1,
            pdf_quality_report={"reason": "too_little_text_or_image_signal", "needs_human_confirmation": True},
        )
        codex_candidate = Paper(
            title="Codex candidate still needs human review",
            pdf_path="candidate.pdf",
            workflow_status="Codex_Candidate",
            pdf_quality_status="A_text_readable",
            pdf_quality_report={"reason": "native_text_is_readable", "needs_human_confirmation": False},
        )
        human_confirmed = Paper(
            title="Human confirmed paper",
            pdf_path="confirmed.pdf",
            workflow_status="Human_Confirmed",
            pdf_quality_status="A_text_readable",
            pdf_quality_report={"reason": "native_text_is_readable", "needs_human_confirmation": False},
        )
        session.add(paper)
        session.add(codex_candidate)
        session.add(human_confirmed)
        session.flush()
        session.add(DFTResult(paper_id=paper.id, property_type="band_gap", value=0.5, unit="eV"))
        session.add(
            PaperFigure(
                paper_id=paper.id,
                caption="Figure with a small crop and missing full-page snapshot.",
                page=3,
                image_path=None,
                crop_status="candidate_crop",
                prov=[
                    {
                        "bbox": {"l": 1, "t": 1, "r": 40, "b": 30},
                        "pixel_size": {"width": 120, "height": 80},
                    }
                ],
            )
        )
        session.add(EvidenceLocator(paper_id=paper.id, source_type="text", evidence_text="candidate", locator_status="candidate"))
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "paper_id": str(paper.id),
                    "target_type": "dft_results",
                    "target_id": "row-1",
                    "field_name": "value",
                    "source": "assigned_dft_audit",
                    "source_label": "Assigned AI DFT audit",
                    "agent_role": "dft_auditor",
                    "model_name": "glm-test",
                    "decision": "FLAG",
                    "recommended_action": "needs_human_review",
                    "verification_status": "unverified",
                    "confidence": 0.62,
                    "reason": "Check this value against the source PDF.",
                    "evidence_location": {"page": 4},
                    "writes_final_truth": False,
                    "human_confirmation_required": True,
                },
                status="candidate",
                confidence=0.62,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/workbench/review-center?limit=50")

    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "codex_workbench_v1"
    assert data["metadata"]["status_counts"]["Needs_Human_Confirmation"] == 1
    by_title = {row["title"]: row for row in data["rows"]}
    assert by_title["Review center paper"]["needs_human_confirmation"] is True
    assert by_title["Review center paper"]["pdf_exists"] is True
    assert by_title["Review center paper"]["pdf_url"].endswith(f"/api/papers/{by_title['Review center paper']['paper_id']}/pdf")
    assert by_title["Review center paper"]["pdf_artifact_status"]["pdf_exists"] is True
    assert by_title["Review center paper"]["pdf_artifact_status"]["pdf_path_kind"] == "storage_relative"
    assert by_title["Review center paper"]["has_dft_candidates"] is True
    assert by_title["Review center paper"]["evidence_count"] == 1
    assert by_title["Review center paper"]["locator_issue_count"] == 1
    assert by_title["Review center paper"]["locator_issue_counts"]["missing_page"] == 1
    assert by_title["Review center paper"]["top_locator_issues"][0]["code"] == "missing_page"
    assert by_title["Review center paper"]["figure_issue_count"] >= 2
    assert by_title["Review center paper"]["figure_issue_counts"]["missing_full_page_snapshot"] == 1
    assert by_title["Review center paper"]["figure_issue_counts"]["small_crop"] == 1
    assert {item["code"] for item in by_title["Review center paper"]["top_figure_issues"]} >= {
        "missing_full_page_snapshot",
        "small_crop",
    }
    assert by_title["Review center paper"]["object_review_audit_count"] == 1
    assert by_title["Review center paper"]["object_review_audits"][0]["candidate_type"] == "object_review_audit"
    assert by_title["Review center paper"]["object_review_audits"][0]["decision"] == "FLAG"
    assert by_title["Review center paper"]["object_review_audits"][0]["verification_status"] == "unverified"
    assert by_title["Codex candidate still needs human review"]["needs_human_confirmation"] is True
    assert by_title["Human confirmed paper"]["needs_human_confirmation"] is False


def test_dft_review_queue_api_exposes_object_review_audit_summary(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT queue object audit paper", pdf_path="queue-object.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Table 1 reports the adsorption energy.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=5,
                bbox={"l": 10, "t": 20, "r": 120, "b": 80},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.9,
            )
        )
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "paper_id": str(paper.id),
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "value",
                    "source": "assigned_dft_audit",
                    "source_label": "Assigned AI DFT audit",
                    "agent_role": "dft_auditor",
                    "model_name": "glm-test",
                    "decision": "REVISE",
                    "recommended_action": "propose_correction",
                    "verification_status": "unverified",
                    "confidence": 0.74,
                    "reason": "The imported object-level audit remains a candidate.",
                    "evidence_location": {"page": 5, "table": "Table 1"},
                    "writes_final_truth": False,
                    "human_confirmation_required": True,
                },
                status="candidate",
                confidence=0.74,
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = row.id

    client = TestClient(app)
    response = client.get(f"/api/papers/export/dft-review-queue?paper_id={paper_id}&limit=10&status=needs_review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["schema_version"] == "dft_review_queue_v1"
    assert len(payload["rows"]) == 1
    row_payload = payload["rows"][0]
    assert row_payload["object_review_audits_count"] == 1
    assert row_payload["object_review_audits"][0]["candidate_type"] == "object_review_audit"
    assert row_payload["object_review_audits"][0]["decision"] == "REVISE"
    assert row_payload["object_review_audits"][0]["verification_status"] == "unverified"
    assert row_payload["object_review_audits"][0]["evidence_location"]["page"] == 5
    assert row_payload["locator_reliability_status"] == "reliable"
    assert row_payload["locator_reliability_warnings"] == []
    assert row_payload["primary_locator_reliability"]["page"] == 5
    assert row_payload["primary_locator_reliability"]["bbox"] == {"l": 10, "t": 20, "r": 120, "b": 80}
    assert row_payload["primary_locator_reliability"]["status"] == "exact_page"

    with Session() as session:
        stored_row = session.get(DFTResult, row_id)
        assert stored_row.candidate_status == "system_candidate"
        assert stored_row.value == -1.20


def test_review_conflict_aggregation_is_read_only_for_dft_fields(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Conflict Aggregation Paper", pdf_path="conflict.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="The adsorption energy of Li2S4 is -1.20 eV.",
            confidence=0.7,
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = paper.id
        row_id = row.id

    with Session() as session:
        GeminiAuditService(session).submit(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=row_id,
            decision="PASS",
            reviewer="gemini_test",
            agent_role="dft_auditor",
            model_name="gemini-test",
            field_names=["value"],
            reviewer_note="Value matches the evidence sentence.",
            confidence=0.8,
        )

    with Session() as session:
        GeminiAuditService(session).submit(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=row_id,
            decision="FLAG",
            reviewer="glm_test",
            agent_role="dft_auditor",
            model_name="glm-test",
            field_names=["value"],
            reviewer_note="The cited evidence appears to support a different number.",
            confidence=0.75,
        )

    with Session() as session:
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="claude_dft_audit",
            source_label="Claude DFT audit",
            normalized_payload={"verdict": "REVISE"},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper_id,
                candidate_type="external_audit_opinion",
                normalized_payload={
                    "source": "claude_dft_audit",
                    "source_label": "Claude DFT audit",
                    "verdict": "REVISE",
                    "raw_payload": {
                        "reviews": [
                            {
                                "target_type": "dft_results",
                                "target_id": str(row_id),
                                "field_name": "value",
                                "decision": "REVISE",
                                "corrected_value": -1.35,
                                "unit": "eV",
                                "confidence": 0.66,
                                "evidence_location": {"page": 4, "section": "Results"},
                                "reason": "Table value differs from extracted value.",
                            }
                        ]
                    },
                },
                confidence=0.66,
                status="candidate",
            )
        )
        session.add_all(
            [
                PaperCorrection(
                    paper_id=paper_id,
                    source="glm_test",
                    field_name="dft_results",
                    target_path=f"dft_results:{row_id}:value",
                    operation="replace",
                    proposed_value=-1.35,
                    reason="GLM proposed corrected value.",
                    evidence_payload={"source_label": "GLM DFT audit", "confidence": 0.75},
                    status="pending",
                ),
                PaperCorrection(
                    paper_id=paper_id,
                    source="gemini_test",
                    field_name="dft_results",
                    target_path=f"dft_results:{row_id}:value",
                    operation="replace",
                    proposed_value=-1.20,
                    reason="Gemini kept extracted value.",
                    evidence_payload={"source_label": "Gemini DFT audit", "confidence": 0.8},
                    status="rejected",
                ),
            ]
        )
        before = session.get(DFTResult, row_id)
        before_status = before.candidate_status
        before_value = before.value

        session.flush()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper_id)

        after = session.get(DFTResult, row_id)
        review = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_id == str(row_id),
                ExtractionFieldReview.field_name == "value",
            )
        )

    assert payload["conflict_count"] == 1
    conflict = payload["rows"][0]
    assert conflict["target_type"] == "dft_results"
    assert conflict["target_id"] == str(row_id)
    assert conflict["field_name"] == "value"
    assert "value_conflict" in conflict["conflict_types"]
    assert "decision_conflict" in conflict["conflict_types"]
    assert {item["source_type"] for item in conflict["opinions"]} >= {
        "extraction_field_review",
        "external_audit_opinion",
        "paper_correction",
    }
    assert after.candidate_status == before_status
    assert after.value == before_value
    assert review.reviewer_status == "review_conflict"


def test_review_conflicts_api_and_review_center_counts(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Conflict API Paper", pdf_path="conflict-api.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S8",
            value=-0.5,
            unit="eV",
            evidence_text="Evidence text",
        )
        session.add(row)
        session.flush()
        session.add_all(
            [
                PaperCorrection(
                    paper_id=paper.id,
                    source="ai_a",
                    field_name="dft_results",
                    target_path=f"dft_results:{row.id}:value",
                    operation="replace",
                    proposed_value=-0.5,
                    reason="Keep value.",
                    status="pending",
                ),
                PaperCorrection(
                    paper_id=paper.id,
                    source="ai_b",
                    field_name="dft_results",
                    target_path=f"dft_results:{row.id}:value",
                    operation="replace",
                    proposed_value=-0.7,
                    reason="Use table value.",
                    status="pending",
                ),
            ]
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    response = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}")
    assert response.status_code == 200
    assert response.json()["conflict_count"] == 1

    center = client.get("/api/workbench/review-center?limit=50")
    assert center.status_code == 200
    row_payload = next(item for item in center.json()["rows"] if item["paper_id"] == paper_id)
    assert row_payload["review_conflict_count"] == 1


def _seed_object_review_audit(
    session,
    *,
    paper_id,
    target_type: str,
    target_id,
    field_name: str,
    decision: str,
    corrected_value,
    confidence: float,
    locator_status: str,
    evidence_text: str,
    source: str,
):
    run = session.query(ExternalAnalysisRun).filter(ExternalAnalysisRun.paper_id == paper_id).first()
    if run is None:
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
    session.add(
        ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper_id,
            candidate_type="object_review_audit",
            status="candidate",
            confidence=confidence,
            normalized_payload={
                "paper_id": str(paper_id),
                "target_type": target_type,
                "target_id": str(target_id),
                "field_name": field_name,
                "source": source,
                "source_label": source,
                "agent_role": "dft_auditor",
                "model_name": source + "-model",
                "decision": decision,
                "corrected_value": corrected_value,
                "confidence": confidence,
                "reason": f"{source} review",
                "evidence_payload": {
                    "evidence_text": evidence_text,
                    "locator": {"page": 5, "locator_status": locator_status},
                },
                "evidence_location": {"page": 5, "locator_status": locator_status},
                "verification_status": "unverified",
            },
        )
    )


def test_review_adjudication_supports_auto_suggest_and_manual_modes(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Adjudication modes", pdf_path="adjudication.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        auto_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Exact table evidence supports -1.20 eV.",
            candidate_status="system_candidate",
        )
        suggest_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            value=-1.10,
            unit="eV",
            evidence_text="Competing evidence must be reconciled.",
            candidate_status="system_candidate",
        )
        manual_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S8",
            value=-0.80,
            unit="eV",
            evidence_text="Weak locator evidence only.",
            candidate_status="system_candidate",
        )
        session.add_all([auto_row, suggest_row, manual_row])
        session.flush()
        session.add_all(
            [
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="table",
                    target_type="dft_results",
                    target_id=str(auto_row.id),
                    field_name="value",
                    page=5,
                    bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                    evidence_text=auto_row.evidence_text,
                    locator_status="exact_page",
                    locator_confidence=0.95,
                ),
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="table",
                    target_type="dft_results",
                    target_id=str(suggest_row.id),
                    field_name="value",
                    page=5,
                    bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                    evidence_text=suggest_row.evidence_text,
                    locator_status="exact_page",
                    locator_confidence=0.9,
                ),
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="text",
                    target_type="dft_results",
                    target_id=str(manual_row.id),
                    field_name="value",
                    page=5,
                    evidence_text=manual_row.evidence_text,
                    locator_status="text_only",
                    locator_confidence=0.5,
                ),
            ]
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=auto_row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.20,
            confidence=0.91,
            locator_status="exact_page",
            evidence_text=auto_row.evidence_text,
            source="gemini-auto",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=suggest_row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.35,
            confidence=0.78,
            locator_status="exact_page",
            evidence_text="Table 3 supports -1.35 eV.",
            source="gemini-suggest",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=suggest_row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.35,
            confidence=0.82,
            locator_status="exact_page",
            evidence_text="Cross-check also supports -1.35 eV.",
            source="claude-suggest",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=suggest_row.id,
            field_name="value",
            decision="REVISE",
            corrected_value=-1.10,
            confidence=0.41,
            locator_status="text_only",
            evidence_text="Narrative text is weaker.",
            source="glm-suggest",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=manual_row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-0.82,
            confidence=0.59,
            locator_status="text_only",
            evidence_text="Weak discussion evidence.",
            source="gemini-manual",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=manual_row.id,
            field_name="value",
            decision="REVISE",
            corrected_value=-0.77,
            confidence=0.58,
            locator_status="missing_locator",
            evidence_text="No exact locator.",
            source="glm-manual",
        )
        session.commit()

        auto_payload = ReviewAdjudicationService(session).list_with_adjudication(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(auto_row.id),
            include_non_conflicts=True,
        )
        suggest_payload = ReviewAdjudicationService(session).list_with_adjudication(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(suggest_row.id),
            include_non_conflicts=True,
        )
        manual_payload = ReviewAdjudicationService(session).list_with_adjudication(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(manual_row.id),
            include_non_conflicts=True,
        )

    auto_adjudication = auto_payload["rows"][0]["adjudication"]
    assert auto_adjudication["adjudication_mode"] == "auto"
    assert auto_adjudication["recommended_action"] == "verify"
    assert auto_adjudication["eligible_for_auto_apply"] is True

    suggest_adjudication = suggest_payload["rows"][0]["adjudication"]
    assert suggest_adjudication["adjudication_mode"] == "suggest"
    assert suggest_adjudication["recommended_action"] == "propose_correction"
    assert suggest_adjudication["eligible_for_auto_apply"] is False

    manual_adjudication = manual_payload["rows"][0]["adjudication"]
    assert manual_adjudication["adjudication_mode"] == "manual"
    assert manual_adjudication["eligible_for_auto_apply"] is False
    assert "no_exact_locator" in manual_adjudication["blocked_reasons"]


def test_review_conflicts_api_accepts_ai_adjudication_without_bypassing_audit(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Accept AI adjudication", pdf_path="accept-ai.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.10,
            unit="eV",
            evidence_text="Stored value is weaker than the table-backed consensus.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=5,
                bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.93,
            )
        )
        for source, decision, corrected_value, confidence, locator_status in [
            ("gemini-accept", "PASS", -1.35, 0.84, "exact_page"),
            ("claude-accept", "PASS", -1.35, 0.81, "exact_page"),
            ("glm-accept", "REVISE", -1.10, 0.42, "text_only"),
        ]:
            _seed_object_review_audit(
                session,
                paper_id=paper.id,
                target_type="dft_results",
                target_id=row.id,
                field_name="value",
                decision=decision,
                corrected_value=corrected_value,
                confidence=confidence,
                locator_status=locator_status,
                evidence_text="Consensus evidence." if locator_status == "exact_page" else "Weak narrative evidence.",
                source=source,
            )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    conflicts = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}&include_non_conflicts=true")
    assert conflicts.status_code == 200
    adjudication = conflicts.json()["rows"][0]["adjudication"]
    assert adjudication["adjudication_mode"] == "suggest"
    assert adjudication["recommended_action"] == "propose_correction"

    accept = client.post(
        "/api/workbench/review-conflicts/accept-ai",
        json={
            "paper_id": paper_id,
            "target_type": "dft_results",
            "target_id": row_id,
            "field_name": "value",
            "reviewer": "review_center_test",
        },
    )
    assert accept.status_code == 200
    result = accept.json()
    assert result["action"] == "propose_correction"
    assert result["result"]["status"] == "pending"

    with Session() as session:
        correction = session.scalar(select(PaperCorrection).where(PaperCorrection.paper_id == UUID(paper_id)))
        assert correction is not None
        assert correction.status == "pending"
        assert correction.proposed_value == -1.35
        assert session.scalar(select(AuditLog).where(AuditLog.action == "propose_dft_result_correction")) is not None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "accept_ai_adjudication")) is not None


def test_review_conflicts_auto_advance_batch_records_audit(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(
            title="Auto advance batch",
            pdf_path="auto-batch.pdf",
            workflow_status="Initial_Parsed",
            library_name="活跃测试库",
        )
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Reliable table evidence.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=4,
                bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.94,
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.20,
            confidence=0.9,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="gemini-batch",
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-conflicts/auto-advance",
        json={"paper_ids": [paper_id], "reviewer": "ai_auto_batch", "limit": 50},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] == 1
    assert payload["executed"] == 1
    assert payload["executed_items"][0]["action"] == "verify"

    with Session() as session:
        stored = session.get(DFTResult, UUID(payload["executed_items"][0]["target_id"]))
        assert stored.candidate_status in {"ML_Ready", "human_reviewed_needs_evidence"}
        assert session.scalar(select(AuditLog).where(AuditLog.action == "auto_apply_ai_adjudication")) is not None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "auto_advance_review_adjudication_batch")) is not None
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "review_adjudication")).all()
        assert jobs
        assert {job.library_name for job in jobs} == {"活跃测试库"}


def test_review_conflicts_auto_advance_batch_skips_object_review_targets(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(
            title="Auto advance guards object review",
            pdf_path="auto-guard.pdf",
            workflow_status="Initial_Parsed",
            library_name="自动推进测试库",
        )
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Reliable table evidence.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=4,
                bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.94,
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.20,
            confidence=0.9,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="gemini-batch",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="writing_card",
            target_id="writing-card-guard",
            field_name="core_hypothesis",
            decision="REVIEW",
            corrected_value=None,
            confidence=0.72,
            locator_status="exact_page",
            evidence_text="Object review targets must stay manual.",
            source="claude-writing",
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-conflicts/auto-advance",
        json={"paper_ids": [paper_id], "reviewer": "ai_auto_batch", "limit": 50},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] == 1
    assert payload["executed"] == 1
    assert payload["skipped"] == 0

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status in {"ML_Ready", "human_reviewed_needs_evidence"}
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "review_adjudication")).all()
        assert len(jobs) == 1
        assert jobs[0].payload["target_type"] == "dft_results"
        assert jobs[0].payload["target_id"] == row_id
        assert jobs[0].library_name == "自动推进测试库"
        assert session.scalar(select(AuditLog).where(AuditLog.target_type == "writing_card").where(AuditLog.action == "auto_apply_ai_adjudication")) is None


def test_batch_stage2_deep_parse_respects_requested_paper_ids_beyond_recent_window(workbench_env, monkeypatch):
    _, _, Session = workbench_env
    with Session() as session:
        old_target = Paper(
            title="Old unparsed target",
            pdf_path="old-target.pdf",
            workflow_status="Unparsed",
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        session.add(old_target)
        for index in range(505):
            session.add(
                Paper(
                    title=f"Recent paper {index}",
                    pdf_path=f"recent-{index}.pdf",
                    workflow_status="Imported",
                    created_at=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
                )
            )
        session.commit()
        old_target_id = str(old_target.id)

    rerun_calls: list[str] = []

    def fake_rerun_stage2(self, paper_id):
        rerun_calls.append(str(paper_id))
        return {"dft_results": 1, "mechanism_claims": 0, "writing_cards": 0}

    monkeypatch.setattr("app.services.paper_reprocessing.PaperReprocessingService.rerun_stage2", fake_rerun_stage2)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-center/prepare-ai-materials",
        json={"paper_ids": [old_target_id], "mode": "prepare_suspected_missing", "reviewer": "review_center_batch"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "prepare_suspected_missing"
    assert payload["selection_scope"] == "requested_paper_ids"
    assert payload["requested"] == 1
    assert payload["completed"] == 1
    assert rerun_calls == [old_target_id]


def test_review_center_sorting_and_batch_stage2_endpoints(workbench_env, monkeypatch):
    _, _, Session = workbench_env
    with Session() as session:
        newest = Paper(title="Newest", year=2023, pdf_path="newest.pdf", workflow_status="Imported")
        high_conflict = Paper(title="Conflict first", year=2024, pdf_path="conflict.pdf", workflow_status="Needs_Human_Confirmation")
        missing = Paper(title="Missing first", year=2022, pdf_path="missing.pdf", workflow_status="Suspected_Missing")
        session.add_all([newest, high_conflict, missing])
        session.flush()
        row = DFTResult(paper_id=high_conflict.id, property_type="adsorption_energy", value=-0.5, unit="eV")
        session.add(row)
        session.flush()
        session.add_all(
            [
                PaperCorrection(
                    paper_id=high_conflict.id,
                    source="ai-a",
                    field_name="dft_results",
                    target_path=f"dft_results:{row.id}:value",
                    operation="replace",
                    proposed_value=-0.5,
                    reason="Keep value",
                    status="pending",
                ),
                PaperCorrection(
                    paper_id=high_conflict.id,
                    source="ai-b",
                    field_name="dft_results",
                    target_path=f"dft_results:{row.id}:value",
                    operation="replace",
                    proposed_value=-0.7,
                    reason="Use table value",
                    status="pending",
                ),
            ]
        )
        session.commit()
        newest_id = str(newest.id)
        missing_id = str(missing.id)

    client = TestClient(app)
    conflicts_sorted = client.get("/api/workbench/review-center?limit=10&sort_by=conflicts_desc")
    assert conflicts_sorted.status_code == 200
    assert conflicts_sorted.json()["metadata"]["sort_by"] == "conflicts_desc"
    assert conflicts_sorted.json()["rows"][0]["title"] == "Conflict first"

    missing_sorted = client.get("/api/workbench/review-center?limit=10&sort_by=suspected_missing_desc")
    assert missing_sorted.status_code == 200
    assert missing_sorted.json()["rows"][0]["paper_id"] == missing_id

    recent_sorted = client.get("/api/workbench/review-center?limit=10&sort_by=recent")
    assert recent_sorted.status_code == 200
    assert any(row["paper_id"] == newest_id and row["paper_short_id"] for row in recent_sorted.json()["rows"])

    def fake_rerun_stage2(self, paper_id):
        return {"dft_results": 1, "mechanism_claims": 0, "writing_cards": 0}

    monkeypatch.setattr("app.services.paper_reprocessing.PaperReprocessingService.rerun_stage2", fake_rerun_stage2)

    batch = client.post(
        "/api/workbench/review-center/prepare-ai-materials",
        json={"paper_ids": [newest_id], "mode": "prepare_filtered", "reviewer": "review_center_batch"},
    )
    assert batch.status_code == 200
    assert batch.json()["mode"] == "prepare_filtered"
    assert batch.json()["completed"] == 1

    suspected = client.post(
        "/api/workbench/review-center/prepare-ai-materials",
        json={"paper_ids": [missing_id], "mode": "prepare_suspected_missing", "reviewer": "review_center_batch"},
    )
    assert suspected.status_code == 200
    assert suspected.json()["mode"] == "prepare_suspected_missing"
    assert suspected.json()["requested"] == 1
