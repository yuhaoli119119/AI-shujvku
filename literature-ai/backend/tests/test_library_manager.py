from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
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
    monkeypatch.setenv("LITAI_FORCE_CONFIGURED_DATABASE", "false")
    get_settings.cache_clear()
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


def test_activate_library_tolerates_read_only_access_metadata_updates(isolated_manager, monkeypatch, tmp_path):
    root = tmp_path / "desktop-project"
    (root / "papers" / "pdf").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "project_config.json").write_text(
        json.dumps({"project_name": "桌面项目"}, ensure_ascii=False),
        encoding="utf-8",
    )

    imported = isolated_manager.import_library(str(root))
    calls: list[tuple[str, str]] = []

    def fake_switch_database(database_url: str, storage_root: str | None = None) -> None:
        calls.append((database_url, storage_root or ""))

    def raise_meta_permission(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(root / "library.json"))

    def raise_config_permission(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(root / "config" / "project_config.json"))

    monkeypatch.setattr("app.db.session.switch_database", fake_switch_database)
    monkeypatch.setattr(LibraryManager, "_write_library_meta", raise_meta_permission)
    monkeypatch.setattr(LibraryManager, "_ensure_shared_project_config", raise_config_permission)

    activated = isolated_manager.activate_library(imported.name)

    assert activated.name == "桌面项目"
    assert activated.is_active is True
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


def test_force_configured_database_does_not_create_or_switch_sqlite(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    default_root = tmp_path / "default-library"
    monkeypatch.setattr(LibraryManager, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(LibraryManager, "DEFAULT_LIBRARY_ROOT", default_root)
    monkeypatch.setenv("LITAI_DATABASE_URL", "postgresql+psycopg://user:pass@127.0.0.1:1/test?connect_timeout=1")
    monkeypatch.setenv("LITAI_FORCE_CONFIGURED_DATABASE", "true")
    get_settings.cache_clear()

    calls: list[tuple[str, str]] = []

    def fake_switch_database(database_url: str, storage_root: str | None = None) -> None:
        calls.append((database_url, storage_root or ""))

    monkeypatch.setattr("app.db.session.switch_database", fake_switch_database)
    manager = LibraryManager()
    activated = manager.activate_library(DEFAULT_LIBRARY_NAME)

    assert activated.is_active is True
    assert calls == []
    assert not (default_root / "database.sqlite").exists()
    get_settings.cache_clear()


def test_create_library_uses_selected_parent_directory(isolated_manager, tmp_path):
    parent = tmp_path / "custom-parent"
    parent.mkdir(parents=True, exist_ok=True)

    created = isolated_manager.create_library("共享库", root_path=str(parent))

    assert Path(created.root_path) == (parent / "共享库").resolve()
    assert (parent / "共享库" / "database.sqlite").exists()


def test_import_rejects_plain_directory_without_library_markers(isolated_manager, tmp_path):
    plain_dir = tmp_path / "plain-folder"
    plain_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="not an existing library root"):
        isolated_manager.import_library(str(plain_dir))


def test_registry_maps_container_data_paths_to_manager_data_root(tmp_path, monkeypatch):
    registry_path = tmp_path / "data" / "library_registry.json"
    default_root = tmp_path / "data" / "libraries" / "default"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 2,
                "active_library": "graphdyne-dft",
                "libraries": [
                    {
                        "name": DEFAULT_LIBRARY_NAME,
                        "root_path": "/data/libraries/default",
                        "description": DEFAULT_LIBRARY_NAME,
                        "created_at": "2026-06-02T00:00:00",
                    },
                    {
                        "name": "graphdyne-dft",
                        "root_path": "/data/libraries/graphdyne-dft",
                        "description": "graphdyne-dft",
                        "created_at": "2026-06-02T00:00:00",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(LibraryManager, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(LibraryManager, "DEFAULT_LIBRARY_ROOT", default_root)

    LibraryManager()

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    paths = {item["name"]: item["root_path"] for item in payload["libraries"]}
    assert paths[DEFAULT_LIBRARY_NAME] == str(default_root.resolve())
    assert paths["graphdyne-dft"] == "/data/libraries/graphdyne-dft"
    listed = {item.name: item.root_path for item in LibraryManager().list_libraries()}
    assert listed["graphdyne-dft"] == "/data/libraries/graphdyne-dft"
