from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.utils.library_names import DEFAULT_LIBRARY_NAME, normalize_library_name
from app.utils.project_paths import canonical_registry_path


def _kind_from_url(database_url: str) -> str:
    if database_url.startswith("postgresql"):
        return "postgresql"
    return "unsupported"


def _mask_url(database_url: str) -> str:
    if "@" in database_url:
        return database_url.split("@")[-1]
    return "***"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _data_mount_suffix(path: Any) -> str | None:
    text = str(path or "").strip()
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    if normalized == "/data":
        return ""
    if normalized.startswith("/data/"):
        return normalized.removeprefix("/data/").strip("/")
    for marker in ("/literature-ai/data/", "literature-ai/data/"):
        marker_index = lowered.rfind(marker)
        if marker_index >= 0:
            return normalized[marker_index + len(marker) :].strip("/")
    return None


def _resolve_runtime_path(path: Any) -> Path:
    text = str(path or "").strip()
    data_suffix = _data_mount_suffix(text)
    if data_suffix is None:
        return Path(text).resolve()
    data_root = canonical_registry_path().resolve().parent
    return data_root if not data_suffix else (data_root / data_suffix).resolve()


def _registry_entry(payload: dict[str, Any] | None, active_library: str | None) -> dict[str, Any] | None:
    if payload is None or not active_library:
        return None
    for entry in payload.get("libraries", []):
        if normalize_library_name(entry.get("name")) == normalize_library_name(active_library):
            return entry
    return None


def _configured_database_paper_counts(database_url: str, active_library: str | None) -> dict[str, int]:
    if not database_url.startswith("postgresql"):
        return {"papers_total": 0, "active_library_papers_total": 0}
    try:
        from sqlalchemy import text

        from app.db.session import get_engine
    except Exception:
        return {"papers_total": 0, "active_library_papers_total": 0}

    try:
        with get_engine(database_url).connect() as connection:
            total = int(connection.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0)
            active_total = total
            if active_library:
                active_total = int(
                    connection.execute(
                        text("SELECT COUNT(*) FROM papers WHERE library_name = :library_name"),
                        {"library_name": active_library},
                    ).scalar()
                    or 0
                )
            return {"papers_total": total, "active_library_papers_total": active_total}
    except Exception:
        return {"papers_total": 0, "active_library_papers_total": 0}


def get_registered_active_library_info() -> dict[str, Any]:
    registry_path = canonical_registry_path().resolve()
    payload = _load_json(registry_path)
    active_library = normalize_library_name(payload.get("active_library")) if payload else None
    if not isinstance(active_library, str) or not active_library.strip():
        active_library = DEFAULT_LIBRARY_NAME
    entry = _registry_entry(payload, active_library)
    active_library_root = None
    if entry and entry.get("root_path"):
        active_library_root = str(_resolve_runtime_path(entry["root_path"]))

    return {
        "canonical_registry_path": str(registry_path),
        "active_library": active_library,
        "active_library_root": active_library_root,
        "registry_entry_found": entry is not None,
    }


def get_active_database_info() -> dict[str, Any]:
    """Return metadata for the configured PostgreSQL source of truth."""
    from app.config import get_settings

    settings = get_settings()
    database_url = settings.database_url
    configured_kind = _kind_from_url(database_url)
    registered_active = get_registered_active_library_info()
    active_library = registered_active["active_library"]
    configured_counts = _configured_database_paper_counts(database_url, active_library)

    return {
        "db_kind": configured_kind,
        "db_url_masked": _mask_url(database_url),
        "active_library": active_library,
        "active_library_root": registered_active.get("active_library_root"),
        "storage_root": str(Path(settings.storage_root).resolve()),
        "papers_total": configured_counts["active_library_papers_total"],
        "configured_db_papers_total": configured_counts["papers_total"],
    }


def activate_active_library_database() -> dict[str, Any]:
    """Initialize the configured PostgreSQL database and return runtime info."""
    from app.config import get_settings
    from app.db.session import init_db

    settings = get_settings()
    if not settings.database_url.strip().lower().startswith("postgresql"):
        raise RuntimeError("Only PostgreSQL is supported. Configure LITAI_DATABASE_URL.")
    init_db(settings.database_url)
    return get_active_database_info()
