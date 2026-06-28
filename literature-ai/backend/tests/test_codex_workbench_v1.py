from __future__ import annotations

import os

import json
import re
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
    CatalystSample,
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
from app.services.dft_review_service import DFTResultReviewService
from app.services.gemini_audit_service import GeminiAuditService
from app.services.paper_query import PaperQueryService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.workbench_status import workflow_needs_human_confirmation


@pytest.fixture
def workbench_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        storage_root = root / "storage"
        monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "true")
        monkeypatch.setenv("LITAI_DOCLING_DO_OCR", "false")
        get_settings.cache_clear()

        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
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


def test_artifact_status_missing_pdf_reference_does_not_pose_as_invalid_pdf(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(
            title="Metadata only placeholder",
            pdf_path="",
            oa_status="metadata_only",
            workflow_status="Needs_Human_Confirmation",
            pdf_quality_status="Broken",
            pdf_quality_report={
                "quality_status": "Broken",
                "quality_score": 0.0,
                "reason": "missing_pdf_reference",
                "parse_allowed": False,
                "needs_human_confirmation": True,
                "metrics": {"file_exists": False},
            },
            workspace_path="by_id/test-metadata-only",
        )
        session.add(paper)
        session.commit()

        status = build_paper_artifact_status(paper, settings=get_settings())

    assert "missing_pdf" in status["blocking_errors"]
    assert "invalid_pdf_content" not in status["blocking_errors"]


def test_prepare_workspace_keeps_metadata_only_paper_out_of_fake_broken_state(workbench_env):
    _, storage_root, Session = workbench_env
    with Session() as session:
        paper = Paper(
            title="Metadata placeholder",
            abstract="Metadata-only abstract",
            pdf_path="",
            oa_status="metadata_only",
            workflow_status="Imported",
        )
        session.add(paper)
        session.commit()
        paper_id = paper.id

        summary = PaperWorkbenchService(session, get_settings()).prepare_paper_workspace(paper_id)
        session.refresh(paper)

    workspace_root = storage_root / "by_id" / str(paper_id)
    assert summary["workflow_status"] == "Imported"
    assert summary["pdf_quality_status"] is None
    assert paper.workflow_status == "Imported"
    assert paper.pdf_quality_status is None
    assert paper.pdf_quality_report is None
    with (workspace_root / "quality_report.json").open("r", encoding="utf-8") as handle:
        quality = json.load(handle)
    assert quality["quality_status"] == "Broken"
    assert quality["reason"] == "missing_pdf_reference"


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
    Image.new("RGB", (900, 1200), color=(245, 245, 245)).save(storage_root / "figures" / "page_004.png")
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
        assert figure_payload.asset_url == "/api/papers/assets/storage/figures/figure-2.png"
        assert figure_payload.image_review["crop_status"] == "candidate_crop"
        assert figure_payload.review_required is False
        assert figure_payload.figure_reliability_status == "reliable"
        assert figure_payload.figure_reliability_warnings == []
        assert figure_payload.object_review_audit_count == 2
        assert figure_payload.latest_object_review_audit["source"] == "codex_figure_audit"
        assert figure_payload.latest_object_review_audit["decision"] == "REVISE"
        assert figure_payload.latest_object_review_audit["verification_status"] == "unverified"
        assert figure_payload.conflict_count == 0
        assert figure_payload.field_conflicts == []
        assert conflict_payload["conflict_count"] == 0
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
        assert card_payload.conflict_count == 0
        assert card_payload.field_conflicts == []
        assert conflict_payload["conflict_count"] == 0
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
        assert claim_payload.conflict_count == 0
        assert claim_payload.field_conflicts == []
        assert conflict_payload["conflict_count"] == 0
        assert stored_claim.claim_text == "Defect sites strengthen polysulfide adsorption through charge redistribution."
        assert session.get(Paper, paper_id).workflow_status == "Parsed_Material_Ready"
        assert session.query(PaperCorrection).count() == 0


def test_paper_detail_exposes_dft_conflict_affected_field_names(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT paper detail conflicts", pdf_path="dft-paper-detail.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="CO",
            reaction_step="adsorption",
            value=-3.2,
            unit="eV",
            evidence_text="Adsorption energy is -3.2 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-3.2",
                reviewed_value="-3.2",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -3.2,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
                "reaction_step": "transition state",
            },
            confidence=0.86,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_detail_conflict",
        )
        session.commit()

        detail = PaperQueryService(session).get_paper_detail(paper.id)

    assert detail is not None
    dft_payload = detail.dft_results_items[0]
    assert dft_payload.conflict_count == 1
    assert set(dft_payload.affected_field_names) >= {"property_type", "adsorbate", "reaction_step"}
    assert set(dft_payload.conflict_field_names) >= {"property_type", "adsorbate", "reaction_step"}
    assert set(dft_payload.field_conflicts[0]["affected_field_names"]) >= {"property_type", "adsorbate", "reaction_step"}
    assert dft_payload.field_conflicts[0]["field_name"] == "value"


def test_rejected_dft_target_clears_current_and_summary_conflicts(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(
            title="Rejected DFT conflict lifecycle",
            pdf_path="rejected-dft-conflicts.pdf",
            workflow_status="Parsed_Material_Ready",
        )
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="*O",
            value=1.8,
            unit="eV",
            candidate_status="Rejected",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=1.8,
                reviewed_value=None,
                reviewer_status="rejected",
                reviewer="literature_library_dft",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="REVISE",
            corrected_value=2.3,
            confidence=0.9,
            locator_status="exact_page",
            evidence_text="Historical AI correction.",
            source="older_dft_audit",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="REJECT",
            corrected_value=None,
            confidence=0.95,
            locator_status="exact_page",
            evidence_text="Final rejection evidence.",
            source="newer_dft_audit",
        )
        session.commit()

        conflicts = ReviewConflictAggregationService(session).list_conflicts(
            paper_id=paper.id,
            limit=1000,
        )
        center = PaperWorkbenchService(session, get_settings()).review_center(
            limit=10,
            paper_ids=[paper.id],
        )["rows"][0]
        light_center = PaperWorkbenchService(session, get_settings()).review_center(
            limit=10,
            summary_only=True,
            paper_ids=[paper.id],
        )["rows"][0]
        light_detail = PaperQueryService(session).get_paper_detail(paper.id, compact=True)

    assert conflicts["conflict_count"] == 0
    assert center["has_dft_candidates"] is True
    assert center["has_active_dft_candidates"] is False
    assert center["active_dft_candidate_count"] == 0
    assert center["dft_review_conflict_count"] == 0
    assert center["dft_review_conflict_total_count"] == 0
    assert center["dft_completeness_status"] == "Human_Complete"
    assert light_center["dft_completeness_status"] == "Human_Complete"
    assert light_center["dft_audit"]["ide_ai_review_recommended"] is False
    assert light_center["dft_audit"]["rescan_stop_reason"] == "all_candidates_rejected"
    assert light_detail is not None
    assert light_detail.dft_review_status == "reviewed"


def test_paper_detail_dft_conflict_state_stays_pending_when_whole_row_fix_is_not_fully_absorbed(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT detail pending whole-row fix", pdf_path="dft-detail-pending.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="H2",
            reaction_step="DFT",
            value=-3.2,
            unit="eV",
            evidence_text="Adsorption energy is -3.2 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-3.2",
                reviewed_value="-3.2",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -3.2,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
            },
            confidence=0.86,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_detail_pending",
            extra_payload={"adsorbate": None},
        )
        session.commit()

        detail = PaperQueryService(session).get_paper_detail(paper.id)

    assert detail is not None
    dft_payload = detail.dft_results_items[0]
    assert dft_payload.candidate_status == "ML_Ready"
    assert dft_payload.conflict_count == 1
    assert "adsorbate" in dft_payload.affected_field_names
    assert "adsorbate_conflict" in dft_payload.field_conflicts[0]["conflict_types"]


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
            library_name="审核库A",
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
    assert by_title["Review center paper"]["library_name"] == "审核库A"
    assert by_title["Review center paper"]["pdf_exists"] is True
    assert by_title["Review center paper"]["pdf_url"].endswith(f"/api/papers/{by_title['Review center paper']['paper_id']}/pdf")
    assert by_title["Review center paper"]["pdf_artifact_status"]["pdf_exists"] is True
    assert by_title["Review center paper"]["pdf_artifact_status"]["pdf_path_kind"] == "storage_relative"
    assert by_title["Review center paper"]["has_dft_candidates"] is True
    assert by_title["Review center paper"]["has_active_dft_candidates"] is True
    assert by_title["Review center paper"]["active_dft_candidate_count"] == 1
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


def test_review_center_api_filters_by_library_name(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper_a = Paper(
            title="Library A review paper",
            pdf_path="library-a.pdf",
            library_name="库A",
            paper_type="supplementary",
        )
        paper_b = Paper(title="Library B review paper", pdf_path="library-b.pdf", library_name="库B")
        session.add_all([paper_a, paper_b])
        session.flush()
        session.add_all(
            [
                DFTResult(paper_id=paper_a.id, property_type="band_gap", value=1.1, unit="eV"),
                DFTResult(paper_id=paper_b.id, property_type="band_gap", value=2.2, unit="eV"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/workbench/review-center", params={"library_name": "库A"})

    assert response.status_code == 200
    payload = response.json()
    titles = {row["title"] for row in payload["rows"]}
    assert titles == {"Library A review paper"}
    assert payload["rows"][0]["paper_type"] == "supplementary"
    assert payload["metadata"]["library_name"] == "库A"


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
        row_id = str(row.id)
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
    assert row_payload["review_conflict_total_count"] == 1
    assert row_payload["review_conflict_count"] == 0


def test_review_center_suppresses_conflicts_when_current_value_already_matches_finalized_truth(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Resolved conflict paper", pdf_path="resolved-conflict.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="activation_energy",
            value=0.109,
            unit="eV",
            evidence_text="109 meV from Table 1.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="0.109",
                reviewed_value="0.109",
                unit="eV",
                evidence_text="109 meV from Table 1.",
                reviewer_status="verified",
                reviewer="codex_low_risk_auto",
                reviewer_note="Verified from imported evidence.",
            )
        )
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="codex_low_risk_auto",
                field_name="dft_results",
                target_path=f"dft_results:{row.id}:value",
                operation="replace",
                proposed_value=0.109,
                reason="Normalized 109 meV into eV final storage.",
                status="approved",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=109.0,
            confidence=0.95,
            locator_status="exact_page",
            evidence_text="109 meV from Table 1.",
            source="gemini-finalized",
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    conflicts = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}")
    assert conflicts.status_code == 200
    assert conflicts.json()["conflict_count"] == 0

    center = client.get("/api/workbench/review-center?limit=50")
    assert center.status_code == 200
    row_payload = next(item for item in center.json()["rows"] if item["paper_id"] == paper_id)
    assert row_payload["has_dft_candidates"] is True
    assert row_payload["has_active_dft_candidates"] is False
    assert row_payload["active_dft_candidate_count"] == 0
    assert row_payload["review_conflict_total_count"] == 1
    assert row_payload["review_conflict_count"] == 0


def test_review_conflicts_ignore_adopted_figure_revision_opinions(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Adopted figure revision paper", pdf_path="figure-adopted.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            figure_label="fig_1",
            caption="Figure 1. Catalyst structure and spectra.",
            page=5,
            content_summary="Old summary",
            image_path="figures/figure1.png",
            crop_status="candidate_crop",
            figure_role="structure",
        )
        session.add(figure)
        session.flush()
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="figures",
            target_id=figure.id,
            field_name="content_summary",
            decision="REVISE",
            corrected_value="Updated summary from reviewed figure evidence.",
            confidence=0.93,
            locator_status="exact_page",
            evidence_text="Figure 1",
            source="codex_figure_review",
        )
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="gpt-5-codex",
                field_name="figures",
                target_path=f"figures:{figure.id}:content_summary",
                operation="replace",
                proposed_value="Updated summary from reviewed figure evidence.",
                reason="Manual adjudication adopted this figure-review opinion.",
                evidence_payload={
                    "page": 5,
                    "figure": "Figure 1",
                    "quoted_text": "Figure 1",
                    "review_source": "codex_figure_review",
                    "review_source_label": "codex_figure_review",
                    "review_decision": "REVISE",
                },
                status="approved",
            )
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    conflicts = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}")
    assert conflicts.status_code == 200
    assert conflicts.json()["conflict_count"] == 0

    center = client.get("/api/workbench/review-center?limit=50")
    assert center.status_code == 200
    row_payload = next(item for item in center.json()["rows"] if item["paper_id"] == paper_id)
    assert row_payload["visual_review_conflict_count"] == 0
    assert row_payload["visual_review_conflict_total_count"] == 0
    assert row_payload["review_conflict_count"] == 0
    assert row_payload["review_conflict_total_count"] == 0


def test_review_conflicts_ignore_dft_object_audit_already_absorbed_by_final_row(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Absorbed DFT opinion paper", pdf_path="absorbed-dft.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="d_band_center",
            adsorbate=None,
            reaction_step="d-band center",
            source_section="Page 1",
            value=-2.85,
            unit="eV",
            evidence_text="slight downshift of d-band center was observed for ISAA In-Pdene (-3.03 eV) relative to that of Pdene (-2.85 eV).",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-2.85",
                reviewed_value="-2.85",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="gemini_finalized",
                reviewer_note="Verified Pdene d-band center against page 1.",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -2.85,
                "unit": "eV",
                "material": "Pdene",
                "property": "d_band_center",
                "adsorbate": None,
            },
            confidence=0.92,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_review",
            extra_payload={"reaction_step": None, "adsorbate": None},
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    conflicts = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}")
    assert conflicts.status_code == 200
    assert conflicts.json()["conflict_count"] == 0

    center = client.get("/api/workbench/review-center?limit=50")
    assert center.status_code == 200
    row_payload = next(item for item in center.json()["rows"] if item["paper_id"] == paper_id)
    assert row_payload["dft_review_conflict_count"] == 0
    assert row_payload["review_conflict_count"] == 0


def test_review_conflicts_dft_same_numeric_whole_row_dict_reports_real_field_conflict(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT semantic conflict paper", pdf_path="dft-semantic.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="H2",
            reaction_step="DFT",
            value=-12.2,
            unit="eV",
            evidence_text="Adsorption energy is -12.2 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-12.2",
                reviewed_value="-12.2",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -12.2,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
            },
            confidence=0.87,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_semantic",
            extra_payload={"reaction_step": None, "adsorbate": None},
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=True)

    assert payload["conflict_count"] == 1
    conflict = payload["rows"][0]
    assert conflict["field_name"] == "value"
    assert "adsorbate_conflict" in conflict["conflict_types"]
    assert "reaction_step_conflict" not in conflict["conflict_types"]
    assert "value_conflict" not in conflict["conflict_types"]
    assert "decision_conflict" not in conflict["conflict_types"]


def test_review_conflicts_approved_scalar_correction_does_not_adopt_whole_row_dict_with_extra_blank_field_change(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT adopted scalar correction", pdf_path="dft-adopted-scalar.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="Co atom",
            reaction_step="SAC-to-DAC stability comparison",
            value=-10.5,
            unit="eV",
            evidence_text="Adsorption energy is -10.5 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-10.5",
                reviewed_value="-10.5",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -10.5,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
            },
            confidence=0.91,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_adopted",
            extra_payload={"reaction_step": None, "adsorbate": None},
        )
        session.flush()
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="gpt-5-codex",
                field_name="dft_results",
                target_path=f"dft_results:{row.id}:value",
                operation="replace",
                proposed_value=-10.5,
                reason="Approved scalar correction already adopted the opinion.",
                evidence_payload={
                    "page": 5,
                    "review_source": "codex_dft_adopted",
                    "review_source_label": "codex_dft_adopted",
                    "review_decision": "PROPOSED",
                    "unit": "eV",
                },
                status="approved",
            )
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=True)

    assert payload["conflict_count"] == 1
    conflict = payload["rows"][0]
    assert "adsorbate_conflict" in conflict["conflict_types"]
    assert "reaction_step_conflict" not in conflict["conflict_types"]
    assert "value_conflict" not in conflict["conflict_types"]
    assert "decision_conflict" not in conflict["conflict_types"]


def test_review_conflicts_fully_absorbed_whole_row_dft_opinion_is_not_reported(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT absorbed whole-row opinion", pdf_path="dft-absorbed.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate=None,
            reaction_step="DFT",
            value=-10.5,
            unit="eV",
            evidence_text="Adsorption energy is -10.5 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-10.5",
                reviewed_value="-10.5",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -10.5,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
                "reaction_step": "DFT",
            },
            confidence=0.91,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_absorbed",
            extra_payload={"reaction_step": "DFT", "adsorbate": None},
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=False)

    assert payload["conflict_count"] == 0
    assert payload["rows"] == []


def test_review_conflicts_ignore_pending_duplicate_dft_unit_correction_after_approved_apply(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT duplicate unit correction", pdf_path="dft-duplicate-unit.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="bader_charge",
            adsorbate=None,
            reaction_step=None,
            value=-0.3,
            unit="e",
            evidence_text="Bader charge is -0.3 e.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="antigravity_dft_20260621_145940",
                field_name="dft_results",
                target_path=f"dft_results:{row.id}:unit",
                operation="replace",
                proposed_value="e",
                reason="Approved correction already applied the DFT unit.",
                status="approved",
            )
        )
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="local_ide",
                field_name="dft_results",
                target_path=f"dft_results:{row.id}:unit",
                operation="replace",
                proposed_value="e",
                reason="Older pending duplicate should not remain a conflict.",
                status="pending",
            )
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=False)

    assert payload["conflict_count"] == 0
    assert payload["rows"] == []


def test_review_conflicts_rejected_original_row_is_not_reported_when_matching_replacement_row_exists(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT rejected original replaced", pdf_path="dft-replaced.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        rejected_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="CO",
            reaction_step=None,
            value=10.5,
            unit="eV",
            evidence_text="Misparsed legacy row.",
            candidate_status="Rejected",
        )
        replacement_row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate=None,
            reaction_step="SAC-to-DAC stability comparison",
            value=-10.5,
            unit="eV",
            evidence_text="Correct replacement row.",
            candidate_status="ML_Ready",
        )
        session.add_all([rejected_row, replacement_row])
        session.flush()
        session.add_all(
            [
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(rejected_row.id),
                    field_name="adsorbate",
                    original_value="CO",
                    reviewed_value=None,
                    evidence_text=rejected_row.evidence_text,
                    reviewer_status="rejected",
                    reviewer="reject_legacy",
                ),
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(rejected_row.id),
                    field_name="value",
                    original_value=10.5,
                    reviewed_value=None,
                    unit="eV",
                    evidence_text=rejected_row.evidence_text,
                    reviewer_status="rejected",
                    reviewer="reject_legacy",
                ),
            ]
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=rejected_row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -10.5,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
                "reaction_step": "SAC-to-DAC stability comparison",
            },
            confidence=0.92,
            locator_status="exact_page",
            evidence_text="Correct replacement row.",
            source="codex_dft_replacement",
            extra_payload={"adsorbate": None, "reaction_step": "SAC-to-DAC stability comparison"},
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=False)

    assert payload["conflict_count"] == 0
    assert payload["rows"] == []


def test_review_conflicts_rejected_original_row_is_not_reported_when_replacement_is_semantically_compatible(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT rejected original semantically replaced", pdf_path="dft-semantic-replaced.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        rejected_row = DFTResult(
            paper_id=paper.id,
            property_type="reaction_barrier",
            adsorbate=None,
            reaction_step=None,
            value=0.75,
            unit="eV",
            evidence_text="Legacy row missing object identity.",
            candidate_status="Rejected",
        )
        replacement_row = DFTResult(
            paper_id=paper.id,
            property_type="reaction_barrier",
            adsorbate="HOO*",
            reaction_step="HOO* transition from the initial molecular state to the final dissociated state",
            value=0.75,
            unit="eV",
            evidence_text="Correct replacement row.",
            candidate_status="ML_Ready",
        )
        session.add_all([rejected_row, replacement_row])
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(rejected_row.id),
                field_name="value",
                original_value=0.75,
                reviewed_value=None,
                unit="eV",
                evidence_text=rejected_row.evidence_text,
                reviewer_status="rejected",
                reviewer="reject_legacy",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=rejected_row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": 0.75,
                "unit": "eV",
                "property": "reaction_barrier",
                "adsorbate": "HOO",
                "reaction_step": "HOO* transition barrier",
            },
            confidence=0.94,
            locator_status="exact_page",
            evidence_text="Correct replacement row.",
            source="codex_dft_semantic_replacement",
            extra_payload={"adsorbate": "HOO", "reaction_step": "HOO* transition barrier"},
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=False)

    assert payload["conflict_count"] == 0
    assert payload["rows"] == []


def test_review_conflicts_dft_explicit_reaction_step_difference_still_produces_reaction_step_conflict(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT reaction-step disagreement", pdf_path="dft-reaction-step.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate=None,
            reaction_step="DFT",
            value=-12.2,
            unit="eV",
            evidence_text="Binding energy is -12.2 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-12.2",
                reviewed_value="-12.2",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -12.2,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
                "reaction_step": "HOO* transition barrier",
            },
            confidence=0.87,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_reaction_step",
            extra_payload={"reaction_step": None},
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, include_non_conflicts=True)

    assert payload["conflict_count"] == 1
    conflict_types = payload["rows"][0]["conflict_types"]
    assert "reaction_step_conflict" in conflict_types
    assert "value_conflict" not in conflict_types


def test_review_conflicts_dft_numeric_disagreement_still_produces_value_conflict(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT numeric disagreement", pdf_path="dft-numeric-disagreement.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-10.5,
            unit="eV",
            evidence_text="Adsorption energy evidence.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-10.5,
            confidence=0.8,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="gemini_same_value",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={"value": -10.8, "unit": "eV", "adsorbate": "Li2S4"},
            confidence=0.84,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_new_value",
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id)

    assert payload["conflict_count"] == 1
    assert "value_conflict" in payload["rows"][0]["conflict_types"]


def test_review_conflicts_dft_opposite_review_decisions_still_produce_decision_conflict(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT decision disagreement", pdf_path="dft-decision-disagreement.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-1.5,
            unit="eV",
            evidence_text="Adsorption energy evidence.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.5,
            confidence=0.8,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="gemini_accept",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="REJECT",
            corrected_value=-1.5,
            confidence=0.79,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="glm_reject",
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id)

    assert payload["conflict_count"] == 1
    conflict_types = payload["rows"][0]["conflict_types"]
    assert "decision_conflict" in conflict_types
    assert "value_conflict" not in conflict_types


def test_review_conflicts_non_dft_object_reviews_stay_out_of_dft_conflict_queue(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Non DFT conflict regression", pdf_path="non-dft-conflict.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            figure_label="Figure 1",
            caption="Figure 1",
            page=3,
            crop_status="candidate_crop",
            image_path="figures/figure-1.png",
        )
        session.add(figure)
        session.flush()
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="figures",
            target_id=figure.id,
            field_name="crop_status",
            decision="PASS",
            corrected_value="usable_crop",
            confidence=0.73,
            locator_status="exact_page",
            evidence_text="Figure 1 crop is usable.",
            source="figure_accept",
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="figures",
            target_id=figure.id,
            field_name="crop_status",
            decision="REVISE",
            corrected_value="needs_manual_crop_check",
            confidence=0.75,
            locator_status="exact_page",
            evidence_text="Figure 1 crop needs manual review.",
            source="figure_revise",
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id)

    assert payload["conflict_count"] == 0
    assert payload["rows"] == []


def test_review_conflicts_figure_key_elements_structural_shape_equivalence_does_not_conflict(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Figure key elements equivalence", pdf_path="figure-key-elements.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            figure_label="fig_2",
            caption="Fig. 2",
            page=4,
            crop_status="recropped",
            image_path="figures/figure-2.png",
            figure_role="characterization",
            content_summary="(a) STEM image and (b) EXAFS comparison.",
        )
        session.add(figure)
        session.flush()
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="local_ide",
                field_name="figures",
                target_path=f"figures:{figure.id}:key_elements",
                operation="replace",
                proposed_value=[
                    {"description": "Panel (a): HAADF-STEM image with Pt dispersion"},
                    {"description": "Panel (b): EXAFS fitting and shell assignment"},
                ],
                reason="Original approved shape used dict entries.",
                evidence_payload={"page": 4, "figure": "Fig. 2", "quoted_text": "HAADF-STEM image"},
                status="approved",
                reviewed_by="local_ide",
            )
        )
        session.add(
            PaperCorrection(
                paper_id=paper.id,
                source="ide_ai",
                field_name="figures",
                target_path=f"figures:{figure.id}:key_elements",
                operation="replace",
                proposed_value=[
                    "Panel (a): HAADF-STEM image with Pt dispersion",
                    "Panel (b): EXAFS fitting and shell assignment",
                ],
                reason="Canonical plain-string cleanup.",
                evidence_payload={"page": 4, "figure": "Fig. 2", "quoted_text": "EXAFS fitting"},
                status="approved",
                reviewed_by="ide_ai",
            )
        )
        session.commit()
        payload = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id)

    assert payload["conflict_count"] == 0


def test_review_adjudication_uses_real_dft_field_names_for_whole_row_conflicts(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="DFT adjudication field mapping", pdf_path="dft-adjudication-fields.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="Co atom",
            reaction_step="SAC-to-DAC stability comparison",
            value=-10.5,
            unit="eV",
            evidence_text="Binding energy is -10.5 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value="-10.5",
                reviewed_value="-10.5",
                unit="eV",
                evidence_text=row.evidence_text,
                reviewer_status="verified",
                reviewer="verified_scalar",
            )
        )
        _seed_object_review_audit(
            session,
            paper_id=paper.id,
            target_type="dft_results",
            target_id=row.id,
            field_name="value",
            decision="PROPOSED",
            corrected_value={
                "value": -10.5,
                "unit": "eV",
                "property": "binding_energy",
                "adsorbate": None,
            },
            confidence=0.9,
            locator_status="exact_page",
            evidence_text=row.evidence_text,
            source="codex_dft_field_mapping",
            extra_payload={"reaction_step": None, "adsorbate": None},
        )
        session.commit()
        payload = ReviewAdjudicationService(session).list_with_adjudication(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            include_non_conflicts=True,
        )

    conflict_row = payload["rows"][0]
    assert conflict_row["affected_field_names"] == ["adsorbate"]
    assert "reaction_step_conflict" not in conflict_row["conflict_types"]
    recommended_payload = conflict_row["adjudication"]["recommended_payload"]
    assert recommended_payload["affected_field_names"] == ["adsorbate"]
    if "field_names" in recommended_payload:
        assert recommended_payload["field_names"] != ["value"]
        assert "adsorbate" in recommended_payload["field_names"]


def test_review_center_suppresses_duplicate_system_candidate_when_finalized_row_exists(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Duplicate system candidate paper", pdf_path="duplicate-system-candidate.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        session.add_all(
            [
                DFTResult(
                    paper_id=paper.id,
                    property_type="permeance",
                    adsorbate="H2 through large deformable GDY membrane (1620 C atoms)",
                    value=782000,
                    unit="GPU",
                    candidate_status="ML_Ready",
                    evidence_text="Finalized permeance value.",
                    reaction_step="300 K, ILJ+AIREBO, deformable",
                    evidence_payload={
                        "material_binding": {
                            "evidence_anchor": {
                                "page": 8,
                                "source_document_type": "main",
                            }
                        }
                    },
                ),
                DFTResult(
                    paper_id=paper.id,
                    property_type="permeance",
                    adsorbate="H2",
                    value=782000,
                    unit="GPU",
                    candidate_status="system_candidate",
                    evidence_text="Duplicate system candidate for the same finalized value.",
                    reaction_step="MD simulation",
                    evidence_payload={
                        "page": 8,
                        "source_document_type": "main",
                    },
                ),
            ]
        )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    center = client.get("/api/workbench/review-center?limit=50")
    assert center.status_code == 200
    row_payload = next(item for item in center.json()["rows"] if item["paper_id"] == paper_id)
    assert row_payload["has_dft_candidates"] is True
    assert row_payload["dft_candidate_count"] == 2
    assert row_payload["has_active_dft_candidates"] is False
    assert row_payload["active_dft_candidate_count"] == 0


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
    extra_payload: dict | None = None,
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
                **(extra_payload or {}),
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


def test_apply_imported_whole_row_dft_opinion_applies_explicit_null_adsorbate(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Imported whole-row null", pdf_path="imported-null.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="H2",
            reaction_step="DFT",
            value=-1.25,
            unit="eV",
            evidence_text="Binding energy is -1.25 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.commit()
        paper_id = paper.id
        row_id = row.id

    with Session() as session:
        result = DFTResultReviewService(session).apply_imported_opinion(
            paper_id=paper_id,
            result_id=row_id,
            reviewer="imported_ai_test",
            opinion={
                "decision": "PROPOSED",
                "source": "imported_ai",
                "source_label": "imported_ai",
                "corrected_value": {
                    "property_type": "binding_energy",
                    "adsorbate": None,
                    "reaction_step": "DFT",
                    "value": -1.25,
                    "unit": "eV",
                },
                "evidence_location": {
                    "page": 5,
                    "quoted_text": "Binding energy is -1.25 eV.",
                    "locator_status": "exact_page",
                },
            },
        )
        session.commit()
        stored = session.get(DFTResult, row_id)
        review = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
                ExtractionFieldReview.field_name == "adsorbate",
            )
        )

    assert result["action"] == "verify"
    assert stored is not None
    assert stored.adsorbate is None
    assert review is not None
    assert review.reviewer_status == "verified"


def test_apply_imported_whole_row_dft_opinion_repairs_verified_field_with_expected_versions(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Imported whole-row repair", pdf_path="imported-repair.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Fe-based DAC structures")
        session.add(sample)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            property_type="binding_energy",
            adsorbate="H2",
            reaction_step="DFT",
            value=-1.25,
            unit="eV",
            evidence_text="Binding energy is -1.25 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        session.add_all(
            [
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="adsorbate",
                    original_value="H2",
                    reviewed_value="H2",
                    evidence_text=row.evidence_text,
                    reviewer_status="verified",
                    reviewer="verified_scalar",
                ),
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    original_value="-1.25",
                    reviewed_value="-1.25",
                    unit="eV",
                    evidence_text=row.evidence_text,
                    reviewer_status="verified",
                    reviewer="verified_scalar",
                ),
            ]
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    with Session() as session:
        review_versions = {
            review.field_name: review.write_version
            for review in session.scalars(
                select(ExtractionFieldReview).where(
                    ExtractionFieldReview.paper_id == paper_id,
                    ExtractionFieldReview.target_type == "dft_results",
                    ExtractionFieldReview.target_id == str(row_id),
                )
            ).all()
        }
        result = DFTResultReviewService(session).apply_imported_opinion(
            paper_id=paper_id,
            result_id=row_id,
            reviewer="imported_ai_test",
            expected_row_state={
                "candidate_status": "ML_Ready",
                "property_type": "binding_energy",
                "adsorbate": "H2",
                "reaction_step": "DFT",
                "value": -1.25,
                "unit": "eV",
            },
            expected_write_versions={"adsorbate": review_versions["adsorbate"]},
            opinion={
                "decision": "PROPOSED",
                "source": "imported_ai",
                "source_label": "imported_ai",
                "corrected_value": {
                    "property_type": "binding_energy",
                    "material": "Fe-based DAC structures",
                    "adsorbate": None,
                    "reaction_step": "DFT",
                    "value": -1.25,
                    "unit": "eV",
                },
                "evidence_location": {
                    "page": 5,
                    "quoted_text": "Binding energy is -1.25 eV.",
                    "locator_status": "exact_page",
                },
            },
        )
        stored = session.get(DFTResult, row_id)
        adsorbate_review = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
                ExtractionFieldReview.field_name == "adsorbate",
            )
        )

    assert result["action"] == "verify"
    assert stored is not None
    assert stored.adsorbate is None
    assert adsorbate_review is not None
    assert adsorbate_review.reviewed_value is None
    assert adsorbate_review.write_version == review_versions["adsorbate"] + 1


def test_apply_imported_whole_row_dft_opinion_rejects_stale_expected_row_state_via_api(workbench_env):
    _, _, Session = workbench_env
    with Session() as session:
        paper = Paper(title="Imported stale row state", pdf_path="imported-stale.pdf", workflow_status="Parsed_Material_Ready")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            adsorbate="H2",
            reaction_step="DFT",
            value=-1.25,
            unit="eV",
            evidence_text="Binding energy is -1.25 eV.",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/dft-results/{row_id}/apply-imported-opinion",
        json={
            "reviewer": "imported_ai_test",
            "expected_row_state": {
                "candidate_status": "ML_Ready",
                "adsorbate": "WRONG_EXPECTED_VALUE",
                "value": -1.25,
            },
            "opinion": {
                "decision": "PROPOSED",
                "source": "imported_ai",
                "source_label": "imported_ai",
                "corrected_value": {
                    "property_type": "binding_energy",
                    "adsorbate": None,
                    "reaction_step": "DFT",
                    "value": -1.25,
                    "unit": "eV",
                },
                "evidence_location": {
                    "page": 5,
                    "quoted_text": "Binding energy is -1.25 eV.",
                    "locator_status": "exact_page",
                },
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "write_conflict:dft_result_state_stale"


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
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-conflicts/auto-advance",
        json={"paper_ids": [paper_id], "reviewer": "ai_auto_batch", "limit": 50},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible"] == 1
    assert payload["executed"] == 0
    assert payload["skipped"] == 1
    assert payload["skipped_items"][0]["status"] == "audit_only"
    assert payload["skipped_items"][0]["reason"] == "dft_auto_advance_disabled"

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        assert session.scalar(select(AuditLog).where(AuditLog.action == "auto_apply_ai_adjudication")) is None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "auto_advance_review_adjudication_batch")) is not None
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "review_adjudication")).all()
        assert jobs == []


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
    assert payload["executed"] == 0
    assert payload["skipped"] == 1
    assert payload["skipped_items"][0]["status"] == "audit_only"

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "review_adjudication")).all()
        assert jobs == []
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
        session.add(
            ExtractionFieldReview(
                paper_id=high_conflict.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=-0.5,
                reviewed_value=-0.5,
                unit="eV",
                reviewer_status="review_conflict",
                reviewer="review_center_sort_fixture",
                review_payload={
                    "ai_audits": [
                        {
                            "source": "ai-a",
                            "decision": "PASS",
                            "corrected_value": -0.5,
                            "unit": "eV",
                            "confidence": 0.93,
                            "evidence_payload": {
                                "evidence_text": "DFT adsorption energy is -0.5 eV.",
                                "locator": {"page": 3, "status": "exact_page"},
                            },
                        },
                        {
                            "source": "ai-b",
                            "decision": "REVISE",
                            "corrected_value": -0.7,
                            "unit": "eV",
                            "confidence": 0.91,
                            "evidence_payload": {
                                "evidence_text": "The table reports -0.7 eV.",
                                "locator": {"page": 3, "status": "exact_page"},
                            },
                        },
                    ]
                },
            )
        )
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

    serial_sorted = client.get("/api/workbench/review-center?limit=10&sort_by=paper_code_asc")
    assert serial_sorted.status_code == 200
    code_numbers = []
    for row in serial_sorted.json()["rows"]:
        code = str(row.get("paper_code") or "").strip().upper()
        match = re.match(r"^[A-Z](\d+)$", code)
        if match:
            code_numbers.append(int(match.group(1)))
    assert code_numbers == sorted(code_numbers)

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
