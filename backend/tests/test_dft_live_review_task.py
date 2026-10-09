from __future__ import annotations

from io import BytesIO
import json
from zipfile import ZipFile

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    ExternalAnalysisRun,
    Paper,
    PaperFigure,
    PaperRelationship,
)
from app.services.dft_review_bundle_service import DFTReviewBundleService
from test_dft_review_bundle import _mark_figure_table_review_completed, _seed_review_materials


def _assert_no_private_or_byte_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert not str(key).startswith("_")
            assert "byte" not in str(key).lower()
            _assert_no_private_or_byte_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_private_or_byte_fields(item)


def test_get_review_task_is_read_only_and_returns_live_local_ai_contract(setup_test_db):
    paper_id, row_id = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    settings = get_settings()

    with Session(setup_test_db) as session:
        before_audits = session.scalar(select(func.count()).select_from(AuditLog))
        before_runs = session.scalar(select(func.count()).select_from(ExternalAnalysisRun))
        task = DFTReviewBundleService(session, settings).get_review_task(paper_id)
        after_audits = session.scalar(select(func.count()).select_from(AuditLog))
        after_runs = session.scalar(select(func.count()).select_from(ExternalAnalysisRun))

    assert task["schema_version"] == "dft_live_review_task_v1"
    assert task["generated_at"]
    assert task["paper_id"] == str(paper_id)
    assert task["paper_code"] == "B0078"
    assert task["writeback_paper_id"] == str(paper_id)
    assert task["bundle_fingerprint"]
    assert task["figure_table_completed_snapshot_fingerprint"]
    assert task["chart_scope_type"] == "paper_reviewed_aggregate"
    assert task["chart_run_id"] is None
    assert task["paper"]["paper_code"] == "B0078"
    assert task["writeback"]["paper_id"] == str(paper_id)
    assert {item["source_document_type"] for item in task["source_documents"]} == {
        "main_text",
        "supplementary_information",
    }
    assert task["target_ids"] == [str(row_id)]
    assert task["target_count"] == len(task["target_ids"])
    assert task["evidence_count"] == len(task["evidence_items"])
    assert task["existing_terminal_context_count"] == len(task["existing_terminal_context"])
    assert task["source_pdf_inventory"]
    assert task["targets"][0]["target_id"] == str(row_id)
    assert task["targets"][0]["evidence_ids"] == task["target_evidence_map"][str(row_id)]
    assert task["target_evidence_map"][str(row_id)]
    assert any(item["source_document_type"] == "main_text" for item in task["evidence_items"])
    assert any(
        item["source_document_type"] == "supplementary_information"
        for item in task["evidence_items"]
    )
    for item in task["evidence_items"]:
        assert {
            "source_paper_id",
            "source_paper_code",
            "source_document_type",
            "source_record_id",
            "item_type",
            "page",
            "figure",
            "table",
            "section",
            "original_text",
        } <= set(item)

    review_template = task["local_ai"]["review_result_template"]
    assert review_template["review_source"]["review_source_type"] == "local_ai"
    assert task["review_result_template"] == review_template
    import_template = task["local_ai"]["import_analysis_template"]
    metadata = import_template["raw_payload"]["review_metadata"]
    assert metadata["paper_id"] == str(paper_id)
    assert metadata["review_source"]["review_source_type"] == "local_ai"
    assert metadata["web_ai_review_source"]["review_source_type"] == "local_ai"
    assert metadata["local_ai_verification_required"] is True
    assert import_template["raw_payload"]["local_ai_verification_plan"]["evidence_check_count"] >= 1
    assert task["local_ai_writeback_contract"]["writeback"]["export_authorization"] == {
        "decisions": ["PASS", "REVISE"],
        "required_recommended_action": "ready_for_ml_export",
        "otherwise": "not_authorized",
    }
    assert before_audits == after_audits
    assert before_runs == after_runs
    _assert_no_private_or_byte_fields(task)


def test_get_review_task_recomputes_fingerprints_after_source_state_changes(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    settings = get_settings()

    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, settings)
        before = service.get_review_task(paper_id)
        figure = session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).first()
        figure.caption = "Changed current live DFT caption"
        session.flush()
        after = service.get_review_task(paper_id)

    assert after["bundle_fingerprint"] != before["bundle_fingerprint"]
    assert after["figure_table_completed_snapshot_fingerprint"] != before[
        "figure_table_completed_snapshot_fingerprint"
    ]


def test_get_review_task_does_not_change_existing_offline_zip_contract(setup_test_db):
    paper_id, _ = _seed_review_materials(setup_test_db)
    _mark_figure_table_review_completed(setup_test_db, paper_id)
    settings = get_settings()

    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, settings)
        live_task = service.get_review_task(paper_id)
        bundle = service.build_zip(paper_id)

    assert bundle["manifest"]["bundle_fingerprint"] == live_task["bundle_fingerprint"]
    with ZipFile(BytesIO(bundle["content"])) as archive:
        assert "manifest.json" in archive.namelist()
        template = json.loads(archive.read("return_template.json"))
    assert template["review_source"]["review_source_type"] == "web_ai"
    assert bundle["manifest"]["retention_policy"] == "generated_in_memory_not_persisted_on_server"


def _seed_main_writeback_si_evidence(engine, *, multiple_si=False):
    with Session(engine) as session:
        main = Paper(
            title="Main paper with SI-anchored DFT rows",
            paper_code="B-SI-SOURCE",
            paper_type="article",
            authors=[],
            pdf_path="main-si-source.pdf",
        )
        si = Paper(
            title="Supporting information",
            paper_code="S-SI-SOURCE",
            paper_type="supplementary",
            authors=[],
            pdf_path="si-source.pdf",
        )
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
        si_two = None
        if multiple_si:
            si_two = Paper(
                title="Second supporting information",
                paper_code="S-SI-SOURCE-2",
                paper_type="supplementary",
                authors=[],
                pdf_path="si-source-2.pdf",
            )
            session.add(si_two)
            session.flush()
            session.add(
                PaperRelationship(
                    source_paper_id=main.id,
                    target_paper_id=si_two.id,
                    relationship_type="supplementary",
                    created_by="test",
                )
            )
        sample = CatalystSample(paper_id=main.id, name="Fe-N4")
        session.add(sample)
        session.flush()
        evidence_rows = [
            (
                "Table S2 reports the DFT adsorption energy.",
                {
                    "source_document_type": "supplementary_information",
                    "corrected_value": {
                        "configuration_index": 2,
                        "method": "DFT",
                    },
                    "source_location": {
                        "source_document_type": "supplementary_information",
                        "page": 17,
                        "table": "Table S2",
                    },
                },
            ),
            (
                "Table S3 reports the DFT free energy.",
                {
                    "corrected_value": {"bond_pair": "Li2-S"},
                    "material_binding": {
                        "evidence_anchor": {
                            "source_document_type": "supplementary_information",
                            "page": 18,
                            "table": "Table S3",
                        }
                    }
                },
            ),
            (
                "Figure S8 reports the DFT charge transfer.",
                {
                    "corrected_value": {
                        "environment": "2DOL",
                        "solvent_complex": "2DOL/Li2S8",
                    },
                    "material_binding": {
                        "source_location": {
                            "source_document_type": "supplementary_information",
                            "page": 7,
                            "figure": "Figure S8",
                        }
                    }
                },
            ),
        ]
        rows = []
        for index, (evidence_text, evidence_payload) in enumerate(evidence_rows):
            row = DFTResult(
                paper_id=main.id,
                catalyst_sample_id=sample.id,
                property_type=("adsorption_energy", "free_energy", "charge_transfer")[index],
                value=float(index + 1),
                unit="eV" if index < 2 else "e",
                source_section="DFT results",
                evidence_text=evidence_text,
                evidence_payload=evidence_payload,
            )
            rows.append(row)
        session.add_all(rows)
        session.commit()
        return main.id, si.id, si_two.id if si_two is not None else None, [row.id for row in rows]


def test_live_task_separates_main_item_owner_from_unique_si_pdf_source(setup_test_db):
    paper_id, si_id, _, row_ids = _seed_main_writeback_si_evidence(setup_test_db)

    with Session(setup_test_db) as session:
        task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)

    dft_evidence = {
        item["page"]: item
        for item in task["evidence_items"]
        if item["item_type"] == "dft_result"
    }
    assert set(dft_evidence) == {7, 17, 18}
    for item in dft_evidence.values():
        assert item["evidence_id"].startswith("si:text:")
        assert item["item_paper_id"] == str(paper_id)
        assert item["item_paper_code"] == "B-SI-SOURCE"
        assert item["source_paper_id"] == str(si_id)
        assert item["source_paper_code"] == "S-SI-SOURCE"
        assert item["source_document_type"] == "supplementary_information"

    targets = {item["target_id"]: item for item in task["targets"]}
    assert set(targets) == {str(row_id) for row_id in row_ids}
    for row_id in row_ids:
        target = targets[str(row_id)]
        assert target["item_paper_id"] == str(paper_id)
        assert target["source_paper_id"] == str(si_id)
        assert task["target_evidence_map"][str(row_id)]

    assert targets[str(row_ids[0])]["configuration_index"] == 2
    assert targets[str(row_ids[0])]["method"] == "DFT"
    assert targets[str(row_ids[0])]["evidence_details"]["configuration_index"] == 2
    assert targets[str(row_ids[1])]["bond_pair"] == "Li2-S"
    assert targets[str(row_ids[2])]["environment"] == "2DOL"
    assert targets[str(row_ids[2])]["solvent_complex"] == "2DOL/Li2S8"

    checks = task["import_analysis_template"]["raw_payload"]["local_ai_verification_plan"][
        "unique_evidence_checks"
    ]
    assert {(check["source_paper_id"], check["page"]) for check in checks} == {
        (str(si_id), 7),
        (str(si_id), 17),
        (str(si_id), 18),
    }
    assert all(check["item_paper_id"] == str(paper_id) for check in checks)
    assert all(check["item_paper_code"] == "B-SI-SOURCE" for check in checks)
    assert task["local_ai_writeback_contract"]["required_local_ai_verification"][
        "get_codex_item_arguments"
    ]["paper_id"].startswith("item_paper_id")


def test_live_task_does_not_guess_si_when_multiple_si_documents_have_no_source_id(setup_test_db):
    paper_id, _, si_two_id, _ = _seed_main_writeback_si_evidence(
        setup_test_db,
        multiple_si=True,
    )
    assert si_two_id is not None

    with Session(setup_test_db) as session:
        task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)

    dft_evidence = [item for item in task["evidence_items"] if item["item_type"] == "dft_result"]
    assert dft_evidence
    assert all(item["item_paper_id"] == str(paper_id) for item in dft_evidence)
    assert all(item["evidence_id"].startswith("main:text:") for item in dft_evidence)
    assert all(item["source_paper_id"] == str(paper_id) for item in dft_evidence)
    assert all(item["source_document_type"] == "main_text" for item in dft_evidence)
    checks = task["import_analysis_template"]["raw_payload"]["local_ai_verification_plan"][
        "unique_evidence_checks"
    ]
    assert all(check["source_paper_id"] == str(paper_id) for check in checks)
