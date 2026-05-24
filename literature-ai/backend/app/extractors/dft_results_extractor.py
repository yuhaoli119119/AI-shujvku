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
    evidence_text: str = Field(..., description="The exact sentence or table row text that serves as evidence")
    source_location: SourceLocationModel = Field(default_factory=SourceLocationModel)
    confidence: float = Field(..., description="Confidence score from 0.0 to 1.0")

class DFTResultListModel(BaseModel):
    results: list[DFTResultItemModel]


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

# 吸附质关键词 → 标准名映射
ADSORBATE_MAP: dict[str, str] = {
    "s8": "S8",
    "li2s8": "Li2S8",
    "li2s6": "Li2S6",
    "li2s4": "Li2S4",
    "li2s2": "Li2S2",
    "li2s": "Li2S",
    "sulfur": "S8",
    "polysulfide": "LiPS(generic)",
    "lips": "LiPS(generic)",
}

# 能量单位标准化
UNIT_ALIASES: dict[str, str] = {
    "ev": "eV",
    "ev/atom": "eV/atom",
    "kcal/mol": "kcal/mol",
    "kj/mol": "kJ/mol",
    "mev": "meV",
}

TABLE_HEADER_CATEGORY_RULES: list[tuple[re.Pattern[str], str, str | None]] = [
    (re.compile(r"(adsorption|binding).*(energy|e[_\-\s]*ads|e[_\-\s]*bind)|(^e[_\-\s]*ads(?:\s*\(.*\))?$)|(^e[_\-\s]*bind(?:\s*\(.*\))?$)", re.IGNORECASE), "adsorption_energy", "eV"),
    (re.compile(r"(delta\s*g|gibbs|free energy|^dg$|^Δg$)", re.IGNORECASE), "gibbs_free_energy_change", "eV"),
    (re.compile(r"(barrier|activation|^ea$|energy barrier)", re.IGNORECASE), "reaction_barrier", "eV"),
    (re.compile(r"(bader).*(charge)|(^bader$)", re.IGNORECASE), "bader_charge", "e"),
    (re.compile(r"(charge transfer|electron transfer)", re.IGNORECASE), "charge_transfer", "e"),
    (re.compile(r"(d-?band|epsilon[_\-\s]*d|ε[_\-\s]*d)", re.IGNORECASE), "d_band_center", "eV"),
]

NUMERIC_CATEGORIES = {
    "adsorption_energy",
    "gibbs_free_energy_change",
    "reaction_barrier",
    "li2s_decomposition_barrier",
    "li2s_nucleation_barrier",
    "bader_charge",
    "charge_transfer",
    "d_band_center",
}

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
    "dos_claim": [
        r"(?:DOS|density\s+of\s+states).{0,120}(?:enhanc|increas|reduc|shift|broaden|narrow)",
        r"(PDOS|projected\s+DOS).{0,120}(?:hybridiz|overlap|contribut)",
        ],
    "charge_density_difference_claim": [
        r"(?:charge\s+density\s+difference|\u0394\u03c1|CDD|electron\s+density\s+difference).{0,150}",
        ],
}


def _resolve_adsorbate(text: str) -> str | None:
    """从文本中推断吸附质."""
    text_lower = text.lower()
    for key, name in ADSORBATE_MAP.items():
        if key in text_lower:
            return name
    return None


def _extract_context_around_match(text: str, match_start: int, match_end: int, window: int = 200) -> str:
    """截取匹配周围的上下文作为 evidence."""
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    snippet = text[start:end].replace("\n", " ").strip()
    if len(snippet) > 400:
        return snippet[:400] + "..."
    return snippet


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
        combined = f"{caption}\n{content}"
        for pat_tuple in patterns:
            if isinstance(pat_tuple, tuple):
                pattern, vg, ug = pat_tuple
            else:
                pattern, vg, ug = pat_tuple, 1, 2
            for m in re.finditer(pattern, combined, re.IGNORECASE):
                try:
                    val = float(m.group(vg)) if vg else None
                    raw_unit = m.group(ug).strip() if ug and ug < len(m.groups()) + 1 else None
                    unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
                except (ValueError, IndexError):
                    val, unit = None, None
                loc = SourceLocation(
                    table=caption[:80] if caption else None,
                    page=getattr(tbl, "page", None),
                )
                results.append(DFTResultItem(
                    category=category,
                    adsorbate=_resolve_adsorbate(combined),
                    value=val,
                    unit=unit,
                    evidence_text=_extract_context_around_match(combined, m.start(), m.end()),
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
        content = getattr(tbl, "markdown_content", "") or ""
        headers, rows = _parse_markdown_table(content)
        if not headers or not rows:
            continue
        category_columns, adsorbate_col = _infer_table_columns(headers)
        if not category_columns:
            continue
        for row in rows:
            row_text = " | ".join(row)
            adsorbate = None
            if adsorbate_col is not None and adsorbate_col < len(row):
                adsorbate = _resolve_adsorbate(row[adsorbate_col]) or row[adsorbate_col].strip() or None
            for col_idx, (category, default_unit) in category_columns.items():
                if col_idx >= len(row):
                    continue
                cell = row[col_idx].strip()
                if not cell:
                    continue
                value_match = re.search(r"[-+]?\d*\.?\d+", cell)
                if category in NUMERIC_CATEGORIES and not value_match:
                    continue
                value = float(value_match.group(0)) if value_match else None
                unit_match = re.search(r"(eV|meV|kJ/mol|kcal/mol|e[\u2212-]?|electrons?)", cell, re.IGNORECASE)
                unit = None
                if unit_match:
                    raw_unit = unit_match.group(1).strip()
                    unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit)
                elif default_unit:
                    unit = default_unit
                header = headers[col_idx]
                evidence = f"{header}: {cell}; row: {row_text}"
                results.append(
                    DFTResultItem(
                        category=category,
                        adsorbate=adsorbate or _resolve_adsorbate(evidence),
                        value=value,
                        unit=unit,
                        evidence_text=evidence[:450],
                        source_location=SourceLocation(
                            table=caption[:80] if caption else "Table",
                            page=getattr(tbl, "page", None),
                        ),
                        confidence=0.82 if value is not None else 0.6,
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

        full_text_parts: list[str] = [abstract]
        sec_text_map: dict[int, tuple[str, int | None]] = {}
        offset = 0
        for sec in sections:
            txt = getattr(sec, "text", "") or ""
            title = getattr(sec, "section_title", "") or None
            ps = getattr(sec, "page_start", None)
            if txt:
                sec_text_map[offset] = (title, ps)
                full_text_parts.append(f"\n\n{txt}")
                offset += len(txt) + 2
        full_text = "\n\n".join(full_text_parts)

        for cat in self.categories:
            all_results.extend(self._scan_text(full_text, cat, sec_text_map, sections))
        all_results.extend(_scan_structured_tables(tables))
        for cat in self.categories:
            all_results.extend(_scan_tables_for_category(tables, cat))
        all_results.extend(self._scan_figure_captions(figures))

        if self.llm and self.llm.is_configured() and (markdown or abstract or sections):
            logger.info("Running hybrid LLM DFT extraction")
            system_prompt = (
                "You are an expert materials science data extractor.\n"
                "Extract all explicit DFT calculation results for single/dual-atom catalysts (SAC/DAC) and Li-S battery applications.\n"
                "Categories: adsorption_energy, gibbs_free_energy_change, reaction_barrier, li2s_decomposition_barrier, li2s_nucleation_barrier, "
                "bader_charge, charge_transfer, d_band_center, dos_claim, charge_density_difference_claim.\n"
                "Only return claims that are directly supported by the provided text, captions, or tables.\n"
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
        figures = getattr(doc, "figures", []) or []
        markdown = getattr(doc, "markdown", "") or ""
        section_regex = re.compile(
            r"(comput|dft|theor|result|discuss|mechan|electronic|dos|band|adsor|free energy|barrier|bader|charge)",
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
        for fig in figures[:12]:
            caption = getattr(fig, "caption", "") or ""
            if caption and section_regex.search(caption):
                parts.append(f"## Figure Caption\n{caption[:1200]}")
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
        patterns = CATEGORY_RULES.get(category, [])
        for pat_tuple in patterns:
            if isinstance(pat_tuple, tuple):
                pattern, vg, ug = pat_tuple
            else:
                pattern, vg, ug = pat_tuple, 1, 2
            for m in re.finditer(pattern, text, re.IGNORECASE):
                try:
                    val = float(m.group(vg)) if vg else None
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
                results.append(DFTResultItem(
                    category=category,
                    adsorbate=_resolve_adsorbate(evidence),
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
            cap = getattr(fig, "caption", "") or ""
            if not cap:
                continue
            for cat, patterns in CATEGORY_RULES.items():
                for pat_tuple in patterns:
                    if isinstance(pat_tuple, tuple):
                        pattern, vg, ug = pat_tuple
                    else:
                        pattern, vg, ug = pat_tuple, 1, 2
                    for m in re.finditer(pattern, cap, re.IGNORECASE):
                        try:
                            val = float(m.group(vg)) if vg else None
                            raw_unit = m.group(ug).strip() if ug and ug < len(m.groups()) + 1 else None
                            unit = UNIT_ALIASES.get(raw_unit.lower(), raw_unit) if raw_unit else None
                        except (ValueError, IndexError):
                            val, unit = None, None
                        loc = SourceLocation(
                            figure=cap[:100],
                            page=getattr(fig, "page", None),
                        )
                        results.append(DFTResultItem(
                            category=cat,
                            adsorbate=_resolve_adsorbate(cap),
                            value=val,
                            unit=unit,
                            evidence_text=_extract_context_around_match(cap, m.start(), m.end()),
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
            key = f"{item.category}:{item.value}:{item.unit or ''}:{item.adsorbate or ''}:{item.source_location.section or ''}:{item.source_location.table or ''}"
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
            "confidence": round(item.confidence, 2),
        }
