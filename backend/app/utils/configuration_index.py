"""Canonical parsing for an explicitly supplied DFT configuration index."""

from __future__ import annotations

from typing import Any


def parse_positive_configuration_index(value: Any) -> int | None:
    """Return an explicitly supplied positive integer, never infer one from text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or not candidate.isascii() or not candidate.isdecimal():
        return None
    parsed = int(candidate)
    return parsed if parsed > 0 else None


def extract_configuration_index(evidence_payload: Any) -> int | None:
    """Read a positive configuration index from supported payload fields only."""
    if not isinstance(evidence_payload, dict):
        return None
    values = [evidence_payload.get("configuration_index")]
    corrected = evidence_payload.get("corrected_value")
    if isinstance(corrected, dict):
        values.append(corrected.get("configuration_index"))
    configuration = evidence_payload.get("configuration")
    if isinstance(configuration, dict):
        values.extend((configuration.get("index"), configuration.get("configuration_index")))
    for value in values:
        parsed = parse_positive_configuration_index(value)
        if parsed is not None:
            return parsed
    return None
