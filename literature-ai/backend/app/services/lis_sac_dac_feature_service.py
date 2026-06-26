from __future__ import annotations

import re
from typing import Any

from app.db.models import CatalystSample, DFTResult, ElectrochemicalPerformance
from app.domain.lis_sac_dac_field_dictionary import list_topic_field_definitions
from app.domain.project_library_context import get_project_library_context
from app.schemas.project_library_features import (
    ProjectLibraryFeatureExtractionPayload,
    ProjectLibraryFeatureValue,
)


_METAL_SYMBOLS = {
    "Li",
    "Be",
    "Na",
    "Mg",
    "Al",
    "K",
    "Ca",
    "Sc",
    "Ti",
    "V",
    "Cr",
    "Mn",
    "Fe",
    "Co",
    "Ni",
    "Cu",
    "Zn",
    "Ga",
    "Rb",
    "Sr",
    "Y",
    "Zr",
    "Nb",
    "Mo",
    "Tc",
    "Ru",
    "Rh",
    "Pd",
    "Ag",
    "Cd",
    "In",
    "Sn",
    "Cs",
    "Ba",
    "La",
    "Ce",
    "Pr",
    "Nd",
    "Pm",
    "Sm",
    "Eu",
    "Gd",
    "Tb",
    "Dy",
    "Ho",
    "Er",
    "Tm",
    "Yb",
    "Lu",
    "Hf",
    "Ta",
    "W",
    "Re",
    "Os",
    "Ir",
    "Pt",
    "Au",
    "Hg",
    "Tl",
    "Pb",
    "Bi",
}
_WEAK_TEXT_EXCLUDED_SYMBOLS = {"Li", "S", "C", "N", "O", "H"}
_LI_S_SPECIES_PATTERN = re.compile(
    r"\b(?:lips|li2s\d*|li\d+s\d+|s8)\b",
    re.IGNORECASE,
)
_STRUCTURE_OPTIONAL_BLOCKERS = {"missing_support_material", "missing_metal_metal_distance"}
_CAPACITY_UNITS = {
    "mah g^-1": "mAh g^-1",
    "mah/g": "mAh g^-1",
    "mahg-1": "mAh g^-1",
    "mah g-1": "mAh g^-1",
    "ma h g^-1": "mAh g^-1",
}
_RATE_UNITS = {"c": "C"}
_CYCLES_UNITS = {"cycle": "cycles", "cycles": "cycles"}
_DECAY_UNITS = {
    "% per cycle": "% per cycle",
    "percent per cycle": "% per cycle",
    "%/cycle": "% per cycle",
}
_SULFUR_LOADING_UNITS = {
    "mg cm^-2": "mg cm^-2",
    "mg/cm^2": "mg cm^-2",
    "mg cm-2": "mg cm^-2",
}
_ES_UNITS = {
    "ul mg^-1": "uL mg^-1",
    "ul/mg": "uL mg^-1",
    "μl mg^-1": "uL mg^-1",
    "µl mg^-1": "uL mg^-1",
}
_DISTANCE_UNITS = {"a": "angstrom", "å": "angstrom", "angstrom": "angstrom"}


class LiSSacDacFeatureService:
    def __init__(self, *, context_key: str = "li_s_sac_dac") -> None:
        self._context = get_project_library_context(context_key)
        self._field_keys = {
            field.canonical_key
            for field in list_topic_field_definitions(self._context.key)
        }

    def extract_structure_features(
        self,
        *,
        catalyst_sample: CatalystSample | None = None,
        dft_result: DFTResult | None = None,
        candidate_payload: dict[str, Any] | None = None,
        evidence_payload: dict[str, Any] | None = None,
    ) -> ProjectLibraryFeatureExtractionPayload:
        payloads = self._payloads(candidate_payload, evidence_payload, dft_result)
        blockers: list[str] = []
        fields: dict[str, ProjectLibraryFeatureValue] = {}

        metal_centers, metal_source, metal_blocker = self._resolve_metal_centers(catalyst_sample, payloads)
        if metal_centers:
            fields["metal_centers"] = self._field_value(
                "metal_centers",
                metal_centers,
                source=metal_source,
                normalized=True,
            )
        else:
            fields["metal_centers"] = self._unknown_field("metal_centers")
        if metal_blocker:
            blockers.append(metal_blocker)

        catalyst_scope, scope_source, scope_blocker = self._resolve_catalyst_scope(catalyst_sample, payloads)
        if catalyst_scope:
            fields["catalyst_scope"] = self._field_value(
                "catalyst_scope",
                catalyst_scope,
                source=scope_source,
                normalized=True,
            )
        else:
            blockers.append(scope_blocker)
            fields["catalyst_scope"] = self._unknown_field("catalyst_scope")

        pairing_value, pairing_source, pairing_blocker = self._resolve_pairing_type(payloads, catalyst_scope, metal_centers)
        if pairing_value:
            fields["metal_pairing_type"] = self._field_value(
                "metal_pairing_type",
                pairing_value,
                source=pairing_source,
                normalized=True,
            )
        else:
            fields["metal_pairing_type"] = self._unknown_field("metal_pairing_type")
            if pairing_blocker:
                blockers.append(pairing_blocker)

        support_value, support_source = self._first_candidate(payloads, "support_material", "support")
        if support_value is None and catalyst_sample is not None:
            support_value = catalyst_sample.support
            support_source = "catalyst_sample.support"
        if self._text(support_value):
            fields["support_material"] = self._field_value(
                "support_material",
                self._text(support_value),
                source=support_source,
                normalized=True,
            )
        else:
            blockers.append("missing_support_material")
            fields["support_material"] = self._unknown_field("support_material")

        coordination_value, coordination_source = self._first_candidate(payloads, "coordination_environment", "coordination")
        if coordination_value is None and catalyst_sample is not None:
            coordination_value = catalyst_sample.coordination
            coordination_source = "catalyst_sample.coordination"
        if self._text(coordination_value):
            fields["coordination_environment"] = self._field_value(
                "coordination_environment",
                self._text(coordination_value),
                source=coordination_source,
                normalized=True,
            )
        else:
            blockers.append("missing_coordination_environment")
            fields["coordination_environment"] = self._unknown_field("coordination_environment")

        distance_value, distance_unit, distance_source, distance_blocker = self._resolve_distance(payloads)
        if distance_value is not None:
            fields["metal_metal_distance"] = self._field_value(
                "metal_metal_distance",
                distance_value,
                unit=distance_unit,
                source=distance_source,
                normalized=True,
            )
        else:
            fields["metal_metal_distance"] = self._unknown_field("metal_metal_distance")
            if catalyst_scope == "DAC":
                blockers.append(distance_blocker or "missing_metal_metal_distance")

        status = self._structure_status(blockers)
        return ProjectLibraryFeatureExtractionPayload(
            context_key=self._context.key,
            feature_set="structure",
            status=status,
            blockers=self._ordered_blockers(blockers),
            fields=fields,
        )

    def extract_experimental_performance_features(
        self,
        *,
        performance: ElectrochemicalPerformance | None = None,
        candidate_payload: dict[str, Any] | None = None,
        evidence_payload: dict[str, Any] | None = None,
    ) -> ProjectLibraryFeatureExtractionPayload:
        payloads = self._payloads(candidate_payload, evidence_payload)
        blockers: list[str] = []
        fields: dict[str, ProjectLibraryFeatureValue] = {}

        capacity_value, capacity_unit, capacity_source, capacity_blocker = self._resolve_measurement(
            payloads,
            key="specific_capacity",
            accepted_units=_CAPACITY_UNITS,
            blocker_prefix="specific_capacity",
        )
        if capacity_value is not None:
            fields["specific_capacity"] = self._field_value(
                "specific_capacity",
                capacity_value,
                unit=capacity_unit,
                source=capacity_source,
                normalized=True,
            )
        else:
            fields["specific_capacity"] = self._unknown_field("specific_capacity")
            if capacity_blocker:
                blockers.append(capacity_blocker)

        rate_value, rate_unit, rate_source, rate_blocker = self._resolve_rate(payloads, performance)
        if rate_value is not None:
            fields["rate_c_value"] = self._field_value(
                "rate_c_value",
                rate_value,
                unit=rate_unit,
                source=rate_source,
                normalized=True,
            )
        else:
            fields["rate_c_value"] = self._unknown_field("rate_c_value")
            if rate_blocker:
                blockers.append(rate_blocker)

        cycles_value, cycles_unit, cycles_source, cycles_blocker = self._resolve_measurement(
            payloads,
            key="cycling_stability_cycles",
            accepted_units=_CYCLES_UNITS,
            blocker_prefix="cycling_stability_cycles",
        )
        if cycles_value is None and performance is not None and performance.cycle_number is not None:
            cycles_value = performance.cycle_number
            cycles_unit = "cycles"
            cycles_source = "electrochemical_performance.cycle_number"
        if cycles_value is not None:
            fields["cycling_stability_cycles"] = self._field_value(
                "cycling_stability_cycles",
                cycles_value,
                unit=cycles_unit,
                source=cycles_source,
                normalized=True,
            )
        else:
            fields["cycling_stability_cycles"] = self._unknown_field("cycling_stability_cycles")
            if cycles_blocker:
                blockers.append(cycles_blocker)

        decay_value, decay_unit, decay_source, decay_blocker = self._resolve_measurement(
            payloads,
            key="capacity_decay_rate",
            accepted_units=_DECAY_UNITS,
            blocker_prefix="capacity_decay_rate",
        )
        if decay_value is None and performance is not None and performance.decay_per_cycle is not None:
            decay_value = performance.decay_per_cycle
            decay_unit = "% per cycle"
            decay_source = "electrochemical_performance.decay_per_cycle"
        if decay_value is not None:
            fields["capacity_decay_rate"] = self._field_value(
                "capacity_decay_rate",
                decay_value,
                unit=decay_unit,
                source=decay_source,
                normalized=True,
            )
        else:
            fields["capacity_decay_rate"] = self._unknown_field("capacity_decay_rate")
            if decay_blocker:
                blockers.append(decay_blocker)

        sulfur_value, sulfur_unit, sulfur_source, sulfur_blocker = self._resolve_measurement(
            payloads,
            key="sulfur_loading",
            accepted_units=_SULFUR_LOADING_UNITS,
            blocker_prefix="sulfur_loading",
        )
        if sulfur_value is None and performance is not None and performance.sulfur_loading_mg_cm2 is not None:
            sulfur_value = performance.sulfur_loading_mg_cm2
            sulfur_unit = "mg cm^-2"
            sulfur_source = "electrochemical_performance.sulfur_loading_mg_cm2"
        if sulfur_value is not None:
            fields["sulfur_loading"] = self._field_value(
                "sulfur_loading",
                sulfur_value,
                unit=sulfur_unit,
                source=sulfur_source,
                normalized=True,
            )
        else:
            fields["sulfur_loading"] = self._unknown_field("sulfur_loading")
            if sulfur_blocker:
                blockers.append(sulfur_blocker)

        es_value, es_unit, es_source, es_blocker = self._resolve_measurement(
            payloads,
            key="electrolyte_to_sulfur_ratio",
            accepted_units=_ES_UNITS,
            blocker_prefix="electrolyte_to_sulfur_ratio",
        )
        if es_value is None and performance is not None and self._text(performance.electrolyte_sulfur_ratio):
            parsed = self._parse_inline_numeric_unit(performance.electrolyte_sulfur_ratio)
            if parsed and self._normalize_unit(parsed[1], _ES_UNITS):
                es_value = parsed[0]
                es_unit = self._normalize_unit(parsed[1], _ES_UNITS)
                es_source = "electrochemical_performance.electrolyte_sulfur_ratio"
            elif parsed:
                es_blocker = "unsupported_electrolyte_to_sulfur_ratio_unit"
        if es_value is not None:
            fields["electrolyte_to_sulfur_ratio"] = self._field_value(
                "electrolyte_to_sulfur_ratio",
                es_value,
                unit=es_unit,
                source=es_source,
                normalized=True,
            )
        else:
            fields["electrolyte_to_sulfur_ratio"] = self._unknown_field("electrolyte_to_sulfur_ratio")
            if es_blocker:
                blockers.append(es_blocker)

        status = self._performance_status(fields, blockers)
        return ProjectLibraryFeatureExtractionPayload(
            context_key=self._context.key,
            feature_set="experimental_performance",
            status=status,
            blockers=self._ordered_blockers(blockers),
            fields=fields,
        )

    def _payloads(
        self,
        candidate_payload: dict[str, Any] | None,
        evidence_payload: dict[str, Any] | None,
        dft_result: DFTResult | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        payloads: list[tuple[str, dict[str, Any]]] = []
        for name, payload in (
            ("candidate_payload", candidate_payload),
            ("evidence_payload", evidence_payload),
            ("dft_result.evidence_payload", dft_result.evidence_payload if dft_result is not None else None),
        ):
            if isinstance(payload, dict):
                payloads.append((name, payload))
                for nested_key in ("fields", "structure", "experimental_performance"):
                    nested = payload.get(nested_key)
                    if isinstance(nested, dict):
                        payloads.append((f"{name}.{nested_key}", nested))
        return payloads

    def _resolve_metal_centers(
        self,
        catalyst_sample: CatalystSample | None,
        payloads: list[tuple[str, dict[str, Any]]],
    ) -> tuple[list[str] | None, str | None, str | None]:
        if catalyst_sample is not None:
            centers = self._normalize_metal_centers(catalyst_sample.metal_centers)
            if centers:
                return centers, "catalyst_sample.metal_centers", None

        value, source = self._first_candidate(payloads, "metal_centers", "metal_center", "metal_sites")
        centers = self._normalize_metal_centers(value)
        if centers:
            return centers, source, None

        material_value, material_source = self._first_candidate(payloads, "material_identity", "catalyst_name")
        material_text = self._text(material_value)
        centers = self._normalize_metal_centers_from_weak_text(material_value)
        if centers:
            blocker = "ambiguous_metal_centers_weak_text" if self._contains_li_s_species(material_text) else "weak_metal_centers_source"
            return centers, material_source, blocker
        return None, None, "missing_metal_centers"

    def _resolve_catalyst_scope(
        self,
        catalyst_sample: CatalystSample | None,
        payloads: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str | None, str | None, str]:
        values: list[tuple[str, str]] = []
        if catalyst_sample is not None and self._text(catalyst_sample.catalyst_type):
            values.append((str(catalyst_sample.catalyst_type), "catalyst_sample.catalyst_type"))
        for key in ("catalyst_scope", "catalyst_type"):
            candidate, source = self._first_candidate(payloads, key)
            if self._text(candidate):
                values.append((str(candidate), source or key))

        scopes = {self._normalize_scope(raw) for raw, _ in values if self._normalize_scope(raw)}
        if len(scopes) == 1:
            scope = next(iter(scopes))
            source = next(source for raw, source in values if self._normalize_scope(raw) == scope)
            return scope, source, "missing_catalyst_scope"
        if len(scopes) > 1:
            return None, None, "ambiguous_catalyst_scope"

        raw_text = " ".join(raw for raw, _ in values).lower()
        if "atomically dispersed" in raw_text:
            return None, None, "ambiguous_catalyst_scope"
        return None, None, "missing_catalyst_scope"

    def _resolve_pairing_type(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        catalyst_scope: str | None,
        metal_centers: list[str] | None,
    ) -> tuple[str | None, str | None, str | None]:
        explicit, source = self._first_candidate(payloads, "metal_pairing_type", "pairing_type")
        explicit_text = self._text(explicit)
        if explicit_text:
            lowered = explicit_text.lower()
            if lowered in {"homonuclear", "heteronuclear"}:
                return lowered, source, None
            return None, None, "ambiguous_metal_pairing_type"
        if catalyst_scope != "DAC":
            return None, None, None
        if not metal_centers or len(metal_centers) != 2:
            return None, None, "missing_metal_pairing_type"
        return (
            "homonuclear" if metal_centers[0] == metal_centers[1] else "heteronuclear",
            "derived_from_metal_centers",
            None,
        )

    def _resolve_distance(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
    ) -> tuple[float | None, str | None, str | None, str | None]:
        value, source = self._first_candidate(payloads, "metal_metal_distance", "m_m_distance", "mm_distance")
        parsed = self._parse_measurement_value(value)
        if parsed is None:
            return None, None, None, None
        number, unit = parsed
        normalized_unit = self._normalize_unit(unit, _DISTANCE_UNITS)
        if normalized_unit is None:
            return None, None, None, "ambiguous_metal_metal_distance"
        return number, normalized_unit, source, None

    def _resolve_measurement(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        *,
        key: str,
        accepted_units: dict[str, str],
        blocker_prefix: str,
    ) -> tuple[float | int | None, str | None, str | None, str | None]:
        value, source = self._first_candidate(payloads, key)
        parsed = self._parse_measurement_value(value)
        if parsed is None:
            return None, None, None, None
        number, unit = parsed
        normalized_unit = self._normalize_unit(unit, accepted_units)
        if normalized_unit is None:
            return None, None, None, f"unsupported_{blocker_prefix}_unit"
        return number, normalized_unit, source, None

    def _resolve_rate(
        self,
        payloads: list[tuple[str, dict[str, Any]]],
        performance: ElectrochemicalPerformance | None,
    ) -> tuple[float | None, str | None, str | None, str | None]:
        value, source = self._first_candidate(payloads, "rate_c_value", "rate")
        parsed = self._parse_measurement_value(value)
        if parsed is None and performance is not None and self._text(performance.rate):
            parsed = self._parse_inline_numeric_unit(performance.rate)
            source = "electrochemical_performance.rate"
        if parsed is None:
            return None, None, None, None
        number, unit = parsed
        normalized_unit = self._normalize_unit(unit, _RATE_UNITS)
        if normalized_unit is not None:
            return number, normalized_unit, source, None
        if "ma" in self._unit_token(unit):
            return None, None, None, "rate_requires_conversion"
        return None, None, None, "unsupported_rate_c_value_unit"

    def _parse_measurement_value(self, value: Any) -> tuple[float | int, str] | None:
        if isinstance(value, dict):
            number = self._numeric(value.get("value"))
            unit = self._text(value.get("unit"))
            if number is not None and unit:
                return number, unit
            return None
        if isinstance(value, (int, float)):
            return float(value), ""
        if isinstance(value, str):
            return self._parse_inline_numeric_unit(value)
        return None

    def _parse_inline_numeric_unit(self, value: str) -> tuple[float, str] | None:
        match = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%μµÅ/\-^0-9 ]+)\s*$", value)
        if not match:
            return None
        return float(match.group(1)), match.group(2).strip()

    def _normalize_metal_centers(self, value: Any) -> list[str] | None:
        if isinstance(value, list):
            centers = [self._normalize_symbol(item) for item in value]
            cleaned = [item for item in centers if item]
            return cleaned or None
        text = self._text(value)
        if not text:
            return None
        tokens = re.findall(r"[A-Z][a-z]?", text)
        centers = [token for token in tokens if token in _METAL_SYMBOLS]
        unique_centers: list[str] = []
        for center in centers:
            if center not in unique_centers:
                unique_centers.append(center)
        return unique_centers or None

    def _normalize_metal_centers_from_weak_text(self, value: Any) -> list[str] | None:
        centers = self._normalize_metal_centers(value)
        if not centers:
            return None
        filtered = [center for center in centers if center not in _WEAK_TEXT_EXCLUDED_SYMBOLS]
        return filtered or None

    def _normalize_symbol(self, value: Any) -> str | None:
        text = self._text(value)
        if not text:
            return None
        candidate = text[:1].upper() + text[1:].lower()
        return candidate if candidate in _METAL_SYMBOLS else None

    def _normalize_scope(self, value: Any) -> str | None:
        text = self._text(value)
        if not text:
            return None
        lowered = text.lower()
        if "dual atom" in lowered or lowered == "dac" or "dual-atom" in lowered:
            return "DAC"
        if "single atom" in lowered or lowered == "sac" or "single-atom" in lowered:
            return "SAC"
        return None

    def _contains_li_s_species(self, value: str | None) -> bool:
        if not value:
            return False
        return bool(_LI_S_SPECIES_PATTERN.search(value))

    def _normalize_unit(self, unit: str | None, accepted_units: dict[str, str]) -> str | None:
        token = self._unit_token(unit)
        if not token:
            return None
        return accepted_units.get(token)

    def _unit_token(self, unit: str | None) -> str:
        return re.sub(r"\s+", " ", str(unit or "").strip().lower())

    def _first_candidate(self, payloads: list[tuple[str, dict[str, Any]]], *keys: str) -> tuple[Any, str | None]:
        for source, payload in payloads:
            for key in keys:
                if key in payload and payload.get(key) not in (None, "", [], {}):
                    return payload.get(key), f"{source}.{key}"
        return None, None

    def _numeric(self, value: Any) -> float | int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value
        if isinstance(value, str) and re.match(r"^\s*[-+]?\d+(?:\.\d+)?\s*$", value):
            return float(value)
        return None

    def _text(self, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    def _field_value(
        self,
        canonical_key: str,
        value: Any,
        *,
        unit: str | None = None,
        source: str | None = None,
        normalized: bool = False,
    ) -> ProjectLibraryFeatureValue:
        return ProjectLibraryFeatureValue(
            canonical_key=canonical_key,
            value=value,
            unit=unit,
            source=source,
            normalized=normalized,
        )

    def _unknown_field(self, canonical_key: str) -> ProjectLibraryFeatureValue:
        return ProjectLibraryFeatureValue(
            canonical_key=canonical_key,
            unknown=True,
        )

    def _ordered_blockers(self, blockers: list[str]) -> list[str]:
        deduped: list[str] = []
        for blocker in blockers:
            if blocker and blocker not in deduped:
                deduped.append(blocker)
        return deduped

    def _structure_status(self, blockers: list[str]) -> str:
        ordered = self._ordered_blockers(blockers)
        if not ordered:
            return "ready"
        if set(ordered).issubset(_STRUCTURE_OPTIONAL_BLOCKERS):
            return "candidate_usable"
        return "needs_fields"

    def _performance_status(
        self,
        fields: dict[str, ProjectLibraryFeatureValue],
        blockers: list[str],
    ) -> str:
        normalized_count = sum(1 for field in fields.values() if field.normalized)
        ordered = self._ordered_blockers(blockers)
        if normalized_count == 0:
            return "needs_fields"
        if ordered:
            return "needs_fields"
        if normalized_count >= 4:
            return "ready"
        return "candidate_usable"
