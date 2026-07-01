from __future__ import annotations

import asyncio
import csv
import io
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.papers.aggregation import export_dft_results_csv
from app.db.models import AuditLog, CatalystSample, DFTAuditIssue, DFTResult, EvidenceSpan, ExtractionFieldReview, Paper
from app.main import app
from app.mcp.context import mcp_auth_context
from app.mcp.server import (
    get_dft_audit_issues,
    repair_dft_audit_issue,
    repair_dft_audit_issues_batch,
    verify_dft_results_batch,
)
from app.rag.eligibility import is_rag_eligible
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_export_service import build_dft_ml_dataset
from app.services.dft_review_service import DFTResultReviewService


def _primary_repair_auth() -> str:
    return "test-primary-repair-e2e-key"


def _audit_only_auth() -> str:
    return "test-audit-only-e2e-key"


def _paper(session: Session, title: str) -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf", authors=["DFT E2E"])
    session.add(paper)
    session.flush()
    return paper


def _catalyst(session: Session, paper: Paper, name: str = "Fe-GDY") -> CatalystSample:
    sample = CatalystSample(
        paper_id=paper.id,
        name=name,
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        coordination="Fe-N4",
        support="graphdiyne",
    )
    session.add(sample)
    session.flush()
    return sample


def _dft_row(session: Session, paper: Paper, *, status: str = "system_candidate") -> DFTResult:
    sample = _catalyst(session, paper)
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=sample.id,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        reaction_step="Li2S4 adsorption",
        value=-1.20,
        unit="eV",
        evidence_text="Table 1 reports Fe-GDY Li2S4 adsorption energy is -1.20 eV.",
        candidate_status=status,
    )
    session.add(row)
    session.flush()
    session.add(
        EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(row.id),
            text=row.evidence_text,
            page=5,
        )
    )
    session.flush()
    return row


def _missing_issue(session: Session, paper: Paper) -> DFTAuditIssue:
    return DFTAuditIssueService(session).upsert_issue(
        paper_id=paper.id,
        target_id="new",
        issue_type="missing_dft_result",
        status="needs_primary_ai",
        fingerprint=f"e2e-missing-{paper.id}",
        suggested_dft={
            "material_identity": "Fe-GDY",
            "property_type": "adsorption_energy",
            "adsorbate": "Li2S4",
            "reaction_step": "Li2S4 adsorption",
            "value": -1.10,
            "unit": "eV",
        },
        evidence_payload={
            "source_document_type": "main_text",
            "page": 5,
            "table": "Table 1",
            "quoted_text": "Fe-GDY Li2S4 adsorption energy is -1.10 eV.",
        },
        source_identity="assigned_dft_audit",
        source_candidate_id="candidate-e2e-missing",
    )


def _targeted_issue(
    session: Session,
    paper: Paper,
    row: DFTResult,
    *,
    issue_type: str = "wrong_value",
    fingerprint: str | None = None,
) -> DFTAuditIssue:
    service = DFTAuditIssueService(session)
    return service.upsert_issue(
        paper_id=paper.id,
        target_id=str(row.id),
        issue_type=issue_type,
        status="needs_primary_ai",
        fingerprint=fingerprint or f"e2e-{issue_type}-{row.id}",
        current_snapshot=service.snapshot_dft_result(row),
        suggested_value={"value": -1.20, "unit": "eV"},
        evidence_payload={"source_document_type": "main_text", "page": 5, "quoted_text": "-1.20 eV"},
        source_identity="assigned_dft_audit",
        source_candidate_id=f"candidate-e2e-{issue_type}",
    )


def _safe_review(session: Session, paper: Paper, row: DFTResult) -> ExtractionFieldReview:
    review = ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name="value",
        reviewer_status="verified",
        target_resolution_status="active",
        evidence_text=row.evidence_text,
        review_payload={
            "human_verification": {
                "reviewer": "historical_human",
                "decision": "verified",
                "writes_final_truth": True,
            }
        },
    )
    session.add(review)
    session.flush()
    return review


async def _response_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8-sig")


def _export_rows(session: Session):
    response = asyncio.run(
        export_dft_results_csv(
            property_type=None,
            adsorbate=None,
            year_min=None,
            year_max=None,
            session=session,
        )
    )
    text = asyncio.run(_response_text(response))
    return response, list(csv.DictReader(io.StringIO(text)))


def test_missing_issue_fast_processing_closes_issue_and_opens_export_gate(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "DFT lifecycle missing to verify")
        issue = _missing_issue(session, paper)
        session.commit()
        paper_id = paper.id
        issue_id = str(issue.id)

    with mcp_auth_context(_audit_only_auth()):
        issues_payload = get_dft_audit_issues(str(paper_id))
    assert issues_payload["count"] == 1
    assert issues_payload["items"][0]["id"] == issue_id
    assert issues_payload["items"][0]["target_id"] == "new"
    assert issues_payload["items"][0]["live_snapshot"] is None

    with mcp_auth_context(_audit_only_auth()):
        fast_result = repair_dft_audit_issues_batch(
            paper_id=str(paper_id),
            auto_finalize=True,
        )

    assert fast_result["requested_count"] == 1
    assert fast_result["processed_count"] == 1
    assert fast_result["finalized_count"] == 1
    assert fast_result["failed_count"] == 0
    assert fast_result["capability_used"] == "review_dft"
    row_id = UUID(fast_result["items"][0]["dft_result_id"])
    with mcp_auth_context(_audit_only_auth()):
        repeated_verify = verify_dft_results_batch(
            paper_id=str(paper_id),
            dft_result_ids=[str(row_id)],
            confirm_reviewed_against_pdf=True,
            reviewer_note="Repeated fast verification automatically uses current write versions.",
        )
    assert repeated_verify["verified"] == 1
    assert repeated_verify["skipped"] == 0

    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        repair_logs = session.scalars(select(AuditLog).where(AuditLog.action == "repair_dft_audit_issue")).all()
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)).all()
        verify_logs = session.scalars(select(AuditLog).where(AuditLog.action == "verify_dft_result")).all()
        response, rows = _export_rows(session)
        dataset = build_dft_ml_dataset(session)

        assert row is not None
        assert row.candidate_status == "ML_Ready"
        assert issue.status == "closed"
        assert issue.target_id == str(row.id)
        assert issue.resolved_by == "assigned_dft_audit"
        assert issue.resolved_at is not None
        assert reviews
        assert all(review.reviewer_status == "verified" for review in reviews)
        assert all(review.reviewer == "assigned_dft_audit" for review in reviews)
        assert all(log.payload["writes_final_truth"] is False for log in repair_logs)
        assert repair_logs[0].payload["capability_used"] == "review_dft"
        assert any(log.payload["closed_audit_issue_ids"] == [issue_id] for log in verify_logs)
        assert response.headers["x-d1-exported-count"] == "1"
        assert rows[0]["paper_id"] == str(paper_id)
        assert rows[0]["value"] == "-1.1"
        assert dataset["metadata"]["eligible_count"] == 1
        assert dataset["records"][0]["record_id"] == str(row_id)
        assert is_rag_eligible(session, row, "dft_result") is True


def test_human_reject_closes_issue_and_blocks_export_even_with_historical_safe_review(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "DFT lifecycle reject")
        row = _dft_row(session, paper, status="system_candidate")
        historical_safe_rejected = _dft_row(session, paper, status="Rejected")
        _safe_review(session, paper, historical_safe_rejected)
        wrong_unit = _targeted_issue(session, paper, row, issue_type="wrong_unit", fingerprint="e2e-reject-wrong-unit")
        negative = _targeted_issue(
            session,
            paper,
            row,
            issue_type="negative_consensus",
            fingerprint="e2e-reject-negative-consensus",
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id
        historical_row_id = historical_safe_rejected.id
        issue_ids = {str(wrong_unit.id), str(negative.id)}

    with Session(setup_test_db) as session:
        reject_result = DFTResultReviewService(session).reject_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reject_candidate=True,
            reviewer="human_reviewer",
            reviewer_note="Human rejected the DFT row after checking the source table.",
            field_names=["value"],
        )
        current_review = session.scalar(
            select(ExtractionFieldReview)
            .where(ExtractionFieldReview.paper_id == paper_id)
            .where(ExtractionFieldReview.target_id == str(row_id))
            .where(ExtractionFieldReview.field_name == "value")
        )
        second_reject = DFTResultReviewService(session).reject_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reject_candidate=True,
            reviewer="human_reviewer",
            reviewer_note="Repeated reject should not create new issue closures.",
            field_names=["value"],
            expected_write_version=current_review.write_version,
        )

    assert set(reject_result["closed_audit_issue_ids"]) == issue_ids
    assert second_reject["closed_audit_issue_ids"] == []
    assert reject_result["export_safety"]["is_exportable"] is False
    assert "target_rejected" in reject_result["export_safety"]["blocked_reasons"]
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
        historical_row = session.get(DFTResult, historical_row_id)
        issues = session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper_id)).all()
        response, rows = _export_rows(session)
        dataset = build_dft_ml_dataset(session)
        reject_log = session.scalar(select(AuditLog).where(AuditLog.action == "reject_dft_result"))

        assert row.candidate_status == "Rejected"
        assert historical_row.candidate_status == "Rejected"
        assert all(issue.status == "closed" for issue in issues)
        assert {issue.resolution_note for issue in issues} == {"target_rejected"}
        assert {issue.resolved_by for issue in issues} == {"human_reviewer"}
        assert reject_log.payload["closed_audit_issue_ids"]
        assert rows == []
        assert response.headers["x-d1-exported-count"] == "0"
        assert "target_rejected" in response.headers["x-d1-blocked-reasons"]
        assert dataset["records"] == []
        assert dataset["metadata"]["blocked_reasons"]["target_rejected"] == 2
        assert is_rag_eligible(session, row, "dft_result") is False
        assert is_rag_eligible(session, historical_row, "dft_result") is False


def test_stale_issue_blocks_primary_repair_and_verify_does_not_close_uncertain_or_duplicate(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "DFT lifecycle stale and nonclosing")
        row = _dft_row(session, paper)
        wrong_value = _targeted_issue(session, paper, row, issue_type="wrong_value", fingerprint="e2e-stale-wrong")
        duplicate = _targeted_issue(session, paper, row, issue_type="duplicate_suspected", fingerprint="e2e-duplicate")
        uncertain = _targeted_issue(session, paper, row, issue_type="uncertain", fingerprint="e2e-uncertain")
        row.value = -0.95
        session.add(row)
        session.commit()
        paper_id = paper.id
        row_id = row.id
        wrong_value_id = str(wrong_value.id)
        duplicate_id = str(duplicate.id)
        uncertain_id = str(uncertain.id)

    response = TestClient(app).get(f"/api/dft/audit-issues?paper_id={paper_id}")
    assert response.status_code == 200
    wrong_item = next(item for item in response.json()["items"] if item["id"] == wrong_value_id)
    assert wrong_item["is_stale"] is True
    assert "value" in wrong_item["stale_fields"]
    assert wrong_item["live_snapshot"]["value"] == -0.95

    with mcp_auth_context(_primary_repair_auth()):
        first = repair_dft_audit_issue(
            wrong_value_id,
            "update_dft_fields",
            {"fields": {"value": -1.20}},
            "Primary repair attempted stale update.",
            {"page": 5, "quoted_text": "-1.20 eV"},
        )
        second = repair_dft_audit_issue(
            wrong_value_id,
            "update_dft_fields",
            {"fields": {"value": -1.20}},
            "Repeated stale repair remains blocked.",
            {"page": 5, "quoted_text": "-1.20 eV"},
        )

    assert first["status"] == "stale_issue"
    assert second["status"] == "stale_issue"
    assert first["stale_fields"] == ["value"]
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
        wrong_value = session.get(DFTAuditIssue, UUID(wrong_value_id))
        assert row.value == -0.95
        assert row.candidate_status == "system_candidate"
        assert wrong_value.status == "needs_user_decision"

    with Session(setup_test_db) as session:
        verify_result = DFTResultReviewService(session).verify_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reviewed_against_pdf=True,
            reviewer="human_reviewer",
            reviewer_note="Human verified current value but non-eligible issues stay open.",
            field_names=["value"],
            evidence_payload={"page": 5, "quoted_text": "Current table value is -0.95 eV."},
        )

    assert set(verify_result["closed_audit_issue_ids"]) == {wrong_value_id}
    with Session(setup_test_db) as session:
        duplicate = session.get(DFTAuditIssue, UUID(duplicate_id))
        uncertain = session.get(DFTAuditIssue, UUID(uncertain_id))
        wrong_value = session.get(DFTAuditIssue, UUID(wrong_value_id))
        assert wrong_value.status == "closed"
        assert wrong_value.resolution_note == "human_verified"
        assert duplicate.status == "needs_primary_ai"
        assert duplicate.resolved_at is None
        assert uncertain.status == "needs_primary_ai"
        assert uncertain.resolved_at is None
