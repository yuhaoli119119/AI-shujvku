from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Iterable

REGISTRY_VERSION = "figure-types-2026.09.16-v1"
@dataclass(frozen=True)
class FigureType:
    key: str
    name_zh: str
    aliases: tuple[str, ...]
    parent: str
_TYPES = (
    FigureType("free_energy_diagram", "自由能图", ("台阶图", "自由能台阶图", "反应能量图", "free energy diagram", "energy profile", "reaction energy diagram"), "computational"),
    FigureType("charge_discharge_profile", "充放电曲线", ("充放电曲线", "恒流充放电", "GCD", "galvanostatic charge discharge", "charge discharge profile", "充放电循环图"), "electrochemistry"),
    FigureType("cycling_performance", "循环性能", ("循环性能", "长循环", "循环稳定性", "cycling performance", "cycle life", "充放电循环图"), "electrochemistry"),
    FigureType("rate_performance", "倍率性能", ("倍率性能", "rate capability", "rate performance"), "electrochemistry"),
    FigureType("cyclic_voltammetry", "循环伏安", ("循环伏安", "CV", "cyclic voltammetry", "voltammogram"), "electrochemistry"),
    FigureType("electrochemical_impedance", "电化学阻抗", ("阻抗", "Nyquist", "EIS", "electrochemical impedance"), "electrochemistry"),
    FigureType("density_of_states", "态密度", ("DOS", "态密度", "density of states"), "computational"),
    FigureType("projected_density_of_states", "投影态密度", ("PDOS", "分波态密度", "投影态密度", "projected density of states"), "computational"),
    FigureType("band_structure", "能带结构", ("能带", "能带结构", "band structure"), "computational"),
    FigureType("charge_density_difference", "差分电荷密度", ("差分电荷密度", "电荷密度差", "charge density difference", "CDD"), "computational"),
    FigureType("adsorption_structure", "吸附结构", ("吸附结构", "吸附构型", "adsorption structure", "adsorption configuration"), "computational"),
    FigureType("reaction_mechanism", "反应机理", ("反应机理", "反应机制", "reaction mechanism", "mechanism"), "schematic"),
    FigureType("synthesis_workflow", "合成流程", ("合成流程", "制备流程", "synthesis workflow", "preparation process"), "schematic"),
    FigureType("structural_schematic", "结构示意图", ("结构示意图", "模型示意图", "structural schematic", "structure model"), "schematic"),
    FigureType("microscopy", "显微图", ("显微图", "电镜", "SEM", "TEM", "HRTEM", "microscopy"), "characterization"),
    FigureType("elemental_mapping", "元素分布图", ("元素分布图", "元素映射", "EDS mapping", "elemental mapping"), "characterization"),
    FigureType("xrd", "X射线衍射", ("XRD", "X射线衍射", "x-ray diffraction"), "spectroscopy"),
    FigureType("raman", "拉曼光谱", ("Raman", "拉曼", "拉曼光谱"), "spectroscopy"),
    FigureType("xps", "X射线光电子能谱", ("XPS", "X射线光电子能谱", "photoelectron spectroscopy"), "spectroscopy"),
    FigureType("xanes", "X射线吸收近边结构", ("XANES", "X射线吸收近边结构"), "spectroscopy"),
    FigureType("exafs", "扩展X射线吸收精细结构", ("EXAFS", "扩展X射线吸收精细结构"), "spectroscopy"),
    FigureType("morphology_characterization", "形貌表征", ("形貌表征", "morphology characterization", "morphology"), "characterization"),
    FigureType("performance_comparison", "性能对比", ("性能对比", "benchmark comparison", "performance comparison"), "comparison"),
    FigureType("other", "其他", ("other", "其他", "未归一化"), "other"),
)
REGISTRY = {item.key: item for item in _TYPES}
def _norm(value: str) -> str:
    return re.sub(r"[\s_\-/]+", "", str(value or "").strip().casefold())
ALIAS_INDEX: dict[str, set[str]] = {}
for item in _TYPES:
    for alias in (item.key, item.name_zh, *item.aliases):
        ALIAS_INDEX.setdefault(_norm(alias), set()).add(item.key)
def resolve_query(value: str) -> set[str]:
    query = _norm(value)
    if not query:
        return set()
    exact = ALIAS_INDEX.get(query)
    if exact:
        return set(exact)
    matches: set[str] = set()
    for alias, keys in ALIAS_INDEX.items():
        if query in alias or alias in query:
            matches.update(keys)
    return matches
def validate_keys(keys: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in keys:
        key = str(raw).strip()
        if key not in REGISTRY:
            raise ValueError(f"unknown_figure_type:{key}; use other and preserve raw_type_name")
        if key not in result:
            result.append(key)
    return result
def public_registry() -> dict:
    return {"version": REGISTRY_VERSION, "types": [{"key": x.key, "name_zh": x.name_zh, "aliases": list(x.aliases), "parent": x.parent} for x in _TYPES]}
def search_terms(keys: Iterable[str], raw_type_name: str | None = None) -> list[str]:
    terms: list[str] = []
    for key in keys:
        item = REGISTRY[key]
        terms.extend((item.key, item.name_zh, *item.aliases))
    if raw_type_name:
        terms.append(raw_type_name)
    return list(dict.fromkeys(str(x).strip() for x in terms if str(x).strip()))
