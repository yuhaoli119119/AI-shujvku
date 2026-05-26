from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Base, DFTResult, EvidenceSpan, ExternalAnalysisRun, Paper
from app.utils.active_database import require_active_library_sqlite
from scripts.audit_ai_workflow_boundary import build_audit, run_e2e_rollback, run_real_extraction_sample_rollback


@pytest.fixture
def active_sqlite_db(tmp_path, monkeypatch):
    db_path = tmp_path / "database.sqlite"
    monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    try:
        yield engine, db_path
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_active_database_helper_proves_sqlite_source_of_truth(active_sqlite_db):
    _, db_path = active_sqlite_db

    info = require_active_library_sqlite()

    assert info["db_kind"] == "sqlite"
    assert Path(info["db_path"]) == db_path.resolve()
    assert info["is_active_library_sqlite"] is True


def test_d2_e2e_rollback_keeps_ai_to_verified_boundary(active_sqlite_db):
    engine, _ = active_sqlite_db
    with Session(engine, future=True) as session:
        paper = Paper(title="D2 E2E Paper", pdf_path="paper.pdf", authors=[])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            evidence_text="The adsorption energy is -1.23 eV.",
        )
        session.add(row)
        run = ExternalAnalysisRun(paper_id=paper.id, source="internal_ai", source_label="AI candidate", raw_text="{}")
        session.add(run)
        session.flush()
        session.add(
            EvidenceSpan(
                paper_id=paper.id,
                object_type="dft_results",
                object_id=str(row.id),
                text=row.evidence_text,
                page=None,
            )
        )
        session.commit()

        report = build_audit(session)
        assert report["external_analysis_runs"] == 1
        assert report["dft_export_safe_eligible"] == 0

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, future=True) as session:
                result = run_e2e_rollback(session)
        finally:
            transaction.rollback()

    with Session(engine, future=True) as session:
        assert result["status"] == "passed"
        assert result["corrected_review_status_after_save"] == "corrected"
        assert result["mark_verified_status"] == "verified"
        assert result["mark_verified_safe_flag"] is True
        assert result["export_gate_safe_verified"] is False
        assert "unsafe_locator" in result["export_gate_reasons"]
        assert result["unsafe_data_blocked"] is True

        post_rollback = build_audit(session)
        assert post_rollback["review_status_counts"] == {}
        assert post_rollback["dft_export_safe_eligible"] == 0


def test_d2_e2e_seed_if_needed_is_identified_and_rolled_back(active_sqlite_db):
    engine, _ = active_sqlite_db
    with Session(engine, future=True) as session:
        assert build_audit(session)["dft_export_total_candidates"] == 0

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, future=True) as session:
                result = run_e2e_rollback(session, seed_if_needed=True)
        finally:
            transaction.rollback()

    assert result["status"] == "passed"
    assert result["seed_created"] is True
    assert result["seed_cleaned_by_rollback"] is True
    assert result["seed_marker"] == "D2_E2E_TEST"
    assert result["seed_page"] is None
    assert result["seed_bbox"] is None
    assert result["safe_eligible_count"] == 0
    assert result["blocked_count"] == 2
    assert "unsafe_locator" in result["export_gate_reasons"]
    assert "missing_review" in result["unsafe_gate_reasons"]
    assert "missing_evidence" in result["unsafe_gate_reasons"]
    assert result["writing_cards_safe_usable"] == 0

    with Session(engine, future=True) as session:
        titles = [paper.title for paper in session.query(Paper).all()]
        assert not any("D2_E2E_TEST" in (title or "") for title in titles)
        assert build_audit(session)["dft_export_total_candidates"] == 0


def test_d2_real_extraction_sample_uses_real_markdown_and_rolls_back(active_sqlite_db, tmp_path):
    engine, _ = active_sqlite_db
    markdown = tmp_path / "real_sample.md"
    markdown.write_text(
        "Li2S2 to Li2S conversion is the rate-limiting step. "
        "The graphene baseline has an activation barrier (E a = 2.73 eV), "
        "while SAC substrates reduce the barrier.",
        encoding="utf-8",
    )
    with Session(engine, future=True) as session:
        paper = Paper(
            title="D2 real paper sample",
            pdf_path="real.pdf",
            markdown_path=str(markdown),
            authors=["A"],
        )
        session.add(paper)
        session.commit()
        paper_id = paper.id

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(bind=connection, future=True) as session:
                result = run_real_extraction_sample_rollback(session, paper_id=paper_id)
        finally:
            transaction.rollback()

    assert result["status"] == "passed"
    assert result["sample_paper_id"] == str(paper_id)
    assert result["target_property_type"] == "reaction_barrier"
    assert result["target_value"] == 2.73
    assert result["target_unit"] == "eV"
    assert "E a = 2.73 eV" in result["evidence_text"]
    assert result["evidence_reference_count"] == 1
    assert result["page"] is None
    assert result["bbox"] is None
    assert result["corrected_review_status_after_save"] == "corrected"
    assert result["mark_verified_status"] == "verified"
    assert result["export_gate_safe_verified"] is False
    assert "unsafe_locator" in result["export_gate_reasons"]
    assert result["unsafe_data_blocked"] is True
    assert result["writing_cards_safe_usable"] == 0

    with Session(engine, future=True) as session:
        assert session.query(DFTResult).count() == 0
        assert build_audit(session)["dft_export_safe_eligible"] == 0
