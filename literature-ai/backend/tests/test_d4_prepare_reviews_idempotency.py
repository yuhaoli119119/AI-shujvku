from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.extraction import router as extraction_router
from app.db.models import Base, DFTResult, EvidenceLocator, ExtractionFieldReview, Paper, WritingCard
from app.db.session import get_db_session
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
    db_url = f"sqlite:///{tmp_path / 'd4_prepare_reviews_idempotency.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session, title: str = "D4 prepare pilot") -> Paper:
    paper = Paper(
        title=title,
        year=2026,
        journal="Pilot Journal",
        pdf_path="pilot.pdf",
        markdown_path="pilot.md",
        authors=["Reviewer"],
    )
    session.add(paper)
    session.flush()
    return paper


def _dft_result(session: Session, paper: Paper) -> DFTResult:
    row = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        value=-1.23,
        unit="eV",
        reaction_step="adsorption",
        evidence_text="DFT evidence text.",
        confidence=0.9,
    )
    session.add(row)
    session.flush()
    return row


def _exact_locator(session: Session, paper: Paper, row: DFTResult) -> EvidenceLocator:
    locator = EvidenceLocator(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name=None,
        chunk_id=str(row.id),
        source_type="text",
        page=1,
        bbox=EXACT_BBOX,
        evidence_text=row.evidence_text or "DFT evidence text.",
        locator_status="exact_page",
        locator_confidence=0.98,
        parser_source="docling",
    )
    session.add(locator)
    session.flush()
    return locator


def _writing_card(session: Session, paper: Paper) -> WritingCard:
    card = WritingCard(
        paper_id=paper.id,
        research_gap="Pending review gap",
        evidence_chain=[
            {
                "text": "Pending review evidence.",
                "reviewer_status": "pending",
                "target_resolution_status": "active",
                "locator_status": "exact_page",
                "page": 1,
                "bbox": EXACT_BBOX,
                "can_jump_to_pdf_page": True,
            }
        ],
    )
    session.add(card)
    session.flush()
    return card


def _counts(session: Session, paper_id: UUID | None = None) -> dict[str, int]:
    query = select(ExtractionFieldReview)
    if paper_id is not None:
        query = query.where(ExtractionFieldReview.paper_id == paper_id)
    reviews = session.scalars(query).all()
    return {
        "total": len(reviews),
        "pending": sum(1 for row in reviews if row.reviewer_status == "pending"),
        "verified": sum(1 for row in reviews if row.reviewer_status == "verified"),
        "safe_verified": sum(1 for row in reviews if row.reviewer_status == "verified" and row.target_resolution_status in {"active", "remapped"}),
    }


def _client(SessionLocal) -> TestClient:
    app = FastAPI()
    app.include_router(extraction_router, prefix="/api/extraction")

    def override_db():
        with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    return TestClient(app)


def test_first_prepare_creates_pending_unverified_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            session.commit()

            response = ExtractionReviewService(session).prepare_pending_reviews(paper.id)

            assert response.created_count == 4
            assert response.existing_count == 0
            assert response.skipped_count == 0
            assert response.verified_count == 0
            assert response.safe_verified_count == 0
            assert len(response.review_ids) == 4
            assert {item.reviewer_status for item in response.items} == {"pending"}
            assert all(item.verified is False for item in response.items)
            assert any(item.target_id == str(row.id) and item.field_name == "value" for item in response.items)
            assert _counts(session, paper.id) == {"total": 4, "pending": 4, "verified": 0, "safe_verified": 0}
    finally:
        engine.dispose()


def test_second_prepare_is_idempotent_and_does_not_duplicate_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            _dft_result(session, paper)
            session.commit()

            first = ExtractionReviewService(session).prepare_pending_reviews(paper.id)
            first_ids = set(first.review_ids)
            second = ExtractionReviewService(session).prepare_pending_reviews(paper.id)

            assert first.created_count == 4
            assert second.created_count == 0
            assert second.existing_count == 4
            assert second.skipped_count == 0
            assert set(second.review_ids) == first_ids
            assert _counts(session, paper.id) == {"total": 4, "pending": 4, "verified": 0, "safe_verified": 0}
    finally:
        engine.dispose()


def test_prepare_endpoint_ignores_verified_like_payload_and_does_not_create_verified_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            _dft_result(session, paper)
            paper_id = paper.id
            session.commit()

        client = _client(SessionLocal)
        response = client.post(
            f"/api/extraction/results/{paper_id}/reviews/prepare",
            json={"reviewer_status": "verified", "verified": True},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["created_count"] == 4
        assert payload["verified_count"] == 0
        assert payload["safe_verified_count"] == 0
        assert {item["reviewer_status"] for item in payload["items"]} == {"pending"}
        assert all(item["verified"] is False for item in payload["items"])

        with SessionLocal() as session:
            assert _counts(session, paper_id) == {"total": 4, "pending": 4, "verified": 0, "safe_verified": 0}
    finally:
        engine.dispose()


def test_prepare_does_not_change_paper_metadata_or_materialized_facts(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            before_paper = {
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "pdf_path": paper.pdf_path,
                "markdown_path": paper.markdown_path,
            }
            before_dft = {
                "adsorbate": row.adsorbate,
                "property_type": row.property_type,
                "value": row.value,
                "unit": row.unit,
                "reaction_step": row.reaction_step,
                "evidence_text": row.evidence_text,
            }
            session.commit()

            ExtractionReviewService(session).prepare_pending_reviews(paper.id)
            session.refresh(paper)
            session.refresh(row)

            assert {
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "pdf_path": paper.pdf_path,
                "markdown_path": paper.markdown_path,
            } == before_paper
            assert {
                "adsorbate": row.adsorbate,
                "property_type": row.property_type,
                "value": row.value,
                "unit": row.unit,
                "reaction_step": row.reaction_step,
                "evidence_text": row.evidence_text,
            } == before_dft
    finally:
        engine.dispose()


def test_prepare_does_not_unlock_dft_export_or_writing_evidence_pack(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft_result(session, paper)
            _exact_locator(session, paper, row)
            _writing_card(session, paper)
            session.commit()

            before_export = is_export_eligible_extraction(session, row, target_type="dft_results")
            before_card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            response = ExtractionReviewService(session).prepare_pending_reviews(paper.id)

            after_export = is_export_eligible_extraction(session, row, target_type="dft_results")
            after_card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            assert response.verified_count == 0
            assert response.safe_verified_count == 0
            assert before_export.eligible is False
            assert after_export.eligible is False
            assert "unsafe_review" in after_export.reasons
            assert before_card.can_use_for_writing is False
            assert after_card.can_use_for_writing is False
            assert "unsafe_review" in after_card.blocked_reasons
    finally:
        engine.dispose()


def test_prepare_does_not_affect_unrelated_paper_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            pilot = _paper(session, title="Pilot")
            other = _paper(session, title="Other")
            _dft_result(session, pilot)
            _dft_result(session, other)
            session.commit()

            response = ExtractionReviewService(session).prepare_pending_reviews(pilot.id)

            assert response.created_count == 4
            assert _counts(session, pilot.id) == {"total": 4, "pending": 4, "verified": 0, "safe_verified": 0}
            assert _counts(session, other.id) == {"total": 0, "pending": 0, "verified": 0, "safe_verified": 0}
            assert _counts(session)["total"] == 4
    finally:
        engine.dispose()
