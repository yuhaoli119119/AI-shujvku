"""Revision-bound bounded work-package views; full legacy response stays intact."""
from __future__ import annotations

from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from app.db.models import Paper, PaperSection, PaperTable, RebuildVisualAsset, AuditLog
from app.services.rebuild_workflow_service import list_rows, serialize_asset


def _inactive_binding_ids(value, inactive_file_ids):
    if isinstance(value, list):
        return set().union(*(_inactive_binding_ids(child, inactive_file_ids) for child in value))
    if not isinstance(value, dict):
        return set()
    affected = {str(value["file_id"])} if value.get("file_id") is not None and str(value["file_id"]) in inactive_file_ids else set()
    for child in value.values():
        if isinstance(child, (dict, list)):
            affected.update(_inactive_binding_ids(child, inactive_file_ids))
    return affected


def mark_scientific_file_bindings(value, inactive_file_ids):
    """Annotate retained file UUIDs without rebinding or changing science."""
    if isinstance(value, list):
        affected = set()
        for child in value:
            affected.update(mark_scientific_file_bindings(child, inactive_file_ids))
        return affected
    if not isinstance(value, dict):
        return set()
    affected = {str(value["file_id"])} if value.get("file_id") is not None and str(value["file_id"]) in inactive_file_ids else set()
    for key, child in list(value.items()):
        if isinstance(child, (dict, list)):
            if key in {"source_import_event", "provenance", "conflict", "before", "after"}:
                # Original immutable events and raw provenance remain byte-for-byte
                # JSON-equivalent. Mark only their public parent projection.
                affected.update(_inactive_binding_ids(child, inactive_file_ids))
            else:
                affected.update(mark_scientific_file_bindings(child, inactive_file_ids))
    if affected:
        value["scientific_review_required"] = True
        value["unresolved_file_ids"] = sorted(affected)
        value["scientific_file_status"] = "original_binding_requires_independent_review"
    return affected


def endpoint(paper_id, **params):
    return f"/api/rebuild/papers/{paper_id}/ai-work-package?" + urlencode({key: value for key, value in params.items() if value is not None})


def bounded_view(session, paper_id, *, view, snapshot, limit, offset):
    revision = snapshot["revision"]
    paper = session.get(Paper, paper_id)
    from app.services.rebuild_file_lifecycle import file_set
    lifecycle = file_set(session, paper_id)
    inactive_ids = {item["id"] for item in lifecycle["items"]} - set(lifecycle["active_file_ids"])
    result = {"view": view, "paper": {"id": str(paper_id), "paper_code": paper.paper_code, "title": paper.title},
              "output_revision": revision, "scientific_acceptance": "not_implied"}
    if view == "rows":
        page = list_rows(session, paper_id=paper_id, limit=limit, offset=offset)
        mark_scientific_file_bindings(page.get("items", []), inactive_ids)
        page["next_page"] = endpoint(paper_id, view="rows", rows_limit=limit, rows_offset=page["next_offset"], expected_revision=revision) if page["has_more"] else None
        return {**result, "rows": page}
    model = RebuildVisualAsset if view == "assets" else PaperTable if view == "legacy_tables" else PaperSection
    if view == "legacy_tables":
        from uuid import UUID
        ids = [UUID(item["id"]) for item in snapshot["output_manifest"] if item["kind"] == "paper_tables"]
        statement = select(model).where(model.id.in_(ids)).order_by(model.id)
    else:
        statement = select(model).where(model.paper_id == paper_id).order_by(model.id)
    if view == "sections":
        statement = statement.options(load_only(*[getattr(PaperSection, name) for name in (
            "id", "paper_id", "section_title", "section_type", "text", "page_start", "page_end",
            "section_level", "section_number", "parent_heading", "heading_path")]))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    records = list(session.scalars(statement.limit(limit).offset(offset)))
    # Section chunk pages return complete original text, not a scientific crop.
    # Source metadata remains in the authenticated sections endpoint, linked here.
    items = [serialize_asset(row) for row in records] if view == "assets" else [
        {"id": str(row.id), "paper_id": str(row.paper_id), "caption": row.caption, "page": row.page,
         "markdown_content": row.markdown_content, "format": "legacy_markdown",
         "source_rule": "Listed as a frozen dependency, not automatically verified. Consult per-record source_identity; unknown bodies must not be borrowed to complete another asset."} for row in records] if view == "legacy_tables" else [
        {"id": str(row.id), "paper_id": str(paper_id), "section_title": row.section_title,
         "section_type": row.section_type, "text": row.text, "page_start": row.page_start,
         "page_end": row.page_end, "section_level": row.section_level, "section_number": row.section_number,
         "parent_heading": row.parent_heading, "heading_path": row.heading_path,
         "provenance_endpoint": f"/api/rebuild/papers/{paper_id}/sections"} for row in records]
    if view == "legacy_tables":
        from app.services.rebuild_table_content import representation_complete
        for item in items:
            checks = [entry for entry in snapshot.get("table_completeness", []) if item["id"] == entry.get("legacy_id") or item["id"] in entry.get("legacy_record_ids", [])]
            verified = [entry for entry in checks if entry.get("file_id") and entry.get("source_sha256") and
                        entry.get("source_sha256") == entry.get("physical_sha256") and entry["complete"]]
            item["source_identity"] = {"status": "source_bound" if verified else "unverified",
                                       "bindings": [{key: entry.get(key) for key in ("file_id", "source_sha256", "physical_sha256", "physical_pages", "reason")} for entry in (verified or checks)],
                                       "use_for_completeness": bool(verified) and representation_complete({"format": "markdown", "text": item["markdown_content"] or ""}),
                                       "scientific_acceptance": "not_implied"}
    if view == "sections" and records:
        from app.services.rebuild_sections import EVENT, VERSION
        events = {event.target_id: event for event in session.scalars(select(AuditLog).where(
            AuditLog.paper_id == paper_id, AuditLog.action == EVENT, AuditLog.source == VERSION,
            AuditLog.target_id.in_([str(row.id) for row in records])).order_by(AuditLog.created_at, AuditLog.id))}
        for item in items:
            event = events.get(item["id"])
            if (event and isinstance(event.payload, dict) and isinstance(event.payload.get("section"), dict) and
                    event.payload["section"].get("text") == item["text"]):
                item["source_import_event"] = {**event.payload, "section": {key: value for key, value in event.payload["section"].items() if key != "text"}}
                item["source_event_projection"] = "The complete original section.text is returned once as this item's text. Reconstruct event.section.text from item.text before checking content_sha256; no scientific text is truncated."
            else:
                item["source_import_event"] = event.payload if event else None
            item["coverage_status"] = "stored_event_not_independent_acceptance" if event else "legacy_file_coverage_unknown"
            item["correction_rule"] = "Use provenance_endpoint for existing boundary corrections before any chapter write; pagination is original text plus its import event, not a replacement for correction history."
    mark_scientific_file_bindings(items, inactive_ids)
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {**result, "items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": next_offset is not None, "next_offset": next_offset,
            "next_page": endpoint(paper_id, view=view, rows_limit=limit, rows_offset=next_offset, expected_revision=revision) if next_offset is not None else None}


def compact_package(session, paper_id, snapshot):
    from app.db.models import RebuildDataRow
    from app.services.rebuild_quality import producer_contract
    from app.services.rebuild_table_content import table_write_contract
    from app.services.rebuild_sections import section_write_contract
    paper = session.get(Paper, paper_id)
    revision = snapshot["revision"]
    totals = {name: session.scalar(select(func.count()).select_from(model).where(model.paper_id == paper_id)) or 0
              for name, model in [("rows", RebuildDataRow), ("assets", RebuildVisualAsset), ("sections", PaperSection)]}
    totals["legacy_tables"] = sum(item["kind"] == "paper_tables" for item in snapshot["output_manifest"])
    from app.services.rebuild_file_lifecycle import file_set
    lifecycle = file_set(session, paper_id)
    active_ids = set(lifecycle["active_file_ids"])
    return {"view": "compact", "paper": {"id": str(paper_id), "paper_code": paper.paper_code, "title": paper.title},
            "output_revision": revision, "totals": totals,
            "files": [item for item in lifecycle["items"] if item["id"] in active_ids],
            "file_history": [item for item in lifecycle["items"] if item["id"] not in active_ids],
            "file_lifecycle": lifecycle,
            "pages": {name: endpoint(paper_id, view=name, rows_limit=50, rows_offset=0, expected_revision=revision) for name in totals},
            "delivery_package_endpoint": f"/api/rebuild/papers/{paper_id}/delivery-package",
            "quality_endpoint": f"/api/rebuild/papers/{paper_id}/quality?expected_revision={revision}",
            "staged_package_endpoint": f"/api/rebuild/papers/{paper_id}/staged-work-package?stage=source_scope&limit=25",
            "coverage_plan_endpoint": f"/api/rebuild/papers/{paper_id}/coverage-plan?limit=50",
            "templates_endpoint": "/api/rebuild/templates",
            "read_endpoints": {"text_pages": "/api/rebuild/files/{file_id}/text-pages?offset=0&limit=5",
                               "render_page": "/api/rebuild/files/{file_id}/render-page?page=1&dpi=120",
                               "asset_inventory": "/api/rebuild/files/{file_id}/asset-inventory"},
            "table_write_contract": table_write_contract(), "producer_contract": producer_contract(),
            "section_write_contract": section_write_contract(paper_id),
            "rule": "Read bounded pages through has_more=false, keep the same output_revision; 409 means restart the read, never merge revisions. Full view remains available. No scientific counts imply PDF completeness.",
            "scientific_acceptance": "not_implied"}
