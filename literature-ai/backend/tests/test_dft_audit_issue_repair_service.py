from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTAuditIssue, DFTResult, ExtractionFieldReview, Paper
from app.mcp.context import mcp_auth_context
from app.mcp.server import (
    get_dft_audit_issues,
    mcp_server,
    repair_dft_audit_issue,
    repair_dft_audit_issues_batch,
)
from app.services.dft_audit_issue_service import DFTAuditIssueService


def _review_auth() -> str:
    return "test-primary-repair-key"


def _review_dft_auth() -> str:
    return "test-audit-only-key"


def _review_corrections_auth() -> str:
    return "test-correction-only-key"


def _read_auth() -> str:
    return "test-reader-key"


def _propose_auth() -> str:
    return "test-propose-only-key"


def _paper(session: Session, title: str = "DFT issue repair paper") -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf")
    session.add(paper)
    session.flush()
    return paper


def _missing_issue(
    session: Session,
    paper: Paper,
    *,
    evidence: dict | None = None,
    suffix: str = "",
) -> DFTAuditIssue:
    material_identity = f"Fe-GDY{suffix}"
    return DFTAuditIssueService(session).upsert_issue(
        paper_id=paper.id,
        target_id="new",
        issue_type="missing_dft_result",
        status="needs_primary_ai",
        fingerprint=f"missing-{paper.id}{suffix}",
        suggested_dft={
            "material_identity": material_identity,
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
            "quoted_text": f"{material_identity} Li2S4 adsorption energy is -1.10 eV.",
        },
        source_identity="audit_ai",
        source_candidate_id=f"candidate-1{suffix}",
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

    assert {"get_dft_audit_issues", "repair_dft_audit_issue", "repair_dft_audit_issues_batch"} <= set(by_name)
    assert set(by_name["get_dft_audit_issues"].inputSchema["required"]) == {"paper_id"}
    assert set(by_name["repair_dft_audit_issue"].inputSchema["required"]) == {
        "issue_id",
        "action",
        "repair_payload",
        "reason",
        "evidence_payload",
    }
    assert set(by_name["repair_dft_audit_issues_batch"].inputSchema["required"]) == {"paper_id"}


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


@pytest.mark.parametrize("auth_factory", [_read_auth, _review_corrections_auth])
def test_repair_dft_audit_issue_requires_dft_write_capability(setup_test_db, auth_factory):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Repair capability denied")
        issue = _missing_issue(session, paper)
        session.commit()
        issue_id = str(issue.id)

    with mcp_auth_context(auth_factory()):
        with pytest.raises(PermissionError):
            repair_dft_audit_issue(
                issue_id=issue_id,
                action="create_missing_dft",
                repair_payload={},
                reason="should not be allowed",
                evidence_payload={},
            )


@pytest.mark.parametrize("auth_factory", [_review_auth, _review_dft_auth, _propose_auth])
def test_repair_dft_audit_issue_accepts_existing_dft_write_identity(setup_test_db, auth_factory):
    with Session(setup_test_db) as session:
        paper = _paper(session, f"Fast repair {auth_factory.__name__}")
        issue = _missing_issue(session, paper)
        session.commit()
        issue_id = str(issue.id)

    with mcp_auth_context(auth_factory()):
        result = repair_dft_audit_issue(
            issue_id=issue_id,
            action="create_missing_dft",
            repair_payload={},
            reason="fast processing",
            evidence_payload={},
        )

    assert result["status"] == "created"


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
        assert issue.target_type == "dft_results"
        assert issue.target_id == str(rows[0].id)
        assert issue.resolved_by is None
        assert issue.resolved_at is None
        assert audit is not None
        assert audit.action == "repair_dft_audit_issue"
        assert audit.payload["writes_final_truth"] is False
        assert audit.payload["required_capability"] == "repair_dft_issues"
        assert audit.payload["capability_used"] == "repair_dft_issues"
        assert audit.payload["actor_role"] == "dft_fast_processor"
        assert audit.payload["source_prefix"] == "primary_ai"
        assert audit.payload["repair_actor"] == {
            "source_prefix": "primary_ai",
            "actor_role": "dft_fast_processor",
            "required_capability": "repair_dft_issues",
        }
        assert "test" not in str(audit.payload)


def test_batch_fast_path_repairs_and_finalizes_all_missing_issues(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Batch fast DFT")
        first = _missing_issue(session, paper, suffix="-1")
        second = _missing_issue(session, paper, suffix="-2")
        other_paper = _paper(session, "Batch fast DFT other paper")
        other_issue = _missing_issue(session, other_paper, suffix="-other")
        session.commit()
        paper_id = paper.id
        issue_ids = {first.id, second.id}
        other_issue_id = other_issue.id

    with mcp_auth_context(_propose_auth()):
        result = repair_dft_audit_issues_batch(
            paper_id=str(paper_id),
            auto_finalize=True,
        )

    assert result["requested_count"] == 2
    assert result["processed_count"] == 2
    assert result["finalized_count"] == 2
    assert result["failed_count"] == 0
    assert result["capability_used"] == "propose_corrections"

    with Session(setup_test_db) as session:
        issues = [session.get(DFTAuditIssue, issue_id) for issue_id in issue_ids]
        untouched_other_issue = session.get(DFTAuditIssue, other_issue_id)
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        reviews = session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)
        ).all()

        assert len(rows) == 2
        assert all(issue is not None and issue.status == "closed" for issue in issues)
        assert untouched_other_issue is not None
        assert untouched_other_issue.status == "needs_primary_ai"
        assert all(row.candidate_status in {"ML_Ready", "human_reviewed_needs_evidence"} for row in rows)
        assert reviews
        assert all(review.reviewer_status == "verified" for review in reviews)


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
        existing = DFTResult(
            paper_id=paper.id,
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
    assert result["changed_fields"] == ["catalyst_sample_id"]
    assert result["material_binding"]["status"] == "bound"
    with Session(setup_test_db) as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        samples = session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
        assert len(rows) == 1
        assert len(samples) == 1
        assert samples[0].name == "Fe-GDY"
        assert rows[0].catalyst_sample_id == samples[0].id


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
        assert issue.resolved_by is None
        assert issue.resolved_at is None


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


def test_link_existing_duplicate_marks_issue_without_writing_dft_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Link duplicate issue")
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
        issue = _targeted_issue(session, paper, row, issue_type="duplicate_suspected")
        session.commit()
        issue_id = str(issue.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        result = repair_dft_audit_issue(
            issue_id,
            "link_existing_duplicate",
            {"dft_result_id": row_id},
            "Same row already exists.",
            {},
        )

    assert result["status"] == "linked_duplicate"
    assert result["dft_result_id"] == row_id
    assert result["writes_final_truth"] is False
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, UUID(row_id))
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert row.candidate_status == "system_candidate"
        assert issue.status == "fixed_by_primary_ai"


def test_mark_needs_user_decision_does_not_write_resolved_metadata(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Mark issue")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.0, unit="eV")
        session.add(row)
        session.flush()
        needs_user = _targeted_issue(session, paper, row, issue_type="uncertain")
        session.commit()
        needs_user_id = str(needs_user.id)
        row_id = str(row.id)

    with mcp_auth_context(_review_auth()):
        user_result = repair_dft_audit_issue(
            needs_user_id,
            "mark_needs_user_decision",
            {},
            "Conflicting source scope needs user decision.",
            {},
        )

    assert user_result["status"] == "needs_user_decision"
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, UUID(row_id))
        needs_user = session.get(DFTAuditIssue, UUID(needs_user_id))
        assert row.candidate_status == "system_candidate"
        assert needs_user.status == "needs_user_decision"
        assert needs_user.resolved_by is None
        assert needs_user.resolved_at is None


def test_primary_repair_ai_cannot_mark_false_positive(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "False positive human only")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.0, unit="eV")
        session.add(row)
        session.flush()
        issue = _targeted_issue(session, paper, row, issue_type="duplicate_suspected")
        session.commit()
        issue_id = str(issue.id)

    with mcp_auth_context(_review_auth()):
        with pytest.raises(ValueError, match="Unsupported DFT audit issue repair action"):
            repair_dft_audit_issue(
                issue_id,
                "mark_false_positive",
                {},
                "The duplicate suspicion was caused by a repeated table caption.",
                {},
            )

    with Session(setup_test_db) as session:
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert issue.status == "needs_primary_ai"
        assert issue.resolved_by is None
        assert issue.resolved_at is None
