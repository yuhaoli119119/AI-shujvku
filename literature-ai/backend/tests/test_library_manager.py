from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.library_manager import (
    DEFAULT_LIBRARY_NAME,
    LEGACY_STORAGE_MODE,
    LibraryManager,
    SHARED_STORAGE_MODE,
)


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    default_root = tmp_path / "default-library"
    monkeypatch.setattr(LibraryManager, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(LibraryManager, "DEFAULT_LIBRARY_ROOT", default_root)
    return LibraryManager()


def test_create_library_builds_shared_project_layout(isolated_manager):
    root = Path(isolated_manager.create_library("共享库").root_path)

    assert (root / "database.sqlite").exists()
    assert (root / "papers" / "pdf").exists()
    assert (root / "papers" / "docling_json").exists()
    assert (root / "config" / "project_config.json").exists()

    config = json.loads((root / "config" / "project_config.json").read_text(encoding="utf-8"))
    meta = json.loads((root / "library.json").read_text(encoding="utf-8"))
    assert config["project_name"] == "共享库"
    assert meta["storage_mode"] == SHARED_STORAGE_MODE


def test_import_desktop_project_detects_shared_storage(isolated_manager, monkeypatch, tmp_path):
    root = tmp_path / "desktop-project"
    (root / "papers" / "pdf").mkdir(parents=True)
    (root / "papers" / "tei").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "project_config.json").write_text(
        json.dumps({"project_name": "桌面项目"}, ensure_ascii=False),
        encoding="utf-8",
    )

    imported = isolated_manager.import_library(str(root))
    calls: list[tuple[str, str]] = []

    def fake_switch_database(database_url: str, storage_root: str | None = None) -> None:
        calls.append((database_url, storage_root or ""))

    monkeypatch.setattr("app.db.session.switch_database", fake_switch_database)
    activated = isolated_manager.activate_library(imported.name)

    assert activated.name == "桌面项目"
    assert calls
    assert calls[-1][1].endswith(str(Path("papers")))


def test_default_library_stays_on_legacy_storage(isolated_manager, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_switch_database(database_url: str, storage_root: str | None = None) -> None:
        calls.append((database_url, storage_root or ""))

    monkeypatch.setattr("app.db.session.switch_database", fake_switch_database)
    isolated_manager.activate_library(DEFAULT_LIBRARY_NAME)

    assert calls
    assert calls[-1][1].endswith(str(Path(LEGACY_STORAGE_MODE)))
