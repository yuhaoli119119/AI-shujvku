from __future__ import annotations

from typing import Any

from sqlalchemy import or_


DEFAULT_LIBRARY_NAME = "默认文献库"
DEFAULT_LIBRARY_ALIASES = {
    DEFAULT_LIBRARY_NAME,
    "?????",
    "é»è®¤æç®åº",
    "榛樿鏂囩尞搴?",
}


def normalize_library_name(library_name: str | None) -> str:
    text = (library_name or "").strip()
    if not text or text in DEFAULT_LIBRARY_ALIASES or "榛樿" in text or "é»" in text:
        return DEFAULT_LIBRARY_NAME
    return text


def library_name_variants(library_name: str | None) -> tuple[str, ...]:
    normalized = normalize_library_name(library_name)
    if normalized == DEFAULT_LIBRARY_NAME:
        return tuple(sorted(DEFAULT_LIBRARY_ALIASES))
    return (normalized,)


def build_library_name_clause(column: Any, library_name: str | None):
    variants = library_name_variants(library_name)
    if len(variants) == 1:
        return column == variants[0]
    return or_(*[column == item for item in variants])
