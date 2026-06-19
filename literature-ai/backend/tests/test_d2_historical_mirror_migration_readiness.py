from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.utils import active_database as active_database_module
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


def _write_sqlite(
    path: Path,
    *,
    rows: list[tuple[str, str, str | None, str | None, str | None, str | None]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = rows or [
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
    ]
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
            rows,
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
    monkeypatch.setattr(active_database_module, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(
        readiness,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str((current_root / "database.sqlite").resolve()),
            "effective_db_path": str((current_root / "database.sqlite").resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": False,
        },
    )

    report = readiness.build_report()

    assert report["current_active_library_root"] == str(current_root.resolve())
    assert report["proposed_canonical_library_root"] == str(proposed_root.resolve())
    assert report["files_to_copy_by_type"]["database.sqlite"] == 1
    assert report["files_to_copy_by_type"]["pdf"] == 1
    assert report["files_to_copy_by_type"]["figures"] == 1
    assert report["all_source_files_to_copy_count"] == 6
    assert report["db_referenced_files_to_copy_count"] == 4
    assert report["unreferenced_files_count"] == 1
    assert report["source_file_inventory_db_referenced"]["total_files"] == 4
    assert report["source_file_inventory_unreferenced"]["total_files"] == 1
    assert report["unreferenced_pdf_count"] == 0
    assert report["unreferenced_non_pdf_count"] == 1
    assert report["missing_referenced_files_count"] == 2
    assert report["migration_mode_recommendation"] == "blocked_until_missing_referenced_files_are_resolved"
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
    monkeypatch.setattr(active_database_module, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(
        readiness,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str((current_root / "database.sqlite").resolve()),
            "effective_db_path": str((current_root / "database.sqlite").resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": False,
        },
    )

    monkeypatch.chdir(workspace_root)
    report_from_workspace = readiness.build_report()
    monkeypatch.chdir(project_root)
    report_from_project = readiness.build_report()
    monkeypatch.chdir(backend_root)
    report_from_backend = readiness.build_report()

    assert report_from_workspace["current_active_database_path"] == report_from_project["current_active_database_path"]
    assert report_from_project["current_active_database_path"] == report_from_backend["current_active_database_path"]
    assert report_from_workspace["proposed_canonical_library_root"] == report_from_backend["proposed_canonical_library_root"]
    assert report_from_workspace["all_source_files_to_copy_count"] == report_from_backend["all_source_files_to_copy_count"]
    assert report_from_workspace["db_referenced_files_to_copy_count"] == report_from_backend["db_referenced_files_to_copy_count"]
    assert report_from_workspace["migration_mode_recommendation"] == report_from_backend["migration_mode_recommendation"]


def _configure_post_migration(monkeypatch, tmp_path, *, recovered: bool = False, missing_artifact: bool = False):
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "literature-ai"
    backend_root = project_root / "backend"
    target_root = project_root / "data" / "libraries" / "default"
    canonical_registry = project_root / "data" / "library_registry.json"

    rows = []
    for index in range(15):
        rows.append(
            (
                f"paper-{index}",
                f"Paper {index}",
                "storage/pdf/ready.pdf" if index == 0 else None,
                "storage/tei/ready.tei.xml" if index == 0 else None,
                "storage/docling_json/ready.docling.json" if index == 0 else None,
                "storage/markdown/ready.md" if index == 0 else None,
            )
        )
    if missing_artifact:
        rows[0] = (
            "paper-0",
            "Paper 0",
            "storage/pdf/missing.pdf",
            "storage/tei/ready.tei.xml",
            "storage/docling_json/ready.docling.json",
            "storage/markdown/ready.md",
        )

    _write_sqlite(target_root / "database.sqlite", rows=rows)
    _write_artifact(target_root / "library.json", '{"name":"default","storage_mode":"library"}')
    _write_artifact(target_root / "storage" / "pdf" / "ready.pdf")
    _write_artifact(target_root / "storage" / "tei" / "ready.tei.xml")
    _write_artifact(target_root / "storage" / "docling_json" / "ready.docling.json")
    _write_artifact(target_root / "storage" / "markdown" / "ready.md")
    historical_root = backend_root / "D\uf03a\uf05cDesktop\uf05ctest\uf05clibraries\uf05cdefault"
    _write_artifact(historical_root / "library.json", '{"name":"legacy"}')

    _write_registry(canonical_registry, root_path=target_root)

    monkeypatch.setattr(readiness, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(readiness, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(readiness, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(readiness, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(readiness, "default_library_root", lambda: target_root.resolve())
    monkeypatch.setattr(readiness, "shadow_registry_paths", lambda: [])
    monkeypatch.setattr(active_database_module, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(
        readiness,
        "get_active_database_info",
        lambda: {
            "active_library_db_path": str((target_root / "database.sqlite").resolve()),
            "effective_db_path": str((target_root / "database.sqlite").resolve()),
            "db_kind": "sqlite",
            "recovered_from_candidate_scan": recovered,
        },
    )
    return target_root, historical_root


def test_post_migration_readiness_reports_complete_without_apply(monkeypatch, tmp_path):
    target_root, historical_root = _configure_post_migration(monkeypatch, tmp_path)

    report = readiness.build_report()

    assert report["migration_phase"] == "post_migration"
    assert report["migration_complete"] is True
    assert report["migration_action_required"] is False
    assert report["apply_should_run"] is False
    assert report["readiness_result"] == "complete"
    assert report["active_root_status"] == "canonical_target_active"
    assert report["historical_mirror_status"] == "legacy_retained_not_active"
    assert report["recommended_next_gate"] == "none_or_post_migration_monitoring"
    assert report["migration_mode_recommendation"] == "already_migrated_no_apply"
    assert report["target_conflicts_count"] == 0
    assert report["expected_active_files_count"] == 2
    assert report["db_referenced_files_present"] is True
    assert report["current_active_library_root"] == str(target_root.resolve())
    assert historical_root.exists()


def test_post_migration_recovered_candidate_scan_reports_attention(monkeypatch, tmp_path):
    _configure_post_migration(monkeypatch, tmp_path, recovered=True)

    report = readiness.build_report()

    assert report["migration_phase"] == "post_migration"
    assert report["migration_complete"] is False
    assert report["migration_action_required"] is True
    assert report["apply_should_run"] is False
    assert report["readiness_result"] == "post_migration_attention_required"
    assert "recovered_from_candidate_scan_true" in report["post_migration_risk_reasons"]


def test_post_migration_missing_referenced_artifact_reports_attention(monkeypatch, tmp_path):
    _configure_post_migration(monkeypatch, tmp_path, missing_artifact=True)

    report = readiness.build_report()

    assert report["migration_phase"] == "post_migration"
    assert report["migration_complete"] is False
    assert report["db_referenced_files_present"] is False
    assert report["missing_referenced_files_count"] == 1
    assert "missing_referenced_files" in report["post_migration_risk_reasons"]
