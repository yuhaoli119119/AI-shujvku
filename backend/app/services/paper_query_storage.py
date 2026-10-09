from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8192)
def cached_pdf_size_for_storage(stored_path: str, storage_root: str) -> int | None:
    raw = str(stored_path or "").strip()
    if not raw:
        return None

    root = Path(storage_root)
    parts = [part for part in re.split(r"[\\/]+", raw) if part]
    lowered = [part.lower() for part in parts]
    basename = parts[-1] if parts else ""
    candidates: list[Path] = []
    raw_path = Path(raw)

    if raw_path.is_absolute():
        candidates.append(raw_path)
    if "storage" in lowered:
        idx = lowered.index("storage")
        candidates.append(root.parent / Path(*parts[idx:]))
    if "pdf" in lowered:
        idx = lowered.index("pdf")
        candidates.append(root / Path(*parts[idx + 1 :]))
    if basename:
        candidates.append(root / "pdf" / basename)
        candidates.append(root / basename)
    if not raw_path.is_absolute():
        candidates.append(root / raw_path)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if resolved.is_file():
                return int(resolved.stat().st_size)
        except OSError:
            continue
    return None
