from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.normalizers.chemistry_normalizer import get_property_taxonomy


MISSING_UNIT_MARKERS = {
    "", "n/a", "na", "none", "null", "unknown", "unspecified",
    "not reported", "not specified", "not specified in evidence", "source_unreported",
}

DEFAULT_UNIT_BY_DIMENSION = {
    "energy": "eV",
    "length": "Å",
    "charge": "e",
    "magnetic_moment": "μB",
    "dos": "states/eV",
    "potential": "V",
    "dimensionless": "dimensionless",
}


@dataclass(frozen=True)
class DFTUnitResolution:
    unit: str | None
    origin: str
    basis: str
    confidence: float

    @property
    def resolved(self) -> bool:
        return bool(self.unit)

    @property
    def inferred(self) -> bool:
        return self.origin == "ai_inferred"

    def as_payload(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "unit_origin": self.origin,
            "unit_inference_basis": self.basis,
            "unit_confidence": self.confidence,
        }


def resolve_dft_unit(property_type: Any, unit: Any) -> DFTUnitResolution:
    """Resolve a usable unit without pretending inferred text came from the paper."""
    raw = str(unit or "").strip()
    if raw.casefold() not in MISSING_UNIT_MARKERS:
        return DFTUnitResolution(raw, "source_explicit", "source_value", 1.0)

    taxonomy = get_property_taxonomy(str(property_type or ""))
    physical_dimension = str(taxonomy.get("physical_dimension") or "unknown")
    inferred = DEFAULT_UNIT_BY_DIMENSION.get(physical_dimension)
    basis = f"property_taxonomy:{taxonomy.get('canonical_property_type') or property_type}"
    confidence = 0.95
    if not inferred:
        heuristic_dimension = _infer_dimension_from_property_name(property_type)
        inferred = DEFAULT_UNIT_BY_DIMENSION.get(heuristic_dimension)
        if inferred:
            basis = f"property_name_heuristic:{heuristic_dimension}"
            confidence = 0.85
    if inferred:
        return DFTUnitResolution(
            inferred,
            "ai_inferred",
            basis,
            confidence,
        )
    return DFTUnitResolution(None, "unresolved", "unknown_physical_dimension", 0.0)


def _infer_dimension_from_property_name(property_type: Any) -> str:
    label = re.sub(r"[^a-z0-9]+", "_", str(property_type or "").casefold()).strip("_")
    if re.search(r"(bond_?length|distance|spacing|radius|diameter|lattice_?constant)", label):
        return "length"
    if re.search(r"(magnetic|spin)_?moment", label):
        return "magnetic_moment"
    if re.search(r"(^|_)(dos|pdos)(_|$)|density_?of_?states", label):
        return "dos"
    if re.search(r"(charge|electron_?transfer)", label):
        return "charge"
    if re.search(r"(potential|voltage)", label):
        return "potential"
    if re.search(r"(energy|barrier|enthalpy|cohp|band_?center|work_?function)", label):
        return "energy"
    if re.search(r"(occupancy|ratio|fraction|coordination_?number)", label):
        return "dimensionless"
    return "unknown"


def analysis_entity_id(row: Any) -> str:
    """Return a stable grouping key even when catalyst metadata is absent.

    Extractors may persist ``analysis_entity_id`` when two properties are known to
    describe the same anonymous source row/configuration. A row-local fallback
    keeps the observation usable without falsely pairing unrelated anonymous data.
    """
    catalyst_id = getattr(row, "catalyst_sample_id", None)
    if catalyst_id:
        return f"catalyst_sample:{catalyst_id}"

    payload = getattr(row, "evidence_payload", None)
    layers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        layers.append(payload)
        for key in ("corrected_value", "imported_evidence_payload", "material_identity_payload"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                layers.append(nested)

    for key in ("analysis_entity_id", "source_entity_id", "catalyst_group_id"):
        for layer in layers:
            value = str(layer.get(key) or "").strip()
            if value:
                return f"source_entity:{getattr(row, 'paper_id', '')}:{_token(value)}"

    for key in ("material_identity", "material", "structure_name"):
        for layer in layers:
            value = str(layer.get(key) or "").strip()
            if value:
                return f"material:{getattr(row, 'paper_id', '')}:{_token(value)}"

    table_id = _first(layers, "source_table_id", "table_id")
    row_index = _first(layers, "source_row_index", "row_index")
    configuration = _first(layers, "configuration_index", "configuration")
    if table_id and (row_index or configuration):
        return (
            f"source_row:{getattr(row, 'paper_id', '')}:"
            f"{_token(table_id)}:{_token(row_index or configuration)}"
        )

    return f"record:{getattr(row, 'paper_id', '')}:{getattr(row, 'id', '')}"


def _first(layers: list[dict[str, Any]], *keys: str) -> str | None:
    for layer in layers:
        for key in keys:
            value = str(layer.get(key) or "").strip()
            if value:
                return value
    return None


def _token(value: Any) -> str:
    token = re.sub(r"[^a-z0-9.+_-]+", "_", str(value or "").strip().casefold()).strip("_")
    return token or "unknown"
