from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    ExtractionFieldReview,
    Paper,
    PaperCorrection,
    WorkflowJob,
)
from app.main import app
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_review_service import DFTResultReviewService


def _create_rebind_case(
    engine,
    *,
    result_count: int = 2,
    duplicate_planned_identity: bool = False,
    add_target_conflict: bool = False,
) -> dict[str, object]:
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        paper = Paper(title="DFT group rebind", library_name="A", pdf_path="rebind.pdf")
        other_paper = Paper(title="Other paper", library_name="A", pdf_path="other.pdf")
        session.add_all([paper, other_paper])
        session.flush()
        source = CatalystSample(paper_id=paper.id, name="Wrong catalyst 30")
        target = CatalystSample(paper_id=paper.id, name="Correct catalyst 15")
        other_sample = CatalystSample(paper_id=other_paper.id, name="Cross-paper catalyst")
        session.add_all([source, target, other_sample])
        session.flush()

        rows: list[DFTResult] = []
        for index in range(result_count):
            row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=source.id,
                adsorbate="Li2S",
                property_type="adsorption_energy",
                value=-1.0 if duplicate_planned_identity else -1.0 - index,
                unit="eV",
                candidate_status="ai_verified_ml_ready",
                candidate_identity=f"legacy-candidate-{index}",
                evidence_text=f"Original evidence row {index}",
                evidence_payload={"material_identity": "Wrong catalyst 30", "page": index + 1},
            )
            session.add(row)
            session.flush()
            if not duplicate_planned_identity:
                lifecycle = DFTAuditIssueLifecycleService(session)
                identity = lifecycle.build_identity(
                    paper_id=paper.id,
                    payload=lifecycle.authoritative_payload_for_result(row),
                )
                lifecycle.apply_result_identity(row, identity)
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    original_value=row.value,
                    reviewed_value=row.value,
                    reviewer_status="verified",
                    reviewer="human",
                )
            )
            rows.append(row)

        conflict_row = None
        if add_target_conflict:
            conflict_row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=target.id,
                adsorbate=rows[0].adsorbate,
                property_type=rows[0].property_type,
                value=rows[0].value,
                unit=rows[0].unit,
                candidate_identity="existing-target-conflict",
                evidence_payload={"material_identity": target.name},
            )
            session.add(conflict_row)
            session.flush()
            lifecycle = DFTAuditIssueLifecycleService(session)
            identity = lifecycle.build_identity(
                paper_id=paper.id,
                payload=lifecycle.authoritative_payload_for_result(conflict_row),
            )
            lifecycle.apply_result_identity(conflict_row, identity)

        session.commit()
        return {
            "paper_id": paper.id,
            "other_paper_id": other_paper.id,
            "source_id": source.id,
            "target_id": target.id,
            "other_sample_id": other_sample.id,
            "result_ids": [row.id for row in rows],
            "candidate_identities": {row.id: row.candidate_identity for row in rows},
            "old_subject_keys": {row.id: row.subject_key for row in rows},
            "conflict_row_id": conflict_row.id if conflict_row else None,
        }


def _payload(case: dict[str, object], **overrides) -> dict[str, object]:
    payload = {
        "target_sample_id": str(case["target_id"]),
        "dft_result_ids": [str(result_id) for result_id in case["result_ids"]],
        "expected_result_count": len(case["result_ids"]),
        "confirm_rebind": True,
        "reason": "The source catalyst name was assigned incorrectly.",
        "reviewer": "group-rebind-test",
    }
    payload.update(overrides)
    return payload


def _post_rebind(client: TestClient, case: dict[str, object], payload: dict[str, object] | None = None):
    return client.post(
        f"/api/papers/{case['paper_id']}/catalyst-samples/{case['source_id']}/rebind-dft-results",
        json=payload or _payload(case),
    )


def test_rebinds_complete_group_refreshes_identity_and_is_idempotent(setup_test_db):
    case = _create_rebind_case(setup_test_db)
    client = TestClient(app)

    response = _post_rebind(client, case)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "rebound"
    assert payload["source_sample_id"] == str(case["source_id"])
    assert payload["target_sample_id"] == str(case["target_id"])
    assert set(payload["rebound_result_ids"]) == {str(value) for value in case["result_ids"]}
    assert payload["rebound_result_count"] == 2
    assert payload["requires_reverification"] is True
    assert payload["remaining_dft_result_count"] == 0
    assert len(payload["result_audit_log_ids"]) == 2
    assert len(payload["correction_ids"]) == 2
    assert len(payload["invalidated_review_ids"]) == 2
    assert len(payload["reverification_task_ids"]) == 2

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_(case["result_ids"])).order_by(DFTResult.id)
        ).all()
        assert {row.catalyst_sample_id for row in rows} == {case["target_id"]}
        assert {row.candidate_status for row in rows} == {"system_candidate"}
        assert all(row.identity_version == 2 for row in rows)
        assert all(row.identity_payload["subject"]["material_key"] == "correct catalyst 15" for row in rows)
        assert all(row.subject_key != case["old_subject_keys"][row.id] for row in rows)
        assert {row.candidate_identity for row in rows} == set(case["candidate_identities"].values())
        assert {row.evidence_text for row in rows} == {"Original evidence row 0", "Original evidence row 1"}
        assert {row.evidence_payload["material_identity"] for row in rows} == {"Wrong catalyst 30"}
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id.in_([str(value) for value in case["result_ids"]])
            )
        ).all()
        assert {review.reviewer_status for review in reviews} == {"pending"}
        side_effect_counts = {
            "group_audits": session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "rebind_dft_result_group")
            ),
            "manual_audits": session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "manual_update_dft_result")
            ),
            "corrections": session.scalar(select(func.count()).select_from(PaperCorrection)),
            "jobs": session.scalar(select(func.count()).select_from(WorkflowJob)),
        }

    retry = _post_rebind(client, case)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "already_rebound"
    assert retry.json()["audit_log_id"] == payload["audit_log_id"]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "rebind_dft_result_group")
        ) == side_effect_counts["group_audits"]
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "manual_update_dft_result")
        ) == side_effect_counts["manual_audits"]
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == side_effect_counts["corrections"]
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == side_effect_counts["jobs"]


def test_rebind_rolls_back_everything_when_one_row_update_fails(setup_test_db, monkeypatch):
    case = _create_rebind_case(setup_test_db)
    original = DFTResultReviewService.manually_update_result
    calls = {"count": 0}

    def fail_second_group_update(self, **kwargs):
        if kwargs.get("commit") is False:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected second-row failure")
        return original(self, **kwargs)

    monkeypatch.setattr(DFTResultReviewService, "manually_update_result", fail_second_group_update)
    client = TestClient(app, raise_server_exceptions=False)
    response = _post_rebind(client, case)
    assert response.status_code == 500

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == {case["source_id"]}
        assert {row.candidate_status for row in rows} == {"ai_verified_ml_ready"}
        assert {row.candidate_identity for row in rows} == set(case["candidate_identities"].values())
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0
        reviews = session.scalars(select(ExtractionFieldReview)).all()
        assert {review.reviewer_status for review in reviews} == {"verified"}


@pytest.mark.parametrize(
    ("payload_override", "expected_detail"),
    [
        ({"expected_result_count": 1}, "result count is stale"),
        ({"dft_result_ids": []}, "at least 1 item"),
    ],
)
def test_rebind_rejects_stale_count_and_empty_ids(setup_test_db, payload_override, expected_detail):
    case = _create_rebind_case(setup_test_db)
    response = _post_rebind(TestClient(app), case, _payload(case, **payload_override))
    assert response.status_code in {400, 422}
    assert expected_detail.lower() in response.text.lower()


def test_rebind_rejects_same_cross_paper_incomplete_and_mixed_sets(setup_test_db):
    client = TestClient(app)

    same_case = _create_rebind_case(setup_test_db)
    same = _post_rebind(
        client,
        same_case,
        _payload(same_case, target_sample_id=str(same_case["source_id"])),
    )
    assert same.status_code == 400
    assert "must be different" in same.text

    cross_case = _create_rebind_case(setup_test_db)
    cross = _post_rebind(
        client,
        cross_case,
        _payload(cross_case, target_sample_id=str(cross_case["other_sample_id"])),
    )
    assert cross.status_code == 400
    assert "same paper" in cross.text

    incomplete_case = _create_rebind_case(setup_test_db)
    incomplete = _post_rebind(
        client,
        incomplete_case,
        _payload(incomplete_case, dft_result_ids=[str(incomplete_case["result_ids"][0])]),
    )
    assert incomplete.status_code == 400
    assert "exactly cover" in incomplete.text

    mixed_case = _create_rebind_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        target_row = DFTResult(
            paper_id=mixed_case["paper_id"],
            catalyst_sample_id=mixed_case["target_id"],
            property_type="band_gap",
            value=1.2,
            unit="eV",
        )
        session.add(target_row)
        session.commit()
        target_row_id = target_row.id
    mixed_ids = [str(mixed_case["result_ids"][0]), str(target_row_id)]
    mixed = _post_rebind(client, mixed_case, _payload(mixed_case, dft_result_ids=mixed_ids))
    assert mixed.status_code == 400
    assert "unexpected=" in mixed.text


@pytest.mark.parametrize("conflict_kind", ["batch", "database"])
def test_rebind_identity_conflict_returns_409_without_writes(setup_test_db, conflict_kind):
    case = _create_rebind_case(
        setup_test_db,
        duplicate_planned_identity=conflict_kind == "batch",
        add_target_conflict=conflict_kind == "database",
    )
    response = _post_rebind(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "observation_key_conflict" in response.text

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == {case["source_id"]}
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "rebind_dft_result_group")
        ) == 0
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0


def test_single_patch_recomputes_identity_v2_when_catalyst_sample_changes(setup_test_db):
    case = _create_rebind_case(setup_test_db, result_count=1)
    result_id = case["result_ids"][0]
    response = TestClient(app).patch(
        f"/api/papers/{case['paper_id']}/dft-results/{result_id}",
        json={
            "confirm_manual_update": True,
            "updates": {"catalyst_sample_id": str(case["target_id"])},
            "reason": "Correct the catalyst sample binding.",
            "reviewer": "single-patch-test",
        },
    )
    assert response.status_code == 200, response.text

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        row = session.get(DFTResult, UUID(str(result_id)))
        assert row.catalyst_sample_id == case["target_id"]
        assert row.identity_version == 2
        assert row.subject_key != case["old_subject_keys"][result_id]
        assert row.identity_payload["subject"]["material_key"] == "correct catalyst 15"
        assert row.candidate_identity == case["candidate_identities"][result_id]
