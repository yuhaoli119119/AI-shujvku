"""Single-paper external AI work: ownership, source pins, CAS and durable receipts.

No model calls, thread creation, automatic continuation or schema changes.
The caller owns the transaction. Unknown/abandoned work retains its occupancy.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db.models import AuditLog, Paper, WorkflowJob
from app.services.rebuild_file_lifecycle import file_set
from app.services.rebuild_file_preview import _checked_source
from app.services.rebuild_mutation_audit import safe_json, run_audited
from app.services.rebuild_queue import paper_busy
from app.services.rebuild_workflow_service import RebuildWorkflowError, get_file

VERSION = "mcp_rebuild_v2"
TYPE = "rebuild_external_extract"
EVENT = "mcp_v2_receipt"


def _digest(value):
    return hashlib.sha256(json.dumps(safe_json(value), sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _identity(actor, request_id):
    if not actor or not actor.startswith("mcp:"):
        raise RebuildWorkflowError("authenticated_mcp_actor_required")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request_id or ""):
        raise RebuildWorkflowError("request_id_invalid")


def _lock(session, paper_id):
    # The Paper row is the per-paper writer mutex. Global scheduler control is
    # unrelated to an individual paper and must never serialize cross-paper AI.
    from app.services.paper_db_phase import BUSY, LockConflictError, is_lock_conflict
    session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
    try:
        paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    except OperationalError as exc:
        session.rollback()
        if is_lock_conflict(exc):
            raise LockConflictError(BUSY) from exc
        raise
    if paper is None:
        raise RebuildWorkflowError("paper_not_found")
    return paper


def _job(session, paper_id, work_id, actor):
    job = session.get(WorkflowJob, work_id)
    if (job is None or job.type != TYPE or (job.payload or {}).get("protocol") != VERSION
            or job.payload.get("paper_id") != str(paper_id) or job.payload.get("actor") != actor):
        raise RebuildWorkflowError("work_not_owned")
    return job


def _source_pins(session, paper_id, file_ids):
    pins = {}
    for fid in file_ids:
        record = get_file(session, UUID(str(fid)))
        if record.paper_id != paper_id:
            raise RebuildWorkflowError("file_not_in_scope")
        _checked_source(record, get_settings())  # Hash real bytes, not only DB metadata.
        pins[str(record.id)] = record.sha256
    return pins


def _backup_before_work(session, paper_id, work_id):
    """Immutable per-paper preimage before an external writer is admitted."""
    from sqlalchemy import inspect
    from app.db.models import (RebuildPaperFile, RebuildVisualAsset, RebuildDataRow,
                               RebuildDataValue, RebuildValueSource, PaperSection)
    content = {}
    for model in (Paper, RebuildPaperFile, RebuildVisualAsset, RebuildDataRow,
                  RebuildDataValue, RebuildValueSource, PaperSection):
        records = ([session.get(Paper, paper_id)] if model is Paper else
                   session.scalars(select(model).where(model.paper_id == paper_id)).all())
        content[model.__tablename__] = [{column.key: safe_json(getattr(record, column.key))
                                       for column in inspect(model).column_attrs if column.key != "embedding"}
                                      for record in records]
    body = json.dumps({"protocol": VERSION, "paper_id": str(paper_id), "work_id": work_id,
                       "preimage": content}, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    directory = get_settings().storage_root / "mcp-v2-backups" / str(paper_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{work_id}-{digest}.json"
    if not path.exists():
        with path.open("xb") as handle:
            handle.write(body)
            handle.flush()
            import os
            os.fsync(handle.fileno())
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RebuildWorkflowError("backup_verification_failed")
    return {"backup_id": path.name, "sha256": digest, "bytes": len(body)}


def _revision(session, paper_id, job):
    from app.services.rebuild_progress_guard import evidence_signature
    from app.services.reading_assistance import reading_revision
    paper = session.get(Paper, paper_id)
    return _digest({"work_id": job.job_id if job else None,
                    **({"status": job.status, "progress": job.progress} if job else {}),
                    "files": file_set(session, paper_id)["file_set_revision"],
                    "output": evidence_signature(session, paper_id),
                    "reading": reading_revision(paper)})


def _public(session, paper_id, job):
    return {"paper_id": str(paper_id), "work_id": job.job_id, "status": job.status,
            "file_ids": job.payload["file_ids"], "progress": job.progress,
            "work_revision": _revision(session, paper_id, job),
            "scientific_acceptance": "not_implied"}


def state(session, paper_id, actor, work_id=None):
    if work_id:
        return _public(session, paper_id, _job(session, paper_id, work_id, actor))
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.type == TYPE,
        WorkflowJob.payload.op("->>")("paper_id") == str(paper_id),
        WorkflowJob.payload.op("->>")("actor") == actor,
        WorkflowJob.payload.op("->>")("protocol") == VERSION,
    ).order_by(WorkflowJob.created_at.desc(), WorkflowJob.job_id.desc()).limit(1))
    return _public(session, paper_id, job) if job else {"paper_id": str(paper_id), "work_id": None}


def _receipt(session, paper_id, work_id, actor, request_id, operation=None, body=None):
    event = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == EVENT, AuditLog.source == VERSION,
        AuditLog.target_id == request_id))
    if event is None:
        return None
    data = event.payload
    if (data["actor"] != actor or data["work_id"] != work_id or
            operation is not None and (data["operation"] != operation or data["fingerprint"] != _digest(body))):
        if operation is None:
            raise RebuildWorkflowError("receipt_not_found")
        alternate = _digest([request_id, actor, work_id, operation, body])
        event = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
            AuditLog.action == EVENT, AuditLog.source == VERSION, AuditLog.target_id == alternate))
        if event is None:
            return None
        data = event.payload
    return {**data["result"], "replayed": True, "receipt_is_historical": True}


def _save_receipt(session, paper_id, work_id, actor, request_id, operation, body, result):
    old = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == EVENT, AuditLog.source == VERSION, AuditLog.target_id == request_id))
    if old is not None:
        request_id = _digest([request_id, actor, work_id, operation, body])
    session.add(AuditLog(paper_id=paper_id, action=EVENT, source=VERSION,
        target_type="workflow_jobs", target_id=request_id,
        payload={"actor": actor, "work_id": work_id, "operation": operation,
                 "fingerprint": _digest(body), "result": safe_json(result)}))
    session.flush()
    return result


def begin(session, paper_id, actor, request_id, file_ids, file_set_revision):
    _identity(actor, request_id)
    paper = _lock(session, paper_id)
    body = {"file_ids": sorted(str(fid) for fid in file_ids), "file_set_revision": file_set_revision}
    work_id = "mcpwork-" + _digest([str(paper_id), actor, request_id])[:48]
    replay = _receipt(session, paper_id, work_id, actor, request_id, "begin", body)
    if replay is not None:
        return replay
    if paper_busy(session, paper_id):
        raise RebuildWorkflowError("paper_has_other_unresolved_writer_or_reviewer")
    lifecycle = file_set(session, paper_id)
    active = set(lifecycle["active_file_ids"])
    selected_files = sorted(set(body["file_ids"]) & active) or sorted(active)
    if not selected_files:
        raise RebuildWorkflowError("file_not_in_scope")
    file_set_revision = lifecycle["file_set_revision"]
    pins = _source_pins(session, paper_id, selected_files)
    backup = _backup_before_work(session, paper_id, work_id)
    job = WorkflowJob(job_id=work_id, type=TYPE, status="running", library_name=paper.library_name,
        payload={"protocol": VERSION, "actor": actor, "agent": actor, "run_id": work_id,
                 "paper_id": str(paper_id), "file_ids": selected_files, "source_pins": pins,
                 "file_set_revision": file_set_revision, "backup": backup},
        progress={"revision": 0, "phase": "parsing", "content_status": "parsing",
                  "status_checked_at": datetime.utcnow().isoformat(), "file_stages": [], "missing_items": []})
    session.add(job)
    session.flush()
    return _save_receipt(session, paper_id, work_id, actor, request_id, "begin", body, _public(session, paper_id, job))


def require_work(session, paper_id, work_id, actor, expected_revision=None):
    _lock(session, paper_id)
    job = _job(session, paper_id, work_id, actor)
    if paper_busy(session, paper_id, excluding=work_id):
        raise RebuildWorkflowError("paper_has_other_unresolved_writer_or_reviewer")
    if file_set(session, paper_id)["file_set_revision"] != job.payload["file_set_revision"]:
        raise RebuildWorkflowError("source_changed")
    if _source_pins(session, paper_id, job.payload["file_ids"]) != job.payload["source_pins"]:
        raise RebuildWorkflowError("source_changed")
    if expected_revision is not None and _revision(session, paper_id, job) != expected_revision:
        raise RebuildWorkflowError("revision_conflict")
    return job


def _check_file_references(value, allowed):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "file_id" and item is not None and str(item) not in allowed:
                raise RebuildWorkflowError("file_not_in_scope")
            _check_file_references(item, allowed)
    elif isinstance(value, list):
        for item in value:
            _check_file_references(item, allowed)


def mutate(session, paper_id, work_id, actor, request_id, expected_revision, operation, payload, fn):
    _identity(actor, request_id)
    _lock(session, paper_id)
    job = None
    if work_id:
        try:
            job = _job(session, paper_id, work_id, actor)
        except RebuildWorkflowError:
            work_id = None
    body = {"payload": payload.model_dump(mode="json", exclude_unset=True), "expected_revision": expected_revision}
    replay = _receipt(session, paper_id, work_id, actor, request_id, operation, body)
    if replay is not None:
        return replay
    if paper_busy(session, paper_id, excluding=work_id):
        raise RebuildWorkflowError("paper_has_other_unresolved_writer_or_reviewer")
    allowed = set(job.payload["file_ids"]) if job else set(file_set(session, paper_id)["active_file_ids"])
    _check_file_references(body["payload"], allowed)
    session.info[VERSION] = work_id
    try:
        if operation.startswith(("assets.", "rows.")):
            result = run_audited(session, paper_id=paper_id, operation=operation, payload=payload,
                actor=actor, request_id=request_id, task_id=work_id, mutate=fn)
        else:
            result = fn()
        session.flush()
        if job:
            job.progress = {**job.progress, "revision": job.progress.get("revision", 0) + 1,
                            "status_checked_at": datetime.utcnow().isoformat()}
        session.flush()
        result = {**result, "work_id": work_id, "work_revision": _revision(session, paper_id, job)}
        return _save_receipt(session, paper_id, work_id, actor, request_id, operation, body, result)
    finally:
        session.info.pop(VERSION, None)


def _status(session, paper_id, work_id, actor, request_id, expected_revision, status,
            message, file_stages, missing_items, *, terminal):
    _identity(actor, request_id)
    _lock(session, paper_id)
    _job(session, paper_id, work_id, actor)
    operation = "finish" if terminal else "report"
    body = {"status": status, "message": message, "file_stages": file_stages,
            "missing_items": missing_items, "expected_revision": expected_revision}
    replay = _receipt(session, paper_id, work_id, actor, request_id, operation, body)
    if replay is not None:
        return replay
    if not expected_revision:
        raise RebuildWorkflowError("expected_revision_required")
    job = require_work(session, paper_id, work_id, actor, expected_revision)
    if status not in ({"completed", "partial", "failed"} if terminal else {"running", "needs_check"}):
        raise RebuildWorkflowError("work_status_invalid")
    _check_file_references(file_stages, set(job.payload["file_ids"]))
    from app.services.paper_processing_status import processing_statuses
    readback = processing_statuses(session, [paper_id])[paper_id]
    if terminal and status == "completed":
        # Counted historical work is not evidence that this attempt read every file.
        stages = {(str(item.get("file_id")), item.get("stage")): item for item in file_stages}
        if missing_items or any(stages.get((fid, "read"), {}).get("status") != "completed" for fid in job.payload["file_ids"]):
            raise RebuildWorkflowError("completion_requires_file_stage_coverage")
        files = {item["file_id"]: item for item in readback["files"]}
        for fid in job.payload["file_ids"]:
            for stage, count_key in (("assets", "assets"), ("rows", "sources")):
                entry = stages.get((fid, stage), {})
                if not ((entry.get("status") == "completed" and files.get(fid, {}).get("counts", {}).get(count_key)) or
                        (entry.get("status") == "not_applicable" and entry.get("message", "").strip())):
                    raise RebuildWorkflowError("completion_requires_file_results_or_reason")
    if terminal and status in {"partial", "failed"} and not (message or missing_items):
        raise RebuildWorkflowError("incomplete_reason_required")
    job.progress = {**job.progress, "revision": job.progress["revision"] + 1,
        "phase": "parsed" if status == "completed" else status,
        "content_status": "parsed" if status == "completed" else status,
        "message": message, "file_stages": file_stages, "missing_items": missing_items,
        "needs_check": status == "needs_check", "status_checked_at": datetime.utcnow().isoformat()}
    if terminal:
        job.status = "failed" if status == "failed" else "completed"
        job.result = {"file_ids": job.payload["file_ids"] if status == "completed" else [],
                      "counts": readback["counts"], "scientific_acceptance": "not_implied"}
        job.error = message if status == "failed" else None
    session.flush()
    return _save_receipt(session, paper_id, work_id, actor, request_id, operation, body, _public(session, paper_id, job))


def report(session, paper_id, work_id, actor, request_id, expected_revision,
           status="running", message="", file_stages=None, missing_items=None):
    return _status(session, paper_id, work_id, actor, request_id, expected_revision, status,
                   message, file_stages or [], missing_items or [], terminal=False)


def finish(session, paper_id, work_id, actor, request_id, expected_revision,
           status="partial", message="", file_stages=None, missing_items=None):
    return _status(session, paper_id, work_id, actor, request_id, expected_revision, status,
                   message, file_stages or [], missing_items or [], terminal=True)


def receipt(session, paper_id, work_id, actor, request_id):
    _identity(actor, request_id)
    if work_id:
        _job(session, paper_id, work_id, actor)
    result = _receipt(session, paper_id, work_id, actor, request_id)
    if result is None:
        raise RebuildWorkflowError("receipt_not_found")
    return result


def guard_workbench_write(session, paper_id, actor, task_id=None):
    """Use a short database transaction; historical jobs do not gate saves."""
    _lock(session, paper_id)
