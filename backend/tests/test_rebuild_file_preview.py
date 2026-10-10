from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import rebuild
from app.config import get_settings
from app.db.session import get_db_session
from app.services.rebuild_file_preview import (
    install_derived_pdf,
    resolve_original_download,
    resolve_reading_pdf,
)
from app.services.rebuild_workflow_service import RebuildWorkflowError


pytestmark = pytest.mark.no_test_database


def _record(root: Path, name: str, content: bytes):
    source = root / name
    source.write_bytes(content)
    return SimpleNamespace(storage_path=name, original_filename=name,
                           sha256=hashlib.sha256(content).hexdigest())


def _docx(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return path.read_bytes()


def _pdf(path: Path) -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Table S1 actual PDF page")
    document.save(path)
    document.close()
    return path.read_bytes()


def test_docx_preview_is_real_pdf_and_original_stays_downloadable(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    settings = SimpleNamespace(storage_root=root)
    docx_bytes = _docx(tmp_path / "made.docx")
    record = _record(root, "si.docx", docx_bytes)
    pdf = tmp_path / "reading.pdf"
    pdf_bytes = _pdf(pdf)

    with pytest.raises(RebuildWorkflowError, match="derived_pdf_preview_unavailable"):
        resolve_reading_pdf(record, settings)
    cached = install_derived_pdf(record, settings, pdf)
    assert cached.name == f"{record.sha256}.pdf"
    assert resolve_reading_pdf(record, settings).read_bytes() == pdf_bytes
    assert install_derived_pdf(record, settings, pdf) == cached
    original, media_type = resolve_original_download(record, settings)
    assert original.read_bytes() == docx_bytes
    assert media_type.endswith("wordprocessingml.document")


def test_cache_cannot_be_replaced_with_different_pages(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    settings = SimpleNamespace(storage_root=root)
    record = _record(root, "si.docx", _docx(tmp_path / "made.docx"))
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _pdf(first)
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Different page")
    document.save(second)
    document.close()
    install_derived_pdf(record, settings, first)
    with pytest.raises(RebuildWorkflowError, match="derived_pdf_cache_conflict"):
        install_derived_pdf(record, settings, second)


def test_pdf_keeps_original_preview_and_zip_is_not_pdf(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    settings = SimpleNamespace(storage_root=root)
    pdf = _pdf(tmp_path / "main.pdf")
    main = _record(root, "main.pdf", pdf)
    assert resolve_reading_pdf(main, settings).read_bytes() == pdf

    unsupported = _record(root, "archive.zip", b"PK\x03\x04garbage")
    with pytest.raises(RebuildWorkflowError, match="source_preview_format_unsupported"):
        resolve_reading_pdf(unsupported, settings)

    main.sha256 = "0" * 64
    with pytest.raises(RebuildWorkflowError, match="source_sha256_mismatch"):
        resolve_reading_pdf(main, settings)


def test_preview_endpoint_serves_pdf_and_download_serves_original(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    settings = SimpleNamespace(storage_root=root)
    original = _docx(tmp_path / "made.docx")
    record = _record(root, "si.docx", original)
    pdf = tmp_path / "reading.pdf"
    _pdf(pdf)
    install_derived_pdf(record, settings, pdf)
    monkeypatch.setattr(rebuild, "get_file", lambda session, file_id: record)

    app = FastAPI()
    app.include_router(rebuild.router, prefix="/api/rebuild")
    app.dependency_overrides[get_db_session] = lambda: object()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        missing_cache = root / "rebuild_previews" / f"{record.sha256}.pdf"
        missing_cache.rename(tmp_path / "held.pdf")
        unavailable = client.get("/api/rebuild/files/1887ad2c-340b-4bb8-9e65-e32891889c36/preview")
        assert unavailable.status_code == 409
        assert unavailable.json()["detail"] == "derived_pdf_preview_unavailable"
        (tmp_path / "held.pdf").rename(missing_cache)

        preview = client.get("/api/rebuild/files/1887ad2c-340b-4bb8-9e65-e32891889c36/preview")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "application/pdf"
        assert preview.headers["cache-control"] == "private, no-store"
        assert preview.headers["content-disposition"].startswith("inline;")
        assert preview.content.startswith(b"%PDF-")
        with fitz.open(stream=preview.content, filetype="pdf") as document:
            assert document.page_count == 1
            assert "Table S1" in document[0].get_text()

        download = client.get("/api/rebuild/files/1887ad2c-340b-4bb8-9e65-e32891889c36/download")
        assert download.status_code == 200
        assert download.headers["content-type"].endswith("wordprocessingml.document")
        assert download.headers["content-disposition"].startswith("attachment;")
        assert hashlib.sha256(download.content).hexdigest() == record.sha256
        assert download.content == original
