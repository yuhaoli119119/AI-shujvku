from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import d2_active_library_test_pollution_audit as audit


def _write_sqlite(path: Path, *, pdf_path: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE papers (
                id TEXT PRIMARY KEY,
                title TEXT,
                pdf_path TEXT,
                tei_path TEXT,
                docling_json_path TEXT,
                markdown_path TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO papers (id, title, pdf_path, tei_path, docling_json_path, markdown_path) VALUES (?, ?, ?, ?, ?, ?)",
            ("paper-1", "Paper 1", pdf_path, None, None, None),
        )
        connection.commit()
    finally:
        connection.close()


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_pollution_audit_reports_clean_state(monkeypatch, tmp_path):
    library_root = tmp_path / "library"
    db_path = library_root / "database.sqlite"
    _write_sqlite(db_path)

    monkeypatch.setattr(
        audit,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str(db_path.resolve()),
            "effective_db_path": str(db_path.resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": False,
        },
    )

    report = audit.build_report()

    assert report["tiny_uuid_only_unref_pdf_count"] == 0
    assert report["pollution_detected"] is False
    assert report["cleanup_deleted_count"] == 0


def test_pollution_audit_detects_tiny_uuid_unref_pdf_without_deleting(monkeypatch, tmp_path):
    library_root = tmp_path / "library"
    db_path = library_root / "database.sqlite"
    pollutant = library_root / "storage" / "pdf" / "12345678-1234-4234-9234-123456789abc.pdf"
    _write_sqlite(db_path)
    _write_file(pollutant, b"%PDF-1.4 tiny")

    monkeypatch.setattr(
        audit,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str(db_path.resolve()),
            "effective_db_path": str(db_path.resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": False,
        },
    )

    report = audit.build_report()

    assert report["tiny_uuid_only_unref_pdf_count"] == 1
    assert report["pollution_detected"] is True
    assert pollutant.exists()


def test_pollution_cleanup_does_not_delete_db_referenced_artifacts(monkeypatch, tmp_path):
    library_root = tmp_path / "library"
    db_path = library_root / "database.sqlite"
    referenced_relative = "storage/pdf/12345678-1234-4234-9234-123456789abc.pdf"
    referenced_pdf = library_root / referenced_relative
    unreferenced_pdf = library_root / "storage" / "pdf" / "abcdef12-1234-4234-9234-123456789abc.pdf"
    _write_sqlite(db_path, pdf_path=referenced_relative)
    _write_file(referenced_pdf, b"%PDF-1.4 referenced")
    _write_file(unreferenced_pdf, b"%PDF-1.4 unref")

    monkeypatch.setattr(
        audit,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str(db_path.resolve()),
            "effective_db_path": str(db_path.resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": False,
        },
    )

    report = audit.build_report(cleanup=True, apply=True)

    assert report["cleanup_deleted_count"] == 1
    assert referenced_pdf.exists()
    assert not unreferenced_pdf.exists()
