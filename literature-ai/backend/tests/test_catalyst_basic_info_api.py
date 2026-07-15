from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ActiveSiteMetal,
    AuditLog,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    ExtractionFieldReview,
    Paper,
    WorkflowJob,
)
from app.main import app
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.utils.review_safety import is_export_eligible_extraction


def test_update_catalyst_basic_info_standardizes_fields_and_audits(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(
            title="Catalyst basic info paper",
            library_name="锂硫双原子",
            pdf_path="basic-info.pdf",
            workflow_status="Initial_Parsed",
        )
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Co-GeC",
            catalyst_type="DAC",
            metal_centers=["co", "Co", "GeC"],
            coordination=None,
            support="Gr",
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={
            "name": "Co-GeC",
            "catalyst_type": "DAC",
            "metal_centers": ["co", "ge"],
            "coordination": "Co-Ge bridge",
            "support": "Gr",
            "source": "ai_auto_basic_info",
            "note": "AI filled from structured DFT grouping.",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    updated = payload["catalyst_sample"]
    assert updated["catalyst_type"] == "dual_atom"
    assert updated["metal_centers"] == ["Co", "Ge"]
    assert updated["support"] == "graphene"
    assert updated["support_normalized"] == "graphene"
    assert updated["metal_1_descriptors"]["element_symbol"] == "Co"
    assert updated["metal_1_descriptors"]["electronegativity"] == 1.88
    assert payload["active_site_refresh"]["active_site_status"] == "refreshed"
    assert payload["active_site_refresh"]["inserted_count"] == 2

    detail = client.get(f"/api/papers/{paper_id}", params={"mode": "full"})
    assert detail.status_code == 200, detail.text
    sample_payload = detail.json()["catalyst_samples_items"][0]
    assert sample_payload["support"] == "graphene"
    assert sample_payload["support_normalized"] == "graphene"
    assert sample_payload["metal_centers"] == ["Co", "Ge"]
    assert sample_payload["metal_1_descriptors"]["element_symbol"] == "Co"
    assert sample_payload["descriptor_blockers"] == []

    with SessionLocal() as session:
        stored = session.get(CatalystSample, UUID(sample_id))
        assert stored is not None
        assert stored.catalyst_type == "dual_atom"
        assert stored.support == "graphene"
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.paper_id == UUID(paper_id),
                AuditLog.action == "update_catalyst_basic_info",
                AuditLog.target_id == sample_id,
            )
        )
        assert audit is not None
        assert audit.payload["normalization"]["raw"]["support"] == "Gr"
        assert audit.payload["after"]["support"] == "graphene"
        assert audit.payload["active_site_refresh"]["active_site_status"] == "refreshed"
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == UUID(sample_id)).order_by(ActiveSiteMetal.site_role)
        ).all()
        assert [row.site_role for row in active_site_rows] == ["M1", "M2"]
        assert [row.element_symbol for row in active_site_rows] == ["Co", "Ge"]
        assert {row.enrichment_status for row in active_site_rows} == {"system_enriched"}


def test_multi_metal_screening_set_does_not_generate_fake_dual_atom_descriptors(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Screening set", library_name="锂硫双原子", pdf_path="screening.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="M-BP screening collection",
            catalyst_type="dual_atom",
            metal_centers=["Co", "Fe", "Ni", "V"],
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)

    client = TestClient(app)
    detail = client.get(f"/api/papers/{paper_id}", params={"mode": "full"})
    assert detail.status_code == 200, detail.text
    sample_payload = detail.json()["catalyst_samples_items"][0]
    assert sample_payload["metal_1_descriptors"] is None
    assert sample_payload["metal_2_descriptors"] is None
    assert sample_payload["dac_combined_descriptors"] is None
    assert "screening_set_not_active_site" in sample_payload["descriptor_blockers"]
    assert "too_many_metal_centers_for_descriptor" in sample_payload["descriptor_blockers"]


def test_update_catalyst_basic_info_clears_stale_active_site_rows_for_screening_set(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Stale site cleanup", library_name="锂硫双原子", pdf_path="cleanup.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Fe-Co DAC",
            catalyst_type="dual_atom",
            metal_centers=["Fe", "Co"],
        )
        session.add(sample)
        session.flush()
        session.add_all(
            [
                ActiveSiteMetal(
                    paper_id=paper.id,
                    catalyst_sample_id=sample.id,
                    active_site_key=f"catalyst:{sample.id}|site:confirmed_active_center",
                    site_type="dual_atom",
                    site_role="M1",
                    element_symbol="Fe",
                    element_order=1,
                    order_source="test",
                    normalized_pair_key="Fe-Co",
                    enrichment_status="system_enriched",
                ),
                ActiveSiteMetal(
                    paper_id=paper.id,
                    catalyst_sample_id=sample.id,
                    active_site_key=f"catalyst:{sample.id}|site:confirmed_active_center",
                    site_type="dual_atom",
                    site_role="M2",
                    element_symbol="Co",
                    element_order=2,
                    order_source="test",
                    normalized_pair_key="Fe-Co",
                    enrichment_status="system_enriched",
                ),
            ]
        )
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={
            "catalyst_type": "dual_atom",
            "metal_centers": ["Fe", "Co", "Ni"],
            "source": "literature_library_user",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["active_site_refresh"]["active_site_status"] == "skipped"
    assert payload["active_site_refresh"]["deleted_count"] == 2
    assert payload["active_site_refresh"]["skipped_reason"] == "screening_set_not_active_site"

    with SessionLocal() as session:
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == UUID(sample_id))
        ).all()
        assert active_site_rows == []


def test_update_catalyst_basic_info_rejects_wrong_paper(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Paper A", library_name="A", pdf_path="a.pdf")
        other = Paper(title="Paper B", library_name="B", pdf_path="b.pdf")
        session.add_all([paper, other])
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Fe-N-C")
        session.add(sample)
        session.commit()
        other_id = str(other.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{other_id}/catalyst-samples/{sample_id}/basic-info",
        json={"support": "graphene"},
    )
    assert response.status_code == 404


def test_update_catalyst_basic_info_partial_payload_only_changes_provided_fields(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Partial catalyst update", library_name="A", pdf_path="partial.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Co-GeC",
            catalyst_type="DAC",
            metal_centers=["Co", "Ge"],
            support="graphene substrate",
        )
        session.add(sample)
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={"coordination": "Co-Ge bridge", "source": "ai_patch"},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        stored = session.get(CatalystSample, UUID(sample_id))
        assert stored is not None
        assert stored.coordination == "Co-Ge bridge"
        assert stored.support == "graphene substrate"
        assert stored.catalyst_type == "DAC"


def test_create_catalyst_basic_info_from_dft_group_and_bind_rows(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Unbound DFT group", library_name="A", pdf_path="unbound.pdf")
        session.add(paper)
        session.flush()
        rows = [
            DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.2, unit="eV"),
            DFTResult(paper_id=paper.id, property_type="barrier", value=0.4, unit="eV"),
        ]
        session.add_all(rows)
        session.commit()
        paper_id = str(paper.id)
        row_ids = [str(row.id) for row in rows]

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/from-dft-group",
        json={
            "dft_result_ids": row_ids,
            "name": "Co-GeC",
            "catalyst_type": "DAC",
            "metal_centers": ["Co", "Ge"],
            "support": "Gr",
            "source": "literature_library_frontend",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "created_and_bound"
    assert set(payload["bound_dft_result_ids"]) == set(row_ids)
    assert payload["active_site_refresh"]["active_site_status"] == "refreshed"
    assert payload["active_site_refresh"]["inserted_count"] == 2

    with SessionLocal() as session:
        sample = session.get(CatalystSample, UUID(payload["catalyst_sample_id"]))
        assert sample is not None
        assert sample.name == "Co-GeC"
        assert sample.catalyst_type == "dual_atom"
        assert sample.support == "graphene"
        stored_rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_([UUID(row_id) for row_id in row_ids]))
        ).all()
        assert {row.catalyst_sample_id for row in stored_rows} == {sample.id}
        active_site_rows = session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == sample.id).order_by(ActiveSiteMetal.site_role)
        ).all()
        assert [row.element_symbol for row in active_site_rows] == ["Co", "Ge"]
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.paper_id == UUID(paper_id),
                AuditLog.action == "create_or_bind_catalyst_sample",
            )
        )
        assert audit is not None
        assert audit.payload["created"] is True
        assert set(audit.payload["bound_dft_result_ids"]) == set(row_ids)


def test_create_catalyst_basic_info_reuses_unique_exact_name(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Reuse sample", library_name="A", pdf_path="reuse.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Co-GeC", support="GeC")
        row = DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.2, unit="eV")
        session.add_all([sample, row])
        session.commit()
        paper_id = str(paper.id)
        sample_id = str(sample.id)
        row_id = str(row.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{paper_id}/catalyst-samples/from-dft-group",
        json={
            "dft_result_ids": [row_id],
            "name": " co-gec ",
            "catalyst_type": "dual_atom",
            "metal_centers": ["Co", "Ge"],
            "support": "GeC",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "bound_existing"
    assert payload["catalyst_sample_id"] == sample_id
    with SessionLocal() as session:
        assert session.query(CatalystSample).filter(CatalystSample.paper_id == UUID(paper_id)).count() == 1
        stored = session.get(DFTResult, UUID(row_id))
        assert stored is not None
        assert str(stored.catalyst_sample_id) == sample_id


def _create_catalyst_name_change_case(
    engine,
    *,
    duplicate_planned_identity: bool = False,
    add_database_conflict: bool = False,
    add_duplicate_sample: bool = False,
) -> dict[str, object]:
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        paper = Paper(title="Catalyst name Identity v2 rekey", library_name="A", pdf_path="rename.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(
            paper_id=paper.id,
            name="Wrong catalyst name",
            catalyst_type="single_atom",
            metal_centers=["Fe"],
            support="graphene",
        )
        session.add(sample)
        if add_duplicate_sample:
            session.add(CatalystSample(paper_id=paper.id, name=" correct catalyst name "))
        session.flush()

        rows = []
        reviews = []
        issues = []
        for index in range(2):
            row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=sample.id,
                adsorbate="Li2S",
                property_type="adsorption_energy",
                value=-1.0 if duplicate_planned_identity else -1.0 - index,
                unit="eV",
                candidate_status="ai_verified_ml_ready" if index == 0 else "ML_Ready",
                candidate_identity=f"candidate-identity-{index}",
                evidence_text=f"Original evidence text {index}",
                evidence_payload={
                    "material_identity": "Wrong catalyst name",
                    "active_site_instance_key": "same-site" if duplicate_planned_identity else f"site-{index}",
                    "page": index + 1,
                    "quoted_text": f"Original evidence quote {index}",
                },
            )
            session.add(row)
            session.flush()
            if not duplicate_planned_identity:
                lifecycle = DFTAuditIssueLifecycleService(session)
                lifecycle.apply_result_identity(
                    row,
                    lifecycle.build_identity(
                        paper_id=paper.id,
                        payload=lifecycle.authoritative_payload_for_result(row),
                    ),
                )
            review = ExtractionFieldReview(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                original_value=row.value,
                reviewed_value=row.value,
                reviewer_status="verified",
                reviewer=f"human-{index}",
                reviewer_note=f"Original reviewer note {index}",
                review_payload={
                    "human_verification": {"decision": "verified", "marker": index},
                    "preserved_payload": f"review-{index}",
                },
            )
            session.add(review)
            reviews.append(review)
            issue = DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                result_id=row.id,
                issue_type="wrong_material",
                status="closed" if index == 0 else "needs_primary_ai",
                current_snapshot=DFTAuditIssueLifecycleService.snapshot_dft_result(row),
                fingerprint=f"catalyst-name-issue-{index}",
            )
            session.add(issue)
            issues.append(issue)
            rows.append(row)

        conflict_row = None
        if add_database_conflict:
            conflict_row = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=None,
                adsorbate=rows[0].adsorbate,
                property_type=rows[0].property_type,
                value=rows[0].value,
                unit=rows[0].unit,
                candidate_identity="existing-target-identity",
                evidence_payload={
                    "material_identity": "Correct catalyst name",
                    "active_site_instance_key": rows[0].evidence_payload["active_site_instance_key"],
                },
            )
            session.add(conflict_row)
            session.flush()
            lifecycle = DFTAuditIssueLifecycleService(session)
            lifecycle.apply_result_identity(
                conflict_row,
                lifecycle.build_identity(
                    paper_id=paper.id,
                    payload=lifecycle.authoritative_payload_for_result(conflict_row),
                ),
            )

        session.commit()
        return {
            "paper_id": paper.id,
            "sample_id": sample.id,
            "result_ids": [row.id for row in rows],
            "candidate_identities": {row.id: row.candidate_identity for row in rows},
            "candidate_statuses": {row.id: row.candidate_status for row in rows},
            "evidence_payloads": {row.id: dict(row.evidence_payload) for row in rows},
            "evidence_texts": {row.id: row.evidence_text for row in rows},
            "review_states": {
                UUID(review.target_id): {
                    "reviewer_status": review.reviewer_status,
                    "reviewed_value": review.reviewed_value,
                    "reviewer": review.reviewer,
                    "reviewer_note": review.reviewer_note,
                    "review_payload": dict(review.review_payload),
                }
                for review in reviews
            },
            "issue_states": {
                issue.id: {
                    "status": issue.status,
                    "current_snapshot": dict(issue.current_snapshot),
                    "result_id": issue.result_id,
                    "target_id": issue.target_id,
                }
                for issue in issues
            },
            "old_identities": {
                row.id: (row.identity_version, row.subject_key, row.observation_key, row.identity_payload)
                for row in rows
            },
            "conflict_row_id": conflict_row.id if conflict_row else None,
        }


def _catalyst_name_change_payload(case: dict[str, object], **overrides) -> dict[str, object]:
    payload = {
        "name": "Correct catalyst name",
        "support": "graphene",
        "confirm_name_change_with_dft": True,
        "name_change_reason": "The stored catalyst sample name was transcribed incorrectly.",
        "expected_current_name": "Wrong catalyst name",
        "affected_dft_result_ids": [str(result_id) for result_id in case["result_ids"]],
        "expected_dft_result_count": len(case["result_ids"]),
        "reviewer": "catalyst-name-test",
    }
    payload.update(overrides)
    return payload


def _post_catalyst_name_change(client: TestClient, case: dict[str, object], payload=None):
    return client.post(
        f"/api/papers/{case['paper_id']}/catalyst-samples/{case['sample_id']}/basic-info",
        json=payload or _catalyst_name_change_payload(case),
    )


def test_catalyst_name_change_rekeys_all_dft_rows_preserves_reviews_and_is_idempotent(setup_test_db):
    case = _create_catalyst_name_change_case(setup_test_db)
    client = TestClient(app)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        exportable_row = session.get(DFTResult, case["result_ids"][0])
        assert is_export_eligible_extraction(
            session,
            exportable_row,
            target_type="dft_results",
        ).eligible is True

    response = _post_catalyst_name_change(client, case)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["catalyst_sample"]["name"] == "Correct catalyst name"
    assert payload["name_change"]["status"] == "renamed"
    assert set(payload["name_change"]["affected_dft_result_ids"]) == {
        str(result_id) for result_id in case["result_ids"]
    }
    assert payload["name_change"]["affected_dft_result_count"] == 2
    assert payload["name_change"]["requires_reverification"] is False
    assert payload["name_change"]["review_state_preserved"] is True
    assert payload["name_change"]["invalidated_review_ids"] == []
    assert payload["name_change"]["reverification_task_ids"] == []

    with SessionLocal() as session:
        sample = session.get(CatalystSample, case["sample_id"])
        rows = session.scalars(
            select(DFTResult).where(DFTResult.id.in_(case["result_ids"])).order_by(DFTResult.id)
        ).all()
        assert sample.name == "Correct catalyst name"
        lifecycle = DFTAuditIssueLifecycleService(session)
        for row in rows:
            expected_identity = lifecycle.build_identity(
                paper_id=case["paper_id"],
                payload=lifecycle.authoritative_payload_for_result(row, catalyst_sample=sample),
            )
            assert row.identity_version == 2 == expected_identity.identity_version
            assert row.subject_key == expected_identity.subject_key
            assert row.observation_key == expected_identity.observation_key
            assert row.identity_payload == expected_identity.identity_payload
            assert row.identity_payload["subject"]["material_key"] == "correct catalyst name"
            assert row.candidate_identity == case["candidate_identities"][row.id]
            assert row.evidence_payload == case["evidence_payloads"][row.id]
            assert row.evidence_text == case["evidence_texts"][row.id]
            assert row.candidate_status == case["candidate_statuses"][row.id]
        reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id.in_([str(result_id) for result_id in case["result_ids"]])
            )
        ).all()
        for review in reviews:
            expected = case["review_states"][UUID(review.target_id)]
            assert review.reviewer_status == expected["reviewer_status"]
            assert review.reviewed_value == expected["reviewed_value"]
            assert review.reviewer == expected["reviewer"]
            assert review.reviewer_note == expected["reviewer_note"]
            assert review.review_payload == expected["review_payload"]
        for issue in session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.id.in_(case["issue_states"]))).all():
            expected = case["issue_states"][issue.id]
            assert issue.status == expected["status"]
            assert issue.current_snapshot == expected["current_snapshot"]
            assert issue.result_id == expected["result_id"]
            assert issue.target_id == expected["target_id"]
        exportable_row = session.get(DFTResult, case["result_ids"][0])
        assert is_export_eligible_extraction(
            session,
            exportable_row,
            target_type="dft_results",
        ).eligible is True
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "update_catalyst_basic_info",
                AuditLog.target_id == str(case["sample_id"]),
            )
        )
        rekey = audit.payload["name_identity_rekey"]
        assert rekey["previous_name"] == "Wrong catalyst name"
        assert rekey["new_name"] == "Correct catalyst name"
        assert len(rekey["identity_changes"]) == 2
        assert all(change["old_subject_key"] != change["new_subject_key"] for change in rekey["identity_changes"])
        assert rekey["request_fingerprint"]
        assert rekey["requires_reverification"] is False
        assert rekey["review_state_preserved"] is True
        assert rekey["invalidated_review_ids"] == []
        assert rekey["reverification_task_ids"] == []
        side_effect_counts = {
            "audits": session.scalar(select(func.count()).select_from(AuditLog)),
            "jobs": session.scalar(select(func.count()).select_from(WorkflowJob)),
        }

    retry = _post_catalyst_name_change(client, case)
    assert retry.status_code == 200, retry.text
    assert retry.json()["name_change"]["status"] == "already_renamed"
    assert retry.json()["name_change"]["requires_reverification"] is False
    assert retry.json()["name_change"]["review_state_preserved"] is True
    assert retry.json()["name_change"]["invalidated_review_ids"] == []
    assert retry.json()["name_change"]["reverification_task_ids"] == []
    assert retry.json()["name_change"]["audit_log_id"] == payload["name_change"]["audit_log_id"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog)) == side_effect_counts["audits"]
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == side_effect_counts["jobs"]


@pytest.mark.parametrize(
    ("drop_key", "override", "expected_status", "expected_text"),
    [
        (None, {"confirm_name_change_with_dft": False}, 400, "confirmation"),
        (None, {"name_change_reason": "   "}, 400, "reason"),
        ("affected_dft_result_ids", {}, 400, "affected_dft_result_ids"),
        ("expected_dft_result_count", {}, 400, "expected_dft_result_count"),
        (None, {"expected_dft_result_count": 1}, 409, "count_stale"),
    ],
)
def test_catalyst_name_change_rejects_incomplete_guards_without_writes(
    setup_test_db,
    drop_key,
    override,
    expected_status,
    expected_text,
):
    case = _create_catalyst_name_change_case(setup_test_db)
    request_payload = _catalyst_name_change_payload(case, **override)
    if drop_key:
        request_payload.pop(drop_key)
    response = _post_catalyst_name_change(TestClient(app), case, request_payload)
    assert response.status_code == expected_status, response.text
    assert expected_text.lower() in response.text.lower()
    _assert_catalyst_name_change_rolled_back(setup_test_db, case)


def _assert_catalyst_name_change_rolled_back(engine, case: dict[str, object]) -> None:
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        sample = session.get(CatalystSample, case["sample_id"])
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        assert sample.name == "Wrong catalyst name"
        for row in rows:
            assert (row.identity_version, row.subject_key, row.observation_key, row.identity_payload) == case[
                "old_identities"
            ][row.id]
            assert row.candidate_status == case["candidate_statuses"][row.id]
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 0
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 0
        for review in session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id.in_([str(result_id) for result_id in case["result_ids"]])
            )
        ).all():
            expected = case["review_states"][UUID(review.target_id)]
            assert review.reviewer_status == expected["reviewer_status"]
            assert review.reviewed_value == expected["reviewed_value"]
            assert review.reviewer == expected["reviewer"]
            assert review.reviewer_note == expected["reviewer_note"]
            assert review.review_payload == expected["review_payload"]
        for issue in session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.id.in_(case["issue_states"]))).all():
            expected = case["issue_states"][issue.id]
            assert issue.status == expected["status"]
            assert issue.current_snapshot == expected["current_snapshot"]


def test_catalyst_name_change_rejects_stale_mixed_incomplete_and_duplicate_ids(setup_test_db):
    client = TestClient(app)
    stale_case = _create_catalyst_name_change_case(setup_test_db)
    stale = _post_catalyst_name_change(
        client,
        stale_case,
        _catalyst_name_change_payload(stale_case, expected_current_name="Older visible name"),
    )
    assert stale.status_code == 409
    _assert_catalyst_name_change_rolled_back(setup_test_db, stale_case)

    incomplete_case = _create_catalyst_name_change_case(setup_test_db)
    incomplete = _post_catalyst_name_change(
        client,
        incomplete_case,
        _catalyst_name_change_payload(
            incomplete_case,
            affected_dft_result_ids=[str(incomplete_case["result_ids"][0])],
        ),
    )
    assert incomplete.status_code == 400
    assert "missing=" in incomplete.text
    _assert_catalyst_name_change_rolled_back(setup_test_db, incomplete_case)

    duplicate_case = _create_catalyst_name_change_case(setup_test_db)
    duplicate_ids = [str(value) for value in duplicate_case["result_ids"]]
    duplicate = _post_catalyst_name_change(
        client,
        duplicate_case,
        _catalyst_name_change_payload(
            duplicate_case,
            affected_dft_result_ids=[duplicate_ids[0], duplicate_ids[0]],
        ),
    )
    assert duplicate.status_code == 400
    assert "duplicates" in duplicate.text
    _assert_catalyst_name_change_rolled_back(setup_test_db, duplicate_case)

    mixed_case = _create_catalyst_name_change_case(setup_test_db)
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        other_sample = CatalystSample(paper_id=mixed_case["paper_id"], name="Other catalyst")
        session.add(other_sample)
        session.flush()
        other_row = DFTResult(
            paper_id=mixed_case["paper_id"],
            catalyst_sample_id=other_sample.id,
            property_type="band_gap",
            value=1.0,
            unit="eV",
        )
        session.add(other_row)
        session.commit()
        other_row_id = other_row.id
    mixed_ids = [str(mixed_case["result_ids"][0]), str(other_row_id)]
    mixed = _post_catalyst_name_change(
        client,
        mixed_case,
        _catalyst_name_change_payload(mixed_case, affected_dft_result_ids=mixed_ids),
    )
    assert mixed.status_code == 400
    assert "unexpected=" in mixed.text
    _assert_catalyst_name_change_rolled_back(setup_test_db, mixed_case)


@pytest.mark.parametrize("conflict_kind", ["batch", "database", "duplicate_sample"])
def test_catalyst_name_change_conflicts_return_409_and_roll_back(setup_test_db, conflict_kind):
    case = _create_catalyst_name_change_case(
        setup_test_db,
        duplicate_planned_identity=conflict_kind == "batch",
        add_database_conflict=conflict_kind == "database",
        add_duplicate_sample=conflict_kind == "duplicate_sample",
    )
    response = _post_catalyst_name_change(TestClient(app), case)
    assert response.status_code == 409, response.text
    if conflict_kind == "duplicate_sample":
        assert "catalyst_sample_name_already_exists" in response.text
        assert "use_duplicate_merge" in response.text
        assert "合并重复样本" in response.text
    else:
        assert "observation_key_conflict" in response.text
    _assert_catalyst_name_change_rolled_back(setup_test_db, case)
    if conflict_kind == "duplicate_sample":
        SessionLocal = sessionmaker(bind=setup_test_db, future=True)
        with SessionLocal() as session:
            samples = session.scalars(
                select(CatalystSample).where(CatalystSample.paper_id == case["paper_id"])
            ).all()
            assert len(samples) == 2
            assert {str(sample.name or "").strip() for sample in samples} == {
                "Wrong catalyst name",
                "correct catalyst name",
            }


def test_catalyst_name_change_rolls_back_when_second_identity_write_fails(setup_test_db, monkeypatch):
    case = _create_catalyst_name_change_case(setup_test_db)
    original = DFTAuditIssueLifecycleService.apply_result_identity
    calls = {"count": 0}

    def fail_second_identity(row, identity):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected second identity write failure")
        return original(row, identity)

    monkeypatch.setattr(DFTAuditIssueLifecycleService, "apply_result_identity", staticmethod(fail_second_identity))
    response = _post_catalyst_name_change(TestClient(app, raise_server_exceptions=False), case)
    assert response.status_code == 500
    _assert_catalyst_name_change_rolled_back(setup_test_db, case)


def test_catalyst_name_change_without_dft_keeps_legacy_update_behavior(setup_test_db):
    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        paper = Paper(title="No DFT rename", library_name="A", pdf_path="no-dft.pdf")
        session.add(paper)
        session.flush()
        sample = CatalystSample(paper_id=paper.id, name="Old no-DFT name", support="carbon")
        session.add(sample)
        session.commit()
        paper_id = paper.id
        sample_id = sample.id

    response = TestClient(app).post(
        f"/api/papers/{paper_id}/catalyst-samples/{sample_id}/basic-info",
        json={"name": "New no-DFT name", "support": "graphene"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["catalyst_sample"]["name"] == "New no-DFT name"
    assert "name_change" not in response.json()


def test_same_name_without_matching_rekey_audit_uses_ordinary_update_flow(setup_test_db):
    case = _create_catalyst_name_change_case(setup_test_db)
    response = _post_catalyst_name_change(
        TestClient(app),
        case,
        {
            "name": "Wrong catalyst name",
            "coordination": "Updated without a rename",
            "reviewer": "ordinary-update-test",
        },
    )
    assert response.status_code == 200, response.text
    assert "name_change" not in response.json()

    SessionLocal = sessionmaker(bind=setup_test_db, future=True)
    with SessionLocal() as session:
        sample = session.get(CatalystSample, case["sample_id"])
        assert sample.name == "Wrong catalyst name"
        assert sample.coordination == "Updated without a rename"
        audit = session.scalar(select(AuditLog).where(AuditLog.target_id == str(case["sample_id"])))
        assert audit is not None
        assert "name_identity_rekey" not in audit.payload
        rows = session.scalars(select(DFTResult).where(DFTResult.id.in_(case["result_ids"]))).all()
        for row in rows:
            assert (row.identity_version, row.subject_key, row.observation_key, row.identity_payload) == case[
                "old_identities"
            ][row.id]
            assert row.candidate_status == case["candidate_statuses"][row.id]
        assert {review.reviewer_status for review in session.scalars(select(ExtractionFieldReview)).all()} == {
            "verified"
        }
