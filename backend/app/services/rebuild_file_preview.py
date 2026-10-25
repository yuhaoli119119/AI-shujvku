"""Keep registered source bytes separate from a PDF made for reading them."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import fitz

from app.config import Settings
from app.db.models import RebuildPaperFile
from app.services.rebuild_workflow_service import RebuildWorkflowError, resolve_file_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(8)
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" in names and "word/document.xml" in names:
                    return "docx"
        except (OSError, zipfile.BadZipFile):
            pass
    return "unsupported"


def _checked_source(record: RebuildPaperFile, settings: Settings) -> tuple[Path, str]:
    source = resolve_file_path(record, settings)
    if not re.fullmatch(r"[0-9a-f]{64}", record.sha256 or ""):
        raise RebuildWorkflowError("source_sha256_invalid")
    if _sha256(source) != record.sha256:
        raise RebuildWorkflowError("source_sha256_mismatch")
    return source, _format(source)


def _readable_pdf(path: Path) -> bool:
    if not path.is_file() or _format(path) != "pdf":
        return False
    try:
        with fitz.open(path) as document:
            return document.page_count > 0 and not document.needs_pass
    except (fitz.FileDataError, fitz.EmptyFileError, OSError):
        return False


def preview_cache_path(record: RebuildPaperFile, settings: Settings) -> Path:
    """Original SHA identifies its derived reading PDF; the original stays intact."""
    if not re.fullmatch(r"[0-9a-f]{64}", record.sha256 or ""):
        raise RebuildWorkflowError("source_sha256_invalid")
    return settings.storage_root.resolve() / "rebuild_previews" / f"{record.sha256}.pdf"


def resolve_reading_pdf(record: RebuildPaperFile, settings: Settings) -> Path:
    source, source_format = _checked_source(record, settings)
    if source_format == "pdf":
        return source
    if source_format != "docx":
        raise RebuildWorkflowError("source_preview_format_unsupported")
    cached = preview_cache_path(record, settings)
    if not _readable_pdf(cached):
        raise RebuildWorkflowError("derived_pdf_preview_unavailable")
    return cached


def resolve_original_download(record: RebuildPaperFile, settings: Settings) -> tuple[Path, str]:
    source, source_format = _checked_source(record, settings)
    media_type = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(source_format, "application/octet-stream")
    return source, media_type


def versioned_preview_path(record, settings, converter):
    """An explicit converter identity selects a separate immutable artifact."""
    if (not isinstance(converter, dict) or set(converter) != {'name', 'version'} or
            any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in converter.values())):
        raise RebuildWorkflowError('preview_converter_identity_invalid')
    identity = hashlib.sha256(json.dumps(converter, sort_keys=True).encode()).hexdigest()
    return preview_cache_path(record, settings).parent / 'versions' / record.sha256 / identity / 'reading.pdf'


def preview_provenance(record, settings):
    """Read legacy identity without requiring a new manifest or conversion."""
    path = preview_cache_path(record, settings)
    manifest = path.with_suffix('.json')
    if not manifest.is_file():
        return {'status': 'legacy_converter_unknown', 'source_sha256': record.sha256,
                'reading_pdf_sha256': _sha256(path) if _readable_pdf(path) else None}
    try:
        data = json.loads(manifest.read_text())
        if data['source_sha256'] != record.sha256 or data['reading_pdf_sha256'] != _sha256(path):
            raise ValueError('preview_manifest_mismatch')
        return {**data, 'status': 'manifest_identity_match_only'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'preview_manifest_unverified', 'source_sha256': record.sha256}


def resolve_versioned_preview(record, settings, converter):
    """Reuse only a fully checked source/tool/output tuple; never convert on GET."""
    _checked_source(record, settings)
    path = versioned_preview_path(record, settings, converter)
    try:
        manifest = json.loads(path.with_suffix('.json').read_text())
        if (manifest.get('converter') != converter or manifest.get('source_sha256') != record.sha256 or
                not _readable_pdf(path) or manifest.get('reading_pdf_sha256') != _sha256(path)):
            raise ValueError('cache_identity_mismatch')
    except (OSError, ValueError, TypeError) as exc:
        raise RebuildWorkflowError('versioned_preview_unavailable_or_corrupt') from exc
    return path


def install_derived_pdf(record: RebuildPaperFile, settings: Settings, pdf_path: Path, *, converter=None) -> Path:
    """Install a separately rendered PDF once, without replacing either artifact."""
    _, source_format = _checked_source(record, settings)
    if source_format != "docx":
        raise RebuildWorkflowError("derived_pdf_requires_docx_source")
    if not _readable_pdf(pdf_path):
        raise RebuildWorkflowError("derived_pdf_invalid")
    if converter is not None:
        destination = versioned_preview_path(record, settings, converter)
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = pdf_path.read_bytes()
        manifest = {'version': 'derived_pdf_converter_v1', 'converter': converter,
                    'converter_basis': 'operator_declared_not_execution_verified',
                    'source_sha256': record.sha256, 'reading_pdf_sha256': hashlib.sha256(body).hexdigest()}
        from app.services.rebuild_read_cache import _install, _cache_lock
        with _cache_lock(destination.parent):
            _install(destination, body)
            _install(destination.with_suffix('.json'), json.dumps(manifest, sort_keys=True).encode())
        # Converter updates never silently change the PDF to which old pages
        # point. Versioned artifacts remain explicit candidates for future use.
        return destination
    destination = preview_cache_path(record, settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _readable_pdf(destination) and _sha256(destination) == _sha256(pdf_path):
            return destination
        raise RebuildWorkflowError("derived_pdf_cache_conflict")
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".preview-", delete=False) as handle:
        temporary = Path(handle.name)
        with pdf_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o644)
    try:
        # A second writer must never replace the first PDF, since page numbers
        # already stored for this source refer to a specific rendered artifact.
        os.link(temporary, destination)
    except FileExistsError:
        if not (_readable_pdf(destination) and _sha256(destination) == _sha256(pdf_path)):
            raise RebuildWorkflowError("derived_pdf_cache_conflict")
    finally:
        temporary.unlink(missing_ok=True)
    return destination


PAGE_IDENTITY_FIELDS = ("page_basis", "source_sha256", "reading_pdf_sha256", "ooxml_locator")


def reading_identity(record, settings):
    """Resolve and hash existing bytes only. Never convert/install on a read."""
    source, kind = _checked_source(record, settings)
    path = resolve_reading_pdf(record, settings)
    return path, {"source_sha256": record.sha256,
                  "page_basis": "source_pdf" if kind == "pdf" else "derived_pdf",
                  "reading_pdf_sha256": _sha256(path)}


def validate_ooxml_identity(record, settings, data):
    """Resolve a 1-based OOXML body-child locator against checked original bytes.

    A locator selects the body child, then an indexed element in that child
    (including the child itself), followed by indexed direct XML children.
    No pagination metadata or derived PDF participates in this identity.
    """
    from xml.etree import ElementTree as ET
    source, kind = _checked_source(record, settings)
    if kind != "docx":
        raise RebuildWorkflowError("ooxml_requires_registered_docx")
    if source.stat().st_size != record.file_size:
        raise RebuildWorkflowError("source_size_mismatch")
    if data.get("source_sha256") != record.sha256:
        raise RebuildWorkflowError("source_sha256_mismatch")
    if data.get("page_number") is not None or data.get("reading_pdf_sha256") is not None:
        raise RebuildWorkflowError("ooxml_has_no_physical_page")
    locator = data.get("ooxml_locator") or ""
    if not isinstance(locator, str) or len(locator) > 4000:
        raise RebuildWorkflowError("ooxml_locator_invalid")
    match = re.fullmatch(r"word/document\.xml#body-child\[([1-9][0-9]*)\]((?:/w:[A-Za-z][A-Za-z0-9]*\[[1-9][0-9]*\])+)", locator)
    if not match or len(locator) > 4000:
        raise RebuildWorkflowError("ooxml_locator_invalid")
    try:
        with zipfile.ZipFile(source) as archive:
            if archive.getinfo("word/document.xml").file_size > 32 * 1024 * 1024:
                raise ValueError("oversize XML")
            xml = archive.read("word/document.xml")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError("XML declarations forbidden")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = root.find(ns + "body")
        node = list(body)[int(match[1]) - 1]
        for index, (name, number) in enumerate(re.findall(r"/w:([A-Za-z][A-Za-z0-9]*)\[([1-9][0-9]*)\]", match[2])):
            candidates = list(node.iter(ns + name)) if index == 0 else list(node.findall(ns + name))
            node = candidates[int(number) - 1]
    except (ET.ParseError, ValueError, IndexError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        raise RebuildWorkflowError("ooxml_locator_not_found") from exc
    return {"page_basis": "ooxml", "source_sha256": record.sha256,
            "reading_pdf_sha256": None, "ooxml_locator": locator}


def validate_page_identity(record, settings, data):
    """Normalize new writes; a DOCX page always requires an explicit double binding."""
    if data.get("page_basis") == "ooxml":
        return validate_ooxml_identity(record, settings, data)
    if data.get("ooxml_locator") is not None:
        raise RebuildWorkflowError("ooxml_page_basis_required")
    _, kind = _checked_source(record, settings)
    if data.get("source_sha256") not in (None, record.sha256):
        raise RebuildWorkflowError("source_sha256_mismatch")
    if data.get("page_number") is None:
        if data.get("page_basis") is not None or data.get("reading_pdf_sha256") is not None:
            raise RebuildWorkflowError("source_page_identity_requires_page")
        return {"page_basis": None, "source_sha256": record.sha256, "reading_pdf_sha256": None}
    if kind != "pdf" and (data.get("page_basis") != "derived_pdf" or
                            data.get("source_sha256") is None or data.get("reading_pdf_sha256") is None):
        raise RebuildWorkflowError("source_derived_page_identity_required")
    path, identity = reading_identity(record, settings)
    if data.get("page_basis") not in (None, identity["page_basis"]):
        raise RebuildWorkflowError("source_page_basis_mismatch")
    if data.get("reading_pdf_sha256") not in (None, identity["reading_pdf_sha256"]):
        raise RebuildWorkflowError("source_reading_pdf_sha256_mismatch")
    with fitz.open(path) as doc:
        if doc.needs_pass or not 1 <= data["page_number"] <= doc.page_count:
            raise RebuildWorkflowError("source_page_out_of_range")
    return identity


def stored_source_identity(source):
    # Existing value JSON is the audited extension point; no schema migration.
    from sqlalchemy.orm import object_session
    from app.db.models import RebuildDataValue
    session = object_session(source)
    value = session.get(RebuildDataValue, source.value_id) if session else None
    return dict(((value.conflict or {}).get("source_page_identities") or {}).get(str(source.id), {})) if value else {}


def store_source_identity(value, source, data):
    if not data.get("source_sha256"):
        return  # A fileless quote must not acquire a verified byte identity.
    identities = dict((value.conflict or {}).get("source_page_identities") or {})
    identities[str(source.id)] = {k: data.get(k) for k in PAGE_IDENTITY_FIELDS
                                  if k != "ooxml_locator" or data.get(k) is not None}
    value.conflict = {**(value.conflict or {}), "source_page_identities": identities}
