from __future__ import annotations

from typing import Any


MANUAL_REVIEW_MODULES = ("content", "figures", "dft")


def normalize_manual_review_progress(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Normalize legacy booleans and current structured manual-review progress."""
    source = data if isinstance(data, dict) else {}
    raw_progress = source.get("manual_review_progress")
    progress = raw_progress if isinstance(raw_progress, dict) else {}

    normalized: dict[str, dict[str, Any]] = {}
    for module in MANUAL_REVIEW_MODULES:
        raw = progress.get(module)
        if isinstance(raw, dict):
            normalized[module] = {
                "completed": bool(raw.get("completed")),
                "updated_at": raw.get("updated_at"),
                "updated_by": raw.get("updated_by"),
            }
        else:
            normalized[module] = {
                "completed": bool(raw),
                "updated_at": None,
                "updated_by": None,
            }
    return normalized
