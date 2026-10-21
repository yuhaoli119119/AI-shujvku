"""Narrow metadata-only writes. Caller owns the transaction and paper lock.

The receipt/event contract is shared with other formal rebuild mutations. No
source, image, provenance, scientific value or relationship is assigned here.
"""
from __future__ import annotations

import hashlib
import json
import math
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import inspect, select

from app.db.models import AuditLog, PaperSection, RebuildVisualAsset, RebuildPaperFile
from app.services.rebuild_mutation_audit import VERSION, EVENT, safe_json

ASSET_METADATA_FIELDS = frozenset({"page_numbers", "y_axis_unit", "material_mapping"})

MODELS = {"assets": RebuildVisualAsset, "sections": PaperSection}


def validate_material_mapping(value):
    """Accept bounded, lossless JSON metadata, never a coerced mapping or scalar.

    Panel roles have no universal vocabulary: publication labels, conflicts and
    media identifiers must survive a correction without schema-driven loss.
    Scientific verification of those roles remains the caller's responsibility.
    """
    def reject():
        raise HTTPException(422, "material_mapping_nonempty_bounded_json_object_required")

    if not isinstance(value, dict) or not value:
        reject()

    def check(item, depth=0):
        if depth > 16:
            reject()
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str) or not key.strip() or "\x00" in key:
                    reject()
                check(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                check(nested, depth + 1)
        elif isinstance(item, str):
            if "\x00" in item:
                reject()
        elif item is None or isinstance(item, (bool, int)):
            pass
        elif isinstance(item, float):
            if not math.isfinite(item):
                reject()
        else:
            reject()

    check(value)
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 1_000_000:
            reject()
    except (TypeError, ValueError, UnicodeError):
        reject()
    return value


def metadata_state(session, paper_id, kind, target_id, *, lock=False):
    model = MODELS[kind]
    statement = select(model).where(model.id == target_id, model.paper_id == paper_id)
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "metadata_target_not_found_in_paper")
    # This endpoint deliberately supports legacy sections only: changing a
    # source-bound imported chapter would invalidate its immutable import proof.
    if kind == "sections" and session.scalar(select(AuditLog.id).where(
        AuditLog.paper_id == paper_id, AuditLog.action == "section_imported",
        AuditLog.target_id == str(target_id)).limit(1)):
        raise HTTPException(422, "source_bound_section_metadata_not_supported")
    if kind == "assets" and row.file_id is not None:
        file = session.get(RebuildPaperFile, row.file_id)
        if file is None or file.paper_id != paper_id:
            raise HTTPException(422, "metadata_source_paper_mismatch")
    # Hash actual column data, not a redacted projection: arbitrary provenance
    # key names must never conceal a concurrent change from CAS.
    snapshot = json.loads(json.dumps({column.key: getattr(row, column.key)
        for column in inspect(model).column_attrs}, default=safe_json, allow_nan=False))
    event_ids = sorted(str(value) for value in session.scalars(select(AuditLog.id).where(
        AuditLog.paper_id == paper_id, AuditLog.action == EVENT, AuditLog.source == VERSION,
        AuditLog.target_type == model.__tablename__, AuditLog.target_id == str(target_id))))
    revision = hashlib.sha256(json.dumps({"snapshot": snapshot, "events": event_ids},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return row, {"target_id": str(target_id), "paper_id": str(paper_id),
                 "expected_revision": revision, "snapshot": snapshot}


def correct_metadata(session, paper_id, kind, target_id, payload):
    row, before = metadata_state(session, paper_id, kind, target_id, lock=True)
    if before["expected_revision"] != payload.expected_revision:
        raise HTTPException(409, "metadata_revision_conflict")
    changes = payload.changes.model_dump(exclude_unset=True)
    if kind == "assets" and (len(changes) != 1 or not changes.keys() <= ASSET_METADATA_FIELDS):
        raise HTTPException(422, "exactly_one_allowed_asset_metadata_field_required")
    if kind == "assets" and "material_mapping" in changes:
        validate_material_mapping(changes["material_mapping"])
    identity = (row.provenance or {}).get("page_identity") if kind == "assets" else None
    if kind == "assets" and "page_numbers" in changes and isinstance(identity, dict) and identity.get("page_basis") == "ooxml":
        raise HTTPException(422, "ooxml_has_no_physical_page")
    for field, value in changes.items():
        setattr(row, field, value)
    session.flush()
    # Capture full persisted before/after images in an append-only event. This
    # also advances CAS for an A -> B -> A sequence, without touching provenance.
    _, after = metadata_state(session, paper_id, kind, target_id)
    context = session.info[VERSION]
    key = (MODELS[kind].__tablename__, str(target_id))
    if before["snapshot"] != after["snapshot"]:
        event_id = uuid4()
        session.add(AuditLog(id=event_id, paper_id=paper_id, action=EVENT, source=VERSION,
            target_type=key[0], target_id=key[1], payload={
                "request_id": context["request_id"], "task_id": context["task_id"],
                "actor": context["actor"], "operation": context["operation"],
                "reason": payload.reason, "expected_revision": payload.expected_revision,
                "before": before["snapshot"], "after": after["snapshot"],
                # Maintenance timestamps/version columns remain in the full
                # snapshots, but are not caller-requested metadata changes.
                "changed_fields": [f for f in changes if before["snapshot"][f] != after["snapshot"][f]]}))
        context["item_events"].append(str(event_id))
    session.flush()
    _, after = metadata_state(session, paper_id, kind, target_id)
    return {**after, "before": before["snapshot"], "reason": payload.reason,
            "scientific_acceptance": "not_implied"}
