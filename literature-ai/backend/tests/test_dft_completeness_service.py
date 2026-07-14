from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.db.models import (
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.services.dft_completeness_service import DFTCompletenessService
from app.services.dft_review_bundle_service import DFTReviewBundleService


def _paper(session: Session, title: str) -> Paper:
    paper = Paper(title=title, paper_code=f"T{uuid4().hex[:7].upper()}", pdf_path="paper.pdf")
    session.add(paper)
    session.flush()
    return paper


def _new_candidate_payload() -> dict:
    return {
        "target_type": "dft_results",
        "target_id": "new",
        "decision": "new_candidate",
        "corrected_value": {
            "material_identity": "Fe-N-C",
            "property_type": "adsorption_energy",
            "adsorbate": "Li2S4",
            "value": -1.2,
            "unit": "eV",
        },
    }


def _complete_scope_snapshot() -> dict:
    return {
        "source_pdf_inventory": [
            {
                "paper_id": "main",
                "paper_code": "TMAIN",
                "role": "main",
                "pdf_available": True,
                "included_in_bundle": True,
            },
            {
                "paper_id": "si",
                "paper_code": "TSI",
                "role": "si",
                "pdf_available": True,
                "included_in_bundle": True,
            },
        ],
        "review_gate": {
            "stage_status": "completed",
            "rag_quality_status": "ready",
            "current_snapshot_fingerprint": "chart-current",
            "completed_snapshot_fingerprint": "chart-current",
        },
        "source_snapshot_fingerprint": "bundle-current",
    }


def _gap_run(session: Session, paper: Paper, *, bundle: str = "bundle-current") -> ExternalAnalysisRun:
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="local_ai",
        source_label="completeness-test",
        mapping_status="mapped",
        raw_payload={
            "review_metadata": {
                "review_mode": "comprehensive_review",
                "overall_status": "completed",
                "bundle_fingerprint": bundle,
                "figure_table_completed_snapshot_fingerprint": "chart-current",
            },
            "coverage_acknowledgement": {"missing_data_search_complete": True},
        },
    )
    session.add(run)
    session.flush()
    return run


def test_ai_applied_candidates_use_existing_finalized_status_contract(setup_test_db):
    candidate_count = 370
    with Session(setup_test_db) as session:
        paper = _paper(session, "Finalized candidate scale")
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="local_ai",
            source_label="finalized-scale",
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        rows = [
            DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                adsorbate=f"Li2S4-{index}",
                value=-float(index + 1),
                unit="eV",
                candidate_status="ai_verified_ml_ready",
            )
            for index in range(candidate_count)
        ]
        session.add_all(rows)
        session.flush()
        candidates = []
        issues = []
        for index, row in enumerate(rows):
            candidate = ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload=_new_candidate_payload(),
                status="ai_applied",
                materialized_target_type="dft_results",
                materialized_target_id=str(row.id),
            )
            issue = DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                result_id=row.id,
                issue_type="missing_dft_result",
                severity="high",
                status="closed",
                fingerprint=f"finalized-{index}",
                resolution_code="verified",
            )
            candidates.append(candidate)
            issues.append(issue)
        session.add_all([*candidates, *issues])
        session.flush()
        session.add_all(
            [
                DFTAuditIssueSource(issue_id=issue.id, candidate_id=candidate.id)
                for issue, candidate in zip(issues, candidates, strict=True)
            ]
        )
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert lifecycle["lifecycle_counts"]["discovered_candidate_count"] == candidate_count
        assert lifecycle["lifecycle_counts"]["unhandled_candidate_count"] == 0
        assert lifecycle["lifecycle_counts"]["materialized_unbound_count"] == 0
        assert lifecycle["lifecycle_blockers"] == []


def test_closed_legacy_candidate_binding_remains_valid_without_junction_backfill(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Legacy candidate binding")
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", mapping_status="mapped")
        session.add(run)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            candidate_status="ai_verified_ml_ready",
        )
        session.add(row)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_new_candidate_payload(),
            status="ai_applied",
            materialized_target_type="dft_results",
            materialized_target_id=str(row.id),
        )
        session.add(candidate)
        session.flush()
        session.add(
            DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                result_id=row.id,
                issue_type="missing_dft_result",
                severity="high",
                status="closed",
                fingerprint="legacy-source-candidate-ids",
                source_candidate_ids=[str(candidate.id)],
                resolution_code="verified",
            )
        )
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert lifecycle["lifecycle_blockers"] == []
        assert lifecycle["lifecycle_counts"]["materialized_unbound_count"] == 0


def test_partial_normalized_sources_do_not_fall_back_to_extra_json_ids(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Canonical normalized candidate binding")
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", mapping_status="mapped")
        session.add(run)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            candidate_status="ai_verified_ml_ready",
        )
        session.add(row)
        session.flush()
        candidates = [
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="object_review_audit",
                normalized_payload=_new_candidate_payload(),
                status="ai_applied",
                materialized_target_type="dft_results",
                materialized_target_id=str(row.id),
            )
            for _index in range(2)
        ]
        session.add_all(candidates)
        session.flush()
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            result_id=row.id,
            issue_type="missing_dft_result",
            severity="high",
            status="closed",
            fingerprint="partial-normalized-sources",
            source_candidate_ids=[str(candidate.id) for candidate in candidates],
            resolution_code="verified",
        )
        session.add(issue)
        session.flush()
        session.add(DFTAuditIssueSource(issue_id=issue.id, candidate_id=candidates[0].id))
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert lifecycle["lifecycle_counts"]["materialized_unbound_count"] == 1
        assert lifecycle["lifecycle_ids"]["materialized_unbound_candidate_ids"] == [
            str(candidates[1].id)
        ]
        assert "materialized_dft_candidates_unbound" in lifecycle["lifecycle_blockers"]


@pytest.mark.parametrize("status", ["pending", "candidate", "requires_resolution"])
def test_nonfinalized_candidate_status_remains_unhandled(setup_test_db, status):
    with Session(setup_test_db) as session:
        paper = _paper(session, f"Unhandled {status}")
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", mapping_status="mapped")
        session.add(run)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_new_candidate_payload(),
            status=status,
        )
        session.add(candidate)
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert lifecycle["lifecycle_counts"]["unhandled_candidate_count"] == 1
        assert lifecycle["lifecycle_ids"]["unhandled_candidate_ids"] == [str(candidate.id)]
        assert "unhandled_dft_candidates" in lifecycle["lifecycle_blockers"]


def test_ai_reviewed_new_candidate_requires_binding_or_explicit_nonmaterialized_disposition(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "AI reviewed disposition")
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", mapping_status="mapped")
        session.add(run)
        session.flush()
        unresolved = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_new_candidate_payload(),
            status="ai_reviewed",
        )
        rejected_payload = _new_candidate_payload()
        rejected_payload["decision"] = "REJECT"
        rejected = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=rejected_payload,
            status="ai_reviewed",
        )
        session.add_all([unresolved, rejected])
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert lifecycle["lifecycle_counts"]["unhandled_candidate_count"] == 1
        assert lifecycle["lifecycle_ids"]["unhandled_candidate_ids"] == [str(unresolved.id)]


def test_lifecycle_reports_each_required_blocker_with_ids(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Lifecycle blockers")
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", mapping_status="mapped")
        session.add(run)
        session.flush()
        unhandled = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_new_candidate_payload(),
            status="requires_resolution",
        )
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.2,
            unit="eV",
            candidate_status="ai_verified_ml_ready",
        )
        session.add_all([unhandled, row])
        session.flush()
        unbound = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_new_candidate_payload(),
            status="ai_applied",
            materialized_target_type="dft_results",
            materialized_target_id=str(row.id),
        )
        open_missing = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            result_id=row.id,
            issue_type="missing_dft_result",
            severity="high",
            status="needs_primary_ai",
            fingerprint="verified-open-missing",
        )
        conflict = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            result_id=row.id,
            issue_type="duplicate_suspected",
            severity="high",
            status="needs_user_decision",
            fingerprint="identity-conflict",
            lifecycle_stage="binding_conflict",
            resolution_code="scientific_conflict",
        )
        session.add_all([unbound, open_missing, conflict])
        session.flush()

        lifecycle = DFTCompletenessService(session)._evaluate_lifecycle(paper.id)

        assert set(lifecycle["lifecycle_blockers"]) == {
            "unhandled_dft_candidates",
            "materialized_dft_candidates_unbound",
            "verified_results_with_open_missing_dft_result",
            "identity_or_binding_conflicts_open",
        }
        assert lifecycle["lifecycle_counts"] == {
            "discovered_candidate_count": 2,
            "unhandled_candidate_count": 1,
            "materialized_unbound_count": 1,
            "verified_open_missing_count": 1,
            "identity_binding_conflict_count": 1,
        }


def test_completeness_is_true_only_when_lifecycle_and_review_scope_are_true(setup_test_db, monkeypatch):
    monkeypatch.setattr(
        DFTReviewBundleService,
        "get_completeness_snapshot",
        lambda self, paper_id: _complete_scope_snapshot(),
    )
    with Session(setup_test_db) as session:
        paper = _paper(session, "Complete paper")
        _gap_run(session, paper)
        result = DFTCompletenessService(session).evaluate(
            paper.id,
            exported_verified_rows=4,
            excluded_rows=2,
        )

        assert result["lifecycle_reconciled"] is True
        assert result["review_scope_complete"] is True
        assert result["is_complete"] is True
        assert result["exported_verified_rows"] == 4
        assert result["excluded_rows"] == 2
        assert result["source_snapshot_fingerprint"] == "bundle-current"


@pytest.mark.parametrize(
    ("mutation", "expected_blocker"),
    [
        ("missing_main", "main_source_pdf_missing"),
        ("missing_si", "linked_si_source_pdf_missing"),
        ("chart_stale", "chart_review_scope_stale"),
        ("missing_gap", "dft_gap_discovery_not_current"),
        ("snapshot_mismatch", "source_snapshot_mismatch"),
    ],
)
def test_review_scope_is_conservative_with_precise_blockers(setup_test_db, monkeypatch, mutation, expected_blocker):
    snapshot = _complete_scope_snapshot()
    if mutation == "missing_main":
        snapshot["source_pdf_inventory"][0]["pdf_available"] = False
        snapshot["source_pdf_inventory"][0]["included_in_bundle"] = False
    elif mutation == "missing_si":
        snapshot["source_pdf_inventory"][1]["pdf_available"] = False
        snapshot["source_pdf_inventory"][1]["included_in_bundle"] = False
    elif mutation == "chart_stale":
        snapshot["review_gate"]["completed_snapshot_fingerprint"] = "chart-old"
    monkeypatch.setattr(
        DFTReviewBundleService,
        "get_completeness_snapshot",
        lambda self, paper_id: snapshot,
    )
    with Session(setup_test_db) as session:
        paper = _paper(session, f"Scope blocker {mutation}")
        if mutation != "missing_gap":
            _gap_run(
                session,
                paper,
                bundle="bundle-old" if mutation == "snapshot_mismatch" else "bundle-current",
            )
        result = DFTCompletenessService(session).evaluate(paper.id)

        assert result["lifecycle_reconciled"] is True
        assert result["review_scope_complete"] is False
        assert result["is_complete"] is False
        assert expected_blocker in result["review_scope_blockers"]
