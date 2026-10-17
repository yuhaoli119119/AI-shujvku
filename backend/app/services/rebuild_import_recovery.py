"""Import receipts and conservative, non-executable recovery suggestions.

Never change scientific payloads here. A suggestion is not an authorization,
CAS token, audit receipt, or a guarantee that the next import will succeed.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select
from app.db.models import RebuildDataRow, RebuildDataValue
from app.services.rebuild_workflow_service import IDENTITY_FIELDS, _stored_identity, IMPORT_META, RebuildWorkflowError


def import_summary(session, results):
    cells = [cell for row in results for cell in row.get("values", [])]
    properties = [prop for row in results for prop in row.get("properties", [])]
    persisted = sum(bool(row.get("persisted")) for row in results)
    row_ids = {UUID(row["row_id"]) for row in results if row.get("persisted")}
    rows_with_values = set(session.scalars(select(RebuildDataValue.row_id).where(
        RebuildDataValue.row_id.in_(row_ids)).distinct())) if row_ids else set()
    complete = all(row["complete"] for row in results)
    outcome = "all_success" if complete else "partial_success" if persisted else "all_rejected"
    return {"outcome": outcome,
            "message": {"all_success": "全部成功（含重复项）", "partial_success": "部分成功，仍有需处理项",
                        "all_rejected": "全部拒绝，未持久化数据行"}[outcome],
            "rows": {"submitted": len(results), "persisted": persisted, "not_persisted": len(results) - persisted,
                     "empty_persisted": sum(bool(row.get("persisted")) and UUID(row["row_id"]) not in rows_with_values
                                            for row in results),
                     "pending": sum(row.get("identity_status") == "pending" for row in results)},
            "values": {"submitted": len(cells),
                       "written": sum(cell.get("write_outcome", cell["outcome"]) == "written" for cell in cells),
                       "duplicate": sum(cell.get("write_outcome", cell["outcome"]) == "duplicate" for cell in cells),
                       "persisted": sum(bool(cell.get("persisted")) for cell in cells),
                       "not_persisted": sum(not cell.get("persisted") for cell in cells),
                       "pending": sum(cell["outcome"] == "pending" for cell in cells),
                       "conflict": sum(cell.get("write_outcome", cell["outcome"]) == "conflict" for cell in cells)},
            "properties": {name: sum(prop["outcome"] == name for prop in properties)
                           for name in ("written", "duplicate", "conflict")},
            # Fields inside a rejected row were not individually executed; do
            # not invent per-cell failures/counts from their submitted shape.
            "value_count_scope": "executed_rows_only; rejected rows are located in results/errors"}


def recovery_advice(session, payload, results):
    items = []
    for result in results:
        if result["complete"]:
            continue
        index = result["row_index"]
        item = {"row_index": index, "row_key": result.get("row_key"), "row_id": result.get("row_id"),
                "status": "blocked", "steps": ["Read back this paper's canonical row, values, sources and conflicts before editing.",
                "Preserve the full identity and condition; correct only evidence-backed failed items, preflight, then submit with a NEW request_id."]}
        row = session.get(RebuildDataRow, UUID(result["row_id"])) if result.get("row_id") else None
        raw = payload.rows[index]
        details = result.get("values", [])
        failed = [cell for cell in details if cell["outcome"] == "rejected" and cell.get("persisted") is False]
        fields = [cell.get("field") for cell in failed]
        stored = list(session.scalars(select(RebuildDataValue).where(RebuildDataValue.row_id == row.id))) if row else []
        if row is None:
            reason = "row_identity_unresolved:fix_original_row_and_read_back_before_resubmitting"
        elif (row.properties or {}).get(IMPORT_META, {}).get("pending_identity_fields"):
            reason = "identity_pending:read_back_and_supply_all_pending_dimensions_using_canonical_key"
        elif any((value.conflict or {}).get("status") in {"unit_conflict", "value_conflict"} for value in stored) or (row.properties or {}).get(IMPORT_META, {}).get("property_conflicts"):
            reason = "conflict_requires_adjudication:no_automatic_recovery"
        elif not failed or any(not isinstance(field, str) or not field for field in fields) or len(set(fields)) != len(fields):
            reason = "failed_items_not_unambiguous:read_back_and_review_original_request"
        elif any(value.field_name in fields for value in stored):
            reason = "failed_field_now_exists:read_back_before_any_new_candidate"
        elif any(cell.get("field") in fields and cell["outcome"] != "rejected" for cell in details):
            reason = "same_field_has_other_result:read_back_before_recovery"
        else:
            # Canonical current condition retains dimensions originally declared
            # in values/properties. Do not resubmit successful dimension cells.
            try:
                identity = _stored_identity(session, row, values=stored)
            except RebuildWorkflowError:
                item["reason"] = "stored_identity_unresolved:read_back_before_recovery"
                items.append(item)
                continue
            template = {name: getattr(row, name) for name in IDENTITY_FIELDS}
            template.update(row_key=row.row_key, condition=deepcopy(identity["condition"]),
                            values=[deepcopy(raw["values"][cell["field_index"]]) for cell in failed])
            item.update(status="needs_explicit_correction", field_indices=[cell["field_index"] for cell in failed],
                        row_template=template)
            reason = "template_contains_original_rejected_values:correct_from_evidence_and_recheck_current_state"
        item["reason"] = reason
        items.append(item)
    return {"kind": "advisory_only", "auto_retry_allowed": False,
            "unknown_result": "Retry the EXACT original body with the SAME request_id; a replay is the original receipt, not current state. Never mint a new ID for an unknown write result.",
            "known_failure": "Read back current state; explicitly correct failed items, preserve canonical identity, preflight, then use a NEW request_id. Never replay conflicts as overwrites.",
            "items": items}


def simulated_result(result):
    output = deepcopy(result)
    output.pop("recovery", None)
    for item in output["results"] + output["errors"]:
        item.pop("row_id", None)
        item.pop("value_id", None)
        item.pop("audit_event_ids", None)
        if "persisted" in item:
            item["would_persist"] = item.pop("persisted")
        for cell in item.get("values", []):
            cell.pop("value_id", None)
            if "persisted" in cell:
                cell["would_persist"] = cell.pop("persisted")
    for group in ("rows", "values"):
        summary = output["summary"][group]
        for before, after in (("persisted", "would_persist"), ("not_persisted", "would_not_persist"),
                              ("empty_persisted", "would_persist_empty")):
            if before in summary:
                summary[after] = summary.pop(before)
    output.update(mode="preflight", simulation=True, persisted=False,
                  notice="Rolled back simulation at this moment only; no receipt or request_id reservation. IDs are omitted. State and source files may change before import; formal import revalidates. No recovery template is valid until the actual receipt and current state are read back.")
    return output
