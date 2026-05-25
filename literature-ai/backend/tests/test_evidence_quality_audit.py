from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    DFTResult,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    Paper,
    WritingCard,
)
from scripts.audit_evidence_extraction_quality import run_audit


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'evidence_audit.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, Session


def test_audit_reports_evidence_quality_without_writing(tmp_path):
    engine, Session = _session(tmp_path)
    with Session() as session:
        paper = Paper(title="Evidence Audit Paper", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()

        locator = EvidenceLocator(
            paper_id=paper.id,
            evidence_text="",
            page=None,
            bbox={"x0": 10, "y0": 10, "x1": 5, "y1": 20},
            locator_status="exact",
        )
        span = EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id="missing",
            text="",
            page=None,
        )
        claim = EvidenceClaim(
            paper_id=paper.id,
            claim_text="Claim",
            evidence_text="",
            validation_status="unverified",
        )
        session.add_all([locator, span, claim])
        session.commit()

        report = run_audit(session)
        session.commit()

        stored_locator = session.scalars(select(EvidenceLocator).where(EvidenceLocator.id == locator.id)).one()
        assert report["evidence"]["total"] == 3
        assert report["evidence"]["missing_page"] == 3
        assert report["evidence"]["missing_evidence_text"] == 3
        assert report["evidence"]["abnormal_bbox"] == 1
        assert report["locator_degradation"]["evidence_total"] == 3
        assert report["locator_degradation"]["evidence_text_only_count"] == 0
        assert report["locator_degradation"]["evidence_missing_locator_count"] == 1
        assert report["locator_degradation"]["evidence_unresolved_count"] == 2
        assert report["locator_degradation"]["evidence_bbox_without_page_count"] == 1
        assert report["locator_degradation"]["pdf_jump_exact_eligible_count"] == 0
        assert report["locator_degradation"]["pdf_jump_degraded_count"] == 3
        assert stored_locator.bbox == {"x0": 10, "y0": 10, "x1": 5, "y1": 20}
    engine.dispose()


def test_audit_does_not_treat_stale_or_unknown_as_safe_verified(tmp_path):
    engine, Session = _session(tmp_path)
    with Session() as session:
        paper = Paper(title="Review Audit Paper", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()

        dft_result = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            evidence_text=None,
        )
        session.add(dft_result)
        session.flush()
        stale_verified = ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(dft_result.id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="stale",
            evidence_text=None,
        )
        unknown_verified = ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="missing-target",
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="unknown",
            evidence_text="Evidence exists but target is unknown.",
        )
        session.add_all([stale_verified, unknown_verified])
        session.commit()

        report = run_audit(session)

        assert report["reviews"]["unsafe_resolution_status_counts"] == {"stale": 1, "unknown": 1}
        assert report["reviews"]["verified_but_unsafe_resolution"] == 2
        assert report["reviews"]["verified_missing_evidence_text"] == 1
        assert report["extraction"]["tables"]["dft_results"]["missing_safe_verified_review"] == 1
    engine.dispose()


def test_audit_flags_export_writing_and_ai_candidates_missing_provenance(tmp_path):
    engine, Session = _session(tmp_path)
    with Session() as session:
        paper = Paper(title="Export Audit Paper", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()

        dft_result = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            value=-0.7,
            unit="eV",
            evidence_text=None,
        )
        writing_card = WritingCard(
            paper_id=paper.id,
            research_gap="Gap",
            proposed_solution="Solution",
            evidence_chain=None,
        )
        run = ExternalAnalysisRun(paper_id=paper.id, source="manual", raw_text="{}")
        session.add_all([dft_result, writing_card, run])
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="correction",
            normalized_payload={"field": "value"},
            evidence_payload=None,
            status="pending",
        )
        session.add(candidate)
        session.commit()

        report = run_audit(session)

        assert report["extraction"]["tables"]["dft_results"]["missing_evidence_text_or_payload"] == 1
        assert report["export_writing_dataset"]["dft_results_export_missing_evidence"] == 1
        assert report["export_writing_dataset"]["dft_results_export_missing_safe_verified_review"] == 1
        assert report["export_writing_dataset"]["writing_cards_missing_evidence_chain"] == 1
        assert report["external_analysis_candidates"]["missing_evidence_payload"] == 1
    engine.dispose()
