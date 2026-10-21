"""Transactional receipts for real rebuild writes, using the workbench actor.

No commits here. A receipt and all changed entity images share the caller's
transaction. Paper row locking also serializes first-use idempotency keys.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import inspect, select

from app.db.models import (AuditLog, Paper, RebuildDataRow, RebuildDataValue,
                           RebuildValueSource, RebuildVisualAsset)
from app.services.rebuild_workflow_service import RebuildWorkflowError

VERSION = "rebuild_mutation_v1"
EVENT = "rebuild_mutation"
RECEIPT = "rebuild_mutation_receipt"
_SENSITIVE = re.compile(r"(?:password|passwd|secret|credential|authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|private[_-]?key)", re.I)


def safe_json(value):
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SENSITIVE.search(str(key)) else safe_json(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _snapshot(session, paper_id, operation, *, entity_id=None):
    if entity_id is None:
        raise RebuildWorkflowError("audit_snapshot_target_required")
    models = [RebuildVisualAsset] if operation.startswith("assets.") else [RebuildDataRow, RebuildDataValue, RebuildValueSource]
    result = {}
    for model in models:
        statement = select(model).where(model.paper_id == paper_id)
        if entity_id:
            statement = statement.where((model.id if model in {RebuildVisualAsset, RebuildDataRow} else model.row_id) == UUID(str(entity_id)))
        for record in session.scalars(statement):
            # All persisted columns, including full source pointers and conflicts.
            state = {column.key: getattr(record, column.key) for column in inspect(model).column_attrs}
            result[(model.__tablename__, str(record.id))] = safe_json(state)
    return result


def _record_changes(session, context, before, after, *, item_index=None):
    events = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        event_id = uuid4()
        session.add(AuditLog(id=event_id, paper_id=context["paper_id"], action=EVENT, source=VERSION,
                            target_type=key[0], target_id=key[1],
                            payload={"request_id": context["request_id"], "task_id": context["task_id"], "actor": context["actor"],
                                     "operation": context["operation"], "item_index": item_index,
                                     "before": before.get(key), "after": after.get(key),
                                     "changed_fields": [field for field in sorted(set(before.get(key, {})) | set(after.get(key, {})))
                                                        if before.get(key, {}).get(field) != after.get(key, {}).get(field)]}))
        events.append(str(event_id))
    return events


def capture_item(session, *, index, entity_id):
    """Preserve every successful item, even repeated updates to one entity."""
    context = session.info.get(VERSION)
    if context is None:
        return []  # Pure internal fixture callers need not create API receipts.
    after = _snapshot(session, context["paper_id"], context["operation"], entity_id=entity_id)
    before = context.pop("before_item", {})
    events = _record_changes(session, context, before, after, item_index=index)
    context["item_events"].extend(events)
    context["items_recorded"] = True
    return events


def capture_before_item(session, entity_id):
    """Called after importer identity resolution, before mutating its one entity.

    Cost is proportional to the touched row/asset, never the entire paper.
    """
    context = session.info.get(VERSION)
    if context is not None:
        context["before_item"] = (_snapshot(session, context["paper_id"], context["operation"], entity_id=entity_id)
                                  if entity_id is not None else {})


def audit_ids(request, payload):
    body_id = getattr(payload, "request_id", None)
    header_id = request.headers.get("X-Request-ID")
    if body_id and header_id and str(body_id) != header_id:
        raise RebuildWorkflowError("mutation_request_id_mismatch")
    request_id = str(body_id or header_id or uuid4())
    task_id = getattr(payload, "task_id", None) or request.headers.get("X-Task-ID")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", request_id):
        raise RebuildWorkflowError("mutation_request_id_invalid")
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", task_id):
        raise RebuildWorkflowError("mutation_task_id_invalid")
    return request_id, task_id


def run_audited(session, *, paper_id, operation, payload, actor, request_id, task_id, mutate, receipt_serializer=safe_json):
    if not actor or not actor.strip():
        raise RebuildWorkflowError("mutation_actor_required")
    if session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update()) is None:
        raise RebuildWorkflowError("paper_not_found")
    body = payload.model_dump(mode="json", exclude_unset=True)
    fingerprint = hashlib.sha256(json.dumps({"body": body, "task_id": task_id}, sort_keys=True,
                                           ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    previous = session.scalar(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == RECEIPT,
        AuditLog.source == VERSION, AuditLog.target_type == operation, AuditLog.target_id == request_id))
    if previous:
        if previous.payload["request_sha256"] != fingerprint or previous.payload["actor"] != actor:
            raise RebuildWorkflowError("mutation_request_id_reused")
        # Return the original result; it is a receipt, not a claim about current state.
        return {**previous.payload["result"], "audit": {**previous.payload["result"]["audit"], "replayed": True}}
    context = {"paper_id": paper_id, "operation": operation, "actor": actor, "request_id": request_id,
               "task_id": task_id, "item_events": [], "items_recorded": False}
    session.info[VERSION] = context
    try:
        result = mutate()
        session.flush()
        # Every public mutator captures its successful target. All-rejected
        # batches legitimately have no entity event, only a rejection receipt.
        events = context["item_events"]
    finally:
        session.info.pop(VERSION, None)
    result = {**result, "audit": {"request_id": request_id, "task_id": task_id,
                                  "event_ids": events, "replayed": False}}
    errors = result.get("errors", [])
    session.add(AuditLog(paper_id=paper_id, action=RECEIPT, source=VERSION,
                        target_type=operation, target_id=request_id,
                        payload={"actor": actor, "task_id": task_id, "request_id": request_id,
                                 "request_sha256": fingerprint,
                                 "outcome": "partial" if errors and events else "rejected" if errors else "succeeded",
                                 "result": receipt_serializer(result)}))
    session.flush()
    return result
