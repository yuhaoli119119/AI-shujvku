from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Base, Paper
import app.services.library_manager as library_manager_module
from app.utils import active_database as active_database_module
from app.utils.artifact_paths import resolve_persisted_artifact_path


def _write_sqlite(path: Path, *, paper_count: int) -> None:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    if paper_count:
        with Session(engine, future=True) as session:
            for index in range(paper_count):
                session.add(Paper(title=f"paper-{index}", pdf_path=f"paper-{index}.pdf", authors=[]))
            session.commit()
    engine.dispose()


def test_activate_active_library_database_recovers_populated_mirror_sqlite(tmp_path, monkeypatch):
    workspace_root = tmp_path
    backend_root = workspace_root / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)

    library_root = workspace_root / "data" / "libraries" / "default"
    (library_root / "storage").mkdir(parents=True, exist_ok=True)
    _write_sqlite(library_root / "database.sqlite", paper_count=0)

    mirrored_root = backend_root / active_database_module._library_root_mirror_segment(library_root)
    (mirrored_root / "storage").mkdir(parents=True, exist_ok=True)
    _write_sqlite(mirrored_root / "database.sqlite", paper_count=1)

    registry_path = workspace_root / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": 2,
                "active_library": "默认文献库",
                "libraries": [
                    {
                        "name": "默认文献库",
                        "root_path": str(library_root.resolve()),
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

    monkeypatch.setenv("LITAI_DATABASE_URL", "postgresql+psycopg://user:pass@localhost/test")
    get_settings.cache_clear()
    monkeypatch.setattr(library_manager_module.LibraryManager, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(library_manager_module.LibraryManager, "DEFAULT_LIBRARY_ROOT", library_root)
    monkeypatch.setattr(active_database_module, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(active_database_module, "WORKSPACE_ROOT", workspace_root)

    info = active_database_module.activate_active_library_database()

    assert info["db_kind"] == "sqlite"
    assert Path(info["db_path"]) == (mirrored_root / "database.sqlite").resolve()
    assert info["effective_db_papers_total"] == 1
    assert info["recovered_from_candidate_scan"] is True


def test_resolve_persisted_artifact_path_finds_mirrored_file(tmp_path, monkeypatch):
    workspace_root = tmp_path
    backend_root = workspace_root / "backend"
    backend_root.mkdir(parents=True, exist_ok=True)

    actual_library_root = Path(r"D:\Desktop\代码开发\AI检索数据库\literature-ai\backend\data\libraries\default")
    mirrored_root = backend_root / active_database_module._library_root_mirror_segment(actual_library_root)
    target = mirrored_root / "storage" / "markdown" / "sample.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sample evidence", encoding="utf-8")

    monkeypatch.setattr(active_database_module, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(active_database_module, "WORKSPACE_ROOT", workspace_root)
    from app.utils import artifact_paths as artifact_paths_module

    monkeypatch.setattr(artifact_paths_module, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(artifact_paths_module, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(workspace_root / "empty-storage"))
    get_settings.cache_clear()

    resolved = resolve_persisted_artifact_path(
        "/app/D:\\Desktop\\代码开发\\AI检索数据库\\literature-ai\\backend\\data\\libraries\\default\\storage\\markdown\\sample.md",
        category="markdown",
    )

    assert resolved == target.resolve()
