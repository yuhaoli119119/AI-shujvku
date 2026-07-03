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
from app.services.dft_material_binding_service import DFTMaterialBindingService
from app.services.dft_review_service import DFTResultReviewService
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


def test_new_dft_materialization_merges_method_only_step_with_specific_adsorption_step(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Method-only DFT duplicate paper", pdf_path="method-only.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="method-step-test")
        session.add(run)
        session.flush()

        method_payload = {
            "target_type": "dft_results",
            "target_id": "new",
            "field_name": "dft_results",
            "decision": "new_candidate",
            "corrected_value": {
                "material": "WN4@G/TiS2",
                "adsorbate": "Li2S",
                "property_type": "adsorption_energy",
                "reaction_step": "DFT-D2 GGA-PBE",
                "value": -5.21,
                "unit": "eV",
            },
            "evidence_location": {
                "source_document_type": "supplementary_information",
                "page": 5,
                "quoted_text": "WN4@G/TiS2 Li2S -5.21 eV",
            },
        }
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload=method_payload,
                status="candidate",
            )
        )
        session.flush()

        specific_payload = {
            **method_payload,
            "corrected_value": {
                **method_payload["corrected_value"],
                "reaction_step": "Li2S adsorption on WN4@G side",
            },
        }
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload=specific_payload,
                status="candidate",
            )
        )
        session.flush()

        service = VerificationSessionService(session, get_settings())
        result = service._materialize_new_dft_candidates(paper_id=paper.id, reviewer="pytest")

        dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id)
        ).all()

        assert [item["action"] for item in result["materialized_items"]] == ["created", "deduplicated"]
        assert len(dft_rows) == 1
        assert dft_rows[0].reaction_step == "Li2S adsorption on WN4@G side"
        assert dft_rows[0].catalyst_sample_id is not None
        sample = session.get(CatalystSample, dft_rows[0].catalyst_sample_id)
        assert sample is not None
        assert sample.name == "WN4@G/TiS2"
        assert {candidate.materialized_target_id for candidate in candidates} == {str(dft_rows[0].id)}


def test_dft_material_binding_backfill_reuses_creates_and_skips_missing_identity(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="DFT material binding backfill", pdf_path="binding-backfill.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        existing_sample = CatalystSample(paper_id=paper.id, name="V-BP", catalyst_type="unknown")
        session.add(existing_sample)
        session.flush()
        v_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            value=-4.96,
            unit="eV",
            evidence_payload={"material_identity": "V-BP", "page": 7},
        )
        sc_row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            value=-4.235,
            unit="eV",
            evidence_payload={
                "corrected_value": {"material_identity": "Sc-BP"},
                "page": 7,
            },
        )
        rejected_row = DFTResult(
            paper_id=paper.id,
            property_type="binding_energy",
            value=-5.63,
            unit="eV",
            candidate_status="Rejected",
            evidence_payload={"page": 7},
        )
        session.add_all([v_row, sc_row, rejected_row])
        session.flush()

        result = DFTMaterialBindingService(session).backfill_paper(
            paper_id=paper.id,
            actor="pytest",
        )

        assert result["bound_count"] == 2
        assert result["skipped_count"] == 1
        assert result["created_sample_count"] == 1
        assert v_row.catalyst_sample_id == existing_sample.id
        assert sc_row.catalyst_sample_id is not None
        assert session.get(CatalystSample, sc_row.catalyst_sample_id).name == "Sc-BP"
        assert rejected_row.catalyst_sample_id is None


def test_new_dft_candidate_without_adsorbate_does_not_default_to_h2():
    service = _make_settle_service()
    run = ExternalAnalysisRun(paper_id=uuid4(), source="ide_ai", source_label="adsorbate-null-test")
    candidate_item, reason = service._new_dft_candidate_item(
        {
            "target_type": "dft_results",
            "target_id": "new",
            "field_name": "dft_results",
            "decision": "new_candidate",
            "corrected_value": {
                "material": "V-BP",
                "property_type": "reaction_barrier",
                "value": 0.543,
                "unit": "eV",
                "reaction_step": "Li2S decomposition",
            },
            "evidence_location": {
                "page": 4,
                "quoted_text": "V-BP shows a Li2S decomposition barrier of 0.543 eV.",
            },
        },
        run=run,
    )

    assert reason == ""
    assert candidate_item is not None
    assert candidate_item["adsorbate"] is None


def test_new_dft_materialization_merges_generic_adsorption_step_aliases(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Generic adsorption dedupe paper", pdf_path="generic-adsorption.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="generic-adsorption-test")
        session.add(run)
        session.flush()

        for reaction_step in ("adsorption", "Li2S4 adsorption", "adsorption of Li2S4"):
            session.add(
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
                            "material": "Fe-GDY",
                            "adsorbate": "Li2S4",
                            "property_type": "adsorption_energy",
                            "reaction_step": reaction_step,
                            "value": -1.1,
                            "unit": "eV",
                        },
                        "evidence_location": {
                            "source_document_type": "main_text",
                            "page": 5,
                            "quoted_text": "Fe-GDY Li2S4 adsorption -1.10 eV",
                        },
                    },
                    status="candidate",
                )
            )
        session.flush()

        service = VerificationSessionService(session, get_settings())
        result = service._materialize_new_dft_candidates(paper_id=paper.id, reviewer="pytest")

        dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id)
        ).all()

        assert [item["action"] for item in result["materialized_items"]] == ["created", "deduplicated", "deduplicated"]
        assert len(dft_rows) == 1
        assert {candidate.materialized_target_id for candidate in candidates} == {str(dft_rows[0].id)}


def test_new_dft_materialization_reuses_existing_generic_adsorption_row(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Existing generic adsorption row", pdf_path="existing-generic.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        existing = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-1.1,
            unit="eV",
            evidence_payload={"material_identity": "Fe-GDY", "page": 5, "source_document_type": "main_text"},
            candidate_status="new_candidate",
        )
        session.add(existing)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="existing-generic-test")
        session.add(run)
        session.flush()
        session.add(
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
                        "material": "Fe-GDY",
                        "adsorbate": "Li2S4",
                        "property_type": "adsorption_energy",
                        "reaction_step": "Li2S4 adsorption",
                        "value": -1.1,
                        "unit": "eV",
                    },
                    "evidence_location": {
                        "source_document_type": "supplementary_information",
                        "page": 12,
                        "quoted_text": "Fe-GDY Li2S4 adsorption -1.10 eV",
                    },
                },
                status="candidate",
            )
        )
        session.flush()

        service = VerificationSessionService(session, get_settings())
        result = service._materialize_new_dft_candidates(paper_id=paper.id, reviewer="pytest")

        dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        candidate = session.scalar(select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id))

        assert [item["action"] for item in result["materialized_items"]] == ["deduplicated"]
        assert len(dft_rows) == 1
        assert candidate is not None
        assert candidate.materialized_target_id == str(existing.id)


def test_new_dft_materialization_keeps_distinct_active_site_steps(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Distinct active site adsorption paper", pdf_path="specific-sites.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="specific-site-test")
        session.add(run)
        session.flush()
        for reaction_step in ("Li2S adsorption on WN4@G side", "Li2S adsorption on TiS2 side"):
            session.add(
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
                            "material": "WN4@G/TiS2",
                            "adsorbate": "Li2S",
                            "property_type": "adsorption_energy",
                            "reaction_step": reaction_step,
                            "value": -5.21,
                            "unit": "eV",
                        },
                        "evidence_location": {"page": 5, "quoted_text": reaction_step},
                    },
                    status="candidate",
                )
            )
        session.flush()

        service = VerificationSessionService(session, get_settings())
        result = service._materialize_new_dft_candidates(paper_id=paper.id, reviewer="pytest")

        dft_rows = session.scalars(
            select(DFTResult).where(DFTResult.paper_id == paper.id).order_by(DFTResult.reaction_step.asc())
        ).all()

        assert [item["action"] for item in result["materialized_items"]] == ["created", "created"]
        assert [row.reaction_step for row in dft_rows] == [
            "Li2S adsorption on TiS2 side",
            "Li2S adsorption on WN4@G side",
        ]


def test_new_dft_materialization_skips_supporting_reference_candidates(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Supporting reference candidate paper", pdf_path="supporting-ref.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="supporting-ref-test")
        session.add(run)
        session.flush()
        session.add(
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
                        "material": "Fe-GDY",
                        "adsorbate": "Li2S4",
                        "property_type": "adsorption_energy",
                        "reaction_step": "Li2S4 adsorption",
                        "value": -1.1,
                        "unit": "eV",
                    },
                    "evidence_location": {
                        "source_document_type": "supporting_reference",
                        "page": 8,
                        "quoted_text": "Cited reference reports -1.10 eV.",
                    },
                },
                status="candidate",
            )
        )
        session.flush()

        service = VerificationSessionService(session, get_settings())
        result = service._materialize_new_dft_candidates(paper_id=paper.id, reviewer="pytest")

        dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        candidate = session.scalar(select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id))

        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [{"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"}]
        assert dft_rows == []
        assert candidate.status == "ignored"


def test_method_only_step_match_does_not_merge_ambiguous_specific_steps():
    candidate = {
        "material_identity": "WN4@G/TiS2",
        "property_type": "adsorption_energy",
        "value": -5.21,
        "unit": "eV",
        "adsorbate": "Li2S",
        "reaction_step": "DFT-D2 GGA-PBE",
    }
    rows = [
        DFTResult(reaction_step="Li2S adsorption on WN4@G side"),
        DFTResult(reaction_step="Li2S adsorption on TiS2 side"),
    ]

    assert VerificationSessionService._method_step_compatible_existing(candidate, rows) is None


def test_borrowed_reference_new_candidate_is_retired_instead_of_left_pending():
    candidate = MagicMock()
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()

    service._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")

    assert candidate.status == "ignored"
    service.session.add.assert_called_once_with(candidate)


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


def test_reset_dft_ai_reviews_clears_audits_and_returns_rows_to_pending(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Reset DFT reviews paper", pdf_path="reset-dft.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="gibbs_free_energy_change",
            adsorbate="*H",
            value=-0.09,
            unit="eV",
            evidence_text="Delta G H* is -0.09 eV.",
            candidate_status="Rejected",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=row.value,
                reviewed_value=None,
                unit=row.unit,
                evidence_text=row.evidence_text,
                reviewer_status="rejected",
                reviewer="old_ai_review",
                target_resolution_status="active",
                last_resolved_target_id=str(row.id),
            )
        )
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="old_dft_ai",
            source_identity="mcp:old-dft-ai",
            source_identity_verified=True,
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
                    "field_name": "dft_results",
                    "decision": "REJECT",
                    "evidence_location": {"page": 5, "quoted_text": "-0.09 eV"},
                },
                materialized_target_type="dft_results",
                materialized_target_id=str(row.id),
                status="materialized",
            )
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/dft-ai-reviews/reset",
        json={
            "confirm_reset_dft_ai_reviews": True,
            "reviewer": "test_runner",
            "keep_dft_candidates": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted_object_review_candidates"] == 1
    assert payload["deleted_field_reviews"] == 1
    assert payload["reset_dft_results"] == 1

    with Session() as session:
        row = session.get(DFTResult, UUID(row_id))
        assert row is not None
        assert row.candidate_status == "system_candidate"
        assert session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == UUID(paper_id))
        ).all() == []
        assert session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == UUID(paper_id))
        ).all() == []


def test_dft_verify_can_defer_commit_to_outer_settlement(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Outer transaction paper", pdf_path="outer-transaction.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="*H",
            value=0.04,
            unit="eV",
            evidence_text="The adsorption energy is 0.04 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        session.add(
            ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=row.value,
                reviewed_value=row.value,
                unit=row.unit,
                evidence_text=row.evidence_text,
                reviewer_status="pending",
                reviewer="earlier_review_pass",
                target_resolution_status="active",
                last_resolved_target_id=str(row.id),
            )
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    with Session() as session:
        with pytest.raises(ValueError, match="write_conflict:extraction_review_version_required"):
            DFTResultReviewService(session).verify_result(
                paper_id=paper_id,
                result_id=row_id,
                confirm_reviewed_against_pdf=True,
                reviewer="missing_version",
                field_names=["value"],
                evidence_payload={"page": 5, "quoted_text": "0.04 eV"},
                commit=False,
            )
        session.rollback()

    with Session() as session:
        result = DFTResultReviewService(session).verify_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reviewed_against_pdf=True,
            reviewer="outer_settlement",
            field_names=["value"],
            expected_write_versions={"value": 1},
            evidence_payload={"page": 5, "quoted_text": "0.04 eV"},
            commit=False,
        )
        assert result["reviews"][0]["reviewer_status"] == "verified"
        assert session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_id == str(row_id),
            )
        ) is not None
        session.rollback()

    with Session() as session:
        persisted_review = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_id == str(row_id),
            )
        )
        assert persisted_review is not None
        assert persisted_review.reviewer_status == "pending"


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
                source_identity=f"mcp:{source}",
                source_identity_verified=True,
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
            source_identity="mcp:ai-lane-new",
            source_identity_verified=True,
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
    detail = client.get(f"/api/papers/{paper_id}/dft-results")
    assert detail.status_code == 200
    items = detail.json()["items"]
    target = next(item for item in items if item["id"] == str(row_id))
    assert target["object_review_audit_count"] == 1
    assert [audit["decision"] for audit in target["object_review_audits"]] == ["new_candidate"]


def test_dft_review_queue_includes_materialized_new_candidate_audits(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Queue materialized new candidate paper", pdf_path="queue-new-dft.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="*H",
            value=0.02,
            unit="eV",
            reaction_step="hydrogen adsorption",
            source_section="Table 1",
            evidence_text="Delta G H* = 0.02 eV.",
            candidate_status="new_candidate",
            evidence_payload={
                "page": 6,
                "quoted_text": "Delta G H* = 0.02 eV.",
                "material_identity": "CuMn@N6Gr",
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
                page=6,
                evidence_text="Delta G H* = 0.02 eV.",
                locator_status="exact_page",
                locator_confidence=0.95,
            )
        )
        new_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-new-source",
            source_identity="mcp:ai-new-source",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        reject_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-reject-source",
            source_identity="mcp:ai-reject-source",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add_all([new_run, reject_run])
        session.flush()
        session.add_all(
            [
                ExternalAnalysisCandidate(
                    run_id=new_run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": "new",
                        "field_name": "dft_results",
                        "decision": "new_candidate",
                        "corrected_value": {
                            "material": "CuMn@N6Gr",
                            "property": "gibbs_free_energy_change",
                            "adsorbate": "*H",
                            "value": 0.02,
                            "unit": "eV",
                        },
                        "evidence_location": {"page": 6, "quoted_text": "Delta G H* = 0.02 eV."},
                    },
                    status="materialized",
                    materialized_target_type="dft_results",
                    materialized_target_id=str(row.id),
                ),
                ExternalAnalysisCandidate(
                    run_id=reject_run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": str(row.id),
                        "field_name": "dft_results",
                        "decision": "REJECT",
                        "reason": "This row duplicates a better normalized Gibbs free-energy record.",
                        "evidence_location": {"page": 6, "quoted_text": "Delta G H* = 0.02 eV."},
                    },
                    status="candidate",
                ),
            ]
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id

    client = TestClient(app)
    queue = client.get(f"/api/papers/export/dft-review-queue?paper_id={paper_id}&limit=10&status=needs_review")
    assert queue.status_code == 200
    rows = queue.json()["rows"]
    target = next(item for item in rows if item["record_id"] == str(row_id))
    decisions = {audit["decision"] for audit in target["object_review_audits"]}
    sources = {audit["source_label"] for audit in target["object_review_audits"]}
    assert {"new_candidate", "REJECT"} <= decisions
    assert {"ai-new-source", "ai-reject-source"} <= sources


# DFT single-opinion validation helpers.


def _make_settle_service() -> VerificationSessionService:
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()
    service.session.get.return_value = None
    return service


def _make_audit(
    *,
    decision: str,
    field_name: str = "dft_results",
    corrected_value: dict | None = None,
    evidence_payload: dict | None = None,
    adjudication_role: str = "",
    confidence: float = 0.8,
    material: str | None = None,
    candidate_id: str | None = None,
    source_label: str | None = None,
    source: str | None = None,
    agent_role: str | None = None,
) -> dict:
    audit: dict = {
        "candidate_id": candidate_id or "candidate-1",
        "status": "materialized",
        "decision": decision,
        "field_name": field_name,
        "corrected_value": corrected_value or {},
        "evidence_payload": evidence_payload if evidence_payload is not None else {"page": 1, "quoted_text": "evidence"},
        "confidence": confidence,
        "candidate": MagicMock(),
    }
    if source_label:
        audit["source_label"] = source_label
    if source:
        audit["source"] = source
    if agent_role:
        audit["agent_role"] = agent_role
    if adjudication_role:
        audit["adjudication_role"] = adjudication_role
    if material:
        audit["material"] = material
    return audit


def test_single_dft_opinion_without_evidence_anchor_needs_repair():
    row = DFTResult(
        id=uuid4(),
        paper_id=uuid4(),
        property_type="adsorption_energy",
        adsorbate="H",
        value=-0.95,
        unit="eV",
    )
    audits = [
        _make_audit(
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV"},
            evidence_payload={},
        )
    ]

    service = _make_settle_service()
    result = service._settle_dft_row_from_existing_audits(
        row=row,
        audits=audits,
        reviewer="test_reviewer",
        write_lock_tokens=None,
    )

    assert result["status"] == "need_repair"
    assert result["reason"] == "missing_evidence_anchor"
