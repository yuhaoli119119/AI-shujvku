"""Bounded observation of business evidence. Never dispatches or changes models."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import inspect, select

from app.db.models import PaperSection, RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildVisualAsset, WorkflowJob
from app.services.rebuild_mutation_audit import safe_json

MAX_EMPTY_COMPLETIONS = 3
MAX_STALLED_OBSERVATIONS = 3
MIN_OBSERVATION_SECONDS = 60
STALL_SECONDS = 15 * 60


def evidence_signature(session, paper_id, *, role="writer", task_id=None):
    """Real committed semantic output, excluding version bumps and retry metadata."""
    if role == "reviewer":
        from app.services.rebuild_review_progress import progress_signature
        return progress_signature(session, paper_id, task_id)
    output = []
    for model in (RebuildVisualAsset, RebuildDataRow, RebuildDataValue, RebuildValueSource, PaperSection):
        for record in session.scalars(select(model).where(model.paper_id == paper_id).order_by(model.id)):
            state = {column.key: getattr(record, column.key) for column in inspect(model).column_attrs
                     if column.key not in {"embedding", "updated_at", "created_at", "version"}}
            if model is RebuildDataRow:
                state["properties"] = {key: value for key, value in (state.get("properties") or {}).items() if not key.startswith("_rebuild_")}
            output.append([model.__tablename__, safe_json(state)])
    # Only explicit validated freeze coverage is an additional signal; native
    # completed turns, arbitrary checkpoint text and repeated receipts are not.
    coverage = set()
    for job in session.scalars(select(WorkflowJob).where(WorkflowJob.type == "rebuild_acceptance",
                               WorkflowJob.payload.op("->>")("paper_id") == str(paper_id))):
        for item in (job.payload or {}).get("coverage", []):
            coverage.add(json.dumps({"file_id": item["file_id"], "source_sha256": item["source_sha256"],
                                     "reading_pdf_sha256": item["reading_pdf_sha256"], "covered_pages": sorted(item["covered_pages"])}, sort_keys=True))
    if coverage:
        output.append(["validated_frozen_coverage", sorted(coverage)])
    digest = hashlib.sha256(json.dumps(output, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": digest, "entities": len(output)}


def _date(value, fallback):
    try:
        return datetime.fromisoformat(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


def initial_guard(signature, *, now=None, role="writer"):
    stamp = (now or datetime.utcnow()).isoformat()
    return {"version": "business_progress_v1", "role": role, "stall_seconds": 3600 if role == "reviewer" else STALL_SECONDS, "baseline": signature, "latest": signature,
            "seen_evidence_hashes": [signature["sha256"]], "last_advance_at": stamp, "started_at": stamp,
            "empty_completed_observations": 0, "stalled_observations": 0, "business_advanced": False,
            "needs_attention": False, "diagnostics": []}


def observe(guard, signature, native, *, now=None):
    now = now or datetime.utcnow()
    state = dict(guard or initial_guard(signature, now=now))
    diagnostics = list(state.get("diagnostics", []))
    seen = list(state.get("seen_evidence_hashes", []))
    progressed = signature["sha256"] not in seen
    if progressed:
        seen.append(signature["sha256"])
        state.update(last_advance_at=now.isoformat(), business_advanced=True,
                     empty_completed_observations=0, stalled_observations=0)
    # Fast UI polling does not exhaust the observation budget in seconds.
    due = now - _date(state.get("last_counted_at"), now - timedelta(seconds=MIN_OBSERVATION_SECONDS)) >= timedelta(seconds=MIN_OBSERVATION_SECONDS)
    empty_completed = native.get("turn_status") == "completed" and native.get("item_count") == 0
    if due and not progressed:
        if empty_completed:
            state["empty_completed_observations"] = state.get("empty_completed_observations", 0) + 1
        if now - _date(state.get("last_advance_at"), now) >= timedelta(seconds=state.get("stall_seconds", STALL_SECONDS)):
            state["stalled_observations"] = state.get("stalled_observations", 0) + 1
    if due:
        state["last_counted_at"] = now.isoformat()
    stopped = state.get("needs_attention", False) or state.get("empty_completed_observations", 0) >= MAX_EMPTY_COMPLETIONS or state.get("stalled_observations", 0) >= MAX_STALLED_OBSERVATIONS or len(seen) > 2048
    # Once stopped, new evidence needs an explicit reconciliation; no auto restart.
    state.update(latest=signature, seen_evidence_hashes=seen[:2049], needs_attention=stopped,
                 last_observed_at=now.isoformat(), automatic_retry=False)
    if due or progressed:
        diagnostics.append({"at": now.isoformat(), "native": {key: native.get(key) for key in
                            ("thread_id", "turn_id", "turn_status", "goal_status", "item_count")},
                            "evidence": signature, "new_evidence": progressed,
                            "reason": "needs_attention" if stopped else "evidence_advanced" if progressed else "no_new_business_evidence",
                            "root_cause": "undetermined"})
    state["diagnostics"] = diagnostics[-20:]
    return state


def retry_guidance(status_code, *, attempt=0, payload_kind=None, retry_after=None):
    """Advice only. Mutation outcome uncertainty is always reconciled first."""
    if status_code == 413:
        return {"action": "split_images" if payload_kind == "images" else "split_context" if payload_kind == "context" else "classify_context_vs_images",
                "automatic_retry": False, "same_attempt_only": True, "model_change": False}
    if status_code == 429 and attempt < 3:
        try:
            delay = min(120, max(1, float(retry_after))) if retry_after is not None else min(60, 5 * 2 ** attempt)
        except (TypeError, ValueError):
            delay = min(60, 5 * 2 ** attempt)
        return {"action": "bounded_backoff", "delay_seconds": delay, "attempt_limit": 3,
                "requires_known_rejection_before_retry": True, "model_change": False}
    return {"action": "needs_attention", "automatic_retry": False, "model_change": False}
