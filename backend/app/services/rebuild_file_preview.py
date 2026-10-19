"""Keep registered source bytes separate from a PDF made for reading them."""
from __future__ import annotations

import hashlib
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


def install_derived_pdf(record: RebuildPaperFile, settings: Settings, pdf_path: Path) -> Path:
    """Install a separately rendered PDF once, without replacing either artifact."""
    _, source_format = _checked_source(record, settings)
    if source_format != "docx":
        raise RebuildWorkflowError("derived_pdf_requires_docx_source")
    if not _readable_pdf(pdf_path):
        raise RebuildWorkflowError("derived_pdf_invalid")
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


PAGE_IDENTITY_FIELDS = ("page_basis", "source_sha256", "reading_pdf_sha256")


def reading_identity(record, settings):
    """Resolve and hash existing bytes only. Never convert/install on a read."""
    source, kind = _checked_source(record, settings)
    path = resolve_reading_pdf(record, settings)
    return path, {"source_sha256": record.sha256,
                  "page_basis": "source_pdf" if kind == "pdf" else "derived_pdf",
                  "reading_pdf_sha256": _sha256(path)}


def validate_page_identity(record, settings, data):
    """Normalize new writes; a DOCX page always requires an explicit double binding."""
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
    identities[str(source.id)] = {k: data.get(k) for k in PAGE_IDENTITY_FIELDS}
    value.conflict = {**(value.conflict or {}), "source_page_identities": identities}
