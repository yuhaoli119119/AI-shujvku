import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import AuditLog, RebuildDataValue, WorkflowJob
from app.services.codex_web_dispatch import ThreadStatus
from app.services.rebuild_task_authorization import issue_reviewer_authorization
from app.services.rebuild_review_progress import progress_signature
from app.services.rebuild_progress_guard import evidence_signature, initial_guard, observe
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.utils import project_paths
from test_rebuild_mutation_audit import audit_env
from test_rebuild_acceptance import base, freeze_body, review_body


def grant(env, monkeypatch, *, same_handle=False, fake_owner=False, durable=False):
    key = env["settings"].storage_root.parent / "fixture-authority.key"
    key.write_bytes(b"separate-fixture-authority-key-32bytes-minimum")
    key.chmod(0o600)
    monkeypatch.setenv("LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE", str(key))
    get_settings.cache_clear(); settings = get_settings()
    frozen = env["client"].post(base(env) + "/freeze", json=freeze_body(env)).json()
    # Keep the real project/outputs containment rule, but relocate this test's
    # project boundary beneath pytest's own writable artifact directory. No
    # assumption about container mount names or the checkout being writable.
    fixture_project = settings.storage_root.parent / "review-fixture-project"
    monkeypatch.setattr(project_paths, "PROJECT_ROOT", fixture_project)
    writable = (fixture_project / "data" / "reviewer-evidence" / "paper-1" if durable else
                fixture_project / "outputs" / "review-evidence")
    writable.mkdir(parents=True, exist_ok=True)
    async def native(thread_id, **kwargs):
        return ThreadStatus("completed", "active", None, None, turn_id=thread_id + "-turn", thread_id=thread_id, cwd="/opt/literature-ai")
    with env["factory"]() as session:
        issued = asyncio.run(issue_reviewer_authorization(session, frozen["job_id"], request_id="controlled-1", owner="other" if fake_owner else "writer",
            reviewer_task_id="review-native", reviewer_thread_id="writer-native" if same_handle else "review-native", writer_thread_id="writer-native",
            evidence_root=writable, issuer_key=key.read_bytes(), settings=settings, native_read=native))
        session.commit()
    return frozen, issued, settings, writable


@pytest.mark.parametrize("durable", [False, True])
def test_same_workbench_account_separate_verified_native_tasks_can_accept(audit_env, monkeypatch, durable):
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch, durable=durable)
    body = review_body(env, frozen); body["reviewer_task_id"] = "review-native"
    response = env["client"].post(base(env) + f'/{frozen["job_id"]}/reviews', json=body,
                                 headers={"X-Reviewer-Authorization": issued["authorization"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "accepted" and result["writer"] == result["review"]["reviewer"] == "writer"
    identity = result["review"]["reviewer_identity"]
    assert identity["kind"] == "coordinator_authorized_native_task" and identity["writer_thread_id"] != identity["reviewer_thread_id"]
    with env["factory"]() as session:
        logs = session.scalars(select(AuditLog)).all()
        assert issued["authorization"] not in json.dumps([entry.payload for entry in logs])


def test_forged_task_or_token_and_same_task_are_rejected(audit_env, monkeypatch):
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch)
    endpoint = base(env) + f'/{frozen["job_id"]}/reviews'
    body = review_body(env, frozen); body["reviewer_task_id"] = "review-native"
    assert env["client"].post(endpoint, json=body).status_code != 200
    assert env["client"].post(endpoint, json=body, headers={"X-Reviewer-Authorization": "a" * 64}).status_code != 200
    body["reviewer_task_id"] = "writer-native"
    assert env["client"].post(endpoint, json=body, headers={"X-Reviewer-Authorization": issued["authorization"]}).status_code != 200
    with pytest.raises(RebuildWorkflowError, match="independent_reviewer_required"):
        grant(env, monkeypatch, same_handle=True)
    async def must_not_read(*args, **kwargs):
        pytest.fail("same writer task identity must be rejected before native calls")
    with env["factory"]() as session:
        with pytest.raises(RebuildWorkflowError, match="independent_reviewer_required"):
            asyncio.run(issue_reviewer_authorization(session, frozen["job_id"], request_id="same-task", owner="writer",
                reviewer_task_id="writer-task", reviewer_thread_id="different-review-thread", writer_thread_id="writer-native",
                evidence_root=writable, issuer_key=Path(settings.rebuild_review_authority_key_file).read_bytes(),
                settings=settings, native_read=must_not_read))


def test_authority_is_separate_from_shared_session_and_native_identity_must_verify(audit_env, monkeypatch):
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch)
    async def missing(*args, **kwargs): return None
    with env["factory"]() as session:
        kwargs = dict(request_id="other-issuance", owner="writer", reviewer_task_id="another-native", reviewer_thread_id="another-native",
                      writer_thread_id="writer-native", evidence_root=writable, settings=settings, native_read=missing)
        with pytest.raises(RebuildWorkflowError, match="not_authenticated"):
            asyncio.run(issue_reviewer_authorization(session, frozen["job_id"], issuer_key=b"shared-login-is-not-authority", **kwargs))
        key = Path(settings.rebuild_review_authority_key_file).read_bytes()
        with pytest.raises(RebuildWorkflowError, match="native_identity_unverified"):
            asyncio.run(issue_reviewer_authorization(session, frozen["job_id"], issuer_key=key, **kwargs))


@pytest.mark.parametrize("durable", [False, True])
def test_readonly_review_progress_is_verified_and_duplicate_checkpoint_does_not_advance(audit_env, monkeypatch, durable):
    from datetime import datetime, timedelta
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch, durable=durable)
    with env["factory"]() as session:
        baseline = evidence_signature(session, env["paper"], role="reviewer", task_id="review-native")
        scientific_before = session.scalar(select(func.count()).select_from(RebuildDataValue))
    item = {"file_id": str(env["file"]), "page_number": 1, "source_sha256": frozen["coverage"][0]["source_sha256"],
            "reading_pdf_sha256": frozen["coverage"][0]["reading_pdf_sha256"], "check_kind": "table_precision", "quote": "Synthetic table", "locator": "first page"}
    artifact = {"version": "review_progress_v1", "paper_id": str(env["paper"]), "freeze_id": frozen["job_id"],
                "revision": frozen["revision"], "reviewer_task_id": "review-native", "observations": [item]}
    path = writable / "stage-1.json"; path.write_text(json.dumps(artifact))
    body = {"request_id": "progress-1", "reviewer_task_id": "review-native", "expected_revision": frozen["revision"],
            "artifact_path": path.name, "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    endpoint = base(env) + f'/{frozen["job_id"]}/review-progress'
    headers = {"X-Reviewer-Authorization": issued["authorization"]}
    response = env["client"].post(endpoint, json=body, headers=headers)
    assert response.status_code == 200, response.text
    with env["factory"]() as session:
        advanced = evidence_signature(session, env["paper"], role="reviewer", task_id="review-native")
        assert advanced != baseline
        assert session.scalar(select(func.count()).select_from(RebuildDataValue)) == scientific_before
    body["request_id"] = "progress-duplicate"
    assert env["client"].post(endpoint, json=body, headers=headers).status_code == 200
    with env["factory"]() as session:
        assert evidence_signature(session, env["paper"], role="reviewer", task_id="review-native") == advanced
    # Arbitrary messages/forged quotes cannot count as source-grounded progress.
    artifact["observations"][0]["quote"] = "not in physical source"
    path = writable / "forged.json"; path.write_text(json.dumps(artifact))
    body.update(request_id="forged-progress", artifact_path=path.name, artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert env["client"].post(endpoint, json=body, headers=headers).status_code == 400
    clock = datetime(2026, 10, 17)
    guard = initial_guard(baseline, now=clock, role="reviewer")
    guarded = observe(guard, advanced, {"turn_status": "inProgress"}, now=clock + timedelta(minutes=20))
    assert not guarded["needs_attention"] and guarded["business_advanced"]


@pytest.mark.parametrize("location", ["outside_project", "outside_outputs", "outputs_lookalike"])
def test_fixture_project_boundary_still_rejects_real_outside_evidence_roots(audit_env, monkeypatch, location):
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch)
    project = project_paths.PROJECT_ROOT
    outside = {"outside_project": project.parent / "outside-evidence",
               "outside_outputs": project / "not-outputs",
               "outputs_lookalike": project / "outputs-lookalike"}[location]
    outside.mkdir(parents=True, exist_ok=True)
    assert not outside.resolve().is_relative_to((project / "outputs").resolve())
    async def must_not_read(*args, **kwargs):
        pytest.fail("outside-root refusal must precede native calls")
    with env["factory"]() as session:
        with pytest.raises(RebuildWorkflowError, match="review_evidence_root_outside_project_outputs"):
            asyncio.run(issue_reviewer_authorization(session, frozen["job_id"], request_id="outside-root", owner="writer",
                reviewer_task_id="other-review-native", reviewer_thread_id="other-review-native", writer_thread_id="writer-native",
                evidence_root=outside, issuer_key=Path(settings.rebuild_review_authority_key_file).read_bytes(),
                settings=settings, native_read=must_not_read))


@pytest.mark.parametrize("escape", ["relative", "absolute", "symlink"])
def test_portable_review_progress_keeps_authorized_artifact_containment(audit_env, monkeypatch, escape):
    env = audit_env; frozen, issued, settings, writable = grant(env, monkeypatch)
    outside = writable.parent / "outside-progress.json"
    outside.write_text('{"outside": true}')
    if escape == "relative":
        submitted = "../outside-progress.json"
    elif escape == "absolute":
        submitted = str(outside)
    else:
        link = writable / "escape.json"
        link.symlink_to(outside)
        submitted = link.name
    body = {"request_id": "outside-artifact", "reviewer_task_id": "review-native", "expected_revision": frozen["revision"],
            "artifact_path": submitted, "artifact_sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}
    response = env["client"].post(base(env) + f'/{frozen["job_id"]}/review-progress', json=body,
                                 headers={"X-Reviewer-Authorization": issued["authorization"]})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "review_progress_artifact_path_invalid"
