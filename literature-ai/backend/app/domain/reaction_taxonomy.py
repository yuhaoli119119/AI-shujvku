from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.normalizers.chemistry_normalizer import ChemistryNormalizer, canonicalize_adsorbate

PROFILE_VERSION = "reaction_profiles_v1"
REACTION_TYPES = ("SRR_LiS", "HER", "OER", "ORR", "CO2RR", "UNKNOWN")

# 材料 / 载体维度。石墨炔(graphdiyne)、石墨烯(graphene)、C2N 等是催化剂的
# 材料身份，永远不能被当成 reaction_type；它们只能出现在 material_identity /
# support / structure_context 这些维度里。
MATERIAL_DIMENSION_TERMS = frozenset(
    {
        "graphdiyne",
        "gdy",
        "graphyne",
        "graphene",
        "c2n",
        "c3n4",
        "gcn",
        "carbon nanotube",
        "cnt",
        "mxene",
        "n-doped carbon",
        "n doped carbon",
        "defective graphene",
        "nitrogen-doped graphene",
    }
)


def is_material_dimension_term(text: Any) -> bool:
    """判断一个词是否是材料/载体维度而不是反应类型."""
    cleaned = _clean(text)
    if not cleaned:
        return False
    if cleaned in MATERIAL_DIMENSION_TERMS:
        return True
    return any(cleaned == term or cleaned.startswith(f"{term} ") for term in MATERIAL_DIMENSION_TERMS)


@dataclass(frozen=True)
class ReactionProfile:
    key: str
    version: str
    status: str
    allowed_intermediates: frozenset[str]
    intermediate_aliases: Mapping[str, str]
    property_aliases: Mapping[str, str]
    allowed_properties: frozenset[str]
    canonical_units: Mapping[str, str]
    step_graph: Mapping[str, frozenset[str]]
    required_context_terms: frozenset[str] = frozenset()
    exclusion_context_terms: frozenset[str] = frozenset()
    tabular_tasks: Mapping[str, Any] | None = None


_COMMON_ENERGY_ALIASES = {
    "adsorption energy": "adsorption_energy",
    "binding energy": "binding_energy",
    "gibbs free energy": "gibbs_free_energy_change",
    "gibbs free energy change": "gibbs_free_energy_change",
    "free energy change": "gibbs_free_energy_change",
    "reaction free energy": "gibbs_free_energy_change",
}

_SRR_PROPERTIES = {
    **_COMMON_ENERGY_ALIASES,
    # 原子硫脱附能垒 / 原子硫结合强度判据：硫中毒与催化剂再生描述量，
    # 语义上区别于一般 reaction_barrier 与 adsorption_energy，单独登记。
    "sulfur desorption barrier": "sulfur_desorption_barrier",
    "sulfur atom desorption barrier": "sulfur_desorption_barrier",
    "desorption barrier of sulfur": "sulfur_desorption_barrier",
    "s desorption barrier": "sulfur_desorption_barrier",
    "sulfur desorption criterion": "sulfur_desorption_criterion",
    "sulfur binding criterion": "sulfur_desorption_criterion",
    "sulfur atom binding criterion": "sulfur_desorption_criterion",
    "desorption criterion": "sulfur_desorption_criterion",
    "li2s decomposition barrier": "li2s_decomposition_barrier",
    "li2s decomposition energy barrier": "li2s_decomposition_barrier",
    "decomposition barrier of li2s": "li2s_decomposition_barrier",
    "li2s dissociation energy": "li2s_dissociation_energy",
    "dissociation energy of li2s": "li2s_dissociation_energy",
    "li2s deposition barrier": "li2s_deposition_barrier",
    "deposition barrier of li2s": "li2s_deposition_barrier",
    "li2s nucleation barrier": "li2s_nucleation_barrier",
    "nucleation barrier of li2s": "li2s_nucleation_barrier",
    "migration barrier": "migration_barrier",
    "d band center": "d_band_center",
    "d-band center": "d_band_center",
    "bader charge": "bader_charge",
    "charge transfer": "charge_transfer",
}

_ELECTROCAT_PROPERTIES = {
    **_COMMON_ENERGY_ALIASES,
    "step free energy": "gibbs_free_energy_change",
    "limiting potential": "limiting_potential",
    "onset potential": "onset_potential",
    "overpotential": "overpotential",
}


def _profile(
    key: str,
    status: str,
    intermediates: set[str],
    aliases: Mapping[str, str],
    properties: Mapping[str, str],
    context: set[str],
    exclusions: set[str] | None = None,
    step_graph: Mapping[str, set[str]] | None = None,
) -> ReactionProfile:
    allowed_properties = frozenset(properties.values())
    units = {name: ("V" if "potential" in name else "eV") for name in allowed_properties}
    units.update({"bader_charge": "e", "charge_transfer": "e"})
    return ReactionProfile(
        key=key,
        version=PROFILE_VERSION,
        status=status,
        allowed_intermediates=frozenset(intermediates),
        intermediate_aliases=aliases,
        property_aliases=properties,
        allowed_properties=allowed_properties,
        canonical_units=units,
        step_graph={source: frozenset(targets) for source, targets in (step_graph or {}).items()},
        required_context_terms=frozenset(context),
        exclusion_context_terms=frozenset(exclusions or set()),
        # Task definitions belong to export v3 (phase 4); the stable hook exists now.
        tabular_tasks={},
    )


_PROFILES = {
    "SRR_LiS": _profile(
        "SRR_LiS", "production",
        # ``S_atom`` 是原子硫（single sulfur atom），``S8`` 是环八硫分子。
        # 两者是不同的中间体，绝不能互相归一化。
        {"S8", "S_atom", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S"},
        # Bare "s" (as stored in the adsorbate column of ACS Li-S papers) is the
        # atomic sulfur adsorbate, never the S8 ring.  It must stay an exact
        # match so "s8" and "s atom" keep resolving to their own species.
        {"s8": "S8", "s": "S_atom", "sulfur": "S_atom", "sulphur": "S_atom",
         "s atom": "S_atom", "single sulfur atom": "S_atom",
         "atomic sulfur": "S_atom", "s_atom": "S_atom",
         "li2s8": "Li2S8", "li2s6": "Li2S6",
         "li2s4": "Li2S4", "li2s2": "Li2S2", "li2s": "Li2S"},
        _SRR_PROPERTIES,
        {"li-s", "lithium sulfur", "lithium-sulfur", "polysulfide", "sulfur reduction", "srr"},
        {"hydrogen evolution", "oxygen evolution", "oxygen reduction", "co2 reduction"},
        {"S8": {"Li2S8"}, "Li2S8": {"Li2S6"}, "Li2S6": {"Li2S4"},
         "Li2S4": {"Li2S2"}, "Li2S2": {"Li2S"}},
    ),
    "HER": _profile(
        "HER", "experimental", {"*H"},
        {"*h": "*H", "h*": "*H", "delta g h*": "*H", "δg h*": "*H", "δg_h*": "*H"},
        {**_ELECTROCAT_PROPERTIES, "delta g h*": "gibbs_free_energy_change", "δg h*": "gibbs_free_energy_change"},
        {"hydrogen evolution", "her"},
        step_graph={"*": {"*H"}, "*H": {"H2"}},
    ),
    "OER": _profile(
        "OER", "experimental", {"*OH", "*O", "*OOH"},
        {"*oh": "*OH", "oh*": "*OH", "*o": "*O", "o*": "*O", "*ooh": "*OOH", "ooh*": "*OOH"},
        _ELECTROCAT_PROPERTIES, {"oxygen evolution", "oer"},
        step_graph={"*": {"*OH"}, "*OH": {"*O"}, "*O": {"*OOH"}, "*OOH": {"O2"}},
    ),
    "ORR": _profile(
        "ORR", "experimental", {"O2", "*OOH", "*O", "*OH"},
        {"o2": "O2", "*ooh": "*OOH", "ooh*": "*OOH", "*o": "*O", "o*": "*O", "*oh": "*OH", "oh*": "*OH"},
        _ELECTROCAT_PROPERTIES, {"oxygen reduction", "orr"},
        step_graph={"O2": {"*OOH"}, "*OOH": {"*O", "*OH"}, "*O": {"*OH"}, "*OH": {"H2O"}},
    ),
    "CO2RR": _profile(
        "CO2RR", "experimental", {"CO2", "*COOH", "*CO", "*OCHO", "*CHO", "*HCOO"},
        {"co2": "CO2", "*cooh": "*COOH", "cooh*": "*COOH", "*co": "*CO", "co*": "*CO",
         "*ocho": "*OCHO", "ocho*": "*OCHO", "*cho": "*CHO", "cho*": "*CHO", "*hcoo": "*HCOO"},
        _ELECTROCAT_PROPERTIES, {"co2 reduction", "co2rr", "carbon dioxide reduction"},
        step_graph={"CO2": {"*COOH", "*OCHO"}, "*COOH": {"*CO"}, "*OCHO": {"*HCOO"}},
    ),
    "UNKNOWN": _profile("UNKNOWN", "quarantine", set(), {}, {}, set()),
}


_DASH_EQUIVALENTS = {
    "−": "-",  # MINUS SIGN
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH
    "‐": "-",  # HYPHEN
    "‑": "-",  # NON-BREAKING HYPHEN
}


def _clean(text: Any) -> str:
    value = str(text or "").strip().lower().replace("∗", "*").replace("＊", "*")
    for source, target in _DASH_EQUIVALENTS.items():
        value = value.replace(source, target)
    value = value.replace("Δ", "delta ").replace("δ", "delta ").replace("_", " ")
    return re.sub(r"\s+", " ", value)


def _value(candidate: Any, *names: str) -> Any:
    for name in names:
        if isinstance(candidate, Mapping) and candidate.get(name) is not None:
            return candidate[name]
        value = getattr(candidate, name, None)
        if value is not None:
            return value
    return None


def normalize_reaction_type(text: Any) -> str:
    # 材料/载体维度不是反应类型（例如石墨炔 graphdiyne 是催化剂材料，
    # 不是 reaction_type）。显式挡在这里，避免材料名被误分类成 HER/OER 等。
    if is_material_dimension_term(text):
        return "UNKNOWN"
    cleaned = re.sub(r"[^a-z0-9]", "", _clean(text))
    aliases = {
        "srrlis": "SRR_LiS", "lis": "SRR_LiS", "lithiumsulfur": "SRR_LiS",
        "sulfurreductionreaction": "SRR_LiS", "srr": "SRR_LiS",
        "her": "HER", "hydrogenevolutionreaction": "HER",
        "oer": "OER", "oxygenevolutionreaction": "OER",
        "orr": "ORR", "oxygenreductionreaction": "ORR",
        "co2rr": "CO2RR", "carbondioxidereductionreaction": "CO2RR",
        "unknown": "UNKNOWN", "ambiguous": "UNKNOWN",
    }
    return aliases.get(cleaned, "UNKNOWN")


def get_reaction_profile(reaction_type: Any) -> ReactionProfile:
    return _PROFILES[normalize_reaction_type(reaction_type)]


def normalize_intermediate(reaction_type: Any, text: Any) -> str | None:
    if text is None or not str(text).strip():
        return None
    profile = get_reaction_profile(reaction_type)
    cleaned = _clean(text)
    compact = cleaned.replace(" ", "")
    for alias, canonical in profile.intermediate_aliases.items():
        if cleaned == _clean(alias) or compact == _clean(alias).replace(" ", ""):
            return canonical
    general = canonicalize_adsorbate(str(text))
    return general if general in profile.allowed_intermediates else None


def normalize_property_type(reaction_type: Any, text: Any) -> str | None:
    if text is None or not str(text).strip():
        return None
    profile = get_reaction_profile(reaction_type)
    cleaned = _clean(text)
    for alias, canonical in profile.property_aliases.items():
        if cleaned == _clean(alias):
            return canonical
    generic = ChemistryNormalizer._normalize_property(str(text))
    return generic if generic in profile.allowed_properties else None


def _combined_text(candidate: Any, paper_context: Any = None) -> str:
    parts = [paper_context]
    if isinstance(candidate, Mapping):
        parts.extend(candidate.values())
    else:
        parts.extend(_value(candidate, name) for name in ("property_type", "adsorbate", "intermediate", "reaction_step", "evidence_text"))
    return _clean(" ".join(str(part) for part in parts if part is not None))


def _contains_context_term(text: str, term: str) -> bool:
    """Match abbreviations and phrases without entering ordinary words."""
    pattern = rf"(?<![a-z0-9]){re.escape(_clean(term))}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def classify_reaction_record(candidate: Any, paper_context: Any = None) -> dict[str, Any]:
    explicit = normalize_reaction_type(_value(candidate, "reaction_type"))
    if explicit != "UNKNOWN":
        return {"reaction_type": explicit, "status": "classified", "confidence": 1.0, "reason": "explicit_reaction_type"}

    text = _combined_text(candidate, paper_context)
    context_matches = [
        key
        for key, profile in _PROFILES.items()
        if key != "UNKNOWN"
        and any(_contains_context_term(text, term) for term in profile.required_context_terms)
    ]
    if len(context_matches) == 1:
        key = context_matches[0]
        return {"reaction_type": key, "status": "classified", "confidence": 0.9, "reason": "reaction_context"}
    if len(context_matches) > 1:
        return {"reaction_type": "UNKNOWN", "status": "ambiguous", "confidence": 0.0, "reason": "conflicting_reaction_context"}

    raw_intermediate = _value(candidate, "intermediate", "adsorbate")
    raw_property = _value(candidate, "property_type", "property")
    srr_intermediate = normalize_intermediate("SRR_LiS", raw_intermediate)
    srr_property = normalize_property_type("SRR_LiS", raw_property)
    # Plain "S8" is too easy to confuse with figure/table labels (for example
    # "Figure S8"). Treat it as SRR-specific only when surrounding context also
    # mentions Li-S/SRR terms; Li2Sx species remain specific enough on their own.
    srr_specific_intermediates = {"Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S"}
    if srr_intermediate in srr_specific_intermediates or srr_property in {
        "li2s_decomposition_barrier",
        "li2s_dissociation_energy",
        "li2s_deposition_barrier",
        "li2s_nucleation_barrier",
    }:
        return {"reaction_type": "SRR_LiS", "status": "classified", "confidence": 0.95, "reason": "srr_specific_signal"}

    # Shared electrocatalytic intermediates are deliberately insufficient alone.
    return {"reaction_type": "UNKNOWN", "status": "ambiguous", "confidence": 0.0, "reason": "insufficient_or_shared_context"}


def validate_reaction_record(reaction_type: Any, candidate: Any) -> dict[str, Any]:
    key = normalize_reaction_type(reaction_type)
    profile = get_reaction_profile(key)
    if key == "UNKNOWN":
        return {"reaction_type": key, "status": "ambiguous", "valid": False, "reasons": ["unknown_reaction_type"]}

    raw_intermediate = _value(candidate, "intermediate", "adsorbate")
    raw_property = _value(candidate, "property_type", "property")
    intermediate = normalize_intermediate(key, raw_intermediate)
    property_type = normalize_property_type(key, raw_property)
    reasons: list[str] = []
    if raw_intermediate and intermediate is None:
        reasons.append("intermediate_out_of_scope")
    if raw_property and property_type is None:
        reasons.append("property_out_of_scope")
    material_level_properties = {"d_band_center", "bader_charge", "charge_transfer"}
    if not raw_intermediate and property_type not in material_level_properties:
        reasons.append("missing_intermediate")
    if not raw_property:
        reasons.append("missing_property_type")
    valid = not reasons
    return {
        "reaction_type": key,
        "profile_version": profile.version,
        "status": "valid" if valid else "out_of_scope",
        "valid": valid,
        "intermediate": intermediate,
        "property_type": property_type,
        "canonical_unit": profile.canonical_units.get(property_type) if property_type else None,
        "reasons": reasons,
    }


__all__ = [
    "PROFILE_VERSION", "REACTION_TYPES", "MATERIAL_DIMENSION_TERMS", "ReactionProfile",
    "classify_reaction_record", "get_reaction_profile", "is_material_dimension_term",
    "normalize_intermediate", "normalize_property_type", "normalize_reaction_type",
    "validate_reaction_record",
]
