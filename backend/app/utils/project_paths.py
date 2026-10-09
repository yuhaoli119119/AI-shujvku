from __future__ import annotations

from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

CANONICAL_REGISTRY_PATH = (PROJECT_ROOT / "data" / "library_registry.json").resolve()
DEFAULT_LIBRARY_ROOT = (PROJECT_ROOT / "data" / "libraries" / "default").resolve()
PROJECT_DATA_ROOT = (PROJECT_ROOT / "data").resolve()


def canonical_registry_path() -> Path:
    return CANONICAL_REGISTRY_PATH


def default_library_root() -> Path:
    return DEFAULT_LIBRARY_ROOT


def project_data_root() -> Path:
    return PROJECT_DATA_ROOT


def resolve_data_mount_path(path: Any) -> Path:
    """Resolve Docker /data paths to the canonical project data directory.

    In Docker, PROJECT_ROOT is "/" and this returns /data unchanged. On a
    desktop/local Python run, registry entries created inside Docker still use
    /data; mapping them here keeps the same registry portable across both
    runtimes.
    """
    text = str(path or "").strip()
    normalized = text.replace("\\", "/")
    if normalized == "/data":
        return PROJECT_DATA_ROOT
    if normalized.startswith("/data/"):
        suffix = normalized.removeprefix("/data/").strip("/")
        return (PROJECT_DATA_ROOT / suffix).resolve()
    return Path(text).resolve()


def shadow_registry_paths() -> list[Path]:
    candidates = [
        (WORKSPACE_ROOT / "data" / "library_registry.json").resolve(),
        (BACKEND_ROOT / "data" / "library_registry.json").resolve(),
    ]
    unique: list[Path] = []
    for path in candidates:
        if path != CANONICAL_REGISTRY_PATH and path not in unique:
            unique.append(path)
    return unique
