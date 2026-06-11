"""D1 Phase 3: Review boundary enforcement tests.

Verifies that:
1. is_safe_verified_review only accepts verified + active/remapped
2. stale / ambiguous / unresolved / unknown / None are not safe verified
3. verified + stale target is not safe verified
4. AI candidate cannot overwrite human verified
5. ExternalAnalysisCandidate missing evidence_payload cannot enter trusted path
6. Import AI candidate does not create reviewer_status=verified
7. Manual verified save requires evidence reference
8. Manual verified save requires evidence_text
9. Export/writing continues to block unsafe review
10. stale/ambiguous/unresolved/unknown cannot be serialized as verified
11. Remapped target can be safe verified but must keep remapped status
12. Orphaned/missing target must be blocked
13. Review boundary reason can be returned by API or audit statistics
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
    WritingCard,
)
from app.rag.retriever import Retriever
from app.services.extraction_review_service import ExtractionReviewService
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.paper_query import PaperQueryService
from app.services.review_service import ReviewService
from app.utils.review_safety import (
    build_review_boundary_reason,
    can_ai_candidate_update_target,
    can_manual_review_mark_verified,
    is_safe_verified_review,
    is_unsafe_review_status,
    normalize_review_status,
    normalize_target_resolution_status,
    serialize_review_gate,
    trusted_external_candidate,
)


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'review_boundary.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="Boundary Test Paper", pdf_path="test.pdf", authors=["T"])
    session.add(paper)
    session.flush()
    return paper


def _dft(session: Session, paper: Paper, *, evidence_text: str | None = "Evidence text") -> DFTResult:
    row = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        value=-1.23,
        unit="eV",
        evidence_text=evidence_text,
    )
    session.add(row)
    session.flush()
    return row


def _safe_review(session: Session, paper: Paper, row: DFTResult, *,
                  reviewer_status: str = "verified",
                  target_resolution_status: str = "active") -> ExtractionFieldReview:
    review = ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name="value",
        reviewer_status=reviewer_status,
        target_resolution_status=target_resolution_status,
        evidence_text=row.evidence_text,
    )
    session.add(review)
    session.flush()
    return review


def _evidence_ref(session: Session, paper: Paper, row: DFTResult) -> EvidenceSpan:
    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(row.id),
        text=row.evidence_text or "Evidence text",
    )
    session.add(span)
    session.flush()
    return span


# ---- Test 1: is_safe_verified_review only accepts verified + active/remapped ----

def test_is_safe_verified_review_accepts_verified_active(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, reviewer_status="verified", target_resolution_status="active")
            session.commit()
            assert is_safe_verified_review(review) is True
    finally:
        engine.dispose()


def test_is_safe_verified_review_accepts_verified_remapped(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, reviewer_status="verified", target_resolution_status="remapped")
            session.commit()
            assert is_safe_verified_review(review) is True
    finally:
        engine.dispose()


# ---- Test 2: stale / ambiguous / unresolved / unknown / None are not safe verified ----

def test_is_safe_verified_rejects_stale(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="stale")
            session.commit()
            assert is_safe_verified_review(review) is False
    finally:
        engine.dispose()


def test_is_safe_verified_rejects_ambiguous(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="ambiguous")
            session.commit()
            assert is_safe_verified_review(review) is False
    finally:
        engine.dispose()


def test_is_safe_verified_rejects_unresolved(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="unresolved")
            session.commit()
            assert is_safe_verified_review(review) is False
    finally:
        engine.dispose()


def test_is_safe_verified_rejects_unknown(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="unknown")
            session.commit()
            assert is_safe_verified_review(review) is False
    finally:
        engine.dispose()


def test_is_safe_verified_rejects_none(tmp_path):
    assert is_safe_verified_review(None) is False


# ---- Test 3: verified + stale target is not safe verified ----

def test_verified_plus_stale_target_is_not_safe(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, reviewer_status="verified", target_resolution_status="stale")
            session.commit()
            assert is_safe_verified_review(review) is False
    finally:
        engine.dispose()


# ---- Test 4: AI candidate cannot overwrite human verified ----

def test_ai_candidate_cannot_overwrite_human_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, reviewer_status="verified", target_resolution_status="active")
            review.reviewed_value = -1.23
            run = ExternalAnalysisRun(paper_id=paper.id, source="internal_ai", raw_text="{}")
            session.add_all([review, run])
            session.flush()
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="correction",
                normalized_payload={"field_name": "dft_results", "target_path": f"dft_results:{dft.id}:value", "operation": "replace", "proposed_value": -9.99},
                evidence_payload={"quoted_text": "AI suggestion"},
                status="pending",
            )
            session.add(candidate)
            session.commit()

            # materialize should not overwrite the verified review
            service = ExternalAnalysisService(session, Settings())
            result = service.materialize_candidates(run.id, explicit_all=True)
            session.flush()

            stored = session.get(ExtractionFieldReview, review.id)
            assert stored.reviewer_status == "verified"
            assert stored.reviewed_value == -1.23
    finally:
        engine.dispose()


def test_can_ai_candidate_update_target_blocks_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft)
            session.commit()

            assert can_ai_candidate_update_target(existing_review=review, candidate_source="internal_ai") is False
            assert can_ai_candidate_update_target(existing_review=review, candidate_source="external") is False
            assert can_ai_candidate_update_target(existing_review=review, candidate_source="manual") is True
    finally:
        engine.dispose()


# ---- Test 5: ExternalAnalysisCandidate missing evidence_payload cannot enter trusted path ----

def test_external_candidate_missing_evidence_not_trusted(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            run = ExternalAnalysisRun(paper_id=paper.id, source="external", raw_text="{}")
            session.add(run)
            session.flush()
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="correction",
                normalized_payload={"field_name": "title", "target_path": "title", "operation": "replace", "proposed_value": "X", "reason": "No evidence"},
                evidence_payload=None,
                status="pending",
            )
            session.add(candidate)
            session.commit()

            assert trusted_external_candidate(candidate) is False
    finally:
        engine.dispose()


# ---- Test 6: Import AI candidate does not create reviewer_status=verified ----

def test_import_ai_candidate_does_not_create_verified_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            run = ExternalAnalysisRun(paper_id=paper.id, source="internal_ai", raw_text="{}")
            session.add(run)
            session.flush()
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="correction",
                normalized_payload={"field_name": "title", "target_path": "title", "operation": "replace", "proposed_value": "X", "reason": "AI suggestion"},
                evidence_payload={"quoted_text": "Some text"},
                status="pending",
            )
            session.add(candidate)
            session.commit()

            # materialize should create a PaperCorrection, not an ExtractionFieldReview with verified
            service = ExternalAnalysisService(session, Settings())
            result = service.materialize_candidates(run.id, explicit_all=True)
            session.flush()

            # No ExtractionFieldReview should exist with verified from AI import
            reviews = session.query(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper.id,
                ExtractionFieldReview.reviewer_status == "verified",
            ).all()
            assert len(reviews) == 0
    finally:
        engine.dispose()


def test_catalyst_sample_correction_without_pdf_anchor_stays_unmaterialized(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            sample = CatalystSample(paper_id=paper.id, name="graphene")
            session.add(sample)
            session.flush()

            service = ExternalAnalysisService(session, Settings())
            run = service.import_run(
                paper_id=paper.id,
                source="external",
                source_label="external",
                raw_text=None,
                raw_payload={
                    "correction_proposals": [
                        {
                            "field_name": "catalyst_samples",
                            "target_path": f"catalyst_samples:{sample.id}:name",
                            "operation": "replace",
                            "proposed_value": "single-vacancy graphene",
                            "reason": "Try to refine the material identity.",
                            "evidence_payload": {"evidence_text": "single-vacancy graphene"},
                        }
                    ]
                },
            )
            session.flush()

            candidate = session.query(ExternalAnalysisCandidate).filter_by(run_id=run.id).one()
            assert candidate.status == "requires_resolution"

            result = service.materialize_candidates(run.id, explicit_all=True)
            session.flush()

            assert result.created_corrections == 0
            assert result.skipped_candidates == 1
            assert session.query(PaperCorrection).count() == 0
    finally:
        engine.dispose()


def test_approve_catalyst_sample_correction_requires_pdf_anchor(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            sample = CatalystSample(paper_id=paper.id, name="graphene")
            session.add(sample)
            session.flush()
            correction = PaperCorrection(
                paper_id=paper.id,
                source="external",
                field_name="catalyst_samples",
                target_path=f"catalyst_samples:{sample.id}:name",
                operation="replace",
                proposed_value="single-vacancy graphene",
                reason="Need a finer material identity.",
                evidence_payload={"evidence_text": "single-vacancy graphene"},
                status="pending",
            )
            session.add(correction)
            session.commit()

            with SessionLocal() as verification_session:
                stored = verification_session.query(PaperCorrection).one()
                service = ReviewService(verification_session)
                error_raised = False
                try:
                    service.approve_correction(stored.id, reviewer="reviewer")
                except ValueError as exc:
                    error_raised = True
                    assert "Catalyst sample corrections require" in str(exc)
                assert error_raised, "approve_correction should reject catalyst sample edits without a PDF anchor"
    finally:
        engine.dispose()


# ---- Test 7: Manual verified save requires evidence reference ----

def test_mark_verified_requires_evidence_reference(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper, evidence_text="Some evidence")
            # No evidence reference created
            session.commit()

            from app.schemas.extraction import ExtractionReviewMarkVerifiedRequest
            payload = ExtractionReviewMarkVerifiedRequest(
                target_type="dft_results",
                target_id=str(dft.id),
                field_names=["value"],
                reviewer="manual_tester",
            )

            service = ExtractionReviewService(session)
            error_raised = False
            try:
                service.mark_verified(paper.id, payload)
            except ValueError as exc:
                error_raised = True
                assert "missing_evidence_reference" in str(exc)
            assert error_raised, "mark_verified should have raised ValueError for missing evidence reference"
    finally:
        engine.dispose()


# ---- Test 8: Manual verified save requires evidence_text ----

def test_mark_verified_requires_evidence_text(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper, evidence_text="")  # Empty evidence text
            _evidence_ref(session, paper, dft)
            session.commit()

            from app.schemas.extraction import ExtractionReviewMarkVerifiedRequest
            payload = ExtractionReviewMarkVerifiedRequest(
                target_type="dft_results",
                target_id=str(dft.id),
                field_names=["value"],
                reviewer="manual_tester",
            )

            service = ExtractionReviewService(session)
            error_raised = False
            try:
                service.mark_verified(paper.id, payload)
            except ValueError as exc:
                error_raised = True
                assert "missing_evidence_text" in str(exc)
            assert error_raised, "mark_verified should have raised ValueError for missing evidence text"
    finally:
        engine.dispose()


# ---- Test 9: Export/writing continues to block unsafe review ----

def test_export_blocks_unsafe_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            _safe_review(session, paper, dft, target_resolution_status="stale")
            _evidence_ref(session, paper, dft)
            session.commit()

            from app.utils.review_safety import is_export_eligible_extraction
            gate = is_export_eligible_extraction(session, dft, target_type="dft_results")
            assert gate.eligible is False
            assert "unsafe_review" in gate.reasons
    finally:
        engine.dispose()


def test_writing_blocks_unsafe_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    research_gap="Test gap",
                    evidence_chain=[{"reviewer_status": "verified", "target_resolution_status": "stale", "text": "x"}],
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)
            card = detail.writing_cards_items[0]
            assert card.can_use_for_writing is False
            assert "unsafe_review" in card.blocked_reasons
    finally:
        engine.dispose()


# ---- Test 10: stale/ambiguous/unresolved/unknown cannot be serialized as verified ----

def test_unsafe_resolution_not_serialized_as_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            for status in ("stale", "ambiguous", "unresolved", "unknown"):
                dft = _dft(session, paper)
                _safe_review(session, paper, dft, target_resolution_status=status)
            session.commit()

            reviews = ExtractionReviewService(session).list_reviews(paper.id)
            assert len(reviews) == 4
            for review in reviews:
                assert review.reviewer_status == "verified"
                assert review.verified is False, f"Review with target_resolution_status={review.target_resolution_status} should not be serialized as verified"
    finally:
        engine.dispose()


# ---- Test 11: Remapped target can be safe verified ----

def test_remapped_target_is_safe_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="remapped")
            session.commit()
            assert is_safe_verified_review(review) is True

            # Serialized response should show verified=True
            reviews = ExtractionReviewService(session).list_reviews(paper.id)
            assert len(reviews) == 1
            assert reviews[0].verified is True
            assert reviews[0].target_resolution_status == "remapped"
    finally:
        engine.dispose()


# ---- Test 12: Orphaned/missing target must be blocked ----

def test_orphaned_target_is_blocked(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            # Create review with unresolved target (orphaned)
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id="00000000-0000-0000-0000-000000000000",  # Non-existent
                field_name="value",
                reviewer_status="verified",
                target_resolution_status="unresolved",
                evidence_text="Some evidence",
            )
            session.add(review)
            session.commit()

            assert is_safe_verified_review(review) is False
            assert is_unsafe_review_status(review) is True
    finally:
        engine.dispose()


# ---- Test 13: Review boundary reason is available ----

def test_boundary_reason_available(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft, target_resolution_status="stale")
            session.commit()

            reason = build_review_boundary_reason(review=review)
            assert "target_resolution=stale" in reason
    finally:
        engine.dispose()


def test_serialize_review_gate_for_ai_candidate(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            review = _safe_review(session, paper, dft)
            session.commit()

            gate = serialize_review_gate(review, is_ai_candidate=True)
            assert gate.is_safe_verified is False
            assert "ai_candidate_cannot_be_verified" in gate.blocked_reasons
    finally:
        engine.dispose()


def test_normalize_functions():
    assert normalize_review_status(None) == "missing"
    assert normalize_review_status({"reviewer_status": "verified"}) == "verified"
    assert normalize_review_status({"review_status": "stale"}) == "stale"
    assert normalize_review_status({"status": "unknown"}) == "unknown"

    assert normalize_target_resolution_status(None) == "missing"
    assert normalize_target_resolution_status({"target_resolution_status": "active"}) == "active"
    assert normalize_target_resolution_status({"resolution_status": "remapped"}) == "remapped"


def test_is_unsafe_review_status():
    assert is_unsafe_review_status(None) is True
    assert is_unsafe_review_status({"reviewer_status": "verified", "target_resolution_status": "active"}) is False
    assert is_unsafe_review_status({"reviewer_status": "verified", "target_resolution_status": "stale"}) is True
    assert is_unsafe_review_status({"reviewer_status": "pending", "target_resolution_status": "active"}) is True


def test_save_reviews_rejects_verified_status(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = _dft(session, paper)
            session.commit()

            from app.schemas.extraction import ExtractionFieldReviewSaveItem
            service = ExtractionReviewService(session)

            # Attempt to save with reviewer_status=verified should be rejected
            error_raised = False
            try:
                service.save_reviews(paper.id, [
                    ExtractionFieldReviewSaveItem(
                        target_type="dft_results",
                        target_id=str(dft.id),
                        field_name="value",
                        reviewed_value=-1.23,
                        reviewer_status="verified",
                    )
                ])
            except ValueError as exc:
                error_raised = True
                assert "Cannot set reviewer_status=verified through save" in str(exc)
            assert error_raised, "save_reviews should reject reviewer_status=verified"
    finally:
        engine.dispose()


def test_can_manual_review_mark_verified_checks():
    allowed, reason = can_manual_review_mark_verified(
        target_exists=False,
        evidence_reference_exists=True,
        evidence_text_exists=True,
        target_resolution_status="active",
    )
    assert allowed is False
    assert reason == "target_not_found"

    allowed, reason = can_manual_review_mark_verified(
        target_exists=True,
        evidence_reference_exists=False,
        evidence_text_exists=True,
        target_resolution_status="active",
    )
    assert allowed is False
    assert reason == "missing_evidence_reference"

    allowed, reason = can_manual_review_mark_verified(
        target_exists=True,
        evidence_reference_exists=True,
        evidence_text_exists=False,
        target_resolution_status="active",
    )
    assert allowed is False
    assert reason == "missing_evidence_text"

    allowed, reason = can_manual_review_mark_verified(
        target_exists=True,
        evidence_reference_exists=True,
        evidence_text_exists=True,
        target_resolution_status="active",
    )
    assert allowed is True
    assert reason == ""
