from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.utils import active_database as active_database_module
from scripts import d2_historical_mirror_migration_readiness as readiness
from scripts import d2_target_conflict_and_artifact_inventory_gate as gate


def _write_registry(path: Path, *, root_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "active_library": "默认文献库",
                "libraries": [
                    {
                        "name": "默认文献库",
                        "root_path": str(root_path.resolve()),
                        "description": "默认文献库",
                        "created_at": "2026-05-26T00:00:00",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_sqlite(path: Path, *, rows: list[tuple[str, str, str | None, str | None, str | None, str | None]]) -> None:
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
        if rows:
            connection.executemany(
                "INSERT INTO papers (id, title, pdf_path, tei_path, docling_json_path, markdown_path) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        connection.commit()
    finally:
        connection.close()


def _write_artifact(path: Path, content: str = "sample") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_report_summarizes_target_conflicts_and_unreferenced_inventory(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "literature-ai"
    backend_root = project_root / "backend"
    current_root = backend_root / "D\uf03a\uf05cDesktop\uf05ctest\uf05clibraries\uf05cdefault"
    proposed_root = project_root / "data" / "libraries" / "default"
    canonical_registry = project_root / "data" / "library_registry.json"
    shadow_registry = workspace_root / "data" / "library_registry.json"

    _write_sqlite(
        current_root / "database.sqlite",
        rows=[
            (
                "paper-1",
                "Ready paper",
                "storage/pdf/ready.pdf",
                "storage/tei/ready.tei.xml",
                "storage/docling_json/ready.docling.json",
                "storage/markdown/ready.md",
            )
        ],
    )
    _write_artifact(current_root / "library.json", '{"name":"默认文献库","storage_mode":"library"}')
    _write_artifact(current_root / "storage" / "pdf" / "ready.pdf", "ready-pdf")
    _write_artifact(current_root / "storage" / "tei" / "ready.tei.xml", "ready-tei")
    _write_artifact(current_root / "storage" / "docling_json" / "ready.docling.json", "ready-docling")
    _write_artifact(current_root / "storage" / "markdown" / "ready.md", "ready-markdown")
    _write_artifact(current_root / "storage" / "pdf" / "stub-only.pdf", "tiny")
    _write_artifact(current_root / "storage" / "figures" / "figure-1.png", "figure")

    _write_sqlite(proposed_root / "database.sqlite", rows=[])
    _write_artifact(proposed_root / "library.json", '{"name":"默认文献库","storage_mode":"library"}')

    _write_registry(canonical_registry, root_path=current_root)
    _write_registry(shadow_registry, root_path=proposed_root)

    monkeypatch.setattr(readiness, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(readiness, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(readiness, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(readiness, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(readiness, "default_library_root", lambda: proposed_root.resolve())
    monkeypatch.setattr(readiness, "shadow_registry_paths", lambda: [shadow_registry.resolve()])
    monkeypatch.setattr(active_database_module, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(
        gate,
        "get_active_database_info",
        lambda: {
            "configured_db_path": str((current_root / "database.sqlite").resolve()),
            "active_library_db_path": str((current_root / "database.sqlite").resolve()),
            "effective_db_path": str((current_root / "database.sqlite").resolve()),
            "effective_matches_active_library_db_path": True,
        },
    )

    report = gate.build_report()

    assert report["target_root"] == str(proposed_root.resolve())
    assert len(report["target_conflicts"]) == 2
    assert report["target_database_summary"]["exists"] is True
    assert report["target_database_summary"]["papers_total"] == 0
    assert report["target_database_summary"]["is_current_runtime_db"] is False
    assert "initialized-but-empty" in report["target_database_summary"]["origin_assessment"]
    assert report["target_library_json_summary"]["exists"] is True
    assert "materialized" in report["target_library_json_summary"]["origin_assessment"]
    assert report["target_quarantine_plan"]["apply_supported"] is False
    assert report["target_quarantine_plan"]["backup_required_before_move"] is True
    assert report["db_referenced_files_to_copy_count"] == 4
    assert report["all_source_files_to_copy_count"] == 7
    assert report["unreferenced_files_count"] == 2
    assert report["unreferenced_pdf_count"] == 1
    assert report["unreferenced_pdf_total_bytes"] == 4
    assert report["unreferenced_pdf_examples"] == ["storage/pdf/stub-only.pdf"]
    assert report["unreferenced_pdf_origin_hints"]["tiny_placeholder_or_test_residue"] == 1
    assert report["unreferenced_non_pdf_count"] == 1
    assert report["missing_referenced_files_count"] == 0
    assert report["migration_mode_recommendation"] == "db_referenced_only_plus_required_library_metadata"
    assert report["risk_level"] == "high"
    assert report["apply_executed"] is False
