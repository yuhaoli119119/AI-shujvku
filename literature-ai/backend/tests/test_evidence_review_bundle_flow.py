from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditLog, Paper, PaperFigure, PaperRelationship, PaperTable
from app.main import app
from app.mcp.context import mcp_auth_context
from app.mcp.server import finalize_chart_review, get_chart_review_task, resolve_chart_review_actions
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.services.table_curation_service import TableCurationService


def _paper_with_chart_objects(session: Session, code: str = "BCHART") -> tuple[Paper, PaperFigure, PaperTable]:
    figure_root = get_settings().storage_paths["figures"]
    figure_root.mkdir(parents=True, exist_ok=True)
    (figure_root / "test-chart-schematic.png").write_bytes(b"\x89PNG\r\n\x1a\nchart")
    paper = Paper(title=f"{code} chart review", paper_code=code, authors=[], pdf_path=f"{code}.pdf")
    session.add(paper)
    session.flush()
    figure = PaperFigure(
        paper_id=paper.id,
        caption="Fig. 1. A complete catalyst schematic.",
        page=1,
        figure_label="Fig. 1",
        image_path="test-chart-schematic.png",
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


def test_supporting_information_bundle_includes_only_dft_related_figures(setup_test_db):
    with Session(setup_test_db) as session:
        figure_root = get_settings().storage_paths["figures"]
        figure_root.mkdir(parents=True, exist_ok=True)
        for filename in ("main-overview.png", "si-dft.png", "si-sem.png"):
            (figure_root / filename).write_bytes(b"\x89PNG\r\n\x1a\nchart")
        main = Paper(title="Main chart paper", paper_code="BCHSI", authors=[], pdf_path="main.pdf")
        si = Paper(title="Supporting information", paper_code="BCHSI-SI", authors=[], pdf_path="si.pdf")
        session.add_all([main, si])
        session.flush()
        session.add(
            PaperRelationship(
                source_paper_id=main.id,
                target_paper_id=si.id,
                relationship_type="supplementary",
                created_by="test",
            )
        )
        main_figure = PaperFigure(
            paper_id=main.id,
            caption="Fig. 1. Catalyst overview image.",
            page=1,
            figure_label="Fig. 1",
            image_path="main-overview.png",
            figure_role="overview",
            content_summary="Main-paper overview image.",
            key_elements=["catalyst overview"],
        )
        si_dft_figure = PaperFigure(
            paper_id=si.id,
            caption="Fig. S2. DFT adsorption energy and charge density difference.",
            page=2,
            figure_label="Fig. S2",
            image_path="si-dft.png",
            figure_role="dft_evidence",
            content_summary="DFT adsorption energy profile.",
            key_elements=["DFT", "adsorption energy"],
        )
        si_non_dft_figure = PaperFigure(
            paper_id=si.id,
            caption="Fig. S3. SEM image and particle morphology.",
            page=3,
            figure_label="Fig. S3",
            image_path="si-sem.png",
            figure_role="microscopy",
            content_summary="SEM morphology image.",
            key_elements=["SEM", "particle morphology"],
        )
        session.add_all([main_figure, si_dft_figure, si_non_dft_figure])
        session.flush()

        materials = _service(session)._build_materials(main.id)
        figure_ids = materials["figure_id_map"]
        payload = _result_payload(
            session,
            main,
            [
                _figure_action(main_figure, evidence_ids=["main:figure:001"]),
                _figure_action(si_dft_figure, evidence_ids=["si:figure:001"]),
            ],
            [],
        )
        validation = _service(session).validate_result(main.id, payload)

    assert str(main_figure.id) in figure_ids
    assert str(si_dft_figure.id) in figure_ids
    assert str(si_non_dft_figure.id) not in figure_ids
    assert {item["source_record_id"] for item in materials["extracted_figures"]} == {
        str(main_figure.id),
        str(si_dft_figure.id),
    }
    assert "excluded_non_dft_supplementary_figures:1" in materials["warnings"]
    assert validation["valid"] is True


def test_non_dft_supplementary_figure_changes_do_not_invalidate_chart_completion(setup_test_db):
    with Session(setup_test_db) as session:
        figure_root = get_settings().storage_paths["figures"]
        figure_root.mkdir(parents=True, exist_ok=True)
        for filename in ("main-scope.png", "si-dft-scope.png", "si-unused-scope.png"):
            (figure_root / filename).write_bytes(b"\x89PNG\r\n\x1a\nchart")
        main = Paper(title="Scoped chart paper", paper_code="BCHSC", authors=[], pdf_path="main.pdf")
        si = Paper(title="Scoped supporting information", paper_code="BCHSC-SI", authors=[], pdf_path="si.pdf")
        session.add_all([main, si])
        session.flush()
        session.add(
            PaperRelationship(
                source_paper_id=main.id,
                target_paper_id=si.id,
                relationship_type="supplementary",
                created_by="test",
            )
        )
        main_figure = PaperFigure(
            paper_id=main.id,
            caption="Fig. 1. Main catalyst overview.",
            page=1,
            figure_label="Fig. 1",
            image_path="main-scope.png",
            figure_role="overview",
            content_summary="Main-paper catalyst overview.",
            key_elements=["catalyst overview"],
        )
        si_dft_figure = PaperFigure(
            paper_id=si.id,
            caption="Fig. S2. DFT adsorption energy and charge density difference.",
            page=2,
            figure_label="Fig. S2",
            image_path="si-dft-scope.png",
            figure_role="dft_evidence",
            content_summary="DFT adsorption energy profile.",
            key_elements=["DFT", "adsorption energy"],
        )
        si_unused_figure = PaperFigure(
            paper_id=si.id,
            caption="Fig. S3. SEM image and particle morphology.",
            page=3,
            figure_label="Fig. S3",
            image_path="si-unused-scope.png",
            figure_role="microscopy",
            content_summary="SEM morphology image.",
            key_elements=["SEM", "particle morphology"],
        )
        table = PaperTable(
            paper_id=si.id,
            caption="Table S1. DFT calculated values.",
            markdown_content="| Metric | Value |\n|---|---|\n| Adsorption energy | -1.2 eV |",
            page=4,
        )
        session.add_all([main_figure, si_dft_figure, si_unused_figure, table])
        session.flush()
        payload = _result_payload(
            session,
            main,
            [
                _figure_action(main_figure, evidence_ids=["main:figure:001"]),
                _figure_action(si_dft_figure, evidence_ids=["si:figure:001"]),
            ],
            [_table_action(table, evidence_ids=["si:table:001"])],
        )
        completed = _service(session).apply_result(main.id, payload)
        si_unused_figure.content_summary = "Updated SEM morphology image that remains outside the chart review scope."
        session.add(si_unused_figure)
        session.commit()
        task = _service(session).get_review_task(main.id)

    assert completed["stage_status"] == "completed"
    assert task["stage_status"] == "completed"
    assert task["unresolved_count"] == 0


def _fingerprint(session: Session, paper: Paper) -> str:
    return _service(session)._build_materials(paper.id)["bundle_fingerprint"]


def _review_source(label: str = "web-ai") -> dict:
    return {
        "review_source_type": "web_ai",
        "reviewer_label": label,
        "reviewer_model": "unit-test",
        "tool_capabilities": ["pdf_reading", "image_understanding", "table_reconstruction"],
    }


def _local_ai_verification(note: str = "Checked the target object and its cited PDF page.") -> dict:
    return {
        "verified_against_pdf": True,
        "used_tools": ["get_codex_item", "read_paper_page"],
        "verification_note": note,
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


def test_documented_needs_human_remains_pending_and_blocks_chart_stage(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHNH")
        payload = _result_payload(
            session,
            paper,
            [
                _figure_action(
                    figure,
                    "NEEDS_HUMAN",
                    evidence_checked=True,
                    confidence=0.2,
                    reason=(
                        "The figure is a documented cross-page exception. "
                        "The current source page evidence is sufficient to preserve it as a human-attention item."
                    ),
                )
            ],
            [_table_action(table)],
        )
        validation = _service(session).validate_result(paper.id, payload)
        applied = _service(session).apply_result(paper.id, payload)
        paper_id = paper.id

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert validation["valid"] is True
    assert validation["unresolved_count"] == 1
    assert validation["stage_status"] == "needs_local_ai"
    assert "needs_human" in validation["unresolved_actions"][0]["blocked_reasons"]
    assert applied["chart_review_completed"] is False
    assert response.status_code == 409


def test_documented_low_confidence_table_needs_human_remains_pending(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHTB")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure)],
            [
                _table_action(
                    table,
                    "NEEDS_HUMAN",
                    confidence=0.1,
                    reason="The table is a documented cross-page exception for the DFT review stage.",
                )
            ],
            status="needs_human",
        )
        validation = _service(session).validate_result(paper.id, payload)
        applied = _service(session).apply_result(paper.id, payload)

    assert validation["valid"] is True
    assert validation["unresolved_count"] == 1
    assert validation["stage_status"] == "needs_local_ai"
    assert "needs_human" in validation["unresolved_actions"][0]["blocked_reasons"]
    assert applied["stage_status"] == "needs_local_ai"
    assert applied["chart_review_completed"] is False


def test_manual_figures_completion_requires_tables_resolved(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHMT")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure)],
            [
                _table_action(
                    table,
                    "NEEDS_HUMAN",
                    reason="Table must still be checked against the PDF.",
                )
            ],
            status="needs_human",
        )
        applied = _service(session).apply_result(paper.id, payload)
        paper_id = paper.id

    response = TestClient(app).post(
        f"/api/papers/{paper_id}/manual-review-progress",
        json={"module": "figures", "completed": True, "reviewer": "test"},
    )

    assert applied["stage_status"] == "needs_local_ai"
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "figure_table_review_not_completed"
    assert body["detail"]["chart_review"]["unresolved_count"] == 1


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


def test_web_ai_common_json_shape_issues_are_normalized_or_unresolved(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHJS")
        payload = _result_payload(
            session,
            paper,
            [_figure_action(figure, dft_relevance="yes")],
            [
                _table_action(
                    table,
                    "merge",
                    dft_relevance="not_dft",
                    reason="The web AI marked this as merge but did not provide both table ids.",
                )
            ],
        )
        validation = _service(session).validate_result(paper.id, payload)

    assert validation["valid"] is True
    assert validation["apply_ready"] is False
    assert validation["unresolved_count"] == 1
    assert validation["execution_plan"][0]["payload"]["dft_relevance"] == "explicit_dft"
    assert validation["execution_plan"][1]["action"] == "MERGE"
    assert validation["execution_plan"][1]["payload"]["dft_relevance"] == "none"
    assert "merge_requires_source_table_id_and_target_table_id" in validation["unresolved_actions"][0]["blocked_reasons"]


def test_exact_duplicate_table_actions_are_deduped_before_conflict_checks(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHDU")
        table_action = _table_action(table)
        payload = _result_payload(session, paper, [_figure_action(figure)], [table_action, dict(table_action)])
        validation = _service(session).validate_result(paper.id, payload)

    assert validation["valid"] is True
    assert len([item for item in validation["execution_plan"] if item["category"] == "table"]) == 1


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


def test_keep_without_rag_ready_figure_metadata_cannot_complete_chart_stage(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCHRG")
        figure.figure_role = "unknown"
        figure.content_summary = None
        figure.key_elements = None
        session.add(figure)
        session.flush()
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        validation = _service(session).validate_result(paper.id, payload)
        result = _service(session).apply_result(paper.id, payload)
        applied_logs = session.scalars(select(AuditLog).where(AuditLog.action == "offline_evidence_review_applied")).all()

    assert validation["valid"] is False
    assert any(error["code"] == "figure_rag_quality_incomplete" for error in validation["errors"])
    assert result["stage_status"] == "invalid"
    assert result["chart_review_completed"] is False
    assert applied_logs == []


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


def test_completed_chart_review_immediately_allows_dft_bundle_export(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH08")
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        completed = _service(session).apply_result(paper.id, payload)
        paper_id = paper.id

    response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert completed["stage_status"] == "completed"
    assert response.status_code == 200


def test_local_ai_resolves_merge_delete_reject_and_low_confidence_atomically(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, first_table = _paper_with_chart_objects(session, "BCH09")
        second_table = PaperTable(
            paper_id=paper.id,
            caption="Table 1 continued.",
            markdown_content="| Metric | Value |\n|---|---|\n| B | 2 |",
            page=3,
            extraction_source="docling",
        )
        invalid_table = PaperTable(
            paper_id=paper.id,
            caption="Navigation noise.",
            markdown_content="| Noise | Value |\n|---|---|\n| x | y |",
            page=4,
            extraction_source="docling",
        )
        session.add_all([second_table, invalid_table])
        session.flush()
        verification = _local_ai_verification()
        payload = _result_payload(
            session,
            paper,
            [
                _figure_action(
                    figure,
                    "REJECT",
                    confidence=0.2,
                    local_ai_verification=verification,
                )
            ],
            [
                {
                    "action": "MERGE",
                    "source_table_id": str(second_table.id),
                    "target_table_id": str(first_table.id),
                    "evidence_ids": ["main:table:001", "main:table:002"],
                    "evidence_checked": True,
                    "confidence": 0.2,
                    "reason": "The PDF shows this is one continued table.",
                    "local_ai_verification": verification,
                },
                _table_action(
                    invalid_table,
                    "DELETE",
                    evidence_ids=["main:table:003"],
                    confidence=0.2,
                    local_ai_verification=verification,
                ),
            ],
        )

        result = _service(session).resolve_review_actions(paper.id, payload)
        remaining_figures = session.query(PaperFigure).filter(PaperFigure.paper_id == paper.id).count()
        remaining_table_ids = [
            row.id for row in session.query(PaperTable).filter(PaperTable.paper_id == paper.id).all()
        ]
        first_table_id = first_table.id
        operation_logs = session.scalars(
            select(AuditLog).where(AuditLog.action == "offline_evidence_review_op")
        ).all()

    assert result["stage_status"] == "completed"
    assert result["unresolved_actions"] == []
    assert remaining_figures == 0
    assert remaining_table_ids == [first_table_id]
    assert {row.payload["action"]["action"] for row in operation_logs} == {"REJECT", "MERGE", "DELETE"}
    assert all(row.payload["actor_type"] == "ai" for row in operation_logs)


def test_chart_mutation_invalidates_completion_and_old_payload_is_not_idempotent(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH10")
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        completed = _service(session).apply_result(paper.id, payload)
        old_fingerprint = completed["completed_snapshot_fingerprint"]
        table.markdown_content = "| Metric | Value |\n|---|---|\n| changed | 99 |"
        session.add(table)
        session.commit()
        task = _service(session).get_review_task(paper.id)
        repeated = _service(session).apply_result(paper.id, payload)
        paper_id = paper.id

    dft_response = TestClient(app).post(f"/api/papers/{paper_id}/dft-review-bundle")

    assert task["stage_status"] == "stale"
    assert task["current_snapshot_fingerprint"] != old_fingerprint
    assert repeated["valid"] is False
    assert repeated.get("idempotent") is not True
    assert dft_response.status_code == 409
    assert dft_response.json()["detail"]["figure_table_review"]["stage_status"] == "stale"


def test_evidence_gated_table_update_refreshes_effective_chart_completion(setup_test_db):
    with Session(setup_test_db) as session:
        paper, figure, table = _paper_with_chart_objects(session, "BCH11")
        payload = _result_payload(session, paper, [_figure_action(figure)], [_table_action(table)])
        completed = _service(session).apply_result(paper.id, payload)
        old_fingerprint = completed["completed_snapshot_fingerprint"]
        TableCurationService(session, reviewer="local_ai_chart_review").update_table(
            paper_id=paper.id,
            table_id=table.id,
            updates={
                "markdown_content": "| Metric | Value |\n|---|---|\n| reviewed | 100 |",
            },
            reason="Verified updated table against the source PDF.",
            evidence_payload={
                "page": table.page,
                "table": table.caption,
                "quoted_text": "Table 1. Catalyst metrics.",
                "table_id": str(table.id),
            },
        )
        session.commit()
        task = _service(session).get_review_task(paper.id)

    assert task["stage_status"] == "completed"
    assert task["current_snapshot_fingerprint"] != old_fingerprint
    assert task["completed_snapshot_fingerprint"] == task["current_snapshot_fingerprint"]
    assert task["unresolved_count"] == 0
