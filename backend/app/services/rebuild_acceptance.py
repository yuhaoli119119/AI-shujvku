"""Explicit scientific acceptance receipts. No legacy result promotes itself.

Uses existing workflow_jobs and append-only audit_logs, requiring no migration.
Frozen DB output manifests bind extraction, deterministic checks and independent
review to one paper/file/output revision. All callers own the transaction.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

import fitz
from sqlalchemy import select, text
from functools import lru_cache

from app.db.models import (AuditLog, Paper, PaperSection, RebuildDataRow, RebuildDataValue,
                           RebuildPaperFile, RebuildValueSource, RebuildVisualAsset, WorkflowJob)
from app.services.rebuild_mutation_audit import safe_json
from app.services.rebuild_file_preview import resolve_reading_pdf, _sha256
from app.services.rebuild_workflow_service import RebuildWorkflowError

JOB_TYPE = "rebuild_acceptance"
VERSION = "rebuild_acceptance_v1"
LABELS = {"pending": "待提取验收", "extracted": "已提取，待规则检查", "rules_checked": "规则检查通过，待独立验收",
          "needsfix": "需要修正", "accepted": "独立验收通过", "partial": "原文冲突 / 部分覆盖",
          "review_incomplete": "独立验收未完成"}


@lru_cache(maxsize=4096)
def _hash_at_stat(path, device, inode, size, mtime_ns, ctime_ns):
    from pathlib import Path
    return _sha256(Path(path))


def hash_bound_file(path, *, force=False):
    stat = path.stat()
    if force:
        return _sha256(path)
    return _hash_at_stat(str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _digest(value):
    return hashlib.sha256(json.dumps(safe_json(value), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def contract():
    return {"version": VERSION, "states": LABELS, "parsed_implies_accepted": False,
            "persistence": "workflow_jobs with append-only audit_logs; explicit freeze only; no history import",
            "independence": "Different authenticated reviewer principal, or same workbench account with a separate coordinator-issued authorization bound to distinct live-verified native task handles. Payload task names alone never prove independence. No shared-login HTTP grant issuer exists.",
            "gates": ["explicit frozen paper/file/output manifest", "complete physical-page coverage", "deterministic rule checks",
                      "independent reviewer evidence bound to frozen revision", "no unresolved source conflicts for accepted"],
            "unknown": "pending; HTTP 200, outputs counts, turn/completed and parsed cannot promote acceptance"}


def _scope(session, paper_id):
    from app.services.catalog_attachments import supplementary_attachments
    attachments = supplementary_attachments(session, [paper_id]).get(paper_id, [])
    # An unregistered linked SI remains explicit and prevents a full acceptance.
    missing = [{"paper_id": item["paper_id"], "reason": "linked_si_registration_required"}
               for item in attachments if not item["file_id"]]
    files = list(session.scalars(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id)
                                .order_by(RebuildPaperFile.id)))
    return files, missing


def output_manifest(session, paper_id, *, force_physical=False):
    manifest = []
    for model in (RebuildVisualAsset, RebuildDataRow, RebuildDataValue, RebuildValueSource, PaperSection):
        # PostgreSQL hashes persisted row JSON in place. Status/package reads
        # transfer only identities+hashes, not every chapter/value/conflict body.
        # sha256(bytea) is built into PostgreSQL; no pgcrypto migration needed.
        table = model.__tablename__
        image_column = ", t.image_path" if model is RebuildVisualAsset else ""
        rows = session.execute(text(
            f"SELECT t.id, encode(sha256(convert_to((to_jsonb(t) - 'embedding')::text, 'UTF8')), 'hex') AS row_sha256"
            f"{image_column} FROM {table} AS t WHERE t.paper_id = :paper_id ORDER BY t.id"), {"paper_id": paper_id})
        for record in rows:
            state = {"persisted_row_sha256": record.row_sha256}
            if model is RebuildVisualAsset and record.image_path:
                from app.config import get_settings
                from app.services.rebuild_workflow_service import resolve_asset_image_path
                try:
                    state["image_blob"] = {"sha256": hash_bound_file(resolve_asset_image_path(record, get_settings()), force=force_physical)}
                except (RebuildWorkflowError, OSError):
                    state["image_blob"] = {"status": "unavailable"}
            manifest.append({"kind": model.__tablename__, "id": str(record.id), "sha256": _digest(state)})
    return manifest


def current_snapshot(session, paper_id, *, force_physical=False):
    files, missing = _scope(session, paper_id)
    file_manifest = [{"file_id": str(file.id), "source_sha256": file.sha256, "role": file.role,
                      "storage_path": file.storage_path, "file_size": file.file_size} for file in files]
    manifest = output_manifest(session, paper_id, force_physical=force_physical)
    # Title/number may change; stable paper_id is the scientific identity.
    frozen = {"paper_id": str(paper_id), "files": file_manifest, "unregistered_files": missing, "output_manifest": manifest}
    return {**frozen, "revision": _digest(frozen)}, files


def _rules(session, paper_id, snapshot, coverage):
    issues = []
    by_id = {str(item["file_id"]): item for item in coverage}
    if snapshot["unregistered_files"]:
        issues.append({"code": "linked_si_registration_required", "files": snapshot["unregistered_files"]})
    if not snapshot["files"]:
        issues.append({"code": "no_registered_files"})
    for file in snapshot["files"]:
        item = by_id.get(file["file_id"])
        if item is None or len(item["covered_pages"]) != item["total_pages"]:
            issues.append({"code": "file_coverage_incomplete", "file_id": file["file_id"]})
    for value in session.scalars(select(RebuildDataValue).where(RebuildDataValue.paper_id == paper_id)):
        conflict = value.conflict or {}
        if conflict.get("status") in {"unresolved", "conflict", "pending", "value_conflict", "unit_conflict"} or conflict.get("legacy_unresolved") in {"value_conflict", "unit_conflict"} or (
            len(conflict.get("candidates", [])) > 1 and conflict.get("status") not in {"corrected_previous_retained", "evidence_history"}
        ):
            issues.append({"code": "unresolved_source_conflict", "value_id": str(value.id)})
        if value.value_type != "missing" and session.scalar(select(RebuildValueSource.id).where(RebuildValueSource.value_id == value.id).limit(1)) is None:
            issues.append({"code": "value_source_missing", "value_id": str(value.id)})
    for row in session.scalars(select(RebuildDataRow).where(RebuildDataRow.paper_id == paper_id)):
        if (row.properties or {}).get("_rebuild_import", {}).get("pending_identity_fields"):
            issues.append({"code": "identity_pending", "row_id": str(row.id)})
    from app.services.rebuild_table_content import table_content
    for asset in session.scalars(select(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == paper_id)):
        if asset.asset_type == "table" and table_content(asset)["format"] == "empty":
            issues.append({"code": "table_body_missing", "asset_id": str(asset.id)})
        if asset.image_path:
            from app.config import get_settings
            from app.services.rebuild_workflow_service import resolve_asset_image_path
            try:
                hash_bound_file(resolve_asset_image_path(asset, get_settings()))
            except (RebuildWorkflowError, OSError):
                issues.append({"code": "asset_image_missing", "asset_id": str(asset.id)})
    if not snapshot["output_manifest"] and not all((item.get("no_extractable_content_reason") or "").strip() for item in coverage):
        issues.append({"code": "empty_output_requires_reason"})
    return {"version": VERSION, "passed": not issues, "issues": issues,
            "scientific_acceptance": "not_implied", "revision": snapshot["revision"]}


def _locked_paper(session, paper_id):
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise RebuildWorkflowError("paper_not_found")
    # The established assets/rows/value/source/section/dedup write services all
    # take this same Paper row before their entity locks. Different papers stay
    # independent; never lock entire scientific tables for a single receipt.
    return paper


def freeze(session, paper_id, payload, settings, *, actor):
    if not actor or not actor.strip() or not payload.task_id.strip():
        raise RebuildWorkflowError("acceptance_actor_and_task_required")
    paper = _locked_paper(session, paper_id)
    job_id = "accept-" + _digest([str(paper_id), payload.request_id])[:48]
    request_hash = _digest(payload.model_dump(mode="json"))
    existing = session.get(WorkflowJob, job_id)
    if existing:
        if (existing.payload["request_sha256"], existing.payload["writer"]) != (request_hash, actor):
            raise RebuildWorkflowError("acceptance_request_id_reused")
        return {**serialize(existing), "replayed": True}
    snapshot, files = current_snapshot(session, paper_id, force_physical=True)
    by_id = {item.file_id: item for item in payload.coverage}
    if len(by_id) != len(payload.coverage) or set(by_id) != {file.id for file in files}:
        raise RebuildWorkflowError("acceptance_coverage_file_set_mismatch")
    coverage = []
    for file in files:
        item = by_id[file.id]
        if item.source_sha256 != file.sha256:
            raise RebuildWorkflowError("source_sha256_mismatch")
        path = resolve_reading_pdf(file, settings)  # Checks physical source hash.
        reading_hash = _sha256(path)
        if reading_hash != item.reading_pdf_sha256:
            raise RebuildWorkflowError("acceptance_reading_pdf_hash_mismatch")
        with fitz.open(path) as document:
            count = document.page_count
        if any(page > count for page in item.covered_pages):
            raise RebuildWorkflowError("acceptance_coverage_page_out_of_range")
        if len(item.covered_pages) < count and not (item.remaining_reason or "").strip():
            raise RebuildWorkflowError("acceptance_remaining_reason_required")
        coverage.append({**item.model_dump(mode="json"), "total_pages": count})
    rules = _rules(session, paper_id, snapshot, coverage)
    state = "rules_checked" if rules["passed"] else "partial" if any(issue["code"] in {"file_coverage_incomplete", "unresolved_source_conflict", "linked_si_registration_required"} for issue in rules["issues"]) else "needsfix"
    job = WorkflowJob(job_id=job_id, type=JOB_TYPE, status=state, library_name=paper.library_name,
                      payload={"paper_id": str(paper_id), "writer": actor, "writer_task_id": payload.task_id,
                               "request_sha256": request_hash, "snapshot": snapshot, "coverage": coverage},
                      progress={"extraction": "extracted", "rules": rules, "independent_review": "pending"})
    session.add(job)
    session.add(AuditLog(paper_id=paper_id, action="extraction_frozen", source=VERSION, target_type="workflow_jobs", target_id=job_id,
                        payload={"actor": actor, "task_id": payload.task_id, "request_id": payload.request_id,
                                 "revision": snapshot["revision"], "output_manifest": snapshot["output_manifest"], "coverage": coverage,
                                 "transitions": ["extracted", state], "rules": rules}))
    session.flush()
    return serialize(job)


def serialize(job):
    return {"job_id": job.job_id, "paper_id": job.payload["paper_id"], "status": job.status, "label": LABELS.get(job.status, "待核实"),
            "writer": job.payload["writer"], "writer_task_id": job.payload["writer_task_id"],
            "revision": job.payload["snapshot"]["revision"], "coverage": job.payload["coverage"],
            "output_manifest": job.payload["snapshot"]["output_manifest"], "progress": job.progress, "review": job.result,
            "parsed_implies_accepted": False}


def review(session, paper_id, job_id, payload, settings, *, actor, authorization=None):
    _locked_paper(session, paper_id)
    job = session.get(WorkflowJob, job_id)
    if job is None or job.type != JOB_TYPE or job.payload["paper_id"] != str(paper_id):
        raise RebuildWorkflowError("acceptance_freeze_not_found")
    if not actor or not actor.strip():
        raise RebuildWorkflowError("independent_reviewer_required")
    identity = {"kind": "distinct_authenticated_principal", "actor": actor, "task_id": payload.reviewer_task_id}
    if actor == job.payload["writer"]:
        from app.services.rebuild_task_authorization import verify_reviewer_authorization
        claims = verify_reviewer_authorization(session, authorization=authorization, frozen=job, actor=actor,
                                               reviewer_task_id=payload.reviewer_task_id, settings=settings)
        identity = {"kind": "coordinator_authorized_native_task", "actor": actor, "task_id": payload.reviewer_task_id,
                    "grant_id": claims["grant_id"], "reviewer_thread_id": claims["reviewer_thread_id"],
                    "writer_thread_id": claims["writer_thread_id"]}
    elif payload.reviewer_task_id == job.payload["writer_task_id"]:
        raise RebuildWorkflowError("independent_reviewer_required")
    request_hash = _digest(payload.model_dump(mode="json"))
    events = session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == "independent_review", AuditLog.source == VERSION, AuditLog.target_id == job_id)).all()
    for event in events:
        if event.payload["request_id"] == payload.request_id:
            if (event.payload["request_sha256"], event.payload["actor"]) != (request_hash, actor):
                raise RebuildWorkflowError("acceptance_request_id_reused")
            return {**event.payload["receipt"], "replayed": True}
    if job.status == "accepted":
        raise RebuildWorkflowError("accepted_receipt_is_immutable_freeze_new_revision")
    snapshot, files = current_snapshot(session, paper_id, force_physical=True)
    if (payload.expected_revision != job.payload["snapshot"]["revision"] or
            payload.decision != "review_incomplete" and snapshot["revision"] != payload.expected_revision):
        raise RebuildWorkflowError("acceptance_frozen_revision_changed")
    reviewed = set(payload.reviewed_file_ids)
    frozen_files = {UUID(item["file_id"]) for item in job.payload["coverage"]}
    if (not reviewed <= frozen_files or len(reviewed) != len(payload.reviewed_file_ids) or
            payload.decision == "accepted" and reviewed != {file.id for file in files}):
        raise RebuildWorkflowError("acceptance_review_file_set_mismatch")
    coverage_by_file = {UUID(item["file_id"]): item for item in job.payload["coverage"]}
    if {item.file_id for item in payload.evidence_locators} != reviewed:
        raise RebuildWorkflowError("acceptance_review_evidence_each_file_required")
    for item in payload.evidence_locators:
        if item.page_number not in coverage_by_file[item.file_id]["covered_pages"]:
            raise RebuildWorkflowError("acceptance_review_evidence_outside_frozen_coverage")
    # Recheck physical bytes, including derived reading editions, before any decision.
    reading_hashes = {item["file_id"]: item["reading_pdf_sha256"] for item in job.payload["coverage"]}
    for file in files if payload.decision != "review_incomplete" else []:
        if _sha256(resolve_reading_pdf(file, settings)) != reading_hashes[str(file.id)]:
            raise RebuildWorkflowError("acceptance_reading_pdf_hash_mismatch")
    rules = _rules(session, paper_id, snapshot, job.payload["coverage"])
    if payload.decision == "accepted" and (not rules["passed"] or payload.unresolved_conflicts):
        raise RebuildWorkflowError("acceptance_gates_not_satisfied")
    before = job.status
    job.status = payload.decision
    job.progress = {**job.progress, "rules": rules, "independent_review": payload.decision}
    job.result = {**payload.model_dump(mode="json"), "reviewer": actor, "reviewer_identity": identity}
    result = serialize(job)
    session.add(AuditLog(paper_id=paper_id, action="independent_review", source=VERSION, target_type="workflow_jobs", target_id=job_id,
                        payload={"request_id": payload.request_id, "request_sha256": request_hash, "actor": actor,
                                 "task_id": payload.reviewer_task_id, "before": before, "after": payload.decision, "receipt": result}))
    session.flush()
    return result


def acceptance_status(session, paper_id):
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.type == JOB_TYPE,
        WorkflowJob.payload.op("->>")("paper_id") == str(paper_id)).order_by(WorkflowJob.created_at.desc(), WorkflowJob.job_id.desc()))
    if job is None:
        return {"status": "pending", "label": LABELS["pending"], "parsed_implies_accepted": False, "reason": "no_explicit_freeze_and_review_receipt"}
    result = serialize(job)
    current, _ = current_snapshot(session, paper_id)
    if current["revision"] != result["revision"]:
        return {**result, "status": "review_incomplete", "label": LABELS["review_incomplete"],
                "frozen_status": job.status, "stale": True, "reason": "paper_file_or_output_changed_since_freeze"}
    if job.status == "accepted":
        from app.config import get_settings
        try:
            settings = get_settings()
            expected = {item["file_id"]: item["reading_pdf_sha256"] for item in job.payload["coverage"]}
            from app.services.rebuild_file_preview import _format, preview_cache_path
            from app.services.rebuild_workflow_service import resolve_file_path
            for file in _scope(session, paper_id)[0]:
                original = resolve_file_path(file, settings)
                if hash_bound_file(original) != file.sha256:
                    raise RebuildWorkflowError("source_sha256_mismatch")
                reading = original if _format(original) == "pdf" else preview_cache_path(file, settings)
                if hash_bound_file(reading) != expected[str(file.id)]:
                    raise RebuildWorkflowError("acceptance_reading_pdf_hash_mismatch")
        except (RebuildWorkflowError, OSError, ValueError, KeyError):
            return {**result, "status": "review_incomplete", "label": LABELS["review_incomplete"],
                    "frozen_status": job.status, "stale": True, "reason": "frozen_physical_source_unverified"}
    return result
