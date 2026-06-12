from __future__ import annotations

import asyncio
import csv
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.papers.aggregation import export_dft_results_csv
from app.config import Settings
from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    Paper,
    PaperCorrection,
    PaperNote,
    PaperRelationship,
    WritingCard,
)
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.extraction_review_service import ExtractionReviewService
from app.services.paper_query import PaperQueryService


def _session(tmp_path, name: str = "d3_review_lock.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session, title: str = "D3 Review Lock Paper") -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf", authors=[])
    session.add(paper)
    session.flush()
    return paper


def _dft(session: Session, paper: Paper, *, evidence_text: str | None = "Reviewed evidence.") -> DFTResult:
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


def _catalyst_sample(session: Session, paper: Paper) -> CatalystSample:
    sample = CatalystSample(
        paper_id=paper.id,
        name="Fe-N4 catalyst",
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        support="N-doped carbon",
    )
    session.add(sample)
    session.flush()
    return sample


def _evidence_ref(session: Session, paper: Paper, row: DFTResult) -> EvidenceSpan:
    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(row.id),
        text=row.evidence_text or "Reviewed evidence.",
        page=1,
    )
    session.add(span)
    session.flush()
    return span


def _verified_review(session: Session, paper: Paper, row: DFTResult) -> ExtractionFieldReview:
    review = ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name="value",
        original_value=row.value,
        reviewed_value=row.value,
        unit=row.unit,
        evidence_text=row.evidence_text,
        reviewer_status="verified",
        target_resolution_status="active",
    )
    session.add(review)
    session.flush()
    return review


async def _response_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8-sig")


def _export_rows(session: Session):
    response = asyncio.run(
        export_dft_results_csv(
            property_type=None,
            adsorbate=None,
            year_min=None,
            year_max=None,
            session=session,
        )
    )
    text = asyncio.run(_response_text(response))
    return response, list(csv.DictReader(io.StringIO(text)))


def test_external_ai_materialize_never_creates_verified_review_even_with_verified_like_payload(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            support = _paper(session, "D3 Support Paper")
            run = ExternalAnalysisRun(paper_id=paper.id, source="external", raw_text="{}")
            session.add(run)
            session.flush()
            candidates = [
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="note",
                    normalized_payload={
                        "content": "Verified-looking note must remain a pending note.",
                        "field_name": "abstract",
                        "reviewer_status": "verified",
                    },
                    evidence_payload={"quoted_text": "note evidence"},
                    status="pending",
                ),
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="correction",
                    normalized_payload={
                        "field_name": "title",
                        "target_path": "title",
                        "operation": "replace",
                        "proposed_value": "Verified-looking title",
                        "reason": "External suggestion only.",
                        "reviewer_status": "verified",
                        "status": "verified",
                    },
                    evidence_payload={"quoted_text": "correction evidence"},
                    status="pending",
                ),
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="relationship",
                    normalized_payload={
                        "relationship_type": "supports",
                        "target_paper_id": str(support.id),
                        "note": "Verified-looking relationship remains a relationship.",
                        "reviewer_status": "verified",
                    },
                    evidence_payload={"quoted_text": "relationship evidence"},
                    status="pending",
                ),
            ]
            session.add_all(candidates)
            session.commit()

            result = ExternalAnalysisService(session, Settings()).materialize_candidates(
                run.id,
                explicit_all=True,
                created_by="external_ai",
            )
            session.flush()

            assert result.created_notes == 1
            assert result.created_corrections == 1
            assert result.created_relationships == 1
            assert session.query(ExtractionFieldReview).filter_by(reviewer_status="verified").count() == 0
            assert session.query(PaperNote).count() == 1
            correction = session.query(PaperCorrection).one()
            assert correction.status == "pending"
            assert session.query(PaperRelationship).count() == 1
    finally:
        engine.dispose()


def test_save_reviews_rejects_verified_status_and_does_not_mutate_existing_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _evidence_ref(session, paper, row)
            service = ExtractionReviewService(session)
            service.mark_verified(
                paper.id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_names=["value"],
                    reviewer="manual_reviewer",
                    reviewer_note="Human verified against evidence.",
                ),
            )

            with pytest.raises(ValueError, match="Verified reviews cannot be downgraded through save"):
                service.save_reviews(
                    paper.id,
                    [
                        ExtractionFieldReviewSaveItem(
                            target_type="dft_results",
                            target_id=str(row.id),
                            field_name="value",
                            reviewed_value=-9.99,
                            reviewer_status="corrected",
                            reviewer="ai_candidate",
                            reviewer_note="Attempted overwrite.",
                        )
                    ],
                )

            stored = session.query(ExtractionFieldReview).filter_by(target_id=str(row.id), field_name="value").one()
            assert stored.reviewer_status == "verified"
            assert stored.reviewed_value == -1.23
            assert stored.reviewer == "manual_reviewer"
            assert stored.reviewer_note == "Human verified against evidence."
    finally:
        engine.dispose()


def test_serialized_unsafe_review_payload_cannot_unlock_writing(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            for status in ("stale", "ambiguous", "unresolved", "unknown"):
                session.add(
                    WritingCard(
                        paper_id=paper.id,
                        research_gap=f"{status} gap",
                        evidence_chain=[
                            {
                                "text": "Serialized review claims verification.",
                                "reviewer_status": "verified",
                                "target_resolution_status": status,
                            }
                        ],
                    )
                )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert len(detail.writing_cards_items) == 4
            for card in detail.writing_cards_items:
                assert card.can_use_for_writing is False
                assert card.review_gate_status == "blocked"
                assert "unsafe_review" in card.blocked_reasons
    finally:
        engine.dispose()


def test_export_headers_and_block_counts_are_stable_for_mixed_safe_unsafe_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            sample = _catalyst_sample(session, paper)
            safe = _dft(session, paper)
            safe.catalyst_sample_id = sample.id
            _verified_review(session, paper, safe)
            _evidence_ref(session, paper, safe)

            unsafe = _dft(session, paper)
            _evidence_ref(session, paper, unsafe)
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(unsafe.id),
                    field_name="value",
                    reviewer_status="verified",
                    target_resolution_status="stale",
                    evidence_text=unsafe.evidence_text,
                )
            )

            missing_evidence = _dft(session, paper)
            _verified_review(session, paper, missing_evidence)
            session.commit()

            response, rows = _export_rows(session)

            assert response.headers["x-d3-export-safety-gate"] == "safe_verified_with_required_evidence"
            assert response.headers["x-d3-export-count"] == "1"
            assert response.headers["x-d3-block-count"] == "2"
            assert len(rows) == 1
            assert rows[0]["review_status"] == "verified"
            assert rows[0]["review_gate_status"] == "safe_verified"
            assert rows[0]["value"] == "-1.23"
            assert response.headers["x-d1-exported-count"] == "1"
            assert response.headers["x-d1-blocked-count"] == "2"
            assert "unsafe_review" in response.headers["x-d1-blocked-reasons"]
            assert "missing_evidence" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()
