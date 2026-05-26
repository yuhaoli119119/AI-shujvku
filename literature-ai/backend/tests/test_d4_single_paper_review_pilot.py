from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.extraction import prepare_extraction_field_reviews
from app.db.models import Base, DFTResult, EvidenceLocator, EvidenceSpan, ExtractionFieldReview, Paper, WritingCard
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.extraction_review_service import ExtractionReviewService
from app.services.paper_query import PaperQueryService
from app.utils.review_safety import is_export_eligible_extraction


EXACT_BBOX = {
    "x0": 10,
    "y0": 20,
    "x1": 120,
    "y1": 220,
    "width": 600,
    "height": 800,
    "coordinate_system": "pdf_points",
}


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'd4_single_paper_review_pilot.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(
        title="D4 pilot paper",
        year=2026,
        journal="Pilot Journal",
        pdf_path="pilot.pdf",
        markdown_path="pilot.md",
        authors=["Reviewer"],
    )
    session.add(paper)
    session.flush()
    return paper


def _dft_result(session: Session, paper: Paper, *, evidence_text: str = "DFT evidence text.") -> DFTResult:
    row = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        value=-1.23,
        unit="eV",
        reaction_step="adsorption",
        evidence_text=evidence_text,
        confidence=0.9,
    )
    session.add(row)
    session.flush()
    return row


def _locator(
    session: Session,
    paper: Paper,
    row: DFTResult,
    *,
    page: int | None,
    locator_status: str,
    bbox: dict | None = EXACT_BBOX,
    can_be_exact: bool = True,
) -> EvidenceLocator:
    locator = EvidenceLocator(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name=None,
        chunk_id=str(row.id),
        source_type="text",
        page=page,
        bbox=bbox,
        evidence_text=row.evidence_text or "DFT evidence text.",
        locator_status=locator_status,
        locator_confidence=0.98 if can_be_exact else 0.6,
        parser_source="docling",
        warning_reason=None if page else "page missing from parser output",
    )
    session.add(locator)
    session.flush()
    return locator


def _span(session: Session, paper: Paper, row: DFTResult, *, page: int | None) -> EvidenceSpan:
    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(row.id),
        text=row.evidence_text or "DFT evidence text.",
        page=page,
    )
    session.add(span)
    session.flush()
    return span


def _review_like_writing_card(session: Session, paper: Paper, *, locator_status: str, page: int | None) -> WritingCard:
    card = WritingCard(
        paper_id=paper.id,
        research_gap="Pilot gap",
        evidence_chain=[
            {
                "text": "Review-like evidence.",
                "reviewer_status": "verified",
                "target_resolution_status": "active",
                "locator_status": locator_status,
                "page": page,
                "bbox": EXACT_BBOX if page else {"x0": 10, "y0": 20, "x1": 120, "y1": 220},
                "can_jump_to_pdf_page": bool(page),
            }
        ],
    )
    session.add(card)
    session.flush()
    return card


def test_d4_extraction_candidate_can_be_prepared_as_pending_unverified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            before = {
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "pdf_path": paper.pdf_path,
                "markdown_path": paper.markdown_path,
            }
            session.commit()

            prepared = ExtractionReviewService(session).prepare_pending_reviews(paper.id)
            session.refresh(paper)

            value_review = next(item for item in prepared if item.target_id == str(row.id) and item.field_name == "value")
            assert value_review.reviewer_status == "pending"
            assert value_review.verified is False
            assert value_review.original_value == -1.23
            assert value_review.reviewed_value is None
            assert value_review.evidence_text == "DFT evidence text."
            assert {
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "pdf_path": paper.pdf_path,
                "markdown_path": paper.markdown_path,
            } == before
    finally:
        engine.dispose()


def test_d4_prepare_review_endpoint_is_pending_only(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            session.commit()

            prepared = asyncio.run(prepare_extraction_field_reviews(paper.id, session=session))

            assert prepared
            assert any(item.target_id == str(row.id) and item.field_name == "value" for item in prepared)
            assert {item.reviewer_status for item in prepared} == {"pending"}
            assert all(item.verified is False for item in prepared)
    finally:
        engine.dispose()


def test_d4_prepared_candidate_and_save_reviews_cannot_set_verified(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            service = ExtractionReviewService(session)
            service.prepare_pending_reviews(paper.id)

            with pytest.raises(ValueError, match="Cannot set reviewer_status=verified"):
                service.save_reviews(
                    paper.id,
                    [
                        ExtractionFieldReviewSaveItem(
                            target_type="dft_results",
                            target_id=str(row.id),
                            field_name="value",
                            reviewed_value=-1.23,
                            unit="eV",
                            evidence_text=row.evidence_text,
                            reviewer_status="verified",
                        )
                    ],
                )

            stored = session.scalar(
                select(ExtractionFieldReview).where(
                    ExtractionFieldReview.paper_id == paper.id,
                    ExtractionFieldReview.target_id == str(row.id),
                    ExtractionFieldReview.field_name == "value",
                )
            )
            assert stored.reviewer_status == "pending"
    finally:
        engine.dispose()


def test_d4_mark_verified_is_the_only_verified_path(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            _locator(session, paper, row, page=1, locator_status="exact_page")
            service = ExtractionReviewService(session)
            service.prepare_pending_reviews(paper.id)

            corrected = service.save_reviews(
                paper.id,
                [
                    ExtractionFieldReviewSaveItem(
                        target_type="dft_results",
                        target_id=str(row.id),
                        field_name="value",
                        reviewed_value=-1.23,
                        unit="eV",
                        evidence_text=row.evidence_text,
                        reviewer_status="corrected",
                    )
                ],
            )
            assert corrected[0].verified is False

            marked = service.mark_verified(
                paper.id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_names=["value"],
                    reviewer="human_reviewer",
                ),
            )

            assert marked[0].reviewer_status == "verified"
            assert marked[0].verified is True
    finally:
        engine.dispose()


def test_d4_missing_exact_locator_blocks_export_and_writing_even_with_review_like_data(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            _span(session, paper, row, page=None)
            _review_like_writing_card(session, paper, locator_status="missing_page", page=None)
            service = ExtractionReviewService(session)

            service.mark_verified(
                paper.id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_names=["value"],
                    reviewer="human_reviewer",
                ),
            )

            export_gate = is_export_eligible_extraction(session, row, target_type="dft_results")
            card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            assert export_gate.eligible is False
            assert "unsafe_locator" in export_gate.reasons
            assert card.can_use_for_writing is False
            assert "unsafe_locator" in card.blocked_reasons
    finally:
        engine.dispose()


def test_d4_exact_locator_and_human_verified_are_required_for_export_and_writing(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            _locator(session, paper, row, page=1, locator_status="exact_page")
            _review_like_writing_card(session, paper, locator_status="exact_page", page=1)
            service = ExtractionReviewService(session)
            service.prepare_pending_reviews(paper.id)

            pending_gate = is_export_eligible_extraction(session, row, target_type="dft_results")
            assert pending_gate.eligible is False
            assert "unsafe_review" in pending_gate.reasons

            service.mark_verified(
                paper.id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_names=["value"],
                    reviewer="human_reviewer",
                ),
            )

            export_gate = is_export_eligible_extraction(session, row, target_type="dft_results")
            card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            assert export_gate.eligible is True
            assert export_gate.review_gate_status == "safe_verified"
            assert export_gate.provenance_level == "exact_pdf_page"
            assert card.can_use_for_writing is True
    finally:
        engine.dispose()

