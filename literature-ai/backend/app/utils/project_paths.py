from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

CANONICAL_REGISTRY_PATH = (PROJECT_ROOT / "data" / "library_registry.json").resolve()
DEFAULT_LIBRARY_ROOT = (PROJECT_ROOT / "data" / "libraries" / "default").resolve()


def canonical_registry_path() -> Path:
    return CANONICAL_REGISTRY_PATH


def default_library_root() -> Path:
    return DEFAULT_LIBRARY_ROOT


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
