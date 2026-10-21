"""Read-only attachment projection: explicit ownership, preserved IDs and bytes."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Paper, PaperRelationship, RebuildPaperFile
from app.utils.artifact_paths import resolve_paper_pdf_path

SI_RELATIONSHIPS = {"supplementary", "supplementary_information", "supporting_information", "si"}


@lru_cache(maxsize=256)
def _digest(path, size, mtime, ctime):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identity(reference, settings):
    try:
        path = resolve_paper_pdf_path(reference, settings.storage_root)
        if path is not None and path.is_file():
            stat = path.stat()
            digest = _digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            return str(path.resolve()), digest, True
    except (OSError, ValueError):
        pass
    # Missing sources are still listed. Never merge different missing paths just
    # because a stale registered hash says they match.
    return str(reference or ""), None, False


def supplementary_attachments(session, paper_ids):
    """Merge SI Paper records and main-owned SI registrations by actual bytes/path."""
    ids = list(paper_ids)
    result = {pid: [] for pid in ids}
    if not ids:
        return result
    settings = get_settings()
    papers = {p.id: p for p in session.scalars(select(Paper).where(Paper.id.in_(ids)))}
    links = list(session.scalars(select(PaperRelationship).where(
        PaperRelationship.source_paper_id.in_(ids),
        PaperRelationship.relationship_type.in_(SI_RELATIONSHIPS))))
    target_ids = {r.target_paper_id for r in links}
    children = {p.id: p for p in session.scalars(select(Paper).where(Paper.id.in_(target_ids)))} if target_ids else {}
    registrations = list(session.scalars(select(RebuildPaperFile).where(
        RebuildPaperFile.paper_id.in_(ids),
        RebuildPaperFile.role == "si").order_by(RebuildPaperFile.created_at, RebuildPaperFile.id)))
    records_by_owner = {}
    for record in registrations:
        records_by_owner.setdefault(record.paper_id, []).append(record)
    from app.services.rebuild_file_lifecycle import file_set
    for pid in ids:
        lifecycle = file_set(session, pid)
        active_ids = set(lifecycle["active_file_ids"])
        historical = [record for record in records_by_owner.get(pid, []) if str(record.id) not in active_ids]
        historical_identities = [_identity(record.storage_path, settings) for record in historical]
        candidates = []
        owned_children = sorted((children[r.target_paper_id] for r in links if r.source_paper_id == pid and r.target_paper_id in children),
                                key=lambda p: (p.created_at, str(p.id)))
        for child in owned_children:
            path, digest, exists = _identity(child.pdf_path, settings)
            if any((path and path == old_path) or (exists and old_exists and digest and digest == old_digest)
                   for old_path, old_digest, old_exists in historical_identities):
                # The linked child is the retained superseded registration, not
                # an unregistered new SI. History remains in file_lifecycle.
                continue
            candidates.append({"id": str(child.id), "file_id": None, "paper_id": str(child.id),
                "attachment_code": child.paper_code, "original_filename": Path(child.pdf_path or "supplementary.pdf").name,
                "sha256": digest, "pdf_exists": exists, "preview_url": f"/api/papers/{child.id}/pdf",
                "download_url": f"/api/papers/{child.id}/pdf?download=true", "_path": path})
        for record in records_by_owner.get(pid, []):
            if str(record.id) not in active_ids:
                continue
            path, digest, exists = _identity(record.storage_path, settings)
            candidates.append({"id": str(record.id), "file_id": str(record.id), "paper_id": None,
                "attachment_code": None, "original_filename": record.original_filename,
                "sha256": digest or record.sha256, "pdf_exists": exists,
                "preview_url": f"/api/rebuild/files/{record.id}/preview",
                "download_url": f"/api/rebuild/files/{record.id}/download", "_path": path})
        merged = []
        for item in candidates:
            duplicate = next((old for old in merged if (item["_path"] and old["_path"] == item["_path"]) or
                             (item["pdf_exists"] and old["pdf_exists"] and item["sha256"] and item["sha256"] == old["sha256"])), None)
            if duplicate is None:
                merged.append(item)
            elif item["file_id"]:
                duplicate.update(file_id=item["file_id"], original_filename=item["original_filename"],
                                 preview_url=item["preview_url"], download_url=item["download_url"])
        main = papers.get(pid)
        digits = "".join(c for c in (main.paper_code or "") if c.isdigit()) if main else ""
        used_codes = {item["attachment_code"] for item in merged if item["attachment_code"]}
        ordinal = 1
        for item in merged:
            item.pop("_path")
            if not item["attachment_code"] and digits:
                while ("S" + digits + (f"-{ordinal}" if ordinal > 1 else "")) in used_codes:
                    ordinal += 1
                item["attachment_code"] = "S" + digits + (f"-{ordinal}" if ordinal > 1 else "")
                used_codes.add(item["attachment_code"])
            result[pid].append(item)
    return result
