from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Base, DFTResult, EvidenceLocator, EvidenceSpan, ExtractionFieldReview, Paper, WritingCard
from app.rag.retriever import Retriever
from app.services.extraction_pipeline import ExtractionPipelineService
from app.utils import active_database
from app.utils.review_safety import is_export_eligible_extraction, writing_card_gate


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'd4_real_extraction_lock.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="D4 Real Extraction Lock Paper", pdf_path="paper.pdf", authors=[])
    session.add(paper)
    session.flush()
    return paper


def test_extraction_evidence_without_page_or_bbox_remains_text_only(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            service = ExtractionPipelineService(session=session, settings=Settings(storage_root=tmp_path / "storage"))

            service._persist_evidence_span(
                paper_id=paper.id,
                object_type="dft_result",
                object_id="result-without-locator",
                item={
                    "evidence_text": "Text evidence exists, but parser output did not preserve page or bbox.",
                    "source_location": {"section": "Results"},
                    "confidence": 0.61,
                },
            )
            session.commit()

            span = session.scalars(select(EvidenceSpan)).one()
            locator = session.scalars(select(EvidenceLocator)).one()
            serialized = service.locators.list_locators_for_paper(paper.id)[0]

            assert span.page is None
            assert locator.page is None
            assert locator.bbox is None
            assert serialized.page is None
            assert serialized.bbox is None
            assert serialized.locator_status == "text_only"
            assert serialized.provenance_level == "text_evidence_only"
            assert serialized.can_jump_to_pdf_page is False
            assert serialized.can_highlight_in_pdf is False
    finally:
        engine.dispose()


def test_extraction_output_does_not_create_verified_review_or_unlock_export(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            service = ExtractionPipelineService(session=session, settings=Settings(storage_root=tmp_path / "storage"))

            service._persist_dft_results(
                paper.id,
                [
                    {
                        "category": "adsorption_energy",
                        "adsorbate": "Li2S4",
                        "value": -1.23,
                        "unit": "eV",
                        "reaction_step": "adsorption",
                        "evidence_text": "The adsorption energy of Li2S4 is -1.23 eV.",
                        "source_location": {"section": "Results"},
                        "confidence": 0.82,
                    }
                ],
            )
            session.commit()

            row = session.scalars(select(DFTResult)).one()
            reviews = session.scalars(select(ExtractionFieldReview)).all()
            gate = is_export_eligible_extraction(session, row, target_type="dft_results")

            assert reviews == []
            assert gate.eligible is False
            assert gate.review_gate_status == "blocked"
            assert "missing_review" in gate.reasons
    finally:
        engine.dispose()


def test_text_only_evidence_with_unsafe_review_cannot_enter_writing_or_export(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = DFTResult(
                paper_id=paper.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                evidence_text="The adsorption energy of Li2S4 is -1.23 eV.",
            )
            session.add(row)
            session.flush()
            session.add_all(
                [
                    EvidenceSpan(
                        paper_id=paper.id,
                        object_type="dft_results",
                        object_id=str(row.id),
                        text=row.evidence_text,
                        page=None,
                    ),
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="dft_results",
                        target_id=str(row.id),
                        field_name="value",
                        reviewer_status="verified",
                        target_resolution_status="stale",
                        evidence_text=row.evidence_text,
                    ),
                ]
            )
            session.commit()

            gate = is_export_eligible_extraction(session, row, target_type="dft_results")
            retrieved = Retriever(session).retrieve("Li2S4 adsorption energy", [paper.id], 5)

            assert gate.eligible is False
            assert gate.provenance_level == "text_evidence_only"
            assert gate.locator_status == "missing_page"
            assert "unsafe_review" in gate.reasons
            assert retrieved["dft_results"] == []
    finally:
        engine.dispose()


def test_text_only_writing_evidence_without_safe_review_payload_is_blocked(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            card = WritingCard(
                paper_id=paper.id,
                research_gap="Evidence exists but lacks a safe review payload.",
                evidence_chain=[
                    {
                        "text": "This text-only evidence has no page, bbox, or review status.",
                        "source": "Results",
                    }
                ],
            )
            session.add(card)
            session.commit()

            gate = writing_card_gate(card)

            assert gate.can_use_for_writing is False
            assert gate.evidence_chain_status == "present"
            assert gate.review_gate_status == "blocked"
            assert gate.blocked_reasons == ("missing_review",)
    finally:
        engine.dispose()


def test_active_database_info_ignores_temp_sqlite_when_configured_database_is_postgresql(tmp_path, monkeypatch):
    registry_path = tmp_path / "library.json"
    active_root = tmp_path / "libraries" / "default"
    active_root.mkdir(parents=True)
    db_path = active_root / "database.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE papers (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO papers (id) VALUES ('paper-1')")
        connection.commit()
    finally:
        connection.close()

    registry_path.write_text(
        json.dumps(
            {
                "active_library": "default",
                "libraries": [{"name": "default", "root_path": str(active_root)}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(active_database, "canonical_registry_path", lambda: registry_path)
    monkeypatch.setattr(active_database, "WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(active_database, "BACKEND_ROOT", tmp_path / "backend")

    import app.config as config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: Settings(
            database_url="postgresql+psycopg://example:example@localhost:5432/example",
            storage_root=active_root / "storage",
        ),
    )

    info = active_database.get_active_database_info()

    assert info["configured_db_kind"] == "postgresql"
    assert info["active_library_db_path"] is None
    assert info["active_library_root"] == str(active_root.resolve())
    assert info["effective_db_path"] is None
    assert info["effective_db_papers_total"] == 0
    assert info["matches_active_library_db_path"] is False
