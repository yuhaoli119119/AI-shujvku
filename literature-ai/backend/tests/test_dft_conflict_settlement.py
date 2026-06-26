from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    CatalystSample,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    Paper,
)
from app.main import app
from app.services.paper_query import PaperQueryService
from app.services.review_conflict_service import ReviewConflictAggregationService


def _session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _seed_dft_audit(
    session,
    *,
    paper_id,
    target_id,
    field_name: str,
    decision: str,
    corrected_value: Any,
    confidence: float = 0.91,
    status: str = "candidate",
    source: str = "ai_dft_audit",
):
    run = session.query(ExternalAnalysisRun).filter(ExternalAnalysisRun.paper_id == paper_id).first()
    if run is None:
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="assigned_dft_audit",
            source_label="Assigned AI DFT audit",
            normalized_payload={},
            mapping_status="normalized",
        )
        session.add(run)
        session.flush()
    session.add(
        ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper_id,
            candidate_type="object_review_audit",
            status=status,
            confidence=confidence,
            normalized_payload={
                "paper_id": str(paper_id),
                "target_type": "dft_results",
                "target_id": str(target_id),
                "field_name": field_name,
                "source": source,
                "source_label": source,
                "agent_role": "dft_auditor",
                "model_name": f"{source}-model",
                "decision": decision,
                "corrected_value": corrected_value,
                "confidence": confidence,
                "reason": f"{source} review",
                "evidence_payload": {
                    "evidence_text": "Table evidence supports this DFT value.",
                    "locator": {"page": 5, "locator_status": "exact_page"},
                },
                "evidence_location": {"page": 5, "locator_status": "exact_page"},
            },
        )
    )


def _add_field_review(
    session,
    *,
    paper_id,
    target_id,
    field_name: str,
    status: str,
    value: Any,
    unit: str | None = None,
):
    session.add(
        ExtractionFieldReview(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=str(target_id),
            field_name=field_name,
            original_value=value,
            reviewed_value=value if status == "verified" else None,
            unit=unit,
            evidence_text="Reviewer checked the source table.",
            reviewer_status=status,
            reviewer="test_reviewer",
            reviewer_note=f"{field_name} {status}",
            target_resolution_status="active",
        )
    )


def test_verified_dft_field_reviews_suppress_old_whole_row_ai_conflict(setup_test_db):
    Session = _session_factory(setup_test_db)
    with Session() as session:
        paper = Paper(title="Settled whole-row DFT conflict", pdf_path="settled.pdf")
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Co-N3Cl1")
        session.add(catalyst)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            reaction_step="adsorption",
            value=-1.59,
            unit="eV",
            evidence_text="strengthened binding energy of -1.59 eV",
            candidate_status="ML_Ready",
        )
        session.add(row)
        session.flush()
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=row.id,
            field_name="dft_results",
            decision="PROPOSED",
            corrected_value={
                "value": -1.59,
                "unit": "eV",
                "property_type": "adsorption_energy",
                "adsorbate": "Li2S6",
                "reaction_step": "Li2S6 adsorption",
                "material_identity": "Co-N3Cl1",
            },
            source="ai_old_step",
        )
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=row.id,
            field_name="dft_results",
            decision="ACCEPT",
            corrected_value={
                "value": -1.59,
                "unit": "eV",
                "property_type": "adsorption_energy",
                "adsorbate": "Li2S6",
                "reaction_step": "adsorption",
                "material_identity": "Co-N3Cl1",
            },
            source="ai_current_step",
        )
        _add_field_review(session, paper_id=paper.id, target_id=row.id, field_name="reaction_step", status="verified", value="adsorption")
        session.commit()

        historical = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, active_only=False)
        active = ReviewConflictAggregationService(session).list_conflicts(paper_id=paper.id, active_only=True)
        by_target = ReviewConflictAggregationService(session).conflicts_by_target(
            paper_ids={paper.id},
            target_type="dft_results",
            target_ids={str(row.id)},
        )

    assert historical["conflict_count"] == 1
    assert historical["rows"][0]["affected_field_names"] == ["reaction_step"]
    assert active["conflict_count"] == 0
    assert by_target == {}


def test_accept_ai_for_dft_conflict_writes_review_and_closes_active_conflict(setup_test_db):
    Session = _session_factory(setup_test_db)
    with Session() as session:
        paper = Paper(title="Accept AI closes DFT conflict", pdf_path="accept-ai.pdf")
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            reaction_step="adsorption",
            value=-1.20,
            unit="eV",
            evidence_text="The adsorption energy is -1.20 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=row.id,
            field_name="value",
            decision="PASS",
            corrected_value=-1.20,
            confidence=0.95,
            source="ai_keep_current",
        )
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=row.id,
            field_name="value",
            decision="REVISE",
            corrected_value=-1.30,
            confidence=0.70,
            source="ai_revise_value",
        )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    client = TestClient(app)
    before = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}&target_type=dft_results&target_id={row_id}&field_name=value")
    assert before.status_code == 200
    assert before.json()["conflict_count"] == 1

    response = client.post(
        "/api/workbench/review-conflicts/accept-ai",
        json={
            "paper_id": paper_id,
            "target_type": "dft_result",
            "target_id": row_id,
            "field_name": "value",
            "reviewer": "review_center",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "verify"

    after = client.get(f"/api/workbench/review-conflicts?paper_id={paper_id}&target_type=dft_results&target_id={row_id}&field_name=value")
    assert after.status_code == 200
    assert after.json()["conflict_count"] == 0

    with Session() as session:
        review = session.query(ExtractionFieldReview).filter_by(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=row_id,
            field_name="value",
        ).one()
        assert review.reviewer_status == "verified"


def test_mixed_verified_and_rejected_dft_rows_do_not_make_paper_level_conflict(setup_test_db):
    Session = _session_factory(setup_test_db)
    with Session() as session:
        paper = Paper(title="B0095-like DFT settlement", paper_code="B0095X", pdf_path="b0095-like.pdf")
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Co-N3Cl1")
        session.add(catalyst)
        session.flush()
        ready_a = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            reaction_step="adsorption",
            value=-1.59,
            unit="eV",
            evidence_text="strengthened binding energy of -1.59 eV",
            candidate_status="ML_Ready",
        )
        ready_b = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S6",
            reaction_step="Li2S6 adsorption",
            value=-1.23,
            unit="eV",
            evidence_text="adsorption energy of -1.23 eV",
            candidate_status="ML_Ready",
        )
        rejected = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="CO",
            value=-1.50,
            unit="eV",
            evidence_text="rejected CO row",
            candidate_status="Rejected",
        )
        session.add_all([ready_a, ready_b, rejected])
        session.flush()
        _add_field_review(session, paper_id=paper.id, target_id=ready_a.id, field_name="adsorbate", status="verified", value="Li2S6")
        _add_field_review(session, paper_id=paper.id, target_id=ready_a.id, field_name="catalyst", status="verified", value=str(catalyst.id))
        _add_field_review(session, paper_id=paper.id, target_id=ready_a.id, field_name="reaction_step", status="verified", value="adsorption")
        _add_field_review(session, paper_id=paper.id, target_id=ready_b.id, field_name="value", status="verified", value=-1.23, unit="eV")
        _add_field_review(session, paper_id=paper.id, target_id=rejected.id, field_name="value", status="rejected", value=-1.50, unit="eV")
        _add_field_review(session, paper_id=paper.id, target_id=rejected.id, field_name="adsorbate", status="rejected", value="CO")
        _add_field_review(session, paper_id=paper.id, target_id=rejected.id, field_name="energy_type", status="rejected", value="adsorption_energy")
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=ready_a.id,
            field_name="dft_results",
            decision="PROPOSED",
            corrected_value={
                "value": -1.59,
                "unit": "eV",
                "property_type": "adsorption_energy",
                "adsorbate": "Li2S6",
                "reaction_step": "Li2S6 adsorption",
                "material_identity": "Co-N3Cl1",
            },
            source="ai_old_ready_a",
        )
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=ready_a.id,
            field_name="dft_results",
            decision="ACCEPT",
            corrected_value={
                "value": -1.59,
                "unit": "eV",
                "property_type": "adsorption_energy",
                "adsorbate": "Li2S6",
                "reaction_step": "adsorption",
                "material_identity": "Co-N3Cl1",
            },
            source="ai_current_ready_a",
        )
        _seed_dft_audit(
            session,
            paper_id=paper.id,
            target_id=rejected.id,
            field_name="dft_results",
            decision="REJECT",
            corrected_value={"value": -1.50, "unit": "eV", "property_type": "adsorption_energy", "adsorbate": "CO"},
            source="ai_reject_bad_row",
        )
        session.commit()

        active = ReviewConflictAggregationService(session).list_conflicts(
            paper_id=paper.id,
            target_type="dft_results",
            active_only=True,
        )
        detail = PaperQueryService(session).get_paper_detail(paper.id)

    assert active["conflict_count"] == 0
    assert detail is not None
    assert detail.dft_review_status == "reviewed"
    rows = {item.id: item for item in detail.dft_results_items}
    assert rows[ready_a.id].candidate_status == "ML_Ready"
    assert rows[ready_a.id].conflict_count == 0
    assert rows[ready_b.id].candidate_status == "ML_Ready"
    assert rows[ready_b.id].conflict_count == 0
    assert rows[rejected.id].candidate_status == "Rejected"
    assert rows[rejected.id].conflict_count == 0
