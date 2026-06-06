from __future__ import annotations

import re
from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, PaperFigure, PaperSection, PaperTable


DFT_SIGNAL_PATTERNS = (
    r"\bDFT\b",
    r"density\s+functional",
    r"adsorption\s+energ",
    r"binding\s+energ",
    r"\bE\s*[_-]?\s*ads\b",
    r"gibbs",
    r"free\s+energ",
    r"overpotential",
    r"reaction\s+barrier",
    r"energy\s+barrier",
    r"limiting\s+potential",
    r"band\s+gap",
    r"\bHOMO\b",
    r"\bLUMO\b",
    r"bader",
    r"d[-\s]?band",
    r"work\s+function",
    r"charge\s+transfer",
    r"formation\s+energ",
    r"supplementary",
)


STATUS_LABELS = {
    "Unparsed": "未解析",
    "Initial_Parsed": "初步解析",
    "Suspected_Missing": "疑似漏提",
    "AI_Rescanned": "AI 已重扫",
    "Human_Complete": "人工确认完整",
    "DB_Ready": "可入库",
}


class DFTCompletenessAuditor:
    """Audit whether DFT candidate extraction covered the paper evidence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def audit_paper(
        self,
        paper_id: UUID,
        *,
        parsed_count: int | None = None,
        exportable_count: int | None = None,
        blocked_count: int | None = None,
    ) -> dict[str, Any]:
        parsed_count = (
            parsed_count
            if parsed_count is not None
            else int(
                self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)) or 0
            )
        )
        signals = self._collect_signals(paper_id)
        signal_count = len(signals)
        suspected_missing_count = max(0, signal_count - int(parsed_count or 0))
        status = self._coverage_status(
            signal_count=signal_count,
            parsed_count=int(parsed_count or 0),
            suspected_missing_count=suspected_missing_count,
            exportable_count=int(exportable_count or 0),
            blocked_count=int(blocked_count or 0),
        )
        kind_counts = Counter(item["source_type"] for item in signals)
        return {
            "schema_version": "dft_completeness_audit_v1",
            "coverage_status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "detected_signal_count": signal_count,
            "detected_sections": kind_counts.get("section", 0),
            "detected_tables": kind_counts.get("table", 0),
            "detected_figures": kind_counts.get("figure", 0),
            "parsed_dft_count": int(parsed_count or 0),
            "exportable_dft_count": int(exportable_count or 0),
            "blocked_dft_count": int(blocked_count or 0),
            "suspected_missing_count": suspected_missing_count,
            "signal_examples": signals[:8],
            "audit_policy": (
                "Signals are a recall-oriented checklist for AI rescanning; they do not prove a value "
                "exists unless the AI/human reviewer anchors it to PDF evidence."
            ),
        }

    def _collect_signals(self, paper_id: UUID) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper_id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper_id)).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).all()

        for section in sections:
            text = " ".join([section.section_title or "", section.section_type or "", section.text or ""])
            self._append_if_signal(
                signals,
                source_type="section",
                source_id=str(section.id),
                page=section.page_start,
                label=section.section_title or section.section_type,
                text=text,
            )
        for table in tables:
            text = " ".join([table.caption or "", table.markdown_content or ""])
            self._append_if_signal(
                signals,
                source_type="table",
                source_id=str(table.id),
                page=table.page,
                label=table.caption,
                text=text,
            )
        for figure in figures:
            text = " ".join([figure.figure_label or "", figure.caption or "", figure.content_summary or ""])
            self._append_if_signal(
                signals,
                source_type="figure",
                source_id=str(figure.id),
                page=figure.page,
                label=figure.figure_label or figure.caption,
                text=text,
            )
        return signals

    @classmethod
    def _append_if_signal(
        cls,
        signals: list[dict[str, Any]],
        *,
        source_type: str,
        source_id: str,
        page: int | None,
        label: str | None,
        text: str,
    ) -> None:
        hits = cls._keyword_hits(text)
        if not hits:
            return
        signals.append(
            {
                "source_type": source_type,
                "source_id": source_id,
                "page": page,
                "label": cls._shorten(label, 120),
                "keywords": hits,
                "excerpt": cls._shorten(text, 360),
            }
        )

    @staticmethod
    def _keyword_hits(text: str) -> list[str]:
        hits = []
        for pattern in DFT_SIGNAL_PATTERNS:
            if re.search(pattern, text or "", re.IGNORECASE):
                hits.append(pattern.replace("\\b", "").replace("\\s+", " "))
        return hits[:6]

    @staticmethod
    def _coverage_status(
        *,
        signal_count: int,
        parsed_count: int,
        suspected_missing_count: int,
        exportable_count: int,
        blocked_count: int,
    ) -> str:
        if parsed_count <= 0:
            return "Unparsed"
        if suspected_missing_count > 0:
            return "Suspected_Missing"
        if exportable_count > 0 and blocked_count == 0:
            return "DB_Ready"
        if blocked_count == 0 and signal_count > 0:
            return "Human_Complete"
        return "Initial_Parsed"

    @staticmethod
    def _shorten(value: str | None, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."
