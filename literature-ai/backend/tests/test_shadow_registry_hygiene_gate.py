from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import d2_shadow_registry_hygiene_gate as hygiene_gate


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
                        "root_path": str(root_path),
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


def _write_sqlite(path: Path, *, papers_total: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE papers (id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO papers DEFAULT VALUES", [tuple()] * papers_total)
        connection.commit()
    finally:
        connection.close()


def test_build_report_flags_stale_shadow(monkeypatch, tmp_path):
    canonical_root = tmp_path / "canonical_library"
    canonical_db = canonical_root / "database.sqlite"
    _write_sqlite(canonical_db, papers_total=15)

    stale_root = tmp_path / "stale_library"
    stale_db = stale_root / "database.sqlite"
    _write_sqlite(stale_db, papers_total=1)

    canonical_registry = tmp_path / "literature-ai" / "data" / "library_registry.json"
    shadow_same = tmp_path / "workspace" / "data" / "library_registry.json"
    shadow_stale = tmp_path / "backend" / "data" / "library_registry.json"
    _write_registry(canonical_registry, root_path=canonical_root)
    _write_registry(shadow_same, root_path=canonical_root)
    _write_registry(shadow_stale, root_path=stale_root)

    monkeypatch.setattr(hygiene_gate, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(hygiene_gate, "shadow_registry_paths", lambda: [shadow_same.resolve(), shadow_stale.resolve()])
    monkeypatch.setattr(
        hygiene_gate,
        "activate_active_library_database",
        lambda: {
            "active_library": "默认文献库",
            "active_library_db_path": str(canonical_db.resolve()),
            "effective_db_path": str(canonical_db.resolve()),
            "recovered_from_candidate_scan": False,
        },
    )
    monkeypatch.setattr(
        hygiene_gate,
        "get_active_database_info",
        lambda: {
            "active_library": "默认文献库",
            "active_library_db_path": str(canonical_db.resolve()),
            "effective_db_path": str(canonical_db.resolve()),
            "recovered_from_candidate_scan": False,
        },
    )

    report = hygiene_gate.build_report()

    assert report["canonical_registry_path"] == str(canonical_registry.resolve())
    assert report["active_database_path"] == str(canonical_db.resolve())
    assert report["active_database_papers_total"] == 15
    assert report["whether_each_shadow_registry_points_to_active_db"][str(shadow_same.resolve())] is True
    assert report["whether_each_shadow_registry_points_to_active_db"][str(shadow_stale.resolve())] is False
    assert report["whether_each_shadow_registry_is_stale_or_dangerous"][str(shadow_stale.resolve())] is True
    assert report["risk_level"] == "medium"


def test_apply_creates_backups_and_reports_without_rewriting_shadow_registry(monkeypatch, tmp_path):
    canonical_root = tmp_path / "canonical_library"
    canonical_db = canonical_root / "database.sqlite"
    _write_sqlite(canonical_db, papers_total=15)

    canonical_registry = tmp_path / "literature-ai" / "data" / "library_registry.json"
    shadow_registry = tmp_path / "workspace" / "data" / "library_registry.json"
    _write_registry(canonical_registry, root_path=canonical_root)
    _write_registry(shadow_registry, root_path=tmp_path / "other_library")
    original_shadow_text = shadow_registry.read_text(encoding="utf-8")

    monkeypatch.setattr(hygiene_gate, "canonical_registry_path", lambda: canonical_registry.resolve())
    monkeypatch.setattr(hygiene_gate, "shadow_registry_paths", lambda: [shadow_registry.resolve()])
    monkeypatch.setattr(
        hygiene_gate,
        "activate_active_library_database",
        lambda: {
            "active_library": "默认文献库",
            "active_library_db_path": str(canonical_db.resolve()),
            "effective_db_path": str(canonical_db.resolve()),
            "recovered_from_candidate_scan": False,
        },
    )
    monkeypatch.setattr(
        hygiene_gate,
        "get_active_database_info",
        lambda: {
            "active_library": "默认文献库",
            "active_library_db_path": str(canonical_db.resolve()),
            "effective_db_path": str(canonical_db.resolve()),
            "recovered_from_candidate_scan": False,
        },
    )

    result = hygiene_gate.apply_hygiene()

    assert result["apply_executed"] is True
    assert str(canonical_registry.resolve()) in result["backups"]
    assert str(shadow_registry.resolve()) in result["backups"]
    for path in result["generated_shadow_reports"]:
        assert Path(path).exists()
    assert shadow_registry.read_text(encoding="utf-8") == original_shadow_text
