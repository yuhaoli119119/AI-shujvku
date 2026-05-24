"""写作卡片抽取器 — Stage 2 MVP (规则+模板+启发式，无大模型依赖).

输入: UnifiedPaperDocument (或其 sections/tables/figures)
输出: WritingCard (结构化写作卡片)
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
class EvidenceItem:
    text: str
    source: str  # e.g. "Abstract", "Introduction", "Fig.3 caption"


@dataclass
class SectionStrategy:
    purpose: str = ""
    key_moves: list[str] = field(default_factory=list)
    typical_length_hint: str = ""


@dataclass
class FigureLogic:
    fig_id: str = ""
    purpose: str = ""
    supports_claim: str = ""


@dataclass
class WritingCard:
    """可复用的写作卡片骨架."""
    paper_type: str = "unknown"           # experimental/computational/review/mixed
    research_gap: str = ""
    proposed_solution: str = ""
    core_hypothesis: str = ""
    evidence_chain: list[EvidenceItem] = field(default_factory=list)
    section_strategy: dict[str, SectionStrategy] = field(default_factory=dict)   # section_title → strategy
    figure_logic: list[FigureLogic] = field(default_factory=list)
    abstract_logic: str = ""
    introduction_logic: str = ""
    discussion_logic: str = ""

    def to_dict(self) -> dict:
        return {
            "paper_type": self.paper_type,
            "research_gap": self.research_gap,
            "proposed_solution": self.proposed_solution,
            "core_hypothesis": self.core_hypothesis,
            "evidence_chain": [
                {"text": ev.text, "source": ev.source} for ev in self.evidence_chain
            ],
            "section_strategy": {
                k: {
                    "purpose": v.purpose,
                    "key_moves": v.key_moves,
                    "typical_length_hint": v.typical_length_hint,
                }
                for k, v in self.section_strategy.items()
            },
            "figure_logic": [
                {
                    "fig_id": fl.fig_id,
                    "purpose": fl.purpose,
                    "supports_claim": fl.supports_claim,
                }
                for fl in self.figure_logic
            ],
            "abstract_logic": self.abstract_logic,
            "introduction_logic": self.introduction_logic,
            "discussion_logic": self.discussion_logic,
        }

class EvidenceItemModel(BaseModel):
    text: str
    source: str

class SectionStrategyModel(BaseModel):
    purpose: str
    key_moves: list[str]
    typical_length_hint: str

class FigureLogicModel(BaseModel):
    fig_id: str
    purpose: str
    supports_claim: str

class WritingCardModel(BaseModel):
    paper_type: str = Field(description="computational, experimental, review, or mixed")
    research_gap: str = Field(description="The core problem or knowledge gap this paper aims to solve")
    proposed_solution: str = Field(description="The main solution or approach developed in the paper")
    core_hypothesis: str = Field(description="The underlying hypothesis driving the study")
    evidence_chain: list[EvidenceItemModel] = Field(description="Key findings and the figures/tables that support them")
    section_strategy: dict[str, SectionStrategyModel] = Field(description="Writing logic breakdown per section")
    figure_logic: list[FigureLogicModel] = Field(description="Role of each figure in the argument")
    abstract_logic: str = Field(description="Sentence-by-sentence logic structure of the abstract")
    introduction_logic: str = Field(description="Paragraph-by-paragraph logical flow of the introduction")
    discussion_logic: str = Field(description="Thematic flow and argument structure in the discussion")

    def to_dict(self) -> dict:
        return {
            "paper_type": self.paper_type,
            "research_gap": self.research_gap,
            "proposed_solution": self.proposed_solution,
            "core_hypothesis": self.core_hypothesis,
            "evidence_chain": [
                {"text": ev.text, "source": ev.source} for ev in self.evidence_chain
            ],
            "section_strategy": {
                k: {
                    "purpose": v.purpose,
                    "key_moves": v.key_moves,
                    "typical_length_hint": v.typical_length_hint,
                }
                for k, v in self.section_strategy.items()
            },
            "figure_logic": [
                {
                    "fig_id": fl.fig_id,
                    "purpose": fl.purpose,
                    "supports_claim": fl.supports_claim,
                }
                for fl in self.figure_logic
            ],
            "abstract_logic": self.abstract_logic,
            "introduction_logic": self.introduction_logic,
            "discussion_logic": self.discussion_logic,
        }


# ---------------------------------------------------------------------------
# 论文类型判定规则
# ---------------------------------------------------------------------------

_PAPER_TYPE_RULES: dict[str, list[tuple[str, float]]] = {
    "computational": [
        (r"\bDFT\b", 2.0),
        (r"\bfirst.?principles?\b", 1.5),
        (r"\bdensity\s+functional\b", 1.5),
        (r"\bcomputational\b", 1.5),
        (r"\bmolecular\s+dynamics?\b", 1.0),
        (r"\bNEB\b", 1.2),
        (r"\bab\s+initio\b", 1.5),
        (r"\bsimulation(s)?\b", 0.8),
        (r"\btheoretical\b", 0.8),
    ],
    "experimental": [
        (r"\bsynthesized?\b", 1.5),
        (r"\bexperiment(al|s)?\b", 1.2),
        (r"\bXRD\b|\bXPS\b|\bSEM\b|\bTEM\b|\bXAFS?\b", 1.8),
        (r'\b(battery|cell)(\s+performance)?\s*test', 1.5),
        (r"\bcycling\s+(test|performance|stability)\b", 1.5),
        (r"\belectrochemical\s+measurement", 1.3),
        (r"\bin\s+vitro|in\s+vivo|clinical", 1.5),
        (r"\bfabricat", 1.0),
    ],
}

_GAP_MARKERS: list[tuple[str, str]] = [
    # (pattern, gap_template)
    (r"however.{0,150}(?:remain[s]?|still|lack|limit|challenge|problem|issue|poor|insufficient)",
     "Existing approaches still face challenges with {context}."),
    (r"despite.{0,150}(?:progress|advance|development).{0,80}(?:challenge|issue|limit|drawback|bottleneck)",
     "Despite prior advances, {context} remains unsolved."),
    (r"(?:major|critical|significant|key).{0,60}(?:challenge|issue|problem|barrier|limitation)",
     "{context} is a critical challenge in the field."),
    (r"there\s+is\s+(?:no|little|rarely|few).{0,100}(?:report|study|investigation|work)",
     "There is limited work on {context}."),
]

_SOLUTION_MARKERS: list[tuple[str, str]] = [
    (r"(?:herein|in\s+this\s+work|we\s+(?:report|present|propose|demonstrate)).{0,200}",
     "This work proposes/develops {context}."),
    (r"(?:novel|new|design|strategy|approach|method).{0,200}(?:was\s+(?:designed|developed)|to\s+(?:address|solve|overcome))",
     "A novel {context} was designed to address the above issues."),
    (r"(?:we\s+(?:design|construct|fabricate|synthesize)).{0,200}",
     "We developed/fabricated {context}."),
]

_HYPOTHESIS_MARKERS: list[tuple[str, str]] = [
    (r"(?:we\s+hypothesiz|our\s+hypothesis|it\s+is\s+hypothesized?|we\s+propose\s+that?).{0,300}",
     "{context}"),
    (r"(?:based\s+on|motivated\s+by|inspired\s+by).{0,200}(?:can|could|would|may|might).{0,200}",
     "{context} is expected to..."),
    (r"(?:expected\s+that|anticipate|predict).{0,250}",
     "{context}"),
]


def _extract_sentences(text: str, max_n: int = 10) -> list[str]:
    """将文本拆分为句子列表."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if len(s.strip()) > 15][:max_n]


def _find_section_by_type(sections: list[Any], type_patterns: list[str]) -> tuple[Any | None, str]:
    """按章节类型模糊匹配查找章节."""
    for sec in sections:
        title = getattr(sec, "section_title", "") or ""
        title_lower = title.lower()
        for pat in type_patterns:
            if re.search(pat, title_lower):
                return sec, title
    return None, ""


def _score_text_for_type(text: str) -> str:
    """根据文本内容评分判断论文类型."""
    scores: dict[str, float] = {}
    text_lower = text.lower()
    for ptype, rules in _PAPER_TYPE_RULES.items():
        score = 0.0
        for pattern, weight in rules:
            matches = re.findall(pattern, text_lower)
            score += weight * len(matches)
        scores[ptype] = score

    # 也检查是否有实验方法标记
    total = sum(scores.values())
    if total < 1.0:
        return "unknown"
    if scores.get("computational", 0) >= scores.get("experimental", 0) * 0.7:
        return "computational" if scores["computational"] > scores.get("experimental", 0) else "mixed"
    elif scores.get("experimental", 0) > scores.get("computational", 0):
        return "experimental"
    else:
        return "mixed"


def _classify_figure_purpose(caption: str) -> tuple[str, str]:
    """推断图表目的.
    Returns (purpose_category, description).
    """
    cap_lower = caption.lower()
    if re.search(r'(?:adsorption|binding)\s*(?:energy|config|structure)', cap_lower):
        return "structural/energetic", "Shows adsorption configuration and binding energy"
    if re.search(r'DOS|PDOS|(?:band\s+structure)|(?:density\s+of\s+states)', cap_lower):
        return "electronic_structure", "Reveals electronic structure / DOS features"
    if re.search(r'(?:charge\s+density|electron\s+density|CDD|difference)', cap_lower):
        return "charge_analysis", "Illustrates charge transfer / density difference"
    if re.search(r'(?:reaction\s+pathway|energy\s+profile|Gibbs|free\s+energy|barrier|NEB)', cap_lower):
        return "kinetic_thermodynamic", "Presents reaction energetics / barriers"
    if re.search(r'(?:cycling|capacity|voltage|rate|performance|cycle)', cap_lower):
        return "electrochemical_performance", "Demonstrates electrochemical performance"
    if re.search(r'(?:mechanism|schematic|illustration|scheme|design)', cap_lower):
        return "conceptual_schematic", "Proposes conceptual mechanism or design"
    if re.search(r'(?:XRD|XPS|SEM|TEM|Raman|FTIR|XAFS|BET)', cap_lower):
        return "characterization", "Material characterization evidence"
    if re.search(r'(?:model|calculation|simulation|optimized?)', cap_lower):
        return "computational_model", "Computational model and results"
    return "general", "Supporting figure/table"


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class WritingCardExtractor:
    """从论文文档中提取写作卡片骨架.

    策略:
    1. 论文类型 → 关键词加权打分
    2. Research Gap → however/despite 句式 + Introduction 段落定位
    3. Solution → herein/we propose 句式
    4. Hypothesis → we hypothesize 句式
    5. Evidence Chain → Results 各段落首句 + Figure/Table 引用
    6. Section Strategy → 基于标准 IMRaD 结构的模板
    7. Figure Logic → 图注分类 + 与 claim 的关联
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.llm = LLMService(settings) if settings else None

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

    def extract(self, unified_document: Any) -> dict:
        """从 UnifiedPaperDocument 抽取写作卡片骨架."""
        doc = self._coerce_input(unified_document)

        abstract = getattr(doc, "abstract", "") or ""
        markdown = getattr(doc, "markdown", "") or ""

        logger.info("Running rule-based Writing Card extraction")
        card = WritingCard()

        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        figures = getattr(doc, "figures", []) or []
        abstract = getattr(doc, "abstract", "") or ""

        full_text = "\n\n".join(
            [abstract] + [getattr(s, "text", "") or "" for s in sections]
        )

        # ---- 1. 论文类型 ----
        card.paper_type = _score_text_for_type(full_text)

        # ---- 2. Research Gap (主要在 Introduction) ----
        intro_sec, intro_title = _find_section_by_type(sections, [
            r"intro", r"background", r"motiv",
        ])
        intro_text = getattr(intro_sec, "text", "") or "" if intro_sec else ""
        card.research_gap = self._extract_gap(intro_text, abstract)

        # ---- 3. Proposed Solution (通常在 Abstract末尾 / Intro末尾) ----
        card.proposed_solution = self._extract_solution(abstract, intro_text)

        # ---- 4. Core Hypothesis ----
        card.core_hypothesis = self._extract_hypothesis(
            abstract, intro_text, full_text[:5000]
        )

        # ---- 5. Evidence Chain ----
        results_sec, _ = _find_section_by_type(sections, [
            r"result", r"discuss",
        ])
        results_text = getattr(results_sec, "text", "") or "" if results_sec else ""
        card.evidence_chain = self._build_evidence_chain(
            results_text, figures, tables
        )

        # ---- 6. Section Strategy ----
        card.section_strategy = self._infer_section_strategies(sections)

        # ---- 7. Figure Logic ----
        card.figure_logic = self._infer_figure_logic(figures)

        # ---- 8. Abstract / Introduction / Discussion logic ----
        card.abstract_logic = self._infer_abstract_logic(abstract)
        card.introduction_logic = self._infer_introduction_logic(intro_text)
        discuss_sec, _ = _find_section_by_type(sections, [
            r"discuss", r"conclusion",
        ])
        discuss_text = getattr(discuss_sec, "text", "") or "" if discuss_sec else ""
        card.discussion_logic = self._infer_discussion_logic(discuss_text)

        rule_payload = card.to_dict()

        if self.llm and self.llm.is_configured() and (markdown or abstract or sections):
            logger.info("Running hybrid LLM Writing Card extraction")
            system_prompt = (
                "You are an expert scientific writer and reviewer.\n"
                "Extract the structural and logical writing skeleton of this paper.\n"
                "Identify the core hypothesis, research gap, evidence chain, section-level logical flows, and figure roles.\n"
                "Use only claims directly supported by the provided text.\n"
                "Return the extracted logic matching the JSON schema exactly."
            )
            text_to_process = self._build_focus_text(doc)
            try:
                llm_output = self.llm.structured_extract(system_prompt, text_to_process, WritingCardModel)
                if llm_output:
                    return self._merge_cards(rule_payload, llm_output.model_dump())
            except Exception as e:
                logger.warning(f"LLM Writing Card extraction failed, keeping rule-based card: {e}")

        return rule_payload

    def _build_focus_text(self, doc: Any, max_chars: int = 40000) -> str:
        abstract = getattr(doc, "abstract", "") or ""
        sections = getattr(doc, "sections", []) or []
        tables = getattr(doc, "tables", []) or []
        figures = getattr(doc, "figures", []) or []
        markdown = getattr(doc, "markdown", "") or ""
        section_regex = re.compile(r"(intro|background|result|discuss|conclusion|method|comput|experiment)", re.IGNORECASE)
        parts: list[str] = []
        if abstract:
            parts.append("## Abstract\n" + abstract[:5000])
        for sec in sections:
            title = getattr(sec, "section_title", "") or ""
            text = getattr(sec, "text", "") or ""
            if not text:
                continue
            if section_regex.search(title):
                parts.append(f"## Section: {title or 'Untitled'}\n{text[:5000]}")
        for fig in figures[:10]:
            caption = getattr(fig, "caption", "") or ""
            if caption:
                parts.append(f"## Figure Caption\n{caption[:1000]}")
        for tbl in tables[:10]:
            caption = getattr(tbl, "caption", "") or ""
            if caption:
                parts.append(f"## Table Caption\n{caption[:800]}")
        if not parts and markdown:
            parts.append(markdown[:max_chars])
        return "\n\n".join(parts)[:max_chars]

    def _merge_cards(self, rule_payload: dict, llm_payload: dict) -> dict:
        merged = dict(rule_payload)
        for key in ("paper_type", "research_gap", "proposed_solution", "core_hypothesis", "abstract_logic", "introduction_logic", "discussion_logic"):
            llm_value = llm_payload.get(key)
            if isinstance(llm_value, str) and llm_value.strip():
                merged[key] = llm_value.strip()
        if llm_payload.get("evidence_chain"):
            merged["evidence_chain"] = self._merge_evidence_chain(rule_payload.get("evidence_chain") or [], llm_payload["evidence_chain"])
        if llm_payload.get("section_strategy"):
            merged["section_strategy"] = llm_payload["section_strategy"]
        if llm_payload.get("figure_logic"):
            merged["figure_logic"] = llm_payload["figure_logic"]
        return merged

    @staticmethod
    def _merge_evidence_chain(rule_items: list[dict], llm_items: list[dict]) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        for item in llm_items + rule_items:
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            if not text:
                continue
            key = text[:120].lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append({"text": text[:400], "source": source or "Unknown"})
        return merged[:20]

    # -- 各子模块 -----------------------------------------------------------

    def _extract_gap(self, intro_text: str, abstract: str) -> str:
        """从 Introduction / Abstract 中提取 research gap."""
        combined = f"{abstract}\n\n{intro_text}"
        for pattern, template in _GAP_MARKERS:
            m = re.search(pattern, combined, re.IGNORECASE | re.DOTALL)
            if m:
                context = m.group(0)[:300].replace("\n", " ").strip()
                return template.format(context=context)
        # fallback: 取 Introduction 最后 1/3 的第一句
        if intro_text:
            sentences = _extract_sentences(intro_text)
            if len(sentences) > 3:
                return sentences[-min(3, len(sentences))][:300]
        return "Research gap not explicitly stated."

    def _extract_solution(self, abstract: str, intro_text: str) -> str:
        """提取 proposed solution."""
        combined = f"{abstract}\n\n{intro_text}"
        for pattern, template in _SOLUTION_MARKERS:
            m = re.search(pattern, combined, re.IGNORECASE | re.DOTALL)
            if m:
                context = m.group(0)[:350].replace("\n", " ").strip()
                return template.format(context=context)
        # fallback: Abstract 最后一句常含 solution
        abs_sentences = _extract_sentences(abstract)
        if abs_sentences:
            return abs_sentences[-1][:350]
        return "Solution not clearly extracted."

    def _extract_hypothesis(self, abstract: str, intro: str, full_head: str) -> str:
        """提取 core hypothesis."""
        combined = f"{abstract}\n\n{intro}\n\n{full_head}"
        for pattern, template in _HYPOTHESIS_MARKERS:
            m = re.search(pattern, combined, re.IGNORECASE | re.DOTALL)
            if m:
                context = m.group(0)[:400].replace("\n", " ").strip()
                return template.format(context=context)
        # fallback: 从 solution 推断
        sol = self._extract_solution(abstract, intro)
        if sol != "Solution not clearly extracted.":
            return f"The core hypothesis underlying this approach: {sol[:250]}"
        return "Hypothesis not explicitly stated."

    @staticmethod
    def _build_evidence_chain(
        results_text: str,
        figures: list[Any],
        tables: list[Any],
    ) -> list[EvidenceItem]:
        """构建证据链: Results 段落核心句 + 图表引用."""
        chain: list[EvidenceItem] = []

        # 从 Results 文本提取关键句（含数据引用的句子）
        result_sentences = _extract_sentences(results_text, max_n=30)
        evidence_keywords = [
            "show", "demonstrat", "reveals", "indicates", "confirms",
            "suggest", "found", "observe", "exhibit", "achieve",
            "ev", "energy", "barrier", "adsorption", "conductivity",
            "fig", "figure", "table", "capacity", "cycle",
        ]
        for sent in result_sentences:
            sent_lower = sent.lower()
            if any(kw in sent_lower for kw in evidence_keywords):
                chain.append(EvidenceItem(text=sent[:400], source="Results"))

        # 图表作为证据补充
        for fig in figures:
            cap = getattr(fig, "caption", "") or ""
            if cap:
                chain.append(EvidenceItem(text=cap[:300], source=f"Figure"))
        for tbl in tables:
            cap = getattr(tbl, "caption", "") or ""
            if cap:
                chain.append(EvidenceItem(text=cap[:300], source="Table"))

        # 去重并限制长度
        seen: set[str] = set()
        deduped: list[EvidenceItem] = []
        for ev in chain:
            key = ev.text[:80].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(ev)
        return deduped[:20]

    @staticmethod
    def _infer_section_strategies(sections: list[Any]) -> dict[str, SectionStrategy]:
        """为每个已识别章节推断写作策略."""
        strategies: dict[str, SectionStrategy] = {}
        for sec in sections:
            title = getattr(sec, "section_title", "") or "Untitled"
            txt = getattr(sec, "text", "") or ""
            title_lower = title.lower()
            strat = SectionStrategy()

            if re.search(r'intro|background', title_lower):
                strat.purpose = "Motivate the problem, establish the gap, position this work."
                strat.key_moves = ["Broad opening → Narrow focus", "Gap statement", "Solution preview"]
                strat.typical_length_hint = "~15-25% of paper"
            elif re.search(r'method|experiment|computational|theoretical|detail', title_lower):
                strat.purpose = "Describe how the study was conducted; enable reproducibility."
                strat.key_moves = ["Materials/scope definition", "Methodology details", "Validation approach"]
                strat.typical_length_hint = "~20-30% of paper"
            elif re.search(r'result', title_lower):
                strat.purpose = "Present findings without interpretation; lead reader through evidence."
                n_figs = len(re.findall(r'fig[ure]?.?\d', txt.lower()))
                strat.key_moves = [f"Structural/evidence layer ({n_figs} figs referenced)", "Quantitative findings", "Comparative analysis"]
                strat.typical_length_hint = "~25-35% of paper"
            elif re.search(r'discussion', title_lower):
                strat.purpose = "Interpret results in context of hypothesis and literature."
                strat.key_moves = ["Result → Mechanism mapping", "Literature comparison", "Limitations & outlook"]
                strat.typical_length_hint = "~15-20% of paper"
            elif re.search(r'conclusion', title_lower):
                strat.purpose = "Synthesize contributions and point forward."
                strat.key_moves = ["Key takeaways", "Broader impact", "Future directions"]
                strat.typical_length_hint = "~3-5% of paper"
            else:
                strat.purpose = f"Supporting section: {title}"
                strat.key_moves = ["Content presentation"]
                strat.typical_length_hint = "varies"

            strategies[title] = strat
        return strategies

    @staticmethod
    def _infer_figure_logic(figures: list[Any]) -> list[FigureLogic]:
        """推断每张图在论证中的角色."""
        logic_list: list[FigureLogic] = []
        for i, fig in enumerate(figures):
            cap = getattr(fig, "caption", "") or ""
            if not cap:
                continue
            purpose_cat, desc = _classify_figure_purpose(cap)

            # 尝试提取 Fig 编号
            fig_m = re.match(r'(?:fig(?:ure)?\.?\s*)([\w\-\.]+)', cap, re.IGNORECASE)
            fig_id = fig_m.group(0) if fig_m else f"Figure_{i+1}"

            # 推断支持的声明
            claim = ""
            if "adsorption" in cap.lower() or "binding" in cap.lower():
                claim = "Adsorption strength / binding configuration"
            elif "DOS" in cap or "band" in cap.lower():
                claim = "Electronic structure / conductivity origin"
            elif "charge" in cap.lower() and "density" in cap.lower():
                claim = "Charge transfer pathway"
            elif "barrier" in cap.lower() or "energy" in cap.lower():
                claim = "Reaction kinetics / thermodynamics"
            elif "cycle" in cap.lower() or "capacity" in cap.lower():
                claim = "Electrochemical performance validation"

            logic_list.append(FigureLogic(
                fig_id=fig_id,
                purpose=f"{purpose_cat}: {desc}",
                supports_claim=claim or "General supporting evidence",
            ))
        return logic_list

    @staticmethod
    def _infer_abstract_logic(abstract: str) -> str:
        """分析摘要的逻辑结构."""
        if not abstract or len(abstract) < 50:
            return "Abstract too short or missing."

        sentences = _extract_sentences(abstract, max_n=15)
        n = len(sentences)
        parts: list[str] = []

        if n <= 3:
            parts.append(f"[Short abstract: {n} sentences — likely background + main result]")
        else:
            # 用启发式标注各句功能
            for i, sent in enumerate(sentences):
                s = sent.lower()
                if i == 0:
                    label = "[Background/context opener]"
                elif any(w in s for w in ["however", "but", "yet", "challenge", "limit"]):
                    label = "[Gap/problem framing]"
                elif any(w in s for w in ["herein", "we report", "we propose", "we present", "this work"]):
                    label = "[Solution announcement]"
                elif any(w in s for w in ["hypothesiz", "expect", "predict", "design"]):
                    label = "[Hypothesis/approach]"
                elif any(w in s for w in ["show", "demonstrat", "reveal", "achieve", "eV", "capacity"]):
                    label = "[Key result]"
                elif any(w in s for w in ["conclus", "suggest", "provide", "offer", "implication"]):
                    label = "[Implication/closing]"
                else:
                    label = "[Supporting detail]"
                parts.append(label)

        return " | ".join(parts)

    @staticmethod
    def _infer_introduction_logic(intro: str) -> str:
        """分析引言的逻辑流."""
        if not intro or len(intro) < 50:
            return "Introduction too short or missing."

        paragraphs = re.split(r'\n\s*\n', intro)
        moves: list[str] = []
        for i, para in enumerate(paragraphs):
            p = para.strip()
            if len(p) < 30:
                continue
            p_lower = p.lower()
            if i == 0:
                moves.append("Para 1: Broad context / field significance")
            elif any(w in p_lower for w in ["however", "but", "yet", "unfortunately", "challenge", "gap", "limit"]):
                moves.append(f"Para {i+1}: Problem/gap identification")
            elif any(w in p_lower for w in ["herein", "this work", "we propose", "we design", "we report"]):
                moves.append(f"Para {i+1}: Solution & contribution preview")
            elif any(w in p_lower for w in ["previous", "prior", "earlier", "reported"]):
                moves.append(f"Para {i+1}: Literature positioning")
            else:
                moves.append(f"Para {i+1}: Contextual development")

        return "; ".join(moves) if moves else "Could not parse introduction structure."

    @staticmethod
    def _infer_discussion_logic(discuss: str) -> str:
        """分析讨论的逻辑结构."""
        if not discuss or len(discuss) < 50:
            return "Discussion too short or missing."

        sentences = _extract_sentences(discuss, max_n=20)
        themes: list[str] = []
        for sent in sentences:
            s = sent.lower()
            if any(w in s for w in ["consistent", "agree", "confirm", "support", "validate"]):
                themes.append("[Self-consistency check]")
            elif any(w in s for w in ["compare", "contrast", "higher than", "lower than", "superior", "outperform"]):
                themes.append("[Comparison with literature/prior art]")
            elif any(w in s for w in ["mechanism", "origin", "reason", "because", "due to", "attribute"]):
                themes.append("[Mechanistic explanation]")
            elif any(w in s for w in ["limitation", "drawback", "challenge remain", "future", "further work"]):
                themes.append("[Limitations & future outlook]")
            else:
                continue

        return " → ".join(themes) if themes else "Discussion parsed but no clear thematic moves identified."
