from __future__ import annotations

import asyncio
import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.papers.aggregation import export_dft_results_csv
from app.db.models import Base, DFTResult, EvidenceSpan, ExtractionFieldReview, Paper


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'export_gate.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="Export Gate Paper", pdf_path="paper.pdf", authors=["A"])
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


def _safe_review(session: Session, paper: Paper, row: DFTResult) -> ExtractionFieldReview:
    review = ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name="value",
        reviewer_status="verified",
        target_resolution_status="active",
        evidence_text=row.evidence_text,
    )
    session.add(review)
    session.flush()
    return review


def _evidence_ref(session: Session, paper: Paper, row: DFTResult, *, page: int | None = None) -> EvidenceSpan:
    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(row.id),
        text=row.evidence_text or "Evidence text",
        page=page,
    )
    session.add(span)
    session.flush()
    return span


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
    rows = list(csv.DictReader(io.StringIO(text)))
    return response, rows


def test_dft_export_default_excludes_missing_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _evidence_ref(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-exported-count"] == "0"
            assert response.headers["x-d1-blocked-count"] == "1"
            assert "missing_review" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_default_excludes_unsafe_review_statuses(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        unsafe_statuses = ["unknown", "stale", "ambiguous", "unresolved"]
        with SessionLocal() as session:
            paper = _paper(session)
            for status in unsafe_statuses:
                row = _dft(session, paper)
                _evidence_ref(session, paper, row)
                session.add(
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="dft_results",
                        target_id=str(row.id),
                        field_name="value",
                        reviewer_status="verified" if status != "unknown" else "unknown",
                        target_resolution_status=status if status != "unknown" else "active",
                        evidence_text=row.evidence_text,
                    )
                )
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-blocked-count"] == str(len(unsafe_statuses))
            assert "unsafe_review" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_default_excludes_missing_evidence_reference(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-blocked-count"] == "1"
            assert "missing_evidence" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_allows_safe_verified_with_evidence_text(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert response.headers["x-d1-exported-count"] == "1"
            assert response.headers["x-d1-blocked-count"] == "0"
            assert rows[0]["value"] == "-1.23"
            assert rows[0]["review_status"] == "verified"
            assert rows[0]["review_gate_status"] == "safe_verified"
    finally:
        engine.dispose()


def test_dft_export_does_not_fabricate_page_or_bbox(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            _, rows = _export_rows(session)

            assert rows[0]["provenance_level"] == "text_evidence_only"
            assert rows[0]["locator_status"] == "missing_locator"
            assert "page" not in rows[0]
            assert "bbox" not in rows[0]
    finally:
        engine.dispose()
