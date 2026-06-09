from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
    WritingCard,
)
from app.db.session import get_db_session
from app.main import app
from app.services.gemini_audit_service import GeminiAuditService
from app.services.paper_query import PaperQueryService
from app.services.paper_workbench_service import PaperWorkbenchService
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
    _, _, Session = workbench_env
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
            prov=[{"bbox": {"l": 10, "t": 20, "r": 300, "b": 200}}],
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
    _, _, Session = workbench_env
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
    assert by_title["Review center paper"]["has_dft_candidates"] is True
    assert by_title["Review center paper"]["evidence_count"] == 1
    assert by_title["Review center paper"]["locator_issue_count"] == 1
    assert by_title["Review center paper"]["locator_issue_counts"]["missing_page"] == 1
    assert by_title["Review center paper"]["top_locator_issues"][0]["code"] == "missing_page"
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
