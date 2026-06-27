from __future__ import annotations

import re
from typing import Any

from app.utils.evidence_anchors import has_evidence_anchor


def imported_evidence_payload(opinion: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    payload = opinion.get("evidence_payload")
    if isinstance(payload, (dict, list)) and has_evidence_anchor(payload):
        return payload
    location = opinion.get("evidence_location")
    if isinstance(location, dict):
        return location
    return payload if isinstance(payload, (dict, list)) else None


def first_anchor(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
        return None
    return payload if isinstance(payload, dict) else None


def first_text(*values: Any) -> str | None:
    for value in values:
        if value in (None, "", []):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def numeric_key(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.8g}"
    except (TypeError, ValueError):
        return str(value)


def normalize_imported_dft_value(
    *,
    value: Any,
    unit: str | None,
    property_type: Any = None,
) -> tuple[float | None, str | None]:
    if value in (None, ""):
        return None, unit
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None, unit
    unit_text = str(unit or "").strip()
    unit_key = unit_text.lower().replace(" ", "")
    if unit_key in {"mev"}:
        return numeric_value / 1000.0, "eV"
    if unit_key in {"ev"}:
        return numeric_value, "eV"
    if "gpu" in unit_key:
        ascii_key = "".join(ch for ch in unit_key if ch.isascii())
        if any(marker in ascii_key for marker in ("10^3", "x10^3", "103")) or (
            ascii_key.startswith("10") and ascii_key != "gpu"
        ):
            return numeric_value * 1000.0, "GPU"
        return numeric_value, "GPU"
    return numeric_value, unit_text or unit


def normalize_dft_value_for_comparison(value: Any, unit: Any) -> dict[str, Any]:
    try:
        numeric = float(value) if value is not None else None
    except (TypeError, ValueError):
        numeric = None
    normalized_unit = str(unit or "").strip().lower().replace(" ", "")
    if normalized_unit in {"e", "|e|", "electron", "electrons"}:
        normalized_unit = "e"
    if normalized_unit == "mev" and numeric is not None:
        return {"value": numeric / 1000.0, "unit": "ev"}
    return {"value": numeric, "unit": normalized_unit}


def same_normalized_dft_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("value") is None or right.get("value") is None:
        return False
    if str(left.get("unit") or "") != str(right.get("unit") or ""):
        return False
    tolerance = max(1e-9, abs(float(left["value"])) * 1e-6)
    return abs(float(left["value"]) - float(right["value"])) <= tolerance


def material_identity_parts_compatible(left: str, right: str) -> bool:
    left_normalized = str(left or "").strip().lower()
    right_normalized = str(right or "").strip().lower()
    if left_normalized == right_normalized:
        return True
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return True
    left_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", left_normalized)
        if len(token) >= 5
    }
    right_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", right_normalized)
        if len(token) >= 5
    }
    return bool(left_tokens & right_tokens)


def existing_material_binding_name_matches(current_name: Any, material_identity: Any) -> bool:
    current_normalized = normalized_text(current_name)
    expected_normalized = normalized_text(material_identity)
    if not current_normalized or not expected_normalized:
        return False
    return (
        current_normalized == expected_normalized
        or current_normalized in expected_normalized
        or expected_normalized in current_normalized
    )
