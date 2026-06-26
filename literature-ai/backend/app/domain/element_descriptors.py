from __future__ import annotations

from typing import Any


ELEMENT_DESCRIPTOR_SOURCE = "literature_ai_static_element_descriptors"
ELEMENT_DESCRIPTOR_SOURCE_VERSION = "li_s_sac_dac_v1"


_ELEMENT_DESCRIPTORS: dict[str, dict[str, float | int]] = {
    "Fe": {"atomic_number": 26, "electronegativity": 1.83, "valence_electron_count": 8},
    "Co": {"atomic_number": 27, "electronegativity": 1.88, "valence_electron_count": 9},
    "Ni": {"atomic_number": 28, "electronegativity": 1.91, "valence_electron_count": 10},
    "Mn": {"atomic_number": 25, "electronegativity": 1.55, "valence_electron_count": 7},
    "Cu": {"atomic_number": 29, "electronegativity": 1.90, "valence_electron_count": 11},
    "Zn": {"atomic_number": 30, "electronegativity": 1.65, "valence_electron_count": 12},
    "V": {"atomic_number": 23, "electronegativity": 1.63, "valence_electron_count": 5},
    "Mo": {"atomic_number": 42, "electronegativity": 2.16, "valence_electron_count": 6},
    "W": {"atomic_number": 74, "electronegativity": 2.36, "valence_electron_count": 6},
    "Ti": {"atomic_number": 22, "electronegativity": 1.54, "valence_electron_count": 4},
    "Cr": {"atomic_number": 24, "electronegativity": 1.66, "valence_electron_count": 6},
    "Ru": {"atomic_number": 44, "electronegativity": 2.20, "valence_electron_count": 8},
    "Ir": {"atomic_number": 77, "electronegativity": 2.20, "valence_electron_count": 9},
    "Pt": {"atomic_number": 78, "electronegativity": 2.28, "valence_electron_count": 10},
    "Pd": {"atomic_number": 46, "electronegativity": 2.20, "valence_electron_count": 10},
    "Rh": {"atomic_number": 45, "electronegativity": 2.28, "valence_electron_count": 9},
}


def normalize_element_symbol(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    letters = "".join(ch for ch in text if ch.isalpha())
    if not letters:
        return None
    return letters[0].upper() + letters[1:].lower()


def element_descriptor(symbol: Any) -> dict[str, Any]:
    normalized = normalize_element_symbol(symbol)
    data = _ELEMENT_DESCRIPTORS.get(normalized or "")
    base = {
        "element_symbol": normalized,
        "atomic_number": None,
        "electronegativity": None,
        "valence_electron_count": None,
        "element_descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
        "element_descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
    }
    if data is None:
        return base
    return {**base, **data}


def build_metal_descriptor_payload(metal_centers: Any) -> dict[str, Any]:
    symbols = _metal_symbols(metal_centers)
    descriptors = [element_descriptor(symbol) for symbol in symbols]
    descriptor_blockers = [
        "unknown_metal_descriptor"
        for descriptor in descriptors
        if descriptor["atomic_number"] is None
    ]
    descriptor_blockers = sorted(set(descriptor_blockers))
    metal_1 = descriptors[0] if descriptors else None
    metal_2 = descriptors[1] if len(descriptors) > 1 else None
    return {
        "metal_descriptor_summary": {
            "metal_centers": symbols,
            "descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
            "descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
            "descriptor_available_count": sum(1 for item in descriptors if item["atomic_number"] is not None),
            "descriptor_missing_count": sum(1 for item in descriptors if item["atomic_number"] is None),
            "metal_center_order_source": "catalyst_sample.metal_centers" if symbols else None,
        },
        "metal_1_descriptors": metal_1,
        "metal_2_descriptors": metal_2,
        "dac_combined_descriptors": _combined_descriptors(descriptors),
        "descriptor_blockers": descriptor_blockers,
    }


def _metal_symbols(metal_centers: Any) -> list[str]:
    if not isinstance(metal_centers, list):
        return []
    symbols: list[str] = []
    for item in metal_centers:
        symbol = normalize_element_symbol(item)
        if symbol:
            symbols.append(symbol)
    return symbols


def _combined_descriptors(descriptors: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(descriptors) < 2:
        return None
    first, second = descriptors[0], descriptors[1]
    pair = _canonical_pair(first.get("element_symbol"), second.get("element_symbol"))
    return {
        "metal_pair_canonical": pair,
        "atomic_number_delta": _delta(first, second, "atomic_number"),
        "electronegativity_delta": _delta(first, second, "electronegativity"),
        "valence_electron_count_delta": _delta(first, second, "valence_electron_count"),
        "atomic_number_mean": _mean(first, second, "atomic_number"),
        "electronegativity_mean": _mean(first, second, "electronegativity"),
        "valence_electron_count_mean": _mean(first, second, "valence_electron_count"),
        "descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
        "descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
    }


def _canonical_pair(first: Any, second: Any) -> str | None:
    symbols = [str(value) for value in (first, second) if value]
    if len(symbols) != 2:
        return None
    symbols.sort(key=lambda symbol: (_ELEMENT_DESCRIPTORS.get(symbol, {}).get("atomic_number") is None, _ELEMENT_DESCRIPTORS.get(symbol, {}).get("atomic_number", 999), symbol))
    return "-".join(symbols)


def _delta(first: dict[str, Any], second: dict[str, Any], key: str) -> float | int | None:
    left = first.get(key)
    right = second.get(key)
    if left is None or right is None:
        return None
    return abs(right - left)


def _mean(first: dict[str, Any], second: dict[str, Any], key: str) -> float | int | None:
    left = first.get(key)
    right = second.get(key)
    if left is None or right is None:
        return None
    return (left + right) / 2
