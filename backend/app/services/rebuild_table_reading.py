"""Read-only representation gate with explicit legacy PDF identity bindings."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

import fitz
from sqlalchemy import select

from app.db.models import Paper, PaperRelationship, PaperTable, RebuildPaperFile, RebuildVisualAsset
from app.services.catalog_attachments import SI_RELATIONSHIPS
from app.services.rebuild_file_preview import _sha256
from app.services.rebuild_table_content import representation_complete, table_content
from app.services.rebuild_workflow_service import resolve_file_path, RebuildWorkflowError
from app.utils.artifact_paths import resolve_paper_pdf_path

VERSION = "table_representation_v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=512)
def _cached_hash(path, device, inode, size, mtime_ns, ctime_ns):
    from pathlib import Path
    return _sha256(Path(path))


def _physical(path, *, force=False):
    if path is None or not path.is_file() or path.suffix.lower() != ".pdf":
        return None
    stat = path.stat()
    return _sha256(path) if force else _cached_hash(str(path.resolve()), stat.st_dev, stat.st_ino,
                                                  stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def table_label(value):
    value = str(value or "").strip()
    bare = re.fullmatch(r"s?\d+[a-z]?", value, re.I)
    match = bare or re.match(r"^(?:table|表)[\s_.-]*(s?\d+[a-z]?)(?=$|[\s:：.)\-—,，;；])", value, re.I)
    return (match.group(0) if bare else match.group(1)).lower() if match else None


def table_representations(session, paper_id, settings, *, force_physical=False):
    """Return source-bound representation checks/dependencies without writes."""
    checks, scopes, dependencies = [], [], {}
    legal_links = list(session.scalars(select(PaperRelationship).where(
        PaperRelationship.source_paper_id == paper_id,
        PaperRelationship.relationship_type.in_(SI_RELATIONSHIPS))))
    scope_ids = {paper_id, *(link.target_paper_id for link in legal_links)}
    scope_rows = list(session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(scope_ids)).order_by(PaperTable.id)))
    verified_legacy = set()
    physical_cache, scope_cache, source_cache = {}, {}, {}
    def physical(path):
        key = str(path)
        if key not in physical_cache:
            try:
                physical_cache[key] = _physical(path, force=force_physical)
            except (OSError, ValueError):
                physical_cache[key] = None
        return physical_cache[key]
    def source_binding(file):
        if file.id in source_cache:
            return source_cache[file.id]
        digest, count = None, None
        try:
            path = resolve_file_path(file, settings)
            digest = physical(path)
            if digest and digest == file.sha256:
                with fitz.open(path) as document:
                    count = document.page_count
        except (RebuildWorkflowError, OSError, ValueError, RuntimeError):
            pass
        source_cache[file.id] = {"paper_id": str(paper_id), "file_id": str(file.id), "role": file.role,
                                 "source_sha256": file.sha256, "physical_sha256": digest,
                                 "physical_total_pages": count}
        return source_cache[file.id]
    def scope(file):
        if file.id in scope_cache:
            return scope_cache[file.id]
        source = source_binding(file)
        registered_hash = source["physical_sha256"]
        links = legal_links if file.role == "si" else []
        owners = [paper_id] if file.role == "main" else [link.target_paper_id for link in links] if file.role == "si" else []
        binding = {**source,
                   "legal_scope": [], "relationships": sorted(
                       [{"id": str(link.id), "target_paper_id": str(link.target_paper_id), "type": link.relationship_type} for link in links], key=lambda x: x["id"])}
        records = []
        for owner in sorted(set(owners), key=str):
            paper = session.get(Paper, owner)
            try:
                path = resolve_paper_pdf_path(paper.pdf_path, settings.storage_root) if paper else None
                digest = physical(path)
            except (OSError, ValueError):
                digest = None
            binding["legal_scope"].append({"paper_id": str(owner), "physical_sha256": digest})
            if digest and source["physical_total_pages"] and registered_hash == file.sha256 == digest:
                records.extend(row for row in scope_rows if row.paper_id == owner)
        verified_legacy.update(str(row.id) for row in records)
        scope_cache[file.id] = binding, records
        scopes.append(binding)
        return binding, records
    def dependency(row):
        state = {column.name: getattr(row, column.name) for column in PaperTable.__table__.columns}
        record = {"id": str(row.id), "paper_id": str(row.paper_id), "sha256": _digest(state)}
        dependencies[str(row.id)] = record
        return record
    consumed = set()
    for asset in session.scalars(select(RebuildVisualAsset).where(
            RebuildVisualAsset.paper_id == paper_id, RebuildVisualAsset.asset_type == "table").order_by(RebuildVisualAsset.id)):
        content = table_content(asset)
        complete = representation_complete(content)
        result = {"asset_id": str(asset.id), "format": content["format"], "complete": complete,
                  "reason": "formal_representation" if complete else "pending_structure",
                  "legacy_records": [], "binding": {}}
        checks.append(result)
        formal = content["format"] in {"structured", "markdown"}
        if formal and not complete:
            result["reason"] = "formal_representation_incomplete"
        file = session.get(RebuildPaperFile, asset.file_id) if asset.file_id else None
        pages = asset.page_numbers or []
        if file is None or file.paper_id != paper_id:
            result.update(complete=False, reason="table_source_unverified")
            result["binding"] = {"file_id": str(asset.file_id) if asset.file_id else None,
                                 "file_owner_id": str(file.paper_id) if file else None}
            continue
        binding, rows = scope(file)
        result["binding"] = {**binding, "physical_pages": pages}
        count = binding["physical_total_pages"]
        if (binding["physical_sha256"] != file.sha256 or not count or not isinstance(pages, list) or not pages or
                any(type(page) is not int or page < 1 or page > count for page in pages)):
            result.update(complete=False, reason="table_source_unverified")
            continue
        label = table_label(asset.figure_label) or table_label(asset.caption)
        labels = {item for item in (table_label(asset.figure_label), table_label(asset.caption)) if item}
        if not label or len(labels) > 1:
            if not formal:
                result["reason"] = "legacy_identity_unverified"
            continue
        result["binding"] = {**binding, "label": label, "physical_pages": pages}
        candidates = [row for row in rows if row.page in pages and table_label(row.caption) == label]
        page_records = {page: [row for row in candidates if row.page == page] for page in set(pages)}
        if any(len(matches) != 1 for matches in page_records.values()):
            if not formal:
                result["reason"] = "legacy_match_missing_or_ambiguous"
            continue
        selected = [matches[0] for _, matches in sorted(page_records.items())]
        result["legacy_records"] = [dependency(row) for row in selected]
        consumed.update(str(row.id) for row in selected)
        if formal:
            continue  # Existing formal priority also applies to completeness.
        if not all(representation_complete({"format": "markdown", "text": row.markdown_content or ""}) for row in selected):
            result["reason"] = "legacy_representation_incomplete"
        else:
            result.update(format="legacy_markdown", complete=True, reason="source_bound_legacy")
    # The detail page also consumes remaining legacy tables directly. Freeze
    # their full records and legal PDF/SI scope, including relation changes.
    for file in session.scalars(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id).order_by(RebuildPaperFile.id)):
        binding, rows = scope(file)
        for row in rows:
            dependency(row)
            if str(row.id) in consumed:
                continue
            complete = bool(table_label(row.caption) and type(row.page) is int and row.page > 0 and
                            row.page <= binding["physical_total_pages"] and
                            representation_complete({"format": "markdown", "text": row.markdown_content or ""}))
            checks.append({"asset_id": None, "legacy_id": str(row.id), "format": "legacy_markdown", "complete": complete,
                           "reason": "source_bound_legacy" if complete else "legacy_representation_incomplete",
                           "legacy_records": [dependencies[str(row.id)]], "binding": {**binding, "physical_pages": [row.page]}})
            consumed.add(str(row.id))
    for row in scope_rows:
        dependency(row)
        if str(row.id) not in consumed and str(row.id) not in verified_legacy:
            checks.append({"asset_id": None, "legacy_id": str(row.id), "format": "legacy_markdown", "complete": False,
                           "reason": "legacy_identity_unverified", "legacy_records": [dependencies[str(row.id)]], "binding": {}})
    return {"version": VERSION, "tables": checks, "scopes": scopes,
            "legacy_records": sorted(dependencies.values(), key=lambda row: row["id"])}
