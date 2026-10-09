import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

with open(backend_dir / "app" / "utils" / "artifact_paths.py", "r", encoding="utf-8") as f:
    content = f.read()

# Let's cleanly replace the whole resolve_persisted_artifact_path function.
import re

pattern = re.compile(r"def resolve_persisted_artifact_path\(.*?\) -> Path \| None:\n.*?(?=def canonicalize_persisted_artifact_reference\()", re.DOTALL)

new_func = """def resolve_persisted_artifact_path(
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
    direct_candidates.extend(_windows_mirror_candidates(raw))
    direct_candidates.extend(_windows_mirror_candidates(stripped))

    stripped_path = Path(stripped)
    first_part = stripped_path.parts[0].lower() if stripped_path.parts else ""
    if category and category in runtime_settings.storage_paths and not stripped_path.is_absolute() and first_part not in ("storage", category.lower()):
        direct_candidates.append(runtime_settings.storage_paths[category] / stripped_path)
        direct_candidates.append(runtime_settings.storage_root / stripped_path)
        
    if first_part == "by_id":
        direct_candidates.extend([
            runtime_settings.storage_root / stripped_path,
            WORKSPACE_ROOT / "data" / "storage" / stripped_path,
            BACKEND_ROOT / "data" / "storage" / stripped_path,
        ])
        
    basename = _basename(stripped)
    if basename:
        storage_paths = runtime_settings.storage_paths
        if category and category in storage_paths:
            direct_candidates.append(storage_paths[category] / basename)
        elif not category:
            for category_name in KNOWN_STORAGE_CATEGORIES:
                category_root = storage_paths.get(category_name)
                if category_root is not None:
                    direct_candidates.append(category_root / basename)
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

    if must_exist:
        return None
    fallback = Path(stripped)
    try:
        return fallback.resolve()
    except OSError:
        return fallback


"""

new_content = pattern.sub(new_func, content)

with open(backend_dir / "app" / "utils" / "artifact_paths.py", "w", encoding="utf-8") as f:
    f.write(new_content)

print("Restored artifact_paths.py")
