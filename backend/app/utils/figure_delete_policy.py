from __future__ import annotations

import re
from typing import Any


NOISE_CROP_STATUSES = {"noise", "noisy", "missing", "failed"}
DELETE_MARKERS = (
    "duplicate",
    "duplicated",
    "redundant",
    "parser fragment",
    "fragment crop",
    "duplicate crop",
    "duplicate panel",
    "non-scientific noise",
    "publisher logo",
    "crossmark",
    "header",
    "footer",
    "watermark",
)


def _read_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def normalized_figure_identity(item: Any) -> str | None:
    figure_label = str(_read_value(item, "figure_label") or "").strip()
    caption = str(_read_value(item, "caption") or "").strip()
    for raw in (figure_label, caption):
        match = re.search(
            r"\b(?P<kind>fig(?:ure)?|scheme)[_\s.\-]*(?P<number>\d+)(?P<suffix>[a-z])?",
            raw,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        kind = "scheme" if match.group("kind").lower() == "scheme" else "figure"
        suffix = (match.group("suffix") or "").lower()
        return f"{kind}:{match.group('number')}{suffix}"

    match = re.search(r"\b(\d+)([a-z])?\b", figure_label)
    if not match:
        return None
    suffix = (match.group(2) or "").lower()
    return f"figure:{match.group(1)}{suffix}"


def direct_delete_eligibility(item: Any, *, duplicate_group_size: int = 1) -> tuple[bool, str | None]:
    crop_status = str(_read_value(item, "crop_status") or "").strip().lower()
    figure_role = str(_read_value(item, "figure_role") or "").strip().lower()
    flags = _text_list(_read_value(item, "flags"))
    warnings = _text_list(_read_value(item, "figure_reliability_warnings"))
    key_elements = _text_list(_read_value(item, "key_elements"))
    text = " ".join(
        [
            str(_read_value(item, "caption") or ""),
            str(_read_value(item, "content_summary") or ""),
            str(_read_value(item, "figure_label") or ""),
            figure_role,
            " ".join(flags),
            " ".join(warnings),
            " ".join(key_elements),
        ]
    ).lower()

    if figure_role == "noise":
        return True, "figure_role_noise"
    if duplicate_group_size > 1:
        return True, f"duplicate_group_{duplicate_group_size}"
    if crop_status in NOISE_CROP_STATUSES:
        return True, f"crop_status_{crop_status}"
    for marker in DELETE_MARKERS:
        if marker in text:
            return True, f"text_marker_{marker.replace(' ', '_')}"
    return False, None
