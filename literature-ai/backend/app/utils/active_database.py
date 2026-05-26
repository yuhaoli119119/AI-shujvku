from __future__ import annotations

from pathlib import Path
from typing import Any


def _kind_from_url(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql"):
        return "postgresql"
    return "unknown"


def _sqlite_path(database_url: str) -> str | None:
    if not database_url.startswith("sqlite:///"):
        return None
    return str(Path(database_url.removeprefix("sqlite:///")).resolve())


def _mask_url(database_url: str) -> str:
    if "@" in database_url:
        return database_url.split("@")[-1]
    if database_url.startswith("sqlite:///"):
        path = _sqlite_path(database_url)
        return f"sqlite:///{Path(path).name if path else 'database.sqlite'}"
    return "***"


def get_active_database_info() -> dict[str, Any]:
    """Return safe runtime DB source-of-truth metadata for scripts and APIs."""
    from app.config import get_settings
    from app.services.library_manager import LibraryManager

    settings = get_settings()
    database_url = settings.database_url
    kind = _kind_from_url(database_url)
    path = _sqlite_path(database_url)
    active_library: str | None = None
    active_library_database_path: str | None = None

    try:
        active = LibraryManager().get_active_library()
        if active:
            active_library = active.name
            active_library_database_path = str((Path(active.root_path) / "database.sqlite").resolve())
    except Exception:
        active = None

    return {
        "db_kind": kind,
        "db_path": path,
        "db_url_masked": _mask_url(database_url),
        "active_library": active_library,
        "active_library_db_path": active_library_database_path,
        "matches_active_library_db_path": bool(
            kind == "sqlite"
            and path is not None
            and active_library_database_path is not None
            and Path(path) == Path(active_library_database_path)
        ),
        "is_active_library_sqlite": kind == "sqlite" and path is not None and Path(path).name == "database.sqlite",
    }


def activate_active_library_database() -> dict[str, Any]:
    """Switch process settings to the registered active library SQLite DB if available."""
    from app.services.library_manager import LibraryManager

    manager = LibraryManager()
    active = manager.get_active_library()
    if active:
        manager.activate_library(active.name)
    return get_active_database_info()


def require_active_library_sqlite() -> dict[str, Any]:
    info = get_active_database_info()
    if not info["is_active_library_sqlite"]:
        raise RuntimeError(
            "Expected active library SQLite database, got "
            f"kind={info['db_kind']} path={info['db_path']} active_library_db_path={info['active_library_db_path']}"
        )
    return info
