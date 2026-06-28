from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTAuditIssue, DFTResult, ExtractionFieldReview, Paper
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import get_dft_audit_issues, mcp_server, repair_dft_audit_issue
from app.services.dft_audit_issue_service import DFTAuditIssueService


def _review_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="primary_ai",
        display_name="Primary AI",
        capabilities=frozenset({"read_papers", "review_dft"}),
        raw_key="test",
    )


def _read_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="reader",
        display_name="Reader",
        capabilities=frozenset({"read_papers"}),
        raw_key="test",
    )


def _paper(session: Session, title: str = "DFT issue repair paper") -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf")
    session.add(paper)
    session.flush()
    return paper


def _missing_issue(session: Session, paper: Paper, *, evidence: dict | None = None) -> DFTAuditIssue:
    return DFTAuditIssueService(session).upsert_issue(
        paper_id=paper.id,
        target_id="new",
        issue_type="missing_dft_result",
        status="needs_primary_ai",
        fingerprint=f"missing-{paper.id}",
        suggested_dft={
            "material_identity": "Fe-GDY",
            "property_type": "adsorption_energy",
            "adsorbate": "Li2S4",
            "reaction_step": "Li2S4 adsorption",
            "value": -1.1,
            "unit": "eV",
        },
        evidence_payload=evidence
        or {
            "source_document_type": "main_text",
            "page": 5,
            "table": "Table 1",
            "quoted_text": "Fe-GDY Li2S4 adsorption energy is -1.10 eV.",
        },
        source_identity="audit_ai",
        source_candidate_id="candidate-1",
    )


def _targeted_issue(session: Session, paper: Paper, row: DFTResult, issue_type: str = "wrong_value") -> DFTAuditIssue:
    return DFTAuditIssueService(session).upsert_issue(
        paper_id=paper.id,
        target_id=str(row.id),
        issue_type=issue_type,
        status="needs_primary_ai",
        fingerprint=f"{issue_type}-{row.id}",
        current_snapshot=DFTAuditIssueService.snapshot_dft_result(row),
        suggested_value={"value": -1.2, "unit": "eV"},
        evidence_payload={"source_document_type": "main_text", "page": 4, "quoted_text": "-1.20 eV"},
        source_identity="audit_ai",
        source_candidate_id="candidate-2",
    )


def test_dft_audit_issue_mcp_tools_are_registered_with_expected_contract():
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert {"get_dft_audit_issues", "repair_dft_audit_issue"} <= set(by_name)
    assert set(by_name["get_dft_audit_issues"].inputSchema["required"]) == {"paper_id"}
    assert set(by_name["repair_dft_audit_issue"].inputSchema["required"]) == {
        "issue_id",
        "action",
        "repair_payload",
        "reason",
        "evidence_payload",
    }


def test_get_dft_audit_issues_mcp_returns_read_only_issue_summary(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "MCP issue read")
        issue = _missing_issue(session, paper)
        session.commit()
        paper_id = str(paper.id)
        issue_id = str(issue.id)

    with mcp_auth_context(_read_auth()):
        payload = get_dft_audit_issues(paper_id=paper_id)

    assert payload["count"] == 1
    assert payload["items"][0]["issue_id"] == issue_id
    assert payload["items"][0]["issue_type"] == "missing_dft_result"
    assert payload["items"][0]["source_count"] == 1


def test_create_missing_dft_creates_sample_and_ai_candidate_without_human_verification(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Create missing DFT")
        issue = _missing_issue(session, paper)
        session.commit()
        issue_id = str(issue.id)
        paper_id = paper.id

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(
            issue_id=issue_id,
            action="create_missing_dft",
            repair_payload={},
            reason="Primary AI confirmed the table value against evidence.",
            evidence_payload={},
        )

    assert result["status"] == "created"
    with Session(setup_test_db) as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        samples = session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)).all()
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        audit = session.scalar(select(AuditLog).where(AuditLog.target_id == issue_id))

        assert len(rows) == 1
        assert len(samples) == 1
        assert samples[0].name == "Fe-GDY"
        assert rows[0].candidate_status == "ai_primary_applied"
        assert rows[0].candidate_status not in {"ML_Ready", "human_verified"}
        assert rows[0].evidence_payload["issue_id"] == issue_id
        assert rows[0].evidence_payload["source_candidate_ids"] == ["candidate-1"]
        assert reviews == []
        assert issue.status == "fixed_by_primary_ai"
        assert audit is not None
        assert audit.action == "repair_dft_audit_issue"
        assert audit.payload["writes_final_truth"] is False


def test_create_missing_dft_is_idempotent_for_same_issue(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Idempotent missing DFT")
        issue = _missing_issue(session, paper)
        session.commit()
        issue_id = str(issue.id)
        paper_id = paper.id

    with mcp_auth_context(_review_auth()):
        first = repair_dft_audit_issue(issue_id, "create_missing_dft", {}, "first repair", {})
        second = repair_dft_audit_issue(issue_id, "create_missing_dft", {}, "retry repair", {})

    assert first["status"] == "created"
    assert second["status"] == "linked_existing"
    assert first["dft_result_id"] == second["dft_result_id"]
    with Session(setup_test_db) as session:
        assert len(session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()) == 1
        assert len(session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()) == 1


def test_create_missing_dft_dedupes_generic_adsorption_step_against_existing_row(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Generic dedupe repair")
        sample = CatalystSample(paper_id=paper.id, name="Fe-GDY", catalyst_type="unknown")
        session.add(sample)
        session.flush()
        existing = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-1.1,
            unit="eV",
            candidate_status="system_candidate",
            evidence_payload={"material_identity": "Fe-GDY", "source_document_type": "main_text", "page": 4},
        )
        session.add(existing)
        session.flush()
        issue = _missing_issue(session, paper)
        session.commit()
        issue_id = str(issue.id)
        existing_id = str(existing.id)
        paper_id = paper.id

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(issue_id, "create_missing_dft", {}, "dedupe against existing", {})

    assert result["status"] == "linked_existing"
    assert result["dft_result_id"] == existing_id
    with Session(setup_test_db) as session:
        assert len(session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()) == 1


def test_supporting_reference_missing_repair_does_not_create_main_paper_dft(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Supporting reference repair")
        issue = _missing_issue(
            session,
            paper,
            evidence={
                "source_document_type": "supporting_reference",
                "page": 8,
                "quoted_text": "A cited paper reports -1.10 eV.",
            },
        )
        session.commit()
        issue_id = str(issue.id)
        paper_id = paper.id

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(issue_id, "create_missing_dft", {}, "supporting reference only", {})

    assert result["status"] == "needs_user_decision"
    assert result["reason"] == "supporting_reference_not_main_paper_data"
    with Session(setup_test_db) as session:
        assert session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all() == []
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert issue.status == "needs_user_decision"


def test_update_dft_fields_updates_whitelisted_fields_and_keeps_ai_status(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Update fields")
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-1.0,
            unit="eV",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        issue = _targeted_issue(session, paper, row)
        session.commit()
        issue_id = str(issue.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(
            issue_id,
            "update_dft_fields",
            {"fields": {"value": -1.2, "unit": "eV", "reaction_step": "Li2S4 adsorption"}},
            "Correct value from Table 1.",
            {"source_document_type": "main_text", "page": 4, "quoted_text": "-1.20 eV"},
        )

    assert result["status"] == "updated"
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, UUID(row_id))
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert row.value == -1.2
        assert row.reaction_step == "Li2S4 adsorption"
        assert row.candidate_status == "ai_primary_applied"
        assert row.candidate_status not in {"ML_Ready", "human_verified"}
        assert issue.status == "fixed_by_primary_ai"


def test_update_dft_fields_rejects_non_whitelisted_fields(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Forbidden update")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.0, unit="eV")
        session.add(row)
        session.flush()
        issue = _targeted_issue(session, paper, row)
        session.commit()
        issue_id = str(issue.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        with pytest.raises(ValueError, match="Unsupported DFT repair field"):
            repair_dft_audit_issue(
                issue_id,
                "update_dft_fields",
                {"fields": {"candidate_status": "ML_Ready"}},
                "bad update",
                {"page": 4, "quoted_text": "-1.20 eV"},
            )

    with Session(setup_test_db) as session:
        assert session.get(DFTResult, UUID(row_id)).candidate_status == "system_candidate"


def test_update_dft_fields_stale_snapshot_does_not_write(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Stale update")
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="adsorption",
            value=-1.0,
            unit="eV",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        issue = _targeted_issue(session, paper, row)
        row.value = -0.9
        session.add(row)
        session.commit()
        issue_id = str(issue.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(
            issue_id,
            "update_dft_fields",
            {"fields": {"value": -1.2}},
            "stale update attempt",
            {"page": 4, "quoted_text": "-1.20 eV"},
        )

    assert result["status"] == "stale_issue"
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, UUID(row_id))
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert row.value == -0.9
        assert row.candidate_status == "system_candidate"
        assert issue.status == "needs_user_decision"


def test_mark_needs_user_decision_and_false_positive_do_not_write_dft_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Mark issue")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.0, unit="eV")
        session.add(row)
        session.flush()
        needs_user = _targeted_issue(session, paper, row, issue_type="uncertain")
        false_positive = _targeted_issue(session, paper, row, issue_type="duplicate_suspected")
        session.commit()
        needs_user_id = str(needs_user.id)
        false_positive_id = str(false_positive.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        user_result = repair_dft_audit_issue(
            needs_user_id,
            "mark_needs_user_decision",
            {},
            "Conflicting source scope needs user decision.",
            {},
        )
        false_result = repair_dft_audit_issue(
            false_positive_id,
            "mark_false_positive",
            {},
            "The duplicate suspicion was caused by a repeated table caption.",
            {},
        )

    assert user_result["status"] == "needs_user_decision"
    assert false_result["status"] == "false_positive"
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, UUID(row_id))
        needs_user = session.get(DFTAuditIssue, UUID(needs_user_id))
        false_positive = session.get(DFTAuditIssue, UUID(false_positive_id))
        assert row.candidate_status == "system_candidate"
        assert needs_user.status == "needs_user_decision"
        assert false_positive.status == "false_positive"
