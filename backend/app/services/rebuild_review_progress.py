"""Verified readonly-review stage evidence, independent of scientific DB writes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import fitz
from sqlalchemy import select

from app.db.models import AuditLog, RebuildPaperFile, WorkflowJob
from app.services.rebuild_acceptance import _digest, _locked_paper, current_snapshot
from app.services.rebuild_file_preview import resolve_reading_pdf, _sha256
from app.services.rebuild_task_authorization import verify_reviewer_authorization
from app.services.rebuild_workflow_service import RebuildWorkflowError

CHECKS = {"coverage", "source_pointer", "table_precision", "figure_reading", "section_boundary", "conflict_check"}


def record_progress(session, paper_id, freeze_id, payload, settings, *, actor, authorization):
    _locked_paper(session, paper_id)
    frozen = session.get(WorkflowJob, freeze_id)
    if frozen is None or frozen.type != "rebuild_acceptance" or frozen.payload["paper_id"] != str(paper_id):
        raise RebuildWorkflowError("acceptance_freeze_not_found")
    claims = verify_reviewer_authorization(session, authorization=authorization, frozen=frozen, actor=actor,
                                           reviewer_task_id=payload.reviewer_task_id, settings=settings)
    fingerprint = _digest(payload.model_dump(mode="json"))
    old = session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == "review_progress",
                                                AuditLog.target_id == claims["grant_id"])).all()
    for entry in old:
        if entry.payload["request_id"] == payload.request_id:
            if entry.payload["request_sha256"] != fingerprint:
                raise RebuildWorkflowError("review_progress_request_id_reused")
            return {**entry.payload["receipt"], "replayed": True}
    current, _ = current_snapshot(session, paper_id)
    if current["revision"] != payload.expected_revision or claims["revision"] != payload.expected_revision:
        raise RebuildWorkflowError("acceptance_frozen_revision_changed")
    root = Path(claims["evidence_root"]).resolve()
    path = (root / payload.artifact_path).resolve()
    if not path.is_relative_to(root) or path == root or not path.is_file() or path.stat().st_size > 2_000_000:
        raise RebuildWorkflowError("review_progress_artifact_path_invalid")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != payload.artifact_sha256:
        raise RebuildWorkflowError("review_progress_artifact_hash_mismatch")
    try:
        artifact = json.loads(body)
        if any(artifact.get(key) != value for key, value in {
            "version": "review_progress_v1", "paper_id": str(paper_id), "freeze_id": freeze_id,
            "revision": payload.expected_revision, "reviewer_task_id": payload.reviewer_task_id}.items()):
            raise ValueError("artifact identity mismatch")
        observations = artifact["observations"]
        if not isinstance(observations, list) or not 1 <= len(observations) <= 100:
            raise ValueError("bounded observations required")
        expected = {item["file_id"]: item for item in frozen.payload["coverage"]}
        verified = set()
        verified_observations = []
        documents = {}
        try:
            for item in observations:
                fid, page, check, quote = item["file_id"], item["page_number"], item["check_kind"], item["quote"]
                coverage = expected[fid]
                if (type(page) is not int or page not in coverage["covered_pages"] or check not in CHECKS or
                        item["source_sha256"] != coverage["source_sha256"] or
                        item["reading_pdf_sha256"] != coverage["reading_pdf_sha256"] or
                        not isinstance(quote, str) or not quote.strip() or len(quote) > 20000 or
                        not isinstance(item.get("locator"), str) or not item["locator"].strip()):
                    raise ValueError("observation identity/coverage invalid")
                if fid not in documents:
                    file = session.get(RebuildPaperFile, UUID(fid))
                    if file is None or file.paper_id != paper_id:
                        raise ValueError("observation file not owned")
                    pdf = resolve_reading_pdf(file, settings)
                    if _sha256(pdf) != coverage["reading_pdf_sha256"]:
                        raise ValueError("reading PDF changed")
                    documents[fid] = fitz.open(pdf)
                physical = documents[fid][page - 1].get_text("text")
                if " ".join(quote.split()) not in " ".join(physical.split()):
                    raise ValueError("observation quote not in physical page")
                # New random filenames, messages or quote permutations cannot
                # manufacture progress. One source/page/check fact is counted once.
                verified.add((fid, page, check))
                verified_observations.append({key: item[key] for key in
                    ("file_id", "page_number", "source_sha256", "reading_pdf_sha256", "check_kind", "quote", "locator")})
        finally:
            for document in documents.values():
                document.close()
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise RebuildWorkflowError("review_progress_evidence_invalid") from exc
    result = {"verified": True, "scientific_acceptance": "not_implied", "artifact_sha256": payload.artifact_sha256,
              "reviewer_task_id": payload.reviewer_task_id, "coverage_keys": sorted(verified),
              "verified_observations": verified_observations}
    session.add(AuditLog(paper_id=paper_id, action="review_progress", source="rebuild_review_progress_v1", target_type="workflow_jobs",
                        target_id=claims["grant_id"], payload={"actor": actor, "reviewer_task_id": payload.reviewer_task_id,
                        "request_id": payload.request_id, "request_sha256": fingerprint, "revision": payload.expected_revision,
                        "artifact_path": str(path), "receipt": result}))
    session.flush()
    return result


def progress_signature(session, paper_id, task_id):
    keys = set()
    for event in session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == "review_progress",
                             AuditLog.source == "rebuild_review_progress_v1", AuditLog.payload.op("->>")("reviewer_task_id") == task_id)):
        if event.payload["receipt"].get("verified"):
            keys.update((event.payload["revision"], *key) for key in event.payload["receipt"].get("coverage_keys", []))
    return {"sha256": _digest(sorted(keys)), "entities": len(keys), "kind": "verified_readonly_review_coverage"}
