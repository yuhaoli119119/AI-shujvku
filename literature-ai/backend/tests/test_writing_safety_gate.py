from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Base,
    DFTResult,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    WritingCard,
)
from app.rag.retriever import Retriever
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.extraction_review_service import ExtractionReviewService
from app.services.paper_query import PaperQueryService
from app.utils.review_safety import trusted_external_candidate


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'writing_gate.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="Writing Gate Paper", pdf_path="paper.pdf", authors=[])
    session.add(paper)
    session.flush()
    return paper


def _safe_evidence_chain() -> list[dict[str, str]]:
    return [
        {
            "text": "Reviewed evidence.",
            "source": "Results",
            "reviewer_status": "verified",
            "target_resolution_status": "active",
        }
    ]


def test_writing_card_missing_evidence_chain_is_blocked(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            session.add(WritingCard(paper_id=paper.id, research_gap="Gap", evidence_chain=None))
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            card = detail.writing_cards_items[0]
            assert card.can_use_for_writing is False
            assert card.evidence_chain_status == "missing"
            assert card.review_gate_status == "blocked"
            assert "missing_evidence_chain" in card.blocked_reasons
    finally:
        engine.dispose()


def test_writing_card_with_unsafe_review_chain_is_blocked(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    research_gap="Gap",
                    evidence_chain=[
                        {
                            "text": "Old evidence.",
                            "source": "Results",
                            "reviewer_status": "verified",
                            "target_resolution_status": "stale",
                        }
                    ],
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            card = detail.writing_cards_items[0]
            assert card.can_use_for_writing is False
            assert card.evidence_chain_status == "present"
            assert card.review_gate_status == "blocked"
            assert "unsafe_review" in card.blocked_reasons
    finally:
        engine.dispose()


def test_retriever_keeps_unsafe_writing_card_out_of_writing_path(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    research_gap="unsafe gap",
                    proposed_solution="unsafe solution",
                    evidence_chain=[{"text": "Evidence", "reviewer_status": "unknown"}],
                )
            )
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    research_gap="safe gap",
                    proposed_solution="safe solution",
                    evidence_chain=_safe_evidence_chain(),
                )
            )
            session.commit()

            retrieved = Retriever(session).retrieve("safe unsafe gap solution", [paper.id], 5)

            assert len(retrieved["writing_cards"]) == 1
            assert retrieved["writing_cards"][0]["research_gap"] == "safe gap"
            assert retrieved["writing_cards"][0]["can_use_for_writing"] is True
    finally:
        engine.dispose()


def test_external_candidate_missing_evidence_payload_is_not_trusted(tmp_path):
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
                normalized_payload={
                    "field_name": "title",
                    "target_path": "title",
                    "operation": "replace",
                    "proposed_value": "New title",
                    "reason": "No evidence.",
                },
                evidence_payload=None,
                status="pending",
            )
            session.add(candidate)
            session.commit()

            assert trusted_external_candidate(candidate) is False
    finally:
        engine.dispose()


def test_ai_candidate_does_not_overwrite_manual_verified_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                evidence_text="Manual evidence.",
            )
            session.add(dft)
            session.flush()
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(dft.id),
                field_name="value",
                original_value=-1.23,
                reviewed_value=-1.23,
                reviewer_status="verified",
                target_resolution_status="active",
                evidence_text="Manual evidence.",
            )
            run = ExternalAnalysisRun(paper_id=paper.id, source="external", raw_text="{}")
            session.add_all([review, run])
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="correction",
                    normalized_payload={
                        "field_name": "dft_results",
                        "target_path": f"dft_results:{dft.id}:value",
                        "operation": "replace",
                        "proposed_value": -9.99,
                        "reason": "External suggestion.",
                        "evidence_payload": {"quoted_text": "External evidence."},
                    },
                    evidence_payload={"quoted_text": "External evidence."},
                    status="pending",
                )
            )
            session.commit()

            result = ExternalAnalysisService(session, Settings()).materialize_candidates(run.id, explicit_all=True)
            session.flush()

            stored = session.get(ExtractionFieldReview, review.id)
            assert result.created_corrections == 1
            assert stored.reviewer_status == "verified"
            assert stored.reviewed_value == -1.23
    finally:
        engine.dispose()


def test_unsafe_review_resolution_is_not_serialized_as_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            dft = DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                evidence_text="Evidence.",
            )
            session.add(dft)
            session.flush()
            session.add_all(
                [
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="dft_results",
                        target_id=str(dft.id),
                        field_name=status,
                        reviewer_status="verified",
                        target_resolution_status=status,
                        evidence_text="Evidence.",
                    )
                    for status in ("stale", "ambiguous", "unresolved", "unknown")
                ]
            )
            session.commit()

            reviews = ExtractionReviewService(session).list_reviews(paper.id)

            assert reviews
            assert all(item.reviewer_status == "verified" for item in reviews)
            assert all(item.verified is False for item in reviews)
    finally:
        engine.dispose()


def test_unreviewed_extraction_fact_is_not_retrieved_for_writing(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            unsafe = MechanismClaim(
                paper_id=paper.id,
                claim_type="unsafe",
                claim_text="Unsafe claim should not enter writing.",
                evidence_text="Unsafe evidence.",
            )
            safe = MechanismClaim(
                paper_id=paper.id,
                claim_type="safe",
                claim_text="Safe claim can enter writing.",
                evidence_text="Safe evidence.",
            )
            session.add_all([unsafe, safe])
            session.flush()
            session.add_all(
                [
                    EvidenceSpan(
                        paper_id=paper.id,
                        object_type="mechanism_claims",
                        object_id=str(safe.id),
                        text="Safe evidence.",
                    ),
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="mechanism_claims",
                        target_id=str(safe.id),
                        field_name="claim_text",
                        reviewer_status="verified",
                        target_resolution_status="active",
                        evidence_text="Safe evidence.",
                    ),
                ]
            )
            session.commit()

            retrieved = Retriever(session).retrieve("claim writing evidence", [paper.id], 5)

            assert len(retrieved["mechanism_claims"]) == 1
            assert retrieved["mechanism_claims"][0]["claim_type"] == "safe"
    finally:
        engine.dispose()
