from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings, get_settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_ROOT.parent
CONTAINER_PREFIXES = ("/app/", "\\app\\", "/app\\", "\\app/")
KNOWN_STORAGE_CATEGORIES = ("pdf", "tei", "docling_json", "figures", "tables", "markdown", "text")


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


def _path_parts(path_str: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path_str.strip()) if part]


def _library_root(settings: Settings) -> Path:
    return settings.storage_root.resolve().parent


def _storage_relative_suffix(path_str: str, category: str | None) -> Path | None:
    parts = _path_parts(path_str)
    if not parts:
        return None
    lowered = [part.lower() for part in parts]

    if "storage" in lowered:
        index = lowered.index("storage")
        return Path(*parts[index:])

    if category:
        category_lower = category.lower()
        if category_lower in lowered:
            index = lowered.index(category_lower)
            return Path("storage", *parts[index:])

    for category_name in KNOWN_STORAGE_CATEGORIES:
        if category_name in lowered:
            index = lowered.index(category_name)
            return Path("storage", *parts[index:])
    return None


def _storage_relative_candidates(settings: Settings, path_str: str, category: str | None) -> list[Path]:
    suffix = _storage_relative_suffix(path_str, category)
    if suffix is None:
        return []

    candidates: list[Path] = [_library_root(settings) / suffix]
    parts = suffix.parts
    if len(parts) >= 3 and parts[0].lower() == "storage" and parts[1].lower() in KNOWN_STORAGE_CATEGORIES:
        candidates.append(settings.storage_root / Path(*parts[2:]))
    return candidates


def _search_roots(settings: Settings, category: str | None) -> list[Path]:
    roots: list[Path] = []
    if category and category in settings.storage_paths:
        roots.append(settings.storage_paths[category])
    roots.extend(
        [
            settings.storage_root,
            _library_root(settings),
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

    direct_candidates = [Path(raw), Path(stripped)]
    direct_candidates.extend(_storage_relative_candidates(runtime_settings, raw, category))
    direct_candidates.extend(_storage_relative_candidates(runtime_settings, stripped, category))
    stripped_path = Path(stripped)
    if category and category in runtime_settings.storage_paths and not stripped_path.is_absolute():
        direct_candidates.append(runtime_settings.storage_paths[category] / stripped_path)
        direct_candidates.append(runtime_settings.storage_root / stripped_path)
    basename = _basename(stripped)
    if basename:
        for root in _search_roots(runtime_settings, category):
            direct_candidates.append(root / basename)

    seen: set[Path] = set()
    for candidate in direct_candidates:
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


def canonicalize_persisted_artifact_reference(
    stored_path: str | Path | None,
    *,
    category: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    if stored_path is None:
        return None

    runtime_settings = settings or get_settings()
    raw = str(stored_path).strip()
    if not raw:
        return None

    resolved = (
        stored_path.resolve() if isinstance(stored_path, Path) else resolve_persisted_artifact_path(
            raw,
            category=category,
            settings=runtime_settings,
            must_exist=False,
        )
    )
    if resolved is None:
        return None

    storage_root = runtime_settings.storage_root.resolve()
    library_root = _library_root(runtime_settings)
    try:
        relative_to_storage = resolved.relative_to(storage_root)
        return Path("storage", relative_to_storage).as_posix()
    except ValueError:
        pass

    try:
        return resolved.relative_to(library_root).as_posix()
    except ValueError:
        return None
