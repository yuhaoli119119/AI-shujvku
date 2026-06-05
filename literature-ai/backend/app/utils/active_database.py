from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from app.utils.project_paths import (
    BACKEND_ROOT,
    WORKSPACE_ROOT,
    canonical_registry_path,
    default_library_root,
)
from app.utils.library_names import DEFAULT_LIBRARY_NAME, normalize_library_name

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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _resolve_runtime_path(path: Any) -> Path:
    text = str(path or "").strip()
    data_suffix = _data_mount_suffix(text)
    if data_suffix is None:
        return Path(text).resolve()
    if not data_suffix:
        return canonical_registry_path().resolve().parent
    return (canonical_registry_path().resolve().parent / data_suffix).resolve()


def _data_mount_suffix(path: Any) -> str | None:
    text = str(path or "").strip()
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    if normalized == "/data":
        return ""
    if normalized.startswith("/data/"):
        return normalized.removeprefix("/data/").strip("/")
    marker = "/literature-ai/data/"
    marker_index = lowered.rfind(marker)
    if marker_index >= 0:
        return normalized[marker_index + len(marker) :].strip("/")
    marker = "literature-ai/data/"
    marker_index = lowered.rfind(marker)
    if marker_index >= 0:
        return normalized[marker_index + len(marker) :].strip("/")
    return None


def _registry_entry(payload: dict[str, Any] | None, active_library: str | None) -> dict[str, Any] | None:
    if payload is None or not active_library:
        return None
    for entry in payload.get("libraries", []):
        if normalize_library_name(entry.get("name")) == normalize_library_name(active_library):
            return entry
    return None


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


def get_registered_active_library_info() -> dict[str, Any]:
    registry_path = canonical_registry_path().resolve()
    payload = _load_json(registry_path)
    active_library = normalize_library_name(payload.get("active_library")) if payload else None
    if not isinstance(active_library, str) or not active_library.strip():
        active_library = DEFAULT_LIBRARY_NAME
    entry = _registry_entry(payload, active_library)
    active_library_root = None
    active_library_db_path = None
    if entry and entry.get("root_path"):
        active_library_root = str(_resolve_runtime_path(entry["root_path"]))
        active_library_db_path = str((Path(active_library_root) / "database.sqlite").resolve())

    return {
        "canonical_registry_path": str(registry_path),
        "active_library": active_library,
        "active_library_root": active_library_root,
        "active_library_db_path": active_library_db_path,
        "registry_entry_found": entry is not None,
    }


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
        canonical_registry_path().resolve().parent / "libraries",
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
    active_library: str | None,
    active_library_root: Path | None,
    configured_sqlite_path: Path | None,
) -> dict[str, Any] | None:
    preferred_registered = (active_library_root / "database.sqlite").resolve() if active_library_root is not None else None
    preferred_configured = configured_sqlite_path.resolve() if configured_sqlite_path is not None else None
    normalized_active_library = normalize_library_name(active_library)

    if preferred_registered is not None:
        registered_summary = _sqlite_candidate_summary(preferred_registered)
        if (
            registered_summary["exists"]
            and registered_summary["has_papers_table"]
            and (normalized_active_library != DEFAULT_LIBRARY_NAME or registered_summary["papers_total"] > 0)
        ):
            registered_summary["score"] = 10_000 + int(registered_summary["papers_total"])
            return registered_summary

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
    shared_project = effective_db_path.parent / "papers"
    if shared_project.exists():
        return str(shared_project.resolve())
    legacy_storage = effective_db_path.parent / "storage"
    if legacy_storage.exists():
        return str(legacy_storage.resolve())
    fallback = WORKSPACE_ROOT / "storage"
    if fallback.exists():
        return str(fallback.resolve())
    return str(shared_project.resolve())


def _maybe_repair_registered_default_sqlite(info: dict[str, Any]) -> bool:
    active_library = normalize_library_name(info.get("active_library"))
    registered_path_raw = info.get("active_library_db_path")
    effective_path_raw = info.get("effective_db_path")
    if active_library != DEFAULT_LIBRARY_NAME or not registered_path_raw or not effective_path_raw:
        return False

    registered_path = Path(str(registered_path_raw)).resolve()
    effective_path = Path(str(effective_path_raw)).resolve()
    if registered_path == effective_path or registered_path.parent != default_library_root().resolve():
        return False

    registered_summary = _sqlite_candidate_summary(registered_path)
    effective_summary = _sqlite_candidate_summary(effective_path)
    if int(registered_summary["papers_total"]) > 0 or int(effective_summary["papers_total"]) <= 0:
        return False

    registered_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(effective_path, registered_path)
    return True


def get_active_database_info() -> dict[str, Any]:
    """Return safe runtime DB source-of-truth metadata for scripts and APIs."""
    from app.config import get_settings

    settings = get_settings()
    database_url = settings.database_url
    configured_kind = _kind_from_url(database_url)
    configured_path = _sqlite_path(database_url)
    configured_sqlite_path = Path(configured_path).resolve() if configured_path else None
    registered_active = get_registered_active_library_info()
    active_library = registered_active["active_library"]
    active_library_database_path = registered_active["active_library_db_path"]
    active_library_root = (
        Path(str(registered_active["active_library_root"])).resolve()
        if registered_active.get("active_library_root")
        else None
    )
    configured_runtime_is_sqlite = configured_kind == "sqlite" and configured_path is not None
    force_configured_database = bool(getattr(settings, "force_configured_database", False))

    if force_configured_database and configured_kind != "sqlite":
        return {
            "db_kind": configured_kind,
            "db_path": None,
            "db_url_masked": _mask_url(database_url),
            "configured_db_kind": configured_kind,
            "configured_db_path": configured_path,
            "configured_db_url_masked": _mask_url(database_url),
            "active_library": active_library,
            "active_library_db_path": active_library_database_path,
            "matches_active_library_db_path": False,
            "configured_matches_active_library_db_path": False,
            "is_active_library_sqlite": False,
            "effective_db_path": None,
            "effective_storage_root": str(Path(settings.storage_root).resolve()),
            "effective_db_has_papers_table": False,
            "effective_db_papers_total": 0,
            "effective_matches_active_library_db_path": False,
            "recovered_from_candidate_scan": False,
            "force_configured_database": True,
        }

    if force_configured_database and configured_sqlite_path is not None:
        effective = _sqlite_candidate_summary(configured_sqlite_path)
    else:
        effective = _choose_effective_sqlite_candidate(
            active_library=active_library,
            active_library_root=active_library_root,
            configured_sqlite_path=configured_sqlite_path,
        )
    effective_db_path = effective["path"] if effective is not None else None
    if force_configured_database and effective_db_path:
        effective_storage_root = str(Path(settings.storage_root).resolve())
    else:
        effective_storage_root = _effective_storage_root(Path(effective_db_path)) if effective_db_path else None
    resolved_db_path = configured_path if configured_runtime_is_sqlite else (effective_db_path or configured_path)
    resolved_db_kind = configured_kind if configured_runtime_is_sqlite else ("sqlite" if resolved_db_path is not None else configured_kind)
    resolved_db_url_masked = (
        _mask_url(f"sqlite:///{Path(resolved_db_path).as_posix()}") if resolved_db_path is not None else _mask_url(database_url)
    )
    configured_matches_active_library_db_path = bool(
        configured_kind == "sqlite"
        and configured_path is not None
        and active_library_database_path is not None
        and Path(configured_path) == Path(active_library_database_path)
    )
    recovered_from_candidate_scan = False if force_configured_database else bool(
        effective_db_path is not None
        and (
            active_library_database_path is None
            or Path(effective_db_path) != Path(active_library_database_path)
        )
    )

    return {
        "db_kind": resolved_db_kind,
        "db_path": resolved_db_path,
        "db_url_masked": resolved_db_url_masked,
        "configured_db_kind": configured_kind,
        "configured_db_path": configured_path,
        "configured_db_url_masked": _mask_url(database_url),
        "active_library": active_library,
        "active_library_db_path": active_library_database_path,
        "matches_active_library_db_path": bool(
            resolved_db_path is not None
            and active_library_database_path is not None
            and Path(resolved_db_path) == Path(active_library_database_path)
        ),
        "configured_matches_active_library_db_path": configured_matches_active_library_db_path,
        "is_active_library_sqlite": bool(
            resolved_db_kind == "sqlite"
            and resolved_db_path is not None
            and Path(resolved_db_path).name == "database.sqlite"
        ),
        "effective_db_path": effective_db_path,
        "effective_storage_root": effective_storage_root,
        "effective_db_has_papers_table": bool(effective and effective["has_papers_table"]),
        "effective_db_papers_total": int(effective["papers_total"]) if effective is not None else 0,
        "effective_matches_active_library_db_path": bool(
            effective_db_path is not None
            and active_library_database_path is not None
            and Path(effective_db_path) == Path(active_library_database_path)
        ),
        "recovered_from_candidate_scan": recovered_from_candidate_scan,
        "force_configured_database": force_configured_database,
    }


def activate_active_library_database() -> dict[str, Any]:
    """Switch process settings to the registered active library SQLite DB if available."""
    from app.config import get_settings
    from app.db.session import switch_database
    from app.services.library_manager import LibraryManager

    settings = get_settings()
    if bool(getattr(settings, "force_configured_database", False)):
        if _kind_from_url(settings.database_url) != "sqlite":
            from app.db.session import init_db

            init_db(settings.database_url)
            info = get_active_database_info()
            info["force_configured_database"] = True
            return info
        info = get_active_database_info()
        configured_path = info.get("configured_db_path")
        if info.get("configured_db_kind") == "sqlite" and configured_path:
            switch_database(
                f"sqlite:///{Path(str(configured_path)).as_posix()}",
                storage_root=str(Path(settings.storage_root).resolve()),
            )
            info = get_active_database_info()
        info["force_configured_database"] = True
        return info

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
    if _maybe_repair_registered_default_sqlite(info):
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
