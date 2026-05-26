"""End-to-end CRUD coverage for LibraryManager without relying on cwd."""

import json
import shutil
import tempfile
from pathlib import Path

from app.services.library_manager import DEFAULT_LIBRARY_NAME, LibraryManager
from app.utils.project_paths import canonical_registry_path


def test_library_manager():
    original_registry = canonical_registry_path()
    original_data = original_registry.read_text(encoding="utf-8") if original_registry.exists() else None

    tmp_dir = Path(tempfile.mkdtemp(prefix="litai_test_"))
    try:
        LibraryManager.REGISTRY_PATH = tmp_dir / "library_registry.json"
        LibraryManager.DEFAULT_LIBRARY_ROOT = tmp_dir / "libraries" / "default"

        manager = LibraryManager()

        libs = manager.list_libraries()
        assert len(libs) >= 1
        assert libs[0].is_active
        assert libs[0].name == DEFAULT_LIBRARY_NAME

        created = manager.create_library(name="test_library", description="automation test")
        assert created.name == "test_library"
        assert created.paper_count == 0
        assert created.is_active is False

        root = Path(created.root_path)
        assert root.exists()
        assert (root / "database.sqlite").exists()
        assert (root / "library.json").exists()
        assert (root / "papers").exists()
        for subdir in ("pdf", "text", "tei", "docling_json", "figures", "tables", "markdown"):
            assert (root / "papers" / subdir).exists()

        activated = manager.activate_library("test_library")
        assert activated.is_active is True
        assert sum(1 for item in manager.list_libraries() if item.is_active) == 1

        reactivated = manager.activate_library(DEFAULT_LIBRARY_NAME)
        assert reactivated.is_active is True

        import_dir = tmp_dir / "import_test"
        (import_dir / "papers" / "pdf").mkdir(parents=True, exist_ok=True)
        (import_dir / "config").mkdir(parents=True, exist_ok=True)
        (import_dir / "config" / "project_config.json").write_text(
            json.dumps({"project_name": "import_test_library"}),
            encoding="utf-8",
        )
        (import_dir / "library.json").write_text(
            json.dumps(
                {
                    "name": "import_test_library",
                    "description": "imported",
                    "storage_mode": "papers",
                    "library_kind": "shared_project",
                    "created_at": "2026-01-01T00:00:00",
                }
            ),
            encoding="utf-8",
        )

        imported = manager.import_library(str(import_dir))
        assert imported.name == "import_test_library"
        assert imported.is_active is False

        removed_import = manager.unregister_library("import_test_library")
        assert removed_import.name == "import_test_library"
        assert import_dir.exists()

        removed_created = manager.unregister_library("test_library")
        assert removed_created.name == "test_library"
        assert len(manager.list_libraries()) == 1

        try:
            manager.unregister_library(DEFAULT_LIBRARY_NAME)
            assert False, "default library removal should fail"
        except ValueError:
            pass

        try:
            manager.create_library(name="duplicate_library")
            manager.create_library(name="duplicate_library")
            assert False, "duplicate library creation should fail"
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if original_data is not None:
            original_registry.write_text(original_data, encoding="utf-8")
