from __future__ import annotations

import os

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base, CatalystSample, EvidenceLocator, ExtractionFieldReview, Paper
from app.services.locator_recovery_helper import (
    ControlledLocatorRecoveryHelper,
    LocatorRecoveryCandidate,
    LocatorRecoveryRequest,
    build_locator_repair_proposal,
)


DOC_BBOX = {
    "x0": 53.858,
    "y0": 125.995,
    "x1": 541.43,
    "y1": 71.087,
    "width": 595.0,
    "height": 842.0,
    "coordinate_system": "pdf_points",
}


def _proposal(**overrides):
    payload = {
        "paper_id": "paper-1",
        "review_id": "review-1",
        "field_name": "rate",
        "target_value": "0.2C",
        "evidence_text": "0.2 C cycling performance",
        "candidate_artifacts": (
            LocatorRecoveryCandidate(
                text="0.2 C cycling performance",
                source_artifact="markdown",
                page=6,
                bbox=DOC_BBOX,
            ),
        ),
    }
    payload.update(overrides)
    request = LocatorRecoveryRequest(**payload)
    return ControlledLocatorRecoveryHelper().build_proposal(request)


def _session(tmp_path):
    db_url = os.environ["LITAI_TEST_DATABASE_URL"]
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="D4-3F Paper", pdf_path="paper.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    return paper


def test_exact_text_match_returns_proposal_with_page_when_source_has_page():
    proposal = _proposal()

    assert proposal.status == "green"
    assert proposal.proposed_locator_status == "exact_page"
    assert proposal.page == 6
    assert proposal.bbox == DOC_BBOX
    assert proposal.match_method == "exact_match"
    assert proposal.source_artifact == "markdown"
    assert proposal.should_write_locator is False


def test_normalized_whitespace_match_works():
    proposal = _proposal(
        evidence_text="0.2 C cycling performance",
        candidate_artifacts=(
            LocatorRecoveryCandidate(
                text="0.2 C\n  cycling\tperformance",
                source_artifact="tei",
                page=6,
            ),
        ),
    )

    assert proposal.status == "green"
    assert proposal.match_method == "normalized_whitespace_match"
    assert proposal.page == 6


def test_docling_prov_page_no_bbox_can_produce_candidate_proposal():
    proposal = build_locator_repair_proposal(
        LocatorRecoveryRequest(
            paper_id="paper-1",
            review_id="review-1",
            field_name="rate",
            target_value="0.2C",
            evidence_text="Cycling performances at 0.2 C",
            docling_blocks=(
                {
                    "self_ref": "/texts/74",
                    "text": "Cycling performances at 0.2 C",
                    "source_artifact": "docling_json:/texts/74",
                    "prov": [
                        {
                            "page_no": 6,
                            "bbox": {"l": 53.858, "t": 125.995, "r": 541.43, "b": 71.087},
                        }
                    ],
                },
            ),
        )
    )

    assert proposal.status == "green"
    assert proposal.page == 6
    assert proposal.bbox == {
        "x0": 53.858,
        "y0": 125.995,
        "x1": 541.43,
        "y1": 71.087,
        "coordinate_system": "pdf_points",
    }
    assert proposal.source_artifact == "docling_json:/texts/74"


def test_ambiguous_match_does_not_become_green_or_safe():
    proposal = _proposal(
        evidence_text="HAADF-STEM",
        field_name="name",
        target_value="Fe-Co-V",
        candidate_artifacts=(
            LocatorRecoveryCandidate(text="HAADF-STEM", source_artifact="pdf", page=3),
            LocatorRecoveryCandidate(text="HAADF-STEM", source_artifact="pdf", page=7),
        ),
    )

    assert proposal.status == "yellow"
    assert proposal.confidence < 0.5
    assert "ambiguous_match_requires_human_selection" in proposal.blockers
    assert proposal.safe_verified is False
    assert proposal.export_eligible is False


def test_no_match_returns_red():
    proposal = _proposal(
        evidence_text="not in source",
        candidate_artifacts=(
            LocatorRecoveryCandidate(text="different source text", source_artifact="markdown", page=1),
        ),
    )

    assert proposal.status == "red"
    assert proposal.proposed_locator_status == "missing_locator"
    assert "no_text_match" in proposal.blockers


def test_current_red_convergence_settings_empty_dict_evidence_remains_red():
    proposal = _proposal(
        field_name="convergence_settings",
        target_value={},
        evidence_text="{}",
        candidate_artifacts=(
            LocatorRecoveryCandidate(text="DFT functional settings", source_artifact="markdown", page=8),
        ),
    )

    assert proposal.status == "red"
    assert proposal.proposed_locator_status == "missing_locator"
    assert "d4_3e_red_field_not_repairable" in proposal.blockers


def test_helper_does_not_mark_verified():
    proposal = _proposal()

    assert proposal.mark_verified is False


def test_helper_does_not_set_safe_verified():
    proposal = _proposal()

    assert proposal.safe_verified is False


def test_helper_does_not_claim_export_or_writing_eligible():
    proposal = _proposal()

    assert proposal.export_eligible is False
    assert proposal.writing_eligible is False
    assert "does_not_unlock_export_or_writing" in proposal.warnings


def test_helper_does_not_fabricate_page_when_source_lacks_page():
    proposal = _proposal(
        candidate_artifacts=(
            LocatorRecoveryCandidate(
                text="0.2 C cycling performance",
                source_artifact="markdown",
                page=None,
                bbox=DOC_BBOX,
            ),
        ),
    )

    assert proposal.status == "yellow"
    assert proposal.page is None
    assert proposal.proposed_locator_status == "text_only"
    assert "no_page_in_source" in proposal.blockers


def test_helper_does_not_fabricate_bbox_when_source_lacks_bbox():
    proposal = _proposal(
        candidate_artifacts=(
            LocatorRecoveryCandidate(
                text="0.2 C cycling performance",
                source_artifact="markdown",
                page=6,
                bbox=None,
            ),
        ),
    )

    assert proposal.status == "green"
    assert proposal.bbox is None
    assert "bbox_unavailable" in proposal.warnings


def test_proposal_defaults_to_no_write_and_requires_human_confirmation():
    proposal = _proposal()
    payload = proposal.to_dict()

    assert proposal.should_write_locator is False
    assert proposal.requires_human_confirmation is True
    assert payload["should_write_locator"] is False
    assert payload["requires_human_confirmation"] is True


def test_proposal_generation_does_not_write_review_row_or_locator(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            sample = CatalystSample(paper_id=paper.id, name="Fe-Co-V", catalyst_type="single_atom")
            session.add(sample)
            session.flush()
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="catalyst_samples",
                target_id=str(sample.id),
                field_name="catalyst_type",
                original_value="single_atom",
                reviewed_value="single_atom",
                evidence_text="single-atom catalyst, SAC",
                reviewer_status="pending",
                target_resolution_status="active",
            )
            session.add(review)
            session.commit()

            before_review = session.get(ExtractionFieldReview, review.id)
            before_locator_count = session.scalar(select(func.count()).select_from(EvidenceLocator))

            proposal = build_locator_repair_proposal(
                LocatorRecoveryRequest(
                    paper_id=str(paper.id),
                    review_id=str(review.id),
                    field_name=review.field_name,
                    target_value=review.reviewed_value,
                    evidence_text=review.evidence_text,
                    docling_blocks=(
                        {
                            "text": "single-atom catalyst, SAC",
                            "source_artifact": "docling_json:/texts/79",
                            "prov": [{"page_no": 7, "bbox": {"l": 53.0, "t": 20.0, "r": 120.0, "b": 40.0}}],
                        },
                    ),
                )
            )
            session.commit()
            session.expire_all()

            after_review = session.get(ExtractionFieldReview, review.id)
            after_locator_count = session.scalar(select(func.count()).select_from(EvidenceLocator))

            assert proposal.status == "green"
            assert before_locator_count == 0
            assert after_locator_count == 0
            assert before_review is not None
            assert after_review is not None
            assert after_review.reviewer_status == "pending"
            assert after_review.target_resolution_status == "active"
            assert after_review.evidence_text == "single-atom catalyst, SAC"
    finally:
        engine.dispose()
