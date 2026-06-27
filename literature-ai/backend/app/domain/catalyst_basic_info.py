from __future__ import annotations

from typing import Any

from app.domain.element_descriptors import build_metal_descriptor_payload, normalize_element_symbol


CATALYST_BASIC_INFO_SCHEMA_VERSION = "catalyst_basic_info_v1"

ALLOWED_CATALYST_TYPES = {
    "single_atom",
    "dual_atom",
    "multi_atom_cluster",
    "surface",
    "defect_site",
    "unknown",
}

SUPPORT_ALIASES = {
    "graphene": {"graphene", "gr", "graphene sheet", "graphene substrate", "graphene nanosheet", "graphene nanosheets"},
    "N_doped_carbon": {"n-c", "nc", "n doped carbon", "n-doped carbon", "nitrogen doped carbon", "nitrogen-doped carbon"},
    "carbon": {"carbon", "carbon support", "carbon matrix", "carbon substrate"},
    "C3N4": {"c3n4", "g-c3n4", "graphitic carbon nitride", "carbon nitride"},
    "C2N": {"c2n", "c2n monolayer"},
    "GeC": {"gec", "gec monolayer", "germanium carbide"},
    "MoS2": {"mos2", "mos2 monolayer"},
    "MXene": {"mxene", "ti3c2", "ti3c2 mxene"},
    "TiO2": {"tio2", "titanium dioxide"},
    "CeO2": {"ceo2", "ceria"},
    "UNKNOWN": {"unknown", "unclear", "not reported", "n/a", "na"},
}

ALLOWED_SUPPORTS = set(SUPPORT_ALIASES)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_catalyst_type(value: Any) -> tuple[str | None, str | None]:
    raw = _clean_text(value)
    if not raw:
        return None, None
    token = raw.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "sac": "single_atom",
        "single_atom_catalyst": "single_atom",
        "single_atom": "single_atom",
        "dac": "dual_atom",
        "dual_atom_catalyst": "dual_atom",
        "dual_atom": "dual_atom",
        "cluster": "multi_atom_cluster",
        "multi_atom_cluster": "multi_atom_cluster",
        "surface": "surface",
        "defect": "defect_site",
        "defect_site": "defect_site",
        "unknown": "unknown",
    }
    normalized = aliases.get(token)
    if normalized in ALLOWED_CATALYST_TYPES:
        return normalized, raw
    return "unknown", raw


def normalize_support(value: Any) -> tuple[str | None, str | None, str | None]:
    raw = _clean_text(value)
    if not raw:
        return None, None, None
    comparable = raw.lower().replace("_", " ").replace("–", "-").replace("—", "-")
    comparable = " ".join(comparable.split())
    for canonical, aliases in SUPPORT_ALIASES.items():
        if comparable in aliases:
            return canonical, raw, comparable
    if comparable.startswith("other:"):
        return "other", raw, comparable
    return "other", raw, comparable


def normalize_metal_centers(values: Any) -> tuple[list[str], list[Any]]:
    raw_values = values if isinstance(values, list) else ([] if values in (None, "") else [values])
    normalized: list[str] = []
    rejected: list[Any] = []
    for value in raw_values:
        symbol = normalize_element_symbol(value)
        if not symbol:
            rejected.append(value)
            continue
        if symbol not in normalized:
            normalized.append(symbol)
    return normalized, rejected


def catalyst_basic_info_payload(
    *,
    name: Any = None,
    catalyst_type: Any = None,
    metal_centers: Any = None,
    support: Any = None,
    coordination: Any = None,
    synthesis_method: Any = None,
    evidence_strength: Any = None,
) -> dict[str, Any]:
    normalized_type, raw_type = normalize_catalyst_type(catalyst_type)
    normalized_support, raw_support, support_alias = normalize_support(support)
    normalized_metals, rejected_metals = normalize_metal_centers(metal_centers)
    descriptor_payload = build_metal_descriptor_payload(normalized_metals)
    return {
        "schema_version": CATALYST_BASIC_INFO_SCHEMA_VERSION,
        "fields": {
            "name": _clean_text(name) or None,
            "catalyst_type": normalized_type,
            "metal_centers": normalized_metals,
            "coordination": _clean_text(coordination) or None,
            "support": normalized_support,
            "synthesis_method": _clean_text(synthesis_method) or None,
            "evidence_strength": _clean_text(evidence_strength) or None,
        },
        "raw": {
            "catalyst_type": raw_type,
            "support": raw_support,
            "support_alias_used": support_alias,
            "rejected_metal_centers": rejected_metals,
        },
        "allowed_values": {
            "catalyst_type": sorted(ALLOWED_CATALYST_TYPES),
            "support": sorted(ALLOWED_SUPPORTS | {"other"}),
        },
        "metal_descriptors": descriptor_payload,
        "normalization_source": "system_dictionary",
    }
