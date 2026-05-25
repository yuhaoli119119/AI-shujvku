from __future__ import annotations

from typing import Any

from app.normalizers.chemistry_normalizer import ADSORBATE_MAP, CATALYST_TYPE_MAP
from app.normalizers.unit_normalizer import UnitNormalizer
from app.schemas.extraction import EvidenceField, ValidationWarning


DFT_ENERGY_TYPES = {
    "adsorption_energy",
    "gibbs_free_energy_change",
    "reaction_barrier",
    "li2s_decomposition_barrier",
    "li2s_nucleation_barrier",
    "d_band_center",
    "bader_charge",
    "charge_transfer",
    "dos_claim",
    "charge_density_difference_claim",
}


class ExtractionValidator:
    def __init__(self) -> None:
        self.units = UnitNormalizer()

    def validate_payload(self, payload: dict[str, list[Any]]) -> list[ValidationWarning]:
        warnings: list[ValidationWarning] = []
        for name, items in payload.items():
            for idx, item in enumerate(items or []):
                target_id = self._target_id(item, name, idx)
                fields = self._field_map(item)
                warnings.extend(self._validate_required_evidence(name, target_id, fields))
                warnings.extend(self._validate_units(name, target_id, fields))
                warnings.extend(self._validate_ranges(name, target_id, fields))
                warnings.extend(self._validate_enums(name, target_id, fields))
                warnings.extend(self._validate_consistency(name, target_id, fields))
        return warnings

    def _validate_required_evidence(
        self,
        target_type: str,
        target_id: str,
        fields: dict[str, EvidenceField],
    ) -> list[ValidationWarning]:
        warnings = []
        for field_name, field in fields.items():
            if field.value not in (None, "", []) and not field.evidence_text:
                warnings.append(
                    ValidationWarning(
                        severity="warning",
                        code="missing_evidence_text",
                        message=f"{target_type}.{field_name} has a value but no evidence_text.",
                        target_type=target_type,
                        target_id=target_id,
                        field=field_name,
                        value=field.value,
                    )
                )
        return warnings

    def _validate_units(
        self,
        target_type: str,
        target_id: str,
        fields: dict[str, EvidenceField],
    ) -> list[ValidationWarning]:
        warnings = []
        for field_name, field in fields.items():
            if field.value is None or not field.unit:
                continue
            normalized = self.units.normalize({"field_name": field_name, "value": field.value, "unit": field.unit})
            if normalized.get("normalized_unit") and normalized["normalized_unit"] != field.unit:
                warnings.append(
                    ValidationWarning(
                        severity="info",
                        code="unit_normalized",
                        message=f"{field.unit} normalized to {normalized['normalized_unit']}.",
                        target_type=target_type,
                        target_id=target_id,
                        field=field_name,
                        value={"from": field.unit, "to": normalized["normalized_unit"], "value": normalized.get("normalized_value")},
                    )
                )
        return warnings

    def _validate_ranges(
        self,
        target_type: str,
        target_id: str,
        fields: dict[str, EvidenceField],
    ) -> list[ValidationWarning]:
        warnings = []
        checks = {
            "cutoff_energy": (50, 1500, "eV"),
            "vacuum_thickness": (0, 80, "A"),
            "value": (-20, 20, None),
            "capacity": (0, 3000, "mAh/g"),
            "sulfur_loading": (0, 30, "mg/cm2"),
            "sulfur_content": (0, 100, "wt%"),
            "cycle_number": (0, 10000, None),
            "decay_per_cycle": (0, 100, "%/cycle"),
        }
        for field_name, (low, high, unit_hint) in checks.items():
            field = fields.get(field_name)
            if not field or field.value is None:
                continue
            number = self._safe_float(field.value)
            if number is None:
                continue
            if number < low or number > high:
                warnings.append(
                    ValidationWarning(
                        severity="warning",
                        code="out_of_expected_range",
                        message=f"{field_name}={number} is outside expected range {low}-{high}{' ' + unit_hint if unit_hint else ''}.",
                        target_type=target_type,
                        target_id=target_id,
                        field=field_name,
                        value=field.value,
                    )
                )
        return warnings

    def _validate_enums(
        self,
        target_type: str,
        target_id: str,
        fields: dict[str, EvidenceField],
    ) -> list[ValidationWarning]:
        warnings = []
        catalyst_type = fields.get("catalyst_type")
        if catalyst_type and catalyst_type.value:
            raw = str(catalyst_type.value).strip().lower()
            known = set(CATALYST_TYPE_MAP) | {"sac", "dac", "np", "cluster", "bulk", "single_atom", "dual_atom"}
            if raw not in known:
                warnings.append(
                    ValidationWarning(
                        severity="info",
                        code="unknown_catalyst_type",
                        message="Catalyst type is not in the current controlled vocabulary.",
                        target_type=target_type,
                        target_id=target_id,
                        field="catalyst_type",
                        value=catalyst_type.value,
                    )
                )

        energy_type = fields.get("energy_type")
        if energy_type and energy_type.value and str(energy_type.value) not in DFT_ENERGY_TYPES:
            warnings.append(
                ValidationWarning(
                    severity="info",
                    code="unknown_energy_type",
                    message="Energy type is not in the current DFT controlled vocabulary.",
                    target_type=target_type,
                    target_id=target_id,
                    field="energy_type",
                    value=energy_type.value,
                )
            )

        adsorbate = fields.get("adsorbate")
        if adsorbate and adsorbate.value:
            raw_ads = str(adsorbate.value).strip().lower()
            known_ads = set(ADSORBATE_MAP) | {value.lower() for value in ADSORBATE_MAP.values()}
            if raw_ads not in known_ads:
                warnings.append(
                    ValidationWarning(
                        severity="info",
                        code="unknown_adsorbate",
                        message="Adsorbate is not in the current Li-S vocabulary.",
                        target_type=target_type,
                        target_id=target_id,
                        field="adsorbate",
                        value=adsorbate.value,
                    )
                )
        return warnings

    def _validate_consistency(
        self,
        target_type: str,
        target_id: str,
        fields: dict[str, EvidenceField],
    ) -> list[ValidationWarning]:
        warnings = []
        cycle = fields.get("cycle_number")
        capacity = fields.get("capacity")
        if cycle and cycle.value is not None and capacity and capacity.value is None:
            warnings.append(
                ValidationWarning(
                    severity="warning",
                    code="cycle_without_capacity",
                    message="Cycle number was extracted but capacity is missing.",
                    target_type=target_type,
                    target_id=target_id,
                    field="capacity",
                )
            )
        energy_type = fields.get("energy_type")
        value = fields.get("value")
        if energy_type and energy_type.value and "energy" in str(energy_type.value) and value and not value.unit:
            warnings.append(
                ValidationWarning(
                    severity="warning",
                    code="energy_missing_unit",
                    message="Energy-like DFT result is missing a unit.",
                    target_type=target_type,
                    target_id=target_id,
                    field="value",
                    value=value.value,
                )
            )
        return warnings

    @staticmethod
    def _field_map(item: Any) -> dict[str, EvidenceField]:
        if hasattr(item, "model_dump"):
            data = item.model_dump()
        elif isinstance(item, dict):
            data = item
        else:
            data = {}
        fields = {}
        for key, value in data.items():
            if isinstance(value, EvidenceField):
                fields[key] = value
            elif isinstance(value, dict):
                fields[key] = EvidenceField.model_validate(value)
        return fields

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _target_id(item: Any, name: str, idx: int) -> str:
        if isinstance(item, dict) and item.get("target_id"):
            return str(item["target_id"])
        return f"{name}[{idx}]"
