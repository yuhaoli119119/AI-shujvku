from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    AuditLog,
    Base,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperNote,
)
from app.db.session import get_db_session
from app.main import app


@pytest.fixture
def verification_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        db_path = root / "verification.db"
        storage_root = root / "storage"
        monkeypatch.setenv("LITAI_DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        monkeypatch.setenv("LITAI_DOCLING_DO_OCR", "false")
        get_settings.cache_clear()

        engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)

        def override_get_db_session():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        yield Session

        app.dependency_overrides.clear()
        engine.dispose()
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_verification_session_settlement_auto_adopts_consensus_and_single_ai_notes(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Verification paper", pdf_path="verification.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            evidence_text="Table 2 reports -1.2 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=5,
                bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.95,
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    created = client.post(
        "/api/workbench/verification-sessions",
        json={
            "paper_ids": [paper_id],
            "scope": "all",
            "refresh_materials": False,
            "reviewer": "test_runner",
        },
    )
    assert created.status_code == 200
    session_payload = created.json()
    labels = session_payload["lane_labels"]

    with Session() as session:
        primary_run = ExternalAnalysisRun(
            paper_id=UUID(paper_id),
            source="codex_primary",
            source_label=labels["primary"],
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        secondary_run = ExternalAnalysisRun(
            paper_id=UUID(paper_id),
            source="claude_secondary",
            source_label=labels["secondary"],
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        writing_run = ExternalAnalysisRun(
            paper_id=UUID(paper_id),
            source="codex_single",
            source_label=labels["single"],
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add_all([primary_run, secondary_run, writing_run])
        session.flush()
        for run in (primary_run, secondary_run):
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=UUID(paper_id),
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": row_id,
                        "field_name": "value",
                        "decision": "PASS",
                        "corrected_value": -1.2,
                        "confidence": 0.91,
                        "evidence_location": {
                            "page": 5,
                            "table": "Table 2",
                            "evidence_text": "Table 2 reports -1.2 eV.",
                            "locator": {"page": 5, "locator_status": "exact_page"},
                        },
                    },
                    status="pending",
                )
            )
        session.add(
            ExternalAnalysisCandidate(
                run_id=writing_run.id,
                paper_id=UUID(paper_id),
                candidate_type="note",
                normalized_payload={
                    "content": "Discussion section emphasizes stable adsorption ordering.",
                    "field_name": "discussion_summary",
                    "page": 6,
                    "section_title": "Discussion",
                    "quoted_text": "The discussion highlights the adsorption ordering.",
                },
                status="pending",
            )
        )
        session.commit()

    settled = client.post(
        f"/api/workbench/verification-sessions/{session_payload['session_id']}/settle",
        json={"reviewer": "test_runner"},
    )
    assert settled.status_code == 200
    settlement = settled.json()["settlement"]
    assert settlement["high_risk"]["auto_applied_count"] == 1
    assert settlement["low_risk_notes"]["auto_materialized_count"] == 1
    assert settlement["high_risk"]["manual_conflict_count"] == 0

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status in {"ML_Ready", "human_reviewed_needs_evidence"}
        note = session.scalar(select(PaperNote).where(PaperNote.paper_id == UUID(paper_id)))
        assert note is not None
        assert note.section_title == "Discussion"
        assert session.scalar(select(AuditLog).where(AuditLog.action == "single_ai_auto_materialize_note")) is not None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "settle_verification_session")) is not None


def test_manual_conflict_decision_can_adopt_specific_opinion(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Manual conflict", pdf_path="manual.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            value=-1.1,
            unit="eV",
            evidence_text="Stored candidate is weaker than the consensus table value.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="table",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=4,
                bbox={"l": 1, "t": 1, "r": 10, "b": 10},
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.92,
            )
        )
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="manual_conflict",
            source_label="manual-conflict",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        adopt = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": str(row.id),
                "field_name": "value",
                "decision": "PASS",
                "corrected_value": -1.35,
                "confidence": 0.87,
                "evidence_location": {
                    "page": 4,
                    "table": "Table 1",
                    "evidence_text": "Table 1 supports -1.35 eV.",
                    "locator": {"page": 4, "locator_status": "exact_page"},
                },
            },
            status="pending",
        )
        other = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": str(row.id),
                "field_name": "value",
                "decision": "PASS",
                "corrected_value": -1.1,
                "confidence": 0.52,
                "evidence_location": {
                    "page": 4,
                    "section": "Discussion",
                    "evidence_text": "Narrative text repeats the old value.",
                    "locator": {"page": 4, "locator_status": "text_only"},
                },
            },
            status="pending",
        )
        session.add_all([adopt, other])
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)
        adopt_source_id = str(adopt.id)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-conflicts/manual-decision",
        json={
            "paper_id": paper_id,
            "target_type": "dft_results",
            "target_id": row_id,
            "field_name": "value",
            "resolution": "adopt_opinion",
            "reviewer": "manual_test",
            "opinion_source_id": adopt_source_id,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "approve_correction"

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.value == pytest.approx(-1.35)
        assert session.scalar(select(AuditLog).where(AuditLog.action == "manual_conflict_resolution")) is not None
