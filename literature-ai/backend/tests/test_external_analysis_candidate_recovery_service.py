from __future__ import annotations

from datetime import datetime, timedelta
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisCandidateRecovery,
    ExternalAnalysisRun,
    Paper,
)
from app.migrations.dft_audit_issue_source_backfill_v1 import upgrade as backfill_sources
from app.services.external_analysis_candidate_recovery_service import (
    ExternalAnalysisCandidateRecoveryError,
    ExternalAnalysisCandidateRecoveryService,
)


def _audit(value: float, material: str) -> dict:
    return {
        "target_type": "dft_results",
        "target_id": "new",
        "field_name": "dft_results",
        "decision": "new_candidate",
        "corrected_value": {
            "material_identity": material,
            "property_type": "adsorption_energy",
            "adsorbate": "Li2S",
            "value": value,
            "unit": "eV",
        },
        "evidence_location": {
            "source_document_type": "supplementary_information",
            "page": 4,
            "table": "Table S4",
            "quoted_text": f"{material}: {value}",
        },
        "confidence": 0.99,
        "reason": "original run payload",
    }


def _paper_with_history(
    session: Session,
    *,
    code: str,
    audits: list[dict],
) -> tuple[Paper, ExternalAnalysisRun, list[UUID], list[DFTAuditIssue]]:
    paper = Paper(title=code, paper_code=code, pdf_path=f"{code}.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    created_at = datetime(2026, 7, 1, 10, 0, 0)
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="web_ai",
        source_label=f"{code}-original-ai",
        source_identity="mcp:owner",
        source_identity_verified=True,
        raw_payload={"object_review_audits": audits},
        normalized_payload={"object_review_audits": audits},
        mapping_status="mapped",
        created_at=created_at,
    )
    session.add(run)
    session.flush()
    candidate_ids = [uuid4() for _ in audits]
    issues: list[DFTAuditIssue] = []
    events = []
    for index, (candidate_id, audit) in enumerate(zip(candidate_ids, audits, strict=True)):
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            suggested_dft={"raw_corrected_value": audit["corrected_value"]},
            evidence_payload=audit["evidence_location"],
            source_identities=["mcp:owner"],
            source_candidate_ids=[str(candidate_id)],
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        issues.append(issue)
        events.append(
            {
                "action": "apply_imported_dft_opinion",
                "candidate_id": str(candidate_id),
                "candidate_status": "ai_applied",
                "decision": "NEW_CANDIDATE",
                "record_id": str(uuid4()),
                "property_type": "adsorption_energy",
                "result": {
                    "source_label": run.source_label,
                    "nested_readback": {"candidate_id": str(candidate_id)},
                },
            }
        )
    session.add(
        AuditLog(
            paper_id=paper.id,
            action="apply_ide_review_rules",
            source="owner",
            payload={
                "dft_settlement": {"auto_applied_items": events},
                "object_reviews": {"applied_items": events},
            },
            created_at=created_at + timedelta(seconds=1),
        )
    )
    session.flush()
    return paper, run, candidate_ids, issues


def test_recovers_multiple_exact_uuids_then_007_backfills_and_second_apply_is_noop(setup_test_db):
    audits = [_audit(-1.1, "Fe-N4"), _audit(-2.2, "V-N4")]
    with Session(setup_test_db) as session:
        paper, run, candidate_ids, issues = _paper_with_history(
            session,
            code="B9501",
            audits=audits,
        )
        paper_id, run_id = paper.id, run.id
        original_json = {issue.id: list(issue.source_candidate_ids) for issue in issues}
        session.commit()

    with Session(setup_test_db) as session:
        service = ExternalAnalysisCandidateRecoveryService(session)
        dry_run = service.public_analyze([paper_id])
        assert dry_run["missing_candidates"] == 2
        assert dry_run["recoverable_candidates"] == 2
        assert dry_run["conflict_count"] == 0
        applied = service.apply([paper_id], actor="test", reason="unit recovery")
        session.commit()
    assert applied["database_writes"] == 2

    with setup_test_db.begin() as connection:
        source_report = backfill_sources(connection, paper_id=paper_id)
    assert source_report["inserted_relations"] == 2

    with Session(setup_test_db) as session:
        rows = session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.id.in_(candidate_ids))
            .order_by(ExternalAnalysisCandidate.id)
        ).all()
        assert len(rows) == 2
        assert {row.run_id for row in rows} == {run_id}
        assert {row.status for row in rows} == {"candidate"}
        assert all(row.materialized_target_type is None for row in rows)
        assert all(row.materialized_target_id is None for row in rows)
        assert {
            row.normalized_payload["corrected_value"]["value"] for row in rows
        } == {-1.1, -2.2}
        assert session.query(ExternalAnalysisCandidateRecovery).count() == 2
        assert session.query(DFTAuditIssueSource).count() == 2
        for issue_id, values in original_json.items():
            assert session.get(DFTAuditIssue, issue_id).source_candidate_ids == values

        second = ExternalAnalysisCandidateRecoveryService(session).apply(
            [paper_id], actor="test", reason="unit recovery"
        )
        session.commit()
        assert second["database_writes"] == 0
        assert second["summary_audit_log_rows"] == 0
        assert session.query(ExternalAnalysisCandidateRecovery).count() == 2


def test_duplicate_source_payload_blocks_whole_paper_without_writes(setup_test_db):
    audit = _audit(-1.5, "Fe-N4")
    with Session(setup_test_db) as session:
        paper, run, candidate_ids, _issues = _paper_with_history(
            session,
            code="B9502",
            audits=[audit],
        )
        run.normalized_payload = {"object_review_audits": [audit, audit]}
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        service = ExternalAnalysisCandidateRecoveryService(session)
        report = service.public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert report["errors"][0]["reason"] == "source_run_payload_match_not_unique"
        with pytest.raises(ExternalAnalysisCandidateRecoveryError):
            service.apply([paper_id], actor="test", reason="must block")
        session.rollback()
        assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None
        assert session.query(ExternalAnalysisCandidateRecovery).count() == 0


def test_existing_different_candidate_for_same_run_audit_is_conflict(setup_test_db):
    audit = _audit(-1.7, "Fe-N4")
    with Session(setup_test_db) as session:
        paper, run, candidate_ids, _issues = _paper_with_history(
            session,
            code="B9503",
            audits=[audit],
        )
        from app.services.external_analysis_candidates import build_object_review_candidate_values
        from app.services.external_analysis_models import ExternalObjectReviewAuditModel

        session.add(
            ExternalAnalysisCandidate(
                **build_object_review_candidate_values(
                    run,
                    ExternalObjectReviewAuditModel.model_validate(audit),
                )
            )
        )
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert report["errors"][0]["reason"] == "source_audit_already_has_different_candidate"
        assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None


def test_injected_mid_recovery_failure_rolls_back_candidates_and_audit(setup_test_db):
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, _issues = _paper_with_history(
            session,
            code="B9504",
            audits=[_audit(-1.0, "A"), _audit(-2.0, "B")],
        )
        paper_id = paper.id
        session.commit()

    with pytest.raises(RuntimeError, match="fault_after_candidate_recovery_insert"):
        with Session(setup_test_db) as session:
            ExternalAnalysisCandidateRecoveryService(session).apply(
                [paper_id],
                actor="test",
                reason="rollback test",
                fault_after_insert=1,
            )
            session.commit()
    with Session(setup_test_db) as session:
        assert session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.id.in_(candidate_ids))
        ).all() == []
        assert session.query(ExternalAnalysisCandidateRecovery).count() == 0
        assert session.scalar(
            select(AuditLog).where(AuditLog.action == "recover_external_analysis_candidates")
        ) is None


def test_one_candidate_referenced_by_multiple_issues_blocks_whole_paper(setup_test_db):
    audit = _audit(-1.3, "Fe-N4")
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, issues = _paper_with_history(
            session,
            code="B9505",
            audits=[audit],
        )
        duplicate = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            suggested_dft=deepcopy(issues[0].suggested_dft),
            evidence_payload=deepcopy(issues[0].evidence_payload),
            source_identities=["mcp:owner"],
            source_candidate_ids=[str(candidate_ids[0])],
            fingerprint=uuid4().hex,
        )
        session.add(duplicate)
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert report["errors"][0]["reason"] == "candidate_referenced_by_multiple_issues"
        assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None


def test_one_source_audit_reused_by_multiple_candidate_ids_blocks_whole_paper(setup_test_db):
    audit = _audit(-1.4, "Fe-N4")
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, issues = _paper_with_history(
            session,
            code="B9506",
            audits=[audit],
        )
        second_candidate_id = uuid4()
        issues[0].source_candidate_ids = [str(candidate_ids[0]), str(second_candidate_id)]
        log = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper.id))
        payload = deepcopy(log.payload)
        second_event = deepcopy(payload["dft_settlement"]["auto_applied_items"][0])
        second_event["candidate_id"] = str(second_candidate_id)
        payload["dft_settlement"]["auto_applied_items"].append(second_event)
        payload["object_reviews"]["applied_items"].append(deepcopy(second_event))
        log.payload = payload
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert any(
            error["reason"] == "source_audit_reused_by_multiple_candidate_ids"
            for error in report["errors"]
        )
        assert session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.id.in_([candidate_ids[0], second_candidate_id])
            )
        ).all() == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("value", -9.9), ("unit", "kJ/mol")],
)
def test_scientific_value_or_unit_conflict_blocks_recovery(
    setup_test_db,
    field,
    replacement,
):
    audit = _audit(-1.6, "Fe-N4")
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, issues = _paper_with_history(
            session,
            code=f"B95{field[:2].upper()}7",
            audits=[audit],
        )
        changed = deepcopy(issues[0].suggested_dft)
        changed["raw_corrected_value"][field] = replacement
        issues[0].suggested_dft = changed
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert report["errors"][0]["reason"] == "source_run_payload_match_not_unique"
        assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None


def test_evidence_or_source_identity_conflict_blocks_recovery(setup_test_db):
    for suffix, mutate, expected_reason in (
        (
            "evidence",
            lambda issue: setattr(
                issue,
                "evidence_payload",
                {**issue.evidence_payload, "page": 999, "evidence_ids": ["si:missing:999"]},
            ),
            "source_evidence_mismatch",
        ),
        (
            "identity",
            lambda issue: setattr(issue, "source_identities", ["mcp:different-owner"]),
            "source_identity_conflict",
        ),
    ):
        with Session(setup_test_db) as session:
            paper, _run, candidate_ids, issues = _paper_with_history(
                session,
                code=f"B95{suffix[:2].upper()}8",
                audits=[_audit(-1.8, "Fe-N4")],
            )
            mutate(issues[0])
            paper_id = paper.id
            session.commit()
        with Session(setup_test_db) as session:
            report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
            assert report["status"] == "blocked"
            assert report["errors"][0]["reason"] == expected_reason
            assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None


def test_existing_historical_materialized_target_restores_applied_state(setup_test_db):
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, _issues = _paper_with_history(
            session,
            code="B9509",
            audits=[_audit(-2.1, "Fe-N4")],
        )
        log = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper.id))
        record_id = UUID(log.payload["dft_settlement"]["auto_applied_items"][0]["record_id"])
        session.add(
            DFTResult(
                id=record_id,
                paper_id=paper.id,
                property_type="adsorption_energy",
                value=-2.1,
                unit="eV",
            )
        )
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        applied = ExternalAnalysisCandidateRecoveryService(session).apply(
            [paper_id], actor="test", reason="existing target"
        )
        session.commit()
        restored = session.get(ExternalAnalysisCandidate, candidate_ids[0])
        assert applied["database_writes"] == 1
        assert restored.status == "ai_applied"
        assert restored.materialized_target_type == "dft_results"
        assert restored.materialized_target_id == str(record_id)


def test_closed_issue_without_real_target_blocks_recovery(setup_test_db):
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, issues = _paper_with_history(
            session,
            code="B9510",
            audits=[_audit(-2.3, "Fe-N4")],
        )
        issues[0].status = "closed"
        issues[0].target_id = str(uuid4())
        paper_id = paper.id
        session.commit()

    with Session(setup_test_db) as session:
        report = ExternalAnalysisCandidateRecoveryService(session).public_analyze([paper_id])
        assert report["status"] == "blocked"
        assert report["errors"][0]["reason"] == "closed_issue_result_missing"
        assert session.get(ExternalAnalysisCandidate, candidate_ids[0]) is None


def test_existing_recovery_state_reconciliation_is_audited_and_idempotent(setup_test_db):
    with Session(setup_test_db) as session:
        paper, _run, candidate_ids, _issues = _paper_with_history(
            session,
            code="B9511",
            audits=[_audit(-2.5, "Fe-N4")],
        )
        paper_id = paper.id
        ExternalAnalysisCandidateRecoveryService(session).apply(
            [paper_id], actor="test", reason="initial recovery"
        )
        candidate = session.get(ExternalAnalysisCandidate, candidate_ids[0])
        candidate.status = "ai_applied"
        candidate.materialized_target_type = "dft_results"
        candidate.materialized_target_id = str(uuid4())
        session.commit()

    with Session(setup_test_db) as session:
        service = ExternalAnalysisCandidateRecoveryService(session)
        dry_run = service.analyze_existing_states([paper_id])
        assert dry_run["status"] == "validated"
        assert dry_run["reconcile_count"] == 1
        applied = service.reconcile_existing_states(
            [paper_id], actor="test", reason="remove stale materialized target"
        )
        session.commit()
        assert applied["database_writes"] == 1

    with Session(setup_test_db) as session:
        candidate = session.get(ExternalAnalysisCandidate, candidate_ids[0])
        recovery = session.get(ExternalAnalysisCandidateRecovery, candidate_ids[0])
        assert candidate.status == "candidate"
        assert candidate.materialized_target_type is None
        assert candidate.materialized_target_id is None
        assert recovery.match_manifest["state_reconciliations"][-1]["before"][
            "status"
        ] == "ai_applied"
        assert session.scalar(
            select(AuditLog).where(
                AuditLog.action == "reconcile_external_analysis_candidate_recovery_state"
            )
        ) is not None
        second = ExternalAnalysisCandidateRecoveryService(session).reconcile_existing_states(
            [paper_id], actor="test", reason="repeat"
        )
        session.commit()
        assert second["database_writes"] == 0
        assert second["summary_audit_log_rows"] == 0
