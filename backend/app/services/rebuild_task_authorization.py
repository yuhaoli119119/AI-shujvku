"""Coordinator-controlled independent task grants, not self-asserted task names.

There is deliberately no workbench HTTP grant-issuer endpoint. The independent
coordinator invokes this local adapter with a separate private authority key,
after verifying its own writer/reviewer handles. Only a hash/MAC is persisted;
the scoped bearer grant is delivered privately to the selected reviewer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.models import AuditLog, WorkflowJob
from app.services.codex_web_dispatch import read_thread_status
from app.services.rebuild_workflow_service import RebuildWorkflowError

TYPE = "rebuild_task_authorization"
ALLOWED_CWDS = {"/opt/literature-ai", "/opt/AI-shujvku/literature-ai"}


def _key(settings):
    path = getattr(settings, "rebuild_review_authority_key_file", "")
    if not path:
        raise RebuildWorkflowError("independent_task_authority_not_configured")
    try:
        source = Path(path)
        if source.stat().st_mode & 0o077:
            raise ValueError("authority key must be private")
        key = source.read_bytes().strip()
        if len(key) < 32:
            raise ValueError("authority key too short")
        return key
    except (OSError, ValueError) as exc:
        raise RebuildWorkflowError("independent_task_authority_unavailable") from exc


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _mac(key, value):
    return hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def _token(key, claims):
    return hmac.new(key, b"private-review-token:" + _canonical(claims), hashlib.sha256).hexdigest()


def review_evidence_root(value):
    """Keep legacy output grants valid; new grants use the durable data mount."""
    from app.utils.project_paths import PROJECT_ROOT
    root = Path(value).resolve()
    legacy = (PROJECT_ROOT / "outputs").resolve()
    # Resolve the data mount, not the evidence directory: an evidence symlink
    # must not expand the allowed namespace outside that mount.
    durable = (PROJECT_ROOT / "data").resolve() / "reviewer-evidence"
    if root == legacy or root.is_relative_to(legacy):
        return root
    if root != durable and root.is_relative_to(durable):
        return root
    raise RebuildWorkflowError("review_evidence_root_outside_project_outputs")


async def issue_reviewer_authorization(session, freeze_id, *, request_id, owner, reviewer_task_id,
                                        reviewer_thread_id, writer_thread_id, evidence_root,
                                        issuer_key, settings, native_read=None, ttl_seconds=3600,
                                        correction_event_ids=None, writer_ownership_evidence=None, writer_ownership_sha256=None):
    """Trusted local coordinator API. Never call with shared login credentials.

    issuer_key is NOT the workbench session secret and never goes to the model.
    The coordinator supplies handles from its durable dispatch records; live
    native readback verifies identity and project before any grant is persisted.
    """
    key = _key(settings)
    if not isinstance(issuer_key, bytes) or not hmac.compare_digest(key, issuer_key):
        raise RebuildWorkflowError("review_authority_not_authenticated")
    if not owner or not request_id or not 1 <= ttl_seconds <= 86400:
        raise RebuildWorkflowError("review_authority_request_invalid")
    if writer_thread_id == reviewer_thread_id:
        raise RebuildWorkflowError("independent_reviewer_required")
    from app.services.paper_db_phase import run_phase
    evidence_root = review_evidence_root(evidence_root)
    request = {"freeze_id": freeze_id, "owner": owner, "reviewer_task_id": reviewer_task_id,
               "reviewer_thread_id": reviewer_thread_id, "writer_thread_id": writer_thread_id,
               "evidence_root": str(evidence_root)}
    witnessed = any(v is not None for v in (correction_event_ids, writer_ownership_evidence, writer_ownership_sha256))
    witness_data = None
    if witnessed:
        if not correction_event_ids or not writer_ownership_evidence or not writer_ownership_sha256:
            raise RebuildWorkflowError('correction_witness_all_fields_required')
        import asyncio
        from app.services.rebuild_correction_witness import load_artifact
        witness_data, relative = await asyncio.to_thread(load_artifact, evidence_root, writer_ownership_evidence, writer_ownership_sha256)
        request['correction_witness'] = {'event_ids': sorted(str(i) for i in correction_event_ids),
            'artifact_relative_path': relative, 'artifact_sha256': writer_ownership_sha256,
            'basis': 'current_coordinator_witness_not_historical_event_identity'}
    grant_id = "reviewauth-" + hashlib.sha256((freeze_id + "|" + request_id).encode()).hexdigest()[:48]

    def snapshot(worker):
        frozen = worker.get(WorkflowJob, freeze_id)
        if frozen is None or frozen.type != "rebuild_acceptance" or frozen.payload["writer"] != owner:
            raise RebuildWorkflowError("review_authority_freeze_owner_mismatch")
        if reviewer_task_id == frozen.payload["writer_task_id"]:
            raise RebuildWorkflowError("independent_reviewer_required")
        if witnessed:
            from app.services.rebuild_correction_witness import validate_entries
            validate_entries(worker, frozen, owner, writer_thread_id, request['correction_witness']['event_ids'], witness_data)
        existing = worker.get(WorkflowJob, grant_id)
        if existing:
            if (existing.payload["request"] != request or existing.status != "active" or
                    not hmac.compare_digest(existing.payload["claims_mac"], _mac(key, existing.payload["claims"]))):
                raise RebuildWorkflowError("review_authority_request_id_reused")
            return {"replay": {"grant_id": grant_id,
                                "authorization": _token(key, existing.payload["claims"]),
                                "replayed": True}}
        return {"replay": None, "paper_id": frozen.payload["paper_id"],
                "freeze_status": frozen.status, "freeze_payload": dict(frozen.payload)}

    checked = await run_phase(session, snapshot)
    if checked["replay"] is not None:
        return checked["replay"]
    read = native_read or read_thread_status
    statuses = []
    for thread_id in (writer_thread_id, reviewer_thread_id):
        observed = await read(thread_id, settings=settings, include_goal=False)
        if observed is None or observed.thread_id != thread_id or observed.cwd not in ALLOWED_CWDS or not observed.turn_id:
            raise RebuildWorkflowError("review_native_identity_unverified")
        statuses.append(observed)
    def apply(worker):
        from app.services.rebuild_acceptance import _locked_paper
        from uuid import UUID
        _locked_paper(worker, UUID(checked["paper_id"]))
        frozen = worker.scalar(select(WorkflowJob).where(
            WorkflowJob.job_id == freeze_id).with_for_update())
        if (frozen is None or frozen.status != checked["freeze_status"] or
                frozen.payload != checked["freeze_payload"] or
                frozen.payload.get("writer") != owner or
                reviewer_task_id == frozen.payload.get("writer_task_id")):
            raise RebuildWorkflowError("review_authority_freeze_changed_during_readback")
        existing = worker.get(WorkflowJob, grant_id)
        if existing:
            if (existing.payload["request"] != request or existing.status != "active" or
                    not hmac.compare_digest(existing.payload["claims_mac"], _mac(key, existing.payload["claims"]))):
                raise RebuildWorkflowError("review_authority_request_id_reused")
            return {"grant_id": grant_id, "authorization": _token(key, existing.payload["claims"]),
                    "replayed": True}
        reviewer = worker.get(WorkflowJob, reviewer_task_id)
        if reviewer is not None and reviewer.type == "rebuild_pipeline_attempt":
            if (reviewer.payload.get("stage") != "reviewer" or
                    reviewer.payload.get("freeze_id") != freeze_id or
                    reviewer.progress.get("thread_id") != reviewer_thread_id):
                raise RebuildWorkflowError("review_native_task_binding_mismatch")
        elif reviewer_task_id != reviewer_thread_id:
            raise RebuildWorkflowError("review_native_task_binding_mismatch")
        issued_at = datetime.utcnow()
        claims = {**request, "grant_id": grant_id, "paper_id": frozen.payload["paper_id"],
                  "revision": frozen.payload["snapshot"]["revision"], "issued_at": issued_at.isoformat(),
                  "expires_at": (issued_at + timedelta(seconds=ttl_seconds)).isoformat(),
                  "verified_writer_turn_id": statuses[0].turn_id,
                  "verified_reviewer_turn_id": statuses[1].turn_id,
                  "authority": "separate_private_coordinator_key_and_native_readback"}
        if witnessed:
            from app.services.rebuild_correction_witness import load_artifact, validate_entries
            current_data, _ = load_artifact(evidence_root, writer_ownership_evidence, writer_ownership_sha256)
            entries = validate_entries(worker, frozen, owner, writer_thread_id, request['correction_witness']['event_ids'], current_data)
            claims['correction_witness'] = {**request['correction_witness'], 'events': entries}
        token = _token(key, claims)
        worker.add(WorkflowJob(job_id=grant_id, type=TYPE, status="active", library_name=frozen.library_name,
                               payload={"request": request, "claims": claims,
                                        "claims_mac": _mac(key, claims),
                                        "token_sha256": hashlib.sha256(token.encode()).hexdigest()}))
        worker.add(AuditLog(paper_id=frozen.payload["paper_id"], action="review_task_authorized",
                            source="rebuild_authority_v1", target_type="workflow_jobs",
                            target_id=grant_id, payload={"owner": owner, "request_id": request_id,
                            "claims": claims, "claims_mac": _mac(key, claims)}))
        worker.flush()
        return {"grant_id": grant_id, "authorization": token, "replayed": False}

    return await run_phase(session, apply)


def verify_reviewer_authorization(session, *, authorization, frozen, actor, reviewer_task_id, settings):
    if not authorization or len(authorization) != 64:
        raise RebuildWorkflowError("independent_task_authorization_required")
    key = _key(settings)
    digest = hashlib.sha256(authorization.encode()).hexdigest()
    grant = session.scalar(select(WorkflowJob).where(WorkflowJob.type == TYPE,
        WorkflowJob.payload.op("->>")("token_sha256") == digest))
    if grant is None or grant.status != "active":
        raise RebuildWorkflowError("independent_task_authorization_invalid")
    claims = grant.payload["claims"]
    if not hmac.compare_digest(grant.payload["claims_mac"], _mac(key, claims)):
        raise RebuildWorkflowError("independent_task_authorization_invalid")
    if not hmac.compare_digest(authorization, _token(key, claims)):
        raise RebuildWorkflowError("independent_task_authorization_invalid")
    if (claims["owner"] != actor or claims["freeze_id"] != frozen.job_id or claims["paper_id"] != frozen.payload["paper_id"] or
            claims["revision"] != frozen.payload["snapshot"]["revision"] or claims["reviewer_task_id"] != reviewer_task_id or
            claims["writer_thread_id"] == claims["reviewer_thread_id"] or
            datetime.fromisoformat(claims["expires_at"]) <= datetime.utcnow()):
        raise RebuildWorkflowError("independent_task_authorization_invalid")
    if claims.get('correction_witness'):
        from app.services.rebuild_correction_witness import load_artifact, validate_entries
        witness = claims['correction_witness']
        data, _ = load_artifact(claims['evidence_root'], witness['artifact_relative_path'], witness['artifact_sha256'])
        entries = validate_entries(session, frozen, actor, claims['writer_thread_id'], witness['event_ids'], data)
        if entries != witness['events']:
            raise RebuildWorkflowError('correction_witness_claim_changed')
    return claims
