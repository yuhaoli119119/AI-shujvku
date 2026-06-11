from __future__ import annotations

from typing import Any, Iterator


ANCHOR_KEYS = (
    "page",
    "section",
    "section_title",
    "figure",
    "figure_id",
    "table",
    "table_id",
    "quoted_text",
    "evidence_text",
    "bbox",
)

NESTED_ANCHOR_KEYS = (
    "locator",
    "evidence_location",
    "evidence_anchor",
    "location",
    "anchor",
    "primary_locator",
)


def iter_anchor_payloads(payload: Any) -> Iterator[dict[str, Any]]:
    if isinstance(payload, dict):
        yield payload
        for key in NESTED_ANCHOR_KEYS:
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                yield from iter_anchor_payloads(nested)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                yield from iter_anchor_payloads(item)


def has_evidence_anchor(payload: Any) -> bool:
    return first_evidence_anchor(payload) is not None


def first_evidence_anchor(payload: Any) -> dict[str, Any] | None:
    for candidate in iter_anchor_payloads(payload):
        summary = {
            key: candidate.get(key)
            for key in ANCHOR_KEYS
            if candidate.get(key) is not None and str(candidate.get(key)).strip()
        }
        if summary:
            return summary
    return None
