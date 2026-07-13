from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from app.services.dft_rescan_policy import (
    normalize_dft_reaction_step_for_identity,
    normalize_numeric_value,
    normalize_source_document_type,
    normalize_unit,
)


ATOM_PAIR_ALIASES = ("atom_pair", "bond_pair", "bond", "interaction_pair")
ATOM_PAIR_IDENTITY_ERRORS = {
    "conflicting_atom_pair_aliases",
    "missing_atom_pair_identity",
}
_UNICODE_DASHES = str.maketrans({
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "﹣": "-",
    "－": "-",
})


@dataclass(frozen=True)
class AtomPairIdentity:
    canonical: str | None
    normalized_aliases: tuple[str, ...]
    error_code: str | None
    required: bool
    symmetric: bool


@dataclass(frozen=True)
class DFTScientificIdentity:
    subject_signature: str
    observation_signature: str
    atom_pair: AtomPairIdentity
    dedupe_allowed: bool
    components: dict[str, str]


def normalize_dft_property_type(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[_\s-]+", "_", text).strip("_")


def normalize_dft_value_kind(
    value: Any,
    *,
    value_upper: Any = None,
    property_type: Any = None,
) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    normalized = re.sub(r"[_\s-]+", "_", text).strip("_")
    if normalized:
        return normalized
    if value_upper not in (None, "", []):
        return "energy_window" if "window" in normalize_dft_property_type(property_type) else "range"
    return "point"


def property_requires_atom_pair(value: Any) -> bool:
    property_type = normalize_dft_property_type(value)
    return property_type.startswith("bond_length") or "icohp" in property_type or "cohp" in property_type


def property_has_symmetric_atom_pair(value: Any) -> bool:
    return property_requires_atom_pair(value)


def normalize_atom_pair(value: Any, *, symmetric: bool) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_UNICODE_DASHES)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    if not text:
        return ""
    parts = [part.strip() for part in text.split("-")]
    if symmetric and len(parts) == 2 and all(parts):
        parts = sorted(parts)
        return "-".join(parts)
    return text


def resolve_atom_pair_identity(
    payload: dict[str, Any] | None,
    *,
    property_type: Any = None,
    extra_sources: Iterable[dict[str, Any] | None] = (),
) -> AtomPairIdentity:
    root = payload if isinstance(payload, dict) else {}
    effective_property = property_type or _first_value(
        _payload_sources(root),
        ("normalized_property_type", "property_type", "property", "energy_type", "normalized_energy_type"),
    )
    required = property_requires_atom_pair(effective_property)
    symmetric = property_has_symmetric_atom_pair(effective_property)
    normalized: list[str] = []
    for source in (*_payload_sources(root), *(source for source in extra_sources if isinstance(source, dict))):
        for alias in ATOM_PAIR_ALIASES:
            value = source.get(alias)
            if value in (None, "", []):
                continue
            canonical = normalize_atom_pair(value, symmetric=symmetric)
            if canonical and canonical not in normalized:
                normalized.append(canonical)
    if len(normalized) > 1:
        return AtomPairIdentity(
            canonical=None,
            normalized_aliases=tuple(sorted(normalized)),
            error_code="conflicting_atom_pair_aliases",
            required=required,
            symmetric=symmetric,
        )
    if not normalized:
        return AtomPairIdentity(
            canonical=None,
            normalized_aliases=(),
            error_code="missing_atom_pair_identity" if required else None,
            required=required,
            symmetric=symmetric,
        )
    return AtomPairIdentity(
        canonical=normalized[0],
        normalized_aliases=(normalized[0],),
        error_code=None,
        required=required,
        symmetric=symmetric,
    )


def build_dft_scientific_identity(payload: dict[str, Any]) -> DFTScientificIdentity:
    sources = _payload_sources(payload)
    property_type = _first_value(
        sources,
        ("normalized_property_type", "property_type", "property", "energy_type", "normalized_energy_type"),
    )
    material = _first_value(
        sources,
        ("normalized_material_or_catalyst", "normalized_material", "material_identity", "material", "catalyst", "catalyst_name"),
    )
    adsorbate = _first_value(sources, ("normalized_adsorbate", "adsorbate"))
    atom_pair = resolve_atom_pair_identity(payload, property_type=property_type)
    source_type = normalize_source_document_type(
        _first_value(sources, ("source_document_type", "source_type"))
    )
    source_bucket = "supporting_reference" if source_type == "supporting_reference" else "paper_owned"
    atom_component = atom_pair.canonical or (
        f"{atom_pair.error_code}:{'|'.join(atom_pair.normalized_aliases)}" if atom_pair.error_code else ""
    )
    components = {
        "paper_id": _text(payload.get("paper_id")),
        "source_bucket": source_bucket,
        "material": _text(material),
        "active_site_instance_key": _text(_first_value(sources, ("active_site_instance_key",))),
        "adsorbate": _text(adsorbate),
        "property_type": normalize_dft_property_type(property_type),
        "property_subtype": _text(_first_value(sources, ("property_subtype", "normalized_property_subtype"))),
        "reaction_step": normalize_dft_reaction_step_for_identity(
            _first_value(sources, ("normalized_reaction_step", "reaction_step")),
            property_type=property_type,
            adsorbate=adsorbate,
            material=material,
        ),
        "atom_pair": atom_component,
        "site_label": _text(_first_value(sources, ("site_label", "adsorption_site", "site"))),
        "state_context": _text(_first_value(sources, ("state_context",))),
    }
    subject_signature = _signature("dft-subject", components)
    value_upper = _first_value(sources, ("normalized_value_upper", "value_upper"))
    observation = {
        "subject_signature": subject_signature,
        "value": normalize_numeric_value(_first_value(sources, ("normalized_value", "value"))),
        "value_upper": normalize_numeric_value(value_upper),
        "value_kind": normalize_dft_value_kind(
            _first_value(sources, ("normalized_value_kind", "value_kind", "value_type")),
            value_upper=value_upper,
            property_type=property_type,
        ),
        "unit": normalize_unit(_first_value(sources, ("normalized_unit", "unit"))),
    }
    return DFTScientificIdentity(
        subject_signature=subject_signature,
        observation_signature=_signature("dft", observation),
        atom_pair=atom_pair,
        dedupe_allowed=atom_pair.error_code is None,
        components=components,
    )


def _payload_sources(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    sources: list[dict[str, Any]] = [payload]
    for key in ("corrected_value", "evidence_payload", "evidence_location"):
        value = payload.get(key)
        if isinstance(value, dict):
            sources.append(value)
            nested_corrected = value.get("corrected_value")
            if isinstance(nested_corrected, dict):
                sources.append(nested_corrected)
    return tuple(sources)


def _first_value(sources: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", []):
                return value
    return None


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _signature(prefix: str, parts: dict[str, Any]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"
