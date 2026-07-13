from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


@dataclass(frozen=True)
class DFTIdentityV2:
    identity_version: int
    subject_key: str
    observation_key: str | None
    identity_payload: dict[str, Any]
    atom_pair: AtomPairIdentity
    error_codes: tuple[str, ...]
    dedupe_allowed: bool

    @property
    def error_code(self) -> str | None:
        return self.error_codes[0] if self.error_codes else None


@dataclass(frozen=True)
class DFTIdentityRequirement:
    error_code: str
    any_of: tuple[str, ...]


@dataclass(frozen=True)
class DFTIdentityPropertyPolicy:
    name: str
    exact_property_types: frozenset[str]
    property_markers: tuple[str, ...]
    allowed_context_keys: frozenset[str]
    requirements: tuple[DFTIdentityRequirement, ...]
    dimensionless: bool = False


_V2_PROVENANCE_KEYS = {
    "candidate",
    "candidate_id",
    "chunk_id",
    "created_at",
    "evidence",
    "evidence_id",
    "figure",
    "figure_id",
    "page",
    "page_number",
    "row",
    "row_id",
    "row_index",
    "run_id",
    "source",
    "source_candidate_id",
    "source_document_type",
    "source_id",
    "source_identity",
    "source_label",
    "source_page",
    "source_path",
    "source_row",
    "source_row_index",
    "source_run_id",
    "source_table",
    "source_type",
    "source_url",
    "table",
    "table_id",
    "updated_at",
}
_V2_COMMON_CONTEXT_KEYS = frozenset({
    "charge_state",
    "configuration",
    "coverage",
    "facet",
    "spin_state",
    "surface",
    "termination",
})
_V2_ENERGY_CONTEXT_KEYS = frozenset({
    "dispersion_correction",
    "electrode_potential",
    "electric_field",
    "functional",
    "pressure",
    "solvation_model",
    "temperature",
})
_V2_REACTION_CONTEXT_KEYS = frozenset({
    "final_state",
    "initial_state",
    "pathway",
    "transition_state",
})
_V2_ELECTRONIC_CONTEXT_KEYS = frozenset({
    "band_gap_type",
    "energy_reference",
    "functional",
    "orbital",
    "soc",
    "spin_channel",
})
_V2_CHARGE_CONTEXT_KEYS = frozenset({"charge_scheme", "functional", "partitioning_method"})
_V2_BOND_CONTEXT_KEYS = frozenset({"bond_state", "functional", "spin_channel"})
_V2_CONTEXT_ALIASES = {
    "exchange_correlation_functional": "functional",
    "xc_functional": "functional",
    "adsorption_configuration": "configuration",
    "site_configuration": "configuration",
    "surface_facet": "facet",
}
_V2_COMMON_REQUIREMENTS = (
    DFTIdentityRequirement("missing_paper_identity", ("paper_id",)),
    DFTIdentityRequirement("missing_material_identity", ("material_key",)),
    DFTIdentityRequirement("missing_property_type_identity", ("property_type",)),
)
_V2_ADSORPTION_REQUIREMENTS = (
    DFTIdentityRequirement("missing_adsorbate_identity", ("adsorbate",)),
)
_V2_REACTION_BARRIER_REQUIREMENTS = (
    DFTIdentityRequirement("missing_reaction_step_identity", ("reaction_step",)),
    DFTIdentityRequirement(
        "missing_state_context_identity",
        (
            "state_context",
            "property_context.configuration",
            "property_context.initial_state",
            "property_context.transition_state",
            "property_context.final_state",
        ),
    ),
)
_V2_ATOM_OR_SITE_REQUIREMENTS = (
    DFTIdentityRequirement(
        "missing_atom_or_site_identity",
        ("active_site_instance_key", "site_label", "canonical_atom_pair"),
    ),
)


def _v2_policy(
    name: str,
    *,
    exact_property_types: Iterable[str] = (),
    property_markers: Iterable[str] = (),
    context_keys: Iterable[str] = (),
    requirements: tuple[DFTIdentityRequirement, ...] = (),
    dimensionless: bool = False,
) -> DFTIdentityPropertyPolicy:
    return DFTIdentityPropertyPolicy(
        name=name,
        exact_property_types=frozenset(exact_property_types),
        property_markers=tuple(property_markers),
        allowed_context_keys=frozenset(_V2_COMMON_CONTEXT_KEYS | frozenset(context_keys)),
        requirements=(*_V2_COMMON_REQUIREMENTS, *requirements),
        dimensionless=dimensionless,
    )


_V2_PROPERTY_POLICIES = (
    _v2_policy(
        "dimensionless",
        exact_property_types=("coordination_number", "dimensionless_ratio", "poisson_ratio"),
        dimensionless=True,
    ),
    _v2_policy(
        "reaction_barrier",
        property_markers=("reaction_barrier", "activation_barrier", "activation_energy"),
        context_keys=(_V2_ENERGY_CONTEXT_KEYS | _V2_REACTION_CONTEXT_KEYS),
        requirements=_V2_REACTION_BARRIER_REQUIREMENTS,
    ),
    _v2_policy(
        "adsorption_energy",
        property_markers=("adsorption_energy",),
        context_keys=_V2_ENERGY_CONTEXT_KEYS,
        requirements=_V2_ADSORPTION_REQUIREMENTS,
    ),
    _v2_policy(
        "atomic_charge",
        property_markers=("bader", "lowdin"),
        context_keys=_V2_CHARGE_CONTEXT_KEYS,
        requirements=_V2_ATOM_OR_SITE_REQUIREMENTS,
    ),
    _v2_policy(
        "electronic_structure",
        property_markers=("band", "dos", "pdos", "orbital"),
        context_keys=_V2_ELECTRONIC_CONTEXT_KEYS,
    ),
    _v2_policy(
        "bond",
        property_markers=("bond", "icohp", "cohp"),
        context_keys=(_V2_BOND_CONTEXT_KEYS | _V2_ENERGY_CONTEXT_KEYS),
    ),
    _v2_policy(
        "energy",
        property_markers=("energy",),
        context_keys=_V2_ENERGY_CONTEXT_KEYS,
    ),
)
_V2_DEFAULT_PROPERTY_POLICY = _v2_policy("default")

_V2_VALUE_KIND_ALIASES = {
    "point": "point",
    "scalar": "point",
    "single": "point",
    "single_value": "point",
    "range": "range",
    "interval": "range",
    "min_max": "range",
    "lower_upper": "range",
    "energy_window": "energy_window",
    "energy_range": "energy_window",
    "window": "energy_window",
}
_V2_UNIT_RULES: dict[str, tuple[str, Decimal]] = {
    "ev": ("eV", Decimal("1")),
    "electronvolt": ("eV", Decimal("1")),
    "electronvolts": ("eV", Decimal("1")),
    "mev": ("eV", Decimal("0.001")),
    "kev": ("eV", Decimal("1000")),
    "å": ("Å", Decimal("1")),
    "angstrom": ("Å", Decimal("1")),
    "angstroms": ("Å", Decimal("1")),
    "nm": ("Å", Decimal("10")),
    "pm": ("Å", Decimal("0.01")),
    "v": ("V", Decimal("1")),
    "volt": ("V", Decimal("1")),
    "volts": ("V", Decimal("1")),
    "mv": ("V", Decimal("0.001")),
    "e": ("e", Decimal("1")),
    "|e|": ("e", Decimal("1")),
    "ev/atom": ("eV/atom", Decimal("1")),
    "mev/atom": ("eV/atom", Decimal("0.001")),
}


def normalize_dft_property_type(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[_\s-]+", "_", text).strip("_")


def get_dft_identity_v2_property_policy(value: Any) -> DFTIdentityPropertyPolicy:
    property_type = normalize_dft_property_type(value)
    for policy in _V2_PROPERTY_POLICIES:
        if property_type in policy.exact_property_types:
            return policy
        if any(marker in property_type for marker in policy.property_markers):
            return policy
    return _V2_DEFAULT_PROPERTY_POLICY


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


def normalize_dft_value_kind_v2(
    value: Any,
    *,
    value_upper: Any = None,
    property_type: Any = None,
) -> tuple[str, bool]:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    normalized = re.sub(r"[_\s-]+", "_", text).strip("_")
    if not normalized:
        if value_upper in (None, "", []):
            return "point", True
        if "window" in normalize_dft_property_type(property_type):
            return "energy_window", True
        return "range", True
    canonical = _V2_VALUE_KIND_ALIASES.get(normalized)
    if canonical is None:
        return normalized, False
    return canonical, True


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


def build_dft_identity_v2(payload: dict[str, Any]) -> DFTIdentityV2:
    """Build deterministic nullable-first Identity v2 values without database I/O.

    Provenance is intentionally excluded. Unsupported units and invalid required
    identity fields keep ``observation_key`` null so a later write path cannot
    silently deduplicate scientifically ambiguous rows.
    """

    sources = _identity_v2_sources(payload)
    property_type_raw = _first_value(
        sources,
        ("normalized_property_type", "property_type", "property", "energy_type", "normalized_energy_type"),
    )
    property_type = normalize_dft_property_type(property_type_raw)
    material = _first_value(
        sources,
        (
            "normalized_material_or_catalyst",
            "normalized_material",
            "material_key",
            "material_identity",
            "material",
            "catalyst",
            "catalyst_name",
        ),
    )
    adsorbate = _first_value(sources, ("normalized_adsorbate", "adsorbate"))
    atom_pair = resolve_atom_pair_identity(payload, property_type=property_type_raw)
    property_policy = get_dft_identity_v2_property_policy(property_type)
    property_context, reported_context = _property_specific_context(
        sources,
        property_policy,
    )
    subject = {
        "paper_id": _text(payload.get("paper_id")),
        "material_key": _text(material),
        "property_type": property_type,
        "property_subtype": _text(
            _first_value(sources, ("normalized_property_subtype", "property_subtype"))
        ),
        "adsorbate": _text(adsorbate),
        "reaction_step": normalize_dft_reaction_step_for_identity(
            _first_value(sources, ("normalized_reaction_step", "reaction_step")),
            property_type=property_type_raw,
            adsorbate=adsorbate,
            material=material,
        ),
        "active_site_instance_key": _text(
            _first_value(sources, ("active_site_instance_key",))
        ),
        "canonical_atom_pair": atom_pair.canonical,
        "site_label": _text(
            _first_value(
                sources,
                (
                    "site_label",
                    "adsorption_site",
                    "site",
                    "atom",
                    "atom_label",
                    "atom_site",
                    "atom_identity",
                    "site_identity",
                ),
            )
        ),
        "state_context": _normalize_context_value(
            _first_value(sources, ("state_context",))
        ),
        "property_context": property_context,
    }
    subject_key = _signature_v2("dft-subject-v2", subject)

    value_raw = _first_value(sources, ("normalized_value", "value"))
    value_upper_raw = _first_value(sources, ("normalized_value_upper", "value_upper"))
    value_kind, value_kind_supported = normalize_dft_value_kind_v2(
        _first_value(sources, ("normalized_value_kind", "value_kind", "value_type")),
        value_upper=value_upper_raw,
        property_type=property_type_raw,
    )
    unit_raw = _first_value(sources, ("normalized_unit", "unit"))
    unit, factor, unit_error = _normalize_v2_unit(
        unit_raw,
        property_type=property_type,
        dimensionless=property_policy.dimensionless,
    )
    value, value_valid = _canonical_decimal(value_raw, factor=factor)
    value_upper, value_upper_valid = _canonical_decimal(value_upper_raw, factor=factor)

    errors = _required_identity_errors(subject, property_policy)
    if atom_pair.error_code:
        errors.append(atom_pair.error_code)
    if value_raw in (None, "", []):
        errors.append("missing_value_identity")
    elif not value_valid:
        errors.append("invalid_numeric_identity")
    if not value_kind_supported:
        errors.append("unsupported_value_kind_identity")
    if value_kind in {"range", "energy_window"} and value_upper_raw in (None, "", []):
        errors.append("missing_value_upper_identity")
    elif value_upper_raw not in (None, "", []) and not value_upper_valid:
        errors.append("invalid_value_upper_identity")
    if unit_error:
        errors.append(unit_error)

    observation = {
        "value": value,
        "value_upper": value_upper,
        "value_kind": value_kind,
        "unit": unit,
    }
    error_codes = tuple(dict.fromkeys(errors))
    observation_key = None
    if not error_codes:
        observation_key = _signature_v2(
            "dft-observation-v2",
            {"subject_key": subject_key, **observation},
        )
    identity_payload = {
        "identity_version": 2,
        "property_policy": property_policy.name,
        "subject": subject,
        "observation": observation,
        "reported_context": reported_context,
        "atom_pair": {
            "canonical": atom_pair.canonical,
            "normalized_aliases": list(atom_pair.normalized_aliases),
            "error_code": atom_pair.error_code,
            "required": atom_pair.required,
            "symmetric": atom_pair.symmetric,
        },
        "errors": list(error_codes),
    }
    return DFTIdentityV2(
        identity_version=2,
        subject_key=subject_key,
        observation_key=observation_key,
        identity_payload=identity_payload,
        atom_pair=atom_pair,
        error_codes=error_codes,
        dedupe_allowed=observation_key is not None,
    )


def _identity_v2_sources(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    root = payload if isinstance(payload, dict) else {}
    sources: list[dict[str, Any]] = []

    def append_mapping(value: Any) -> None:
        if isinstance(value, dict) and value not in sources:
            sources.append(value)

    append_mapping(root.get("corrected_value"))
    evidence_payload = root.get("evidence_payload")
    if isinstance(evidence_payload, dict):
        append_mapping(evidence_payload.get("corrected_value"))
    append_mapping(root)
    append_mapping(evidence_payload)
    append_mapping(root.get("evidence_location"))
    return tuple(sources)


def _property_specific_context(
    sources: tuple[dict[str, Any], ...],
    policy: DFTIdentityPropertyPolicy,
) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed = policy.allowed_context_keys
    context: dict[str, Any] = {}
    reported_context: dict[str, Any] = {}
    for source in reversed(sources):
        for container_name in ("property_context", "method_context", "calculation_context"):
            container = source.get(container_name)
            if not isinstance(container, dict):
                continue
            for raw_key, raw_value in container.items():
                key = _normalize_context_key(raw_key)
                if _is_provenance_context_key(key) or raw_value in (None, "", []):
                    continue
                normalized_value = _normalize_context_value(raw_value)
                if key in allowed:
                    context[key] = normalized_value
                    reported_context.pop(key, None)
                else:
                    reported_context[key] = normalized_value

    allowed_input_keys = set(allowed)
    allowed_input_keys.update(
        alias for alias, canonical in _V2_CONTEXT_ALIASES.items() if canonical in allowed
    )
    for raw_key in sorted(allowed_input_keys):
        value = _first_value(sources, (raw_key,))
        if value in (None, "", []):
            continue
        key = _V2_CONTEXT_ALIASES.get(raw_key, raw_key)
        context[key] = _normalize_context_value(value)
        reported_context.pop(key, None)
    return (
        {key: context[key] for key in sorted(context)},
        {key: reported_context[key] for key in sorted(reported_context)},
    )


def _required_identity_errors(
    subject: dict[str, Any],
    policy: DFTIdentityPropertyPolicy,
) -> list[str]:
    errors: list[str] = []
    for requirement in policy.requirements:
        if not any(
            _identity_value_present(_identity_subject_value(subject, path))
            for path in requirement.any_of
        ):
            errors.append(requirement.error_code)
    return errors


def _identity_subject_value(subject: dict[str, Any], path: str) -> Any:
    value: Any = subject
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _identity_value_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _normalize_context_key(value: Any) -> str:
    key = normalize_dft_property_type(value)
    return _V2_CONTEXT_ALIASES.get(key, key)


def _is_provenance_context_key(key: str) -> bool:
    if key in _V2_PROVENANCE_KEYS:
        return True
    return key.startswith(
        (
            "candidate_",
            "evidence_",
            "figure_",
            "locator_",
            "page_",
            "row_",
            "table_",
        )
    )


def _normalize_context_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _normalize_context_key(raw_key)
            if _is_provenance_context_key(key) or raw_value in (None, "", []):
                continue
            normalized[key] = _normalize_context_value(raw_value)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_normalize_context_value(item) for item in value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        canonical, valid = _canonical_decimal(value)
        return canonical if valid else _text(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text):
        canonical, valid = _canonical_decimal(text)
        if valid:
            return canonical
    return " ".join(text.casefold().split())


def _normalize_v2_unit(
    value: Any,
    *,
    property_type: str,
    dimensionless: bool,
) -> tuple[str, Decimal, str | None]:
    if value in (None, "") or not str(value).strip():
        return (
            "",
            Decimal("1"),
            None if dimensionless else "missing_unit_identity",
        )
    token = unicodedata.normalize("NFKC", str(value)).translate(_UNICODE_DASHES)
    token = token.strip().casefold().replace(" ", "")
    token = token.replace("ångström", "angstrom").replace("ångstrom", "angstrom")
    token = token.replace("^-1", "-1")
    if token in {"1", "dimensionless", "unitless", "none"}:
        return (
            "" if dimensionless else token,
            Decimal("1"),
            None if dimensionless else "unsupported_unit_identity",
        )
    if dimensionless:
        return token, Decimal("1"), "unsupported_unit_identity"
    if token == "a" and ("length" in property_type or property_requires_atom_pair(property_type)):
        token = "å"
    rule = _V2_UNIT_RULES.get(token)
    if rule is None:
        return token, Decimal("1"), "unsupported_unit_identity"
    canonical, factor = rule
    return canonical, factor, None


def _canonical_decimal(value: Any, *, factor: Decimal = Decimal("1")) -> tuple[str, bool]:
    if value in (None, ""):
        return "", True
    if isinstance(value, bool):
        return _text(value), False
    text = unicodedata.normalize("NFKC", str(value)).translate(_UNICODE_DASHES).strip()
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text):
        return _text(value), False
    try:
        number = Decimal(text) * factor
    except (InvalidOperation, ValueError):
        return _text(value), False
    if not number.is_finite():
        return _text(value), False
    if number == 0:
        return "0", True
    canonical = format(number.normalize(), "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical, True


def _signature_v2(prefix: str, parts: dict[str, Any]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


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
