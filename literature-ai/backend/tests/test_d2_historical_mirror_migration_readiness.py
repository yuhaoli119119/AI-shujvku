from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import d2_historical_mirror_migration_readiness as readiness


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


def _write_sqlite(path: Path) -> None:
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
        connection.executemany(
            "INSERT INTO papers (id, title, pdf_path, tei_path, docling_json_path, markdown_path) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "paper-1",
                    "Ready paper",
                    "storage/pdf/ready.pdf",
                    "storage/tei/ready.tei.xml",
                    "storage/docling_json/ready.docling.json",
                    "storage/markdown/ready.md",
                ),
                (
                    "paper-2",
                    "Missing markdown",
                    "storage/pdf/missing.pdf",
                    None,
                    None,
                    "storage/markdown/missing.md",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_artifact(path: Path, content: str = "sample") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_report_detects_conflicts_and_missing_files(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "literature-ai"
    backend_root = project_root / "backend"
    current_root = backend_root / "D\uf03a\uf05cDesktop\uf05ctest\uf05clibraries\uf05cdefault"
    proposed_root = project_root / "data" / "libraries" / "default"
    canonical_registry = project_root / "data" / "library_registry.json"
    shadow_registry = workspace_root / "data" / "library_registry.json"

    _write_sqlite(current_root / "database.sqlite")
    _write_artifact(current_root / "storage" / "pdf" / "ready.pdf")
    _write_artifact(current_root / "storage" / "tei" / "ready.tei.xml")
    _write_artifact(current_root / "storage" / "docling_json" / "ready.docling.json")
    _write_artifact(current_root / "storage" / "markdown" / "ready.md")
    _write_artifact(current_root / "storage" / "figures" / "figure-1.png")
    _write_artifact(proposed_root / "database.sqlite", "stale-db")
    _write_artifact(proposed_root / "storage" / "pdf" / "ready.pdf", "conflict")

    _write_registry(canonical_registry, root_path=current_root)
    _write_registry(shadow_registry, root_path=proposed_root)

    monkeypatch.setattr(readiness, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(readiness, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(readiness, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(readiness, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(readiness, "default_library_root", lambda: proposed_root.resolve())
    monkeypatch.setattr(readiness, "shadow_registry_paths", lambda: [shadow_registry.resolve()])

    report = readiness.build_report()

    assert report["current_active_library_root"] == str(current_root.resolve())
    assert report["proposed_canonical_library_root"] == str(proposed_root.resolve())
    assert report["files_to_copy_by_type"]["database.sqlite"] == 1
    assert report["files_to_copy_by_type"]["pdf"] == 1
    assert report["files_to_copy_by_type"]["figures"] == 1
    assert report["artifact_paths_already_canonical_count"] == 4
    assert report["artifact_paths_needing_update_count"] == 2
    assert report["sqlite_integrity_check_result"] == "ok"
    assert report["active_db_papers_total"] == 2
    assert report["migration_risk_level"] == "high"
    assert any(item["relative_path"] == "database.sqlite" for item in report["path_conflicts"])
    assert any(item["field"] == "markdown_path" for item in report["missing_files"])
    assert report["sha256_stability"]["unchanged"] is True


def test_build_report_is_cwd_stable(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "literature-ai"
    backend_root = project_root / "backend"
    current_root = backend_root / "D\uf03a\uf05cDesktop\uf05ctest\uf05clibraries\uf05cdefault"
    proposed_root = project_root / "data" / "libraries" / "default"
    canonical_registry = project_root / "data" / "library_registry.json"

    _write_sqlite(current_root / "database.sqlite")
    _write_registry(canonical_registry, root_path=current_root)

    monkeypatch.setattr(readiness, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(readiness, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(readiness, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(readiness, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(readiness, "default_library_root", lambda: proposed_root.resolve())
    monkeypatch.setattr(readiness, "shadow_registry_paths", lambda: [])

    monkeypatch.chdir(workspace_root)
    report_from_workspace = readiness.build_report()
    monkeypatch.chdir(project_root)
    report_from_project = readiness.build_report()
    monkeypatch.chdir(backend_root)
    report_from_backend = readiness.build_report()

    assert report_from_workspace["current_active_database_path"] == report_from_project["current_active_database_path"]
    assert report_from_project["current_active_database_path"] == report_from_backend["current_active_database_path"]
    assert report_from_workspace["proposed_canonical_library_root"] == report_from_backend["proposed_canonical_library_root"]
