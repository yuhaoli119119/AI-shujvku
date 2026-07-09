"""DFT 计算结果抽取器 — Stage 2 MVP (规则+启发式，无大模型依赖).

输入: UnifiedPaperDocument
输出: list[DFTResultItem]  (结构化 DFT 结果)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.config import Settings
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class SourceLocation:
    section: str | None = None
    page: int | None = None
    figure: str | None = None
    table: str | None = None


@dataclass
class _MarkdownTableBlock:
    markdown_content: str
    caption: str = "Markdown table"
    page: int | None = None


@dataclass
class DFTResultItem:
    category: str  # e.g. "adsorption_energy", "reaction_barrier"
    catalyst_name: str | None = None
    active_site_context: str | None = None
    structure_context: str | None = None
    adsorbate: str | None = None  # e.g. "Li2S4", "Li2S", None for generic
    value: float | None = None
    unit: str | None = None
    reaction_step: str | None = None
    evidence_text: str = ""
    source_location: SourceLocation = field(default_factory=SourceLocation)
    confidence: float = 0.5  # 0.0 ~ 1.0
    fact_family: str | None = None
    atom_pair: str | None = None
    site_label: str | None = None
    state_context: str | None = None
    active_site_instance_key: str | None = None
    metal_center_1: str | None = None
    metal_center_2: str | None = None
    support: str | None = None
    source_table_id: str | None = None
    source_table_caption: str | None = None
    source_row_index: int | None = None
    source_column_index: int | None = None
    raw_row_text: str | None = None
    raw_column_header: str | None = None
    parser_version: str = "lis_dac_dft_rules_v1"


class SourceLocationModel(BaseModel):
    section: str | None = None
    page: int | None = None
    figure: str | None = None
    table: str | None = None

class DFTResultItemModel(BaseModel):
    category: str = Field(..., description="Category of DFT result (e.g., adsorption_energy, bader_charge, reaction_barrier)")
    catalyst_name: str | None = Field(
        None,
        description="Exact paper-local catalyst/material/model name. Do not guess; leave null when the value cannot be bound.",
    )
    active_site_context: str | None = Field(
        None,
        description="Exact active-site label or coordination context associated with this value.",
    )
    structure_context: str | None = Field(
        None,
        description="Exact structure/configuration/model context associated with this value.",
    )
    adsorbate: str | None = Field(None, description="The adsorbate molecule/atom if applicable (e.g., Li2S4, S8)")
    value: float | None = Field(None, description="The numerical value extracted")
    unit: str | None = Field(None, description="Unit of the value (e.g., eV, meV)")
    reaction_step: str | None = Field(None, description="Reaction step or table condition if applicable")
    evidence_text: str = Field(..., description="The exact sentence or table row text that serves as evidence")
    source_location: SourceLocationModel = Field(default_factory=SourceLocationModel)
    confidence: float = Field(..., description="Confidence score from 0.0 to 1.0")

class DFTResultListModel(BaseModel):
    results: list[DFTResultItemModel]


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

# M4: 数值捕获正则 — 支持科学计数法
# 原始: [\-\+]?\d+[.]?\d*
# 增强: 同时匹配 "1.5 × 10^3", "2.3e-4", 以及普通小数
_NUMERIC_PAT = r"(?:[\-\+]?\d+(?:\.\d+)?(?:\s*[×x·]\s*10\^[\-\+]?\d+|[\-]?[\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b]+)?|[eE][\-\+]?\d+|[\-\+]?\d+[.]?\d*)"

# 吸附质关键词 → 标准名映射
ADSORBATE_MAP: dict[str, str] = {
    "s8": "S8",
    "li2s8": "Li2S8",
    "li2s6": "Li2S6",
    "li2s4": "Li2S4",
    "li2s2": "Li2S2",
    "li2s": "Li2S",
    "sulfur": "S_atom",
    "sulphur": "S_atom",
    "s atom": "S_atom",
    "single sulfur atom": "S_atom",
    "atomic sulfur": "S_atom",
    "polysulfide": "LiPS",
    "lips": "LiPS",
    "oxygen": "O",
    "o2": "O2",
    "hydrogen": "H",
    "atomic hydrogen": "H",
    "h2": "H2",
    "water": "H2O",
    "co2": "CO2",
    "co": "CO",
    "no2": "NO2",
    "single vacancy": "single_vacancy",
    "monovacancy": "single_vacancy",
    "vacancy": "vacancy",
    "divacancy": "divacancy",
    "stone-wales": "Stone-Wales",
    "stone wales": "Stone-Wales",
    "interstitial": "interstitial",
    "graphene": "graphene",
    "graphite": "graphite",
}

# 能量单位标准化
UNIT_ALIASES: dict[str, str] = {
    "ev": "eV",
    "ev/atom": "eV/atom",
    "v": "V",
    "kcal/mol": "kcal/mol",
    "kj/mol": "kJ/mol",
    "mev": "meV",
    "a": "A",
    "å": "A",
    "angstrom": "A",
    "angstroms": "A",
    "nm": "nm",
    "pm": "pm",
    "μb": "μB",
    "mub": "μB",
    "mu_b": "μB",
    "bohr magneton": "μB",
    "ev/a": "eV/A",
    "ev/å": "eV/A",
    "states/ev": "states/eV",
    "ev-1": "eV^-1",
    "ev^-1": "eV^-1",
}


def _normalize_chem_label(text: str | None) -> str:
    """Normalize mathematical Unicode labels from parsed PDFs for rule matching."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.replace("–", "-").replace("—", "-")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"(?i)\bLi\s*2\s*S\b", "Li2S", normalized)
    normalized = re.sub(r"(?i)\bLi\s*2\s*S\s*([2468])\b", r"Li2S\1", normalized)
    normalized = re.sub(r"(?i)\bE\s+ads\b", "Eads", normalized)
    normalized = re.sub(r"(?i)\bE\s+([bs])\b", r"E\1", normalized)
    normalized = re.sub(r"\b([A-Za-z])\s*-\s*([A-Za-z])\b", r"\1-\2", normalized)
    return normalized.strip()

TABLE_HEADER_CATEGORY_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"(adsorption|binding).*(energy|e[_\-\s]*ads|e[_\-\s]*bind)|(^e[_\-\s]*ads(?:\s*\(.*\))?$)|(^e[_\-\s]*bind(?:\s*\(.*\))?$)", re.IGNORECASE), "adsorption_energy", "eV"),
    (re.compile(r"(delta\s*g|gibbs|free energy|^dg$|^Δg$)", re.IGNORECASE), "gibbs_free_energy_change", "eV"),
    (re.compile(r"li\s*2\s*s.*(?:decompos|decomposition|oxid|breakdown).*(?:barrier|energy)|(?:decompos|decomposition|oxid|breakdown).*(?:barrier|energy).*li\s*2\s*s", re.IGNORECASE), "li2s_decomposition_barrier", "eV"),
    (re.compile(r"(barrier|activation|^ea$|energy barrier)", re.IGNORECASE), "reaction_barrier", "eV"),
    (re.compile(r"(bader).*(charge)|(^bader$)", re.IGNORECASE), "bader_charge", "e"),
    (re.compile(r"(charge transfer|electron transfer)", re.IGNORECASE), "charge_transfer", "e"),
    (re.compile(r"(d-?band|epsilon[_\-\s]*d|ε[_\-\s]*d)", re.IGNORECASE), "d_band_center", "eV"),
    (re.compile(r"(^\s*(?:d\s*)?li\s*[-–]?\s*s\s*(?:bond\s*)?(?:length|distance)?\s*(?:\((?:a|å|angstroms?)\))?\s*$|li\s*[-–]\s*s.*(?:bond\s*)?(?:length|distance))", re.IGNORECASE), "bond_length_Li-S", "A"),
    (re.compile(r"(?:^|\b)(?:limiting\s+potential|U\s*[_\-]\s*L|U\s+L|UL)(?:\b|$)", re.IGNORECASE), "limiting_potential", "V"),
    (re.compile(r"(overpotential|η|eta)", re.IGNORECASE), "overpotential", "V"),
]

NUMERIC_CATEGORIES = {
    "adsorption_energy",
    "activation_energy",
    "binding_energy",
    "cohesive_energy",
    "metal_support_binding_energy_Eb",
    "stability_parameter_Es",
    "formation_energy",
    "fluorination_energy",
    "gibbs_free_energy_change",
    "reaction_energy",
    "reaction_barrier",
    "migration_barrier",
    "permeation_barrier",
    "li2s_decomposition_barrier",
    "li2s_deposition_barrier",
    "li2s_dissociation_energy",
    "li2s_nucleation_barrier",
    "bader_charge",
    "charge_transfer",
    "Lowdin_charge",
    "ICOHP",
    "COHP",
    "d_band_center",
    "d_orbital_occupancy",
    "DOS_at_Fermi",
    "band_gap",
    "work_function",
    "magnetic_moment",
    "limiting_potential",
    "overpotential",
    "lattice_constant",
    "interlayer_distance",
    "pore_diameter",
    "li_s_bond_length",
    "bond_length_Li-S",
    "bond_length_S-S",
    "bond_length_M-N",
    "bond_length_M-S",
    "bond_length_M-M",
    "permeance",
    "adsorption_molecule_fraction",
    "young_modulus",
    "seebeck_coefficient",
    "zt",
    "electrical_conductance",
    "thermal_conductance",
    "thermal_conductivity",
    "carrier_mobility",
    "optical_absorption_peak",
    "electronegativity_sum",
    "unoccupied_d_state_fraction",
    "orbital_occupancy_dxy",
    "orbital_occupancy_dx2_y2",
    "orbital_occupancy_dz2",
    "orbital_occupancy_dxz",
    "orbital_occupancy_dyz",
    "orbital_occupancy_dxz_dyz",
    "cutoff_energy",
    "vacuum_thickness",
    "u_value",
    "convergence_force",
    "convergence_energy",
}
TABLE_ONLY_NUMERIC_CATEGORIES = {"limiting_potential", "overpotential"}
NON_NUMERIC_DFT_CLAIM_CATEGORIES = {"dos_claim", "charge_density_difference_claim"}
TEXT_SCALAR_CATEGORIES = {"functional", "k_points", "supercell", "solvation_model", "calculation_setting"}

# 类别 → 正则模式列表 (每个模式: (pattern, value_group, unit_group))
CATEGORY_RULES: dict[str, list[tuple[str, int, int]]] = {
    "adsorption_energy": [
        r"(?:E\s*[_\-\s]?\s*ads|Eads)\s*(?:\(\s*[^)]{1,40}\s*\)|\s+(?:of|for)\s+[A-Za-z0-9*+\-_/().\s]{1,60}?|[-–]\s*[A-Za-z0-9*+\-_/().\s]{1,40}?)?\s*(?:is|was|=|:|≈|~)\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        # "adsorption energy of X on Y is -1.23 eV"
        (
            r"(?:adsorption|binding)\s+(?:energy|strength).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
            1,
            2,
        ),
        r"(?:E_{?ads}?|E_b?|E_{bind})\s*=?\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol).{0,40}(?:adsorpt|bind)",
        ],
    "gibbs_free_energy_change": [
        r"(?:\u0394G|Gibbs\s*free\s*energy(?:\s*change)?|delta\s*G).{0,60}?([\-\+]?\d+[.]?\d*)\s*(eV|kJ/mol|kcal/mol)",
        r"(?:\u0394G|delta\s*G)\s*[=\u2248]\s*([\-\+]?\d+[.]?\d*)\s*(eV|kJ/mol|kcal/mol)",
        ],
    "reaction_barrier": [
        r"(?:reaction\s+)?(?:barrier|activation\s+energy|\bE_a\b).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"\bE_a\b\s*[=\u2248]\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"\bE\s*[_\-\s]?\s*a\b\s*[=＝\u2248]\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"(?:活化能|活化能垒|能垒|反应能垒).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"energy\s+barrier.{0,30}([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        ],
    "li2s_decomposition_barrier": [
        r"(?:(?:decompos|breakdown|oxidation).{0,20}(?:of\s+)?Li2S|Li2S.{0,20}(?:decompos|breakdown|oxidation)).{0,60}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"Li2S\s+(?:decompos|oxid).{0,40}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        ],
    "li2s_nucleation_barrier": [
        r"(?:nucleat(?:ion)?.{0,20}(?:barrier|energy)|Li2S.{0,20}nucleat).{0,60}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        ],
    "li2s_deposition_barrier": [],
    "li2s_dissociation_energy": [],
    "bader_charge": [
        r"(?:Bader\s+?(?:charge|analysis)).{0,80}?([\-\+]?\d+[.]?\d*)\s*(e[\u2212-]|e)",
        r"Bader.{0,40}?charge\s*(?:of|transfer|gain|loss).{0,20}?([\-\+]?\d+[.]?\d*)",
        ],
    "charge_transfer": [
        r"(?:charge\s+transfer(?:red)?|(?:electron|e[\u2212-])\s+transfer).{0,60}?([\-\+]?\d+[.]?\d*)\s*(e[\u2212-]|e|electrons?)",
        r"(?:transfers?|gains?|loss?).{0,20}?([\-\+]?\d+[.]?\d+)\s*(?:e[\u2212-]?|electrons?)",
        r"Mulliken.{0,30}?([\-\+]?\d+[.]?\d*)\s*e[\u2212-]",
        ],
    "Lowdin_charge": [
        r"(?:Lowdin|Löwdin)\s+(?:charge|population).{0,80}?([\-\+]?\d+[.]?\d*)\s*(e[\u2212-]?|e|electrons?)?",
    ],
    "ICOHP": [
        r"(?:I?COHP|integrated\s+COHP).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV)?",
    ],
    "COHP": [
        r"\bCOHP\b.{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV)?",
    ],
    "d_band_center": [
        r"(?:d-?band\s+center|\u03b5_d|epsilon_d).{0,40}?([-\+]?\d+[.]?\d*)\s*(eV|meV)",
        ],
    "d_orbital_occupancy": [
        r"(?:d[-\s]?orbital\s+occupanc(?:y|ies)|d\s+electron\s+occupanc(?:y|ies)|d\s+occupancy).{0,80}?([\-\+]?\d+[.]?\d*)\s*(electrons?|e)?",
    ],
    "DOS_at_Fermi": [
        r"(?:(?:DOS|density\s+of\s+states).{0,40}(?:Fermi|E[_\-\s]?F)|(?:Fermi|E[_\-\s]?F).{0,40}(?:DOS|density\s+of\s+states)).{0,80}?([\-\+]?\d+[.]?\d*)\s*(states?/eV|eV-1|eV\^-1)?",
    ],
    "bond_length_Li-S": [
        r"(?:Li\s*[-–]\s*S\s*(?:bond\s*)?(?:length|distance)|d\s*Li\s*[-–]?\s*S).{0,40}?([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm)",
        r"([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm).{0,40}(?:Li\s*[-–]\s*S\s*(?:bond\s*)?(?:length|distance)|d\s*Li\s*[-–]?\s*S)",
        ],
    "bond_length_S-S": [
        r"(?:S\s*[-–]\s*S\s*(?:bond\s*)?(?:length|distance)|d\s*S\s*[-–]?\s*S).{0,40}?([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm)",
        r"([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm).{0,40}(?:S\s*[-–]\s*S\s*(?:bond\s*)?(?:length|distance)|d\s*S\s*[-–]?\s*S)",
        ],
    "bond_length_M-N": [
        r"(?:M\s*[-–]\s*N|metal\s*[-–]?\s*N|[A-Z][a-z]?\s*[-–]\s*N)\s*(?:bond\s*)?(?:length|distance)?.{0,40}?([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm)",
        ],
    "bond_length_M-S": [
        r"(?:M\s*[-–]\s*S|metal\s*[-–]?\s*S|[A-Z][a-z]?\s*[-–]\s*S)\s*(?:bond\s*)?(?:length|distance)?.{0,40}?([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm)",
        ],
    "bond_length_M-M": [
        r"(?:M\s*[-–]\s*M|metal\s*[-–]?\s*metal|[A-Z][a-z]?\s*[-–]\s*[A-Z][a-z]?)\s*(?:bond\s*)?(?:length|distance)?.{0,40}?([\-\+]?\d+[.]?\d*)\s*(Å|A|angstroms?|nm|pm)",
        ],
    "limiting_potential": [
        r"(?:limiting\s+potential|\bU\s*[_\-]\s*L\b|\bU\s+L\b|\bUL\b).{0,80}?([\-\+]?\d+[.]?\d*)\s*(V|eV)",
        r"([\-\+]?\d+[.]?\d*)\s*(V|eV).{0,60}(?:limiting\s+potential|\bU\s*[_\-]\s*L\b|\bU\s+L\b|\bUL\b)",
        ],
    "metal_support_binding_energy_Eb": [
        r"\bE\s*[_\-\s]?\s*b\b[^.;\n]{0,120}?\b(?:metal|atom|single[-\s]?atom|support|substrate|anchoring|M\s*[-–]\s*N|M\s*[-–]\s*C|N[-\s]?doped\s+carbon)\b[^.;\n]{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"\b(?:metal|atom|single[-\s]?atom|support|substrate|anchoring|M\s*[-–]\s*N|M\s*[-–]\s*C|N[-\s]?doped\s+carbon)\b[^.;\n]{0,120}?\bE\s*[_\-\s]?\s*b\b[^.;\n]{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"(?:\bE\s*[_\-\s]?\s*b\b|\bEb\b|binding\s+energy).{0,80}?(?:metal|atom|single[-\s]?atom|support|substrate|anchoring|M\s*[-–]\s*N|M\s*[-–]\s*C).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"(?:metal|atom|single[-\s]?atom|support|substrate|anchoring|M\s*[-–]\s*N|M\s*[-–]\s*C).{0,80}?(?:\bE\s*[_\-\s]?\s*b\b|\bEb\b|binding\s+energy).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
    ],
    "stability_parameter_Es": [
        r"(?:\bE\s*[_\-\s]?\s*s\b|\bEs\b|stability\s+(?:parameter|energy)).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
    ],
    "binding_energy": [
        r"(?:binding\s+energy|\bE\s*[_\-\s]?\s*b\b|\bEb\b).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
    ],
    "overpotential": [
        r"(?:overpotential|\u03b7|eta).{0,80}?([\-\+]?\d+[.]?\d*)\s*(V|eV)",
        r"([\-\+]?\d+[.]?\d*)\s*(V|eV).{0,60}(?:overpotential|\u03b7|eta)",
        ],
    "dos_claim": [
        r"(?:DOS|density\s+of\s+states).{0,120}(?:enhanc|increas|reduc|shift|broaden|narrow)",
        r"(PDOS|projected\s+DOS).{0,120}(?:hybridiz|overlap|contribut)",
        ],
    "charge_density_difference_claim": [
        r"(?:charge\s+density\s+difference|\u0394\u03c1|CDD|electron\s+density\s+difference).{0,150}",
        ],
    # Text/table LLM extraction categories for computational-material papers.
    # Rule patterns are intentionally empty; these are populated by LLM output
    # and persisted through the same DFTResult candidate/review chain.
    "activation_energy": [],
    "cohesive_energy": [],
    "formation_energy": [],
    "fluorination_energy": [],
    "permeation_barrier": [],
    "lattice_constant": [],
    "interlayer_distance": [],
    "pore_diameter": [],
    "permeance": [],
    "adsorption_molecule_fraction": [],
    "young_modulus": [],
    "seebeck_coefficient": [],
    "zt": [],
    "electrical_conductance": [],
    "thermal_conductance": [],
    "thermal_conductivity": [],
    "carrier_mobility": [],
    "optical_absorption_peak": [],
    "reaction_energy": [],
    "electronegativity_sum": [],
    "unoccupied_d_state_fraction": [],
    "orbital_occupancy_dxy": [],
    "orbital_occupancy_dx2_y2": [],
    "orbital_occupancy_dz2": [],
    "orbital_occupancy_dxz": [],
    "orbital_occupancy_dyz": [],
    "orbital_occupancy_dxz_dyz": [],
    "cutoff_energy": [],
    "k_points": [],
    "supercell": [],
    "vacuum_thickness": [],
    "u_value": [],
    "functional": [],
    "solvation_model": [],
    "calculation_setting": [],
    "convergence_force": [],
    "convergence_energy": [],
}

GRAPHITE_DEFECT_CATEGORY_RULES: dict[str, list[tuple[str, int, int]]] = {
    "formation_energy": [
        (
            r"(?:formation\s+energ(?:y|ies)|defect\s+formation\s+energ(?:y|ies)|E\s*[_\-\s]?\s*f).{0,100}?([-\+]?\d+(?:\.\d+)?)\s*(eV|meV|kJ/mol|kcal/mol)",
            1,
            2,
        ),
        (
            r"([-\+]?\d+(?:\.\d+)?)\s*(eV|meV|kJ/mol|kcal/mol).{0,80}(?:formation\s+energ(?:y|ies)|defect\s+formation)",
            1,
            2,
        ),
    ],
    "migration_barrier": [
        (
            r"(?:migration|diffusion).{0,40}?(?:barrier|energ(?:y|ies)).{0,80}?([-\+]?\d+(?:\.\d+)?)\s*(eV|meV|kJ/mol|kcal/mol)",
            1,
            2,
        ),
        (
            r"([-\+]?\d+(?:\.\d+)?)\s*(eV|meV|kJ/mol|kcal/mol).{0,80}(?:migration|diffusion).{0,40}?(?:barrier|energ(?:y|ies))",
            1,
            2,
        ),
    ],
    "band_gap": [
        (
            r"\b(?:band[\s\-]*gaps?|E\s*[_\-\s]?\s*g)\b.{0,80}?([-\+]?\d+(?:\.\d+)?)\s*(eV|meV)",
            1,
            2,
        ),
    ],
    "work_function": [
        (
            r"(?:work\s*function|WF).{0,80}?([-\+]?\d+(?:\.\d+)?)\s*(eV|meV)",
            1,
            2,
        ),
    ],
    "magnetic_moment": [
        (
            r"(?:magnetic\s*moment|spin\s*moment|magnetization).{0,80}?([-\+]?\d+(?:\.\d+)?)\s*(?:\u03bcB|μB|mu_B|Bohr\s+magnetons?)",
            1,
            0,
        ),
    ],
}

for _category, _rules in GRAPHITE_DEFECT_CATEGORY_RULES.items():
    CATEGORY_RULES.setdefault(_category, []).extend(_rules)

TABLE_HEADER_CATEGORY_RULES.extend(
    [
        (re.compile(r"(?:^|[^A-Za-z])(?:S\s*[-–]\s*S|S-S)(?:[^A-Za-z]|$).*(?:bond\s*)?(?:length|distance)|(?:bond\s*)?(?:length|distance).*(?:S\s*[-–]\s*S|S-S)", re.IGNORECASE), "bond_length_S-S", "A"),
        (re.compile(r"(?:^|[^A-Za-z])(?:M\s*[-–]\s*N|metal\s*[-–]?\s*N|[A-Z][a-z]?\s*[-–]\s*N)(?:[^A-Za-z]|$).*(?:bond\s*)?(?:length|distance)|(?:bond\s*)?(?:length|distance).*(?:M\s*[-–]\s*N|metal\s*[-–]?\s*N|[A-Z][a-z]?\s*[-–]\s*N)", re.IGNORECASE), "bond_length_M-N", "A"),
        (re.compile(r"(?:^|[^A-Za-z])(?:M\s*[-–]\s*S|metal\s*[-–]?\s*S|[A-Z][a-z]?\s*[-–]\s*S)(?:[^A-Za-z]|$).*(?:bond\s*)?(?:length|distance)|(?:bond\s*)?(?:length|distance).*(?:M\s*[-–]\s*S|metal\s*[-–]?\s*S|[A-Z][a-z]?\s*[-–]\s*S)", re.IGNORECASE), "bond_length_M-S", "A"),
        (re.compile(r"(?:^|[^A-Za-z])(?:M\s*[-–]\s*M|metal\s*[-–]?\s*metal|[A-Z][a-z]?\s*[-–]\s*[A-Z][a-z]?)(?:[^A-Za-z]|$).*(?:bond\s*)?(?:length|distance)|(?:bond\s*)?(?:length|distance).*(?:M\s*[-–]\s*M|metal\s*[-–]?\s*metal|[A-Z][a-z]?\s*[-–]\s*[A-Z][a-z]?)", re.IGNORECASE), "bond_length_M-M", "A"),
        (re.compile(r"(?:li\s*2\s*s.*deposition|deposition.*li\s*2\s*s).*(?:barrier|energy)", re.IGNORECASE), "li2s_deposition_barrier", "eV"),
        (re.compile(r"(?:li\s*2\s*s.*dissociation|dissociation.*li\s*2\s*s).*(?:energy|barrier)", re.IGNORECASE), "li2s_dissociation_energy", "eV"),
        (re.compile(r"(?:li\s*(?:ion)?\s*)?(?:diffusion|migration).*(?:barrier|energy)", re.IGNORECASE), "migration_barrier", "eV"),
        (re.compile(r"(?:reaction\s+energy|conversion\s+energy|^e[_\-\s]*rxn)", re.IGNORECASE), "reaction_energy", "eV"),
        (re.compile(r"(?:lowdin|löwdin).*(?:charge|population)", re.IGNORECASE), "Lowdin_charge", "e"),
        (re.compile(r"\bbader\b.*(?:charge|population)|(?:charge|population).*\bbader\b", re.IGNORECASE), "bader_charge", "e"),
        (re.compile(r"charge\s+transfer|electron\s+transfer", re.IGNORECASE), "charge_transfer", "e"),
        (re.compile(r"\bICOHP\b|integrated\s+COHP", re.IGNORECASE), "ICOHP", "eV"),
        (re.compile(r"\bCOHP\b", re.IGNORECASE), "COHP", "eV"),
        (re.compile(r"d[-\s]?orbital\s+occupanc|d\s+occupanc|d\s+electron\s+occupanc", re.IGNORECASE), "d_orbital_occupancy", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*xy|d\s*xy\s+occupanc", re.IGNORECASE), "orbital_occupancy_dxy", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*x\s*2\s*[-−]?\s*y\s*2|dx\s*2\s*[-−]?\s*y\s*2\s+occupanc", re.IGNORECASE), "orbital_occupancy_dx2_y2", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*(?:z\s*2|2\s*z)|d\s*(?:z\s*2|2\s*z)\s+occupanc", re.IGNORECASE), "orbital_occupancy_dz2", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*xz\b|dxz\s+occupanc", re.IGNORECASE), "orbital_occupancy_dxz", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*yz\b|dyz\s+occupanc", re.IGNORECASE), "orbital_occupancy_dyz", None),
        (re.compile(r"(?:rho|ρ)\s*\(?\s*d\s*xz\s*\+\s*d\s*yz|dxz\s*\+\s*dyz\s+occupanc", re.IGNORECASE), "orbital_occupancy_dxz_dyz", None),
        (re.compile(r"(?:DOS|density\s+of\s+states).*(?:Fermi|E[_\-\s]?F)|(?:Fermi|E[_\-\s]?F).*(?:DOS|density\s+of\s+states)", re.IGNORECASE), "DOS_at_Fermi", None),
        (re.compile(r"(?:d[-\s]?band\s+center|ε\s*d|epsilon\s*d)", re.IGNORECASE), "d_band_center", "eV"),
        (re.compile(r"(?:en[_\-\s]*sum|electronegativity\s+sum|sum\s+of\s+electronegativities)", re.IGNORECASE), "electronegativity_sum", None),
        (re.compile(r"(?:p[_\-\s]*un|unoccupied\s+d[-\s]*states?|proportion\s+of\s+unoccupied)", re.IGNORECASE), "unoccupied_d_state_fraction", None),
        (re.compile(r"(?:\bE\s*[_\-\s]?\s*b\b|\bEb\b).*(?:metal|support|site|anchoring)|(?:metal|support|site|anchoring).*(?:\bE\s*[_\-\s]?\s*b\b|\bEb\b|binding\s+energy)", re.IGNORECASE), "metal_support_binding_energy_Eb", "eV"),
        (re.compile(r"(?:\bE\s*[_\-\s]?\s*s\b|\bEs\b|stability\s+(?:parameter|energy))", re.IGNORECASE), "stability_parameter_Es", "eV"),
        (re.compile(r"(defect\s*)?formation\s+energ|(^e[_\-\s]*f$)|(^e[_\-\s]*form)", re.IGNORECASE), "formation_energy", "eV"),
        (re.compile(r"cohesive\s+energ|(^e[_\-\s]*coh)", re.IGNORECASE), "cohesive_energy", "eV/atom"),
        (re.compile(r"(migration|diffusion).*(barrier|energy)|(^e[_\-\s]*m$)", re.IGNORECASE), "migration_barrier", "eV"),
        (re.compile(r"(band\s*gap|e[_\-\s]*g)", re.IGNORECASE), "band_gap", "eV"),
        (re.compile(r"(work\s*function|^wf$)", re.IGNORECASE), "work_function", "eV"),
        (re.compile(r"(magnetic\s*moment|magnetization|spin\s*moment)", re.IGNORECASE), "magnetic_moment", "μB"),
        (re.compile(r"(?:functional|exchange[-\s]*correlation)", re.IGNORECASE), "functional", None),
        (re.compile(r"(?:cutoff|plane[-\s]*wave|kinetic\s+energy).*(?:energy)?", re.IGNORECASE), "cutoff_energy", "eV"),
        (re.compile(r"(?:k[-\s]*points?|monkhorst)", re.IGNORECASE), "k_points", None),
        (re.compile(r"(?:supercell|cell\s+size)", re.IGNORECASE), "supercell", None),
        (re.compile(r"(?:vacuum|vacuum\s+space|vacuum\s+thickness)", re.IGNORECASE), "vacuum_thickness", "A"),
        (re.compile(r"(?:hubbard\s+u|\bu\s+value\b|\bU\b)", re.IGNORECASE), "u_value", "eV"),
        (re.compile(r"(?:solvation|solvent|implicit\s+solvent)", re.IGNORECASE), "solvation_model", None),
        (re.compile(r"(?:force\s+convergence|convergence\s+force)", re.IGNORECASE), "convergence_force", "eV/A"),
        (re.compile(r"(?:energy\s+convergence|convergence\s+energy)", re.IGNORECASE), "convergence_energy", "eV"),
    ]
)


def _resolve_adsorbate(text: str) -> str | None:
    """从文本中推断吸附质."""
    normalized = re.sub(r"\s+", " ", _normalize_chem_label(text).lower())
    if not normalized:
        return None
    if re.search(r"\b(?:sulfur|sulphur)\s+(?:reduction|host|cathode|species|chemistry|loading|content)\b", normalized):
        return None
    if re.search(r"\b(?:figure|fig\.?|table|scheme)\s+s\s*8\b", normalized):
        s8_context = re.sub(r"\b(?:figure|fig\.?|table|scheme)\s+s\s*8\b", " ", normalized)
    else:
        s8_context = normalized
    if re.search(r"\b(?:cyclo[-\s]?s\s*8|s\s*8\s+(?:molecule|ring|cluster)|(?:molecular\s+)?s\s*8)\b", s8_context):
        return "S8"
    if re.search(r"\b(?:single\s+sulfur\s+atom|atomic\s+sulfur|sulfur\s+atom|s\s+atom)\b", normalized):
        return "S_atom"
    for key in sorted(ADSORBATE_MAP, key=len, reverse=True):
        name = ADSORBATE_MAP[key]
        escaped = re.escape(key.lower()).replace(r"\ ", r"[\s\-]+")
        haystack = s8_context if key == "s8" else normalized
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", haystack):
            return name
    return None


def _evidence_payload_fields(item: DFTResultItem) -> dict[str, Any]:
    return {
        "fact_family": item.fact_family,
        "atom_pair": item.atom_pair,
        "site_label": item.site_label,
        "state_context": item.state_context,
        "active_site_instance_key": item.active_site_instance_key,
        "metal_center_1": item.metal_center_1,
        "metal_center_2": item.metal_center_2,
        "support": item.support,
        "source_table_id": item.source_table_id,
        "source_table_caption": item.source_table_caption,
        "source_row_index": item.source_row_index,
        "source_column_index": item.source_column_index,
        "raw_row_text": item.raw_row_text,
        "raw_column_header": item.raw_column_header,
        "parser_version": item.parser_version,
        "confidence": item.confidence,
    }


def _infer_atom_pair(text: str) -> str | None:
    normalized = _normalize_chem_label(text)
    patterns = [
        (r"\bLi\s*-\s*S\b|\bLi\s+S\b", "Li-S"),
        (r"\bS\s*-\s*S\b|\bS\s+S\b", "S-S"),
        (r"\bM\s*-\s*N\b|\bmetal\s*-\s*N\b|[A-Z][a-z]?\s*-\s*N\b", "M-N"),
        (r"\bM\s*-\s*S\b|\bmetal\s*-\s*S\b|[A-Z][a-z]?\s*-\s*S\b", "M-S"),
        (r"\bM\s*-\s*M\b|\bmetal\s*-\s*metal\b|[A-Z][a-z]?\s*-\s*[A-Z][a-z]?\b", "M-M"),
    ]
    for pattern, atom_pair in patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            return atom_pair
    return None


def _category_atom_pair(category: str, evidence: str) -> str | None:
    if category.startswith("bond_length_"):
        return category.removeprefix("bond_length_")
    return _infer_atom_pair(evidence)


def _fact_family_for_category(category: str) -> str:
    if category.startswith("bond_length_") or category == "li_s_bond_length":
        return "bond_length_table"
    if category in {
        "Lowdin_charge",
        "bader_charge",
        "charge_transfer",
        "ICOHP",
        "COHP",
        "d_orbital_occupancy",
        "DOS_at_Fermi",
        "d_band_center",
        "work_function",
        "magnetic_moment",
        "electronegativity_sum",
        "unoccupied_d_state_fraction",
        "orbital_occupancy_dxy",
        "orbital_occupancy_dx2_y2",
        "orbital_occupancy_dz2",
        "orbital_occupancy_dxz",
        "orbital_occupancy_dyz",
        "orbital_occupancy_dxz_dyz",
    }:
        return "electronic_descriptor_table"
    if category in {"metal_support_binding_energy_Eb", "stability_parameter_Es", "formation_energy", "cohesive_energy"}:
        return "active_site_stability_table"
    if category in {"gibbs_free_energy_change", "reaction_energy"}:
        return "reaction_free_energy_table"
    if category in {
        "reaction_barrier",
        "activation_energy",
        "migration_barrier",
        "li2s_decomposition_barrier",
        "li2s_deposition_barrier",
        "li2s_dissociation_energy",
        "li2s_nucleation_barrier",
    }:
        return "reaction_barrier_table"
    if category in {
        "cutoff_energy",
        "k_points",
        "supercell",
        "vacuum_thickness",
        "u_value",
        "functional",
        "solvation_model",
        "calculation_setting",
        "convergence_force",
        "convergence_energy",
    }:
        return "calculation_settings"
    if category == "adsorption_energy":
        return "adsorption_energy_matrix"
    return "scalar_dft_result"


def _is_ml_descriptor_context(*parts: str | None) -> bool:
    text = " ".join(part or "" for part in parts)
    return bool(
        re.search(
            r"\b(?:machine\s+learning|ML\s+dataset|ML\s+descriptor|input\s+features?|descriptor\s+table|Tables?\s+S[678])\b",
            text,
            re.IGNORECASE,
        )
    )


def _fact_family_for_table_cell(category: str, caption: str, header: str, row_text: str) -> str:
    family = _fact_family_for_category(category)
    if family in {"electronic_descriptor_table", "bond_length_table", "active_site_stability_table"} and _is_ml_descriptor_context(
        caption,
        header,
        row_text,
    ):
        return "ml_descriptor"
    return family


def _adsorbate_from_adsorption_header(header: str) -> str | None:
    text = re.sub(r"\s+", " ", _normalize_chem_label(header)).strip()
    if not text:
        return None
    patterns = [
        r"(?:E\s*[_\-\s]?\s*ads|Eads)\s*\(\s*([^)]+?)\s*\)",
        r"(?:E\s*[_\-\s]?\s*ads|Eads)\s+(?:of|for)\s+(.+?)(?:\s*\(|$)",
        r"(?:E\s*[_\-\s]?\s*ads|Eads)\s*[-–]\s*(.+?)(?:\s*\(|$)",
        r"adsorption\s+energy\s+(?:of|for)\s+(.+?)(?:\s*\(|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\b(?:ev|mev|kj/mol|kcal/mol|a|angstroms?|nm|pm)\b", " ", match.group(1), flags=re.IGNORECASE)
        candidate = re.sub(r"[^A-Za-z0-9*+\-_\s().]", " ", candidate).strip(" -_()")
        resolved = _resolve_adsorbate(candidate)
        if resolved:
            return resolved
    return None


def _is_explicit_adsorbate_header(header: str | None) -> bool:
    return bool(
        re.search(
            r"\b(adsorbate|intermediate|species|molecule|slurry|lips|li2sx)\b",
            header or "",
            re.IGNORECASE,
        )
    )


def _category_should_default_null_adsorbate(category: str) -> bool:
    return _fact_family_for_category(category) in {
        "bond_length_table",
        "electronic_descriptor_table",
        "active_site_stability_table",
        "calculation_setting_table",
        "calculation_settings",
        "reaction_free_energy_table",
        "reaction_barrier_table",
    }


def _row_reaction_step(row: list[str], headers: list[str], category: str, header: str) -> str:
    family = _fact_family_for_category(category)
    if family in {"reaction_free_energy_table", "reaction_barrier_table"} and row:
        for idx, cell in enumerate(row):
            column = headers[idx] if idx < len(headers) else ""
            context = f"{column} {cell}"
            if re.search(
                r"\b(?:step|reaction|process|conversion|pathway|intermediate)\b|Li\s*2\s*S\s*\d?\s*(?:->|→|to|-)\s*Li\s*2\s*S\s*\d?",
                context,
                re.IGNORECASE,
            ) and (
                _looks_like_safe_table_label(cell)
                or re.search(r"Li\s*2\s*S\s*\d?\s*(?:->|→|to|-)\s*Li\s*2\s*S\s*\d?", cell, re.IGNORECASE)
            ):
                return cell.strip()
    return header


def _category_unit_from_label(label: str, caption: str = "") -> tuple[str, str | None] | None:
    text = re.sub(r"\s+", " ", _normalize_chem_label(label)).strip()
    lowered = text.lower()
    if not text:
        return None
    setting_labels = [
        (r"(?:functional|exchange[-\s]*correlation)", "functional", None),
        (r"(?:cutoff|plane[-\s]*wave|kinetic\s+energy)", "cutoff_energy", "eV"),
        (r"(?:k[-\s]*points?|monkhorst)", "k_points", None),
        (r"(?:supercell|cell\s+size)", "supercell", None),
        (r"(?:vacuum|vacuum\s+space|vacuum\s+thickness)", "vacuum_thickness", "A"),
        (r"(?:hubbard\s+u|\bu\s+value\b|\bU\b)", "u_value", "eV"),
        (r"(?:solvation|solvent|implicit\s+solvent)", "solvation_model", None),
        (r"(?:force\s+convergence|convergence\s+force)", "convergence_force", "eV/A"),
        (r"(?:energy\s+convergence|convergence\s+energy)", "convergence_energy", "eV"),
    ]
    for pattern, category, unit in setting_labels:
        if re.search(pattern, text, re.IGNORECASE):
            return category, unit
    if re.search(r"(?:Δ\s*G|delta\s*G|free\s+energy|gibbs)", text, re.IGNORECASE):
        return "gibbs_free_energy_change", "eV"
    if re.search(r"(?:li\s*2\s*s.*deposition|deposition.*li\s*2\s*s).*(?:barrier|energy)", lowered):
        return "li2s_deposition_barrier", "eV"
    if re.search(r"(?:li\s*2\s*s.*decomposition|decomposition.*li\s*2\s*s).*(?:barrier|energy)", lowered):
        return "li2s_decomposition_barrier", "eV"
    if re.search(r"(?:li\s*(?:ion)?\s*)?(?:diffusion|migration).*(?:barrier|energy)", lowered):
        return "migration_barrier", "eV"
    for pattern, category, unit in TABLE_HEADER_CATEGORY_RULES:
        if pattern.search(text) or pattern.search(f"{text} {caption}"):
            return category, unit
    return None


def _is_supplement_label_number(cell: str, match: re.Match[str]) -> bool:
    token = match.group(0)
    if token != "8":
        return False
    prefix = cell[: match.start()]
    suffix = cell[match.end() :]
    if not re.search(r"(?:figure|fig\.?|table|scheme)\s+s\s*$", prefix, re.IGNORECASE):
        return False
    return bool(re.match(r"\b", suffix))


def _has_graphite_defect_context(text: str) -> bool:
    return bool(
        re.search(
            r"\b(graphene|graphite|graphitic|carbon|vacancy|divacancy|monovacancy|defect|stone[\s\-]?wales|interstitial|grain\s+boundary)\b",
            text or "",
            re.IGNORECASE,
        )
    )


def _is_reference_like_evidence(text: str) -> bool:
    lowered = (text or "").lower()
    if re.search(r"\b(references|bibliography)\b", lowered):
        return True
    if re.search(r"\|\s*\[?\d+\]?\s*\|", text or "") and re.search(r"\b(?:journal|doi|vol|pp|pages?|publisher)\b", lowered):
        return True
    return False


def _extract_context_around_match(text: str, match_start: int, match_end: int, window: int = 200) -> str:
    """截取匹配周围的上下文作为 evidence."""
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    snippet = text[start:end].replace("\n", " ").strip()
    if len(snippet) > 400:
        return snippet[:400] + "..."
    return snippet


def _extract_sentence_around_match(text: str, match_start: int, match_end: int) -> str:
    start = max(text.rfind(".", 0, match_start), text.rfind(";", 0, match_start), text.rfind("\n", 0, match_start)) + 1
    end_candidates = [pos for pos in (text.find(".", match_end), text.find(";", match_end), text.find("\n", match_end)) if pos >= 0]
    end = min(end_candidates) + 1 if end_candidates else min(len(text), match_end + 160)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _match_crosses_sentence(match_text: str) -> bool:
    """检测正则匹配文本是否跨越了句子边界.

    跨句匹配是 DFT 提取产生假阳性的主要原因之一：
    正则中 .{0,80} / .{0,120} 等宽泛量词可能把分属不同句子的
    "属性名" 和 "数值" 错误地关联在一起。

    检测策略：在匹配文本内部寻找 ". 大写字母" 或 "? 大写字母"
    的模式（排除首字符，因为匹配开始位置可能在句中）。
    """
    text = match_text or ""
    if len(text) < 4:
        return False
    # 排除首字符（匹配起点可能在句中），检测内部是否有句号+大写
    inner = text[1:]
    if re.search(r"[.!?]\s+[A-Z]", inner):
        return True
    # 分号+大写也视为跨句
    if re.search(r";\s+[A-Z]", inner):
        return True
    return False


def _parse_scientific_notation(text: str) -> float | None:
    """解析科学计数法表达的数值.

    支持格式:
      - "1.5 × 10^3" / "1.5 × 10⁻³"  (LaTeX / Unicode 上标)
      - "1.5e3" / "1.5E-4"             (编程风格)
      - "1.5 × 10³"                     (Unicode 上标数字)
      - "1.5·10^3"                       (中间点)
      - "1.5 x 10^3"                     (小写 x)
      - "10³" / "10⁻³"                  (纯上标)

    Returns:
        解析后的 float，或 None（如果不是科学计数法格式）
    """
    if not text:
        return None
    text = text.strip()

    # Unicode 上标数字 → 普通数字 + 负号处理
    superscript_map = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
    normalized = text.translate(superscript_map)

    # 模式 1: "1.5 × 10^3" / "1.5 x 10^-4" / "1.5·10^3"
    m = re.match(
        r"^\s*([-\+]?\d+(?:\.\d+)?)\s*[×x·]\s*10\^([-\+]?\d+)\s*$",
        normalized,
    )
    if m:
        try:
            mantissa = float(m.group(1))
            exponent = int(m.group(2))
            return mantissa * (10 ** exponent)
        except (ValueError, OverflowError):
            return None

    # 模式 2: "1.5e3" / "1.5E-4" (Python 原生)
    m = re.match(r"^\s*([-\+]?\d+(?:\.\d+)?)[eE]([-\+]?\d+)\s*$", normalized)
    if m:
        try:
            return float(normalized)
        except (ValueError, OverflowError):
            return None

    # 模式 3: 纯 "10^3" / "10^-4"（无尾数）
    m = re.match(r"^\s*10\^([-\+]?\d+)\s*$", normalized)
    if m:
        try:
            exponent = int(m.group(1))
            return float(10 ** exponent)
        except (ValueError, OverflowError):
            return None

    return None


def _parse_numeric_value(text: str) -> float | None:
    """统一的数值解析入口：先尝试科学计数法，再走普通浮点.

    这是对 _parse_float 的增强替代，用于表格单元格和 LLM 输出字段。
    规则提取器中的正则已捕获简单小数，但表格/LLM 渠道可能拿到
    "1.5 × 10^3" 这样的原始值。
    """
    if not text:
        return None
    normalized = _normalize_numeric_text(text).strip()

    # 先试科学计数法
    sci = _parse_scientific_notation(normalized)
    if sci is not None:
        return sci

    # 检测 "数字 × 10^指数" 嵌入在更大字符串中
    m = re.search(
        r"([-\+]?\d+(?:\.\d+)?)\s*[×x·]\s*10\^([-\+]?\d+)",
        normalized,
    )
    if m:
        try:
            mantissa = float(m.group(1))
            exponent = int(m.group(2))
            return mantissa * (10 ** exponent)
        except (ValueError, OverflowError):
            pass

    # Unicode 上标版本
    superscript_map = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
    norm2 = normalized.translate(superscript_map)
    m = re.search(
        r"([-\+]?\d+(?:\.\d+)?)\s*[×x·]\s*10\^([-\+]?\d+)",
        norm2,
    )
    if m:
        try:
            mantissa = float(m.group(1))
            exponent = int(m.group(2))
            return mantissa * (10 ** exponent)
        except (ValueError, OverflowError):
            pass

    # 兜底：普通浮点
    return _parse_float(normalized)


def _normalize_numeric_text(text: str) -> str:
    """Normalize common PDF/OCR minus variants so signed values keep their sign."""
    if not text:
        return ""
    return (
        text.replace("\u2212", "-")
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    return float(_normalize_numeric_text(value).strip())


def _parse_uncertainty_float(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", _normalize_numeric_text(value))
    cleaned = re.sub(r"\(\d+\)$", "", cleaned)
    return float(cleaned)


def _parse_match_float(match: re.Match[str], group_index: int) -> float | None:
    """从正则匹配组中解析数值，增强支持科学计数法.

    M4 修复：原始 _parse_match_float 仅做 float()，无法处理
    正则匹配到的 "1.5 × 10^3" 或 "2.3e-4" 等科学计数法字符串。
    现在先尝试 _parse_numeric_value，失败再走原始逻辑。
    """
    value = match.group(group_index)
    if value is None:
        return None
    normalized = _normalize_numeric_text(value).strip()
    start = match.start(group_index)
    if normalized.startswith("-") and start > 0 and match.string[start - 1].isdigit():
        normalized = normalized[1:]
    # M4: 先尝试科学计数法解析
    sci = _parse_numeric_value(normalized)
    if sci is not None:
        return sci
    # 兜底：原始逻辑
    try:
        return float(normalized)
    except (ValueError, OverflowError):
        return None


def _looks_like_reference_token(value: str | None) -> bool:
    if not value:
        return False
    return re.fullmatch(r"\[?\d+(?:[-,]\d+)*\]?", value.strip()) is not None


def _looks_like_safe_table_label(value: str | None) -> bool:
    text = re.sub(r"\s+", " ", _normalize_chem_label(value)).strip()
    if not text:
        return False
    if len(text) > 48:
        return False
    if any(mark in text for mark in (";", ":", "=")):
        return False
    token_count = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff\-\+/().]+", text))
    if token_count > 6:
        return False
    lowered = text.lower()
    if re.search(r"\b(changed|increase[sd]?|decrease[sd]?|stable|unstable|discussed|observed|shows?|indicates?)\b", lowered):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff\-\+/().\s]+", text))


def _category_semantic_conflict(category: str, evidence: str, value: float | None, unit: str | None) -> bool:
    lowered = (evidence or "").lower()
    normalized_unit = (unit or "").strip().lower()
    dimensional_markers = (
        "diameter",
        "pore size",
        "pore diameter",
        "lattice constant",
        "lattice parameter",
        "bond length",
        "interlayer distance",
        "thickness",
        "width",
        "length",
        "nm",
        "angstrom",
        "å",
        " a ",
    )
    if category in {"band_gap", "work_function"}:
        if normalized_unit in {"a", "å", "nm", "pm"}:
            return True
        if any(marker in lowered for marker in dimensional_markers):
            return True
        if category == "band_gap" and value is not None and value > 10:
            return True
    if category == "limiting_potential" and re.search(r"\bsulfur\b", lowered):
        return True
    if category == "adsorption_energy" and re.search(r"\b(?:\d+\s+electrons?|figure\s+s\s*8|fig\.?\s+s\s*8)\b", lowered):
        return True
    if category in {"adsorption_energy", "metal_support_binding_energy_Eb"} and re.search(
        r"\bdelithiation\s+energy\b|\bE\s*1\b",
        evidence or "",
        re.IGNORECASE,
    ):
        return True
    if category in {"ICOHP", "COHP", "DOS_at_Fermi", "d_orbital_occupancy", "Lowdin_charge", "bader_charge"}:
        if value == 8 and re.search(r"\b(?:see\s+)?(?:figure|fig\.?|table|scheme)\s+s\s*8\b", lowered):
            return True
    return False


def _should_keep_result(category: str, adsorbate: str | None, value: float | None, evidence: str) -> bool:
    if _is_reference_like_evidence(evidence):
        return False
    if category in NON_NUMERIC_DFT_CLAIM_CATEGORIES:
        return False
    if category in NUMERIC_CATEGORIES and value is None:
        return False
    if _looks_like_reference_token(adsorbate):
        return False
    if category in {"limiting_potential", "overpotential"}:
        lowered = evidence.lower()
        if category == "limiting_potential" and not re.search(
            r"(?:limiting\s+potential|\bU\s*[_\-]\s*L\b|\bU\s+L\b|\bUL\b)",
            evidence,
            re.IGNORECASE,
        ):
            return False
        if re.search(r"\[\s*\d+\s*\]", evidence):
            return False
        if value is not None and abs(value) > 20:
            return False
        if re.search(r"\b\d{4}\b", lowered) and not re.search(r"\b(?:0|1|2|3|4|5)\.\d+\s*(?:v|ev)\b", lowered):
            return False
    if category == "adsorption_energy" and not adsorbate:
        return False
    if category == "adsorption_energy":
        lowered = evidence.lower()
        if re.search(r"\bdelithiation\s+energy\b|\bE\s*1\b", evidence, re.IGNORECASE):
            return False
        if re.search(r"\b(?:sulfur|sulphur)\s+(?:reduction|host|cathode|chemistry|loading|content)\b", lowered):
            return False
        if re.search(r"\b(?:figure|fig\.?|table|scheme)\s+s\s*8\b", lowered) and adsorbate == "S8":
            return False
    if category == "metal_support_binding_energy_Eb" and not re.search(
        r"\b(?:metal|atom|single[-\s]?atom|support|substrate|anchoring|stability|M\s*[-–]\s*N|M\s*[-–]\s*C)\b",
        evidence,
        re.IGNORECASE,
    ):
        return False
    if category == "metal_support_binding_energy_Eb" and re.search(
        r"\bdelithiation\s+energy\b|\bE\s*1\b",
        evidence,
        re.IGNORECASE,
    ):
        return False
    if category == "binding_energy":
        lowered = evidence.lower()
        if re.search(r"\b(?:li2s8|li2s6|li2s4|li2s2|li2s|lips|polysulfide)\b", lowered):
            return False
        if re.search(r"\b(?:metal|single[-\s]?atom|support|substrate|anchoring|m\s*[-–]\s*n|m\s*[-–]\s*c)\b", lowered):
            return False
    if category == "stability_parameter_Es" and not re.search(
        r"\b(?:stability|stable|E\s*[_\-\s]?\s*s|Es)\b",
        evidence,
        re.IGNORECASE,
    ):
        return False
    if category == "formation_energy":
        lowered = evidence.lower()
        if not (adsorbate or _has_graphite_defect_context(evidence)):
            return False
        if value is not None and abs(value) < 1e-12:
            return False
        if not re.search(
            r"(formation\s+(?:energ(?:y|ies)|free\s+energ(?:y|ies)|takes|took)|defect\s+formation|e\s*[_\-\s]?\s*f\b)",
            lowered,
            re.IGNORECASE,
        ):
            return False
        if re.search(
            r"\b(underestimat\w*|overestimat\w*|disagreement|difference|deviation|margin|"
            r"order\s+of|error\s+bars?|standard\s+deviations?|finite[-\s]concentration|"
            r"energy\s+scale|energy\s+drops?|cutoff\s+energy|force\s+tolerance|atomization\s+energ|activation\s+energy)\b",
            lowered,
            re.IGNORECASE,
        ):
            return False
        if re.search(r"formation\s+energ(?:y|ies)\s+drops?", lowered):
            return False
        if re.search(r"\b\d+(?:\.\d+)?\s+electrons?\b", lowered) or re.search(r"electrons?.{0,40}\beV\b", lowered):
            return False
    if category == "band_gap" and not re.search(
        r"\b(?:band[\s\-]*gaps?|e\s*[_\-\s]?\s*g)\b",
        evidence,
        re.IGNORECASE,
    ):
        return False
    if category in {"band_gap", "work_function"} and value is not None and not (-1 <= value <= 30):
        return False
    if category == "magnetic_moment" and value is not None and not (-20 <= value <= 20):
        return False
    if category == "adsorption_energy" and value is not None and value > 0:
        lowered = evidence.lower()
        if re.search(r"\d(?:\.\d+)?\s*-\s*\d", lowered) and "positive value" in lowered:
            return False
    return True


def _guess_section_name(sections: list[Any], page: int | None) -> str | None:
    """根据页码推断章节名."""
    if page is None or not sections:
        return None
    for sec in sections:
        ps = getattr(sec, "page_start", None)
        pe = getattr(sec, "page_end", None)
        if ps is not None and pe is not None and ps <= page <= pe:
            title = getattr(sec, "section_title", None)
            if title:
                return title
    return None


def _scan_tables_for_category(tables: list[Any], category: str) -> list[DFTResultItem]:
    """扫描表格内容寻找数值结果（简单启发式）."""
    results: list[DFTResultItem] = []
    patterns = CATEGORY_RULES.get(category, [])
    for tbl in tables:
        caption = getattr(tbl, "caption", "") or ""
        content = getattr(tbl, "markdown_content", "") or ""
        if _is_reference_like_evidence(f"{caption}\n{content}"):
            continue
        if _parse_markdown_table(content)[1]:
            continue
        combined = _normalize_numeric_text(f"{caption}\n{content}")
        for pat_tuple in patterns:
            if isinstance(pat_tuple, tuple):
                pattern, vg, ug = pat_tuple
            else:
                pattern, vg, ug = pat_tuple, 1, 2
            for m in re.finditer(pattern, combined, re.IGNORECASE):
                try:
                    val = _parse_match_float(m, vg) if vg else None
                    raw_unit_value = m.group(ug) if ug and ug < len(m.groups()) + 1 else None
                    raw_unit = raw_unit_value.strip() if isinstance(raw_unit_value, str) else None
                    unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
                except (ValueError, IndexError):
                    val, unit = None, None
                loc = SourceLocation(
                    table=caption[:80] if caption else None,
                    page=getattr(tbl, "page", None),
                )
                adsorbate = _resolve_adsorbate(combined)
                evidence = _extract_context_around_match(combined, m.start(), m.end())
                adsorbate = _resolve_adsorbate(m.group(0)) or adsorbate
                quality_evidence = _extract_sentence_around_match(combined, m.start(), m.end())
                if category not in GRAPHITE_DEFECT_CATEGORY_RULES:
                    quality_evidence = evidence
                if not _should_keep_result(category, adsorbate, val, quality_evidence):
                    continue
                results.append(DFTResultItem(
                    category=category,
                    adsorbate=adsorbate,
                    value=val,
                    unit=unit,
                    evidence_text=evidence,
                    source_location=loc,
                    confidence=0.75 if val is not None else 0.45,
                    fact_family=_fact_family_for_category(category),
                    atom_pair=_category_atom_pair(category, evidence),
                    source_table_id=str(getattr(tbl, "id", "") or getattr(tbl, "table_id", "") or caption or "table"),
                    source_table_caption=caption or None,
                ))
    return results


def _parse_markdown_table(content: str) -> tuple[list[str], list[list[str]]]:
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
    if len(table_lines) < 2:
        return [], []
    headers = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    rows: list[list[str]] = []
    for line in table_lines[1:]:
        if re.fullmatch(r"\|?[\s:\-|\+]+\|?", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(cells)
    return headers, rows


def _extract_markdown_table_blocks(markdown: str) -> list[_MarkdownTableBlock]:
    blocks: list[_MarkdownTableBlock] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            headers, rows = _parse_markdown_table("\n".join(current))
            if headers and rows:
                blocks.append(_MarkdownTableBlock(markdown_content="\n".join(current)))
        current = []

    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append(stripped)
            continue
        flush()
    flush()
    return blocks


def _infer_table_columns(
    headers: list[str],
    caption: str = "",
) -> tuple[dict[int, tuple[str, str | None]], int | None, int | None]:
    category_columns: dict[int, tuple[str, str | None]] = {}
    adsorbate_col: int | None = None
    catalyst_col: int | None = None
    caption_text = _normalize_chem_label(caption)
    for idx, header in enumerate(headers):
        header_text = re.sub(r"\s+", " ", _normalize_chem_label(header)).strip()
        lowered = header_text.lower()
        compact = re.sub(r"[^a-z0-9]+", "", lowered)
        header_context = f"{header_text} {caption_text}".strip()
        if catalyst_col is None and re.search(
            r"\b(catalyst|material|system|systems|model|substrate|structure|structures)\b",
            lowered,
        ):
            catalyst_col = idx
        if adsorbate_col is None and re.search(
            r"\b(adsorbate|intermediate|species|molecule|slurry|lips|li2sx)\b",
            lowered,
        ):
            adsorbate_col = idx
        if re.search(
            r"(?:E\s*[_\-\s]?\s*ads|Eads)\s*(?:\(|\s+(?:of|for)\s+|[-–])|adsorption\s+energy\s+(?:of|for)\s+",
            header_text,
            re.IGNORECASE,
        ):
            category_columns[idx] = ("adsorption_energy", "eV")
            continue
        if re.search(r"(?:Δ\s*G|delta\s*G|free\s+energy|gibbs)", header_text, re.IGNORECASE):
            category_columns[idx] = ("gibbs_free_energy_change", "eV")
            continue
        if compact in {"eb", "ebeV", "ebe"} or re.fullmatch(r"eb(?:ev)?", compact):
            category_columns[idx] = ("metal_support_binding_energy_Eb", "eV")
            continue
        if compact in {"es", "eseV", "ese"} or re.fullmatch(r"es(?:ev)?", compact):
            category_columns[idx] = ("stability_parameter_Es", "eV")
            continue
        if re.search(r"\bd\s*M\s*-\s*S\b|\bM\s*-\s*S\b", header_text, re.IGNORECASE):
            category_columns[idx] = ("bond_length_M-S", "A")
            continue
        if re.search(r"\bd\s*M\s*-\s*N\b|\bM\s*-\s*N\b", header_text, re.IGNORECASE):
            category_columns[idx] = ("bond_length_M-N", "A")
            continue
        if re.search(r"(migration|diffusion).*(barrier|energy)", lowered):
            category_columns[idx] = ("migration_barrier", "eV")
            continue
        if re.search(r"(?:li\s*2\s*s.*deposition|deposition.*li\s*2\s*s).*(?:barrier|energy)", lowered):
            category_columns[idx] = ("li2s_deposition_barrier", "eV")
            continue
        if re.search(r"(?:li\s*2\s*s.*dissociation|dissociation.*li\s*2\s*s).*(?:energy|barrier)", lowered):
            category_columns[idx] = ("li2s_dissociation_energy", "eV")
            continue
        if re.search(r"(?:reaction\s+energy|conversion\s+energy|^e[_\-\s]*rxn)", lowered):
            category_columns[idx] = ("reaction_energy", "eV")
            continue
        if re.search(r"(?:en[_\-\s]*sum|electronegativity\s+sum|sum\s+of\s+electronegativities)", lowered):
            category_columns[idx] = ("electronegativity_sum", None)
            continue
        if re.search(r"(?:p[_\-\s]*un|unoccupied\s+d[-\s]*states?|proportion\s+of\s+unoccupied)", lowered):
            category_columns[idx] = ("unoccupied_d_state_fraction", None)
            continue
        orbital_columns = {
            "orbital_occupancy_dxz_dyz": r"(?:rho|ρ)\s*\(?\s*d\s*xz\s*\+\s*d\s*yz|dxz\s*\+\s*dyz\s+occupanc",
            "orbital_occupancy_dx2_y2": r"(?:rho|ρ)\s*\(?\s*d\s*x\s*2\s*[-−]?\s*y\s*2|dx\s*2\s*[-−]?\s*y\s*2\s+occupanc",
            "orbital_occupancy_dz2": r"(?:rho|ρ)\s*\(?\s*d\s*(?:z\s*2|2\s*z)|d\s*(?:z\s*2|2\s*z)\s+occupanc",
            "orbital_occupancy_dxy": r"(?:rho|ρ)\s*\(?\s*d\s*xy|d\s*xy\s+occupanc",
            "orbital_occupancy_dxz": r"(?:rho|ρ)\s*\(?\s*d\s*xz\b|dxz\s+occupanc",
            "orbital_occupancy_dyz": r"(?:rho|ρ)\s*\(?\s*d\s*yz\b|dyz\s+occupanc",
        }
        matched_orbital = False
        for orbital_category, orbital_pattern in orbital_columns.items():
            if re.search(orbital_pattern, header_text, re.IGNORECASE):
                category_columns[idx] = (orbital_category, None)
                matched_orbital = True
                break
        if matched_orbital:
            continue
        calculation_columns = [
            (r"(?:functional|exchange[-\s]*correlation)", "functional", None),
            (r"(?:cutoff|plane[-\s]*wave|kinetic\s+energy)", "cutoff_energy", "eV"),
            (r"(?:k[-\s]*points?|monkhorst)", "k_points", None),
            (r"(?:supercell|cell\s+size)", "supercell", None),
            (r"(?:vacuum|vacuum\s+space|vacuum\s+thickness)", "vacuum_thickness", "A"),
            (r"(?:hubbard\s+u|\bu\s+value\b|\bU\b)", "u_value", "eV"),
            (r"(?:solvation|solvent|implicit\s+solvent)", "solvation_model", None),
            (r"(?:force\s+convergence|convergence\s+force)", "convergence_force", "eV/A"),
            (r"(?:energy\s+convergence|convergence\s+energy)", "convergence_energy", "eV"),
        ]
        matched_setting = False
        for setting_pattern, setting_category, setting_unit in calculation_columns:
            if re.search(setting_pattern, header_text, re.IGNORECASE):
                category_columns[idx] = (setting_category, setting_unit)
                matched_setting = True
                break
        if matched_setting:
            continue
        if re.search(r"(?:\be\s*[_\-\s]?\s*b\b|\beb\b|binding\s+energy)", lowered) and re.search(
            r"(?:metal|support|site|anchoring|stability|m\s*[-–]\s*n|m\s*[-–]\s*c)",
            header_context,
            re.IGNORECASE,
        ):
            category_columns[idx] = ("metal_support_binding_energy_Eb", "eV")
            continue
        if re.search(r"(?:\be\s*[_\-\s]?\s*s\b|\bes\b|stability\s+(?:parameter|energy))", lowered):
            category_columns[idx] = ("stability_parameter_Es", "eV")
            continue
        for pattern, category, unit in TABLE_HEADER_CATEGORY_RULES:
            if pattern.search(header_text):
                category_columns[idx] = (category, unit)
                break
    if adsorbate_col is None and catalyst_col is None and headers:
        first_header = _normalize_chem_label(headers[0]).lower()
        if (
            not any(pattern.search(first_header) for pattern, _, _ in TABLE_HEADER_CATEGORY_RULES)
            and not re.search(r"\b(site|system|material|catalyst|model|state)\b", first_header, re.IGNORECASE)
        ):
            adsorbate_col = 0
    return category_columns, adsorbate_col, catalyst_col


def _first_numeric_match(cell: str) -> re.Match[str] | None:
    return re.search(
        r"(?:"
        r"[-+]?\d+(?:\.\d+)?\s*[×x·]\s*10\^[-+]?\d+"
        r"|[-+]?\d+(?:\.\d+)?[eE][-+]?\d+"
        r"|[-+]?\d*\.?\d+"
        r")",
        cell,
    )


def _unit_from_cell(cell: str, default_unit: str | None, category: str) -> str | None:
    if category in TEXT_SCALAR_CATEGORIES:
        return None
    unit_match = re.search(
        r"(eV/A|eV/Å|states/eV|eV\^-1|eV-1|eV|meV|kJ/mol|kcal/mol|Å|A|angstroms?|nm|pm|V|μB|mu_B|e[\u2212-]?|electrons?)",
        cell,
        re.IGNORECASE,
    )
    if unit_match:
        raw_unit = unit_match.group(1).strip()
        return UNIT_ALIASES.get(raw_unit.lower(), raw_unit)
    return default_unit


def _scan_key_value_rows(
    headers: list[str],
    rows: list[list[str]],
    *,
    caption: str,
    page: int | None,
    source_table_id: str,
) -> list[DFTResultItem]:
    if len(headers) < 2:
        return []
    first_header = _normalize_chem_label(headers[0]).lower()
    second_header = _normalize_chem_label(headers[1]).lower()
    if not re.search(r"\b(setting|parameter|property|descriptor|feature|quantity|metric)\b", first_header):
        return []
    if not re.search(r"\b(value|result|number|data)\b", second_header):
        return []
    results: list[DFTResultItem] = []
    for row_index, row in enumerate(rows):
        if len(row) < 2:
            continue
        label = row[0].strip()
        cell = _normalize_numeric_text(row[1].strip())
        category_unit = _category_unit_from_label(label, caption)
        if not category_unit or not cell:
            continue
        category, default_unit = category_unit
        value_match = _first_numeric_match(cell)
        if value_match and _is_supplement_label_number(cell, value_match):
            value_match = None
        if category in NUMERIC_CATEGORIES and not value_match:
            continue
        value = None if category in TEXT_SCALAR_CATEGORIES else (_parse_numeric_value(value_match.group(0)) if value_match else None)
        unit = _unit_from_cell(cell, default_unit, category)
        row_text = " | ".join(row)
        evidence = f"{label}: {cell}; row: {row_text}"
        adsorbate = None if _category_should_default_null_adsorbate(category) else _resolve_adsorbate(evidence)
        if not _should_keep_result(category, adsorbate, value, evidence):
            continue
        results.append(
            DFTResultItem(
                category=category,
                adsorbate=adsorbate,
                value=value,
                unit=unit,
                reaction_step=label,
                evidence_text=evidence[:450],
                source_location=SourceLocation(table=caption[:80] if caption else "Table", page=page),
                confidence=0.84 if value is not None else 0.68,
                fact_family=_fact_family_for_table_cell(category, caption, label, row_text),
                atom_pair=_category_atom_pair(category, f"{label} {row_text}"),
                site_label=None,
                state_context=label,
                active_site_instance_key=None,
                source_table_id=source_table_id,
                source_table_caption=caption or None,
                source_row_index=row_index,
                source_column_index=1,
                raw_row_text=row_text,
                raw_column_header=headers[1],
            )
        )
    return results


def _scan_structured_tables(tables: list[Any]) -> list[DFTResultItem]:
    results: list[DFTResultItem] = []
    for tbl in tables:
        caption = getattr(tbl, "caption", "") or ""
        content = _normalize_numeric_text(getattr(tbl, "markdown_content", "") or "")
        if _is_reference_like_evidence(f"{caption}\n{content}"):
            continue
        headers, rows = _parse_markdown_table(content)
        if not headers or not rows:
            continue
        source_table_id = str(getattr(tbl, "id", "") or getattr(tbl, "table_id", "") or caption or "table")
        results.extend(
            _scan_key_value_rows(
                headers,
                rows,
                caption=caption,
                page=getattr(tbl, "page", None),
                source_table_id=source_table_id,
            )
        )
        category_columns, adsorbate_col, catalyst_col = _infer_table_columns(headers, caption)
        explicit_adsorbate_col = (
            adsorbate_col is not None
            and adsorbate_col < len(headers)
            and _is_explicit_adsorbate_header(headers[adsorbate_col])
        )
        results.extend(_scan_metric_rows(headers, rows, caption, getattr(tbl, "page", None)))
        if not category_columns:
            continue
        for row_index, row in enumerate(rows):
            row_text = " | ".join(row)
            adsorbate = None
            catalyst_name = None
            if adsorbate_col is not None and adsorbate_col < len(row):
                raw_adsorbate = row[adsorbate_col].strip()
                adsorbate = _resolve_adsorbate(raw_adsorbate)
                if explicit_adsorbate_col and adsorbate is None and _looks_like_safe_table_label(raw_adsorbate):
                    adsorbate = raw_adsorbate or None
            if catalyst_col is not None and catalyst_col < len(row):
                raw_catalyst = row[catalyst_col].strip()
                if _looks_like_safe_table_label(raw_catalyst):
                    catalyst_name = raw_catalyst
            for col_idx, (category, default_unit) in category_columns.items():
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip()
                if not cell:
                    continue
                # M4 修复：支持科学计数法（1.5 × 10^3, 2.3e-4, 1.5×10³ 等）
                cell = _normalize_numeric_text(cell)
                value_match = _first_numeric_match(cell)
                if value_match and _is_supplement_label_number(cell, value_match):
                    value_match = None
                if category in NUMERIC_CATEGORIES and not value_match:
                    continue
                value = None if category in TEXT_SCALAR_CATEGORIES else (_parse_numeric_value(value_match.group(0)) if value_match else None)
                unit = _unit_from_cell(cell, default_unit, category)
                header = headers[col_idx]
                reaction_step = _row_reaction_step(row, headers, category, header)
                quality_evidence = _normalize_chem_label(f"{caption}; {header}: {cell}; row: {row_text}")
                evidence = f"{header}: {cell}; row: {row_text}"
                header_adsorbate = _adsorbate_from_adsorption_header(header)
                if category == "adsorption_energy":
                    adsorbate_value = header_adsorbate or adsorbate or _resolve_adsorbate(evidence)
                elif _has_graphite_defect_context(f"{caption} {row_text}"):
                    adsorbate_value = adsorbate or _resolve_adsorbate(evidence)
                elif _category_should_default_null_adsorbate(category):
                    adsorbate_value = header_adsorbate or (
                        adsorbate if (explicit_adsorbate_col or category == "formation_energy") else None
                    )
                else:
                    adsorbate_value = header_adsorbate or (adsorbate if explicit_adsorbate_col else None) or _resolve_adsorbate(evidence)
                if _category_semantic_conflict(category, quality_evidence, value, unit):
                    continue
                if not _should_keep_result(category, adsorbate_value, value, quality_evidence):
                    continue
                atom_pair = _category_atom_pair(category, f"{header} {row_text}")
                site_label = row[0].strip() if row and _looks_like_safe_table_label(row[0]) else None
                results.append(
                    DFTResultItem(
                        category=category,
                        catalyst_name=catalyst_name,
                        adsorbate=adsorbate_value,
                        value=value,
                        unit=unit,
                        reaction_step=reaction_step,
                        evidence_text=evidence[:450],
                        source_location=SourceLocation(
                            table=caption[:80] if caption else "Table",
                            page=getattr(tbl, "page", None),
                        ),
                        confidence=0.82 if value is not None else 0.6,
                        fact_family=_fact_family_for_table_cell(category, caption, header, row_text),
                        atom_pair=atom_pair,
                        site_label=site_label,
                        state_context=reaction_step,
                        active_site_instance_key="|".join(part for part in [catalyst_name, site_label] if part) or None,
                        source_table_id=source_table_id,
                        source_table_caption=caption or None,
                        source_row_index=row_index,
                        source_column_index=col_idx,
                        raw_row_text=row_text,
                        raw_column_header=header,
                    )
                )
    return results


def _normalize_metric_label(label: str) -> str:
    return re.sub(r"[\s_\-()]+", "", (label or "").lower())


def _category_from_metric_label(label: str) -> str | None:
    compact = _normalize_metric_label(label)
    lowered = (label or "").lower()
    if compact in {"ul", "uₗ"} or "limiting potential" in lowered:
        return "limiting_potential"
    if compact in {"η", "eta"} or "overpotential" in lowered:
        return "overpotential"
    if compact == "pds" or "potential-determining" in lowered or "potential determining" in lowered:
        return "potential_determining_step"
    return None


def _looks_like_table_section_label(row: list[str]) -> str | None:
    non_empty = [cell for cell in row if cell.strip()]
    if len(non_empty) != 1:
        return None
    label = non_empty[0].strip()
    if re.search(r"\b(?:Fe|Co|Ni|Mn|Cu|TM)\s*[-–]?\s*N\s*\d\s*[-–]?\s*C\b", label, re.IGNORECASE):
        return label
    return None


def _scan_metric_rows(headers: list[str], rows: list[list[str]], caption: str, page: int | None) -> list[DFTResultItem]:
    results: list[DFTResultItem] = []
    current_group: str | None = None
    for row_index, row in enumerate(rows):
        row = [cell.strip() for cell in row]
        section_label = _looks_like_table_section_label(row)
        if section_label:
            current_group = section_label
            continue
        if not row:
            continue
        category = _category_from_metric_label(row[0])
        if not category:
            continue
        row_text = " | ".join(row)
        for col_idx, cell in enumerate(row[1:], start=1):
            cell = cell.strip()
            if not cell:
                continue
            header = headers[col_idx] if col_idx < len(headers) else f"column {col_idx + 1}"
            context = " / ".join(part for part in [current_group, header] if part)
            evidence = f"{caption}; {context}; row: {row_text}" if caption else f"{context}; row: {row_text}"
            if category == "potential_determining_step":
                continue
            # M4 修复：支持科学计数法
            value_match = re.search(
                r"(?:"
                r"[-+]?\d+(?:\.\d+)?\s*[×x·]\s*10\^[-+]?\d+"
                r"|[-+]?\d+(?:\.\d+)?[eE][-+]?\d+"
                r"|[-+]?\d*\.?\d+"
                r")",
                cell,
            )
            if value_match and _is_supplement_label_number(cell, value_match):
                continue
            if not value_match:
                continue
            unit_match = re.search(r"(V|eV|meV)", cell, re.IGNORECASE)
            raw_unit = unit_match.group(1).strip() if unit_match else ("V" if category in {"limiting_potential", "overpotential"} else None)
            unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
            results.append(
                DFTResultItem(
                    category=category,
                    adsorbate=_resolve_adsorbate(evidence),
                    value=_parse_numeric_value(value_match.group(0)),
                    unit=unit,
                    reaction_step=context,
                    evidence_text=evidence[:450],
                    source_location=SourceLocation(table=caption[:80] if caption else "Table", page=page),
                    confidence=0.86,
                    fact_family=_fact_family_for_category(category),
                    atom_pair=_category_atom_pair(category, f"{header} {row_text}"),
                    site_label=current_group,
                    state_context=header,
                    active_site_instance_key=current_group,
                    source_table_id=caption or "metric_table",
                    source_table_caption=caption or None,
                    source_row_index=row_index,
                    source_column_index=col_idx,
                    raw_row_text=row_text,
                    raw_column_header=header,
                )
            )
    return results


def _scan_graphene_defect_inline_tables(text: str) -> list[DFTResultItem]:
    results: list[DFTResultItem] = []
    if not text:
        return results
    normalized = _normalize_numeric_text(text)
    number = r"[-+]?\d+\s*\.\s*\d+(?:\(\d+\))?"
    targets = [
        ("single_vacancy", "MV"),
        ("silicon_substitution", "SiS"),
        ("Stone-Wales", "SW"),
    ]

    row_pattern = re.compile(
        rf"(Method\s+Defect\s+formation\s+energy\s*\(eV\)\s+MV\s+SiS\s+SW.*?)"
        rf"DMC-corrected\s+DFT\s+({number})\s+({number})\s+({number})",
        re.IGNORECASE | re.DOTALL,
    )
    for match in row_pattern.finditer(normalized):
        evidence = re.sub(r"\s+", " ", match.group(0)).strip()[:500]
        for index, (adsorbate, label) in enumerate(targets, start=2):
            try:
                value = _parse_uncertainty_float(match.group(index))
            except (TypeError, ValueError):
                continue
            results.append(
                DFTResultItem(
                    category="formation_energy",
                    adsorbate=adsorbate,
                    value=value,
                    unit="eV",
                    reaction_step=f"DMC-corrected DFT {label}",
                    evidence_text=evidence,
                    source_location=SourceLocation(section="inline defect formation energy table"),
                    confidence=0.9,
                )
            )

    sentence_pattern = re.compile(
        rf"vibrationally\s+corrected\s+DMC\s+defect\s+formation\s+energies\s+are\s+"
        rf"({number})\s*,\s*({number})\s*,\s*(?:and\s+)?({number})\s+at\s+298\s*K\s+for\s+MV,\s*SiS,\s+and\s+SW",
        re.IGNORECASE,
    )
    for match in sentence_pattern.finditer(normalized):
        evidence = _extract_sentence_around_match(normalized, match.start(), match.end())[:500]
        for index, (adsorbate, label) in enumerate(targets, start=1):
            try:
                value = _parse_uncertainty_float(match.group(index))
            except (TypeError, ValueError):
                continue
            results.append(
                DFTResultItem(
                    category="formation_energy",
                    adsorbate=adsorbate,
                    value=value,
                    unit="eV",
                    reaction_step=f"vibrationally corrected DMC at 298 K {label}",
                    evidence_text=evidence,
                    source_location=SourceLocation(section="inline defect formation energy sentence"),
                    confidence=0.88,
                )
            )
    return results


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class DFTResultsExtractor:
    """基于规则 + 启发式的 DFT 结果抽取器 (MVP)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.categories = list(CATEGORY_RULES.keys())
        self.settings = settings
        self.llm = LLMService(settings) if settings else None

    @staticmethod
    def _coerce_input(unified_document: Any) -> Any:
        """兼容对象、字典和列表输入."""
        if isinstance(unified_document, list):
            return type("_NS", (), {"sections": unified_document,
                                      "tables": [], "figures": [], "abstract": "",
                                      "markdown": ""})()
        if isinstance(unified_document, dict):
            ns = type("_NS", (),
                       {"sections": unified_document.get("sections", []),
                        "tables": unified_document.get("tables", []),
                        "figures": unified_document.get("figures", []),
                        "abstract": unified_document.get("abstract", ""),
                        "markdown": unified_document.get("markdown", ""),
                        **{k: v for k, v in unified_document.items()
                           if k not in ("sections", "tables", "figures",
                                        "abstract", "markdown")}})()
            return ns
        return unified_document

    # -- 公共接口 ----------------------------------------------------------

    def extract(self, unified_document: Any) -> list[dict]:
        """从 UnifiedPaperDocument 抽取结构化 DFT 结果."""
        doc = self._coerce_input(unified_document)
        markdown = getattr(doc, "markdown", "") or ""

        # Fallback to rules
        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        figures = getattr(doc, "figures", []) or []
        abstract = getattr(doc, "abstract", "") or ""
        markdown_tables = _extract_markdown_table_blocks(markdown)
        table_sources = [*tables, *markdown_tables]

        logger.info("Running rule-based DFT extraction")
        all_results: list[DFTResultItem] = []

        full_text_parts: list[str] = []
        if abstract:
            full_text_parts.append(abstract)
            
        sec_text_map: dict[int, tuple[str, int | None]] = {}
        offset = len(abstract) + 2 if abstract else 0
        
        for sec in sections:
            txt = getattr(sec, "text", "") or ""
            title = getattr(sec, "section_title", "") or None
            ps = getattr(sec, "page_start", None)
            if txt:
                sec_text_map[offset] = (title, ps)
                full_text_parts.append(txt)
                offset += len(txt) + 2  # +2 for the '\n\n' from join
        if markdown and not sections:
            sec_text_map[offset] = ("markdown", None)
            full_text_parts.append(markdown)
            
        full_text = _normalize_numeric_text("\n\n".join(full_text_parts))

        for cat in self.categories:
            if cat in TABLE_ONLY_NUMERIC_CATEGORIES:
                continue
            all_results.extend(self._scan_text(full_text, cat, sec_text_map, sections))
        all_results.extend(_scan_graphene_defect_inline_tables(full_text))
        all_results.extend(_scan_structured_tables(table_sources))
        for cat in self.categories:
            all_results.extend(_scan_tables_for_category(table_sources, cat))
        all_results.extend(self._scan_figure_captions(figures))

        if self.llm and self.llm.is_configured() and (markdown or abstract or sections):
            logger.info("Running hybrid LLM DFT extraction")
            system_prompt = (
                "You are an expert materials science data extractor.\n"
                "Extract all explicit DFT and first-principles calculation results for computational materials papers, "
                "including graphdiyne/graphyne systems, single/dual-atom catalysts (SAC/DAC), and Li-S battery applications.\n"
                "Categories: adsorption_energy, formation_energy, gibbs_free_energy_change, reaction_barrier, migration_barrier, "
                "li2s_decomposition_barrier, li2s_nucleation_barrier, li_s_bond_length, bader_charge, charge_transfer, d_band_center, "
                "band_gap, work_function, magnetic_moment, activation_energy, binding_energy, cohesive_energy, fluorination_energy, "
                "permeation_barrier, lattice_constant, interlayer_distance, pore_diameter, permeance, "
                "adsorption_molecule_fraction, young_modulus, seebeck_coefficient, zt, electrical_conductance, "
                "thermal_conductance, thermal_conductivity, carrier_mobility, optical_absorption_peak, dos_claim, "
                "charge_density_difference_claim.\n"
                "Only return claims that are directly supported by the provided text or tables.\n"
                "Every result must preserve its paper-local catalyst/material/model identity, active-site context, and "
                "structure/configuration context when explicitly stated. Leave these fields null rather than guessing. "
                "Do not merge equal values from different catalysts, active sites, structures, or DFT settings.\n"
                "Do not infer values from images, plots, graphical symbols, or figure-only content.\n"
                "For numeric categories, keep the exact value and unit from the paper; do not infer missing numbers."
            )
            text_to_process = self._build_focus_text(doc)
            try:
                llm_output = self.llm.structured_extract(system_prompt, text_to_process, DFTResultListModel)
                if llm_output and llm_output.results:
                    all_results.extend(self._from_llm_items(llm_output.results))
            except Exception as e:
                logger.warning(f"LLM extraction failed, keeping rule-based DFT results: {e}")

        all_results = self._deduplicate(all_results)
        return [self._item_to_dict(r) for r in all_results]

    def _build_focus_text(self, doc: Any, max_chars: int = 40000) -> str:
        abstract = getattr(doc, "abstract", "") or ""
        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        markdown = getattr(doc, "markdown", "") or ""
        section_regex = re.compile(
            r"(comput|dft|first.princip|theor|result|discuss|mechan|electronic|dos|band|adsor|free energy|barrier|migration|formation|vacancy|defect|graphene|graphite|graphdiyne|graphyne|gdy|thermoelectric|optical|lattice|cohesive|binding|permeation|bader|charge)",
            re.IGNORECASE,
        )
        parts: list[str] = []
        if abstract:
            parts.append("## Abstract\n" + abstract[:4000])
        for sec in sections:
            title = getattr(sec, "section_title", "") or ""
            text = getattr(sec, "text", "") or ""
            if not text:
                continue
            if section_regex.search(title) or section_regex.search(text[:1200]):
                parts.append(f"## Section: {title or 'Untitled'}\n{text[:6000]}")
        for tbl in tables[:12]:
            caption = getattr(tbl, "caption", "") or "Table"
            content = getattr(tbl, "markdown_content", "") or ""
            if content or caption:
                parts.append(f"## Table: {caption}\n{content[:3000]}")
        if not parts and markdown:
            parts.append(markdown[:max_chars])
        combined = "\n\n".join(parts)
        return combined[:max_chars]

    def _from_llm_items(self, items: list[DFTResultItemModel]) -> list[DFTResultItem]:
        normalized: list[DFTResultItem] = []
        for item in items:
            payload = item.model_dump()
            clean = self._normalize_result_dict(payload)
            if not clean:
                continue
            location = clean.get("source_location") or {}
            normalized.append(
                DFTResultItem(
                    category=clean["category"],
                    catalyst_name=clean.get("catalyst_name"),
                    active_site_context=clean.get("active_site_context"),
                    structure_context=clean.get("structure_context"),
                    adsorbate=clean.get("adsorbate"),
                    value=clean.get("value"),
                    unit=clean.get("unit"),
                    reaction_step=clean.get("reaction_step"),
                    evidence_text=clean["evidence_text"],
                    source_location=SourceLocation(
                        section=location.get("section"),
                        page=location.get("page"),
                        figure=location.get("figure"),
                        table=location.get("table"),
                    ),
                    confidence=clean.get("confidence", 0.6),
                )
            )
        return normalized

    def _normalize_result_dict(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        category = str(payload.get("category") or "").strip()
        if category not in self.categories:
            return None
        evidence = str(payload.get("evidence_text") or "").strip()
        if not evidence:
            return None
        value = payload.get("value")
        if category in NUMERIC_CATEGORIES and value is None:
            return None
        unit = payload.get("unit")
        if isinstance(unit, str):
            raw_unit = unit.strip()
            unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
        adsorbate = payload.get("adsorbate")
        if not adsorbate:
            adsorbate = _resolve_adsorbate(evidence)
        if not _should_keep_result(category, adsorbate, value, evidence):
            return None
        confidence = payload.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else 0.6
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.0, min(confidence, 1.0))
        source_location = payload.get("source_location") or {}
        if not isinstance(source_location, dict):
            source_location = {}
        return {
            "category": category,
            "catalyst_name": payload.get("catalyst_name"),
            "active_site_context": payload.get("active_site_context"),
            "structure_context": payload.get("structure_context"),
            "adsorbate": adsorbate,
            "value": value,
            "unit": unit,
            "evidence_text": evidence[:500],
            "source_location": {
                "section": source_location.get("section"),
                "page": source_location.get("page"),
                "figure": source_location.get("figure"),
                "table": source_location.get("table"),
            },
            "reaction_step": payload.get("reaction_step"),
            "confidence": confidence,
        }

    # -- 内部方法 ----------------------------------------------------------

    def _scan_text(
        self,
        text: str,
        category: str,
        sec_map: dict[int, tuple[str, int | None]],
        sections: list[Any],
    ) -> list[DFTResultItem]:
        results: list[DFTResultItem] = []
        text = _normalize_numeric_text(text)
        patterns = CATEGORY_RULES.get(category, [])
        for pat_tuple in patterns:
            if isinstance(pat_tuple, tuple):
                pattern, vg, ug = pat_tuple
            else:
                pattern, vg, ug = pat_tuple, 1, 2
            for m in re.finditer(pattern, text, re.IGNORECASE):
                # M3 修复：对所有类别做跨句检测，防止 .{0,80} 宽泛量词跨句误匹配
                # 例外：dos_claim / charge_density_difference_claim 是声明型类别，
                # 匹配文本天然跨越多句，不做跨句过滤
                if category not in NON_NUMERIC_DFT_CLAIM_CATEGORIES and _match_crosses_sentence(m.group(0)):
                    continue
                try:
                    val = _parse_match_float(m, vg) if vg else None
                    raw_unit_value = m.group(ug) if ug and ug < len(m.groups()) + 1 else None
                    raw_unit = raw_unit_value.strip() if isinstance(raw_unit_value, str) else None
                    unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
                except (ValueError, IndexError):
                    val, unit = None, None

                # 推断位置
                pos = m.start()
                best_sec, best_page = None, None
                for off, (sec_title, ps) in sec_map.items():
                    if off <= pos:
                        best_sec, best_page = sec_title, ps
                    else:
                        break
                loc = SourceLocation(section=best_sec, page=best_page)

                evidence = _extract_context_around_match(text, m.start(), m.end())
                local_evidence = _extract_sentence_around_match(text, m.start(), m.end())
                adsorbate = _resolve_adsorbate(m.group(0)) or _resolve_adsorbate(local_evidence)
                if (
                    not adsorbate
                    and category not in {"adsorption_energy", "binding_energy"}
                    and not _category_should_default_null_adsorbate(category)
                ):
                    adsorbate = _resolve_adsorbate(evidence)
                if not _should_keep_result(category, adsorbate, val, local_evidence or evidence):
                    continue
                results.append(DFTResultItem(
                    category=category,
                    adsorbate=adsorbate,
                    value=val,
                    unit=unit,
                    evidence_text=evidence,
                    source_location=loc,
                    confidence=self._calc_confidence(val, unit, evidence, category),
                    fact_family=_fact_family_for_category(category),
                    atom_pair=_category_atom_pair(category, evidence),
                ))
        return results

    def _scan_figure_captions(self, figures: list[Any]) -> list[DFTResultItem]:
        """图注也是高价值的数据源."""
        results: list[DFTResultItem] = []
        for fig in figures:
            cap = _normalize_numeric_text(getattr(fig, "caption", "") or "")
            if not cap:
                continue
            figure_had_result = False
            for cat, patterns in CATEGORY_RULES.items():
                if cat in TABLE_ONLY_NUMERIC_CATEGORIES:
                    continue
                for pat_tuple in patterns:
                    if isinstance(pat_tuple, tuple):
                        pattern, vg, ug = pat_tuple
                    else:
                        pattern, vg, ug = pat_tuple, 1, 2
                    for m in re.finditer(pattern, cap, re.IGNORECASE):
                        try:
                            val = _parse_match_float(m, vg) if vg else None
                            raw_unit_value = m.group(ug) if ug and ug < len(m.groups()) + 1 else None
                            raw_unit = raw_unit_value.strip() if isinstance(raw_unit_value, str) else None
                            unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
                        except (ValueError, IndexError):
                            val, unit = None, None
                        loc = SourceLocation(
                            figure=cap[:100],
                            page=getattr(fig, "page", None),
                        )
                        evidence = _extract_context_around_match(cap, m.start(), m.end())
                        adsorbate = _resolve_adsorbate(m.group(0)) or _resolve_adsorbate(evidence)
                        if not _should_keep_result(cat, adsorbate, val, evidence):
                            continue
                        figure_had_result = True
                        results.append(DFTResultItem(
                            category=cat,
                            adsorbate=adsorbate,
                            value=val,
                            unit=unit,
                            evidence_text=evidence,
                            source_location=loc,
                            confidence=0.7,
                            fact_family=_fact_family_for_category(cat),
                            atom_pair=_category_atom_pair(cat, evidence),
                        ))
            if not figure_had_result and re.search(
                r"(?:DFT|adsorption\s+energy|free\s+energy|reaction\s+barrier|DOS|Bader|COHP|bond\s+length)",
                cap,
                re.IGNORECASE,
            ):
                results.append(
                    DFTResultItem(
                        category="ambiguous_record",
                        evidence_text=cap[:450],
                        source_location=SourceLocation(figure=cap[:100], page=getattr(fig, "page", None)),
                        confidence=0.25,
                        fact_family="ambiguous_record",
                    )
                )
        return results

    @staticmethod
    def _calc_confidence(val: float | None, unit: str | None, evidence: str, cat: str) -> float:
        """启发式置信度评分."""
        score = 0.3
        if val is not None:
            score += 0.25
        if unit:
            score += 0.15
        if len(evidence) > 50:
            score += 0.1
        # 某些类别在正文出现时置信度更高
        if cat in ("adsorption_energy", "reaction_barrier", "gibbs_free_energy_change"):
            score += 0.1
        return min(score, 1.0)

    @staticmethod
    def _deduplicate(items: list[DFTResultItem]) -> list[DFTResultItem]:
        """简单去重：保留置信度最高的."""
        seen_keys: dict[str, DFTResultItem] = {}
        for item in items:
            evidence_key = re.sub(r"\s+", " ", (item.evidence_text or "").lower()).strip()[:180]
            key = (
                f"{item.catalyst_name or ''}:{item.active_site_context or ''}:{item.structure_context or ''}:"
                f"{item.category}:{item.value}:{item.unit or ''}:{item.adsorbate or ''}:"
                f"{item.reaction_step or ''}:{evidence_key}"
            )
            if key not in seen_keys or item.confidence > seen_keys[key].confidence:
                seen_keys[key] = item
        return list(seen_keys.values())

    @staticmethod
    def _item_to_dict(item: DFTResultItem) -> dict:
        payload_fields = {
            key: value
            for key, value in _evidence_payload_fields(item).items()
            if value not in (None, "", [], {})
        }
        return {
            "category": item.category,
            "catalyst_name": item.catalyst_name,
            "active_site_context": item.active_site_context,
            "structure_context": item.structure_context,
            "adsorbate": item.adsorbate,
            "value": item.value,
            "unit": item.unit,
            "evidence_text": item.evidence_text,
            "source_location": {
                "section": item.source_location.section,
                "page": item.source_location.page,
                "figure": item.source_location.figure,
                "table": item.source_location.table,
            },
            "reaction_step": item.reaction_step,
            "confidence": round(item.confidence, 2),
            **payload_fields,
            "evidence_payload": payload_fields or None,
        }
