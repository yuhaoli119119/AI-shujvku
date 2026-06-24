from __future__ import annotations

import os
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.services.paper_query import PaperQueryService
from app.utils.review_safety import is_export_eligible_extraction


def _gate(eligible: bool):
    return SimpleNamespace(eligible=eligible, reasons=[] if eligible else ["missing_review"], review_status="verified")


def _audit(source: str, decision: str) -> dict:
    return {"source": source, "source_label": source, "decision": decision}


def test_ai_review_display_status_matrix_matches_export_gate_authority():
    exportable_historical_reject = DFTReviewQueueService.build_ai_review_display_status(
        gate=_gate(True),
        object_review_audits=[_audit("ai-1", "REJECT")],
        conflicts=[],
    )
    assert exportable_historical_reject["status"] == "exportable_with_historical_reject"
    assert exportable_historical_reject["label"] == "AI 意见已收敛"

    exportable_historical_conflict = DFTReviewQueueService.build_ai_review_display_status(
        gate=_gate(True),
        object_review_audits=[_audit("ai-1", "REJECT"), _audit("ai-2", "PASS")],
        conflicts=[{"field_name": "dft_results", "conflict_types": ["decision_conflict"]}],
    )
    assert exportable_historical_conflict["status"] == "exportable_with_historical_reject"
    assert exportable_historical_conflict["label"] == "AI 意见已收敛"
    assert exportable_historical_conflict["class_name"] != "failed"

    rejected = DFTReviewQueueService.build_ai_review_display_status(
        gate=_gate(False),
        object_review_audits=[
            {**_audit("same-ai", "REJECT"), "candidate_id": "submission-1"},
            {**_audit("same-ai", "REJECTED"), "candidate_id": "submission-2"},
        ],
        conflicts=[],
    )
    assert rejected["status"] == "rejected"
    assert rejected["label"] == "AI 一致拒绝"

    conflict = DFTReviewQueueService.build_ai_review_display_status(
        gate=_gate(False),
        object_review_audits=[_audit("ai-1", "REJECT"), _audit("ai-2", "PASS")],
        conflicts=[{"field_name": "value", "conflict_types": ["decision_conflict"]}],
    )
    assert conflict["status"] == "conflict"
    assert conflict["label"] == "AI 冲突"

    exportable_proposed = DFTReviewQueueService.build_ai_review_display_status(
        gate=_gate(True),
        object_review_audits=[_audit("ai-1", "PROPOSED")],
        conflicts=[],
    )
    assert exportable_proposed["status"] == "converged_adopted"
    assert exportable_proposed["label"] == "已采纳 AI 修正"


def test_non_dft_detail_dedupe_key_still_uses_existing_display_semantics():
    payload = {
        "source_label": "same-ai",
        "decision": "PASS",
        "field_name": "caption",
        "corrected_value": "updated",
        "evidence_location": {"page": 3},
    }

    assert PaperQueryService._object_review_audit_dedupe_key("figures", payload) == (
        "same-ai",
        "pass",
        "caption",
        '"updated"',
        '{"page": 3}',
    )


def test_paper_detail_dft_item_exposes_converged_status_for_exportable_historical_reject():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            paper = Paper(title="DFT display paper", pdf_path="display.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", metal_centers=["Fe"])
            session.add(catalyst)
            session.flush()
            row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                property_type="adsorption_energy",
                adsorbate="Li2S4",
                value=-1.23,
                unit="eV",
                evidence_text="Table 1 reports Li2S4 adsorption energy of -1.23 eV on Fe-N4.",
                candidate_status="ML_Ready",
            )
            session.add(row)
            session.flush()
            session.add(
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="table",
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    page=4,
                    evidence_text=row.evidence_text,
                    locator_status="exact_page",
                    locator_confidence=0.95,
                )
            )
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    original_value=row.value,
                    reviewed_value=row.value,
                    unit=row.unit,
                    evidence_text=row.evidence_text,
                    reviewer_status="verified",
                    reviewer="human_review",
                    target_resolution_status="active",
                    last_resolved_target_id=str(row.id),
                )
            )
            run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="older_ai")
            session.add(run)
            session.flush()
            session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=paper.id,
                    candidate_type="object_review_audit",
                    normalized_payload={
                        "target_type": "dft_results",
                        "target_id": str(row.id),
                        "field_name": "dft_results",
                        "decision": "REJECT",
                        "reason": "Older AI opinion before the row was verified.",
                    },
                    status="candidate",
                )
            )
            session.commit()

            detail = PaperQueryService(session).get_paper_detail(paper.id)

            assert detail is not None
            payload = detail.dft_results_items[0]
            assert payload.ai_review_display_status == "exportable_with_historical_reject"
            assert payload.ai_review_display_label == "AI 意见已收敛"
    finally:
        engine.dispose()


def test_rejected_dft_review_is_not_export_gate_eligible():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            paper = Paper(title="Rejected gate paper", pdf_path="rejected.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            row = DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                adsorbate="H",
                value=0.1,
                unit="eV",
                evidence_text="Evidence text.",
                candidate_status="Rejected",
            )
            session.add(row)
            session.flush()
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    original_value=row.value,
                    reviewed_value=None,
                    unit=row.unit,
                    evidence_text=row.evidence_text,
                    reviewer_status="rejected",
                    reviewer="ai_review",
                    target_resolution_status="active",
                    last_resolved_target_id=str(row.id),
                )
            )
            session.commit()

            gate = is_export_eligible_extraction(session, row, target_type="dft_results")

            assert gate.eligible is False
            assert "unsafe_review" in gate.reasons
    finally:
        engine.dispose()
