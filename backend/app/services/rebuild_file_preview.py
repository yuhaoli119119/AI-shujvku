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
