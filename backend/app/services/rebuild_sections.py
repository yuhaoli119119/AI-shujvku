"""Append-only chapters for the mature reader, with immutable source events.

PaperSection keeps original text/headings. AuditLog stores a versioned, complete
import event rather than overloading heading fields or changing the old schema.
Both rows and the request receipt commit together; callers own the transaction.
This checks file identity and physical pagination, not scientific correctness.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid5, NAMESPACE_URL

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, Paper, PaperSection, RebuildPaperFile
from app.schemas.rebuild import RebuildSectionsImportRequest
from app.services.rebuild_workflow_service import RebuildWorkflowError, get_file

EVENT = "section_imported"
RECEIPT = "section_import_request"
VERSION = "rebuild_sections_v1"
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


def list_sections(session: Session, paper_id: UUID) -> list[dict]:
    """Three batched reads, including old chapters with unknown file coverage."""
    rows = list(session.scalars(select(PaperSection).where(
        PaperSection.paper_id == paper_id).order_by(PaperSection.page_start, PaperSection.id)))
    events = {event.target_id: event for event in _events(session, paper_id)}
    files = {str(file.id): file for file in session.scalars(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id == paper_id))}
    items = []
    for row in rows:
        item = {"id": str(row.id), "paper_id": str(paper_id),
                **{field: getattr(row, field) for field in SECTION_FIELDS}}
        item["heading_path"] = item["heading_path"] or []
        item.update(section_key=None, explanation_zh=None, provenance=None,
                    conflicts_with_section_ids=[], scientific_acceptance="not_implied")
        event = events.get(str(row.id))
        if event and isinstance(event.payload, dict):
            data = event.payload
            snapshot = data.get("section", {})
            file = files.get(snapshot.get("file_id"))
            if file:
                item.update(section_key=snapshot.get("section_key"),
                            explanation_zh=snapshot.get("explanation_zh"),
                            conflicts_with_section_ids=snapshot.get("conflicts_with_section_ids", []),
                            provenance={
                                "version": VERSION, "audit_id": str(event.id),
                                "section_id": str(row.id), "file_id": str(file.id),
                                "role": file.role, "original_filename": file.original_filename,
                                "source_sha256": snapshot.get("source_sha256"),
                                "page_basis": snapshot.get("page_basis"),
                                "reading_pdf_sha256": data.get("reading_pdf_sha256"),
                                "sources": snapshot.get("sources", []),
                                "request_id": data.get("request_id"), "actor": data.get("actor"),
                                "content_sha256": data.get("content_sha256"),
                                "created_at": event.created_at.isoformat(),
                                "preview_url": f"/api/rebuild/files/{file.id}/preview",
                                "download_url": f"/api/rebuild/files/{file.id}/download",
                            })
        items.append(item)
    return items


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
    }


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
