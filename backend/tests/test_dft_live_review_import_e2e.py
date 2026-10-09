from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

import fitz
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    ModuleWriteLock,
    Paper,
)
from app.mcp.context import mcp_auth_context
from app.mcp.server import import_analysis
from app.services.dft_review_bundle_service import DFTReviewBundleService
from app.services.dft_review_service import DFTResultReviewService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.verification_session_service import VerificationSessionService


MCP_PROPOSER_KEY = "test-propose-only-key"
MCP_PROPOSER_OWNER = "ordinary_ide_ai"


def _seed_live_dft_review(engine) -> tuple[UUID, UUID, UUID]:
    settings = get_settings()
    pdf_root = settings.storage_paths["pdf"]
    pdf_root.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_root / "dft-live-review-e2e.pdf"
    document = fitz.open()
    for page_number in range(1, 6):
        page = document.new_page()
        page.insert_text(
            (72, 72),
            f"Page {page_number}: Fe-N4 DFT adsorption and free-energy evidence.",
        )
    document.save(str(pdf_path))
    document.close()

    with Session(engine) as session:
        paper = Paper(
            title="Live DFT review import E2E",
            paper_code="B-E2E-LIVE",
            paper_type="article",
            pdf_path=pdf_path.name,
            authors=["Test Author"],
            abstract="A paper containing directly anchored DFT results.",
        )
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4",
            catalyst_type="single_atom_catalyst",
            metal_centers=["Fe"],
        )
        session.add(sample)
        session.flush()
        pass_row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            adsorbate="Li2S",
            reaction_step="adsorption",
            property_type="adsorption_energy",
            value=-1.20,
            unit="eV",
            source_section="DFT results",
            evidence_text="Fe-N4 binds Li2S with an adsorption energy of -1.20 eV.",
            evidence_payload={
                "page": 3,
                "quoted_text": "Fe-N4 binds Li2S with an adsorption energy of -1.20 eV.",
                "material_identity": "Fe-N4",
            },
        )
        revise_row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            adsorbate="LiS",
            reaction_step="conversion",
            property_type="free_energy",
            value=0.55,
            unit="eV",
            source_section="DFT results",
            evidence_text="The corrected LiS conversion free energy is 0.42 eV on Fe-N4.",
            evidence_payload={
                "page": 4,
                "quoted_text": "The corrected LiS conversion free energy is 0.42 eV on Fe-N4.",
                "material_identity": "Fe-N4",
            },
        )
        session.add_all([pass_row, revise_row])
        session.flush()
        session.add_all(
            [
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="pdf",
                    page=3,
                    target_type="dft_results",
                    target_id=str(pass_row.id),
                    field_name="dft_results",
                    evidence_text=pass_row.evidence_text,
                    locator_status="resolved",
                    locator_confidence=1.0,
                    parser_source="test",
                ),
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="pdf",
                    page=4,
                    target_type="dft_results",
                    target_id=str(revise_row.id),
                    field_name="dft_results",
                    evidence_text=revise_row.evidence_text,
                    locator_status="resolved",
                    locator_confidence=1.0,
                    parser_source="test",
                ),
            ]
        )
        session.commit()
        return paper.id, pass_row.id, revise_row.id


def _task_targets(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = task.get("targets")
    assert isinstance(targets, list) and targets, "live task must expose current DFT targets"
    mapped: dict[str, dict[str, Any]] = {}
    for target in targets:
        assert isinstance(target, dict)
        target_id = str(target.get("target_id") or target.get("id") or "").strip()
        assert target_id, "every live task target needs a stable target_id"
        mapped[target_id] = target
    return mapped


def _task_evidence(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence = task.get("evidence_items")
    if isinstance(evidence, dict):
        mapped = {str(key): value for key, value in evidence.items() if isinstance(value, dict)}
    else:
        assert isinstance(evidence, list) and evidence, "live task must expose current evidence"
        mapped = {
            str(item.get("evidence_id")): item
            for item in evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
    assert mapped
    for evidence_id, item in mapped.items():
        assert evidence_id
        assert item.get("source_paper_id"), "evidence must retain real source-paper provenance"
        assert item.get("page") or item.get("page_start"), "evidence must retain a real source page"
    return mapped


def _evidence_id_for_target(evidence: dict[str, dict[str, Any]], target_id: UUID) -> str:
    for evidence_id, item in evidence.items():
        if str(item.get("source_record_id") or "") == str(target_id):
            return evidence_id
    raise AssertionError(f"live task did not map target {target_id} to real evidence")


def _review_payload_from_task(
    task: dict[str, Any],
    *,
    pass_row_id: UUID,
    revise_row_id: UUID,
) -> dict[str, Any]:
    targets = _task_targets(task)
    evidence = _task_evidence(task)
    assert {str(pass_row_id), str(revise_row_id)} <= set(targets)
    pass_evidence_id = _evidence_id_for_target(evidence, pass_row_id)
    revise_evidence_id = _evidence_id_for_target(evidence, revise_row_id)
    target_evidence_map = task.get("target_evidence_map")
    assert isinstance(target_evidence_map, dict)
    assert pass_evidence_id in target_evidence_map[str(pass_row_id)]
    assert revise_evidence_id in target_evidence_map[str(revise_row_id)]
    assert targets[str(pass_row_id)]["evidence_ids"] == target_evidence_map[str(pass_row_id)]
    assert targets[str(revise_row_id)]["evidence_ids"] == target_evidence_map[str(revise_row_id)]
    assert all("evidence_payload" not in item for item in evidence.values())

    template = deepcopy(task.get("review_result_template"))
    assert isinstance(template, dict), "live task must provide review_result_template"
    paper = task.get("paper")
    assert isinstance(paper, dict)
    template.update(
        {
            "schema_version": "offline_dft_review_result_v1",
            "bundle_fingerprint": task["bundle_fingerprint"],
            "figure_table_completed_snapshot_fingerprint": task.get(
                "figure_table_completed_snapshot_fingerprint"
            ),
            "paper_id": paper["paper_id"],
            "paper_code": paper["paper_code"],
            "chart_scope_type": "paper_reviewed_aggregate",
            "chart_run_id": None,
            "review_mode": task["review_mode"],
            "review_source": {
                "review_source_type": "local_ai",
                "reviewer_label": "Codex E2E local AI",
                "reviewer_model": "test",
                "tool_capabilities": ["get_codex_item", "read_paper_page"],
            },
            "overall_status": "uncertain",
            "coverage_acknowledgement": None,
            "object_review_audits": [
                {
                    "target_type": "dft_results",
                    "target_id": str(pass_row_id),
                    "field_name": "dft_results",
                    "decision": "PASS",
                    "evidence_checked": True,
                    "evidence_ids": [pass_evidence_id],
                    "corrected_value": None,
                    "confidence": 0.99,
                    "reason": "The current value matches the source-paper evidence.",
                    "blocking_errors": [],
                    "recommended_action": "ready_for_ml_export",
                    "dedupe_analysis": None,
                },
                {
                    "target_type": "dft_results",
                    "target_id": str(revise_row_id),
                    "field_name": "dft_results",
                    "decision": "REVISE",
                    "evidence_checked": True,
                    "evidence_ids": [revise_evidence_id],
                    "corrected_value": {
                        "material_identity": "Fe-N4",
                        "property_type": "free_energy",
                        "value": 0.42,
                        "unit": "eV",
                        "adsorbate": "LiS",
                        "reaction_step": "conversion",
                    },
                    "confidence": 0.98,
                    "reason": "The source page reports 0.42 eV, not 0.55 eV.",
                    "blocking_errors": [],
                    "recommended_action": "ready_for_ml_export",
                    "dedupe_analysis": None,
                },
            ],
            "uncertainties": [],
            "notes": [],
        }
    )
    return template


def _validated_local_ai_import_request(
    engine,
    paper_id: UUID,
    review_payload: dict[str, Any],
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    with Session(engine) as session:
        validation = DFTReviewBundleService(session, get_settings()).validate_result(
            paper_id,
            review_payload,
        )
    assert validation["valid"] is True, validation["errors"]
    request = validation["import_analysis_request"]
    assert isinstance(request, dict)

    import_template = task.get("import_analysis_template")
    assert isinstance(import_template, dict), "live task must provide import_analysis_template"
    for key in ("paper_id", "source", "source_label", "auto_apply_review_rules", "raw_payload"):
        assert key in import_template
    assert import_template["paper_id"] == request["paper_id"]
    assert import_template["source"] == request["source"]
    assert import_template["auto_apply_review_rules"] is True

    for audit in request["raw_payload"]["object_review_audits"]:
        requirements = audit["required_evidence_checks"]
        assert requirements
        assert all(item["source_paper_id"] == str(paper_id) for item in requirements)
        assert all(int(item["page"]) > 0 for item in requirements)
        audit["source"] = "local_ai"
        audit["source_label"] = "local_ai_after_pdf_evidence_check"
        audit["agent_role"] = "local_ai_pdf_verifier"
        audit["local_ai_verification"] = {
            "verified_against_pdf": True,
            "used_tools": ["get_codex_item", "read_paper_page"],
            "checked_evidence_ids": [item["evidence_id"] for item in requirements],
            "checked_pages": [
                {"paper_id": item["source_paper_id"], "page": item["page"]}
                for item in audit["required_page_checks"]
            ],
            "verification_note": "Checked the real target object and source-paper PDF page.",
        }
    return request


def _acquire_dft_lock(engine, paper_id: UUID) -> str:
    with Session(engine) as session:
        lock = ModuleWriteLockService(session).acquire(
            paper_id=paper_id,
            module_name="dft_results",
            locked_by=MCP_PROPOSER_OWNER,
            meta={"source": "test_dft_live_review_import_e2e"},
        )
        session.commit()
        return lock.lock_token


def _call_import_analysis(request: dict[str, Any], *, lock_token: str) -> dict[str, Any]:
    with mcp_auth_context(MCP_PROPOSER_KEY):
        return import_analysis(
            paper_id=request["paper_id"],
            source=request["source"],
            source_label=request["source_label"],
            raw_payload=request["raw_payload"],
            auto_apply_review_rules=True,
            reviewer=MCP_PROPOSER_OWNER,
            write_lock_token=lock_token,
        )


def test_live_task_import_records_pass_and_revise_as_candidates_without_dft_finalization(
    setup_test_db,
):
    paper_id, pass_row_id, revise_row_id = _seed_live_dft_review(setup_test_db)
    with Session(setup_test_db) as session:
        task_before = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
    assert task_before["bundle_fingerprint"]
    assert task_before["offline_zip_policy"]["purpose"] == "web_ai_third_party_or_offline_review"
    review_payload = _review_payload_from_task(
        task_before,
        pass_row_id=pass_row_id,
        revise_row_id=revise_row_id,
    )
    request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        review_payload,
        task=task_before,
    )
    lock_token = _acquire_dft_lock(setup_test_db, paper_id)

    imported = _call_import_analysis(request, lock_token=lock_token)

    readback = imported["auto_apply_summary"]["dft_readback"]
    for target_id in (str(pass_row_id), str(revise_row_id)):
        assert readback["candidate_status"][target_id] == "system_candidate"
        assert readback["export_safety"][target_id]["eligible"] is False
    assert readback["conflicts"] == []

    with Session(setup_test_db) as session:
        pass_row = session.get(DFTResult, pass_row_id)
        revise_row = session.get(DFTResult, revise_row_id)
        assert pass_row is not None and pass_row.value == pytest.approx(-1.20)
        assert revise_row is not None and revise_row.value == pytest.approx(0.55)
        assert pass_row.candidate_status == "system_candidate"
        assert revise_row.candidate_status == "system_candidate"
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
            )
        ).all()
        assert reviews == []
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper_id)
        ).all()
        assert len(candidates) == 2
        assert {candidate.status for candidate in candidates} <= {
            "candidate",
            "pending",
            "requires_resolution",
            "pending_ai_verification",
        }
        assert all(candidate.materialized_target_id is None for candidate in candidates)
        task_after = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
        assert task_after["target_ids"] == task_before["target_ids"]
        lock = session.scalar(select(ModuleWriteLock).where(ModuleWriteLock.lock_token == lock_token))
        assert lock is not None and lock.status == "active"


def test_missing_recommended_action_cannot_write_or_become_exportable(setup_test_db):
    paper_id, pass_row_id, revise_row_id = _seed_live_dft_review(setup_test_db)
    with Session(setup_test_db) as session:
        task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
    review_payload = _review_payload_from_task(
        task,
        pass_row_id=pass_row_id,
        revise_row_id=revise_row_id,
    )
    review_payload["object_review_audits"][0]["recommended_action"] = None
    request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        review_payload,
        task=task,
    )
    lock_token = _acquire_dft_lock(setup_test_db, paper_id)

    with pytest.raises(ValueError, match="recommended_action"):
        _call_import_analysis(request, lock_token=lock_token)

    with Session(setup_test_db) as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        assert {row.candidate_status for row in rows} == {"system_candidate"}
        assert session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)
        ).all() == []
        assert session.scalars(
            select(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper_id)
        ).all() == []
        from app.utils.review_safety import is_export_eligible_extraction

        gates = {
            str(row.id): is_export_eligible_extraction(session, row, target_type="dft_results")
            for row in rows
        }
        assert all(not gate.eligible for gate in gates.values())


def test_needs_human_import_stays_pending_and_is_not_exportable(setup_test_db):
    paper_id, pass_row_id, revise_row_id = _seed_live_dft_review(setup_test_db)
    with Session(setup_test_db) as session:
        task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
    review_payload = _review_payload_from_task(
        task,
        pass_row_id=pass_row_id,
        revise_row_id=revise_row_id,
    )
    review_payload["object_review_audits"][1].update(
        {
            "decision": "NEEDS_HUMAN",
            "corrected_value": None,
            "evidence_checked": True,
            "recommended_action": "send_to_human_reviewer",
            "reason": "The page evidence needs a human scientific judgment.",
        }
    )
    request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        review_payload,
        task=task,
    )
    lock_token = _acquire_dft_lock(setup_test_db, paper_id)

    imported = _call_import_analysis(request, lock_token=lock_token)

    readback = imported["auto_apply_summary"]["dft_readback"]
    assert readback["export_safety"][str(pass_row_id)]["eligible"] is False
    assert readback["export_safety"][str(revise_row_id)]["eligible"] is False
    assert readback["unfinished_items"]
    with Session(setup_test_db) as session:
        pass_row = session.get(DFTResult, pass_row_id)
        needs_human_row = session.get(DFTResult, revise_row_id)
        assert pass_row is not None and pass_row.candidate_status == "system_candidate"
        assert needs_human_row is not None and needs_human_row.value == pytest.approx(0.55)
        assert needs_human_row.candidate_status == "system_candidate"
        candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper_id)
        ).all()
        by_target = {
            str((candidate.normalized_payload or {}).get("target_id")): candidate
            for candidate in candidates
        }
        assert by_target[str(revise_row_id)].status == "requires_resolution"


def test_import_transaction_failure_rolls_back_dft_and_preserves_caller_owned_lock(
    setup_test_db,
    monkeypatch,
):
    paper_id, pass_row_id, revise_row_id = _seed_live_dft_review(setup_test_db)
    with Session(setup_test_db) as session:
        task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
    review_payload = _review_payload_from_task(
        task,
        pass_row_id=pass_row_id,
        revise_row_id=revise_row_id,
    )
    request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        review_payload,
        task=task,
    )
    lock_token = _acquire_dft_lock(setup_test_db, paper_id)

    def fail_after_dft_mutation(self, **kwargs):
        row = self.session.get(DFTResult, revise_row_id)
        assert row is not None
        row.value = 999.0
        self.session.add(row)
        self.session.flush()
        raise RuntimeError("forced_e2e_transaction_failure")

    monkeypatch.setattr(
        VerificationSessionService,
        "apply_import_rules_for_paper",
        fail_after_dft_mutation,
    )

    with pytest.raises(RuntimeError, match="forced_e2e_transaction_failure"):
        _call_import_analysis(request, lock_token=lock_token)

    with Session(setup_test_db) as session:
        assert session.get(DFTResult, revise_row_id).value == pytest.approx(0.55)
        assert session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)
        ).all() == []
        assert session.scalars(
            select(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper_id)
        ).all() == []
        lock = session.scalar(select(ModuleWriteLock).where(ModuleWriteLock.lock_token == lock_token))
        assert lock is not None
        assert lock.status == "active"
        assert lock.locked_by == MCP_PROPOSER_OWNER


def test_explicit_ai_rejection_stays_candidate_and_does_not_call_terminal_review_service(
    setup_test_db,
    monkeypatch,
):
    paper_id, pass_row_id, revise_row_id = _seed_live_dft_review(setup_test_db)
    with Session(setup_test_db) as session:
        automatic_task = DFTReviewBundleService(session, get_settings()).get_review_task(paper_id)
    initial_payload = _review_payload_from_task(
        automatic_task,
        pass_row_id=pass_row_id,
        revise_row_id=revise_row_id,
    )
    initial_request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        initial_payload,
        task=automatic_task,
    )
    lock_token = _acquire_dft_lock(setup_test_db, paper_id)
    _call_import_analysis(initial_request, lock_token=lock_token)

    with Session(setup_test_db) as session:
        service = DFTReviewBundleService(session, get_settings())
        automatic_after_terminal = service.get_review_task(paper_id)
        terminal_row = session.get(DFTResult, pass_row_id)
        assert terminal_row is not None
        sample_id = terminal_row.catalyst_sample_id
        assert sample_id is not None
        explicit_sample_task = service.get_review_task(
            paper_id,
            catalyst_sample_id=sample_id,
        )
        explicit_task = service.get_review_task(
            paper_id,
            dft_result_ids=[pass_row_id],
        )

    assert set(automatic_after_terminal["target_ids"]) == {
        str(pass_row_id),
        str(revise_row_id),
    }
    assert set(explicit_sample_task["target_ids"]) == {
        str(pass_row_id),
        str(revise_row_id),
    }
    assert explicit_sample_task["explicit_review"] is True
    assert explicit_sample_task["catalyst_sample_id"] == str(sample_id)
    assert explicit_task["target_ids"] == [str(pass_row_id)]
    assert explicit_task["dft_result_ids"] == [str(pass_row_id)]

    reject_payload = deepcopy(explicit_task["review_result_template"])
    reject_payload.update(
        {
            "review_source": {
                "review_source_type": "local_ai",
                "reviewer_label": "Codex explicit terminal re-review",
                "reviewer_model": "test",
                "tool_capabilities": ["get_codex_item", "read_paper_page"],
            },
            "overall_status": "uncertain",
            "coverage_acknowledgement": None,
            "object_review_audits": [
                {
                    "target_type": "dft_results",
                    "target_id": str(pass_row_id),
                    "field_name": "dft_results",
                    "decision": "REJECT",
                    "evidence_checked": True,
                    "evidence_ids": explicit_task["target_evidence_map"][str(pass_row_id)],
                    "corrected_value": None,
                    "confidence": 0.99,
                    "reason": "Explicit PDF re-review found that this terminal DFT row is invalid.",
                    "blocking_errors": [],
                    "recommended_action": "reject_candidate",
                    "dedupe_analysis": None,
                }
            ],
            "uncertainties": [],
            "notes": [],
        }
    )
    reject_request = _validated_local_ai_import_request(
        setup_test_db,
        paper_id,
        reject_payload,
        task=explicit_task,
    )

    original_reject = DFTResultReviewService.reject_result
    reject_calls: list[UUID] = []

    def tracked_reject(self, **kwargs):
        reject_calls.append(kwargs["result_id"])
        return original_reject(self, **kwargs)

    monkeypatch.setattr(DFTResultReviewService, "reject_result", tracked_reject)
    _call_import_analysis(reject_request, lock_token=lock_token)

    assert reject_calls == []
    with Session(setup_test_db) as session:
        rejected = session.get(DFTResult, pass_row_id)
        assert rejected is not None
        assert rejected.candidate_status == "system_candidate"
        assert session.scalar(select(func.count()).select_from(DFTResult)) == 2
        assert session.scalar(select(func.count()).select_from(CatalystSample)) == 1
        rejection_candidates = session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.paper_id == paper_id,
            )
        ).all()
        assert rejection_candidates
        assert {candidate.status for candidate in rejection_candidates} <= {
            "candidate",
            "pending",
            "requires_resolution",
            "pending_ai_verification",
        }
        assert all(candidate.materialized_target_id is None for candidate in rejection_candidates)
