from __future__ import annotations

import re
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
    "li2s8": "Li2S8",
    "li2s6": "Li2S6",
    "li2s4": "Li2S4",
    "li2s2": "Li2S2",
    "li2s": "Li2S",
    "lithium polysulfide": "LiPS", "lips": "LiPS",
    "polysulfide": "LiPS",
    "li": "Li", "lithium": "Li",
    "s": "S",
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
            canonical = _canonicalize(adsorbate, ADSORBATE_MAP)
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

        return result

    @staticmethod
    def _normalize_property(raw: str) -> str:
        mapping = {
            "adsorption energy": "adsorption_energy",
            "binding energy": "adsorption_energy",
            "adsorption_energy": "adsorption_energy",
            "gibbs free energy": "gibbs_free_energy_change",
            "gibbs free energy change": "gibbs_free_energy_change",
            "free energy change": "gibbs_free_energy_change",
            "free energy": "gibbs_free_energy_change",
            "reaction barrier": "reaction_barrier",
            "activation energy": "reaction_barrier",
            "energy barrier": "reaction_barrier",
            "d band center": "d_band_center",
            "d-band center": "d_band_center",
            "bader charge": "bader_charge",
            "charge transfer": "charge_transfer",
            "dos": "dos_claim",
            "density of states": "dos_claim",
            "charge density difference": "charge_density_difference_claim",
        }
        return mapping.get(raw.strip().lower(), raw.strip().lower().replace(" ", "_"))
