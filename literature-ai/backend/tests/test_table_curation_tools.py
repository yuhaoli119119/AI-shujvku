from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperTable,
)
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import (
    create_table,
    delete_table,
    mcp_server,
    merge_table,
    update_table,
)
from app.services.external_analysis_service import ExternalAnalysisService


TABLE_TOOLS = {"update_table", "create_table", "delete_table", "merge_table"}
EVIDENCE = {
    "page": 4,
    "table": "Table 1",
    "quoted_text": "Table 1 source evidence",
}


@pytest.fixture
def table_tool_env(monkeypatch):
    database_url = os.environ["LITAI_TEST_DATABASE_URL"]
    monkeypatch.setenv("LITAI_DATABASE_URL", database_url)
    get_settings.cache_clear()
    engine = create_engine(database_url, future=True)
    try:
        yield engine
    finally:
        from app.db.session import _engines, _session_factories

        for cached_engine in list(_engines.values()):
            cached_engine.dispose()
        _engines.clear()
        _session_factories.clear()
        engine.dispose()
        get_settings.cache_clear()


def _review_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="table_curator",
        display_name="Table Curator",
        capabilities=frozenset({"review_corrections"}),
        raw_key="table-curator-key",
    )


def _paper(session: Session, title: str) -> Paper:
    paper = Paper(title=title, authors=[], pdf_path=f"{title}.pdf")
    session.add(paper)
    session.flush()
    return paper


def _table(
    session: Session,
    paper: Paper,
    *,
    caption: str,
    markdown_content: str,
    page: int,
) -> PaperTable:
    table = PaperTable(
        paper_id=paper.id,
        caption=caption,
        markdown_content=markdown_content,
        page=page,
        extraction_source="docling",
    )
    session.add(table)
    session.flush()
    return table


def test_agent_guide_table_tools_are_registered_with_expected_contract():
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert TABLE_TOOLS <= set(by_name)

    schemas = {name: by_name[name].inputSchema for name in TABLE_TOOLS}
    assert set(schemas["update_table"]["required"]) == {
        "paper_id",
        "table_id",
        "updates",
        "reason",
        "evidence_payload",
    }
    assert set(schemas["create_table"]["required"]) == {
        "paper_id",
        "table",
        "reason",
        "evidence_payload",
    }
    assert set(schemas["delete_table"]["required"]) == {
        "paper_id",
        "table_id",
        "reason",
        "evidence_payload",
    }
    assert set(schemas["merge_table"]["required"]) == {
        "paper_id",
        "source_table_id",
        "target_table_id",
        "reason",
        "evidence_payload",
    }


def test_update_table_is_approved_audited_and_idempotent(table_tool_env):
    with Session(table_tool_env) as session:
        paper = _paper(session, "update-table")
        table = _table(
            session,
            paper,
            caption="Old caption",
            markdown_content="| old |",
            page=4,
        )
        session.commit()
        paper_id, table_id = paper.id, table.id

    with mcp_auth_context(_review_auth()):
        first = update_table(
            str(paper_id),
            str(table_id),
            {"caption": "New caption"},
            "Caption matches the PDF.",
            EVIDENCE,
        )
        second = update_table(
            str(paper_id),
            str(table_id),
            {"caption": "New caption"},
            "Caption matches the PDF.",
            EVIDENCE,
        )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["correction_ids"] == first["correction_ids"]
    assert second["audit_log_id"] == first["audit_log_id"]
    with Session(table_tool_env) as session:
        stored = session.get(PaperTable, table_id)
        corrections = session.scalars(
            select(PaperCorrection).where(PaperCorrection.paper_id == paper_id)
        ).all()
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "update_table")
        ).all()
        assert stored.caption == "New caption"
        assert len(corrections) == 1
        assert corrections[0].status == "approved"
        assert len(audits) == 1


def test_create_table_exact_retry_is_idempotent_but_same_caption_other_page_is_distinct(
    table_tool_env,
):
    with Session(table_tool_env) as session:
        paper = _paper(session, "create-table")
        session.commit()
        paper_id = paper.id

    first_payload = {
        "caption": "Table 1. Results",
        "markdown_content": "| A | 1 |",
        "page": 3,
        "extraction_source": "ide_ai",
    }
    second_payload = {**first_payload, "page": 4}
    with mcp_auth_context(_review_auth()):
        first = create_table(
            str(paper_id),
            first_payload,
            "The parser missed this table.",
            EVIDENCE,
        )
        retry = create_table(
            str(paper_id),
            first_payload,
            "The parser missed this table.",
            EVIDENCE,
        )
        next_page = create_table(
            str(paper_id),
            second_payload,
            "The continued table is a distinct table object.",
            {**EVIDENCE, "page": 4},
        )

    assert retry["idempotent"] is True
    assert retry["table_id"] == first["table_id"]
    assert next_page["idempotent"] is False
    assert next_page["table_id"] != first["table_id"]
    with Session(table_tool_env) as session:
        assert session.query(PaperTable).filter_by(paper_id=paper_id).count() == 2
        assert session.query(PaperCorrection).filter_by(paper_id=paper_id).count() == 2


def test_delete_table_success_and_retry_are_idempotent(table_tool_env):
    with Session(table_tool_env) as session:
        paper = _paper(session, "delete-table")
        table = _table(
            session,
            paper,
            caption="Parser artifact",
            markdown_content="| noise |",
            page=6,
        )
        session.commit()
        paper_id, table_id = paper.id, table.id

    with mcp_auth_context(_review_auth()):
        first = delete_table(
            str(paper_id),
            str(table_id),
            "No source table exists on this page.",
            EVIDENCE,
        )
        retry = delete_table(
            str(paper_id),
            str(table_id),
            "No source table exists on this page.",
            EVIDENCE,
        )

    assert first["idempotent"] is False
    assert retry["idempotent"] is True
    assert retry["correction_ids"] == first["correction_ids"]
    with Session(table_tool_env) as session:
        assert session.get(PaperTable, table_id) is None
        assert session.query(PaperCorrection).filter_by(paper_id=paper_id).count() == 1
        assert session.query(AuditLog).filter_by(action="delete_table").count() == 1


def test_merge_table_atomically_updates_target_deletes_source_and_retries(
    table_tool_env,
):
    with Session(table_tool_env) as session:
        paper = _paper(session, "merge-table")
        source = _table(
            session,
            paper,
            caption="Table 2 continued",
            markdown_content="| B | 2 |",
            page=8,
        )
        target = _table(
            session,
            paper,
            caption="Table 2",
            markdown_content="| A | 1 |",
            page=7,
        )
        session.commit()
        paper_id, source_id, target_id = paper.id, source.id, target.id

    merged_markdown = "A+B"
    with mcp_auth_context(_review_auth()):
        first = merge_table(
            str(paper_id),
            str(source_id),
            str(target_id),
            "The caller verified the target should contain both pages.",
            EVIDENCE,
            {"markdown_content": merged_markdown},
        )
        retry = merge_table(
            str(paper_id),
            str(source_id),
            str(target_id),
            "A different reason must not affect idempotency.",
            {**EVIDENCE, "quoted_text": "Different evidence for the same retry"},
            {"markdown_content": merged_markdown},
        )

    assert first["idempotent"] is False
    assert retry["idempotent"] is True
    assert retry["correction_ids"] == first["correction_ids"]
    with Session(table_tool_env) as session:
        correction_count = session.query(PaperCorrection).filter_by(paper_id=paper_id).count()
        audit_count = session.query(AuditLog).filter_by(paper_id=paper_id).count()

    with mcp_auth_context(_review_auth()):
        with pytest.raises(
            ValueError,
            match="merge_table_conflict: source already merged with different target_updates",
        ):
            merge_table(
                str(paper_id),
                str(source_id),
                str(target_id),
                "This retry conflicts with the completed merge.",
                EVIDENCE,
                {"markdown_content": "DIFFERENT"},
            )

    with Session(table_tool_env) as session:
        assert session.get(PaperTable, source_id) is None
        assert session.get(PaperTable, target_id).markdown_content == merged_markdown
        assert session.query(PaperCorrection).filter_by(paper_id=paper_id).count() == correction_count == 2
        assert session.query(AuditLog).filter_by(paper_id=paper_id).count() == audit_count
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "merge_table")
        ).one()
        assert audit.payload["target_updates"] == {"markdown_content": merged_markdown}
        assert audit.payload["source_before"]["id"] == str(source_id)
        assert audit.payload["target_before"]["id"] == str(target_id)


def test_merge_table_rejects_cross_paper_same_source_target_and_missing_evidence(
    table_tool_env,
):
    with Session(table_tool_env) as session:
        paper_a = _paper(session, "merge-errors-a")
        paper_b = _paper(session, "merge-errors-b")
        table_a = _table(
            session,
            paper_a,
            caption="A",
            markdown_content="| A |",
            page=1,
        )
        table_b = _table(
            session,
            paper_b,
            caption="B",
            markdown_content="| B |",
            page=2,
        )
        session.commit()
        paper_a_id, table_a_id, table_b_id = paper_a.id, table_a.id, table_b.id

    with mcp_auth_context(_review_auth()):
        with pytest.raises(ValueError, match="different"):
            merge_table(
                str(paper_a_id),
                str(table_a_id),
                str(table_a_id),
                "Invalid self merge.",
                EVIDENCE,
            )
        with pytest.raises(ValueError, match="not found for this paper"):
            merge_table(
                str(paper_a_id),
                str(table_a_id),
                str(table_b_id),
                "Invalid cross-paper merge.",
                EVIDENCE,
            )
        with pytest.raises(ValueError, match="structured evidence_payload"):
            merge_table(
                str(paper_a_id),
                str(table_a_id),
                str(table_b_id),
                "Missing evidence.",
                {},
            )

    with Session(table_tool_env) as session:
        assert session.get(PaperTable, table_a_id) is not None
        assert session.get(PaperTable, table_b_id) is not None
        assert session.query(PaperCorrection).count() == 0


def test_si_table_requires_the_table_objects_real_paper_id(table_tool_env):
    with Session(table_tool_env) as session:
        main_paper = _paper(session, "main-paper")
        si_paper = _paper(session, "si-paper")
        si_table = _table(
            session,
            si_paper,
            caption="Table S1",
            markdown_content="| SI |",
            page=2,
        )
        session.commit()
        main_id, si_id, table_id = main_paper.id, si_paper.id, si_table.id

    with mcp_auth_context(_review_auth()):
        with pytest.raises(ValueError, match="not found for this paper"):
            update_table(
                str(main_id),
                str(table_id),
                {"caption": "Wrong owner"},
                "Must not write through the main paper.",
                EVIDENCE,
            )
        result = update_table(
            str(si_id),
            str(table_id),
            {"caption": "Table S1. Corrected"},
            "Use the SI table object's own paper_id.",
            EVIDENCE,
        )

    assert result["paper_id"] == str(si_id)
    with Session(table_tool_env) as session:
        assert session.get(PaperTable, table_id).paper_id == si_id


def test_apply_review_rules_table_noop_does_not_create_correction(table_tool_env):
    with Session(table_tool_env) as session:
        paper = _paper(session, "table-noop-run")
        table = _table(
            session,
            paper,
            caption="Already correct",
            markdown_content="| correct |",
            page=5,
        )
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="noop-run",
            mapping_status="mapped",
        )
        session.add(run)
        session.flush()
        candidate = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "tables",
                "target_id": str(table.id),
                "field_name": "caption",
                "decision": "REVISE",
                "corrected_value": "Already correct",
                "reason": "The proposed value is already present.",
                "evidence_location": EVIDENCE,
            },
            status="pending",
        )
        session.add(candidate)
        session.commit()
        run_id, candidate_id = run.id, candidate.id

    with Session(table_tool_env) as session:
        service = ExternalAnalysisService(session, get_settings())
        first = service.apply_review_rules_for_run(run_id, reviewer="ide_ai")
        second = service.apply_review_rules_for_run(run_id, reviewer="ide_ai")
        session.commit()
        stored_candidate = session.get(ExternalAnalysisCandidate, candidate_id)
        assert first["non_dft_object_reviews"]["applied_items"][0]["action"] == "idempotent_noop"
        assert second["non_dft_object_reviews"]["applied_count"] == 0
        assert stored_candidate.status == "ai_applied"
        assert session.query(PaperCorrection).count() == 0


def test_new_run_does_not_replay_old_table_candidate(table_tool_env):
    with Session(table_tool_env) as session:
        paper = _paper(session, "run-isolation")
        table = _table(
            session,
            paper,
            caption="Original",
            markdown_content="| value |",
            page=9,
        )
        old_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="old-run",
            mapping_status="mapped",
        )
        new_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="new-run",
            mapping_status="mapped",
        )
        session.add_all([old_run, new_run])
        session.flush()
        old_candidate = ExternalAnalysisCandidate(
            run_id=old_run.id,
            paper_id=paper.id,
            candidate_type="correction",
            normalized_payload={
                "field_name": "tables",
                "target_path": f"tables:{table.id}:caption",
                "operation": "replace",
                "proposed_value": "Old run value",
                "reason": "Historical candidate.",
                "evidence_payload": EVIDENCE,
            },
            evidence_payload=EVIDENCE,
            status="pending",
        )
        new_candidate = ExternalAnalysisCandidate(
            run_id=new_run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload={
                "target_type": "tables",
                "target_id": str(table.id),
                "field_name": "caption",
                "decision": "REVISE",
                "corrected_value": "New run value",
                "reason": "Current candidate.",
                "evidence_location": EVIDENCE,
            },
            status="pending",
        )
        session.add_all([old_candidate, new_candidate])
        session.commit()
        table_id = table.id
        old_candidate_id = old_candidate.id
        new_candidate_id = new_candidate.id
        new_run_id = new_run.id

    with Session(table_tool_env) as session:
        service = ExternalAnalysisService(session, get_settings())
        service.apply_review_rules_for_run(new_run_id, reviewer="ide_ai")
        service.apply_review_rules_for_run(new_run_id, reviewer="ide_ai")
        session.commit()
        assert session.get(PaperTable, table_id).caption == "New run value"
        assert session.get(ExternalAnalysisCandidate, old_candidate_id).status == "pending"
        assert session.get(ExternalAnalysisCandidate, new_candidate_id).status == "ai_applied"
        corrections = session.scalars(select(PaperCorrection)).all()
        assert len(corrections) == 1
        assert corrections[0].proposed_value == "New run value"
