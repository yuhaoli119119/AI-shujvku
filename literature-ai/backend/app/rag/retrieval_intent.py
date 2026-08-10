from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


SUPPORTED_EVIDENCE_TYPES: tuple[str, ...] = (
    "sections",
    "catalyst_samples",
    "dft_results",
    "electrochemical_performance",
    "mechanism_claims",
    "writing_cards",
    "figure_cards",
    "figure_data_points",
)

_MODE_ALIASES = {
    "all": "comprehensive",
    "default": "narrative",
}
_SUPPORTED_MODES = {
    "narrative",
    "mechanism",
    "figure",
    "dft_quantitative",
    "electrochemical",
    "comparison",
    "comprehensive",
}

_MODE_EVIDENCE_TYPES: dict[str, tuple[str, ...]] = {
    "narrative": (
        "sections",
        "catalyst_samples",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
    ),
    "mechanism": (
        "sections",
        "catalyst_samples",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
    ),
    "figure": (
        "sections",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
        "figure_data_points",
    ),
    "dft_quantitative": (
        "sections",
        "catalyst_samples",
        "dft_results",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
    ),
    "electrochemical": (
        "sections",
        "catalyst_samples",
        "electrochemical_performance",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
        "figure_data_points",
    ),
    "comparison": (
        "sections",
        "catalyst_samples",
        "electrochemical_performance",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
        "figure_data_points",
    ),
    "comprehensive": SUPPORTED_EVIDENCE_TYPES,
}

_DFT_PATTERNS = (
    r"\bdft\b",
    r"\bdensity functional theor(?:y|ies)\b",
    r"\bfirst[\s-]?principles?\b",
    r"\bab[\s-]?initio\b",
    r"\belectronic structure\b",
    r"\bband structure\b",
    r"\b(?:p?dos|density of states)\b",
    r"\bcharge density difference\b",
    r"\bbader(?: charge)?\b",
    r"\bcharge transfer\b",
    r"\bcomput(?:ed|ational) propert(?:y|ies)\b",
    r"\b(?:adsorption|binding|activation) energ(?:y|ies)\b",
    r"\b(?:reaction|energy) barriers?\b",
    r"\bfree[\s-]?energy (?:diagram|profile|landscape)\b",
    r"\b(?:work function|band gap|d[\s-]?band cent(?:er|re))\b",
    r"\b(?:neb|cohp|elf)\b",
    r"\belectron localization function\b",
    r"密度泛函",
    r"第一性原理",
    r"电子结构",
    r"能带结构",
    r"(?:分波)?态密度",
    r"(?:电荷密度差|差分电荷)",
    r"电荷转移",
    r"(?:吸附能|结合能|活化能|反应能垒|能量势垒|自由能图|功函数|带隙|d带中心|电子局域函数|过渡态)",
    r"计算性质",
)
_ELECTROCHEMICAL_PATTERNS = (
    r"\belectrochemical\b",
    r"\b(?:specific )?capacity\b",
    r"\bcycling (?:performance|stability)\b",
    r"\brate performance\b",
    r"\bsulfur loading\b",
    r"\b(?:eis|cyclic voltammetry|tafel)\b",
    r"电化学",
    r"(?:比容量|循环性能|循环稳定性|倍率性能|硫载量)",
)
_COMPARISON_PATTERNS = (
    r"\bcompar(?:e|ed|ing|ison|ative)\b",
    r"\bversus\b",
    r"\bvs\.?\b",
    r"(?:比较|对比|差异)",
)
_MECHANISM_PATTERNS = (
    r"\bmechanis(?:m|tic)\b",
    r"\breaction pathway\b",
    r"\bredox pathway\b",
    r"(?:机理|机制|反应路径|作用路径)",
)
_FIGURE_PATTERNS = (
    r"\bfig(?:ure)?s?\.?\b",
    r"\b(?:plot|graph|image)s?\b",
    r"(?:关键图|图表|图像|插图)",
)


@dataclass(frozen=True)
class RetrievalIntent:
    intent: str
    mode: str
    selected_evidence_types: tuple[str, ...]
    dft_included: bool
    dft_included_reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "retrieval_intent": self.intent,
            "retrieval_mode": self.mode,
            "selected_evidence_types": list(self.selected_evidence_types),
            "dft_included": self.dft_included,
            "dft_included_reason": self.dft_included_reason,
        }


def route_retrieval_intent(
    query: str,
    *,
    evidence_types: Iterable[str] | None = None,
    mode: str | None = None,
    requested_sections: Iterable[str] | None = None,
) -> RetrievalIntent:
    """Build a deterministic retrieval plan without calling an LLM.

    An explicit non-empty ``evidence_types`` list is an authoritative allowlist.
    Otherwise the query, requested output sections, and explicit mode select a
    stable evidence-type set. DFT is never enabled by paper scope or fallback
    scoring.
    """

    normalized_mode = _normalize_mode(mode)
    explicit_types = _normalize_evidence_types(evidence_types)
    requested = _normalize_requested_sections(requested_sections)
    query_intent = _infer_query_intent(query)

    if explicit_types is not None:
        selected = explicit_types
        effective_mode = normalized_mode or query_intent
        intent = "dft_quantitative" if "dft_results" in selected else effective_mode
    else:
        effective_mode = normalized_mode or query_intent
        selected_set = set(_MODE_EVIDENCE_TYPES[effective_mode])
        if "dft_results" in requested:
            selected_set.add("dft_results")
        if query_intent == "dft_quantitative":
            selected_set.add("dft_results")
        selected = tuple(item for item in SUPPORTED_EVIDENCE_TYPES if item in selected_set)
        intent = (
            "dft_quantitative"
            if "dft_results" in selected and effective_mode != "comprehensive"
            else effective_mode
        )

    dft_included = "dft_results" in selected
    dft_reason = _dft_reason(
        dft_included=dft_included,
        explicit_types=explicit_types,
        normalized_mode=normalized_mode,
        requested_sections=requested,
        query_intent=query_intent,
    )
    return RetrievalIntent(
        intent=intent,
        mode=effective_mode,
        selected_evidence_types=selected,
        dft_included=dft_included,
        dft_included_reason=dft_reason,
    )


def _normalize_mode(mode: str | None) -> str | None:
    normalized = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    normalized = _MODE_ALIASES.get(normalized, normalized)
    if normalized not in _SUPPORTED_MODES:
        supported = ", ".join(sorted(_SUPPORTED_MODES | set(_MODE_ALIASES)))
        raise ValueError(f"Unsupported retrieval mode: {mode}. Expected one of: {supported}")
    return normalized


def _normalize_evidence_types(evidence_types: Iterable[str] | None) -> tuple[str, ...] | None:
    if evidence_types is None:
        return None
    requested = {
        str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
        for item in evidence_types
        if str(item or "").strip()
    }
    if not requested:
        return None
    invalid = sorted(requested - set(SUPPORTED_EVIDENCE_TYPES))
    if invalid:
        raise ValueError(f"Unsupported evidence_types: {invalid}")
    return tuple(item for item in SUPPORTED_EVIDENCE_TYPES if item in requested)


def _normalize_requested_sections(requested_sections: Iterable[str] | None) -> set[str]:
    return {
        str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
        for item in (requested_sections or [])
        if str(item or "").strip()
    }


def _infer_query_intent(query: str) -> str:
    text = str(query or "").strip().lower()
    if _matches(text, _DFT_PATTERNS):
        return "dft_quantitative"
    if _matches(text, _ELECTROCHEMICAL_PATTERNS):
        return "electrochemical"
    if _matches(text, _COMPARISON_PATTERNS):
        return "comparison"
    if _matches(text, _MECHANISM_PATTERNS):
        return "mechanism"
    if _matches(text, _FIGURE_PATTERNS):
        return "figure"
    return "narrative"


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _dft_reason(
    *,
    dft_included: bool,
    explicit_types: tuple[str, ...] | None,
    normalized_mode: str | None,
    requested_sections: set[str],
    query_intent: str,
) -> str:
    if explicit_types is not None:
        return "explicit_evidence_types" if dft_included else "excluded_by_explicit_evidence_types"
    if not dft_included:
        return "no_dft_intent"
    if "dft_results" in requested_sections:
        return "requested_section"
    if normalized_mode == "dft_quantitative":
        return "explicit_dft_mode"
    if normalized_mode == "comprehensive":
        return "comprehensive_mode"
    if query_intent == "dft_quantitative":
        return "query_intent"
    return "selected_by_mode"
