from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


ENERGY_TO_EV = {
    "ev": 1.0,
    "mev": 1e-3,
    "kj/mol": 1.0 / 96.485,
    "kj mol-1": 1.0 / 96.485,
    "kj mol^-1": 1.0 / 96.485,
    "kcal/mol": 1.0 / 23.0605,
    "kcal mol-1": 1.0 / 23.0605,
    "kcal mol^-1": 1.0 / 23.0605,
}

LENGTH_TO_A = {
    "a": 1.0,
    "angstrom": 1.0,
    "angstroms": 1.0,
    "å": 1.0,
    "nm": 10.0,
    "pm": 0.01,
}

CAPACITY_TO_MAH_G = {
    "mah/g": 1.0,
    "mah g-1": 1.0,
    "mahg-1": 1.0,
    "ah/kg": 1.0,
    "ah kg-1": 1.0,
}

LOADING_TO_MG_CM2 = {
    "mg/cm2": 1.0,
    "mg cm-2": 1.0,
    "mgcm-2": 1.0,
    "g/m2": 0.1,
    "g m-2": 0.1,
}

RATIO_TO_UL_MG = {
    "ul/mg": 1.0,
    "ul mg-1": 1.0,
    "ulmg-1": 1.0,
}


@dataclass
class NormalizedUnit:
    original_value: float | None
    original_unit: str | None
    normalized_value: float | None
    normalized_unit: str
    conversion_factor: float = 1.0
    is_valid: bool = True


@dataclass
class NormalizationResult:
    field: str
    original: dict[str, Any]
    normalized: dict[str, Any]
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class UnitNormalizer:
    """Normalize common DFT and electrochemical units into stable ASCII forms."""

    def normalize(self, payload: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(payload, list):
            return [self._normalize_single(item) for item in payload]
        return self._normalize_single(payload)

    def _normalize_single(self, data: dict[str, Any]) -> dict[str, Any]:
        result = data.copy()
        field_name = str(data.get("field_name", "")).lower()
        value = data.get("value")
        unit = data.get("unit")
        changes: list[str] = []

        if "capacity" in field_name or self._canonical_unit(unit) in CAPACITY_TO_MAH_G:
            normalized = self.normalize_capacity(value, unit)
        elif any(token in field_name for token in ["energy", "adsorption", "barrier", "gibbs", "binding"]) or self._canonical_unit(unit) in ENERGY_TO_EV:
            normalized = self.normalize_energy(value, unit)
        elif any(token in field_name for token in ["vacuum", "distance", "length"]) or self._canonical_unit(unit) in LENGTH_TO_A:
            normalized = self.normalize_length(value, unit)
        elif "loading" in field_name:
            normalized = self.normalize_loading(value, unit)
        elif "ratio" in field_name:
            normalized = self.normalize_ratio(value, unit)
        else:
            normalized = NormalizedUnit(value, unit, value, unit or "", 1.0, value is not None)

        result["normalized_value"] = normalized.normalized_value
        result["normalized_unit"] = normalized.normalized_unit
        if normalized.conversion_factor != 1.0 and value is not None and unit is not None:
            changes.append(
                f"{field_name or 'value'}: {value} {unit} -> {normalized.normalized_value} {normalized.normalized_unit}"
            )
        result["_normalization_changes"] = changes
        return result

    def normalize_energy(self, value: float | None, unit: str | None) -> NormalizedUnit:
        return self._convert(value, unit, ENERGY_TO_EV, "eV", precision=6)

    def normalize_length(self, value: float | None, unit: str | None) -> NormalizedUnit:
        return self._convert(value, unit, LENGTH_TO_A, "A", precision=6)

    def normalize_capacity(self, value: float | None, unit: str | None) -> NormalizedUnit:
        return self._convert(value, unit, CAPACITY_TO_MAH_G, "mAh/g", precision=4)

    def normalize_loading(self, value: float | None, unit: str | None) -> NormalizedUnit:
        return self._convert(value, unit, LOADING_TO_MG_CM2, "mg/cm2", precision=4)

    def normalize_ratio(self, value: float | None, unit: str | None) -> NormalizedUnit:
        return self._convert(value, unit, RATIO_TO_UL_MG, "uL/mg", precision=4)

    def clean_numeric_string(self, raw: str) -> tuple[float | None, str | None]:
        match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%/\-0-9µμÅ]+)", raw)
        if not match:
            return None, None
        value = float(match.group(1))
        unit = self._display_unit(match.group(2))
        return value, unit

    def _convert(
        self,
        value: float | None,
        unit: str | None,
        mapping: dict[str, float],
        target_unit: str,
        precision: int,
    ) -> NormalizedUnit:
        if value is None or unit is None:
            return NormalizedUnit(value, unit, value, unit or "", is_valid=False)
        canonical = self._canonical_unit(unit)
        factor = mapping.get(canonical, 1.0)
        normalized_value = round(float(value) * factor, precision)
        normalized_unit = target_unit if canonical in mapping else self._display_unit(unit)
        return NormalizedUnit(value, unit, normalized_value, normalized_unit, factor, True)

    @staticmethod
    def _canonical_unit(unit: str | None) -> str:
        if not unit:
            return ""
        return (
            unit.strip()
            .replace("µ", "u")
            .replace("μ", "u")
            .replace("Å", "a")
            .replace(" ", "")
            .lower()
            .replace("cm2", "cm2")
        )

    @staticmethod
    def _display_unit(unit: str | None) -> str:
        canonical = UnitNormalizer._canonical_unit(unit)
        aliases = {
            "mah/g": "mAh/g",
            "mahg-1": "mAh/g",
            "ah/kg": "Ah/kg",
            "mg/cm2": "mg/cm2",
            "mgcm-2": "mg/cm2",
            "ul/mg": "uL/mg",
            "ulmg-1": "uL/mg",
            "ev": "eV",
            "mev": "meV",
            "kj/mol": "kJ/mol",
            "kcal/mol": "kcal/mol",
            "a": "A",
            "angstrom": "A",
            "å": "A",
        }
        return aliases.get(canonical, unit or "")
