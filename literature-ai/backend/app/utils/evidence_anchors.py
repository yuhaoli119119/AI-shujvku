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

MATERIAL_CORRECTION_ANCHOR_KEYS = (
    "page",
    "section",
    "section_title",
    "quoted_text",
    "figure",
    "figure_id",
    "table",
    "table_id",
)


def iter_anchor_payloads(payload: Any) -> Iterator[dict[str, Any]]:
    # Plain strings are common evidence_location values (e.g. "PDF page 13, Table 5").
    # Treat them as quoted_text so has_evidence_anchor() can recognise them.
    if isinstance(payload, str):
        yield {"quoted_text": payload}
    elif isinstance(payload, dict):
        yield payload
        for key in NESTED_ANCHOR_KEYS:
            nested = payload.get(key)
            if isinstance(nested, (dict, list, str)):
                yield from iter_anchor_payloads(nested)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list, str)):
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


def first_material_correction_anchor(payload: Any) -> dict[str, Any] | None:
    for candidate in iter_anchor_payloads(payload):
        summary = {
            key: candidate.get(key)
            for key in MATERIAL_CORRECTION_ANCHOR_KEYS
            if candidate.get(key) is not None and str(candidate.get(key)).strip()
        }
        if summary:
            return summary
    return None


def has_material_correction_anchor(payload: Any) -> bool:
    return first_material_correction_anchor(payload) is not None
