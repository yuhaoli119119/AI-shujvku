from __future__ import annotations

import os

import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

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
from app.services.verification_session_service import VerificationSessionService


@pytest.fixture
def verification_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        storage_root = root / "storage"
        monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        monkeypatch.setenv("LITAI_DOCLING_DO_OCR", "false")
        get_settings.cache_clear()

        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
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


def test_dft_revise_and_pass_with_equivalent_units_form_consensus():
    row = DFTResult(
        id=uuid4(),
        paper_id=uuid4(),
        property_type="bader_charge",
        adsorbate="FeCo",
        value=0.9,
        unit="e",
        evidence_text="FeCo Bader charge is 0.90 e.",
    )
    revise = {
        "source_identity": "dynamic-review-source-a",
        "decision": "REVISE",
        "field_name": "dft_results",
        "corrected_value": {
            "property_type": "bader_charge",
            "adsorbate": "FeCo",
            "material_identity": "FeCo@C2N",
            "value": 0.9,
            "unit": "e",
        },
        "evidence_payload": {"page": 3, "quoted_text": "FeCo | 0.90"},
    }
    passed = {
        "source_identity": "dynamic-review-source-b",
        "decision": "PASS",
        "field_name": "dft_results",
        "corrected_value": {
            "property_type": "bader_charge",
            "adsorbate": "FeCo",
            "material_identity": "FeCo@C2N",
            "value": 0.90,
            "unit": "|e|",
        },
        "evidence_payload": {"page": 3, "quoted_text": "FeCo | 0.90"},
    }
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()
    service.session.get.return_value = None

    proposal = service._latest_dft_whole_row_proposal([revise, passed])
    assert proposal is revise
    assert service._supporting_pass_for_row(row, [revise, passed], proposal) is passed
    assert service._all_nonnegative_dft_opinions_match(row, [revise, passed]) is True

    conflicting = {
        **passed,
        "source_identity": "dynamic-review-source-c",
        "corrected_value": {**passed["corrected_value"], "value": 1.2},
    }
    assert service._all_nonnegative_dft_opinions_match(row, [revise, passed, conflicting]) is False

    third_ai = {
        "source_identity": "dynamic-adjudicator",
        "decision": "PROPOSED",
        "field_name": "dft_results",
        "adjudication_role": "third_ai",
        "selected_source_ids": ["dynamic-review-source-a"],
        "corrected_value": {"value": 0.91},
    }
    inherited = service._inherit_selected_dft_evidence(third_ai, [revise, passed, third_ai])
    completed = service._complete_dft_third_ai_adjudication(row, inherited, [revise, passed, inherited])
    assert completed["corrected_value"] == {
        "property_type": "bader_charge",
        "adsorbate": "FeCo",
        "reaction_step": None,
        "value": 0.91,
        "unit": "e",
        "material_identity": "FeCo@C2N",
    }
    assert completed["evidence_payload"] == revise["evidence_payload"]


def test_pending_third_ai_adjudication_reopens_an_already_settled_dft_row():
    assert VerificationSessionService._has_pending_dft_adjudication(
        [
            {"status": "materialized", "adjudication_role": "third_ai"},
            {"status": "candidate", "adjudication_role": "third_ai"},
        ]
    ) is True
    assert VerificationSessionService._has_pending_dft_adjudication(
        [{"status": "materialized", "adjudication_role": "third_ai"}]
    ) is False


def test_dft_material_identity_comparison_is_case_insensitive():
    assert VerificationSessionService._material_identity_parts_compatible("CuNi@C2N", "cuni@c2n") is True


def test_new_dft_semantic_signature_ignores_locator_but_keeps_scientific_identity():
    base = {
        "material_identity": "CuCu@C2N",
        "property_type": "limiting_potential",
        "value": -0.76,
        "unit": "V",
        "adsorbate": "C2H4",
        "reaction_step": "limiting potential via *CO -> *CO+*CO",
    }
    same_science_different_locator = {**base, "source_figure": "Table 2", "page": 6}

    assert VerificationSessionService._new_dft_semantic_signature(base) == (
        "cucu@c2n",
        "limiting_potential",
        "-0.76",
        "v",
        "c2h4",
        "limiting potential via *co -> *co+*co",
    )
    assert VerificationSessionService._new_dft_semantic_signature(base) == (
        VerificationSessionService._new_dft_semantic_signature(same_science_different_locator)
    )


def test_borrowed_reference_new_candidate_is_retired_instead_of_left_pending():
    candidate = MagicMock()
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()

    service._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")

    assert candidate.status == "ignored"
    service.session.add.assert_called_once_with(candidate)


def test_matching_third_ai_adjudication_is_consumed_without_rewriting_settled_row():
    row = DFTResult(
        id=uuid4(),
        paper_id=uuid4(),
        property_type="reaction_barrier",
        adsorbate="OH",
        reaction_step="*OH -> *H2O",
        value=0.75,
        unit="eV",
    )
    candidate = MagicMock()
    candidate.status = "candidate"
    audit = {
        "candidate": candidate,
        "candidate_id": "adjudication-1",
        "source_identity": "dynamic-adjudicator",
        "status": "candidate",
        "decision": "PROPOSED",
        "field_name": "dft_results",
        "adjudication_role": "third_ai",
        "corrected_value": {
            "property_type": "reaction_barrier",
            "adsorbate": "OH",
            "reaction_step": "*OH -> *H2O",
            "value": 0.75,
            "unit": "eV",
        },
    }
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()

    assert service._consume_matching_settled_dft_adjudication(row=row, audits=[audit]) is True
    assert candidate.status == "ai_reviewed"
    service.session.add.assert_called_once_with(candidate)


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


def test_materialized_new_candidate_can_settle_with_whole_row_pass(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Missing-row whole-pass follow-up", pdf_path="missing-row-whole-pass.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="free_energy",
            adsorbate="*NO3",
            value=-2.94,
            unit="eV",
            reaction_step="adsorption",
            source_section="Page 8",
            evidence_text="The adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.94 eV on bcc Pd-In(111).",
            candidate_status="new_candidate",
            evidence_payload={
                "page": 8,
                "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
                "material_identity": "bcc Pd-In(111)",
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
                page=8,
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
        whole_row_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add_all([new_candidate_run, whole_row_pass_run])
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
                        "material": "bcc Pd-In(111)",
                        "property": "free_energy",
                        "energy_type": "free_energy",
                        "adsorbate": "*NO3",
                        "reaction_step": "adsorption",
                        "value": -2.94,
                        "unit": "eV",
                        "method": "DFT",
                    },
                    "normalized_material": "bcc Pd-In(111)",
                    "normalized_energy_type": "free_energy",
                    "confidence": 0.91,
                    "evidence_location": {
                        "page": 8,
                        "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
                    },
                },
                status="materialized",
                materialized_target_type="dft_results",
                materialized_target_id=str(row.id),
            )
        )
        session.add(
            ExternalAnalysisCandidate(
                run_id=whole_row_pass_run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "dft_results",
                    "decision": "PASS",
                    "normalized_material": "bcc Pd-In(111)",
                    "normalized_energy_type": "free_energy",
                    "confidence": 0.93,
                    "evidence_location": {
                        "page": 8,
                        "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
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
    assert payload["need_third_ai_count"] == 0

    with Session() as session:
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert len(reviews) == 1
        assert {review.reviewer_status for review in reviews} == {"verified"}


def test_paper_detail_dedupes_materialized_new_candidate_audits(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Detail dedupe paper", pdf_path="detail-dedupe.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="free_energy",
            adsorbate="*NO3",
            value=-2.94,
            unit="eV",
            reaction_step="adsorption",
            source_section="Page 8",
            evidence_text="The adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.94 eV on bcc Pd-In(111).",
            candidate_status="new_candidate",
            evidence_payload={
                "page": 8,
                "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
                "material_identity": "bcc Pd-In(111)",
                "source_document_type": "main",
            },
            extraction_protocol_version="ide_ai_new_candidate_v1",
        )
        session.add(row)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-new",
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        session.add_all(
            [
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": "new",
                        "decision": "new_candidate",
                        "corrected_value": {
                            "material": "bcc Pd-In(111)",
                            "property": "free_energy",
                            "value": -2.94,
                            "unit": "eV",
                        },
                        "confidence": 0.91,
                        "evidence_location": {
                            "page": 8,
                            "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
                        },
                    },
                    status="materialized",
                    materialized_target_type="dft_results",
                    materialized_target_id=str(row.id),
                ),
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": "new",
                        "field_name": "dft_results",
                        "decision": "new_candidate",
                        "corrected_value": {
                            "material": "bcc Pd-In(111)",
                            "property": "free_energy",
                            "value": -2.94,
                            "unit": "eV",
                        },
                        "confidence": 0.91,
                        "evidence_location": {
                            "page": 8,
                            "quoted_text": "the adsorption of NO3- gives *NO3- with a remarkable energy decrease up to 2.83 and 2.94 eV on fcc Pd-In(111) and bcc Pd-In(111)",
                        },
                    },
                    status="materialized",
                    materialized_target_type="dft_results",
                    materialized_target_id=str(row.id),
                ),
            ]
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    client = TestClient(app)
    detail = client.get(f"/api/papers/{paper_id}?mode=light")
    assert detail.status_code == 200
    items = detail.json()["dft_results_items"]
    target = next(item for item in items if item["id"] == str(row_id))
    assert target["object_review_audit_count"] == 1
    assert [audit["decision"] for audit in target["object_review_audits"]] == ["new_candidate"]


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
