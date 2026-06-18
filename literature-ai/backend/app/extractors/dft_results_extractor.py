"""DFT 计算结果抽取器 — Stage 2 MVP (规则+启发式，无大模型依赖).

输入: UnifiedPaperDocument
输出: list[DFTResultItem]  (结构化 DFT 结果)
"""

from __future__ import annotations

import logging
import re
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
class DFTResultItem:
    category: str  # e.g. "adsorption_energy", "reaction_barrier"
    adsorbate: str | None = None  # e.g. "Li2S4", "Li2S", None for generic
    value: float | None = None
    unit: str | None = None
    reaction_step: str | None = None
    evidence_text: str = ""
    source_location: SourceLocation = field(default_factory=SourceLocation)
    confidence: float = 0.5  # 0.0 ~ 1.0


class SourceLocationModel(BaseModel):
    section: str | None = None
    page: int | None = None
    figure: str | None = None
    table: str | None = None

class DFTResultItemModel(BaseModel):
    category: str = Field(..., description="Category of DFT result (e.g., adsorption_energy, bader_charge, reaction_barrier)")
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
    "sulfur": "S8",
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
    "μb": "μB",
    "mub": "μB",
    "mu_b": "μB",
    "bohr magneton": "μB",
}

TABLE_HEADER_CATEGORY_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"(adsorption|binding).*(energy|e[_\-\s]*ads|e[_\-\s]*bind)|(^e[_\-\s]*ads(?:\s*\(.*\))?$)|(^e[_\-\s]*bind(?:\s*\(.*\))?$)", re.IGNORECASE), "adsorption_energy", "eV"),
    (re.compile(r"(delta\s*g|gibbs|free energy|^dg$|^Δg$)", re.IGNORECASE), "gibbs_free_energy_change", "eV"),
    (re.compile(r"(barrier|activation|^ea$|energy barrier)", re.IGNORECASE), "reaction_barrier", "eV"),
    (re.compile(r"(bader).*(charge)|(^bader$)", re.IGNORECASE), "bader_charge", "e"),
    (re.compile(r"(charge transfer|electron transfer)", re.IGNORECASE), "charge_transfer", "e"),
    (re.compile(r"(d-?band|epsilon[_\-\s]*d|ε[_\-\s]*d)", re.IGNORECASE), "d_band_center", "eV"),
    (re.compile(r"(limiting\s+potential|^u[_\-\s]*l$|u\s*l)", re.IGNORECASE), "limiting_potential", "V"),
    (re.compile(r"(overpotential|η|eta)", re.IGNORECASE), "overpotential", "V"),
]

NUMERIC_CATEGORIES = {
    "adsorption_energy",
    "activation_energy",
    "binding_energy",
    "cohesive_energy",
    "formation_energy",
    "fluorination_energy",
    "gibbs_free_energy_change",
    "reaction_barrier",
    "migration_barrier",
    "permeation_barrier",
    "li2s_decomposition_barrier",
    "li2s_nucleation_barrier",
    "bader_charge",
    "charge_transfer",
    "d_band_center",
    "band_gap",
    "work_function",
    "magnetic_moment",
    "limiting_potential",
    "overpotential",
    "lattice_constant",
    "interlayer_distance",
    "pore_diameter",
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
}
TABLE_ONLY_NUMERIC_CATEGORIES = {"limiting_potential", "overpotential"}
NON_NUMERIC_DFT_CLAIM_CATEGORIES = {"dos_claim", "charge_density_difference_claim"}

# 类别 → 正则模式列表 (每个模式: (pattern, value_group, unit_group))
CATEGORY_RULES: dict[str, list[tuple[str, int, int]]] = {
    "adsorption_energy": [
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
        r"(?:reaction\s+)?(?:barrier|activation\s+energy|E_a).{0,80}?([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"E_a\s*[=\u2248]\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
        r"E\s*[_\-\s]?\s*a\s*[=＝\u2248]\s*([\-\+]?\d+[.]?\d*)\s*(eV|meV|kJ/mol|kcal/mol)",
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
    "bader_charge": [
        r"(?:Bader\s+?(?:charge|analysis)).{0,80}?([\-\+]?\d+[.]?\d*)\s*(e[\u2212-]|e)",
        r"Bader.{0,40}?charge\s*(?:of|transfer|gain|loss).{0,20}?([\-\+]?\d+[.]?\d*)",
        ],
    "charge_transfer": [
        r"(?:charge\s+transfer(?:red)?|(?:electron|e[\u2212-])\s+transfer).{0,60}?([\-\+]?\d+[.]?\d*)\s*(e[\u2212-]|e|electrons?)",
        r"(?:transfers?|gains?|loss?).{0,20}?([\-\+]?\d+[.]?\d+)\s*(?:e[\u2212-]?|electrons?)",
        r"Mulliken.{0,30}?([\-\+]?\d+[.]?\d*)\s*e[\u2212-]",
        ],
    "d_band_center": [
        r"(?:d-?band\s+center|\u03b5_d|epsilon_d).{0,40}?([-\+]?\d+[.]?\d*)\s*(eV|meV)",
        ],
    "limiting_potential": [
        r"(?:limiting\s+potential|U\s*[_\-\s]?\s*L|U\s*L).{0,80}?([\-\+]?\d+[.]?\d*)\s*(V|eV)",
        r"([\-\+]?\d+[.]?\d*)\s*(V|eV).{0,60}(?:limiting\s+potential|U\s*[_\-\s]?\s*L)",
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
    "binding_energy": [],
    "cohesive_energy": [],
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
        (re.compile(r"(defect\s*)?formation\s+energ|(^e[_\-\s]*f$)|(^e[_\-\s]*form)", re.IGNORECASE), "formation_energy", "eV"),
        (re.compile(r"(migration|diffusion).*(barrier|energy)|(^e[_\-\s]*m$)", re.IGNORECASE), "migration_barrier", "eV"),
        (re.compile(r"(band\s*gap|e[_\-\s]*g)", re.IGNORECASE), "band_gap", "eV"),
        (re.compile(r"(work\s*function|^wf$)", re.IGNORECASE), "work_function", "eV"),
        (re.compile(r"(magnetic\s*moment|magnetization|spin\s*moment)", re.IGNORECASE), "magnetic_moment", "μB"),
    ]
)


def _resolve_adsorbate(text: str) -> str | None:
    """从文本中推断吸附质."""
    normalized = re.sub(r"\s+", " ", (text or "").lower())
    for key in sorted(ADSORBATE_MAP, key=len, reverse=True):
        name = ADSORBATE_MAP[key]
        escaped = re.escape(key.lower()).replace(r"\ ", r"[\s\-]+")
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", normalized):
            return name
    return None


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
    text = re.sub(r"\s+", " ", str(value or "")).strip()
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
        if re.search(r"\[\s*\d+\s*\]", evidence):
            return False
        if value is not None and abs(value) > 20:
            return False
        if re.search(r"\b\d{4}\b", lowered) and not re.search(r"\b(?:0|1|2|3|4|5)\.\d+\s*(?:v|ev)\b", lowered):
            return False
    if category == "adsorption_energy" and not adsorbate:
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
            r"\b(underestimat|overestimat|disagreement|difference|deviation|margin|"
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
        if category in {"limiting_potential", "overpotential"} and _parse_markdown_table(content)[1]:
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
                    raw_unit = m.group(ug).strip() if ug and ug < len(m.groups()) + 1 else None
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


def _infer_table_columns(headers: list[str]) -> tuple[dict[int, tuple[str, str | None]], int | None]:
    category_columns: dict[int, tuple[str, str | None]] = {}
    adsorbate_col: int | None = None
    for idx, header in enumerate(headers):
        header_text = re.sub(r"\s+", " ", header or "").strip()
        lowered = header_text.lower()
        if adsorbate_col is None and re.search(r"(adsorbate|intermediate|species|molecule|state|slurry|lips|li2sx|sample)", lowered):
            adsorbate_col = idx
        if re.search(r"(migration|diffusion).*(barrier|energy)", lowered):
            category_columns[idx] = ("migration_barrier", "eV")
            continue
        for pattern, category, unit in TABLE_HEADER_CATEGORY_RULES:
            if pattern.search(header_text):
                category_columns[idx] = (category, unit)
                break
    if adsorbate_col is None and headers:
        first_header = headers[0].lower()
        if not any(pattern.search(first_header) for pattern, _, _ in TABLE_HEADER_CATEGORY_RULES):
            adsorbate_col = 0
    return category_columns, adsorbate_col


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
        category_columns, adsorbate_col = _infer_table_columns(headers)
        results.extend(_scan_metric_rows(headers, rows, caption, getattr(tbl, "page", None)))
        if not category_columns:
            continue
        for row in rows:
            row_text = " | ".join(row)
            adsorbate = None
            if adsorbate_col is not None and adsorbate_col < len(row):
                raw_adsorbate = row[adsorbate_col].strip()
                adsorbate = _resolve_adsorbate(raw_adsorbate)
                if adsorbate is None and _looks_like_safe_table_label(raw_adsorbate):
                    adsorbate = raw_adsorbate or None
            for col_idx, (category, default_unit) in category_columns.items():
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip()
                if not cell:
                    continue
                # M4 修复：支持科学计数法（1.5 × 10^3, 2.3e-4, 1.5×10³ 等）
                cell = _normalize_numeric_text(cell)
                value_match = re.search(
                    r"(?:"
                    r"[-+]?\d+(?:\.\d+)?\s*[×x·]\s*10\^[-+]?\d+"  # 1.5 × 10^3
                    r"|[-+]?\d+(?:\.\d+)?[eE][-+]?\d+"              # 1.5e3
                    r"|[-+]?\d*\.?\d+"                               # 普通小数
                    r")",
                    cell,
                )
                if category in NUMERIC_CATEGORIES and not value_match:
                    continue
                value = _parse_numeric_value(value_match.group(0)) if value_match else None
                unit_match = re.search(r"(eV|meV|kJ/mol|kcal/mol|e[\u2212-]?|electrons?)", cell, re.IGNORECASE)
                unit = None
                if unit_match:
                    raw_unit = unit_match.group(1).strip()
                    unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit)
                elif default_unit:
                    unit = default_unit
                header = headers[col_idx]
                evidence = f"{header}: {cell}; row: {row_text}"
                adsorbate_value = _resolve_adsorbate(row_text) or adsorbate or _resolve_adsorbate(evidence)
                if _category_semantic_conflict(category, evidence, value, unit):
                    continue
                if not _should_keep_result(category, adsorbate_value, value, evidence):
                    continue
                results.append(
                    DFTResultItem(
                        category=category,
                        adsorbate=adsorbate_value,
                        value=value,
                        unit=unit,
                        reaction_step=header,
                        evidence_text=evidence[:450],
                        source_location=SourceLocation(
                            table=caption[:80] if caption else "Table",
                            page=getattr(tbl, "page", None),
                        ),
                        confidence=0.82 if value is not None else 0.6,
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
    for row in rows:
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
                results.append(
                    DFTResultItem(
                        category=category,
                adsorbate=_resolve_adsorbate(cell) or _resolve_adsorbate(evidence),
                        value=None,
                        unit=None,
                        reaction_step=(context + ": " + cell) if context else cell,
                        evidence_text=evidence[:450],
                        source_location=SourceLocation(table=caption[:80] if caption else "Table", page=page),
                        confidence=0.78,
                    )
                )
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
        all_results.extend(_scan_structured_tables(tables))
        for cat in self.categories:
            all_results.extend(_scan_tables_for_category(tables, cat))
        all_results.extend(self._scan_figure_captions(figures))

        if self.llm and self.llm.is_configured() and (markdown or abstract or sections):
            logger.info("Running hybrid LLM DFT extraction")
            system_prompt = (
                "You are an expert materials science data extractor.\n"
                "Extract all explicit DFT and first-principles calculation results for computational materials papers, "
                "including graphdiyne/graphyne systems, single/dual-atom catalysts (SAC/DAC), and Li-S battery applications.\n"
                "Categories: adsorption_energy, formation_energy, gibbs_free_energy_change, reaction_barrier, migration_barrier, "
                "li2s_decomposition_barrier, li2s_nucleation_barrier, bader_charge, charge_transfer, d_band_center, "
                "band_gap, work_function, magnetic_moment, activation_energy, binding_energy, cohesive_energy, fluorination_energy, "
                "permeation_barrier, lattice_constant, interlayer_distance, pore_diameter, permeance, "
                "adsorption_molecule_fraction, young_modulus, seebeck_coefficient, zt, electrical_conductance, "
                "thermal_conductance, thermal_conductivity, carrier_mobility, optical_absorption_peak, dos_claim, "
                "charge_density_difference_claim.\n"
                "Only return claims that are directly supported by the provided text or tables.\n"
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
                    raw_unit = m.group(ug).strip() if ug and ug < len(m.groups()) + 1 else None
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
                adsorbate = _resolve_adsorbate(m.group(0)) or _resolve_adsorbate(local_evidence) or _resolve_adsorbate(evidence)
                if not _should_keep_result(category, adsorbate, val, evidence):
                    continue
                results.append(DFTResultItem(
                    category=category,
                    adsorbate=adsorbate,
                    value=val,
                    unit=unit,
                    evidence_text=evidence,
                    source_location=loc,
                    confidence=self._calc_confidence(val, unit, evidence, category),
                ))
        return results

    def _scan_figure_captions(self, figures: list[Any]) -> list[DFTResultItem]:
        """图注也是高价值的数据源."""
        results: list[DFTResultItem] = []
        for fig in figures:
            cap = _normalize_numeric_text(getattr(fig, "caption", "") or "")
            if not cap:
                continue
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
                            raw_unit = m.group(ug).strip() if ug and ug < len(m.groups()) + 1 else None
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
                        results.append(DFTResultItem(
                            category=cat,
                            adsorbate=adsorbate,
                            value=val,
                            unit=unit,
                            evidence_text=evidence,
                            source_location=loc,
                            confidence=0.7,
                        ))
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
            key = f"{item.category}:{item.value}:{item.unit or ''}:{item.adsorbate or ''}:{item.reaction_step or ''}:{evidence_key}"
            if key not in seen_keys or item.confidence > seen_keys[key].confidence:
                seen_keys[key] = item
        return list(seen_keys.values())

    @staticmethod
    def _item_to_dict(item: DFTResultItem) -> dict:
        return {
            "category": item.category,
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
        }
