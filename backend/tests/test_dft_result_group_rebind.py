from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    ActiveSiteMetal,
    AuditLog,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
    WorkflowJob,
)
from app.main import app
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.active_site_enrichment_service import ActiveSiteEnrichmentService
from app.services.dft_review_service import DFTResultReviewService


def _create_rebind_case(
    engine,
    *,
    result_count: int = 2,
    duplicate_planned_identity: bool = False,
    add_target_conflict: bool = False,
    target_name: str | None = "Correct catalyst 15",
) -> dict[str, object]:
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        paper = Paper(title="DFT group rebind", library_name="A", pdf_path="rebind.pdf")
        other_paper = Paper(title="Other paper", library_name="A", pdf_path="other.pdf")
        session.add_all([paper, other_paper])
        session.flush()
        source = CatalystSample(paper_id=paper.id, name="Wrong catalyst 30")
        target = CatalystSample(paper_id=paper.id, name=target_name)
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


def _create_duplicate_merge_case(engine) -> dict[str, object]:
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Duplicate catalyst samples", library_name="A", pdf_path="duplicates.pdf")
        session.add(paper)
        session.flush()
        target = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
        )
        source_one = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4@Li2S",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="graphene",
            evidence_strength="source-one-evidence",
        )
        source_two = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4@Li2S4",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="graphene",
            evidence_strength="source-two-evidence",
        )
        empty_source = CatalystSample(
            paper_id=paper.id,
            name="Fe-N4@Li2S8",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            coordination="Fe-N4",
            support="graphene",
        )
        session.add_all([target, source_one, source_two, empty_source])
        session.flush()

        rows: list[DFTResult] = []
        review_snapshots: dict[str, dict[str, object]] = {}
        for index, (source, adsorbate) in enumerate(
            ((source_one, "Li2S"), (source_two, "Li2S4")),
            start=1,
        ):
            row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=source.id,
                adsorbate=adsorbate,
                property_type="adsorption_energy",
                value=-float(index),
                unit="eV",
                candidate_status="ai_verified_ml_ready",
                candidate_identity=f"merge-candidate-{index}",
                evidence_text=f"Evidence text {index}",
                evidence_payload={"material_identity": source.name, "page": index},
            )
            session.add(row)
            session.flush()
            lifecycle = DFTAuditIssueLifecycleService(session)
            identity = lifecycle.build_identity(
                paper_id=paper.id,
                payload=lifecycle.authoritative_payload_for_result(row),
            )
            lifecycle.apply_result_identity(row, identity)
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=row.value,
                reviewed_value=row.value,
                unit="eV",
                evidence_text=f"Review evidence {index}",
                reviewer_status="verified",
                reviewer="human-reviewer",
                reviewer_note=f"review-note-{index}",
                review_payload={"human_verification": {"decision": "verified", "index": index}},
            )
            session.add(review)
            session.add(
                DFTAuditIssue(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    result_id=row.id,
                    issue_type=f"merge-test-{index}",
                    severity="medium",
                    status="open" if index == 1 else "resolved",
                    current_snapshot={"material_identity": source.name},
                    fingerprint=f"merge-test-fingerprint-{index}",
                )
            )
            review_snapshots[str(row.id)] = {
                "original_value": row.value,
                "reviewed_value": row.value,
                "unit": "eV",
                "evidence_text": f"Review evidence {index}",
                "reviewer_status": "verified",
                "reviewer": "human-reviewer",
                "reviewer_note": f"review-note-{index}",
                "review_payload": {"human_verification": {"decision": "verified", "index": index}},
            }
            rows.append(row)

        session.add_all(
            [
                EvidenceSpan(
                    paper_id=paper.id,
                    object_type="catalyst_samples",
                    object_id=str(source_one.id),
                    text="source span",
                    page=1,
                ),
                EvidenceClaim(
                    paper_id=paper.id,
                    claim_text="source claim",
                    source_type="manual",
                    target_type="catalyst_sample",
                    target_id=str(source_two.id),
                    evidence_text="source claim evidence",
                ),
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="text",
                    target_type="CatalystSample",
                    target_id=str(source_two.id),
                    evidence_text="source locator evidence",
                    locator_status="exact",
                    locator_confidence=1.0,
                    parser_source="test",
                ),
            ]
        )
        for sample in (target, source_one, source_two, empty_source):
            ActiveSiteEnrichmentService(session).refresh_sample(sample)
        session.commit()
        return {
            "paper_id": paper.id,
            "target_id": target.id,
            "source_ids": [source_one.id, source_two.id, empty_source.id],
            "source_names": [source_one.name, source_two.name, empty_source.name],
            "result_ids_by_source": {
                source_one.id: [rows[0].id],
                source_two.id: [rows[1].id],
                empty_source.id: [],
            },
            "result_ids": [row.id for row in rows],
            "candidate_identities": {row.id: row.candidate_identity for row in rows},
            "evidence_payloads": {row.id: row.evidence_payload for row in rows},
            "review_snapshots": review_snapshots,
        }


def _duplicate_merge_payload(case: dict[str, object], **overrides) -> dict[str, object]:
    result_ids_by_source = case["result_ids_by_source"]
    payload = {
        "expected_target_name": "Fe-N4",
        "sources": [
            {
                "source_sample_id": str(source_id),
                "expected_current_name": source_name,
                "dft_result_ids": [str(result_id) for result_id in result_ids_by_source[source_id]],
                "expected_dft_result_count": len(result_ids_by_source[source_id]),
            }
            for source_id, source_name in zip(case["source_ids"], case["source_names"], strict=True)
        ],
        "confirm_same_physical_catalyst": True,
        "reason": "Adsorbate labels split one physical Fe-N4 catalyst into duplicate samples.",
        "reviewer": "duplicate-merge-test",
    }
    payload.update(overrides)
    return payload


def _post_duplicate_merge(
    client: TestClient,
    case: dict[str, object],
    payload: dict[str, object] | None = None,
):
    return client.post(
        f"/api/papers/{case['paper_id']}/catalyst-samples/{case['target_id']}/merge-duplicates",
        json=payload or _duplicate_merge_payload(case),
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


def test_rebind_rejects_unnamed_target_without_writes(setup_test_db):
    case = _create_rebind_case(setup_test_db, target_name=None)
    response = _post_rebind(TestClient(app), case)

    assert response.status_code == 400
    assert "non-empty name" in response.text

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == {case["source_id"]}
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0


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


def test_duplicate_merge_preserves_review_truth_transfers_evidence_and_is_idempotent(setup_test_db):
    case = _create_duplicate_merge_case(setup_test_db)
    client = TestClient(app)

    response = _post_duplicate_merge(client, case)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "merged"
    assert payload["target_sample_id"] == str(case["target_id"])
    assert set(payload["merged_source_sample_ids"]) == {str(source_id) for source_id in case["source_ids"]}
    assert set(payload["deleted_source_sample_ids"]) == {str(source_id) for source_id in case["source_ids"]}
    assert set(payload["moved_dft_result_ids"]) == {str(result_id) for result_id in case["result_ids"]}
    assert payload["moved_dft_result_count"] == 2
    assert payload["requires_reverification"] is False
    assert payload["review_state_preserved"] is True
    assert payload["invalidated_review_ids"] == []
    assert payload["reverification_task_ids"] == []
    assert payload["transferred_evidence_reference_count"] == 3

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        target = session.get(CatalystSample, case["target_id"])
        assert target is not None
        assert target.support == "graphene"
        assert set((target.evidence_strength or "").split("\n\n")) == {
            "source-one-evidence",
            "source-two-evidence",
        }
        assert all(session.get(CatalystSample, source_id) is None for source_id in case["source_ids"])
        rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_(case["result_ids"])).order_by(DFTResult.id)
        ).all()
        assert {row.catalyst_sample_id for row in rows} == {case["target_id"]}
        assert {row.candidate_status for row in rows} == {"ai_verified_ml_ready"}
        assert {row.candidate_identity for row in rows} == set(case["candidate_identities"].values())
        assert {row.evidence_payload["material_identity"] for row in rows} == {
            "Fe-N4@Li2S",
            "Fe-N4@Li2S4",
        }
        assert all(row.identity_version == 2 for row in rows)
        assert all(row.identity_payload["subject"]["material_key"] == "fe-n4" for row in rows)
        reviews = session.scalars(
            select(ExtractionFieldReview)
            .where(ExtractionFieldReview.target_id.in_([str(result_id) for result_id in case["result_ids"]]))
            .order_by(ExtractionFieldReview.target_id)
        ).all()
        assert len(reviews) == 2
        for review in reviews:
            expected = case["review_snapshots"][review.target_id]
            assert review.original_value == expected["original_value"]
            assert review.reviewed_value == expected["reviewed_value"]
            assert review.unit == expected["unit"]
            assert review.evidence_text == expected["evidence_text"]
            assert review.reviewer_status == expected["reviewer_status"]
            assert review.reviewer == expected["reviewer"]
            assert review.reviewer_note == expected["reviewer_note"]
            assert review.review_payload == expected["review_payload"]
        issues = session.scalars(select(DFTAuditIssue).order_by(DFTAuditIssue.issue_type)).all()
        assert [(issue.status, issue.current_snapshot["material_identity"]) for issue in issues] == [
            ("open", "Fe-N4@Li2S"),
            ("resolved", "Fe-N4@Li2S4"),
        ]
        assert {
            span.object_id
            for span in session.scalars(select(EvidenceSpan)).all()
        } == {str(case["target_id"])}
        assert {
            claim.target_id
            for claim in session.scalars(select(EvidenceClaim)).all()
        } == {str(case["target_id"])}
        assert {
            locator.target_id
            for locator in session.scalars(select(EvidenceLocator)).all()
        } == {str(case["target_id"])}
        assert {
            row.catalyst_sample_id
            for row in session.scalars(select(ActiveSiteMetal)).all()
        } == {case["target_id"]}
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 1
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "merge_duplicate_catalyst_samples")
        )
        assert audit.payload["evidence_strength_merge"] == {
            "changed": True,
            "distinct_value_count": 2,
        }

    retry = _post_duplicate_merge(client, case)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "already_merged"
    assert retry.json()["audit_log_id"] == payload["audit_log_id"]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 1
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0


def test_duplicate_merge_can_delete_a_source_that_is_already_empty(setup_test_db):
    case = _create_duplicate_merge_case(setup_test_db)
    payload = _duplicate_merge_payload(case)
    payload["sources"] = [payload["sources"][2]]

    response = _post_duplicate_merge(TestClient(app), case, payload)
    assert response.status_code == 200, response.text
    assert response.json()["moved_dft_result_ids"] == []
    assert response.json()["moved_dft_result_count"] == 0
    assert response.json()["deleted_source_sample_ids"] == [str(case["source_ids"][2])]

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        assert session.get(CatalystSample, case["source_ids"][2]) is None
        assert session.get(CatalystSample, case["source_ids"][0]) is not None
        assert session.get(CatalystSample, case["source_ids"][1]) is not None
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == set(case["source_ids"][:2])


@pytest.mark.parametrize("reference_type", ["mechanism", "performance"])
def test_duplicate_merge_blocks_non_dft_references_without_writes(setup_test_db, reference_type):
    case = _create_duplicate_merge_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        if reference_type == "mechanism":
            session.add(
                MechanismClaim(
                    paper_id=case["paper_id"],
                    catalyst_sample_id=case["source_ids"][0],
                    claim_text="Mechanism belongs to the duplicate sample.",
                )
            )
        else:
            session.add(
                ElectrochemicalPerformance(
                    paper_id=case["paper_id"],
                    catalyst_sample_id=case["source_ids"][0],
                    capacity_value=800.0,
                )
            )
        session.commit()

    response = _post_duplicate_merge(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "non_dft_references" in response.text
    with SessionLocal() as session:
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == set(case["source_ids"][:2])
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 0


@pytest.mark.parametrize("reference_type", ["sample_review", "active_correction"])
def test_duplicate_merge_blocks_sample_review_workflow_references_without_writes(
    setup_test_db,
    reference_type,
):
    case = _create_duplicate_merge_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        source_id = case["source_ids"][0]
        if reference_type == "sample_review":
            session.add(
                ExtractionFieldReview(
                    paper_id=case["paper_id"],
                    target_type="catalyst_samples",
                    target_id=str(source_id),
                    field_name="name",
                    reviewer_status="verified",
                )
            )
        else:
            session.add(
                PaperCorrection(
                    paper_id=case["paper_id"],
                    source="duplicate-merge-test",
                    field_name="name",
                    target_path=f"catalyst_samples:{source_id}:name",
                    proposed_value="Fe-N4",
                    reason="Pending sample correction must not be orphaned.",
                    status="pending",
                )
            )
        session.commit()

    response = _post_duplicate_merge(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "non_dft_references" in response.text
    expected_count = (
        "sample_reviews=1"
        if reference_type == "sample_review"
        else "active_corrections=1"
    )
    assert expected_count in response.text
    with SessionLocal() as session:
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == set(case["source_ids"][:2])


def test_duplicate_merge_blocks_optional_project_library_physical_references(setup_test_db):
    case = _create_duplicate_merge_case(setup_test_db)
    with setup_test_db.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE project_library_ambiguous_records ("
                "id UUID PRIMARY KEY, "
                "catalyst_sample_id UUID REFERENCES catalyst_samples(id) ON DELETE SET NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO project_library_ambiguous_records (id, catalyst_sample_id) "
                "VALUES (:id, :catalyst_sample_id)"
            ),
            {
                "id": uuid4(),
                "catalyst_sample_id": case["source_ids"][0],
            },
        )

    response = _post_duplicate_merge(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "project_library_ambiguous_records" in response.text
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        assert session.scalar(
            text("SELECT COUNT(*) FROM project_library_ambiguous_records")
        ) == 1


def test_duplicate_merge_blocks_incompatible_basic_info_without_writes(setup_test_db):
    case = _create_duplicate_merge_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        source = session.get(CatalystSample, case["source_ids"][1])
        source.support = "carbon nanotube"
        session.commit()

    response = _post_duplicate_merge(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "basic_info_conflict" in response.text
    with SessionLocal() as session:
        target = session.get(CatalystSample, case["target_id"])
        assert target.support is None
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 0


@pytest.mark.parametrize("conflict_kind", ["batch", "database"])
def test_duplicate_merge_identity_conflict_returns_409_without_writes(setup_test_db, conflict_kind):
    case = _create_duplicate_merge_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_(case["result_ids"])).order_by(DFTResult.id)
        ).all()
        if conflict_kind == "batch":
            rows[1].adsorbate = rows[0].adsorbate
            rows[1].value = rows[0].value
            session.add(rows[1])
        else:
            target = session.get(CatalystSample, case["target_id"])
            conflict = DFTResult(
                paper_id=case["paper_id"],
                catalyst_sample_id=case["target_id"],
                adsorbate=rows[0].adsorbate,
                property_type=rows[0].property_type,
                value=rows[0].value,
                unit=rows[0].unit,
                candidate_identity="duplicate-merge-existing-target-conflict",
            )
            session.add(conflict)
            session.flush()
            lifecycle = DFTAuditIssueLifecycleService(session)
            identity = lifecycle.build_identity(
                paper_id=case["paper_id"],
                payload=lifecycle.authoritative_payload_for_result(conflict, catalyst_sample=target),
            )
            lifecycle.apply_result_identity(conflict, identity)
        session.commit()

    response = _post_duplicate_merge(TestClient(app), case)
    assert response.status_code == 409, response.text
    assert "observation_key_conflict" in response.text
    with SessionLocal() as session:
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == set(case["source_ids"][:2])
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 0


@pytest.mark.parametrize(
    ("mutate_payload", "expected_detail"),
    [
        (lambda payload: payload.update(expected_target_name="stale target"), "target_name_stale"),
        (
            lambda payload: payload["sources"][0].update(expected_current_name="stale source"),
            "source_name_stale",
        ),
        (
            lambda payload: payload["sources"][0].update(expected_dft_result_count=2),
            "result_count_stale",
        ),
        (
            lambda payload: payload["sources"][0].update(dft_result_ids=[]),
            "incomplete_result_set",
        ),
    ],
)
def test_duplicate_merge_rejects_stale_or_incomplete_source_snapshot(
    setup_test_db,
    mutate_payload,
    expected_detail,
):
    case = _create_duplicate_merge_case(setup_test_db)
    payload = _duplicate_merge_payload(case)
    mutate_payload(payload)
    response = _post_duplicate_merge(TestClient(app), case, payload)
    assert response.status_code == 409, response.text
    assert expected_detail in response.text


def test_duplicate_merge_rolls_back_when_second_identity_write_fails(setup_test_db, monkeypatch):
    case = _create_duplicate_merge_case(setup_test_db)
    original_apply = DFTAuditIssueLifecycleService.apply_result_identity
    calls = {"count": 0}

    def fail_second_identity_write(row, identity):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected duplicate-merge identity failure")
        original_apply(row, identity)

    monkeypatch.setattr(
        DFTAuditIssueLifecycleService,
        "apply_result_identity",
        staticmethod(fail_second_identity_write),
    )
    response = _post_duplicate_merge(TestClient(app, raise_server_exceptions=False), case)
    assert response.status_code == 500

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        assert all(session.get(CatalystSample, source_id) is not None for source_id in case["source_ids"])
        target = session.get(CatalystSample, case["target_id"])
        assert target.support is None
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert {row.catalyst_sample_id for row in rows} == set(case["source_ids"][:2])
        assert {row.candidate_status for row in rows} == {"ai_verified_ml_ready"}
        assert {row.candidate_identity for row in rows} == set(case["candidate_identities"].values())
        assert {review.reviewer_status for review in session.scalars(select(ExtractionFieldReview)).all()} == {
            "verified"
        }
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "merge_duplicate_catalyst_samples")
        ) == 0
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0
