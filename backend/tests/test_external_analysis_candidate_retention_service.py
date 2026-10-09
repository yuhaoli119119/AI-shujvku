from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    WorkflowJob,
)
from app.main import app
from app.services.external_analysis_candidate_retention_service import (
    ExternalAnalysisCandidateRetentionService,
    ReferencedExternalAnalysisCandidateError,
)
from app.services.external_analysis_service import ExternalAnalysisService
from app.config import Settings


def _paper_run(session: Session, code: str) -> tuple[Paper, ExternalAnalysisRun]:
    paper = Paper(title=code, paper_code=code, pdf_path=f"{code}.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="local_ai",
        source_label=f"{code}-run",
        normalized_payload={"object_review_audits": []},
    )
    session.add(run)
    session.flush()
    return paper, run


def _candidate(session: Session, paper: Paper, run: ExternalAnalysisRun, *, target: str) -> ExternalAnalysisCandidate:
    row = ExternalAnalysisCandidate(
        paper_id=paper.id,
        run_id=run.id,
        candidate_type="object_review_audit",
        normalized_payload={
            "target_type": "dft_results",
            "target_id": target,
            "decision": "new_candidate",
            "corrected_value": {"value": 1.0, "unit": "eV", "property_type": "x", "material_identity": "M"},
        },
        status="ai_applied",
        materialized_target_type="dft_results",
        materialized_target_id=target,
    )
    session.add(row)
    session.flush()
    return row


def test_reference_scan_archives_referenced_and_deletes_only_unreferenced(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_run(session, "B9401")
        result = DFTResult(paper_id=paper.id, property_type="x", value=1.0, unit="eV")
        session.add(result)
        session.flush()
        referenced = _candidate(session, paper, run, target=str(result.id))
        unreferenced = _candidate(session, paper, run, target="new")
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(result.id),
            issue_type="missing_dft_result",
            status="closed",
            source_candidate_ids=[str(referenced.id)],
            current_snapshot={"source_candidate_ids": [str(referenced.id)]},
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        session.flush()
        session.add(DFTAuditIssueSource(issue_id=issue.id, candidate_id=referenced.id))
        result.evidence_payload = {"source_candidate_ids": [str(referenced.id)]}
        session.add(
            AuditLog(
                paper_id=None,
                action="apply_ide_review_rules",
                source="test",
                target_type="external_analysis_candidate",
                target_id=str(referenced.id),
                payload={"candidate_id": str(referenced.id)},
            )
        )
        session.add(
            WorkflowJob(
                job_id=f"candidate-retention-{uuid4().hex}",
                type="verification_session",
                status="completed",
                result={"consumed_candidate_ids": [str(referenced.id)]},
            )
        )
        original_json = list(issue.source_candidate_ids)
        referenced_id, unreferenced_id, issue_id = referenced.id, unreferenced.id, issue.id
        summary = ExternalAnalysisCandidateRetentionService(
            session
        ).archive_referenced_delete_unreferenced(
            [referenced, unreferenced],
            actor="test",
            reason="unit_test",
        )
        session.commit()

    assert summary["archived_referenced_candidates"] == 1
    assert summary["deleted_unreferenced_candidates"] == 1
    kinds = {row["kind"] for row in summary["references"][str(referenced_id)]}
    assert {
        "normalized_dft_issue_source",
        "legacy_dft_issue_json",
        "dft_result_evidence_json",
        "audit_log_target",
        "audit_log_payload",
        "workflow_job_json",
    } <= kinds
    with Session(setup_test_db) as session:
        archived = session.get(ExternalAnalysisCandidate, referenced_id)
        assert archived is not None and archived.archived_at is not None
        assert archived.status == "ai_applied"
        assert session.get(ExternalAnalysisCandidate, unreferenced_id) is None
        assert session.get(DFTAuditIssue, issue_id).source_candidate_ids == original_json


def test_delete_run_and_batch_refuse_referenced_candidates(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_run(session, "B9402")
        candidate = _candidate(session, paper, run, target="new")
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            source_candidate_ids=[str(candidate.id)],
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        session.commit()
        paper_id, run_id, candidate_id = paper.id, run.id, candidate.id

    with Session(setup_test_db) as session:
        service = ExternalAnalysisService(session, Settings())
        try:
            service.delete_run(run_id)
        except ReferencedExternalAnalysisCandidateError as exc:
            assert str(candidate_id) in exc.references
            session.rollback()
        else:
            raise AssertionError("referenced run deletion was not refused")
        try:
            service.delete_runs_for_paper_source(paper_id, "local_ai")
        except ReferencedExternalAnalysisCandidateError:
            session.rollback()
        else:
            raise AssertionError("referenced batch deletion was not refused")
        assert session.get(ExternalAnalysisRun, run_id) is not None


def test_delete_run_api_returns_structured_409(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_run(session, "B9403")
        candidate = _candidate(session, paper, run, target="new")
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="apply_ide_review_rules",
                source="test",
                payload={"candidate_id": str(candidate.id)},
            )
        )
        session.commit()
        run_id = run.id

    response = TestClient(app).delete(f"/api/external-analysis/runs/{run_id}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "external_analysis_candidates_are_referenced"
    assert detail["candidate_count"] == 1
    with Session(setup_test_db) as session:
        assert session.get(ExternalAnalysisRun, run_id) is not None


def test_reset_archives_referenced_candidate_and_reports_counts(setup_test_db):
    with Session(setup_test_db) as session:
        paper, run = _paper_run(session, "B9404")
        result = DFTResult(paper_id=paper.id, property_type="x", value=1.0, unit="eV")
        session.add(result)
        session.flush()
        referenced = _candidate(session, paper, run, target=str(result.id))
        unreferenced = _candidate(session, paper, run, target=str(result.id))
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(result.id),
            issue_type="missing_dft_result",
            source_candidate_ids=[str(referenced.id)],
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        session.commit()
        paper_id, referenced_id, unreferenced_id = paper.id, referenced.id, unreferenced.id

    response = TestClient(app).post(
        f"/api/papers/{paper_id}/dft-ai-reviews/reset",
        json={
            "confirm_reset_dft_ai_reviews": True,
            "reviewer": "test",
            "keep_dft_candidates": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["archived_referenced_candidates"] == 1
    assert payload["deleted_object_review_candidates"] == 1
    with Session(setup_test_db) as session:
        assert session.get(ExternalAnalysisCandidate, referenced_id).archived_at is not None
        assert session.get(ExternalAnalysisCandidate, unreferenced_id) is None
        assert session.scalar(
            select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper_id)
        ).source_candidate_ids == [str(referenced_id)]
