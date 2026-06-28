from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTAuditIssue, Paper, utcnow
from app.main import app
from app.services.dft_audit_report_service import DFTAuditReportService


def _paper(session: Session, title: str) -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf")
    session.add(paper)
    session.flush()
    return paper


def _issue(
    session: Session,
    paper: Paper,
    *,
    issue_type: str,
    status: str,
    created_offset_days: int = 0,
) -> DFTAuditIssue:
    issue = DFTAuditIssue(
        paper_id=paper.id,
        target_type="dft_results",
        target_id="new",
        issue_type=issue_type,
        status=status,
        fingerprint=f"{paper.id}-{issue_type}-{status}-{created_offset_days}",
        created_at=utcnow() + timedelta(days=created_offset_days),
        updated_at=utcnow() + timedelta(days=created_offset_days),
    )
    session.add(issue)
    session.flush()
    return issue


def _repair_log(
    session: Session,
    paper: Paper,
    issue: DFTAuditIssue,
    *,
    action: str = "create_missing_dft",
    source_prefix: str = "dft_primary_repair",
    actor_role: str = "primary_ai_repair",
    capability_used: str = "repair_dft_issues",
    writes_final_truth: bool = False,
    created_offset_days: int = 0,
) -> AuditLog:
    log = AuditLog(
        paper_id=paper.id,
        action="repair_dft_audit_issue",
        source=source_prefix,
        target_type="dft_audit_issues",
        target_id=str(issue.id),
        payload={
            "action": action,
            "writes_final_truth": writes_final_truth,
            "source_prefix": source_prefix,
            "actor_role": actor_role,
            "capability_used": capability_used,
        },
        created_at=utcnow() + timedelta(days=created_offset_days),
    )
    session.add(log)
    session.flush()
    return log


def test_dft_audit_report_empty_database_returns_zeroes(setup_test_db):
    with Session(setup_test_db) as session:
        report = DFTAuditReportService(session).build_report(mcp_api_keys="")

    assert report["issue_status_counts"] == {}
    assert report["issue_type_counts"] == {}
    assert report["open_needs_user_decision_count"] == 0
    assert report["open_needs_primary_ai_count"] == 0
    assert report["fixed_by_primary_ai_pending_review_count"] == 0
    assert report["repair_action_counts"] == {}
    assert report["repair_actor_counts"] == []
    assert report["repair_issue_type_counts"] == {}
    assert report["repair_writes_final_truth_count"] == 0
    assert report["suspect_repair_actor_warnings"] == []
    assert report["mcp_capability_warnings"] == []


def test_dft_audit_report_counts_issues_repairs_and_actors(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "DFT audit report")
        missing = _issue(session, paper, issue_type="missing_dft_result", status="needs_primary_ai")
        _issue(session, paper, issue_type="wrong_value", status="needs_user_decision")
        _issue(session, paper, issue_type="wrong_value", status="fixed_by_primary_ai")
        _issue(session, paper, issue_type="wrong_unit", status="closed")
        _repair_log(session, paper, missing, action="create_missing_dft")
        _repair_log(session, paper, missing, action="update_dft_fields", source_prefix="lab_primary_repair")
        session.commit()

        report = DFTAuditReportService(session).build_report(
            paper_id=paper.id,
            include_closed=False,
            mcp_api_keys="dft_primary_repair|DFT Primary Repair AI|litmcp_x|read_papers,repair_dft_issues",
        )

    assert report["issue_status_counts"] == {
        "fixed_by_primary_ai": 1,
        "needs_primary_ai": 1,
        "needs_user_decision": 1,
    }
    assert report["issue_type_counts"] == {"missing_dft_result": 1, "wrong_value": 2}
    assert report["open_needs_primary_ai_count"] == 1
    assert report["open_needs_user_decision_count"] == 1
    assert report["fixed_by_primary_ai_pending_review_count"] == 1
    assert report["repair_action_counts"] == {"create_missing_dft": 1, "update_dft_fields": 1}
    assert report["repair_issue_type_counts"] == {"missing_dft_result": 2}
    assert {
        (item["source_prefix"], item["actor_role"], item["capability_used"], item["count"])
        for item in report["repair_actor_counts"]
    } == {
        ("dft_primary_repair", "primary_ai_repair", "repair_dft_issues", 1),
        ("lab_primary_repair", "primary_ai_repair", "repair_dft_issues", 1),
    }
    assert report["repair_writes_final_truth_count"] == 0
    assert report["suspect_repair_actor_warnings"] == []
    assert report["mcp_capability_warnings"] == []


def test_dft_audit_report_flags_suspect_repair_payloads_without_raw_key(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Suspect repair report")
        issue = _issue(session, paper, issue_type="wrong_value", status="fixed_by_primary_ai")
        _repair_log(
            session,
            paper,
            issue,
            action="update_dft_fields",
            source_prefix="assigned_dft_audit",
            actor_role="dft_auditor",
            capability_used="review_dft",
            writes_final_truth=True,
        )
        session.commit()

        report = DFTAuditReportService(session).build_report(paper_id=paper.id)

    assert report["repair_writes_final_truth_count"] == 1
    warning_codes = {item["code"] for item in report["suspect_repair_actor_warnings"]}
    assert warning_codes == {
        "repair_writes_final_truth",
        "unexpected_repair_capability",
        "unexpected_repair_actor_role",
    }
    assert "review_dft" in str(report["suspect_repair_actor_warnings"])
    assert "raw_key" not in str(report["suspect_repair_actor_warnings"])
    assert "litmcp" not in str(report["suspect_repair_actor_warnings"])


def test_dft_audit_report_filters_by_paper_and_days(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Current paper")
        other = _paper(session, "Other paper")
        current_issue = _issue(session, paper, issue_type="missing_dft_result", status="needs_primary_ai")
        old_issue = _issue(session, paper, issue_type="wrong_value", status="needs_user_decision", created_offset_days=-40)
        _issue(session, other, issue_type="wrong_unit", status="needs_primary_ai")
        _repair_log(session, paper, current_issue, action="create_missing_dft")
        _repair_log(session, paper, old_issue, action="update_dft_fields", created_offset_days=-40)
        session.commit()

        report = DFTAuditReportService(session).build_report(paper_id=paper.id, days=30)

    assert report["issue_status_counts"] == {"needs_primary_ai": 1}
    assert report["issue_type_counts"] == {"missing_dft_result": 1}
    assert report["repair_action_counts"] == {"create_missing_dft": 1}
    assert report["repair_issue_type_counts"] == {"missing_dft_result": 1}


def test_dft_audit_report_include_closed_and_mcp_capability_warnings(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Closed warning report")
        _issue(session, paper, issue_type="wrong_unit", status="closed")
        session.commit()

        closed_report = DFTAuditReportService(session).build_report(
            paper_id=paper.id,
            include_closed=True,
            mcp_api_keys=(
                "assigned_dft_audit|Assigned DFT Audit|litmcp_audit_secret|"
                "read_papers,repair_dft_issues"
            ),
        )
        open_report = DFTAuditReportService(session).build_report(paper_id=paper.id, include_closed=False)

    assert closed_report["issue_status_counts"] == {"closed": 1}
    assert open_report["issue_status_counts"] == {}
    assert len(closed_report["mcp_capability_warnings"]) == 1
    assert closed_report["mcp_capability_warnings"][0]["source_prefix"] == "assigned_dft_audit"
    assert "litmcp_audit_secret" not in str(closed_report["mcp_capability_warnings"])


def test_dft_audit_report_api_is_read_only_and_returns_payload(setup_test_db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "dft_primary_repair|DFT Primary Repair AI|litmcp_x|read_papers,repair_dft_issues",
    )
    get_settings.cache_clear()
    with Session(setup_test_db) as session:
        paper = _paper(session, "API report")
        _issue(session, paper, issue_type="missing_dft_result", status="needs_primary_ai")
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    response = client.get(f"/api/dft/audit-report?paper_id={paper_id}&days=30")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "dft_audit_report_v1"
    assert payload["filters"]["paper_id"] == paper_id
    assert payload["issue_status_counts"] == {"needs_primary_ai": 1}
    assert payload["mcp_capability_warnings"] == []
    get_settings.cache_clear()
