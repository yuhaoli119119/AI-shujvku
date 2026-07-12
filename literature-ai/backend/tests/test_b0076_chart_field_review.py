from __future__ import annotations

import copy
import json
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from PIL import Image

from app.config import get_settings
from app.db.models import AuditLog, DFTResult, EvidenceLocator, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper, PaperFigure, PaperRelationship, PaperTable, WorkflowJob
from app.main import app
from app.services.content_review_bundle_service import ContentReviewBundleService
from app.services.dft_review_bundle_service import (
    DFTReviewBundleService,
    FigureTableReviewNotCompletedError,
)
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.services.paper_query import PaperQueryService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.artifact_paths import resolve_paper_pdf_path


def test_paper_scope_chart_bundle_keeps_only_dft_related_si_and_deduplicates(setup_test_db):
    settings = get_settings()
    image_path = settings.storage_paths["figures"] / f"{uuid4()}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1200, 900), "white").save(image_path, format="PNG")
    pdf_root = settings.storage_paths["pdf"]
    pdf_root.mkdir(parents=True, exist_ok=True)
    import fitz
    for name in ("B0102.pdf", "S0102.pdf"):
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), name)
        document.save(str(pdf_root / name))
        document.close()
    with Session(setup_test_db) as session:
        main = Paper(paper_code="B0102", title="main", pdf_path="B0102.pdf", authors=[])
        si = Paper(paper_code="S0102", title="support", pdf_path="S0102.pdf", authors=[])
        session.add_all([main, si])
        session.flush()
        session.add(PaperRelationship(source_paper_id=main.id, target_paper_id=si.id, relationship_type="supplementary", created_by="test"))
        main_figure = PaperFigure(paper_id=main.id, figure_label="Figure 1", caption="ordinary main figure", page=1, image_path=image_path.name)
        canonical = PaperFigure(
            paper_id=si.id,
            figure_label="Figure S1",
            caption="Figure S1. DFT adsorption energy comparison for Li2S.",
            page=2,
            image_path=image_path.name,
            figure_role="dft_calculation",
            content_summary="Compares DFT adsorption energies.",
            key_elements=["Li2S", "adsorption energy"],
        )
        duplicate = PaperFigure(
            paper_id=si.id,
            figure_label="fig_candidate_1",
            caption="Figure S1. DFT adsorption energy comparison for Li2S.",
            page=2,
            image_path=image_path.name,
            figure_role="unknown",
        )
        referenced = PaperFigure(
            paper_id=si.id,
            figure_label="Figure S2",
            caption="Figure S2. Reference geometric sketch.",
            page=3,
            image_path=image_path.name,
        )
        unrelated = PaperFigure(
            paper_id=si.id,
            figure_label="Figure S3",
            caption="Figure S3. Experimental cycling photograph.",
            page=4,
            image_path=image_path.name,
        )
        possible_dft = PaperFigure(
            paper_id=si.id,
            figure_label="Figure S4",
            caption="Figure S4. AIMD stability and spin density of the optimized catalyst.",
            page=5,
            image_path=image_path.name,
        )
        session.add_all([main_figure, canonical, duplicate, referenced, unrelated, possible_dft])
        session.flush()
        dft_row = DFTResult(
            paper_id=si.id,
            property_type="reaction_barrier",
            value=0.4,
            unit="eV",
            evidence_text="The referenced SI figure supports the DFT barrier.",
        )
        session.add(dft_row)
        session.flush()
        session.add(EvidenceLocator(
            paper_id=si.id,
            source_type="pdf",
            page=3,
            figure_id=referenced.id,
            target_type="dft_results",
            target_id=str(dft_row.id),
            field_name="value",
            evidence_text="Figure S2 supports the calculated barrier.",
            locator_status="exact_page",
            locator_confidence=1.0,
        ))
        session.add_all([
            PaperTable(paper_id=main.id, caption="Table 1", markdown_content="|a|b|\n|-|-|\n|1|2|", page=1),
            PaperTable(paper_id=si.id, caption="Table S1", markdown_content="|a|b|\n|-|-|\n|3|4|", page=1),
        ])
        session.commit()
        result = EvidenceReviewBundleService(session, settings).build_zip(main.id)
    manifest = result["manifest"]
    assert manifest["chart_counts"] == {"main_figures": 1, "main_tables": 1, "si_figures": 3, "si_tables": 1}
    assert manifest["counts"]["excluded_duplicate_figures"] == 1
    assert manifest["excluded_duplicate_figures"] == [{
        "source_paper_id": str(si.id),
        "page": 2,
        "excluded_figure_id": str(duplicate.id),
        "excluded_figure_label": "fig_candidate_1",
        "canonical_figure_id": str(canonical.id),
        "canonical_figure_label": "Figure S1",
        "reason": "same_page_same_image_reference",
    }]
    assert manifest["pdf_files"]["count"] == 2
    assert manifest["image_files"]["compressed_bytes"] < manifest["image_files"]["original_bytes"]
    with zipfile.ZipFile(BytesIO(result["content"])) as archive:
        names = set(archive.namelist())
        assert "source/main.pdf" in names and "source/si/S0102.pdf" in names
        assert any(name.endswith(".webp") for name in names)
        figures = json.loads(archive.read("parsed/extracted_figures.json"))
        assert {item["source_paper_code"] for item in figures} == {"B0102", "S0102"}
        assert {item["source_record_id"] for item in figures} == {
            str(main_figure.id), str(canonical.id), str(referenced.id), str(possible_dft.id)
        }
        assert str(unrelated.id) not in {item["source_record_id"] for item in figures}
        assert all(item["included_in_bundle"] == (item["bundle_file"] in names) for item in manifest["source_pdf_inventory"])
    with Session(setup_test_db) as session:
        try:
            EvidenceReviewBundleService(session, settings).build_zip(main.id, include_pdf_files=False)
        except ValueError as exc:
            assert str(exc) == "source_pdfs_required_for_comprehensive_review"
        else:
            raise AssertionError("PDF-less chart review bundle must be rejected")
    si_pdf = pdf_root / "S0102.pdf"
    original_si_pdf = si_pdf.read_bytes()
    si_pdf.unlink()
    try:
        with Session(setup_test_db) as session:
            service = EvidenceReviewBundleService(session, settings)
            materials = service._build_materials(main.id)
            template = service._return_template(materials)
            template["overall_status"] = "completed"
            validation = service.validate_result(main.id, template)
        assert any(error["code"] == "source_pdf_missing_for_comprehensive_review" for error in validation["errors"])
    finally:
        si_pdf.write_bytes(original_si_pdf)


def test_resolve_paper_pdf_path_rejects_escape_and_accepts_relative_and_absolute(tmp_path: Path):
    storage = tmp_path / "storage"
    pdf = storage / "pdf" / "B0076.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"pdf")

    assert resolve_paper_pdf_path("storage/pdf/B0076.pdf", storage) == pdf.resolve()
    assert resolve_paper_pdf_path(str(pdf), storage) == pdf.resolve()
    assert resolve_paper_pdf_path("storage/pdf/missing.pdf", storage) is None
    assert resolve_paper_pdf_path("storage/pdf/../secret.pdf", storage) is None
    assert resolve_paper_pdf_path("../storage/pdf/B0076.pdf", storage) is None


def test_run_scoped_chart_bundle_isolated_and_field_writeback_is_scoped(setup_test_db):
    with Session(setup_test_db) as session:
        figure_path = get_settings().storage_paths["figures"] / f"{uuid4()}.png"
        figure_path.parent.mkdir(parents=True, exist_ok=True)
        figure_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        pdf_path = get_settings().storage_paths["pdf"] / f"{uuid4()}-B0076.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4\nB0076 chart test source\n")
        paper = Paper(paper_code="B0076", title="run scoped chart field review", authors=[], pdf_path=pdf_path.name)
        session.add(paper)
        session.flush()
        figure_a = PaperFigure(
            paper_id=paper.id, figure_label="Figure 1", caption="Figure 1 caption", page=1,
            image_path=figure_path.name, figure_role=None, content_summary=None, key_elements=None,
        )
        figure_b = PaperFigure(
            paper_id=paper.id, figure_label="Figure 3", caption="Figure 3 caption", page=2,
            image_path=None, figure_role=None, content_summary=None, key_elements=None,
        )
        session.add_all([figure_a, figure_b])
        session.flush()
        run_a = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label="A")
        run_b = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label="B")
        session.add_all([run_a, run_b])
        session.flush()
        session.add_all([
            ExternalAnalysisCandidate(
                run_id=run_a.id, paper_id=paper.id, candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(figure_a.id), "field_name": "key_elements"},
                status="pending",
            ),
            ExternalAnalysisCandidate(
                run_id=run_b.id, paper_id=paper.id, candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(figure_b.id), "field_name": "content_summary"},
                status="pending",
            ),
            WorkflowJob(
                job_id=f"chart-a-{uuid4()}", type="agent_activity", status="completed",
                library_name="默认文献库", payload={"external_analysis_run_id": str(run_a.id), "paper_id": str(paper.id)},
                progress={}, result={}, runtime_context={},
            ),
        ])
        session.commit()

        service = EvidenceReviewBundleService(session, get_settings())
        generated = service.build_zip(paper.id, run_id=run_a.id, include_pdf_files=True, include_figure_files=False)
        manifest = generated["manifest"]
        assert manifest["bundle_id"] == generated["bundle_id"]
        assert manifest["scope_type"] == "external_analysis_run"
        assert manifest["run_id"] == str(run_a.id)
        assert manifest["expected_coverage"]["figure_ids"] == [str(figure_a.id)]
        assert str(figure_b.id) not in manifest["expected_coverage"]["figure_ids"]

        with zipfile.ZipFile(BytesIO(generated["content"])) as archive:
            names = set(archive.namelist())
            template = json.loads(archive.read("return_template.json"))
            fill_template = json.loads(archive.read("WEB_AI_FILL_THIS.json"))
            output_rules = json.loads(archive.read("OUTPUT_RULES.json"))
            start_here = archive.read("START_HERE.md").decode("utf-8")
            assert template["run_id"] == str(run_a.id)
            assert template["scope_type"] == "external_analysis_run"
            assert {"WEB_AI_FILL_THIS.json", "OUTPUT_RULES.json", "START_HERE.md"} <= names
            assert fill_template == template
            assert output_rules["output_workflow"]["output_filename"] == "B0076_chart_review_result.json"
            assert output_rules["output_workflow"]["reply_as_file_attachment"] is True
            assert any(
                "Every figure_actions and table_actions item must cite" in rule
                for rule in output_rules["hard_invariants"]
            )
            assert "WEB_AI_FILL_THIS.json" in start_here
            assert "reply by attaching" in start_here

        materials = service._build_materials(paper.id, run_id=run_a.id)
        figure_record = materials["extracted_figures"][0]
        payload = {
            **service._return_template(materials),
            "overall_status": "completed",
            "figure_actions": [{
                "action": "KEEP", "figure_id": str(figure_a.id),
                "evidence_ids": [figure_record["evidence_id"]], "evidence_checked": True,
                "figure_role": "property_data", "content_summary": "Figure 1 visual comparison",
                "key_elements": ["curves", "axis labels"], "confidence": 0.95,
                "reason": "verified against the scoped figure evidence",
            }],
        }
        wrong_scope = copy.deepcopy(payload)
        wrong_scope["run_id"] = str(run_b.id)
        assert service.validate_result(paper.id, wrong_scope)["valid"] is False
        wrong_target = copy.deepcopy(payload)
        wrong_target["figure_actions"][0]["figure_id"] = str(figure_b.id)
        assert service.validate_result(paper.id, wrong_target)["valid"] is False
        unknown_evidence = copy.deepcopy(payload)
        unknown_evidence["figure_actions"][0]["evidence_ids"] = ["figure:unknown"]
        assert service.validate_result(paper.id, unknown_evidence)["valid"] is False
        validated = service.validate_result(paper.id, payload)
        assert validated["valid"] is True, validated.get("errors")
        assert validated["stage_status"] == "needs_local_ai"
        assert validated["auto_apply_count"] == 1
        assert validated["unresolved_count"] == 1
        assert validated["unresolved_actions"][0]["blocked_reasons"] == [
            "local_ai_full_figure_verification_required"
        ]
        forged_web_payload = copy.deepcopy(payload)
        forged_web_payload["figure_actions"][0]["local_ai_verification"] = {
            "verified_against_pdf": True,
            "used_tools": ["get_codex_item", "read_paper_page"],
            "verification_note": "A web payload must not grant itself local-AI authority.",
        }
        forged_web_validation = service.validate_result(paper.id, forged_web_payload)
        assert forged_web_validation["unresolved_count"] == 1
        assert forged_web_validation["safety"]["local_ai_verification_authorized"] is False
        applied = service.apply_result(paper.id, payload)
        assert applied["chart_review_completed"] is False
        assert applied["stage_status"] == "needs_local_ai"
        session.refresh(figure_a)
        session.refresh(figure_b)
        assert figure_a.content_summary == "Figure 1 visual comparison"
        assert figure_a.key_elements == ["curves", "axis labels"]
        assert figure_b.content_summary is None
        assert figure_b.key_elements is None

        local_payload = copy.deepcopy(payload)
        local_payload["bundle_fingerprint"] = service.get_review_task(
            paper.id,
            run_id=run_a.id,
        )["bundle_fingerprint"]
        local_payload["review_source"] = {
            "review_source_type": "local_ai",
            "reviewer_label": "test local AI",
            "reviewer_model": "test",
            "tool_capabilities": ["get_codex_item", "read_paper_page"],
        }
        local_payload["figure_actions"][0]["local_ai_verification"] = {
            "verified_against_pdf": True,
            "used_tools": ["get_codex_item", "read_paper_page"],
            "verification_note": "Checked the scoped figure and its source PDF page.",
        }
        local_applied = service.resolve_review_actions(
            paper.id,
            local_payload,
            run_id=run_a.id,
            local_ai_authorized=True,
        )
        assert local_applied["chart_review_completed"] is True
        assert local_applied["stage_status"] == "completed"

        task = session.scalar(select(WorkflowJob).where(WorkflowJob.payload["external_analysis_run_id"].astext == str(run_a.id)))
        assert task is not None
        assert task.result["last_action"] == "chart_review_applied"


def test_run_scoped_export_headers_report_only_the_four_chart_targets(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(paper_code="B0076", title="header scope fixture", authors=[], pdf_path="missing.pdf")
        session.add(paper)
        session.flush()
        figures = [
            PaperFigure(
                paper_id=paper.id,
                figure_label=f"Figure {index}",
                caption=f"Figure {index} caption",
                page=index,
                image_path=None,
            )
            for index in range(1, 6)
        ]
        session.add_all(figures)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label="four targets")
        session.add(run)
        session.flush()
        session.add_all([
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(figure.id)},
                status="pending",
            )
            for figure in figures[:4]
        ])
        session.commit()
        paper_id, run_id = paper.id, run.id

    response = TestClient(app).post(
        f"/api/papers/{paper_id}/evidence-review-bundle?run_id={run_id}&include_pdf_files=false&include_figure_files=false"
    )

    assert response.status_code == 400, response.text
    assert "source_pdfs_required_for_comprehensive_review" in response.text


def test_content_figure_field_reminder_cannot_become_citable(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(paper_code="B0076", title="content gate", authors=[], pdf_path="missing.pdf")
        session.add(paper)
        session.flush()
        from app.db.models import ContentEvidenceItem
        item = ContentEvidenceItem(
            paper_id=paper.id, category="figure_table_evidence", source_type="external_analysis_candidate",
            source_id="figure-field-missing", content="Figure 1 key_elements missing",
            evidence_text="Figure 1.", evidence_locator={"page": 1}, page_start=1,
            review_status="needs_review", citation_status="needs_review",
        )
        session.add(item)
        session.commit()
        bundle = ContentReviewBundleService(session).generate(paper_id=paper.id)
        result = {**bundle["return_template"], "items":[{"item_id":str(item.id),"decision":"approve_citable","evidence_id":f"evidence:{item.id}","evidence_text":"Figure 1."}]}
        try:
            ContentReviewBundleService(session).validate_result(__import__("uuid").UUID(bundle["bundle_id"]), result)
        except ValueError as exc:
            assert "figure_field_review_requires_chart_bundle" in str(exc)
        else:
            raise AssertionError("figure field reminder was accepted as citable")


def _seed_completed_chart_run(session, paper, figure_count=1):
    figure_path = get_settings().storage_paths["figures"] / f"{uuid4()}.png"
    figure_path.parent.mkdir(parents=True, exist_ok=True)
    figure_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    figures = []
    for index in range(figure_count):
        figures.append(PaperFigure(
            paper_id=paper.id,
            figure_label=f"Figure {index + 1}",
            caption=f"Figure {index + 1} caption",
            page=index + 1,
            image_path=figure_path.name,
            figure_role="property_data",
            content_summary=f"Figure {index + 1} shows a comparison.",
            key_elements=["axis labels", "curves"],
        ))
    session.add_all(figures)
    session.flush()
    run = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label="completed chart batch")
    session.add(run)
    session.flush()
    session.add_all([
        ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="figure_table_evidence",
            normalized_payload={"target_type": "figure", "target_id": str(figure.id)},
            status="pending",
        )
        for figure in figures
    ])
    session.flush()
    task = EvidenceReviewBundleService(session, get_settings()).get_review_task(paper.id, run_id=run.id)
    for figure in figures:
        session.add(AuditLog(
            paper_id=paper.id,
            action="offline_evidence_review_op",
            source="test_local_ai",
            target_type="paper_figure",
            target_id=str(figure.id),
            payload={
                "run_id": str(run.id),
                "chart_run_id": str(run.id),
                "actor_type": "local_ai",
                "action": {
                    "action": "KEEP",
                    "local_ai_verification": {
                        "verified_against_pdf": True,
                        "used_tools": ["get_codex_item", "read_paper_page"],
                        "verification_note": "Test fixture verified this figure against its source PDF.",
                    },
                },
            },
        ))
    session.add(AuditLog(
        paper_id=paper.id,
        action="offline_evidence_review_applied",
        source="test",
        target_type="offline_evidence_review",
        target_id=task["current_snapshot_fingerprint"][:32],
        payload={
            "run_id": str(run.id),
            "chart_run_id": str(run.id),
            "stage_status": "completed",
            "completed_snapshot_fingerprint": task["current_snapshot_fingerprint"],
            "completed_snapshot": {"figures": [], "tables": []},
            "response": {
                "stage_status": "completed",
                "chart_review_completed": True,
                "completed_snapshot_fingerprint": task["current_snapshot_fingerprint"],
                "current_snapshot_fingerprint": task["current_snapshot_fingerprint"],
                "unresolved_actions": [],
                "unresolved_count": 0,
                "run_id": str(run.id),
                "chart_run_id": str(run.id),
            },
        },
    ))
    session.commit()
    return run, figures


def test_dft_uses_completed_main_chart_run_without_reselecting_it(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(paper_code="B0076", title="DFT selected chart scope", authors=[], pdf_path="missing.pdf")
        session.add(paper)
        session.flush()
        run, figures = _seed_completed_chart_run(session, paper)
        detail = PaperQueryService(session).get_paper_detail(paper.id)
        assert detail.chart_review_status["stage_status"] == "completed"
        assert detail.chart_review_status["chart_run_id"] == str(run.id)
        assert detail.chart_review_status["primary_completed_run"]["counts"] == {"figures": 1, "tables": 0}
        assert detail.chart_review_status["paper_scope"]["stage_status"] == "not_started"
        bundle = DFTReviewBundleService(session, get_settings()).build_zip(
            paper.id,
            chart_run_id=run.id,
            include_figure_files=False,
        )
        assert bundle["manifest"]["chart_run_id"] is None
        assert bundle["manifest"]["chart_scope_type"] == "paper_reviewed_aggregate"
        assert bundle["manifest"]["counts"]["reviewed_main_figures"] == 1
        assert bundle["manifest"]["figure_table_completed_snapshot_fingerprint"]

        second = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label="second chart batch")
        session.add(second)
        session.flush()
        session.add(ExternalAnalysisCandidate(
            run_id=second.id,
            paper_id=paper.id,
            candidate_type="figure_table_evidence",
            normalized_payload={"target_type": "figure", "target_id": str(figures[0].id)},
            status="pending",
        ))
        session.commit()
        resumed = DFTReviewBundleService(session, get_settings()).build_zip(
            paper.id,
            include_figure_files=False,
        )
        assert resumed["manifest"]["chart_run_id"] is None
        assert resumed["manifest"]["counts"]["reviewed_main_figures"] == 1


def test_scope_options_keep_support_runs_out_of_main_paper_and_fold_duplicates(setup_test_db):
    with Session(setup_test_db) as session:
        main = Paper(paper_code="B0076", title="main paper", authors=[], pdf_path="missing.pdf")
        support = Paper(paper_code="S0076", title="supporting information", authors=[], pdf_path="missing.pdf")
        session.add_all([main, support])
        session.flush()
        completed, figures = _seed_completed_chart_run(session, main)
        completed.source_label = "main_figure_review_20260703_fullset"
        support_figure = PaperFigure(
            paper_id=support.id,
            figure_label="Figure S1",
            caption="support figure",
            page=1,
        )
        session.add(support_figure)
        session.flush()
        duplicate_runs = []
        for _ in range(3):
            run = ExternalAnalysisRun(
                paper_id=support.id,
                source="web_ai",
                source_label="codex_support_figure_20260703_235944",
            )
            session.add(run)
            session.flush()
            session.add(ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=support.id,
                candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(support_figure.id)},
                status="pending",
            ))
            duplicate_runs.append(run)
        session.commit()

        main_options = EvidenceReviewBundleService(session, get_settings()).get_review_scope_options(main.id)
        assert [item["chart_run_id"] for item in main_options["chart_runs"]] == [str(completed.id)]
        assert main_options["primary_completed_run"]["chart_run_id"] == str(completed.id)

        support_options = EvidenceReviewBundleService(session, get_settings()).get_review_scope_options(support.id)
        assert len(support_options["chart_runs"]) == 3
        assert len({item["duplicate_group_key"] for item in support_options["chart_runs"]}) == 1
        assert sum(bool(item["is_duplicate_representative"]) for item in support_options["chart_runs"]) == 1
        assert {item["duplicate_run_count"] for item in support_options["chart_runs"]} == {3}


def test_multiple_distinct_unfinished_chart_runs_still_require_choice(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(paper_code="B0076", title="ambiguous unfinished scopes", authors=[], pdf_path="missing.pdf")
        session.add(paper)
        session.flush()
        figures = [PaperFigure(paper_id=paper.id, figure_label=f"Figure {index}", caption="caption", page=index)
                   for index in (1, 2)]
        session.add_all(figures)
        session.flush()
        for figure in figures:
            run = ExternalAnalysisRun(paper_id=paper.id, source="web_ai", source_label=f"batch-{figure.page}")
            session.add(run)
            session.flush()
            session.add(ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(figure.id)},
                status="pending",
            ))
        session.commit()
        try:
            DFTReviewBundleService(session, get_settings()).build_zip(paper.id, include_figure_files=False)
        except FigureTableReviewNotCompletedError as exc:
            assert exc.state["stage_status"] == "not_started"
        else:
            raise AssertionError("unfinished chart runs must not be treated as reviewed DFT evidence")


def test_dft_rejects_wrong_run_and_stale_selected_chart_snapshot(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(paper_code="B0076", title="DFT stale chart scope", authors=[], pdf_path="missing.pdf")
        other = Paper(paper_code="B0077", title="other paper", authors=[], pdf_path="missing.pdf")
        session.add_all([paper, other])
        session.flush()
        run, figures = _seed_completed_chart_run(session, paper)
        other_run = ExternalAnalysisRun(paper_id=other.id, source="web_ai", source_label="other")
        session.add(other_run)
        session.commit()
        try:
            DFTReviewBundleService(session, get_settings()).build_zip(
                paper.id,
                chart_run_id=other_run.id,
                include_figure_files=False,
            )
        except LookupError:
            pass
        else:
            raise AssertionError("a chart run from another paper must be rejected")

        figures[0].content_summary = "changed after completion"
        session.commit()
        try:
            DFTReviewBundleService(session, get_settings()).build_zip(
                paper.id,
                chart_run_id=run.id,
                include_figure_files=False,
            )
        except FigureTableReviewNotCompletedError as exc:
            assert exc.state["stage_status"] == "stale"
        else:
            raise AssertionError("a changed selected chart snapshot must remain stale")


def test_four_figure_recrop_completion_survives_workspace_refresh_but_real_edit_stales(setup_test_db, tmp_path: Path):
    """Regression for B0076: workspace preparation used to erase RECROP metadata."""
    import fitz

    settings = get_settings()
    pdf_path = settings.storage_paths["pdf"] / f"chart-recrops-{uuid4()}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for page_number in range(4):
        page = document.new_page(width=595.28, height=782.36)
        page.insert_text((72, 72), f"Figure {page_number + 1}")
    document.save(str(pdf_path))
    document.close()

    with Session(setup_test_db) as session:
        image_path = settings.storage_paths["figures"] / f"chart-recrops-{uuid4()}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nchart-recrops")
        paper = Paper(paper_code="B0076", title="four scoped main figures", authors=[], pdf_path=str(pdf_path))
        session.add(paper)
        session.flush()
        figures = [
            PaperFigure(
                paper_id=paper.id,
                figure_label=f"fig_{index}",
                caption=f"Figure {index} complete scientific chart.",
                page=index,
                image_path=image_path.name,
                figure_role="property_data",
                content_summary=f"Figure {index} has complete axes and data.",
                key_elements=["axes", "curves"],
                crop_status="candidate_crop",
                crop_source="test_extraction",
                crop_confidence=0.9,
                prov=[{
                    "image_extraction": "test_extraction",
                    "source": "test_extraction",
                    "confidence": 0.9,
                    "page_no": index,
                }],
            )
            for index in range(1, 5)
        ]
        session.add_all(figures)
        session.flush()
        run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="web_ai",
            source_label="main_figure_review_20260703_fullset",
        )
        session.add(run)
        session.flush()
        session.add_all([
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper.id,
                candidate_type="figure_table_evidence",
                normalized_payload={"target_type": "figure", "target_id": str(figure.id)},
                status="pending",
            )
            for figure in figures
        ])
        session.flush()

        service = EvidenceReviewBundleService(session, settings)
        materials = service._build_materials(paper.id, run_id=run.id)
        evidence_by_figure_id = {
            item["source_record_id"]: item["evidence_id"]
            for item in materials["extracted_figures"]
        }
        actions = []
        for index, figure in enumerate(figures):
            action = {
                "action": "RECROP" if index < 3 else "KEEP",
                "figure_id": str(figure.id),
                "evidence_ids": [evidence_by_figure_id[str(figure.id)]],
                "evidence_checked": True,
                "confidence": 0.99,
                "reason": "Checked against the source PDF page.",
                "figure_role": "property_data",
                "content_summary": f"Figure {index + 1} has complete axes and data.",
                "key_elements": ["axes", "curves"],
            }
            if index < 3:
                action.update({"page": index + 1, "bbox_norm": [0.05, 0.05, 0.95, 0.75]})
            action["local_ai_verification"] = {
                "verified_against_pdf": True,
                "used_tools": ["get_codex_item", "read_paper_page"],
                "verification_note": f"Checked Figure {index + 1} against the source PDF page.",
            }
            actions.append(action)
        payload = {
            **service._return_template(materials),
            "overall_status": "completed",
            "figure_actions": actions,
        }
        payload["review_source"] = {
            "review_source_type": "local_ai",
            "reviewer_label": "test local AI",
            "reviewer_model": "test",
            "tool_capabilities": ["get_codex_item", "read_paper_page"],
        }
        applied = service.apply_result(
            paper.id,
            payload,
            run_id=run.id,
            local_ai_authorized=True,
        )
        assert applied["stage_status"] == "completed"
        session.expire_all()

        # A new service/session view remains complete immediately after apply.
        fresh = EvidenceReviewBundleService(session, settings).get_review_task(paper.id, run_id=run.id)
        assert fresh["stage_status"] == "completed"

        # The workspace refresh must preserve RECROP metadata rather than
        # regenerating candidate_crop from parser provenance.
        workspace_figures = tmp_path / "workspace"
        workspace_figures.mkdir(parents=True)
        PaperWorkbenchService(session, settings)._sync_figure_workspace_files(figures, workspace_figures)
        session.flush()
        session.expire_all()
        after_workspace = EvidenceReviewBundleService(session, settings).get_review_task(paper.id, run_id=run.id)
        assert after_workspace["stage_status"] == "completed"
        restored = session.get(PaperFigure, figures[0].id)
        assert restored.crop_status == "recropped"
        assert restored.crop_source == "offline_evidence_review"

        # Existing completed runs that were damaged by the old workspace
        # refresh shape are reconciled only when the drift is exactly that
        # known three-field rewrite; their evidence/image/geometry still match.
        workspace_service = PaperWorkbenchService(session, settings)
        for figure in figures[:3]:
            legacy = session.get(PaperFigure, figure.id)
            legacy_payload = workspace_service._figure_crop_payload(legacy)
            legacy.crop_status = legacy_payload["crop_status"]
            legacy.crop_source = legacy_payload["crop_source"]
            legacy.crop_confidence = legacy_payload["crop_confidence"]
        session.commit()
        session.expire_all()
        legacy_reconciled = EvidenceReviewBundleService(session, settings).get_review_task(paper.id, run_id=run.id)
        assert legacy_reconciled["stage_status"] == "completed"

        # A genuine edit to a reviewed field remains a stale-snapshot failure.
        restored.content_summary = "manually changed after chart review"
        session.commit()
        session.expire_all()
        stale = EvidenceReviewBundleService(session, settings).get_review_task(paper.id, run_id=run.id)
        assert stale["stage_status"] == "stale"
        assert stale["current_snapshot_fingerprint"] != stale["completed_snapshot_fingerprint"]
