from __future__ import annotations

import os

import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    AuditLog,
    Base,
    CatalystSample,
    DFTAuditIssue,
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
from app.services.dft_audit_issue_service import DFTAuditIssueService
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
        "",
        "point",
        "v",
        "c2h4",
        "limiting potential via *co -> *co+*co",
    )
    assert VerificationSessionService._new_dft_semantic_signature(base) == (
        VerificationSessionService._new_dft_semantic_signature(same_science_different_locator)
    )


def test_new_dft_materialization_keeps_method_only_and_specific_v2_subjects_distinct(verification_env):
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

        assert [item["action"] for item in result["materialized_items"]] == ["created", "created"]
        assert len(dft_rows) == 2
        assert len({row.subject_key for row in dft_rows}) == 2
        assert len({row.observation_key for row in dft_rows}) == 2
        assert {row.reaction_step for row in dft_rows} == {
            "DFT-D2 GGA-PBE",
            "Li2S adsorption on WN4@G side",
        }
        assert all(row.catalyst_sample_id is not None for row in dft_rows)
        assert {session.get(CatalystSample, row.catalyst_sample_id).name for row in dft_rows} == {"WN4@G/TiS2"}
        assert len({candidate.materialized_target_id for candidate in candidates}) == 2


def test_new_dft_materialization_candidate_ids_filter_is_exact(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Exact candidate filter", pdf_path="filter.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="filter-test")
        session.add(run)
        session.flush()

        def candidate(material: str, value: float) -> ExternalAnalysisCandidate:
            return ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                status="candidate",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": "new",
                    "decision": "new_candidate",
                    "corrected_value": {
                        "material_identity": material,
                        "property_type": "adsorption_energy",
                        "adsorbate": "Li2S",
                        "value": value,
                        "unit": "eV",
                    },
                    "evidence_location": {
                        "source_document_type": "main_text",
                        "page": 7,
                        "quoted_text": f"{material}: {value} eV",
                    },
                },
            )

        selected = candidate("selected", -1.1)
        untouched = candidate("untouched", -1.2)
        session.add_all([selected, untouched])
        session.flush()

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
            candidate_ids={selected.id},
        )

        assert result["materialized_count"] == 1
        assert result["materialized_items"][0]["candidate_id"] == str(selected.id)
        assert session.get(ExternalAnalysisCandidate, selected.id).status == "materialized"
        untouched = session.get(ExternalAnalysisCandidate, untouched.id)
        assert untouched.status == "candidate"
        assert untouched.materialized_target_id is None
        assert session.scalar(
            select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)
        ) == 1

        empty = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
            candidate_ids=set(),
        )
        assert empty == {
            "materialized_count": 0,
            "materialized_items": [],
            "skipped_count": 0,
            "skipped_items": [],
        }


def test_new_dft_materialization_rejects_missing_pdf_page_anchor(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Missing PDF anchor paper", pdf_path="missing-anchor.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="pdf-anchor-test")
        session.add(run)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "FePc@WS2",
                    "adsorbate": "Li2S4",
                    "property_type": "pdos_overlap_energy_window",
                    "value": -2.5,
                    "value_upper": -0.5,
                    "unit": "eV",
                },
                "evidence_location": {"quoted_text": "PDOS overlaps from -2.5 to -0.5 eV."},
            },
            status="candidate",
        )
        session.add(candidate)
        session.flush()

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [{"candidate_id": str(candidate.id), "reason": "missing_pdf_evidence_anchor"}]
        assert session.scalar(select(DFTResult).where(DFTResult.paper_id == paper.id)) is None
        stored = session.get(ExternalAnalysisCandidate, candidate.id)
        assert stored.status == "rejected_by_local_ai"
        assert stored.mapping_reason == "missing_pdf_evidence_anchor"


def test_new_dft_materialization_persists_range_and_pdf_locator(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Range DFT candidate paper", pdf_path="range.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="range-test")
        session.add(run)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "FePc@WS2",
                    "adsorbate": "Li2S4",
                    "property_type": "pdos_overlap_energy_window",
                    "value": -2.5,
                    "value_upper": -0.5,
                    "unit": "eV",
                },
                "evidence_location": {
                    "source_document_type": "main_text",
                    "page": 15,
                    "figure": "fig_5",
                    "quoted_text": "PDOS overlaps from -2.5 to -0.5 eV.",
                },
            },
            status="candidate",
        )
        session.add(candidate)
        session.flush()

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )
        row = session.scalar(select(DFTResult).where(DFTResult.paper_id == paper.id))
        locator = session.scalar(select(EvidenceLocator).where(EvidenceLocator.target_id == str(row.id)))

        assert result["materialized_count"] == 1
        assert row.value == -2.5
        assert row.value_upper == -0.5
        assert row.value_kind == "energy_window"
        assert row.source_figure == "fig_5"
        assert row.evidence_payload["page"] == 15
        assert locator is not None
        assert locator.page == 15
        assert locator.locator_status == "exact_page"


def test_interval_candidate_reuses_same_existing_row_and_conflicts_on_different_upper(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Interval identity materialization", pdf_path="interval.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="FePc@WS2", catalyst_type="unknown")
        session.add(sample)
        session.flush()
        existing = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            adsorbate="Li2S4",
            property_type="pdos_overlap_energy_window",
            value=-2.5,
            value_upper=-0.5,
            value_kind="energy_window",
            unit="eV",
            candidate_status="new_candidate",
            evidence_payload={"material_identity": "FePc@WS2", "page": 7},
        )
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="interval-identity")
        session.add_all([existing, run])
        session.flush()

        def add_candidate(upper: float, page: int, value_kind: str):
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": "new",
                    "decision": "new_candidate",
                    "corrected_value": {
                        "material_identity": "FePc@WS2",
                        "adsorbate": "Li2S4",
                        "property_type": "pdos_overlap_energy_window",
                        "value": "-2.5000",
                        "value_upper": upper,
                        "value_kind": value_kind,
                        "unit": "eV",
                    },
                    "evidence_location": {"page": page, "quoted_text": f"PDOS interval ends at {upper} eV"},
                },
                status="candidate",
            )
            session.add(candidate)
            session.flush()
            return candidate

        exact = add_candidate(-0.5, 8, "Energy-Window")
        conflicting = add_candidate(-0.4, 9, "energy_window")
        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        assert result["materialized_items"] == [
            {
                **result["materialized_items"][0],
                "candidate_id": str(exact.id),
                "action": "deduplicated",
                "dft_result_id": str(existing.id),
            }
        ]
        assert result["skipped_items"] == [
            {"candidate_id": str(conflicting.id), "reason": "conflicting_dft_observation_for_subject"}
        ]
        assert session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)) == 1
        assert exact.materialized_target_id == str(existing.id)
        assert conflicting.materialized_target_id is None
        assert conflicting.status == "requires_resolution"


def _atomic_binding_payload(*, material: str = "New-Material", value: float = -3.21) -> dict:
    return {
        "target_type": "dft_results",
        "target_id": "new",
        "decision": "new_candidate",
        "corrected_value": {
            "material_identity": material,
            "adsorbate": "Li2S6",
            "property_type": "adsorption_energy",
            "reaction_step": "Li2S6 adsorption",
            "value": value,
            "unit": "eV",
        },
        "evidence_location": {"page": 11, "quoted_text": f"{material} Li2S6 {value} eV"},
    }


def _atomic_binding_counts(session, paper_id):
    return (
        session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)),
        session.scalar(select(func.count(EvidenceLocator.id)).where(EvidenceLocator.paper_id == paper_id)),
        session.scalar(select(func.count(CatalystSample.id)).where(CatalystSample.paper_id == paper_id)),
    )


@pytest.mark.parametrize("bound_side", ["issue", "candidate"])
def test_materialization_binding_conflict_rolls_back_all_dft_side_effects(verification_env, bound_side):
    Session = verification_env
    with Session() as session:
        paper = Paper(title=f"Atomic conflict {bound_side}", pdf_path="atomic-conflict.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        original_sample = CatalystSample(paper_id=paper.id, name="Original-Material", catalyst_type="unknown")
        session.add(original_sample)
        session.flush()
        original = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=original_sample.id,
            property_type="reaction_barrier",
            value=0.61,
            unit="eV",
            candidate_status="system_candidate",
            evidence_payload={"material_identity": "Original-Material", "page": 3},
        )
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label=f"atomic-{bound_side}")
        session.add_all([original, run])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(original.id),
                field_name="value",
                page=3,
                evidence_text="Original evidence",
                locator_status="exact_page",
            )
        )
        payload = _atomic_binding_payload()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=payload,
            status="candidate",
            materialized_target_type="dft_results" if bound_side == "candidate" else None,
            materialized_target_id=str(original.id) if bound_side == "candidate" else None,
        )
        session.add(candidate)
        session.flush()
        fingerprint = DFTAuditIssueService(session).fingerprint_missing_issue(
            paper_id=paper.id,
            payload=payload,
            candidate_id=str(candidate.id),
        )
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(original.id) if bound_side == "issue" else "new",
            issue_type="missing_dft_result",
            severity="high",
            status="fixed_by_primary_ai" if bound_side == "issue" else "needs_primary_ai",
            fingerprint=fingerprint,
        )
        session.add(issue)
        session.flush()
        before = _atomic_binding_counts(session, paper.id)

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        expected_reason = (
            "dft_audit_issue_bound_to_different_result"
            if bound_side == "issue"
            else "dft_candidate_bound_to_different_result"
        )
        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [{"candidate_id": str(candidate.id), "reason": expected_reason}]
        assert _atomic_binding_counts(session, paper.id) == before
        assert candidate.materialized_target_id == (str(original.id) if bound_side == "candidate" else None)
        assert candidate.status == "requires_resolution"
        assert issue.target_id == (str(original.id) if bound_side == "issue" else "new")
        assert issue.status == "needs_user_decision"


def test_materialization_same_candidate_and_issue_target_is_idempotent(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Atomic same target", pdf_path="atomic-same.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="New-Material", catalyst_type="unknown")
        session.add(sample)
        session.flush()
        payload = _atomic_binding_payload()
        existing = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            adsorbate="Li2S6",
            property_type="adsorption_energy",
            reaction_step="Li2S6 adsorption",
            value=-3.21,
            value_kind="point",
            unit="eV",
            candidate_status="new_candidate",
            evidence_payload={"material_identity": "New-Material", "page": 11},
        )
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="atomic-same")
        session.add_all([existing, run])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(existing.id),
                field_name="value",
                page=11,
                evidence_text="Existing evidence",
                locator_status="exact_page",
            )
        )
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=payload,
            status="requires_resolution",
            materialized_target_type="dft_results",
            materialized_target_id=str(existing.id),
        )
        session.add(candidate)
        session.flush()
        fingerprint = DFTAuditIssueService(session).fingerprint_missing_issue(
            paper_id=paper.id,
            payload=payload,
            candidate_id=str(candidate.id),
        )
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(existing.id),
            issue_type="missing_dft_result",
            severity="high",
            status="fixed_by_primary_ai",
            fingerprint=fingerprint,
        )
        session.add(issue)
        session.flush()
        before = _atomic_binding_counts(session, paper.id)

        first = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )
        after_first = _atomic_binding_counts(session, paper.id)
        second = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        assert first["materialized_items"][0]["action"] == "deduplicated"
        assert first["materialized_items"][0]["dft_result_id"] == str(existing.id)
        assert second["materialized_count"] == 0
        assert before == after_first == _atomic_binding_counts(session, paper.id)
        assert candidate.materialized_target_id == str(existing.id)
        assert candidate.status == "materialized"
        assert issue.target_id == str(existing.id)
        assert issue.status == "fixed_by_primary_ai"


def test_bond_candidate_materialization_keeps_atom_subjects_and_reports_value_conflict(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Bond identity candidates", pdf_path="bond-identity.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="bond-identity")
        session.add(run)
        session.flush()

        def add_candidate(*, value: float, pair_key: str | None, pair: str | None, page: int):
            corrected = {
                "material_identity": "Fe-GDY",
                "property_type": "bond_length",
                "value": value,
                "unit": "Å",
            }
            if pair_key and pair:
                corrected[pair_key] = pair
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": "new",
                    "decision": "new_candidate",
                    "corrected_value": corrected,
                    "evidence_location": {"page": page, "quoted_text": f"{pair or 'unspecified pair'} {value} Å"},
                },
                status="candidate",
            )
            session.add(candidate)
            session.flush()
            return candidate

        li1 = add_candidate(value=2.1, pair_key="atom_pair", pair="Li1-S", page=4)
        li1_reversed = add_candidate(value=2.1, pair_key="bond", pair="S–Li1", page=5)
        li1_conflict = add_candidate(value=2.2, pair_key="bond_pair", pair="Li1-S", page=6)
        li2 = add_candidate(value=2.1, pair_key="interaction_pair", pair="Li2-S", page=7)
        missing_first = add_candidate(value=2.1, pair_key=None, pair=None, page=8)
        missing_second = add_candidate(value=2.1, pair_key=None, pair=None, page=9)

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        assert [item["action"] for item in result["materialized_items"]] == [
            "created",
            "deduplicated",
            "created",
            "created",
            "created",
        ]
        assert result["skipped_items"] == [
            {"candidate_id": str(li1_conflict.id), "reason": "conflicting_dft_observation_for_subject"}
        ]
        assert len(rows) == 4
        assert li1.materialized_target_id == li1_reversed.materialized_target_id
        assert li2.materialized_target_id != li1.materialized_target_id
        assert missing_first.materialized_target_id != missing_second.materialized_target_id
        assert li1_conflict.status == "requires_resolution"
        assert li1_conflict.mapping_reason == "conflicting_dft_observation_for_subject"


def test_materialized_missing_issue_stays_open_until_ai_verification_and_closed_issue_is_not_reopened(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Materialized issue lifecycle", pdf_path="materialized-issue.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="issue-lifecycle")
        session.add(run)
        session.flush()
        payload = {
            "target_type": "dft_results",
            "target_id": "new",
            "decision": "new_candidate",
            "corrected_value": {
                "material_identity": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.1,
                "unit": "eV",
            },
            "evidence_location": {"page": 5, "quoted_text": "Fe-GDY Li2S4 adsorption -1.10 eV"},
        }
        first_candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=payload,
            status="candidate",
        )
        session.add(first_candidate)
        session.flush()

        materialized = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )
        row = session.get(DFTResult, UUID(materialized["materialized_items"][0]["dft_result_id"]))
        issue = session.scalar(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id))

        assert issue.target_id == str(row.id)
        assert issue.status == "fixed_by_primary_ai"
        assert issue.resolved_at is None

        verified = DFTResultReviewService(session).verify_result(
            paper_id=paper.id,
            result_id=row.id,
            confirm_reviewed_against_pdf=True,
            reviewer="pytest-ai",
            evidence_payload=payload["evidence_location"],
            verification_actor_type="ai",
            source_label="pytest-local-ai",
            commit=False,
        )
        assert verified["closed_audit_issue_ids"] == [str(issue.id)]
        assert issue.status == "closed"
        assert row.candidate_status == "ai_verified_ml_ready"

        second_payload = {**payload, "evidence_location": {"page": 12, "quoted_text": payload["evidence_location"]["quoted_text"]}}
        second_candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=second_payload,
            status="candidate",
        )
        session.add(second_candidate)
        session.flush()
        repeated = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )

        assert repeated["materialized_count"] == 0
        assert repeated["skipped_items"] == [
            {"candidate_id": str(second_candidate.id), "reason": "terminal_dft_audit_issue"}
        ]
        assert second_candidate.status == "candidate"
        assert second_candidate.materialized_target_type is None
        assert second_candidate.materialized_target_id is None
        assert issue.status == "closed"
        assert issue.resolution_note == "ai_verified"


def test_new_dft_materialization_does_not_revive_rejected_exact_match(verification_env):
    Session = verification_env
    with Session() as session:
        paper = Paper(title="Rejected exact DFT", pdf_path="rejected-exact.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        existing = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="Li2S4 adsorption",
            value=-1.1,
            unit="eV",
            candidate_status="Rejected",
            evidence_payload={"material_identity": "Fe-GDY", "page": 4},
        )
        session.add(existing)
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="rejected-exact")
        session.add(run)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "dft_results",
                "target_id": "new",
                "decision": "new_candidate",
                "corrected_value": {
                    "material_identity": "Fe-GDY",
                    "property_type": "adsorption_energy",
                    "adsorbate": "Li2S4",
                    "reaction_step": "adsorption of Li2S4",
                    "value": -1.1,
                    "unit": "eV",
                },
                "evidence_location": {"page": 8, "quoted_text": "Fe-GDY Li2S4 -1.10 eV"},
            },
            status="candidate",
        )
        session.add(candidate)
        session.flush()

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )
        issue = session.scalar(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id))

        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [
            {"candidate_id": str(candidate.id), "reason": "exact_dedupe_target_rejected"}
        ]
        assert candidate.status == "requires_resolution"
        assert candidate.materialized_target_id is None
        assert issue.status == "needs_user_decision"
        assert existing.candidate_status == "Rejected"
        assert len(session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()) == 1


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

    assert result["status"] == "rejected"
    assert result["reason"] == "missing_pdf_evidence_anchor"
