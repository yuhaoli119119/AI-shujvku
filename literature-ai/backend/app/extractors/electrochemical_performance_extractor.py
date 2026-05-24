from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any


def _as_namespace(unified_document: Any) -> Any:
    if isinstance(unified_document, list):
        return SimpleNamespace(sections=unified_document, tables=[], figures=[], abstract="", markdown="")
    if isinstance(unified_document, dict):
        return SimpleNamespace(
            sections=unified_document.get("sections", []),
            tables=unified_document.get("tables", []),
            figures=unified_document.get("figures", []),
            abstract=unified_document.get("abstract", ""),
            markdown=unified_document.get("markdown", ""),
        )
    return unified_document


def _get_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _context(text: str, start: int, end: int, window: int = 140) -> str:
    snippet = text[max(0, start - window): min(len(text), end + window)]
    return re.sub(r"\s+", " ", snippet).strip()


class ElectrochemicalPerformanceExtractor:
    """Rule-based extractor for Li-S electrochemical performance facts."""

    CAPACITY_PATTERNS = [
        re.compile(r"(?:specific\s+)?capacity(?:\s+\w+){0,4}\s+(?:of\s+|is\s+|was\s+|reached\s+|delivered\s+|at\s+)?(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mAh\s*/?\s*g(?:-1)?|Ah\s*/?\s*kg(?:-1)?)", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mAh\s*/?\s*g(?:-1)?|Ah\s*/?\s*kg(?:-1)?)\s+(?:specific\s+)?capacity", re.IGNORECASE),
        re.compile(r"(?:delivered|reached|retained|faded\s+to)\s+(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mAh\s*/?\s*g(?:-1)?|Ah\s*/?\s*kg(?:-1)?)", re.IGNORECASE),
    ]
    SULFUR_LOADING_PATTERNS = [
        re.compile(r"sulfur\s+loading(?:\s+\w+){0,3}\s+(?:of\s+|is\s+|was\s+)?(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mg\s*/?\s*cm(?:-2|2))", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mg\s*/?\s*cm(?:-2|2))\s+sulfur\s+loading", re.IGNORECASE),
    ]
    SULFUR_CONTENT_PATTERNS = [
        re.compile(r"sulfur\s+content(?:\s+\w+){0,3}\s+(?:of\s+|is\s+|was\s+)?(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>wt\.?\s*%|%)", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>wt\.?\s*%|%)\s+sulfur", re.IGNORECASE),
    ]
    ES_RATIO_PATTERNS = [
        re.compile(r"(?:E/S|electrolyte[-\s]to[-\s]sulfur)\s+ratio(?:\s+\w+){0,4}\s+(?:of\s+|is\s+|was\s+|at\s+)?(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>uL\s*/?\s*mg|uL\s*mg-1|µL\s*/?\s*mg|muL\s*/?\s*mg)?", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>uL\s*/?\s*mg|uL\s*mg-1|µL\s*/?\s*mg|muL\s*/?\s*mg)\s+(?:E/S|electrolyte[-\s]to[-\s]sulfur)", re.IGNORECASE),
    ]
    CYCLE_PATTERNS = [
        re.compile(r"(?:after|for)\s+(?P<value>\d{1,5})\s+cycles?\b", re.IGNORECASE),
        re.compile(r"(?P<value>\d{1,5})\s+cycles?\s+(?:at|with|under)\b", re.IGNORECASE),
    ]
    RATE_PATTERNS = [
        re.compile(r"(?:at|under|with)\s+(?P<value>\d+(?:\.\d+)?)\s*C\b", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*C\s+(?:rate|cycling)\b", re.IGNORECASE),
        re.compile(r"(?:current\s+density|rate)\s+(?:of\s+)?(?P<value>\d+(?:\.\d+)?)\s*C\b", re.IGNORECASE),
    ]
    DECAY_PATTERNS = [
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*%\s*/\s*cycle\b", re.IGNORECASE),
        re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*%\s+per\s+cycle\b", re.IGNORECASE),
        re.compile(r"decay(?:\s+rate)?(?:\s+of)?\s+(?P<value>\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
    ]

    def extract(self, unified_document: Any) -> list[dict[str, Any]]:
        doc = _as_namespace(unified_document)
        results: list[dict[str, Any]] = []

        abstract = getattr(doc, "abstract", "") or ""
        if abstract:
            results.extend(self._extract_from_text(abstract, "Abstract", None, None, None, 0.74))

        for section in getattr(doc, "sections", []) or []:
            title = _get_attr(section, "section_title") or _get_attr(section, "section_type") or "Unknown Section"
            text = _get_attr(section, "text", "") or ""
            page = _get_attr(section, "page_start") or _get_attr(section, "page_end")
            if text:
                base = 0.84 if self._is_priority_section(title) else 0.68
                results.extend(self._extract_from_text(text, title, page, None, None, base))

        for table in getattr(doc, "tables", []) or []:
            caption = _get_attr(table, "caption", "") or ""
            content = _get_attr(table, "markdown_content", "") or ""
            page = _get_attr(table, "page")
            combined = "\n".join(part for part in [caption, content] if part)
            if combined:
                results.extend(self._extract_from_text(combined, None, page, None, caption[:120] or "Table", 0.7))

        for figure in getattr(doc, "figures", []) or []:
            caption = _get_attr(figure, "caption", "") or ""
            page = _get_attr(figure, "page")
            if caption:
                results.extend(self._extract_from_text(caption, None, page, caption[:120] or "Figure", None, 0.66))

        return self._deduplicate(results)

    def _extract_from_text(
        self,
        text: str,
        section: str | None,
        page: int | None,
        figure: str | None,
        table: str | None,
        base_confidence: float,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for pattern in self.CAPACITY_PATTERNS:
            for match in pattern.finditer(text):
                unit = self._normalize_unit(match.group("unit"))
                value = float(match.group("value"))
                matches.append(self._build_item("capacity", value, unit, text, match, section, page, figure, table, base_confidence + 0.1))

        for pattern in self.SULFUR_LOADING_PATTERNS:
            for match in pattern.finditer(text):
                matches.append(self._build_item("sulfur_loading", float(match.group("value")), self._normalize_unit(match.group("unit")), text, match, section, page, figure, table, base_confidence + 0.08))

        for pattern in self.SULFUR_CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                matches.append(self._build_item("sulfur_content", float(match.group("value")), "wt%", text, match, section, page, figure, table, base_confidence + 0.08))

        for pattern in self.ES_RATIO_PATTERNS:
            for match in pattern.finditer(text):
                unit = self._normalize_unit(match.groupdict().get("unit")) or "uL/mg"
                matches.append(self._build_item("electrolyte_sulfur_ratio", float(match.group("value")), unit, text, match, section, page, figure, table, base_confidence + 0.06))

        for pattern in self.CYCLE_PATTERNS:
            for match in pattern.finditer(text):
                matches.append(self._build_item("cycle_number", float(match.group("value")), None, text, match, section, page, figure, table, base_confidence + 0.02))

        for pattern in self.RATE_PATTERNS:
            for match in pattern.finditer(text):
                rate_value = match.group("value")
                matches.append(self._build_item("rate", None, None, text, match, section, page, figure, table, base_confidence, rate=f"{rate_value}C"))

        for pattern in self.DECAY_PATTERNS:
            for match in pattern.finditer(text):
                matches.append(self._build_item("decay_per_cycle", float(match.group("value")), "%/cycle", text, match, section, page, figure, table, base_confidence + 0.04))
        return matches

    def _build_item(
        self,
        field_name: str,
        value: float | None,
        unit: str | None,
        text: str,
        match: re.Match[str],
        section: str | None,
        page: int | None,
        figure: str | None,
        table: str | None,
        confidence: float,
        rate: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "field_name": field_name,
            "value": value,
            "unit": unit,
            "sulfur_loading_mg_cm2": None,
            "sulfur_content_wt_percent": None,
            "electrolyte_sulfur_ratio": None,
            "capacity_value": None,
            "cycle_number": None,
            "rate": None,
            "decay_per_cycle": None,
            "evidence_text": _context(text, match.start(), match.end()),
            "source_location": {
                "section": section,
                "page": page,
                "figure": figure,
                "table": table,
            },
            "confidence": round(min(confidence, 0.99), 2),
        }
        if field_name == "sulfur_loading":
            item["sulfur_loading_mg_cm2"] = value
        elif field_name == "sulfur_content":
            item["sulfur_content_wt_percent"] = value
        elif field_name == "electrolyte_sulfur_ratio":
            item["electrolyte_sulfur_ratio"] = value
        elif field_name == "capacity":
            item["capacity_value"] = value
        elif field_name == "cycle_number":
            item["cycle_number"] = int(value) if value is not None else None
        elif field_name == "rate":
            item["rate"] = rate
        elif field_name == "decay_per_cycle":
            item["decay_per_cycle"] = value
        return item

    @staticmethod
    def _normalize_unit(unit: str | None) -> str | None:
        if not unit:
            return None
        cleaned = unit.replace(" ", "").replace("µ", "u").replace("μ", "u")
        lowered = cleaned.lower()
        if lowered in {"mah/g", "mahg-1", "mah/g-1"}:
            return "mAh/g"
        if lowered in {"ah/kg", "ahkg-1"}:
            return "Ah/kg"
        if lowered in {"mg/cm2", "mgcm-2"}:
            return "mg/cm2"
        if lowered in {"ul/mg", "ulmg-1", "mul/mg"}:
            return "uL/mg"
        return unit.strip()

    @staticmethod
    def _is_priority_section(title: str | None) -> bool:
        if not title:
            return False
        lowered = title.lower()
        return any(keyword in lowered for keyword in ["electrochemical", "performance", "results", "cycling", "rate capability"])

    @staticmethod
    def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in items:
            key = (
                item["field_name"],
                item.get("value"),
                item.get("unit"),
                item.get("rate"),
                item["source_location"].get("section"),
                item["source_location"].get("page"),
                item["source_location"].get("figure"),
                item["source_location"].get("table"),
            )
            existing = best.get(key)
            if existing is None or item["confidence"] > existing["confidence"]:
                best[key] = item
        return sorted(best.values(), key=lambda row: (row["field_name"], -(row["confidence"] or 0.0)))
