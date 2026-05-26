from __future__ import annotations

import json
from pathlib import Path

from scripts import d2_controlled_historical_mirror_migration as migration


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_registry(path: Path, *, active_library: str, root_path: Path) -> None:
    _write_file(
        path,
        json.dumps(
            {
                "version": 2,
                "active_library": active_library,
                "libraries": [
                    {
                        "name": active_library,
                        "root_path": str(root_path.resolve()),
                        "description": active_library,
                        "created_at": "2026-05-26T00:00:00",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def _referenced_artifact_entries(source_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for artifact_type, extension in (
        ("pdf", ".pdf"),
        ("markdown", ".md"),
        ("tei", ".tei.xml"),
        ("docling_json", ".docling.json"),
    ):
        for index in range(1, 7):
            relative_path = f"storage/{artifact_type}/paper-{index}{extension}"
            absolute_path = source_root / relative_path
            _write_file(absolute_path, f"{artifact_type}-{index}")
            entries.append(
                {
                    "type": artifact_type,
                    "relative_path": relative_path,
                    "absolute_path": str(absolute_path.resolve()),
                    "bytes": int(absolute_path.stat().st_size),
                }
            )
    return entries


def _configure_happy_path(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "literature-ai"
    source_root = project_root / "historical-mirror-root"
    target_root = project_root / "data" / "libraries" / "default"
    canonical_registry = project_root / "data" / "library_registry.json"
    backup_root = workspace_root / "backups"
    active_library = "default"

    _write_file(source_root / "database.sqlite", "sqlite-bytes")
    _write_file(source_root / "library.json", '{"name":"default","storage_mode":"library"}')
    db_referenced_files = _referenced_artifact_entries(source_root)
    _write_file(source_root / "storage" / "pdf" / "extra.pdf", "extra")
    _write_file(source_root / "storage" / "figures" / "figure-1.png", "figure")
    _write_registry(canonical_registry, active_library=active_library, root_path=source_root)

    readiness_report = {
        "active_library": active_library,
        "current_active_library_root": str(source_root.resolve()),
        "current_active_database_path": str((source_root / "database.sqlite").resolve()),
        "proposed_canonical_library_root": str(target_root.resolve()),
        "required_library_metadata_files": [
            {
                "path": str((source_root / "library.json").resolve()),
            }
        ],
        "db_referenced_files": db_referenced_files,
        "active_db_papers_total": 15,
        "missing_referenced_files_count": 0,
        "duplicate_artifact_paths_count": 0,
        "duplicate_artifact_paths": [],
        "unreferenced_files_count": 2,
        "source_root_is_historical_mirror": True,
    }
    gate_report = {
        "target_conflicts_count": 0,
    }
    active_db_info = {
        "db_kind": "sqlite",
        "recovered_from_candidate_scan": False,
        "active_library_db_path": str((source_root / "database.sqlite").resolve()),
        "effective_db_path": str((source_root / "database.sqlite").resolve()),
        "effective_db_papers_total": 15,
    }

    monkeypatch.setattr(migration, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(migration, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(migration, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(migration, "default_library_root", lambda: target_root.resolve())
    monkeypatch.setattr(migration.readiness, "build_report", lambda: readiness_report)
    monkeypatch.setattr(migration.gate, "build_report", lambda: gate_report)
    monkeypatch.setattr(migration, "get_active_database_info", lambda: active_db_info)

    return {
        "workspace_root": workspace_root,
        "project_root": project_root,
        "source_root": source_root,
        "target_root": target_root,
        "canonical_registry": canonical_registry,
        "backup_root": backup_root,
        "active_library": active_library,
    }


def test_dry_run_writes_nothing_by_default(monkeypatch, tmp_path):
    env = _configure_happy_path(monkeypatch, tmp_path)
    before_files = sorted(path.relative_to(env["workspace_root"]).as_posix() for path in env["workspace_root"].rglob("*"))
    before_registry = env["canonical_registry"].read_text(encoding="utf-8")

    report = migration.build_report()

    after_files = sorted(path.relative_to(env["workspace_root"]).as_posix() for path in env["workspace_root"].rglob("*"))
    assert report["apply_executed"] is False
    assert report["mode"] == "dry_run"
    assert env["target_root"].exists() is False
    assert env["canonical_registry"].read_text(encoding="utf-8") == before_registry
    assert before_files == after_files
    assert (env["backup_root"] / migration.BACKUP_DIRNAME).exists() is False


def test_copy_plan_includes_only_referenced_artifacts_and_required_metadata(monkeypatch, tmp_path):
    env = _configure_happy_path(monkeypatch, tmp_path)

    report = migration.build_report()

    planned_relatives = {item["relative_path"] for item in report["copy_plan"]}
    assert "database.sqlite" in planned_relatives
    assert "library.json" in planned_relatives
    assert "storage/pdf/extra.pdf" not in planned_relatives
    assert "storage/figures/figure-1.png" not in planned_relatives
    assert report["copy_plan_summary"]["db_referenced_artifacts_count"] == 24
    assert report["copy_plan_summary"]["db_referenced_artifacts_by_type"] == {
        "pdf": 6,
        "markdown": 6,
        "tei": 6,
        "docling_json": 6,
    }
    assert report["copy_plan_summary"]["includes_unreferenced_files"] is False
    assert report["skipped_unreferenced_files_count"] == 2


def test_apply_is_blocked_when_target_conflicts_exist(monkeypatch, tmp_path):
    env = _configure_happy_path(monkeypatch, tmp_path)
    monkeypatch.setattr(migration.gate, "build_report", lambda: {"target_conflicts_count": 2})
    registry_before = env["canonical_registry"].read_text(encoding="utf-8")

    report = migration.build_report(apply=True)

    assert report["apply_executed"] is False
    assert report["error"] == "apply_blocked_by_preconditions"
    assert "target_conflicts_count=2 (expected 0)" in report["apply_preconditions"]["failures"]
    assert env["canonical_registry"].read_text(encoding="utf-8") == registry_before


def test_apply_is_blocked_when_referenced_artifact_is_missing(monkeypatch, tmp_path):
    env = _configure_happy_path(monkeypatch, tmp_path)
    base_report = migration.readiness.build_report()
    failing_report = dict(base_report)
    failing_report["missing_referenced_files_count"] = 1
    monkeypatch.setattr(migration.readiness, "build_report", lambda: failing_report)
    registry_before = env["canonical_registry"].read_text(encoding="utf-8")

    report = migration.build_report(apply=True)

    assert report["apply_executed"] is False
    assert report["error"] == "apply_blocked_by_preconditions"
    assert "missing referenced artifacts=1 (expected 0)" in report["apply_preconditions"]["failures"]
    assert env["canonical_registry"].read_text(encoding="utf-8") == registry_before


def test_registry_update_waits_until_copy_and_hash_verification_pass(monkeypatch, tmp_path):
    env = _configure_happy_path(monkeypatch, tmp_path)
    events: list[str] = []
    registry_before = env["canonical_registry"].read_text(encoding="utf-8")

    def fake_copy_files(copy_plan):
        events.append("copy")
        return [item["relative_path"] for item in copy_plan]

    def fake_verify_hash_match(copy_plan):
        events.append("verify")
        return False, {item["relative_path"]: {"source_sha256": "a", "target_sha256": "b"} for item in copy_plan}

    def fake_update(**kwargs):
        events.append("update")

    monkeypatch.setattr(migration, "_copy_files", fake_copy_files)
    monkeypatch.setattr(migration, "_verify_hash_match", fake_verify_hash_match)
    monkeypatch.setattr(migration, "_update_canonical_registry_root", fake_update)

    report = migration.build_report(apply=True)

    assert report["apply_executed"] is False
    assert report["error"] == "copied_file_hash_mismatch"
    assert events == ["copy", "verify"]
    assert env["canonical_registry"].read_text(encoding="utf-8") == registry_before


def test_restore_registry_backup_restores_original_contents(tmp_path):
    canonical_registry = tmp_path / "library_registry.json"
    registry_backup = tmp_path / "library_registry.json.bak"
    _write_file(canonical_registry, '{"active_library":"new"}')
    _write_file(registry_backup, '{"active_library":"old"}')

    migration.restore_registry_backup(
        registry_backup_path=registry_backup,
        canonical_registry=canonical_registry,
    )

    assert canonical_registry.read_text(encoding="utf-8") == '{"active_library":"old"}'
