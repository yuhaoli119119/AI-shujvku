"""Immutable, hash-bound PDF chunks and renders; no scientific decisions/writes."""
from __future__ import annotations

import hashlib
import json
import fcntl
from contextlib import contextmanager
from pathlib import Path

import fitz
from sqlalchemy import select

from app.db.models import RebuildVisualAsset
from app.services.rebuild_acceptance import _digest
from app.services.rebuild_file_preview import resolve_reading_pdf, _sha256
from app.services.rebuild_workflow_service import RebuildWorkflowError, serialize_asset

VERSION = "pdf_chunks_v1"


def _reading(record, settings, expected_source=None, expected_reading=None):
    path = resolve_reading_pdf(record, settings)
    digest = _sha256(path)
    if expected_source and expected_source != record.sha256 or expected_reading and expected_reading != digest:
        raise RebuildWorkflowError("immutable_read_source_changed")
    directory = settings.storage_root.resolve() / "rebuild_read_cache" / VERSION / fitz.VersionBind / record.sha256 / digest
    directory.mkdir(parents=True, exist_ok=True)
    return path, digest, directory


@contextmanager
def _cache_lock(directory):
    with (directory / "cache.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _install(path: Path, body: bytes):
    try:
        with path.open("xb") as stream:
            stream.write(body)
    except FileExistsError:
        if path.read_bytes() != body:
            raise RebuildWorkflowError("immutable_cache_content_mismatch")
    return path


def paginated_text(record, settings, *, offset=0, limit=5, expected_source=None, expected_reading=None):
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 20:
        raise RebuildWorkflowError("text_page_bounds_invalid")
    path, reading_hash, directory = _reading(record, settings, expected_source, expected_reading)
    pages = []
    with fitz.open(path) as document:
        total = document.page_count
        for index in range(offset, min(total, offset + limit)):
            cache = directory / f"text-{index + 1}.json"
            with _cache_lock(directory):
                if cache.is_file():
                    try:
                        item = json.loads(cache.read_text())
                        if item["physical_page"] != index + 1 or hashlib.sha256(item["text"].encode()).hexdigest() != item["text_sha256"]:
                            raise ValueError("text cache hash mismatch")
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        raise RebuildWorkflowError("immutable_cache_content_mismatch") from exc
                else:
                    text = document[index].get_text("text", sort=False)
                    item = {"physical_page": index + 1, "text": text,
                            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
                    _install(cache, json.dumps(item, ensure_ascii=False, sort_keys=True).encode())
            pages.append(item)
    next_offset = offset + len(pages) if offset + len(pages) < total else None
    return {"version": VERSION, "paper_id": str(record.paper_id), "file_id": str(record.id),
            "source_sha256": record.sha256, "reading_pdf_sha256": reading_hash,
            "total_pages": total, "offset": offset, "limit": limit, "items": pages,
            "has_more": next_offset is not None, "next_offset": next_offset,
            "scientific_judgment": "not_performed", "ocr": "not_performed",
            "empty_text_rule": "An empty text layer is not proof of an empty page; inspect the page render."}


def rendered_page(record, settings, *, page, dpi=120, expected_source=None, expected_reading=None):
    if type(page) is not int or page < 1 or dpi not in {72, 120, 160}:
        raise RebuildWorkflowError("render_page_bounds_invalid")
    path, digest, directory = _reading(record, settings, expected_source, expected_reading)
    output = directory / f"page-{page}-{dpi}.png"
    with _cache_lock(directory):
        metadata = directory / f"page-{page}-{dpi}.json"
        if output.is_file() and metadata.is_file():
            body = output.read_bytes()
            try:
                if json.loads(metadata.read_text()).get("sha256") != hashlib.sha256(body).hexdigest():
                    raise ValueError("render cache hash mismatch")
            except (ValueError, AttributeError, TypeError) as exc:
                raise RebuildWorkflowError("immutable_cache_content_mismatch") from exc
        else:
            with fitz.open(path) as document:
                if page > document.page_count:
                    raise RebuildWorkflowError("render_page_out_of_range")
                body = document[page - 1].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False).tobytes("png")
            _install(output, body)
            _install(metadata, json.dumps({"sha256": hashlib.sha256(body).hexdigest()}).encode())
    return output, {"source_sha256": record.sha256, "reading_pdf_sha256": digest,
                    "render_sha256": hashlib.sha256(body).hexdigest()}


def asset_inventory(session, record, settings, *, inventory_revision=None):
    _, digest, directory = _reading(record, settings)
    if inventory_revision is not None:
        import re
        if not re.fullmatch(r"[0-9a-f]{64}", inventory_revision):
            raise RebuildWorkflowError("inventory_revision_invalid")
        cached = directory / f"inventory-{inventory_revision}.json"
        with _cache_lock(directory):
            if not cached.is_file():
                raise RebuildWorkflowError("immutable_inventory_not_cached")
            try:
                result = json.loads(cached.read_text())
                state = {key: value for key, value in result.items() if key != "inventory_revision"}
                if _digest(state) != inventory_revision or result["file_id"] != str(record.id):
                    raise ValueError("inventory cache hash mismatch")
            except (ValueError, AttributeError, KeyError, TypeError) as exc:
                raise RebuildWorkflowError("immutable_cache_content_mismatch") from exc
            return result
    assets = [serialize_asset(asset) for asset in session.scalars(select(RebuildVisualAsset).where(
        RebuildVisualAsset.paper_id == record.paper_id, RebuildVisualAsset.file_id == record.id).order_by(RebuildVisualAsset.id))]
    result = {"version": VERSION, "file_id": str(record.id), "paper_id": str(record.paper_id),
              "source_sha256": record.sha256, "reading_pdf_sha256": digest, "items": assets,
              "coverage": "stored_inventory_only_not_complete_page_coverage"}
    revision = _digest(result)
    result["inventory_revision"] = revision
    with _cache_lock(directory):
        _install(directory / f"inventory-{revision}.json", json.dumps(result, ensure_ascii=False, sort_keys=True).encode())
    return result


def chunk_packet_contract():
    return {"version": VERSION, "identity": ["paper_id", "file_id", "source_sha256", "reading_pdf_sha256", "physical_page", "text_sha256"],
            "rules": ["Read bounded physical-page chunks; follow next_offset until has_more=false.",
                      "Retain raw strings and missing values; do not infer numerical values or scientific conclusions.",
                      "413 context: split page/text chunks; 413 images: fewer pages/lower allowed DPI; 429: bounded backoff.",
                      "Cache inventory revision additionally binds current stored assets; source hash alone cannot freeze changing explanations.",
                      "Submit through existing validated write endpoints with stable request_id, then freeze and independently review."],
            "automatic_scientific_adjudication": False}
