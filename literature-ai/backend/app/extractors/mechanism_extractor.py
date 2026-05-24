"""机理声明抽取器 — Stage 2 MVP (规则+启发式，无大模型依赖).

输入: UnifiedPaperDocument
输出: list[MechanismClaim]  (结构化机理声明，每条绑 evidence)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


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
class MechanismClaim:
    mechanism_type: str   # 见 MECHANISM_DEFINITIONS keys
    claim_text: str        # 声明原文/摘要
    evidence_text: str     # 支撑该声明的原文片段
    direction: str | None = None  # "promote" / "inhibit" / "neutral"
    key_species: list[str] = field(default_factory=list)
    source_location: SourceLocation = field(default_factory=SourceLocation)
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "mechanism_type": self.mechanism_type,
            "claim_text": self.claim_text,
            "evidence_text": self.evidence_text,
            "direction": self.direction,
            "key_species": self.key_species,
            "source_location": {
                "section": self.source_location.section,
                "page": self.source_location.page,
                "figure": self.source_location.figure,
                "table": self.source_location.table,
            },
            "confidence": round(self.confidence, 2),
        }


# ---------------------------------------------------------------------------
# 机理类型 → 关键词 / 正则规则
# ---------------------------------------------------------------------------

MECHANISM_DEFINITIONS: dict[str, dict] = {
    "polysulfide_adsorption": {
        "keywords": [
            "adsorb", "anchor", "capture", "trapping", "binding",
            "polysulfid", "LiPS", "Li2Sx", "sulfur species",
            "chemisorption", "physisorption",
        ],
        "patterns": [
            r"(?:strong|weak|efficient|effective).{0,40}(?:adsorb|adsorpt|bind|anchor|capture|trapp).{0,100}(?:polysulfid|LiPS|Li2S[468]|sulfur)",
            r"(?:adsorb|adsorpt|bind|anchor|capture|trapp).{0,80}(?:polysulfid|LiPS|Li2S[468]|sulfur).{0,60}(?:can|could|is able to|effectively)",
            r"(?:can|could|effectively|efficiently).{0,40}(?:adsorb|adsorpt|bind|anchor|capture|trapp).{0,80}(?:polysulfid|LiPS|Li2S[468]|sulfur)",
        ],
        "direction_map": {
            "promote": ["enhance", "strengthen", "improve", "facilitate", "boost", "strong binding"],
            "inhibit": ["weaken", "poor", "insufficient", "weak", "suppress", "hinder"],
        },
    },
    "lips_conversion": {
        "keywords": [
            "conversion", "transform", "convert", "LiPS", "polysulfide",
            "redox", "oxidation", "reduction", "kinetic", "cataly",
        ],
        "patterns": [
            r"(?:accelerat|promot|enhance|facilitate).{0,40}(?:conversion|transformation).{0,80}(?:LiPS|polysulfid|Li2S[468])",
            r"(?:conversion|transform).{0,30}(?:of\s+)?(?:LiPS|polysulfid|Li2S[468]).{0,50}(?:kinetic|rate|fast|slow|barrier)",
            r"(?:LiPS|polysulfid).{0,20}(?:conversion|transform).{0,80}",
        ],
        "direction_map": {
            "promote": ["accelerate", "promote", "enhance", "facilitate", "lower barrier", "fast kinetics"],
            "inhibit": ["slow", "sluggish", "high barrier", "inhibit", "block"],
        },
    },
    "li2s_nucleation": {
        "keywords": [
            "nucleat", "Li2S deposition", "precipitation", "Li2S particle",
            "growth", "size distribution", "uniform",
        ],
        "patterns": [
            r"(?:nucleat|deposition|precipitation).{0,40}Li2S.{0,100}",
            r"Li2S.{0,20}(?:nucleat|deposit|precipitat|particle|growth).{0,100}",
            r"(?:uniform|homogeneous|controlled|guided).{0,40}(?:nucleat|deposition).{0,40}Li2S",
        ],
        "direction_map": {
            "promote": ["uniform", "homogeneous", "controlled", "guide", "reduce size", "prevent agglomeration"],
            "inhibit": ["agglomeration", "large particle", "uneven", "passivation"],
        },
    },
    "li2s_decomposition": {
        "keywords": [
            "decompos", "breakdown", "dissolution", "oxidation of Li2S",
            "Li2S stripping", "delithiation",
        ],
        "patterns": [
            r"(?:decompos|breakdown|dissol|oxid).{0,40}(?:of\s+)?Li2S.{0,100}",
            r"Li2S.{0,20}(?:decompos|breakdown|dissol|oxid|stripp).{0,100}",
        ],
        "direction_map": {
            "promote": ["accelerate", "promote", "lower barrier", "easy decomposition"],
            "inhibit": ["slow", "difficult", "high barrier", "passivation layer"],
        },
    },
    "bidirectional_sulfur_redox": {
        "keywords": [
            "bidirectional", "reversible", "redox cycle", "sulfur redox",
            "charging/discharging", "both directions",
        ],
        "patterns": [
            r"(?:bidirectional|reversible).{0,60}(?:redox|sulfur|conversion)",
            r"sulfur\s+redox.{0,60}(?:bidirection|reversib|both)",
            r"(?:charge|discharge).{0,40}(?:process|reaction|cycle).{0,60}(?:bidirection|reversib|symmetr)",
        ],
        "direction_map": {},
    },
    "orbital_hybridization": {
        "keywords": [
            "hybridiz", "orbital overlap", "p-d coupling", "d-p interaction",
            "band structure", "electronic structure", "DOS", "PDOS",
        ],
        "patterns": [
            r"(?:orbital|band).{0,20}(?:hybridiz|overlap|coupling|interaction|mixin).{0,120}",
            r"(?:p-d|d-p|metal-sulfur).{0,20}(?:hybridiz|overlap|coupling|interact).{0,120}",
            r"(?:DOS|PDOS|density\s+of\s+states).{0,150}(?:overlap|hybridiz|coupling|shift|near\s+Fermi)",
        ],
        "direction_map": {},
    },
    "conductivity_enhancement": {
        "keywords": [
            "conductiv", "electron transfer", "electronic", "resistance",
            "impedance", "charge transport",
        ],
        "patterns": [
            r"(?:conductivity|electronic\s+conduct).{0,60}(?:enhanc|impro|increas|boost).{0,100}",
            r"(?:lowers?\s+(?:the\s+)?(?:charge\s+transfer)?\s*resistance|impedance|R_ct).{0,100}",
            r"(?:improv|enhanc).{0,40}(?:electron\s+transfer|charge\s+transport|conduct).{0,100}",
        ],
        "direction_map": {
            "promote": ["enhance", "improve", "increase", "lower resistance", "faster electron"],
            "inhibit": ["poor conductivity", "insulating", "high resistance"],
        },
    },
    "shuttle_suppression": {
        "keywords": [
            "shuttle effect", "shuttle", "polysulfide diffusion", "migration",
            "blocking layer", "confine", "immobilize",
        ],
        "patterns": [
            r"(?:shuttle\s+effect|shuttling).{0,100}(?:suppress|mitigat|alleviat|inhib|reduc|eliminat|block|prevent)",
            r"(?:suppres|mitigat|allevi|inhib|reduc|elimin|block).{0,60}(?:shuttle|shuttling|diffusion|migration).{0,80}(?:polysulfid|LiPS|Li2Sx)",
            r"(?:suppres|mitigat|allevi|inhib|reduc|elimin|block|prevent).{0,60}(?:shuttle\s+effect|shuttling)",
            r"(?:confin|immobiliz|trapp|anchor).{0,80}(?:polysulfid|LiPS).{0,60}(?:shuttle|diffus|leakage)",
        ],
        "direction_map": {
            "promote": ["suppress", "mitigate", "alleviate", "inhibit", "reduce", "eliminate", "block", "confine", "immobilize"],
            "inhibit": ["severe shuttle", "serious shuttle", "shuttle still exists"],
        },
    },
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

_SPECIES_PATTERNS = [
    r"\b(Li2S[86421]?)\b",
    r"\b(S[8]?)\b",
    r"\b(LiPS)\b",
    r"\b(polysulfides?)\b",
    r"\b(Co(?:-[A-Za-z]+)?)\b",
    r"\b(Fe(?:-[A-Za-z]+)?)\b",
    r"\b(Ni(?:-[A-Za-z]+)?)\b",
    r"\b(Mn(?:-[A-Za-z]+)?)\b",
    r"\b(Mo[Ss]2?)\b",
    r"\b(graphene?|carbon?\s*(nanotube?|fiber?|cloth|foam|network))\b",
]

_MECHANISTIC_ACTION_PATTERNS = [
    r"accelerat", r"promot", r"enhanc", r"facilitat", r"suppress", r"mitigat",
    r"alleviat", r"inhibit", r"reduce", r"lower", r"weaken", r"strengthen",
    r"stabiliz", r"destabiliz", r"anchor", r"adsorb", r"bind", r"capture",
    r"trapp", r"confine", r"immobiliz", r"improv", r"boost",
]


def _extract_key_species(text: str) -> list[str]:
    """从文本中提取关键化学物种."""
    found: list[str] = []
    for pat in _SPECIES_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.group(1):
            name = m.group(1)
            # 标准化大小写
            if name.lower() in ("li2s8", "li2s6", "li2s4", "li2s2", "li2s", "lips", "s8"):
                name = name.replace("li2s", "Li2S").replace("lips", "LiPS").replace("s8", "S8")
            if name not in found:
                found.append(name)
    return found[:5]  # 最多5个物种


def _guess_direction(text_lower: str, direction_map: dict) -> str | None:
    """从文本推断方向."""
    for direction, keywords in direction_map.items():
        for kw in keywords:
            if kw in text_lower:
                return direction
    return "neutral" if direction_map else None


def _has_mechanistic_signal(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _MECHANISTIC_ACTION_PATTERNS)


def _is_supported_claim(mtype: str, evidence: str, direction: str | None, species: list[str]) -> bool:
    has_signal = _has_mechanistic_signal(evidence)
    if mtype in {"polysulfide_adsorption", "lips_conversion", "li2s_nucleation", "li2s_decomposition", "shuttle_suppression", "conductivity_enhancement"}:
        return has_signal and (bool(species) or direction in {"promote", "inhibit"})
    if mtype in {"orbital_hybridization", "bidirectional_sulfur_redox"}:
        return has_signal or len(species) >= 1
    return has_signal


def _extract_evidence(text: str, match_start: int, match_end: int, window: int = 250) -> str:
    """截取匹配周围的上下文作为 evidence."""
    start = max(0, match_start - window // 2)
    end = min(len(text), match_end + window // 2)
    snippet = text[start:end].replace("\n", " ").strip()
    if len(snippet) > 500:
        return snippet[:500] + "..."
    return snippet


def _build_claim_summary(mtype: str, evidence: str, max_len: int = 200) -> str:
    """构建声明摘要（取evidence的核心句）."""
    sentence_end = re.search(r"[.!?]\s", evidence[max_len // 2:])
    if sentence_end:
        end = (max_len // 2) + sentence_end.end()
    else:
        end = min(len(evidence), max_len)
    claim = evidence[:end].strip()
    if not claim.endswith("."):
        claim += "."
    return claim


def _get_section_for_offset(
    offset: int,
    sections: list[Any],
) -> tuple[str | None, int | None]:
    """根据字符偏移量推断所在章节和页码."""
    current_pos = 0
    best_title, best_page = None, None
    for sec in sections:
        txt = getattr(sec, "text", "") or ""
        title = getattr(sec, "section_title", "") or None
        ps = getattr(sec, "page_start", None)
        if txt:
            if current_pos <= offset < current_pos + len(txt):
                return title, ps
            best_title, best_page = title, ps
            current_pos += len(txt) + 2
    return best_title, best_page


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class MechanismExtractor:
    """基于规则 + 启发式的机理声明抽取器 (MVP).

    策略:
    1. 先做关键词初筛（快速排除无关段落）
    2. 对命中的段落用正则精细匹配
    3. 从匹配结果构造 MechanismClaim（含 direction / species / evidence）
    4. 扫描图注、表格作为补充证据源
    """

    def __init__(self) -> None:
        self.mechanism_types = list(MECHANISM_DEFINITIONS.keys())

    @staticmethod
    def _coerce_input(unified_document: Any) -> Any:
        """兼容对象、字典和列表输入."""
        if isinstance(unified_document, list):
            return type("_NS", (), {"sections": unified_document,
                                      "tables": [], "figures": [], "abstract": ""})()
        if isinstance(unified_document, dict):
            ns = type("_NS", (),
                       {"sections": unified_document.get("sections", []),
                        "tables": unified_document.get("tables", []),
                        "figures": unified_document.get("figures", []),
                        "abstract": unified_document.get("abstract", ""),
                        **{k: v for k, v in unified_document.items()
                           if k not in ("sections", "tables", "figures",
                                        "abstract")}})()
            return ns
        return unified_document

    def extract(self, unified_document: Any) -> list[dict]:
        """从 UnifiedPaperDocument 抽取结构化机理声明.
        兼容对象、字典、列表输入。
        """
        doc = self._coerce_input(unified_document)
        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        figures = getattr(doc, "figures", []) or []
        abstract = getattr(doc, "abstract", "") or ""

        all_claims: list[MechanismClaim] = []

        # 拼接全文
        full_parts: list[tuple[str, str | None, int | None]] = []  # (text, title, page_start)
        if abstract:
            full_parts.append((abstract, "Abstract", None))
        for sec in sections:
            txt = getattr(sec, "text", "") or ""
            title = getattr(sec, "section_title", "") or None
            ps = getattr(sec, "page_start", None)
            if txt.strip():
                full_parts.append((txt, title, ps))

        full_text = "\n\n".join(t[0] for t in full_parts)

        # 逐类别扫描正文
        for mtype, defs in MECHANISM_DEFINITIONS.items():
            claims = self._scan_mechanism(full_text, mtype, defs, full_parts)
            all_claims.extend(claims)

        # 扫描图注
        for fig in figures:
            cap = getattr(fig, "caption", "") or ""
            if not cap:
                continue
            for mtype, defs in MECHANISM_DEFINITIONS.items():
                claims = self._scan_single_text(cap, mtype, defs, source_fig=cap[:120])
                for c in claims:
                    c.source_location.page = getattr(fig, "page", None)
                    c.confidence = min(c.confidence, 0.75)
                all_claims.extend(claims)

        # 扫描表格
        for tbl in tables:
            cap = getattr(tbl, "caption", "") or ""
            content = getattr(tbl, "markdown_content", "") or ""
            combined = f"{cap} {content}"
            if len(combined.strip()) < 10:
                continue
            for mtype, defs in MECHANISM_DEFINITIONS.items():
                claims = self._scan_single_text(combined, mtype, defs, source_tbl=cap[:120])
                for c in claims:
                    c.source_location.page = getattr(tbl, "page", None)
                    c.confidence = min(c.confidence, 0.72)
                all_claims.extend(claims)

        # 去重
        all_claims = self._deduplicate(all_claims)

        return [c.to_dict() for c in all_claims]

    # -- 内部方法 ----------------------------------------------------------

    def _scan_mechanism(
        self,
        full_text: str,
        mtype: str,
        defs: dict,
        parts: list[tuple[str, str | None, int | None]],
    ) -> list[MechanismClaim]:
        """对一种机理类型扫描完整文本."""
        results: list[MechanismClaim] = []

        # 阶段1：关键词初筛 → 定位候选段落
        candidates = self._keyword_screening(full_text, defs["keywords"])

        # 阶段2：正则精细匹配候选段落
        for start, end in candidates:
            segment = full_text[start:end]
            for pat in defs["patterns"]:
                for m in re.finditer(pat, segment, re.IGNORECASE | re.DOTALL):
                    abs_start = start + m.start()
                    abs_end = start + m.end()
                    evidence = _extract_evidence(full_text, abs_start, abs_end)

                    # 推断位置
                    title, page = _get_section_for_offset(abs_start,
                                                         [(type("", (), {"text": t, "section_title": ti, "page_start": ps})())
                                                          for t, ti, ps in parts])

                    direction = _guess_direction(segment[m.start():m.end() + 200].lower(), defs.get("direction_map", {}))
                    species = _extract_key_species(evidence)
                    if not _is_supported_claim(mtype, evidence, direction, species):
                        continue

                    confidence = self._calc_confidence(m, evidence, species, mtype)

                    results.append(MechanismClaim(
                        mechanism_type=mtype,
                        claim_text=_build_claim_summary(mtype, evidence),
                        evidence_text=evidence,
                        direction=direction,
                        key_species=species,
                        source_location=SourceLocation(section=title, page=page),
                        confidence=confidence,
                    ))
        return results

    def _scan_single_text(
        self,
        text: str,
        mtype: str,
        defs: dict,
        source_fig: str | None = None,
        source_tbl: str | None = None,
    ) -> list[MechanismClaim]:
        """对单个文本块（如图注/表格）进行扫描."""
        results: list[MechanismClaim] = []
        for pat in defs["patterns"]:
            for m in re.finditer(pat, text, re.IGNORECASE | re.DOTALL):
                evidence = _extract_evidence(text, m.start(), m.end())
                direction = _guess_direction(text.lower(), defs.get("direction_map", {}))
                species = _extract_key_species(evidence)
                if not _is_supported_claim(mtype, evidence, direction, species):
                    continue
                loc = SourceLocation(
                    figure=source_fig,
                    table=source_tbl,
                )
                results.append(MechanismClaim(
                    mechanism_type=mtype,
                    claim_text=_build_claim_summary(mtype, evidence),
                    evidence_text=evidence,
                    direction=direction,
                    key_species=species,
                    source_location=loc,
                    confidence=self._calc_confidence(m, evidence, species, mtype),
                ))
        return results

    @staticmethod
    def _keyword_screening(text: str, keywords: list[str]) -> list[tuple[int, int]]:
        """关键词初筛：找到包含关键词的候选段落边界.

        以句子为单位返回 (start, end).
        """
        text_lower = text.lower()
        hit_positions: set[int] = set()
        for kw in keywords:
            idx = 0
            while True:
                idx = text_lower.find(kw.lower(), idx)
                if idx == -1:
                    break
                hit_positions.add(idx)
                idx += len(kw)

        if not hit_positions:
            return []

        # 扩展到句子/段落级别
        sentence_bounds: list[tuple[int, int]] = []
        sorted_hits = sorted(hit_positions)
        current_start = max(0, sorted_hits[0] - 100)
        current_end = min(len(text), sorted_hits[0] + 300)

        for pos in sorted_hits[1:]:
            if pos <= current_end + 150:
                current_end = min(len(text), pos + 300)
            else:
                sentence_bounds.append((current_start, current_end))
                current_start = max(0, pos - 100)
                current_end = min(len(text), pos + 300)
        sentence_bounds.append((current_start, current_end))
        return sentence_bounds

    @staticmethod
    def _calc_confidence(match: re.Match, evidence: str, species: list[str], mtype: str) -> float:
        score = 0.3
        # 匹配长度（注意：大的范围先判断）
        span = match.end() - match.start()
        if span > 60:
            score += 0.15
        elif span > 30:
            score += 0.1
        # evidence 质量
        if len(evidence) > 100:
            score += 0.15
        if len(evidence) > 250:
            score += 0.05
        # 物种识别
        if species:
            score += 0.15
        if _has_mechanistic_signal(evidence):
            score += 0.12
        # 高价值机理类型加分
        high_value = {"polysulfide_adsorption", "lips_conversion", "shuttle_suppression"}
        if mtype in high_value:
            score += 0.1
        return min(score, 0.98)

    @staticmethod
    def _deduplicate(claims: list[MechanismClaim]) -> list[MechanismClaim]:
        """去重: 同类型+相似evidence保留置信度最高的."""
        seen: dict[str, MechanismClaim] = {}
        for c in claims:
            # 用前80个字符作为指纹
            key = f"{c.mechanism_type}:{c.evidence_text[:80].lower()}"
            if key not in seen or c.confidence > seen[key].confidence:
                seen[key] = c
        return list(seen.values())
