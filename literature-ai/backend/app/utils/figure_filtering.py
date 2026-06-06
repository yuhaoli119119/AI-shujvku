from __future__ import annotations

import re
from typing import Any


DECORATIVE_CAPTION_KEYWORDS: tuple[str, ...] = (
    "crossmark",
    "cross mark",
    "checkmark",
    "elsevier",
    "springer",
    "wiley",
    "acs publications",
    "rsc publishing",
    "royal society",
    "nature publishing",
    "science china press",
    "publisher logo",
    "copyright",
    "\u00a9",
    "creative commons",
    "cc-by",
    "cc by",
    "doi:",
    "https://doi.org",
    "open access",
    "update",
    "logo",
)
SHORT_CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|scheme)\s*\d+\s*[\.:：-]?\s*$", re.IGNORECASE)


def decorative_figure_reason(caption: str | None, prov: list[Any] | None = None) -> str | None:
    """Return why a figure should be treated as decorative, or None when kept."""
    del prov
    if not caption or not caption.strip():
        return "missing caption"

    caption_lower = caption.lower().strip()
    if SHORT_CAPTION_RE.match(caption_lower):
        return None

    for keyword in DECORATIVE_CAPTION_KEYWORDS:
        if keyword in caption_lower:
            return f"decorative keyword: {keyword}"
    return None


def is_decorative_figure(caption: str | None, prov: list[Any] | None = None) -> bool:
    return decorative_figure_reason(caption, prov) is not None
