"""Acceptance is an explicit independent receipt, never output/status inference."""
import copy
import hashlib

from sqlalchemy import func, select

from app.db.models import AuditLog, PaperSection, RebuildDataValue, RebuildPaperFile, WorkflowJob
from test_rebuild_mutation_audit import audit_env, assets, row_body


def freeze_body(env):
    with env["factory"]() as session:
        file = session.get(RebuildPaperFile, env["file"])
        digest = file.sha256
    return {"request_id": "freeze-1", "task_id": "writer-task", "coverage": [
        {"file_id": str(env["file"]), "source_sha256": digest, "reading_pdf_sha256": digest,
         "covered_pages": [1, 2], "no_extractable_content_reason": "Synthetic PDF has no applicable scientific values"}]}


def base(env):
    return f'/api/rebuild/papers/{env["paper"]}/acceptance'


def review_body(env, frozen, decision="accepted"):
    return {"request_id": "review-1", "reviewer_task_id": "independent-task", "expected_revision": frozen["revision"],
            "decision": decision, "message": "Independent synthetic PDF and DB fixture readback.",
            "reviewed_file_ids": [str(env["file"])], "evidence_locators": [
                {"file_id": str(env["file"]), "page_number": 1, "quote": "Synthetic table", "locator": "first page"}]}


def test_old_outputs_pending_and_writer_cannot_accept(audit_env):
    env = audit_env
    assert env["client"].post(assets(env), json={"asset_key": "old", "asset_type": "table", "context_text": "s 1.00s"}).status_code == 200
    pending = env["client"].get(base(env)).json()
    assert pending["acceptance"]["status"] == "pending" and pending["contract"]["parsed_implies_accepted"] is False
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    assert frozen["status"] == "partial" and frozen["progress"]["extraction"] == "extracted"
    assert any(issue["code"] == "table_representation_incomplete" for issue in frozen["progress"]["rules"]["issues"])
    assert len(frozen["output_manifest"]) == 2
    response = env["client"].post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen))
    assert response.status_code == 409 and response.json()["detail"] == "independent_task_authorization_required"


def test_freeze_review_persist_replay_and_changed_output_invalidates(audit_env):
    env = audit_env
    assert env["client"].post("/api/rebuild/rows/import", json=row_body(env)).status_code == 200
    body = freeze_body(env)
    response = env["client"].post(base(env) + "/freeze", json=body)
    assert response.status_code == 200, response.text
    frozen = response.json()
    assert frozen["status"] == "rules_checked"
    assert env["client"].post(base(env) + "/freeze", json=body).json()["replayed"]
    other = env["client_for"]("other")
    try:
        review = review_body(env, frozen)
        endpoint = base(env) + f'/{frozen["job_id"]}/reviews'
        accepted = other.post(endpoint, json=review)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted" and accepted.json()["review"]["reviewer"] == "other"
        assert other.post(endpoint, json=review).json()["replayed"]
        changed = copy.deepcopy(review); changed["message"] = "different result"
        assert other.post(endpoint, json=changed).status_code == 409
    finally:
        other.close()
    assert env["client"].get(base(env)).json()["acceptance"]["status"] == "accepted"
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "independent_review")) == 1
        value = session.scalar(select(RebuildDataValue)); value.raw_value = "0.0101"; session.commit()
    projected = env["client"].get(base(env)).json()["acceptance"]
    assert projected["status"] == "review_incomplete" and projected["frozen_status"] == "accepted"


def test_partial_coverage_conflicts_and_review_incomplete_never_accept(audit_env):
    env = audit_env
    body = row_body(env)
    assert env["client"].post("/api/rebuild/rows/import", json=body).status_code == 200
    with env["factory"]() as session:
        value = session.scalar(select(RebuildDataValue)); value.conflict = {"status": "unresolved", "candidates": [{"raw_value": "0.0100"}, {"raw_value": "0.0200"}]}; session.commit()
    body = freeze_body(env); body["coverage"][0]["covered_pages"] = [1]; body["coverage"][0]["remaining_reason"] = "Physical page 2 still pending"
    response = env["client"].post(base(env) + "/freeze", json=body)
    assert response.status_code == 200, response.text
    frozen = response.json(); assert frozen["status"] == "partial"
    issues = {issue["code"] for issue in frozen["progress"]["rules"]["issues"]}
    assert {"file_coverage_incomplete", "unresolved_source_conflict"} <= issues
    other = env["client_for"]("other")
    try:
        endpoint = base(env) + f'/{frozen["job_id"]}/reviews'
        assert other.post(endpoint, json=review_body(env, frozen)).status_code == 409
        incomplete = other.post(endpoint, json=review_body(env, frozen, "review_incomplete"))
        assert incomplete.status_code == 200, incomplete.text
        assert env["client"].get(base(env)).json()["acceptance"]["status"] == "review_incomplete"
    finally:
        other.close()


def test_physical_file_hash_and_revision_are_enforced(audit_env):
    env = audit_env
    body = freeze_body(env)
    wrong = copy.deepcopy(body); wrong["coverage"][0]["source_sha256"] = "f" * 64
    assert env["client"].post(base(env) + "/freeze", json=wrong).status_code != 200
    wrong = copy.deepcopy(body); wrong["coverage"][0]["covered_pages"] = [1, 3]
    assert env["client"].post(base(env) + "/freeze", json=wrong).status_code != 200
    wrong = copy.deepcopy(body); wrong["coverage"][0]["covered_pages"] = [1, True]
    assert env["client"].post(base(env) + "/freeze", json=wrong).status_code == 422
    frozen = env["client"].post(base(env) + "/freeze", json=body).json()
    with env["factory"]() as session:
        session.add(PaperSection(paper_id=env["paper"], section_title="new", text="New output after freeze")); session.commit()
    other = env["client_for"]("other")
    try:
        assert other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=review_body(env, frozen)).status_code == 409
    finally:
        other.close()


def test_empty_output_must_be_explicit_and_freeze_does_not_create_scientific_state(audit_env):
    env = audit_env
    body = freeze_body(env); body["coverage"][0].pop("no_extractable_content_reason")
    response = env["client"].post(base(env) + "/freeze", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needsfix"
    with env["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataValue)) == 0
        assert session.scalar(select(func.count()).select_from(PaperSection)) == 0
        job = session.get(WorkflowJob, response.json()["job_id"])
        assert job.type == "rebuild_acceptance"


def test_independent_incomplete_review_can_report_no_evidence_without_faking_coverage(audit_env):
    env = audit_env
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    other = env["client_for"]("other")
    body = review_body(env, frozen, "review_incomplete")
    body.update(reviewed_file_ids=[], evidence_locators=[], message="Source could not be independently read; no acceptance asserted.")
    try:
        result = other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=body)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "review_incomplete"
        body.update(request_id="forbidden-empty-accept", decision="accepted")
        assert other.post(base(env) + f'/{frozen["job_id"]}/reviews', json=body).status_code == 422
    finally:
        other.close()


def test_freeze_binds_physical_output_image_not_only_its_database_path(audit_env):
    env = audit_env
    crop = {"asset_key": "frozen-png", "asset_type": "table", "file_id": str(env["file"]), "context_text": "s 1.00s",
            "request_id": "crop", "regions": [{"page_number": 1, "bbox": [0.0, 0.0, 1.0, 1.0]}]}
    response = env["client"].post(assets(env) + "/crop", json=crop)
    assert response.status_code == 200, response.text
    image = env["settings"].storage_root / response.json()["asset"]["image_path"]
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    image.write_bytes(image.read_bytes() + b"synthetic-only-changed-output")
    projected = env["client"].get(base(env)).json()["acceptance"]
    assert projected["stale"] and projected["status"] == "review_incomplete"
    assert projected["revision"] == frozen["revision"]
