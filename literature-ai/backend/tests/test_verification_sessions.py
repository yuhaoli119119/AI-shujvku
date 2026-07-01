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
        "source_identity_verified": True,
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
        "source_identity_verified": True,
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
        "source_identity_verified": True,
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
            source_identity="mcp:codex-primary",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        secondary_run = ExternalAnalysisRun(
            paper_id=UUID(paper_id),
            source="claude_secondary",
            source_label=labels["secondary"],
            source_identity="mcp:claude-secondary",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        writing_run = ExternalAnalysisRun(
            paper_id=UUID(paper_id),
            source="codex_single",
            source_label=labels["single"],
            source_identity="mcp:codex-single",
            source_identity_verified=True,
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
    assert settlement["high_risk"]["auto_applied_count"] == 0
    assert settlement["low_risk_notes"]["auto_materialized_count"] == 1
    assert settlement["high_risk"]["manual_conflict_count"] == 1

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        assert session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == UUID(paper_id),
                ExtractionFieldReview.target_id == row_id,
            )
        ).all() == []
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.paper_id == UUID(paper_id),
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
        ).all()
        assert {candidate.status for candidate in candidates} == {"requires_resolution"}
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
        for source_label in ("ai-1", "ai-2"):
            run = ExternalAnalysisRun(
                paper_id=paper.id,
                source="ide_ai",
                source_label=source_label,
                source_identity=f"mcp:{source_label}",
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
    assert first.json()["auto_applied_count"] == 0
    assert first.json()["audit_consensus_count"] == 1
    assert second.json()["auto_applied_count"] == 0
    assert second.json()["audit_consensus_count"] == 1

    with Session() as session:
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == UUID(paper_id))).all()
        assert len(reviews) == 1
        assert {review.reviewer_status for review in reviews} == {"pending"}


def test_auto_apply_dft_object_reviews_dedupes_same_source_identity(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Same source DFT review paper", pdf_path="same-source.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-0.95,
            unit="eV",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="same_ai",
            source_label="same_ai_dft_review",
            source_identity="mcp:same-ai-dft-review",
            source_identity_verified=True,
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        for idx, confidence in enumerate((0.72, 0.91), start=1):
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    status="candidate",
                    confidence=confidence,
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": str(row.id),
                        "field_name": "value",
                        "decision": "PASS",
                        "corrected_value": -0.95,
                        "confidence": confidence,
                        "source": "same_ai",
                        "source_label": "same_ai_dft_review",
                        "normalized_material": "CoN4",
                        "evidence_location": {"page": idx, "quoted_text": "CoN4 adsorption energy -0.95 eV"},
                    },
                )
            )
        session.flush()

        result = VerificationSessionService(session, get_settings())._auto_apply_object_review_candidates(
            paper_id=paper.id,
            reviewer="pytest",
            include_target_types={"dft_results"},
        )

    assert result["applied_count"] == 0
    assert result["pending_count"] == 1
    assert result["pending_items"][0]["reason"] == "awaiting_two_ai_reviews"
    assert result["pending_items"][0]["eligible_opinion_count"] == 1


def test_li_s_project_library_v4_consensus_requires_user_submit(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Li-S v4 user submit paper", pdf_path="li-s-v4.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(
            paper_id=paper.id,
            name="Fe-N-C",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="N-doped carbon",
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
            evidence_text="Table 1 reports Li2S4 adsorption energy of -1.2 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        for source_label in ("ai-1", "ai-2"):
            run = ExternalAnalysisRun(
                paper_id=paper.id,
                source="ide_ai",
                source_label=source_label,
                source_identity=f"mcp:{source_label}",
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
                        "schema_version": "project_library_ml_export_v4",
                        "project_library_context": "li_s_sac_dac",
                        "database_write_authority": "user_submit_only",
                        "ai_consensus_auto_adopt_allowed": False,
                        "target_type": "dft_results",
                        "target_id": str(row.id),
                        "field_name": "value",
                        "decision": "PASS",
                        "corrected_value": -1.2,
                        "confidence": 0.93,
                        "normalized_material": "Fe-N-C",
                        "normalized_energy_type": "adsorption_energy",
                        "evidence_location": {"page": 4, "quoted_text": "-1.2 eV"},
                    },
                    status="pending",
                )
            )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")

    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_applied_count"] == 0
    assert payload["need_repair_count"] == 1
    assert payload["need_repair_items"][0]["reason"] == "project_library_v4_requires_user_submit"

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == UUID(paper_id),
                ExtractionFieldReview.target_id == row_id,
            )
        ).all()
        assert reviews == []
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == UUID(paper_id))
        ).all()
        assert {candidate.status for candidate in candidates} == {"pending"}


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
            source_identity="mcp:ai-lane-new",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        value_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            source_identity="mcp:ai-lane-pass",
            source_identity_verified=True,
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
    assert payload["auto_applied_count"] == 0
    assert payload["audit_consensus_count"] == 1

    with Session() as session:
        stored = session.get(DFTResult, row_id)
        assert stored.catalyst_sample_id is None
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert reviews == []


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
            source_identity="mcp:ai-lane-new",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        whole_row_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            source_identity="mcp:ai-lane-pass",
            source_identity_verified=True,
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
    assert payload["auto_applied_count"] == 0
    assert payload["audit_consensus_count"] == 1
    assert payload["need_third_ai_count"] == 0

    with Session() as session:
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert reviews == []


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
            source_identity="mcp:ai-lane-proposal",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        value_pass_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="ai-lane-pass",
            source_identity="mcp:ai-lane-pass",
            source_identity_verified=True,
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
    assert payload["auto_applied_count"] == 0
    assert payload["audit_consensus_count"] == 1
    assert payload["need_repair_count"] == 0

    with Session() as session:
        stored = session.get(DFTResult, row_id)
        assert stored.adsorbate == "H"
        assert stored.catalyst_sample_id is not None
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row_id),
            )
        ).all()
        assert reviews == []


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
            source_identity="mcp:manual-conflict",
            source_identity_verified=True,
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


def test_manual_reject_all_dft_opinions_does_not_reject_underlying_result(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Manual reject-all DFT opinions", pdf_path="reject-all.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            value=-1.1,
            unit="eV",
            evidence_text="Stored candidate remains unresolved.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="manual_conflict",
            source_label="manual-conflict",
            source_identity="mcp:manual-conflict",
            source_identity_verified=True,
            raw_payload={},
            normalized_payload={},
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        for value, confidence in ((-1.35, 0.87), (-1.45, 0.82)):
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
                        "corrected_value": value,
                        "confidence": confidence,
                        "evidence_location": {
                            "page": 4,
                            "table": "Table 1",
                            "evidence_text": f"Table 1 supports {value} eV.",
                        },
                    },
                    status="pending",
                )
            )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        "/api/workbench/review-conflicts/manual-decision",
        json={
            "paper_id": paper_id,
            "target_type": "dft_results",
            "target_id": row_id,
            "field_name": "value",
            "resolution": "reject_all",
            "reviewer": "manual_test",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "audit_opinion_rejected"
    assert payload["writes_final_truth"] is False

    with Session() as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.value == pytest.approx(-1.1)
        assert stored.candidate_status == "system_candidate"
        assert session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == UUID(paper_id),
                ExtractionFieldReview.target_id == row_id,
            )
        ).all() == []
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == UUID(paper_id))
        ).all()
        assert {candidate.status for candidate in candidates} == {"ai_reviewed"}


# ---------------------------------------------------------------------------
# Characterization tests for _settle_dft_row_from_existing_audits branch
# priority.  Each test pins which branch fires for a given opinion
# configuration, so future refactors cannot silently reorder the priority
# ladder.
# ---------------------------------------------------------------------------


def _make_settle_service() -> VerificationSessionService:
    """Build a VerificationSessionService with a MagicMock session, matching
    the existing characterization-test pattern in this file."""
    service = object.__new__(VerificationSessionService)
    service.session = MagicMock()
    service.session.get.return_value = None  # _dft_identity_key looks up row
    return service


def _make_audit(
    *,
    source_identity: str,
    decision: str,
    field_name: str = "dft_results",
    corrected_value: dict | None = None,
    evidence_payload: dict | None = None,
    adjudication_role: str = "",
    confidence: float = 0.8,
    material: str | None = None,
    candidate_id: str | None = None,
) -> dict:
    """Build a minimal audit dict accepted by _settle_dft_row_from_existing_audits."""
    audit: dict = {
        "source_identity": source_identity,
        "source_identity_verified": True,
        "candidate_id": candidate_id or f"cand-{source_identity}",
        "status": "materialized",
        "decision": decision,
        "field_name": field_name,
        "corrected_value": corrected_value or {},
        "evidence_payload": evidence_payload if evidence_payload is not None else {"page": 1, "quoted_text": "evidence"},
        "confidence": confidence,
        "candidate": MagicMock(),
    }
    if adjudication_role:
        audit["adjudication_role"] = adjudication_role
    if material:
        audit["material"] = material
    return audit


def test_settle_third_ai_priority_over_pass_reject_conflict():
    """When PASS + REJECT conflict AND a third_ai adjudication exists, the
    third_ai branch must fire — NOT the has_reject+has_positive → need_third_ai
    branch.  The third_ai check sits above the decision-conflict check in the
    priority ladder."""
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
            source_identity="ai_a",
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV"},
        ),
        _make_audit(
            source_identity="ai_b",
            decision="REJECT",
            corrected_value={"value": -0.95, "unit": "eV"},
        ),
        _make_audit(
            source_identity="adjudicator",
            decision="REJECT",
            adjudication_role="third_ai",
            corrected_value={"value": -0.95, "unit": "eV"},
            confidence=0.95,
        ),
    ]

    service = _make_settle_service()
    # Mock the reject-all path so we don't need a real DFTResultReviewService.
    service._apply_reject_all = MagicMock(
        return_value={
            "action": "audit_opinion_rejected",
            "target_type": "dft_results",
            "result": {"status": "audit_opinion_rejected"},
            "writes_final_truth": False,
        }
    )

    result = service._settle_dft_row_from_existing_audits(
        row=row,
        audits=audits,
        reviewer="test_reviewer",
        write_lock_tokens=None,
    )

    assert result["status"] == "audit_consensus_ready"
    assert result["writes_final_truth"] is False
    assert result.get("reason") != "decision_conflict"
    service._apply_reject_all.assert_called_once()


def test_settle_missing_evidence_anchor_priority_over_waiting_second_ai():
    """When opinions exist but NONE have an evidence anchor, the settle
    function must return need_repair / missing_evidence_anchor — NOT
    waiting_second_ai (which would fire if anchored < 2 but the anchor
    check comes first in the priority ladder)."""
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
            source_identity="ai_a",
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV"},
            evidence_payload={},  # no anchor keys
        ),
        _make_audit(
            source_identity="ai_b",
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV"},
            evidence_payload={},  # no anchor keys either
        ),
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


def test_settle_dft_reviews_dedupes_same_source_identity_before_two_ai_gate():
    row = DFTResult(
        id=uuid4(),
        paper_id=uuid4(),
        property_type="adsorption_energy",
        adsorbate="Li2S4",
        value=-0.95,
        unit="eV",
    )
    audits = [
        _make_audit(
            source_identity="same_ai",
            candidate_id="submission-1",
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV", "material_identity": "CoN4"},
            confidence=0.7,
        ),
        _make_audit(
            source_identity="same_ai",
            candidate_id="submission-2",
            decision="PASS",
            corrected_value={"value": -0.95, "unit": "eV", "material_identity": "CoN4"},
            confidence=0.9,
        ),
    ]

    service = _make_settle_service()
    result = service._settle_dft_row_from_existing_audits(
        row=row,
        audits=audits,
        reviewer="test_reviewer",
        write_lock_tokens=None,
    )

    assert result["status"] == "waiting_second_ai"
    assert result["reason"] == "awaiting_two_ai_reviews"
    assert result["eligible_opinion_count"] == 1


def test_settle_whole_row_proposal_without_supporting_pass_returns_value_conflict():
    """A whole-row PROPOSED opinion with no supporting PASS (values differ)
    must return need_third_ai / value_conflict.  It must NOT fall through to
    the same_field_consensus branch, because the whole_row check returns
    early."""
    row = DFTResult(
        id=uuid4(),
        paper_id=uuid4(),
        property_type="adsorption_energy",
        adsorbate="H",
        value=0.75,
        unit="eV",
    )
    audits = [
        _make_audit(
            source_identity="ai_a",
            decision="PROPOSED",
            field_name="dft_results",
            corrected_value={
                "property_type": "adsorption_energy",
                "adsorbate": "H",
                "value": 0.95,
                "unit": "eV",
            },
            confidence=0.9,
        ),
        _make_audit(
            source_identity="ai_b",
            decision="PASS",
            field_name="dft_results",
            corrected_value={
                "property_type": "adsorption_energy",
                "adsorbate": "H",
                "value": 0.80,  # different value → no supporting pass
                "unit": "eV",
            },
            confidence=0.85,
        ),
    ]

    service = _make_settle_service()
    result = service._settle_dft_row_from_existing_audits(
        row=row,
        audits=audits,
        reviewer="test_reviewer",
        write_lock_tokens=None,
    )

    assert result["status"] == "need_third_ai"
    assert result["reason"] == "value_conflict"


def test_settle_incompatible_material_identity_returns_need_repair_not_auto_apply():
    """Two anchored PASS opinions with matching values but incompatible
    material identities must return need_repair / material_identity_conflict
    — NOT auto_applied.  The material-identity check sits inside the
    pass-consensus branch and short-circuits before any apply call."""
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
            source_identity="ai_a",
            decision="PASS",
            field_name="dft_results",
            corrected_value={
                "property_type": "adsorption_energy",
                "adsorbate": "H",
                "value": -0.95,
                "unit": "eV",
                "material_identity": "CoN3",
            },
            material="CoN3",
            confidence=0.9,
        ),
        _make_audit(
            source_identity="ai_b",
            decision="PASS",
            field_name="dft_results",
            corrected_value={
                "property_type": "adsorption_energy",
                "adsorbate": "H",
                "value": -0.95,
                "unit": "eV",
                "material_identity": "FeN4",
            },
            material="FeN4",  # incompatible with CoN3
            confidence=0.88,
        ),
    ]

    service = _make_settle_service()
    # If the material-identity guard fails, _apply_dft_consensus_outcome
    # would be called.  Wire it to fail the test if reached.
    service._apply_dft_consensus_outcome = MagicMock(
        side_effect=AssertionError("material_identity_conflict should short-circuit before apply")
    )

    result = service._settle_dft_row_from_existing_audits(
        row=row,
        audits=audits,
        reviewer="test_reviewer",
        write_lock_tokens=None,
    )

    assert result["status"] == "need_repair"
    assert result["reason"] == "material_identity_conflict"
    service._apply_dft_consensus_outcome.assert_not_called()
