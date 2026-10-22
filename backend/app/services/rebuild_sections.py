"""Append-only chapters for the mature reader, with immutable source events.

PaperSection keeps original text/headings. AuditLog stores a versioned, complete
import event rather than overloading heading fields or changing the old schema.
Both rows and the request receipt commit together; callers own the transaction.
This checks file identity and physical pagination, not scientific correctness.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from uuid import UUID, uuid5, NAMESPACE_URL

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.config import Settings
from app.db.models import AuditLog, Paper, PaperSection, RebuildPaperFile
from app.schemas.rebuild import (RebuildSectionsImportRequest, RebuildSectionCorrectionInput,
                                 RebuildSectionCorrectionsRequest, RebuildSectionOrderRequest)
from app.services.rebuild_workflow_service import RebuildWorkflowError, get_file

EVENT = "section_imported"
RECEIPT = "section_import_request"
VERSION = "rebuild_sections_v1"
CORRECTION_VERSION = "rebuild_section_corrections_v1"
CORRECTION_EVENT = "section_boundary_corrected"
CORRECTION_RECEIPT = "section_correction_request"
SECTION_FIELDS = ("section_title", "section_type", "text", "page_start", "page_end",
                  "section_level", "section_number", "parent_heading", "heading_path")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _events(session, paper_id):
    return list(session.scalars(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == EVENT,
        AuditLog.source == VERSION, AuditLog.target_type == "paper_section"
    ).order_by(AuditLog.created_at, AuditLog.id)))


def _correction_events(session, paper_id):
    return list(session.scalars(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == CORRECTION_EVENT,
        AuditLog.source == CORRECTION_VERSION, AuditLog.target_type == "paper_section"
    ).order_by(AuditLog.created_at, AuditLog.id)))


def _checked_snapshot(row, event):
    """A declared digest binds both the original audit and the stored chapter."""
    if row is None or event is None or not isinstance(event.payload, dict):
        raise RebuildWorkflowError("section_correction_provenance_required")
    snapshot = event.payload.get("section")
    if not isinstance(snapshot, dict) or event.payload.get("content_sha256") != _digest(snapshot):
        raise RebuildWorkflowError("section_correction_content_mismatch")
    if any(getattr(row, field) != snapshot.get(field) for field in SECTION_FIELDS):
        raise RebuildWorkflowError("section_correction_content_mismatch")
    return snapshot


def _check_correction_targets(declaration, rows, imports, files):
    file = files.get(str(declaration.file_id))
    if file is None or file.role not in {"main", "si"}:
        raise RebuildWorkflowError("section_correction_file_paper_mismatch")
    if file.sha256 != declaration.source_sha256:
        raise RebuildWorkflowError("source_sha256_mismatch")
    snapshots = []
    for target in [declaration.old_section, *declaration.new_sections]:
        key = str(target.section_id)
        snapshot = _checked_snapshot(rows.get(key), imports.get(key))
        if (snapshot.get("file_id") != str(file.id) or
                snapshot.get("source_sha256") != declaration.source_sha256):
            raise RebuildWorkflowError("section_correction_file_paper_mismatch")
        if target.content_sha256 != imports[key].payload["content_sha256"]:
            raise RebuildWorkflowError("section_correction_content_mismatch")
        snapshots.append(snapshot)
    basis = {(s.get("page_basis"), imports[str(t.section_id)].payload.get("reading_pdf_sha256"))
             for s, t in zip(snapshots, [declaration.old_section, *declaration.new_sections])}
    if len(basis) != 1:
        raise RebuildWorkflowError("section_correction_reading_source_mismatch")
    # Every reason locator must be in a physical page shared by the old boundary
    # and at least one replacement. Mechanical identity is not scientific review.
    old, *new = snapshots
    if any(s["page_start"] > old["page_end"] or s["page_end"] < old["page_start"] for s in new):
        raise RebuildWorkflowError("section_correction_source_relation_invalid")
    if any(not (old["page_start"] <= s.page_number <= old["page_end"] and
                   any(n["page_start"] <= s.page_number <= n["page_end"] for n in new))
           for s in declaration.sources):
        raise RebuildWorkflowError("section_correction_source_relation_invalid")
    return file, next(iter(basis))


def _valid_corrections(session, paper_id, rows, imports, files):
    """Fail closed for malformed/stale declarations; never interpret conflicts."""
    valid = []
    for event in _correction_events(session, paper_id):
        try:
            data = event.payload
            if not isinstance(data, dict) or data.get("version") != CORRECTION_VERSION:
                continue
            declaration = RebuildSectionCorrectionInput.model_validate(data["declaration"])
            if event.target_id != str(declaration.old_section.section_id):
                continue
            if data.get("declaration_sha256") != _digest(declaration.model_dump(mode="json")):
                continue
            _, (_, reading_hash) = _check_correction_targets(declaration, rows, imports, files)
            if data.get("reading_pdf_sha256") != reading_hash:
                continue
            valid.append((event, declaration))
        except (ValidationError, RebuildWorkflowError, KeyError, TypeError, ValueError):
            continue
    return valid


def list_sections(session: Session, paper_id: UUID, *, preserve_missing_file: bool = False) -> list[dict]:
    """Batched reads; only explicit, source-bound declarations mark history.

    Guide projections retain unresolved audit file references to report errors;
    other readers keep their existing legacy/coverage behavior by default.
    """
    rows = list(session.scalars(select(PaperSection).where(
        PaperSection.paper_id == paper_id).order_by(PaperSection.page_start, PaperSection.section_title, PaperSection.text, PaperSection.id)))
    events = {event.target_id: event for event in _events(session, paper_id)}
    files = {str(file.id): file for file in session.scalars(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id == paper_id))}
    items = []
    for row in rows:
        item = {"id": str(row.id), "paper_id": str(paper_id),
                **{field: getattr(row, field) for field in SECTION_FIELDS}}
        item["heading_path"] = item["heading_path"] or []
        item.update(section_key=None, explanation_zh=None, provenance=None,
                    conflicts_with_section_ids=[], scientific_acceptance="not_implied",
                    display_status="current", correction_history=[])
        event = events.get(str(row.id))
        if event and isinstance(event.payload, dict):
            data = event.payload
            snapshot = data.get("section", {})
            file = files.get(snapshot.get("file_id"))
            if file or (preserve_missing_file and snapshot.get("file_id")):
                item.update(section_key=snapshot.get("section_key"),
                            explanation_zh=snapshot.get("explanation_zh"),
                            conflicts_with_section_ids=snapshot.get("conflicts_with_section_ids", []),
                            provenance={
                                "version": VERSION, "audit_id": str(event.id),
                                "section_id": str(row.id), "file_id": snapshot.get("file_id"),
                                "role": file.role if file else None, "original_filename": file.original_filename if file else None,
                                "source_sha256": snapshot.get("source_sha256"),
                                "page_basis": snapshot.get("page_basis"),
                                "reading_pdf_sha256": data.get("reading_pdf_sha256"),
                                "sources": snapshot.get("sources", []),
                                "request_id": data.get("request_id"), "actor": data.get("actor"),
                                "content_sha256": data.get("content_sha256"),
                                "created_at": event.created_at.isoformat(),
                                "preview_url": f"/api/rebuild/files/{file.id}/preview" if file else None,
                                "download_url": f"/api/rebuild/files/{file.id}/download" if file else None,
                            })
        items.append(item)
    by_id = {item["id"]: item for item in items}
    row_map = {str(row.id): row for row in rows}
    for event, declaration in _valid_corrections(session, paper_id, row_map, events, files):
        old_id = str(declaration.old_section.section_id)
        new_ids = [str(target.section_id) for target in declaration.new_sections]
        relation = {**declaration.model_dump(mode="json"), "version": CORRECTION_VERSION,
                    "audit_id": str(event.id), "actor": event.payload.get("actor"),
                    "request_id": event.payload.get("request_id"),
                    "declaration_sha256": event.payload["declaration_sha256"],
                    "reading_pdf_sha256": event.payload["reading_pdf_sha256"],
                    "created_at": event.created_at.isoformat()}
        by_id[old_id]["display_status"] = "extraction_boundary_history"
        for key in [old_id, *new_ids]:
            by_id[key]["correction_history"].append(relation)
    return _ordered_sections(session, paper_id, items, files)


def section_coverage(sections: list[dict], files: list[dict]) -> dict:
    known = [section for section in sections if section.get("provenance")]
    return {
        "rule": "Coverage lists stored chapters, not scientific acceptance or full-page completeness. Legacy chapters without provenance have unknown file coverage. Read existing text before adding a chapter; a new SI does not replace main-file chapters.",
        "legacy_section_ids_without_file": [s["id"] for s in sections if not s.get("provenance")],
        "files": [{"file_id": file["id"], "role": file["role"], "source_sha256": file["sha256"],
                   "sections": [{"section_id": s["id"], "section_key": s["section_key"],
                                 "section_title": s["section_title"], "page_start": s["page_start"],
                                 "page_end": s["page_end"], "page_basis": s["provenance"]["page_basis"],
                                 "source_sha256": s["provenance"]["source_sha256"]}
                                for s in known if s["provenance"]["file_id"] == file["id"]]}
                  for file in files],
    }


def section_write_contract(paper_id):
    return {
        "version": VERSION, "method": "POST",
        "url": f"/api/rebuild/papers/{paper_id}/sections/import",
        "authentication": "workbench session + X-CSRF-Token + same-origin",
        "body": {"request_id": "stable unique request id (max 64)", "sections": [{
            "section_key": "stable file-local chapter identity (max 128)",
            "file_id": "registered same-paper main/SI UUID", "source_sha256": "original file SHA256",
            "page_basis": "source_pdf or derived_pdf", "reading_pdf_sha256": "required for derived_pdf",
            "section_title": "original heading", "text": "complete original chapter text",
            "explanation_zh": "evidence-based Chinese explanation, separate from original text",
            "page_start": "1-based physical PDF page", "page_end": "inclusive physical PDF page",
            "sources": [{"page_number": "each physical page in the range", "locator": "specific paragraph/heading location", "quote": "original evidence quote"}],
            "conflicts_with_section_ids": "optional same-paper chapter IDs; append with a new key, retain both",
        }]},
        "optional_heading_fields": ["section_type", "section_level", "section_number", "parent_heading", "heading_path"],
        "retry": "Persist the exact JSON before sending. Identical request_id/body replays with zero writes. Identical file_id/section_key/content under another request_id adds no chapter. Reusing a request_id or chapter key with changed input is HTTP 409; read existing coverage, retain history, and use a new explicit key plus conflicts_with_section_ids for an evidence-backed alternative.",
        "source_rule": "The registered file must belong to this paper. Original bytes, claimed source_sha256, registered hash and size must agree. PDF pages must physically exist; every covered page needs a nonempty original quote and locator. DOCX page numbers require the existing derived reading PDF and its exact SHA256; no conversion is triggered by this API. Source quotes/full-text completeness and Chinese interpretation require independent DS verification; the code never certifies science. Missing/unreported content stays unwritten.",
        "preservation": "Append only to paper_sections and audit_logs in one transaction. No replacement, deletion, scientific-value changes or automatic parsing/status promotion. Legacy chapters keep unknown provenance; do not infer a file identity.",
        "boundary_correction": section_correction_contract(paper_id),
        "reading_order": {
            "url": f"/api/rebuild/papers/{paper_id}/sections/order", "method": "POST",
            "body": {"request_id": "stable UUID", "expected_revision": "fresh section_order_revision",
                "reason": "source-grounded reason", "sections": [{"section_id": "current source-bound UUID",
                    "content_sha256": "provenance.content_sha256", "sources": [{"page_number": "physical page within chapter",
                        "locator": "exact original heading position", "quote": "original heading quote"}]}]},
            "rules": "Complete ordered list of source-bound current chapters; contiguous file/page-basis groups. Legacy unbound chapters stay unknown; history stays separate. Same actor/body replay is read-only; changed ID body or stale order revision is 409. Append audit only; never rewrite section bytes or hierarchy. Source/section changes invalidate old order. No scientific acceptance implied."},
    }


def section_correction_contract(paper_id):
    return {
        "version": CORRECTION_VERSION, "method": "POST",
        "url": f"/api/rebuild/papers/{paper_id}/sections/corrections",
        "authentication": "workbench session + X-CSRF-Token + same-origin",
        "body": {"request_id": "stable request identity; persist exact JSON", "corrections": [{
            "kind": "extraction_boundary", "file_id": "same-paper registered main/SI UUID",
            "source_sha256": "original file SHA256",
            "old_section": {"section_id": "existing source-bound chapter UUID",
                            "content_sha256": "provenance.content_sha256"},
            "new_sections": [{"section_id": "existing corrected chapter UUID",
                              "content_sha256": "provenance.content_sha256"}],
            "reason": "source-grounded extraction boundary error explanation",
            "sources": [{"page_number": "physical page shared by old/new",
                         "locator": "original heading/paragraph location", "quote": "original quote"}],
        }]},
        "retry": "Identical request/body: zero events. Changed request body or changed prior declaration for an old chapter: HTTP 409. New request with identical declaration: zero correction events, one receipt.",
        "preservation": "Only append audit events/receipt. Never mutate chapters/import audits/files/scientific data. Generic conflicts are not corrections; no automatic priority or scientific acceptance. Unproven legacy file identity is rejected.",
        "coordination": "Owner must hold the same-paper serial write claim, back up first, and arrange a separate DS metadata task. Code deployment alone does not mark any chapter historical.",
    }


def declare_section_corrections(session: Session, paper_id: UUID,
                                payload: RebuildSectionCorrectionsRequest,
                                settings: Settings, *, actor: str) -> dict:
    """Append metadata atomically. The authenticated caller owns the transaction."""
    if session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update()) is None:
        raise RebuildWorkflowError("paper_not_found")
    request_hash = _digest(payload.model_dump(mode="json"))
    receipt = session.scalar(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == CORRECTION_RECEIPT,
        AuditLog.source == CORRECTION_VERSION, AuditLog.target_type == "paper_sections",
        AuditLog.target_id == payload.request_id))
    if receipt:
        if receipt.payload["request_sha256"] != request_hash:
            raise RebuildWorkflowError("section_correction_request_id_reused")
        return {**receipt.payload["result"], "created": 0, "replayed": True}

    rows = {str(row.id): row for row in session.scalars(select(PaperSection).where(
        PaperSection.paper_id == paper_id))}
    imports = {event.target_id: event for event in _events(session, paper_id)}
    files = {str(file.id): file for file in session.scalars(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id == paper_id))}
    previous = {event.target_id: event for event in _correction_events(session, paper_id)}
    old_ids = set(previous) | {str(c.old_section.section_id) for c in payload.corrections}
    planned = []
    checks = {}
    for declaration in payload.corrections:
        if any(str(t.section_id) in old_ids for t in declaration.new_sections):
            raise RebuildWorkflowError("section_correction_target_is_history")
        file, (basis, expected_reading_hash) = _check_correction_targets(declaration, rows, imports, files)
        if file.id not in checks:
            from app.services.rebuild_file_preview import _checked_source, resolve_reading_pdf
            path, source_format = _checked_source(file, settings)
            if path.stat().st_size != file.file_size:
                raise RebuildWorkflowError("section_source_size_mismatch")
            expected_basis = "source_pdf" if source_format == "pdf" else "derived_pdf"
            reading_path = path if source_format == "pdf" else resolve_reading_pdf(file, settings)
            reading_bytes = reading_path.read_bytes()
            reading_hash = hashlib.sha256(reading_bytes).hexdigest()
            if source_format == "pdf" and reading_hash != file.sha256:
                raise RebuildWorkflowError("source_sha256_mismatch")
            try:
                with fitz.open(stream=reading_bytes, filetype="pdf") as doc:
                    if doc.needs_pass or doc.page_count < 1:
                        raise RebuildWorkflowError("section_reading_pdf_invalid")
                    checks[file.id] = (expected_basis, reading_hash, doc.page_count)
            except (fitz.FileDataError, fitz.EmptyFileError, OSError) as exc:
                raise RebuildWorkflowError("section_reading_pdf_invalid") from exc
        actual_basis, reading_hash, pages = checks[file.id]
        if (basis, expected_reading_hash) != (actual_basis, reading_hash):
            raise RebuildWorkflowError("section_correction_reading_source_mismatch")
        if any(rows[str(t.section_id)].page_end > pages
               for t in [declaration.old_section, *declaration.new_sections]):
            raise RebuildWorkflowError("section_physical_page_out_of_range")
        snapshot = declaration.model_dump(mode="json")
        digest = _digest(snapshot)
        old_id = str(declaration.old_section.section_id)
        prior = previous.get(old_id)
        if prior and (not isinstance(prior.payload, dict) or
                      prior.payload.get("declaration_sha256") != digest):
            raise RebuildWorkflowError("section_correction_relation_conflict")
        planned.append((old_id, snapshot, digest, reading_hash, prior))

    items = []
    for old_id, snapshot, digest, reading_hash, prior in planned:
        if prior is None:
            event_id = uuid5(NAMESPACE_URL, f"literature-ai:{CORRECTION_VERSION}:{paper_id}:{old_id}")
            session.add(AuditLog(id=event_id, paper_id=paper_id, action=CORRECTION_EVENT,
                                source=CORRECTION_VERSION, target_type="paper_section",
                                target_id=old_id, payload={"version": CORRECTION_VERSION,
                                    "declaration": snapshot, "declaration_sha256": digest,
                                    "reading_pdf_sha256": reading_hash, "actor": actor,
                                    "request_id": payload.request_id, "request_sha256": request_hash}))
        else:
            event_id = prior.id
        items.append({"old_section_id": old_id, "audit_id": str(event_id),
                      "new_section_ids": [t["section_id"] for t in snapshot["new_sections"]],
                      "status": "existing" if prior else "created"})
    result = {"paper_id": str(paper_id), "request_id": payload.request_id,
              "created": sum(prior is None for *_, prior in planned), "replayed": False,
              "items": items, "scientific_acceptance": "not_implied"}
    session.add(AuditLog(paper_id=paper_id, action=CORRECTION_RECEIPT,
                        source=CORRECTION_VERSION, target_type="paper_sections",
                        target_id=payload.request_id, payload={"version": CORRECTION_VERSION,
                            "actor": actor, "request_sha256": request_hash, "result": result}))
    session.flush()
    return result


def import_sections(session: Session, paper_id: UUID, payload: RebuildSectionsImportRequest,
                    settings: Settings, *, actor: str) -> dict:
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise RebuildWorkflowError("paper_not_found")
    request_body = payload.model_dump(mode="json")
    request_hash = _digest(request_body)
    receipt = session.scalar(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == RECEIPT,
        AuditLog.source == VERSION, AuditLog.target_type == "paper_sections",
        AuditLog.target_id == payload.request_id))
    if receipt:
        if receipt.payload["request_sha256"] != request_hash:
            raise RebuildWorkflowError("section_request_id_reused")
        return {**receipt.payload["result"], "created": 0, "replayed": True}

    existing = {event.target_id: event for event in _events(session, paper_id)}
    current_rows = {str(row.id): row for row in session.scalars(select(PaperSection).where(
        PaperSection.paper_id == paper_id))}
    checks = {}
    created = 0
    items = []
    for section, snapshot in zip(payload.sections, request_body["sections"]):
        file = get_file(session, section.file_id)
        if file.paper_id != paper_id or file.role not in {"main", "si"}:
            raise RebuildWorkflowError("section_file_paper_mismatch")
        if section.source_sha256 != file.sha256:
            raise RebuildWorkflowError("source_sha256_mismatch")
        key = (file.id, section.page_basis, section.reading_pdf_sha256)
        if key not in checks:
            from app.services.rebuild_file_preview import _checked_source, resolve_reading_pdf
            source_path, source_format = _checked_source(file, settings)
            if source_path.stat().st_size != file.file_size:
                raise RebuildWorkflowError("section_source_size_mismatch")
            expected_basis = "source_pdf" if source_format == "pdf" else "derived_pdf"
            if section.page_basis != expected_basis:
                raise RebuildWorkflowError("section_page_basis_mismatch")
            reading_path = source_path if source_format == "pdf" else resolve_reading_pdf(file, settings)
            # Hash and paginate the same bytes, even if a path changes while
            # this request is validating it. Never write a derived preview.
            reading_bytes = reading_path.read_bytes()
            reading_hash = hashlib.sha256(reading_bytes).hexdigest()
            if source_format == "pdf" and reading_hash != file.sha256:
                raise RebuildWorkflowError("source_sha256_mismatch")
            if section.reading_pdf_sha256 and section.reading_pdf_sha256 != reading_hash:
                raise RebuildWorkflowError("section_reading_pdf_sha256_mismatch")
            try:
                with fitz.open(stream=reading_bytes, filetype="pdf") as document:
                    if document.needs_pass or document.page_count < 1:
                        raise RebuildWorkflowError("section_reading_pdf_invalid")
                    checks[key] = (document.page_count, reading_hash)
            except (fitz.FileDataError, fitz.EmptyFileError, OSError) as exc:
                raise RebuildWorkflowError("section_reading_pdf_invalid") from exc
        page_count, reading_hash = checks[key]
        if section.page_end > page_count:
            raise RebuildWorkflowError("section_physical_page_out_of_range")
        if any(str(other_id) not in current_rows for other_id in section.conflicts_with_section_ids):
            raise RebuildWorkflowError("section_conflict_target_not_same_paper")
        section_id = uuid5(NAMESPACE_URL, f"literature-ai:{paper_id}:{file.id}:{section.section_key}")
        content_hash = _digest(snapshot)
        event = existing.get(str(section_id))
        if event:
            row = current_rows.get(str(section_id))
            if event.payload["content_sha256"] != content_hash:
                raise RebuildWorkflowError("section_key_conflict")
            if row is None or any(getattr(row, field) != snapshot[field] for field in SECTION_FIELDS):
                raise RebuildWorkflowError("section_stored_content_conflict")
            status = "existing"
        else:
            if str(section_id) in current_rows:
                raise RebuildWorkflowError("section_id_collision")
            row = PaperSection(id=section_id, paper_id=paper_id,
                               **{field: snapshot[field] for field in SECTION_FIELDS})
            session.add(row)
            session.flush()
            session.add(AuditLog(paper_id=paper_id, action=EVENT, source=VERSION,
                                 target_type="paper_section", target_id=str(section_id), payload={
                                     "version": VERSION, "section_id": str(section_id),
                                     "section": snapshot, "reading_pdf_sha256": reading_hash,
                                     "actor": actor, "request_id": payload.request_id,
                                     "request_sha256": request_hash, "content_sha256": content_hash,
                                 }))
            current_rows[str(section_id)] = row
            created += 1
            status = "created"
        items.append({"section_id": str(section_id), "file_id": str(file.id),
                      "section_key": section.section_key, "status": status,
                      "conflicts_with_section_ids": snapshot["conflicts_with_section_ids"]})
    result = {"paper_id": str(paper_id), "request_id": payload.request_id,
              "created": created, "replayed": False, "items": items,
              "scientific_acceptance": "not_implied"}
    session.add(AuditLog(paper_id=paper_id, action=RECEIPT, source=VERSION,
                         target_type="paper_sections", target_id=payload.request_id,
                         payload={"version": VERSION, "actor": actor,
                                  "request_sha256": request_hash, "result": result}))
    session.flush()
    return result


ORDER_VERSION = "rebuild_section_order_v1"
ORDER_EVENT = "section_order_declared"


def section_content_revision(items):
    # Source-bound chapter bytes and correction status; independent of display order.
    return _digest(sorted([{'id': s['id'], 'status': s['display_status'],
                            'fields': {f: s[f] for f in SECTION_FIELDS},
                            'provenance': s.get('provenance')} for s in items], key=lambda s: s['id']))


def _group(s):
    p = s.get('provenance') or {}
    return (p.get('file_id'), p.get('page_basis'), p.get('reading_pdf_sha256'))


def _number(s):
    # Only explicit decimal heading numbers are comparable. Roman/letter titles
    # remain intact and can be ordered through the evidence-bound declaration.
    n = s.get('section_number') or ''
    if not re.fullmatch(r'\d+(?:\.\d+)*\.?', n):
        m = re.match(r'^(\d+(?:\.\d+)*)[.)]?\s+', s['section_title'] or '')
        n = m.group(1) if m else ''
    return tuple(map(int, n.rstrip('.').split('.'))) if n else None


def _heading_offsets(group, file):
    """Use unique exact line headings in verified reading bytes, or report unknown.

    Never compare offsets from different files/page bases. No fuzzy semantic
    heading classification or automatic invented numbering is used.
    """
    from app.config import get_settings
    from app.services.rebuild_file_preview import resolve_reading_pdf
    if file is None:
        return {}
    try:
        path = resolve_reading_pdf(file, get_settings())
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if any((s.get('provenance') or {}).get('reading_pdf_sha256') != digest for s in group):
            return {}
        with fitz.open(stream=content, filetype='pdf') as doc:
            texts = {}
            result = {}
            for s in group:
                page = s['page_start']
                if not isinstance(page, int) or not 1 <= page <= len(doc):
                    continue
                if page not in texts:
                    texts[page] = doc[page-1].get_text('text')
                # Whitespace and line wraps are typographic only. Require a
                # line boundary to avoid matching a citation in a paragraph.
                title = s.get('section_title') or ''
                if not title.strip():
                    continue
                words = [r'\s*'.join(re.escape(c) for c in word) for word in title.split()]
                pattern = r'(?m)^\s*' + r'\s+'.join(words) + r'\s*(?=\n|$)'
                hits = list(re.finditer(pattern, texts[page], flags=re.I))
                if len(hits) == 1:
                    result[s['id']] = hits[0].start()
            return result
    except (RebuildWorkflowError, OSError, ValueError, RuntimeError):
        return {}


def _ordered_sections(session, paper_id, items, files):
    groups = defaultdict(list)
    for item in items:
        groups[_group(item)].append(item)
    result = []
    # File groups are explicit in the response. Filename/file UUID determines
    # group presentation only, never asserted as cross-document scientific order.
    for key, group in sorted(groups.items(), key=lambda kv: (
            0 if (files.get(kv[0][0]) and files[kv[0][0]].role == 'main') else 1,
            (files[kv[0][0]].original_filename if kv[0][0] in files else ''), str(kv[0]))):
        offsets = _heading_offsets(group, files.get(key[0])) if key[0] else {}
        pages = defaultdict(list)
        for s in group:
            pages[s['page_start']].append(s)
        for page, same_page in sorted(pages.items(), key=lambda kv: kv[0] or 0):
            current = [s for s in same_page if s['display_status'] == 'current']
            if key[0] in files and current and all(_number(s) is not None for s in current):
                current.sort(key=lambda s: (_number(s), s['section_title'], s['text']))
                basis = 'original_heading_number'
            elif current and all(s['id'] in offsets for s in current) and len({offsets[s['id']] for s in current}) == len(current):
                current.sort(key=lambda s: offsets[s['id']])
                basis = 'verified_reading_heading_offset'
            else:
                # Stable content fallback, explicitly unknown, with no UUID tie-break.
                current.sort(key=lambda s: (s['section_title'] or '', s['text'] or ''))
                basis = 'page_then_content_order_unverified'
            for s in current:
                s['reading_order'] = {'basis': basis, 'file_group': list(key), 'position': None}
                result.append(s)
        history = [s for s in group if s['display_status'] != 'current']
        history.sort(key=lambda s: (s['page_start'] or 0, s['section_title'] or '', s['text'] or ''))
        for s in history:
            s['reading_order'] = {'basis': 'history_separate', 'file_group': list(key), 'position': None}
        result.extend(history)
    revision = section_content_revision(items)
    events = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == ORDER_EVENT, AuditLog.source == ORDER_VERSION).order_by(AuditLog.created_at, AuditLog.id)))
    applied_id = None
    for event in reversed(events):
        data = event.payload
        if not isinstance(data, dict) or data.get('revision') != revision:
            continue
        declaration = data.get('declaration', {})
        if data.get('declaration_sha256') != _digest(declaration):
            continue
        try:
            payload = RebuildSectionOrderRequest.model_validate(declaration)
            ids = [str(e.section_id) for e in payload.sections]
            current = {s['id']: s for s in items if s['display_status'] == 'current' and s.get('provenance')}
            if set(ids) != set(current):
                continue
            rank = {sid: i for i, sid in enumerate(ids)}
            for s in current.values():
                s['reading_order'] = {'basis': 'source_bound_declaration', 'position': rank[s['id']],
                    'file_group': list(_group(s)), 'audit_id': str(event.id),
                    'actor': data.get('actor'), 'reason': payload.reason,
                    'sources': next(e.sources for e in payload.sections if str(e.section_id) == s['id'])}
                s['reading_order']['sources'] = [e.model_dump(mode='json') for e in s['reading_order']['sources']]
            applied_id = str(event.id)
            result.sort(key=lambda s: (s['display_status'] != 'current', rank.get(s['id'], len(rank))))
            break
        except (ValueError, TypeError, KeyError):
            continue
    for s in result:
        s['section_order_revision'] = _digest({'content_revision': revision, 'order_audit_id': applied_id})
    return result


def declare_section_order(session, paper_id, payload: RebuildSectionOrderRequest, *, actor: str):
    if not actor or not actor.strip():
        raise RebuildWorkflowError('section_order_actor_required')
    if session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update()) is None:
        raise RebuildWorkflowError('paper_not_found')
    declaration = payload.model_dump(mode='json')
    fingerprint = _digest(declaration)
    receipt = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == ORDER_EVENT, AuditLog.source == ORDER_VERSION,
        AuditLog.target_type == 'paper_sections', AuditLog.target_id == str(payload.request_id)))
    if receipt:
        if receipt.payload.get('declaration_sha256') != fingerprint or receipt.payload.get('actor') != actor:
            raise RebuildWorkflowError('section_order_request_id_reused')
        return {**receipt.payload['result'], 'replayed': True}
    items = list_sections(session, paper_id)
    revision = section_content_revision(items)
    order_revision = items[0]['section_order_revision'] if items else _digest({'content_revision': revision, 'order_audit_id': None})
    if payload.expected_revision != order_revision:
        raise RebuildWorkflowError('section_order_revision_conflict')
    current = {s['id']: s for s in items if s['display_status'] == 'current' and s.get('provenance')}
    if {str(e.section_id) for e in payload.sections} != set(current):
        raise RebuildWorkflowError('section_order_requires_all_current_sections')
    imports = {e.target_id: e for e in _events(session, paper_id)}
    rows = {str(r.id): r for r in session.scalars(select(PaperSection).where(PaperSection.paper_id == paper_id))}
    closed_groups = set()
    previous_group = None
    previous_page = None
    checked_readings = {}
    for entry in payload.sections:
        item = current[str(entry.section_id)]
        provenance = item.get('provenance')
        if not provenance or entry.content_sha256 != provenance['content_sha256']:
            raise RebuildWorkflowError('section_order_source_binding_required')
        _checked_snapshot(rows.get(item['id']), imports.get(item['id']))
        fid = provenance['file_id']
        if fid not in checked_readings:
            from app.config import get_settings
            from app.services.rebuild_file_preview import resolve_reading_pdf
            file = get_file(session, UUID(fid))
            if file.paper_id != paper_id or file.sha256 != provenance['source_sha256']:
                raise RebuildWorkflowError('section_order_source_binding_required')
            content = resolve_reading_pdf(file, get_settings()).read_bytes()
            reading_hash = hashlib.sha256(content).hexdigest()
            with fitz.open(stream=content, filetype='pdf') as doc:
                checked_readings[fid] = (reading_hash, len(doc))
        reading_hash, pages = checked_readings[fid]
        if reading_hash != provenance['reading_pdf_sha256'] or item['page_end'] > pages:
            raise RebuildWorkflowError('section_order_reading_source_mismatch')
        group = _group(item)
        if group != previous_group:
            if group in closed_groups:
                raise RebuildWorkflowError('section_order_file_groups_must_be_contiguous')
            if previous_group is not None:
                closed_groups.add(previous_group)
            previous_group = group
            previous_page = None
        if previous_page is not None and item['page_start'] < previous_page:
            raise RebuildWorkflowError('section_order_pages_must_be_nondecreasing')
        previous_page = item['page_start']
        if any(not item['page_start'] <= source.page_number <= item['page_end'] for source in entry.sources):
            raise RebuildWorkflowError('section_order_source_page_mismatch')
    if any('\x00' in value for value in [payload.reason, *[v for entry in payload.sections for src in entry.sources for v in (src.locator, src.quote)]]):
        raise RebuildWorkflowError('section_order_reason_invalid')
    from uuid import uuid4
    audit_id = uuid4()
    result = {'paper_id': str(paper_id), 'request_id': str(payload.request_id),
              'revision': _digest({'content_revision': revision, 'order_audit_id': str(audit_id)}), 'ordered_section_ids': [str(e.section_id) for e in payload.sections],
              'replayed': False, 'scientific_acceptance': 'not_implied'}
    session.add(AuditLog(id=audit_id, paper_id=paper_id, action=ORDER_EVENT, source=ORDER_VERSION,
        target_type='paper_sections', target_id=str(payload.request_id), payload={
            'revision': revision, 'declaration': declaration, 'declaration_sha256': fingerprint,
            'actor': actor, 'result': result}))
    session.flush()
    return result
