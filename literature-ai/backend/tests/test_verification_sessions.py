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
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
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
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Li2S4 adsorption surface",
            catalyst_type="model_surface",
            support="graphene",
        )
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
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


def test_verification_session_rejects_ambiguous_paper_ref_across_libraries(verification_env):
    Session = verification_env
    with Session() as session:
        session.add_all(
            [
                Paper(
                    title="Shared verification ref A",
                    pdf_path="shared-a.pdf",
                    library_name="库A",
                    doi="10.1000/shared-verification-ref",
                ),
                Paper(
                    title="Shared verification ref B",
                    pdf_path="shared-b.pdf",
                    library_name="库B",
                    doi="10.1000/shared-verification-ref",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.post(
        "/api/workbench/verification-sessions",
        json={
            "paper_refs": ["10.1000/shared-verification-ref"],
            "scope": "all",
            "refresh_materials": False,
            "reviewer": "test_runner",
        },
    )

    assert response.status_code == 400
    detail = str(response.json()["detail"]).lower()
    assert "ambiguous" in detail or "multiple libraries" in detail or "library" in detail


def test_settle_ai_dft_reviews_endpoint_is_idempotent(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Endpoint settlement paper", pdf_path="endpoint.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Graphdiyne",
            catalyst_type="graphdiyne",
            support="graphdiyne",
        )
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="band_gap",
            adsorbate="2-AGDYNR",
            value=0.825,
            unit="eV",
            evidence_text="The band gap is 0.825 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=3,
                evidence_text="The band gap is 0.825 eV.",
                locator_status="exact_page",
                locator_confidence=0.95,
            )
        )
        for source_label in ("ai-1", "ai-2"):
            run = ExternalAnalysisRun(
                paper_id=paper.id,
                source="ide_ai",
                source_label=source_label,
                raw_payload={},
                normalized_payload={},
                mapping_status="mapped",
            )
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": str(row.id),
                        "field_name": "value",
                        "decision": "PASS",
                        "corrected_value": 0.825,
                        "confidence": 0.93,
                        "normalized_material": "graphdiyne",
                        "normalized_energy_type": "band_gap",
                        "evidence_location": {"page": 3, "quoted_text": "0.825 eV"},
                    },
                    status="pending",
                )
            )
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    first = client.post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")
    second = client.post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["auto_applied_count"] == 1
    assert second.json()["auto_applied_count"] == 0

    with Session() as session:
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == UUID(paper_id))).all()
        assert len(reviews) == 1
        assert {review.reviewer_status for review in reviews} == {"verified"}


def test_dual_ai_consensus_creates_missing_catalyst_sample(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Multi-material paper", pdf_path="multi.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    created = client.post(
        "/api/workbench/verification-sessions",
        json={"paper_ids": [paper_id], "scope": "all", "refresh_materials": False, "reviewer": "test_runner"},
    )
    assert created.status_code == 200
    session_payload = created.json()
    labels = session_payload["lane_labels"]
    proposed = {
        "name": "Pt",
        "catalyst_type": "comparator",
        "metal_centers": ["Pt"],
        "coordination": "Pt surface",
        "support": None,
        "synthesis_method": None,
        "evidence_strength": "Original PDF text",
        "structure_name": "Pt catalyst",
    }

    with Session() as session:
        for source, label in (("ai_a", labels["primary"]), ("ai_b", labels["secondary"])):
            run = ExternalAnalysisRun(
                paper_id=UUID(paper_id), source=source, source_label=label,
                raw_payload={}, normalized_payload={}, mapping_status="mapped",
            )
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=UUID(paper_id),
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "catalyst_samples",
                        "target_id": "new",
                        "field_name": "create",
                        "decision": "REVISE",
                        "corrected_value": proposed,
                        "confidence": 0.95,
                        "evidence_location": {
                            "page": 2,
                            "section": "Introduction",
                            "quoted_text": "0.44 eV on Pt",
                        },
                    },
                    status="pending",
                )
            )
        session.commit()

    settled = client.post(
        f"/api/workbench/verification-sessions/{session_payload['session_id']}/settle",
        json={"reviewer": "dual_ai_test"},
    )
    assert settled.status_code == 200
    assert settled.json()["settlement"]["high_risk"]["auto_applied_count"] == 1
    with Session() as session:
        samples = session.scalars(select(CatalystSample).where(CatalystSample.paper_id == UUID(paper_id))).all()
        assert len(samples) == 1
        assert samples[0].name == "Pt"
        assert samples[0].metal_centers == ["Pt"]
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == UUID(paper_id))
        ).all()
        assert {item.materialized_target_type for item in candidates} == {"catalyst_sample"}
        assert {item.materialized_target_id for item in candidates} == {str(samples[0].id)}


def test_materialized_new_candidate_can_settle_with_independent_value_pass(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Missing-row follow-up", pdf_path="missing-row.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="work_function",
            adsorbate="H2",
            value=5.77,
            unit="eV",
            reaction_step="DFT-PBE",
            source_section="Page 3",
            evidence_text="Pt work function is 5.77 eV.",
            candidate_status="new_candidate",
            evidence_payload={
                "page": 3,
                "quoted_text": "work functions of Ir and Pt are relatively high, at 5.59 eV and 5.77 eV",
                "material_identity": "Pt monometallic (001) surface on Pd seed symmetry",
                "source_document_type": "main",
            },
            extraction_protocol_version="ide_ai_new_candidate_v1",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=3,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.95,
            )
        )
        new_candidate_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-new",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        value_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add_all([new_candidate_run, value_pass_run])
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=new_candidate_run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": "new",
                    "field_name": "dft_results",
                    "decision": "new_candidate",
                    "corrected_value": {
                        "material": "Pt monometallic (001) surface on Pd seed symmetry",
                        "property": "work_function",
                        "energy_type": "work_function",
                        "value": 5.77,
                        "unit": "eV",
                        "method": "DFT-PBE",
                    },
                    "normalized_material": "Pt monometallic (001) surface on Pd seed symmetry",
                    "normalized_energy_type": "work_function",
                    "confidence": 0.91,
                    "evidence_location": {
                        "page": 3,
                        "quoted_text": "work functions of Ir and Pt are relatively high, at 5.59 eV and 5.77 eV",
                    },
                },
                status="materialized",
                materialized_target_type="dft_results",
                materialized_target_id=str(row.id),
            )
        )
        session.add(
            ExternalAnalysisCandidate(
                run_id=value_pass_run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "value",
                    "decision": "PASS",
                    "corrected_value": 5.77,
                    "normalized_material": "Pt monometallic (001) surface on Pd seed symmetry",
                    "normalized_energy_type": "work_function",
                    "confidence": 0.93,
                    "evidence_location": {
                        "page": 3,
                        "quoted_text": "work functions of Ir and Pt are relatively high, at 5.59 eV and 5.77 eV",
                    },
                },
                status="candidate",
            )
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    client = TestClient(app)
    settled = client.post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")
    assert settled.status_code == 200
    payload = settled.json()
    assert payload["auto_applied_count"] == 1

    with Session() as session:
        stored = session.get(DFTResult, row_id)
        assert stored.catalyst_sample_id is not None
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert len(reviews) == 1
        assert {review.reviewer_status for review in reviews} == {"verified"}


def test_field_level_proposal_can_settle_with_independent_value_pass(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Field proposal follow-up", pdf_path="field-proposal.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Ru")
        session.add(sample)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            property_type="adsorption_energy",
            adsorbate="H",
            value=-0.67,
            unit="eV",
            reaction_step="DFT-PBE",
            source_section="Page 4",
            evidence_text="Ru OH* adsorption energy is -0.67 eV.",
            candidate_status="new_candidate",
            evidence_payload={
                "page": 4,
                "quoted_text": "Ru OH* adsorption energy is -0.67 eV.",
                "material_identity": "Ru",
                "source_document_type": "main",
            },
            extraction_protocol_version="ide_ai_new_candidate_v1",
        )
        session.add(row)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                page=4,
                evidence_text=row.evidence_text,
                locator_status="exact_page",
                locator_confidence=0.95,
            )
        )
        proposal_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-proposal",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        value_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add_all([proposal_run, value_pass_run])
        session.flush()
        proposal_candidate = ExternalAnalysisCandidate(
            run_id=proposal_run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": str(row.id),
                "field_name": "adsorbate",
                "decision": "PROPOSED",
                "corrected_value": "OH*",
                "normalized_material": "Ru",
                "normalized_energy_type": "adsorption_energy",
                "confidence": 0.91,
                "evidence_location": {
                    "page": 4,
                    "section": "DFT results",
                    "quoted_text": "Ru OH* adsorption energy is -0.67 eV.",
                },
            },
            status="materialized",
            materialized_target_type="dft_results",
            materialized_target_id=str(row.id),
        )
        pass_candidate = ExternalAnalysisCandidate(
            run_id=value_pass_run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": str(row.id),
                "field_name": "value",
                "decision": "PASS",
                "corrected_value": -0.67,
                "normalized_material": "Ru monometallic (001) surface on Pd seed symmetry",
                "normalized_energy_type": "adsorption_energy",
                "confidence": 0.88,
                "evidence_location": {
                    "page": 4,
                    "table": "Figure 2",
                    "quoted_text": "Ru OH* adsorption energy is -0.67 eV.",
                },
            },
            status="candidate",
        )
        session.add_all([proposal_candidate, pass_candidate])
        session.commit()
        paper_id = paper.id
        row_id = row.id

    client = TestClient(app)
    settled = client.post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")
    assert settled.status_code == 200
    payload = settled.json()
    assert payload["auto_applied_count"] == 1
    assert payload["need_repair_count"] == 0

    with Session() as session:
        stored = session.get(DFTResult, row_id)
        assert stored.adsorbate == "OH*"
        assert stored.catalyst_sample_id is not None
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert len(reviews) == 1
        assert {review.reviewer_status for review in reviews} == {"verified"}


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
