from __future__ import annotations

from typing import Any

from sqlalchemy import or_


DEFAULT_LIBRARY_NAME = "默认文献库"

DEFAULT_LIBRARY_ALIASES = {
    DEFAULT_LIBRARY_NAME,
    "?????",
    "Codex ????????",
    "榛樿鏂囩尞搴?",
    "茅禄聵猫庐陇忙聳聡莽聦庐氓潞聯",
    "姒涙顓婚弬鍥╁盀鎼?",
}


def normalize_library_name(library_name: str | None) -> str:
    text = (library_name or "").strip()
    if not text or text in DEFAULT_LIBRARY_ALIASES or "姒涙" in text or "茅禄聵" in text:
        return DEFAULT_LIBRARY_NAME
    return text


def library_name_variants(library_name: str | None) -> tuple[str, ...]:
    normalized = normalize_library_name(library_name)
    variants: set[str] = {normalized}
    if normalized == DEFAULT_LIBRARY_NAME:
        variants.update(DEFAULT_LIBRARY_ALIASES)
    elif any(ord(ch) > 127 for ch in normalized):
        # Some legacy CLI-driven imports reached the API after the shell replaced
        # every CJK character with "?". Keep those rows discoverable without
        # treating arbitrary mixed strings as aliases.
        variants.add("?" * len(normalized))
    return tuple(sorted(variants))


def build_library_name_clause(column: Any, library_name: str | None):
    variants = library_name_variants(library_name)
    if len(variants) == 1:
        return column == variants[0]
    return or_(*[column == item for item in variants])
