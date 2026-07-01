from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

# Catalyst name patterns → canonical form
CATALYST_NAME_MAP: dict[str, str] = {
    # Fe-Nx variants
    "fe-n4": "Fe-N4", "fen4": "Fe-N4", "fe n4": "Fe-N4",
    "fe-n3": "Fe-N3", "fen3": "Fe-N3",
    "fe-n2": "Fe-N2", "fen2": "Fe-N2",
    "fe-n5": "Fe-N5", "fen5": "Fe-N5",
    "fe-n-c": "Fe-N-C", "fen-c": "Fe-N-C",
    "iron-nitrogen": "Fe-N", "iron nitrogen": "Fe-N",
    # Co variants
    "co-n4": "Co-N4", "con4": "Co-N4",
    "co-n3": "Co-N3", "con3": "Co-N3",
    "co-n-c": "Co-N-C",
    # Ni variants
    "ni-n4": "Ni-N4", "nin4": "Ni-N4",
    "ni-n-c": "Ni-N-C",
    # Mn, Cu, Zn, Pt, Ru variants
    "mn-n4": "Mn-N4", "mnn4": "Mn-N4",
    "cu-n4": "Cu-N4", "cun4": "Cu-N4",
    "zn-n4": "Zn-N4", "znn4": "Zn-N4",
    "pt-n4": "Pt-N4", "ptn4": "Pt-N4",
    "ru-n4": "Ru-N4", "run4": "Ru-N4",
}

CATALYST_TYPE_MAP: dict[str, str] = {
    "single atom": "SAC", "single-atom": "SAC", "sac": "SAC",
    "single-atom catalyst": "SAC", "sac catalyst": "SAC",
    "isolated single atom": "SAC", "single metal atom": "SAC",
    "dual atom": "DAC", "dual-atom": "DAC", "dac": "DAC",
    "dual-atom catalyst": "DAC", "dac catalyst": "DAC",
    "diatomic": "DAC",
    "nanoparticle": "NP", "nanoparticles": "NP",
    "cluster": "cluster",
    "bulk": "bulk",
}

ADSORBATE_MAP: dict[str, str] = {
    "s8": "S8", "sulfur": "S8", "sulphur": "S8",
    "li2s8": "Li2S8", "li2s 8": "Li2S8",
    "li2s6": "Li2S6", "li2s 6": "Li2S6",
    "li2s4": "Li2S4", "li2s 4": "Li2S4",
    "li2s2": "Li2S2", "li2s 2": "Li2S2",
    "li2s": "Li2S",
    "lithium polysulfide": "LiPS", "lips": "LiPS",
    "polysulfide": "LiPS", "lithium sulfide": "Li2S",
    "li": "Li", "lithium": "Li",
    "s": "S",
    "ooh*": "*OOH", "*ooh": "*OOH", "ooh": "*OOH",
    "oh*": "*OH", "*oh": "*OH", "oh": "*OH", "hydroxyl": "*OH",
    "o*": "*O", "*o": "*O", "atomic oxygen": "*O",
    "h*": "*H", "*h": "*H", "atomic hydrogen": "*H", "h": "*H",
    "o2": "O2", "oxygen": "O2",
    "h2": "H2", "hydrogen": "H2",
    "h2o": "H2O", "water": "H2O",
    "n2": "N2", "nitrogen": "N2",
    "nnh": "*NNH", "*nnh": "*NNH",
    "nhnh": "*NHNH", "*nhnh": "*NHNH",
    "nh2nh2": "NH2NH2", "hydrazine": "NH2NH2",
    "nh3": "NH3", "ammonia": "NH3",
    "co2": "CO2", "carbon dioxide": "CO2",
    "co": "CO", "carbon monoxide": "CO",
    "cooh": "*COOH", "*cooh": "*COOH",
    "cho": "*CHO", "*cho": "*CHO",
    "hcoo": "*HCOO", "*hcoo": "*HCOO", "formate": "*HCOO",
    "hcooh": "HCOOH", "formic acid": "HCOOH",
}

SUPPORT_MAP: dict[str, str] = {
    "nitrogen-doped carbon": "N-C", "n-doped carbon": "N-C",
    "n doped carbon": "N-C", "n-carbon": "N-C",
    "graphene": "graphene", "reduced graphene oxide": "rGO", "rgo": "rGO",
    "carbon nanotube": "CNT", "carbon nanotubes": "CNT", "cnt": "CNT", "cnts": "CNT",
    "g-c3n4": "g-C3N4", "c3n4": "C3N4",
    "carbon black": "carbon black",
    "mesoporous carbon": "mesoporous carbon",
    "tio2": "TiO2",
    "ceo2": "CeO2",
    "sio2": "SiO2",
    "al2o3": "Al2O3",
    "mof": "MOF", "metal-organic framework": "MOF",
    "cof": "COF", "covalent organic framework": "COF",
}

METAL_ELEMENT_MAP: dict[str, str] = {
    "fe": "Fe", "iron": "Fe",
    "co": "Co", "cobalt": "Co",
    "ni": "Ni", "nickel": "Ni",
    "cu": "Cu", "copper": "Cu",
    "zn": "Zn", "zinc": "Zn",
    "mn": "Mn", "manganese": "Mn",
    "pt": "Pt", "platinum": "Pt",
    "ru": "Ru", "ruthenium": "Ru",
    "ir": "Ir", "iridium": "Ir",
    "pd": "Pd", "palladium": "Pd",
    "mo": "Mo", "molybdenum": "Mo",
    "w": "W", "tungsten": "W",
    "v": "V", "vanadium": "V",
    "au": "Au", "gold": "Au",
    "ag": "Ag", "silver": "Ag",
    "ti": "Ti", "titanium": "Ti",
    "zr": "Zr", "zirconium": "Zr",
    "cr": "Cr", "chromium": "Cr",
    "ce": "Ce", "cerium": "Ce",
}


@dataclass(frozen=True)
class DFTPropertyTaxonomy:
    canonical_property_type: str
    property_family: str
    property_subtype: str | None
    physical_dimension: str
    ml_role: str


_PROPERTY_TAXONOMY_MAP: dict[str, DFTPropertyTaxonomy] = {
    "adsorption_energy": DFTPropertyTaxonomy("adsorption_energy", "energetics", "adsorption", "energy", "target"),
    "binding_energy": DFTPropertyTaxonomy("adsorption_energy", "energetics", "binding", "energy", "target"),
    "gibbs_free_energy_change": DFTPropertyTaxonomy("gibbs_free_energy_change", "thermodynamics", "gibbs_free_energy_change", "energy", "target"),
    "free_energy": DFTPropertyTaxonomy("gibbs_free_energy_change", "thermodynamics", "gibbs_free_energy_change", "energy", "target"),
    "free_energy_change": DFTPropertyTaxonomy("gibbs_free_energy_change", "thermodynamics", "gibbs_free_energy_change", "energy", "target"),
    "reaction_energy": DFTPropertyTaxonomy("reaction_energy", "thermodynamics", "reaction_energy", "energy", "target"),
    "formation_energy": DFTPropertyTaxonomy("formation_energy", "energetics", "formation", "energy", "target"),
    "cohesive_energy": DFTPropertyTaxonomy("cohesive_energy", "energetics", "cohesive", "energy", "target"),
    "fluorination_energy": DFTPropertyTaxonomy("fluorination_energy", "energetics", "fluorination", "energy", "target"),
    "activation_energy": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "activation_energy", "energy", "target"),
    "reaction_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "reaction_barrier", "energy", "target"),
    "migration_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "migration_barrier", "energy", "target"),
    "permeation_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "permeation_barrier", "energy", "target"),
    "li2s_decomposition_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "li2s_decomposition_barrier", "energy", "target"),
    "li2s_dissociation_energy": DFTPropertyTaxonomy("reaction_energy", "thermodynamics", "li2s_dissociation_energy", "energy", "target"),
    "li2s_deposition_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "li2s_deposition_barrier", "energy", "target"),
    "li2s_nucleation_barrier": DFTPropertyTaxonomy("reaction_barrier", "kinetics", "li2s_nucleation_barrier", "energy", "target"),
    "d_band_center": DFTPropertyTaxonomy("d_band_center", "electronic_descriptor", "d_band_center", "energy", "descriptor"),
    "bader_charge": DFTPropertyTaxonomy("bader_charge", "electronic_descriptor", "bader_charge", "charge", "descriptor"),
    "charge_transfer": DFTPropertyTaxonomy("charge_transfer", "electronic_descriptor", "charge_transfer", "charge", "descriptor"),
    "band_gap": DFTPropertyTaxonomy("band_gap", "electronic_descriptor", "band_gap", "energy", "descriptor"),
    "work_function": DFTPropertyTaxonomy("work_function", "electronic_descriptor", "work_function", "energy", "descriptor"),
    "magnetic_moment": DFTPropertyTaxonomy("magnetic_moment", "electronic_descriptor", "magnetic_moment", "magnetic_moment", "descriptor"),
    "lattice_constant": DFTPropertyTaxonomy("lattice_constant", "structural_descriptor", "lattice_constant", "length", "descriptor"),
    "interlayer_distance": DFTPropertyTaxonomy("interlayer_distance", "structural_descriptor", "interlayer_distance", "length", "descriptor"),
    "pore_diameter": DFTPropertyTaxonomy("pore_diameter", "structural_descriptor", "pore_diameter", "length", "descriptor"),
    "permeance": DFTPropertyTaxonomy("permeance", "transport_descriptor", "permeance", "permeance", "descriptor"),
    "young_modulus": DFTPropertyTaxonomy("young_modulus", "mechanical_descriptor", "young_modulus", "modulus", "descriptor"),
    "carrier_mobility": DFTPropertyTaxonomy("carrier_mobility", "transport_descriptor", "carrier_mobility", "mobility", "descriptor"),
    "seebeck_coefficient": DFTPropertyTaxonomy("seebeck_coefficient", "transport_descriptor", "seebeck_coefficient", "seebeck_coefficient", "descriptor"),
    "zt": DFTPropertyTaxonomy("zt", "transport_descriptor", "zt", "dimensionless", "descriptor"),
    "electrical_conductance": DFTPropertyTaxonomy("electrical_conductance", "transport_descriptor", "electrical_conductance", "conductance", "descriptor"),
    "thermal_conductance": DFTPropertyTaxonomy("thermal_conductance", "transport_descriptor", "thermal_conductance", "conductance", "descriptor"),
    "thermal_conductivity": DFTPropertyTaxonomy("thermal_conductivity", "transport_descriptor", "thermal_conductivity", "conductivity", "descriptor"),
    "optical_absorption_peak": DFTPropertyTaxonomy("optical_absorption_peak", "optical_descriptor", "optical_absorption_peak", "energy", "descriptor"),
    "limiting_potential": DFTPropertyTaxonomy("limiting_potential", "electrocatalytic_metric", "limiting_potential", "potential", "target"),
    "overpotential": DFTPropertyTaxonomy("overpotential", "electrocatalytic_metric", "overpotential", "potential", "target"),
    "dos_claim": DFTPropertyTaxonomy("dos_claim", "qualitative_claim", "dos_claim", "text", "lm_auxiliary"),
    "charge_density_difference_claim": DFTPropertyTaxonomy("charge_density_difference_claim", "qualitative_claim", "charge_density_difference_claim", "text", "lm_auxiliary"),
}


def _canonicalize(text: str, mapping: dict[str, str]) -> str | None:
    """Try to map a text to its canonical form via the mapping table."""
    key = text.strip().lower()
    if key in mapping:
        return mapping[key]
    # Try stripping punctuation
    key2 = re.sub(r"[^a-z0-9]", "", key)
    for k, v in mapping.items():
        if re.sub(r"[^a-z0-9]", "", k) == key2:
            return v
    return None


def canonicalize_adsorbate(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    canonical = _canonicalize(cleaned, ADSORBATE_MAP)
    if canonical:
        return canonical

    star_normalized = cleaned.replace("∗", "*").replace("＊", "*").replace("·", "").strip()
    collapsed = re.sub(r"\s+", "", star_normalized.lower())
    star_aliases = {
        "oh*": "*OH",
        "*oh": "*OH",
        "ooh*": "*OOH",
        "*ooh": "*OOH",
        "o*": "*O",
        "*o": "*O",
        "h*": "*H",
        "*h": "*H",
        "co*": "*CO",
        "*co": "*CO",
        "cooh*": "*COOH",
        "*cooh": "*COOH",
        "cho*": "*CHO",
        "*cho": "*CHO",
        "hcoo*": "*HCOO",
        "*hcoo": "*HCOO",
        "nnh*": "*NNH",
        "*nnh": "*NNH",
        "nhnh*": "*NHNH",
        "*nhnh": "*NHNH",
    }
    return star_aliases.get(collapsed, cleaned)


def get_property_taxonomy(raw: str | None) -> dict[str, Any]:
    normalized = ChemistryNormalizer._normalize_property(raw or "")
    taxonomy = _PROPERTY_TAXONOMY_MAP.get(
        normalized,
        DFTPropertyTaxonomy(
            canonical_property_type=normalized or "unknown_property",
            property_family="other",
            property_subtype=normalized or None,
            physical_dimension="unknown",
            ml_role="unknown",
        ),
    )
    return asdict(taxonomy)


def property_type_filter_aliases(raw: str | None) -> tuple[str, ...]:
    raw_text = str(raw or "").strip()
    normalized = ChemistryNormalizer._normalize_property(raw_text)
    if not normalized:
        return ()
    aliases = {raw_text, normalized, normalized.replace("_", " ")}
    taxonomy = _PROPERTY_TAXONOMY_MAP.get(normalized)
    if taxonomy and taxonomy.canonical_property_type == normalized:
        for key, item in _PROPERTY_TAXONOMY_MAP.items():
            if item.canonical_property_type == normalized:
                aliases.add(key)
                aliases.add(key.replace("_", " "))
    return tuple(sorted(value for value in aliases if value))


def _extract_metal_from_name(name: str) -> str | None:
    """Extract metal elements from a catalyst name like 'Fe-N4' or 'Co-N-C'."""
    for el in ["Fe", "Co", "Ni", "Cu", "Zn", "Mn", "Pt", "Ru", "Ir", "Pd", "Mo", "W", "V", "Au", "Ag", "Ti", "Zr", "Cr", "Ce"]:
        if el.lower() in name.lower():
            return el
    return None


class ChemistryNormalizer:
    """Normalize catalyst names, adsorbates, supports, and material types to canonical forms.

    Maps the messy naming conventions found across different papers into a
    unified vocabulary, making cross-paper comparison and aggregation possible.
    """

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result["_original"] = dict(payload)
        result["_normalized"] = {}

        # Normalize catalyst name
        raw_name = payload.get("name") or payload.get("catalyst_name") or ""
        if raw_name:
            canonical = _canonicalize(raw_name, CATALYST_NAME_MAP)
            if canonical:
                result["name"] = canonical
                result["_normalized"]["name"] = {"from": raw_name, "to": canonical}
            metal = _extract_metal_from_name(raw_name)
            if metal:
                result["_normalized"]["metal"] = metal

        # Normalize catalyst type
        raw_type = payload.get("catalyst_type") or ""
        if raw_type:
            canonical = _canonicalize(raw_type, CATALYST_TYPE_MAP)
            if canonical:
                result["catalyst_type"] = canonical
                result["_normalized"]["catalyst_type"] = {"from": raw_type, "to": canonical}

        # Normalize metal centers
        metals = payload.get("metal_centers") or []
        if metals:
            normalized = []
            for m in metals:
                c = _canonicalize(str(m), METAL_ELEMENT_MAP)
                normalized.append(c or str(m))
            result["metal_centers"] = normalized

        # Normalize adsorbate
        adsorbate = payload.get("adsorbate") or ""
        if adsorbate:
            canonical = canonicalize_adsorbate(adsorbate)
            if canonical:
                result["adsorbate"] = canonical
                result["_normalized"]["adsorbate"] = {"from": adsorbate, "to": canonical}

        # Normalize support
        support = payload.get("support") or ""
        if support:
            canonical = _canonicalize(support, SUPPORT_MAP)
            if canonical:
                result["support"] = canonical
                result["_normalized"]["support"] = {"from": support, "to": canonical}

        # Normalize property type
        prop = payload.get("property_type") or ""
        if prop:
            normalized_prop = self._normalize_property(prop)
            if normalized_prop != prop:
                result["property_type"] = normalized_prop
                result["_normalized"]["property_type"] = {"from": prop, "to": normalized_prop}
            result["_normalized"]["property_taxonomy"] = get_property_taxonomy(prop)

        return result

    @staticmethod
    def _normalize_property(raw: str) -> str:
        lowered = (
            raw.strip()
            .lower()
            .replace("–", "-")
            .replace("—", "-")
            .replace("Δ", "delta ")
            .replace("δ", "delta ")
            .replace("△", "delta ")
            .replace("∗", "*")
            .replace("‡", " double_dagger ")
        )
        lowered = re.sub(r"\s+", " ", lowered).strip()
        compact = lowered.replace(" ", "")
        mapping = {
            "adsorption energy": "adsorption_energy",
            "adsorption_energy": "adsorption_energy",
            "binding energy": "binding_energy",
            "binding_energy": "binding_energy",
            "gibbs free energy": "gibbs_free_energy_change",
            "gibbs free energy change": "gibbs_free_energy_change",
            "free energy change": "gibbs_free_energy_change",
            "free energy": "gibbs_free_energy_change",
            "rds gibbs free energy": "gibbs_free_energy_change",
            "gibbs free energy of rds": "gibbs_free_energy_change",
            "delta g of rds": "gibbs_free_energy_change",
            "rds free energy": "gibbs_free_energy_change",
            "rate determining step gibbs free energy": "gibbs_free_energy_change",
            "rate-determining step gibbs free energy": "gibbs_free_energy_change",
            "决速步骤自由能": "gibbs_free_energy_change",
            "决速步骤吉布斯自由能": "gibbs_free_energy_change",
            "决速步骤对应的吉布斯自由能": "gibbs_free_energy_change",
            "rds 对应吉布斯自由能": "gibbs_free_energy_change",
            "reaction barrier": "reaction_barrier",
            "activation energy": "activation_energy",
            "barrier": "reaction_barrier",
            "energy barrier": "reaction_barrier",
            "migration barrier": "migration_barrier",
            "diffusion barrier": "migration_barrier",
            "permeation barrier": "permeation_barrier",
            "li2s decomposition barrier": "li2s_decomposition_barrier",
            "li2s dissociation energy": "li2s_dissociation_energy",
            "li2s dissociation": "li2s_dissociation_energy",
            "dissociation energy of li2s": "li2s_dissociation_energy",
            "li2s deposition barrier": "li2s_deposition_barrier",
            "deposition barrier of li2s": "li2s_deposition_barrier",
            "li2s nucleation barrier": "li2s_nucleation_barrier",
            "reaction free energy": "gibbs_free_energy_change",
            "自由能变化": "gibbs_free_energy_change",
            "吉布斯自由能变化": "gibbs_free_energy_change",
            "反应能垒": "reaction_barrier",
            "活化能": "activation_energy",
            "能量屏障": "reaction_barrier",
            "迁移能垒": "migration_barrier",
            "扩散能垒": "migration_barrier",
            "li2s 分解能垒": "li2s_decomposition_barrier",
            "li2s 解离能": "li2s_dissociation_energy",
            "li2s 沉积能垒": "li2s_deposition_barrier",
            "d band center": "d_band_center",
            "d-band center": "d_band_center",
            "d_band_center": "d_band_center",
            "bader charge": "bader_charge",
            "charge transfer": "charge_transfer",
            "band gap": "band_gap",
            "work function": "work_function",
            "magnetic moment": "magnetic_moment",
            "dos": "dos_claim",
            "density of states": "dos_claim",
            "charge density difference": "charge_density_difference_claim",
        }
        normalized = mapping.get(lowered)
        if normalized:
            return normalized
        rds_markers = (
            "rds",
            "rate determining step",
            "rate-determining step",
            "决速步骤",
        )
        free_energy_markers = (
            "gibbs free energy",
            "free energy",
            "delta g",
            "吉布斯自由能",
            "自由能",
        )
        if any(marker in lowered for marker in rds_markers) and any(
            marker in lowered for marker in free_energy_markers
        ):
            return "gibbs_free_energy_change"
        underscored = lowered.replace(" ", "_")
        if underscored in _PROPERTY_TAXONOMY_MAP:
            return underscored
        if "li2s" in lowered and "decom" in lowered:
            return "li2s_decomposition_barrier"
        if "li2s" in lowered and "dissociat" in lowered:
            return "li2s_dissociation_energy"
        if "li2s" in lowered and "deposit" in lowered:
            return "li2s_deposition_barrier"
        if "li2s" in lowered and "nucleat" in lowered:
            return "li2s_nucleation_barrier"
        if "li2s" in lowered and "分解" in lowered:
            return "li2s_decomposition_barrier"
        if "li2s" in lowered and "解离" in lowered:
            return "li2s_dissociation_energy"
        if "li2s" in lowered and "沉积" in lowered:
            return "li2s_deposition_barrier"
        if ("migration" in lowered or "diffus" in lowered or "迁移" in lowered or "扩散" in lowered) and (
            "barrier" in lowered or "能垒" in lowered or "屏障" in lowered
        ):
            return "migration_barrier"
        if "permeation" in lowered and "barrier" in lowered:
            return "permeation_barrier"
        if (
            "activation energy" in lowered
            or "活化能" in lowered
            or "double_dagger" in lowered
            or "deltag‡" in compact
            or "deltagdouble_dagger" in compact
        ):
            return "activation_energy"
        if "barrier" in lowered or "能垒" in lowered or "屏障" in lowered:
            return "reaction_barrier"
        return underscored
