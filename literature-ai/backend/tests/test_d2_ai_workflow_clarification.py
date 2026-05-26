from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Base, DFTResult, EvidenceSpan, ExternalAnalysisRun, Paper
from app.utils.active_database import require_active_library_sqlite
from scripts.audit_ai_workflow_boundary import build_audit, run_e2e_rollback


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
        assert result["export_gate_safe_verified"] is True
        assert result["unsafe_data_blocked"] is True

        post_rollback = build_audit(session)
        assert post_rollback["review_status_counts"] == {}
        assert post_rollback["dft_export_safe_eligible"] == 0
