from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditLog, Paper, PaperFigure, PaperTable
from app.mcp.context import mcp_auth_context
from app.mcp.server import finalize_chart_review, get_chart_review_task, resolve_chart_review_actions
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService


def _paper_with_chart_objects(session: Session, code: str = "BCHART") -> tuple[Paper, PaperFigure, PaperTable]:
    paper = Paper(title=f"{code} chart review", paper_code=code, authors=[], pdf_path=f"{code}.pdf")
    session.add(paper)
    session.flush()
    figure = PaperFigure(
        paper_id=paper.id,
        caption="Fig. 1. A complete catalyst schematic.",
        page=1,
        figure_label="Fig. 1",
        figure_role="schematic_illustration",
        content_summary="A catalyst schematic.",
        key_elements=["catalyst", "schematic"],
        crop_status="candidate_crop",
    )
    table = PaperTable(
        paper_id=paper.id,
        caption="Table 1. Catalyst metrics.",
        markdown_content="| Metric | Value |\n|---|---|\n| A | 1 |",
        page=2,
        extraction_source="docling",
    )
    session.add_all([figure, table])
    session.flush()
    return paper, figure, table


def _service(session: Session) -> EvidenceReviewBundleService:
    return EvidenceReviewBundleService(session, get_settings())


def _fingerprint(session: Session, paper: Paper) -> str:
    return _service(session)._build_materials(paper.id)["bundle_fingerprint"]


def _review_source(label: str = "web-ai") -> dict:
    return {
        "review_source_type": "web_ai",
        "reviewer_label": label,
        "reviewer_model": "unit-test",
        "tool_capabilities": ["pdf_reading", "image_understanding", "table_reconstruction"],
    }


def _figure_action(figure: PaperFigure, action: str = "KEEP", **overrides) -> dict:
    payload = {
        "action": action,
        "figure_id": str(figure.id),
        "evidence_ids": ["main:figure:001"],
        "evidence_checked": True,
        "confidence": 0.95,
        "reason": "Checked against the source PDF evidence.",
    }
    payload.update(overrides)
    return payload


def _table_action(table: PaperTable, action: str = "KEEP", **overrides) -> dict:
    payload = {
        "action": action,
        "table_id": str(table.id),
        "evidence_ids": ["main:table:001"],
        "evidence_checked": True,
        "confidence": 0.95,
        "reason": "Checked against the source PDF table.",
    }
    payload.update(overrides)
    return payload


def _result_payload(session: Session, paper: Paper, figures: list[dict], tables: list[dict], *, status: str = "completed") -> dict:
    return {
        "schema_version": "offline_figure_table_evidence_review_result_v1",
        "bundle_fingerprint": _fingerprint(session, paper),
        "paper_id": str(paper.id),
        "paper_code": paper.paper_code,
        "review_source": _review_source(),
        "overall_status": status,
        "figure_actions": figures,
        "table_actions": tables,
        "dft_evidence_candidates": [],
        "uncertainties": [],
        "notes": [],
    }


def test_all_objects_needs_human_is_not_apply_ready(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH01")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure, "NEEDS_HUMAN", evidence_checked=False, confidence=0.2)],
            [_table_action(table, "NEEDS_HUMAN", evidence_checked=False, confidence=0.2)],
            status="needs_human",
        )
        validation = _service(session).validate_result(paper.id, payload)

    assert validation["valid"] is True
    assert validation["apply_ready"] is False
    assert validation["unresolved_count"] == 2
    assert {item["action"] for item in validation["unresolved_actions"]} == {"NEEDS_HUMAN"}


def test_one_skipped_item_does_not_complete_chart_stage(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH02")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure)],
            [_table_action(table, "NEEDS_HUMAN", evidence_checked=False, confidence=0.3)],
            status="completed",
        )
        applied = _service(session).apply_result(paper.id, payload)
        applied_logs = session.scalars(select(AuditLog).where(AuditLog.action == "offline_evidence_review_applied")).all()

    assert applied["chart_review_completed"] is False
    assert applied["stage_status"] == "needs_local_ai"
    assert applied["unresolved_count"] == 1
    assert applied_logs == []


def test_duplicate_conflicting_figure_actions_fail_validation(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH03")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure), _figure_action(figure, reason="A conflicting duplicate action.")],
            [_table_action(table)],
        )
        validation = _service(session).validate_result(paper.id, payload)

    assert validation["valid"] is False
    assert any(error["code"] == "duplicate_or_conflicting_figure_action" for error in validation["errors"])


def test_table_update_without_evidence_ids_fails(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH04")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure)],
            [
                _table_action(
                    table,
                    "UPDATE",
                    evidence_ids=[],
                    complete_markdown="| Metric | Value |\n|---|---|\n| B | 2 |",
                )
            ],
        )
        validation = _service(session).validate_result(paper.id, payload)

    assert validation["valid"] is False
    assert any(error["code"] == "missing_evidence_ids_for_modification" for error in validation["errors"])


def test_no_unresolved_actions_auto_completes_chart_stage(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH05")
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        result = _service(session).apply_result(paper.id, payload)
        session.refresh(paper)
        applied_logs = session.scalars(select(AuditLog).where(AuditLog.action == "offline_evidence_review_applied")).all()

    assert result["chart_review_completed"] is True
    assert result["stage_status"] == "completed"
    assert result["completed_snapshot_fingerprint"]
    assert paper.comprehensive_analysis["manual_review_progress"]["figures"]["completed"] is True
    assert len(applied_logs) == 1


def test_local_ai_batch_resolve_then_finalize_succeeds_via_mcp_tools(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH06")
        initial_payload = _result_payload(
            session,
            paper,
            [_figure_action(figure, "NEEDS_HUMAN", evidence_checked=False, confidence=0.2)],
            [_table_action(table)],
            status="needs_human",
        )
        partial = _service(session).apply_result(paper.id, initial_payload)
        paper_id = str(paper.id)
        resolved_figure_action = _figure_action(figure)
        resolved_table_action = _table_action(table)

    assert partial["chart_review_completed"] is False
    assert partial["unresolved_count"] == 1

    with mcp_auth_context("test-correction-only-key"):
        task = get_chart_review_task(paper_id)
        resolved_payload = {
            **initial_payload,
            "bundle_fingerprint": task["bundle_fingerprint"],
            "overall_status": "completed",
            "review_source": _review_source("local-ai"),
            "figure_actions": [resolved_figure_action],
            "table_actions": [resolved_table_action],
        }
        resolved = resolve_chart_review_actions(paper_id, resolved_payload)
        finalized = finalize_chart_review(paper_id)

    assert task["unresolved_count"] == 1
    assert resolved["chart_review_completed"] is True
    assert finalized["chart_review_completed"] is True
    assert finalized["completed_snapshot_fingerprint"] == resolved["completed_snapshot_fingerprint"]


def test_repeated_submit_is_idempotent(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH07")
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        first = _service(session).apply_result(paper.id, payload)
        second = _service(session).apply_result(paper.id, payload)
        applied_count = session.query(AuditLog).filter_by(action="offline_evidence_review_applied").count()

    assert first["chart_review_completed"] is True
    assert second["idempotent"] is True
    assert second["completed_snapshot_fingerprint"] == first["completed_snapshot_fingerprint"]
    assert applied_count == 1
