from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings, get_settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_ROOT.parent
CONTAINER_PREFIXES = ("/app/", "\\app\\", "/app\\", "\\app/")


def _strip_container_prefix(path_str: str) -> str:
    normalized = path_str.strip()
    lowered = normalized.lower()
    for prefix in CONTAINER_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return normalized[len(prefix):]
    return normalized


def _basename(path_str: str) -> str:
    parts = re.split(r"[\\/]+", path_str.strip())
    return parts[-1] if parts else ""


def _search_roots(settings: Settings, category: str | None) -> list[Path]:
    roots: list[Path] = []
    if category and category in settings.storage_paths:
        roots.append(settings.storage_paths[category])
    roots.extend(
        [
            settings.storage_root,
            WORKSPACE_ROOT / "storage",
            BACKEND_ROOT / "storage",
            BACKEND_ROOT,
            WORKSPACE_ROOT,
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def resolve_persisted_artifact_path(
    stored_path: str | None,
    *,
    category: str | None = None,
    settings: Settings | None = None,
    must_exist: bool = True,
) -> Path | None:
    if not stored_path or not stored_path.strip():
        return None

    runtime_settings = settings or get_settings()
    raw = stored_path.strip()
    stripped = _strip_container_prefix(raw)

    direct_candidates = [raw, stripped]
    basename = _basename(stripped)
    if basename:
        for root in _search_roots(runtime_settings, category):
            direct_candidates.append(str(root / basename))

    seen: set[Path] = set()
    for candidate_str in direct_candidates:
        candidate = Path(candidate_str)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved

    if basename:
        for root in _search_roots(runtime_settings, category):
            if not root.exists():
                continue
            for match in root.rglob(basename):
                if match.is_file():
                    return match.resolve()

    if must_exist:
        return None
    fallback = Path(stripped)
    try:
        return fallback.resolve()
    except OSError:
        return fallback
