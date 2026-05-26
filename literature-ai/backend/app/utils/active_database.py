from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_ROOT.parent
WINDOWS_MIRROR_COLON = "\uf03a"
WINDOWS_MIRROR_SEP = "\uf05c"


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


def _library_root_mirror_segment(library_root: Path) -> str:
    return str(library_root.resolve()).replace(":", WINDOWS_MIRROR_COLON).replace("\\", WINDOWS_MIRROR_SEP).replace("/", WINDOWS_MIRROR_SEP)


def _sqlite_candidate_summary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    summary = {
        "path": str(resolved),
        "exists": resolved.exists(),
        "has_papers_table": False,
        "table_count": 0,
        "papers_total": 0,
    }
    if not resolved.exists():
        return summary
    try:
        connection = sqlite3.connect(str(resolved))
        try:
            cursor = connection.cursor()
            tables = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            summary["table_count"] = len(tables)
            summary["has_papers_table"] = "papers" in tables
            if "papers" in tables:
                summary["papers_total"] = int(cursor.execute("SELECT COUNT(*) FROM papers").fetchone()[0] or 0)
        finally:
            connection.close()
    except sqlite3.Error:
        return summary
    return summary


def _candidate_sqlite_paths(active_library_root: Path | None, configured_sqlite_path: Path | None) -> list[Path]:
    candidates: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    add(configured_sqlite_path)
    if active_library_root is not None:
        add(active_library_root / "database.sqlite")
        add(BACKEND_ROOT / _library_root_mirror_segment(active_library_root) / "database.sqlite")

    for root in (
        WORKSPACE_ROOT / "data" / "libraries",
        BACKEND_ROOT / "data" / "libraries",
        BACKEND_ROOT,
    ):
        if not root.exists():
            continue
        for path in root.rglob("database.sqlite"):
            add(path)
    return candidates


def _choose_effective_sqlite_candidate(
    *,
    active_library_root: Path | None,
    configured_sqlite_path: Path | None,
) -> dict[str, Any] | None:
    preferred_registered = (active_library_root / "database.sqlite").resolve() if active_library_root is not None else None
    preferred_configured = configured_sqlite_path.resolve() if configured_sqlite_path is not None else None

    best: dict[str, Any] | None = None
    for candidate in _candidate_sqlite_paths(active_library_root, configured_sqlite_path):
        summary = _sqlite_candidate_summary(candidate)
        score = 0
        if summary["has_papers_table"]:
            score += 200
        if summary["table_count"] > 0:
            score += 20
        if summary["papers_total"] > 0:
            score += 1000 + min(int(summary["papers_total"]), 500)
        if preferred_registered is not None and candidate == preferred_registered:
            score += 40
        if preferred_configured is not None and candidate == preferred_configured:
            score += 25
        if active_library_root is not None and active_library_root in candidate.parents:
            score += 10

        summary["score"] = score
        if best is None or score > best["score"]:
            best = summary
    return best


def _effective_storage_root(effective_db_path: Path | None) -> str | None:
    if effective_db_path is None:
        return None
    preferred = effective_db_path.parent / "storage"
    if preferred.exists():
        return str(preferred.resolve())
    fallback = WORKSPACE_ROOT / "storage"
    if fallback.exists():
        return str(fallback.resolve())
    return str(preferred.resolve())


def get_active_database_info() -> dict[str, Any]:
    """Return safe runtime DB source-of-truth metadata for scripts and APIs."""
    from app.config import get_settings
    from app.services.library_manager import LibraryManager

    settings = get_settings()
    database_url = settings.database_url
    kind = _kind_from_url(database_url)
    path = _sqlite_path(database_url)
    configured_sqlite_path = Path(path).resolve() if path else None
    active_library: str | None = None
    active_library_database_path: str | None = None
    active_library_root: Path | None = None

    try:
        active = LibraryManager().get_active_library()
        if active:
            active_library = active.name
            active_library_root = Path(active.root_path).resolve()
            active_library_database_path = str((active_library_root / "database.sqlite").resolve())
    except Exception:
        active = None
        active_library_root = None

    effective = _choose_effective_sqlite_candidate(
        active_library_root=active_library_root,
        configured_sqlite_path=configured_sqlite_path,
    )
    effective_db_path = effective["path"] if effective is not None else None
    effective_storage_root = _effective_storage_root(Path(effective_db_path)) if effective_db_path else None

    return {
        "db_kind": kind,
        "db_path": path,
        "db_url_masked": _mask_url(database_url),
        "configured_db_kind": kind,
        "configured_db_path": path,
        "active_library": active_library,
        "active_library_db_path": active_library_database_path,
        "matches_active_library_db_path": bool(
            kind == "sqlite"
            and path is not None
            and active_library_database_path is not None
            and Path(path) == Path(active_library_database_path)
        ),
        "is_active_library_sqlite": kind == "sqlite" and path is not None and Path(path).name == "database.sqlite",
        "effective_db_path": effective_db_path,
        "effective_storage_root": effective_storage_root,
        "effective_db_has_papers_table": bool(effective and effective["has_papers_table"]),
        "effective_db_papers_total": int(effective["papers_total"]) if effective is not None else 0,
        "effective_matches_active_library_db_path": bool(
            effective_db_path is not None
            and active_library_database_path is not None
            and Path(effective_db_path) == Path(active_library_database_path)
        ),
        "recovered_from_candidate_scan": bool(
            effective_db_path is not None
            and (
                path is None
                or Path(effective_db_path) != Path(path)
            )
        ),
    }


def activate_active_library_database() -> dict[str, Any]:
    """Switch process settings to the registered active library SQLite DB if available."""
    from app.db.session import switch_database
    from app.services.library_manager import LibraryManager

    manager = LibraryManager()
    active = manager.get_active_library()
    activation_error: str | None = None
    recovered = False
    if active:
        try:
            manager.activate_library(active.name)
        except Exception as exc:
            activation_error = f"{type(exc).__name__}: {exc}"

    info = get_active_database_info()
    effective_path = info.get("effective_db_path")
    configured_path = info.get("configured_db_path")
    if effective_path and (configured_path is None or Path(effective_path) != Path(configured_path)):
        switch_database(f"sqlite:///{Path(effective_path).as_posix()}", storage_root=info.get("effective_storage_root"))
        recovered = True
        info = get_active_database_info()
    if activation_error is not None:
        info["activation_warning"] = activation_error
    if recovered:
        info["recovered_from_candidate_scan"] = True
    return info


def require_active_library_sqlite() -> dict[str, Any]:
    info = get_active_database_info()
    if not info["is_active_library_sqlite"]:
        info = activate_active_library_database()
    if not info["is_active_library_sqlite"]:
        raise RuntimeError(
            "Expected active library SQLite database, got "
            f"kind={info['db_kind']} path={info['db_path']} "
            f"active_library_db_path={info['active_library_db_path']} effective_db_path={info.get('effective_db_path')}"
        )
    return info
