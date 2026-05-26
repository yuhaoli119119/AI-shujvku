from __future__ import annotations

import asyncio
import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.papers.aggregation import export_dft_results_csv
from app.db.models import Base, DFTResult, EvidenceLocator, EvidenceSpan, ExtractionFieldReview, Paper, WritingCard
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.paper_query import PaperQueryService
from app.utils.locator_degradation import locator_degradation
from app.utils.review_safety import is_export_eligible_extraction


PRECISE_BBOX = {
    "x0": 10,
    "y0": 20,
    "x1": 110,
    "y1": 220,
    "width": 600,
    "height": 800,
    "coordinate_system": "pdf_points",
}


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'd4_locator_shape_lock.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="D4 Locator Shape Paper", pdf_path="paper.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    return paper


def _dft(session: Session, paper: Paper) -> DFTResult:
    row = DFTResult(
        paper_id=paper.id,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        value=-1.23,
        unit="eV",
        evidence_text="DFT evidence text.",
    )
    session.add(row)
    session.flush()
    return row


def _safe_review(session: Session, paper: Paper, row: DFTResult) -> None:
    session.add(
        ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            reviewer_status="verified",
            target_resolution_status="active",
            evidence_text=row.evidence_text,
        )
    )
    session.flush()


def _locator(
    session: Session,
    paper: Paper,
    row: DFTResult,
    *,
    page: int | None,
    locator_status: str,
    bbox=PRECISE_BBOX,
) -> EvidenceLocator:
    locator = EvidenceLocator(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        source_type="text",
        page=page,
        bbox=bbox,
        evidence_text=row.evidence_text or "DFT evidence text.",
        locator_status=locator_status,
        locator_confidence=0.6,
        parser_source="docling",
        warning_reason=None if page else "page missing from parser output",
    )
    session.add(locator)
    session.flush()
    return locator


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


def test_d4_text_only_no_page_bbox_degrades_to_text_only_missing_page():
    degradation = locator_degradation(
        page=None,
        locator_status="text_only",
        evidence_text="Evidence text.",
        bbox=PRECISE_BBOX,
    )

    assert degradation.locator_status == "text_only"
    assert degradation.provenance_level == "text_evidence_only"
    assert degradation.can_jump_to_pdf_page is False
    assert degradation.can_highlight_in_pdf is False
    assert "page missing" in (degradation.warning_reason or "")


def test_d4_no_page_means_no_pdf_jump_even_if_bbox_exists(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _locator(session, paper, row, page=None, locator_status="text_only")
            session.commit()

            locators = EvidenceLocatorService(session).list_locators_for_paper(paper.id)

            assert len(locators) == 1
            assert locators[0].page is None
            assert locators[0].bbox is not None
            assert locators[0].can_jump_to_pdf_page is False
    finally:
        engine.dispose()


def test_d4_text_only_locator_does_not_expose_usable_highlight_bbox(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _locator(session, paper, row, page=None, locator_status="text_only")
            session.commit()

            locator = EvidenceLocatorService(session).list_locators_for_paper(paper.id)[0]

            assert locator.locator_status == "text_only"
            assert locator.bbox is not None
            assert locator.can_highlight_in_pdf is False
            assert locator.can_jump_to_pdf_page is False
    finally:
        engine.dispose()


def test_d4_text_only_missing_page_evidence_does_not_unlock_writing_pack(tmp_path):
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
                            "text": "Evidence text.",
                            "reviewer_status": "verified",
                            "target_resolution_status": "active",
                            "locator_status": "text_only",
                            "page": None,
                            "bbox": PRECISE_BBOX,
                        }
                    ],
                )
            )
            session.commit()

            card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            assert card.can_use_for_writing is False
            assert card.review_gate_status == "blocked"
            assert "unsafe_locator" in card.blocked_reasons
    finally:
        engine.dispose()


def test_d4_text_only_missing_page_evidence_does_not_unlock_dft_export(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _locator(session, paper, row, page=None, locator_status="text_only")
            session.commit()

            gate = is_export_eligible_extraction(session, row, target_type="dft_results")
            response, rows = _export_rows(session)

            assert gate.eligible is False
            assert gate.provenance_level == "text_evidence_only"
            assert gate.locator_status == "text_only"
            assert "unsafe_locator" in gate.reasons
            assert rows == []
            assert "unsafe_locator" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_d4_verified_like_payload_cannot_bypass_safe_locator_gate(tmp_path):
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
                            "text": "Looks reviewed, but locator is not precise.",
                            "reviewer_status": "verified",
                            "target_resolution_status": "active",
                            "evidence_locator": {
                                "locator_status": "missing_page",
                                "page": None,
                                "bbox": PRECISE_BBOX,
                                "can_jump_to_pdf_page": False,
                            },
                        }
                    ],
                )
            )
            session.commit()

            card = PaperQueryService(session).get_paper_detail(paper.id).writing_cards_items[0]

            assert card.can_use_for_writing is False
            assert "unsafe_locator" in card.blocked_reasons
    finally:
        engine.dispose()


def test_d4_api_serialization_marks_bbox_unusable_when_page_missing(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _locator(session, paper, row, page=None, locator_status="text_only")
            session.commit()

            payload = EvidenceLocatorService(session).list_locators_for_paper(paper.id)[0].model_dump(mode="json")

            assert payload["bbox"] is not None
            assert payload["page"] is None
            assert payload["locator_status"] == "text_only"
            assert payload["provenance_level"] == "text_evidence_only"
            assert payload["can_jump_to_pdf_page"] is False
            assert payload["can_highlight_in_pdf"] is False
    finally:
        engine.dispose()

