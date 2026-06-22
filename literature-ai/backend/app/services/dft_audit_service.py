from __future__ import annotations

import re
from collections import Counter
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, PaperFigure, PaperSection, PaperTable
from app.services.dft_rescan_policy import _row_signature


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


DFT_NUMERIC_HEADER_PATTERNS = (
    r"\bE\s*[_-]?\s*ads\b",
    r"\bE\s*[_-]?\s*b\b",
    r"adsorption\s+energ",
    r"binding\s+energ",
    r"free\s+energ",
    r"gibbs",
    r"barrier",
    r"overpotential",
    r"limiting\s+potential",
    r"band\s+gap",
    r"d[-\s]?band",
    r"work\s+function",
    r"formation\s+energ",
    r"\bmu\s*B\b",
    r"\bμ\s*B\b",
    r"\bDelta\s*z\b",
    r"\bΔ\s*z\b",
    r"\bU\b",
)

NUMERIC_WITH_UNIT_PATTERN = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*(?:eV|meV|V|kJ\s*mol\s*-?\s*1|kcal\s*mol\s*-?\s*1|μ\s*B|mu\s*B|Å|Å)",
    re.IGNORECASE,
)

PLAIN_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


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
        numeric_summary = self._numeric_signal_summary(paper_id, include_figures=True)
        text_llm_numeric_summary = self._numeric_signal_summary(paper_id, include_figures=False)
        signal_count = len(signals)
        low_recall = self._low_recall_assessment(
            parsed_count=int(parsed_count or 0),
            signal_count=signal_count,
            numeric_value_count=int(text_llm_numeric_summary["numeric_value_count"]),
        )
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        all_candidates_rejected = self._all_candidates_rejected(
            dft_rows,
            parsed_count=int(parsed_count or 0),
        )
        unique_candidate_count, duplicate_evidence_count = self._dedupe_counts(
            dft_rows,
            fallback_count=int(parsed_count or 0),
        )
        coverage_ratio = self._coverage_ratio(
            unique_candidate_count=unique_candidate_count,
            text_numeric_signal_count=int(text_llm_numeric_summary["numeric_value_count"]),
        )
        rescan_recommended = (
            not all_candidates_rejected
            and coverage_ratio < 0.7
            and int(text_llm_numeric_summary["numeric_value_count"]) >= 10
        )
        suspected_missing_count = 0 if all_candidates_rejected else max(
            0,
            signal_count - int(parsed_count or 0),
            int(low_recall["estimated_missing_count"]),
            int(text_llm_numeric_summary["numeric_value_count"]) - unique_candidate_count if rescan_recommended else 0,
        )
        status = "Human_Complete" if all_candidates_rejected else self._coverage_status(
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
            "coverage_ratio": coverage_ratio,
            "unique_candidate_count": unique_candidate_count,
            "duplicate_evidence_count": duplicate_evidence_count,
            "excluded_numeric_signal_count": 0,
            "rescan_recommended": rescan_recommended,
            "rescan_stop_reason": "all_candidates_rejected" if all_candidates_rejected else None,
            "rescan_next_status": "Needs_IDE_Rescan" if rescan_recommended else None,
            "numeric_signal_summary": numeric_summary,
            "text_llm_numeric_signal_summary": text_llm_numeric_summary,
            "low_recall_warning": False if all_candidates_rejected else bool(low_recall["warning"]),
            "low_recall_reasons": [] if all_candidates_rejected else low_recall["reasons"],
            "llm_rescan_recommended": False,
            "ide_ai_review_recommended": False if all_candidates_rejected else bool(low_recall["warning"] or rescan_recommended),
            "candidate_generation_policy": {
                "web_llm_extract": "disabled",
                "web_llm_scope": "disabled_use_prepare_ai_context_codex_item_import_analysis",
                "image_or_chart_review": "requires_human_or_vlm_not_text_llm",
                "ide_ai_review": "required_before_export_or_ml_ready",
                "verification_boundary": "PDF-anchored review is required; numeric signals alone are not final facts.",
            },
            "signal_examples": signals[:8],
            "audit_policy": (
                "Signals are a recall-oriented checklist for AI rescanning; they do not prove a value "
                "exists unless the AI/human reviewer anchors it to PDF evidence."
            ),
        }

    def audit_papers(
        self,
        paper_ids: set[UUID],
        *,
        parsed_counts: dict[UUID, int] | None = None,
        exportable_counts: dict[UUID, int] | None = None,
        blocked_counts: dict[UUID, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not paper_ids:
            return {}
        parsed_counts = parsed_counts or {}
        exportable_counts = exportable_counts or {}
        blocked_counts = blocked_counts or {}
        signals_by_paper = self._collect_signals_for_papers(paper_ids)
        numeric_summaries = self._numeric_signal_summaries(paper_ids, include_figures=True)
        text_llm_numeric_summaries = self._numeric_signal_summaries(paper_ids, include_figures=False)
        dft_rows_by_paper: dict[UUID, list[DFTResult]] = defaultdict(list)
        for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(paper_ids))).all():
            dft_rows_by_paper[row.paper_id].append(row)
        audits: dict[str, dict[str, Any]] = {}
        for paper_id in paper_ids:
            parsed_count = int(parsed_counts.get(paper_id, 0) or 0)
            exportable_count = int(exportable_counts.get(paper_id, 0) or 0)
            blocked_count = int(blocked_counts.get(paper_id, 0) or 0)
            signals = signals_by_paper.get(paper_id, [])
            numeric_summary = numeric_summaries.get(paper_id, self._empty_numeric_summary())
            text_llm_numeric_summary = text_llm_numeric_summaries.get(paper_id, self._empty_numeric_summary())
            signal_count = len(signals)
            low_recall = self._low_recall_assessment(
                parsed_count=parsed_count,
                signal_count=signal_count,
                numeric_value_count=int(text_llm_numeric_summary["numeric_value_count"]),
            )
            unique_candidate_count, duplicate_evidence_count = self._dedupe_counts(
                dft_rows_by_paper.get(paper_id, []),
                fallback_count=parsed_count,
            )
            all_candidates_rejected = self._all_candidates_rejected(
                dft_rows_by_paper.get(paper_id, []),
                parsed_count=parsed_count,
            )
            coverage_ratio = self._coverage_ratio(
                unique_candidate_count=unique_candidate_count,
                text_numeric_signal_count=int(text_llm_numeric_summary["numeric_value_count"]),
            )
            rescan_recommended = (
                not all_candidates_rejected
                and coverage_ratio < 0.7
                and int(text_llm_numeric_summary["numeric_value_count"]) >= 10
            )
            suspected_missing_count = 0 if all_candidates_rejected else max(
                0,
                signal_count - parsed_count,
                int(low_recall["estimated_missing_count"]),
                int(text_llm_numeric_summary["numeric_value_count"]) - unique_candidate_count if rescan_recommended else 0,
            )
            status = "Human_Complete" if all_candidates_rejected else self._coverage_status(
                signal_count=signal_count,
                parsed_count=parsed_count,
                suspected_missing_count=suspected_missing_count,
                exportable_count=exportable_count,
                blocked_count=blocked_count,
            )
            kind_counts = Counter(item["source_type"] for item in signals)
            audits[str(paper_id)] = {
                "schema_version": "dft_completeness_audit_v1",
                "coverage_status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "detected_signal_count": signal_count,
                "detected_sections": kind_counts.get("section", 0),
                "detected_tables": kind_counts.get("table", 0),
                "detected_figures": kind_counts.get("figure", 0),
                "parsed_dft_count": parsed_count,
                "exportable_dft_count": exportable_count,
                "blocked_dft_count": blocked_count,
                "suspected_missing_count": suspected_missing_count,
                "coverage_ratio": coverage_ratio,
                "unique_candidate_count": unique_candidate_count,
                "duplicate_evidence_count": duplicate_evidence_count,
                "excluded_numeric_signal_count": 0,
                "rescan_recommended": rescan_recommended,
                "rescan_stop_reason": "all_candidates_rejected" if all_candidates_rejected else None,
                "rescan_next_status": "Needs_IDE_Rescan" if rescan_recommended else None,
                "numeric_signal_summary": numeric_summary,
                "text_llm_numeric_signal_summary": text_llm_numeric_summary,
                "low_recall_warning": False if all_candidates_rejected else bool(low_recall["warning"]),
                "low_recall_reasons": [] if all_candidates_rejected else low_recall["reasons"],
                "llm_rescan_recommended": False,
                "ide_ai_review_recommended": False if all_candidates_rejected else bool(low_recall["warning"] or rescan_recommended),
                "candidate_generation_policy": {
                    "web_llm_extract": "disabled",
                    "web_llm_scope": "disabled_use_prepare_ai_context_codex_item_import_analysis",
                    "image_or_chart_review": "requires_human_or_vlm_not_text_llm",
                    "ide_ai_review": "required_before_export_or_ml_ready",
                    "verification_boundary": "PDF-anchored review is required; numeric signals alone are not final facts.",
                },
                "signal_examples": signals[:8],
                "audit_policy": (
                    "Signals are a recall-oriented checklist for AI rescanning; they do not prove a value "
                    "exists unless the AI/human reviewer anchors it to PDF evidence."
                ),
            }
        return audits

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

    def _collect_signals_for_papers(self, paper_ids: set[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        signals_by_paper: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id.in_(paper_ids))).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(paper_ids))).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(paper_ids))).all()

        for section in sections:
            text = " ".join([section.section_title or "", section.section_type or "", section.text or ""])
            self._append_if_signal(
                signals_by_paper[section.paper_id],
                source_type="section",
                source_id=str(section.id),
                page=section.page_start,
                label=section.section_title or section.section_type,
                text=text,
            )
        for table in tables:
            text = " ".join([table.caption or "", table.markdown_content or ""])
            self._append_if_signal(
                signals_by_paper[table.paper_id],
                source_type="table",
                source_id=str(table.id),
                page=table.page,
                label=table.caption,
                text=text,
            )
        for figure in figures:
            text = " ".join([figure.figure_label or "", figure.caption or "", figure.content_summary or ""])
            self._append_if_signal(
                signals_by_paper[figure.paper_id],
                source_type="figure",
                source_id=str(figure.id),
                page=figure.page,
                label=figure.figure_label or figure.caption,
                text=text,
            )
        return signals_by_paper

    def _numeric_signal_summary(self, paper_id: UUID, *, include_figures: bool) -> dict[str, Any]:
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper_id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper_id)).all()
        figures = (
            self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).all()
            if include_figures
            else []
        )
        return self._numeric_signal_summary_from_sources(sections=sections, tables=tables, figures=figures)

    def _numeric_signal_summaries(self, paper_ids: set[UUID], *, include_figures: bool) -> dict[UUID, dict[str, Any]]:
        if not paper_ids:
            return {}
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id.in_(paper_ids))).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(paper_ids))).all()
        figures = (
            self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(paper_ids))).all()
            if include_figures
            else []
        )
        grouped_sections: dict[UUID, list[PaperSection]] = defaultdict(list)
        grouped_tables: dict[UUID, list[PaperTable]] = defaultdict(list)
        grouped_figures: dict[UUID, list[PaperFigure]] = defaultdict(list)
        for section in sections:
            grouped_sections[section.paper_id].append(section)
        for table in tables:
            grouped_tables[table.paper_id].append(table)
        for figure in figures:
            grouped_figures[figure.paper_id].append(figure)
        return {
            paper_id: self._numeric_signal_summary_from_sources(
                sections=grouped_sections.get(paper_id, []),
                tables=grouped_tables.get(paper_id, []),
                figures=grouped_figures.get(paper_id, []),
            )
            for paper_id in paper_ids
        }

    @classmethod
    def _numeric_signal_summary_from_sources(
        cls,
        *,
        sections: list[PaperSection],
        tables: list[PaperTable],
        figures: list[PaperFigure],
    ) -> dict[str, Any]:
        summary = cls._empty_numeric_summary()
        for section in sections:
            text = " ".join([section.section_title or "", section.section_type or "", section.text or ""])
            cls._append_numeric_source(
                summary,
                source_type="section",
                source_id=str(section.id),
                page=section.page_start,
                label=section.section_title or section.section_type,
                text=text,
            )
        for table in tables:
            text = " ".join([table.caption or "", table.markdown_content or ""])
            cls._append_numeric_source(
                summary,
                source_type="table",
                source_id=str(table.id),
                page=table.page,
                label=table.caption,
                text=text,
                markdown_content=table.markdown_content,
            )
        for figure in figures:
            text = " ".join([figure.figure_label or "", figure.caption or "", figure.content_summary or ""])
            cls._append_numeric_source(
                summary,
                source_type="figure",
                source_id=str(figure.id),
                page=figure.page,
                label=figure.figure_label or figure.caption,
                text=text,
            )
        return summary

    @classmethod
    def _append_numeric_source(
        cls,
        summary: dict[str, Any],
        *,
        source_type: str,
        source_id: str,
        page: int | None,
        label: str | None,
        text: str,
        markdown_content: str | None = None,
    ) -> None:
        keyword_hits = cls._keyword_hits(text)
        header_hits = cls._numeric_header_hits(text)
        unit_value_count = len(NUMERIC_WITH_UNIT_PATTERN.findall(text or ""))
        table_value_count = cls._count_markdown_numeric_table_values(markdown_content or "")
        value_count = max(unit_value_count, table_value_count)
        if value_count <= 0 or (not keyword_hits and not header_hits):
            return
        summary["numeric_value_count"] += value_count
        summary["sources_with_numeric_signals"] += 1
        summary["source_counts"][source_type] += 1
        if len(summary["examples"]) < 8:
            summary["examples"].append(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "page": page,
                    "label": cls._shorten(label, 120),
                    "numeric_value_count": value_count,
                    "keywords": keyword_hits[:6],
                    "numeric_headers": header_hits[:6],
                    "excerpt": cls._shorten(text, 360),
                }
            )

    @staticmethod
    def _empty_numeric_summary() -> dict[str, Any]:
        return {
            "numeric_value_count": 0,
            "sources_with_numeric_signals": 0,
            "source_counts": Counter(),
            "examples": [],
        }

    @staticmethod
    def _numeric_header_hits(text: str) -> list[str]:
        hits = []
        for pattern in DFT_NUMERIC_HEADER_PATTERNS:
            if re.search(pattern, text or "", re.IGNORECASE):
                hits.append(pattern.replace("\\b", "").replace("\\s+", " "))
        return hits[:8]

    @classmethod
    def _count_markdown_numeric_table_values(cls, markdown: str) -> int:
        rows = cls._markdown_rows(markdown)
        if len(rows) < 2:
            return 0
        header_index = 0
        headers = rows[header_index]
        numeric_columns = [
            index
            for index, header in enumerate(headers)
            if cls._numeric_header_hits(header) or re.search(r"/\s*(?:eV|meV|V|Å|Å)", header or "", re.IGNORECASE)
        ]
        if not numeric_columns:
            return len(NUMERIC_WITH_UNIT_PATTERN.findall(markdown or ""))
        count = 0
        for row in rows[1:]:
            if cls._is_separator_row(row):
                continue
            for index in numeric_columns:
                if index < len(row) and PLAIN_NUMBER_PATTERN.search(row[index] or ""):
                    count += 1
        return count

    @staticmethod
    def _markdown_rows(markdown: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for line in (markdown or "").splitlines():
            stripped = line.strip()
            if "|" not in stripped:
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells:
                rows.append(cells)
        return rows

    @staticmethod
    def _is_separator_row(row: list[str]) -> bool:
        return all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in row)

    @staticmethod
    def _low_recall_assessment(*, parsed_count: int, signal_count: int, numeric_value_count: int) -> dict[str, Any]:
        reasons: list[str] = []
        if parsed_count <= 1 and numeric_value_count >= 5:
            reasons.append(
                "parsed_dft_count is 0 or 1 while extracted paper content contains multiple DFT numeric values"
            )
        if parsed_count > 1 and numeric_value_count >= max(12, parsed_count * 3):
            reasons.append("DFT numeric evidence count is much higher than parsed DFT candidates")
        if signal_count > parsed_count and numeric_value_count > 0:
            reasons.append("DFT keyword signals outnumber parsed DFT candidates and include numeric evidence")
        estimated_missing_count = max(0, numeric_value_count - parsed_count) if reasons else 0
        return {
            "warning": bool(reasons),
            "reasons": reasons,
            "estimated_missing_count": estimated_missing_count,
        }

    @staticmethod
    def _coverage_ratio(*, unique_candidate_count: int, text_numeric_signal_count: int) -> float:
        return round(float(unique_candidate_count) / max(int(text_numeric_signal_count or 0), 1), 4)

    @staticmethod
    def _dedupe_counts(rows: list[DFTResult], *, fallback_count: int) -> tuple[int, int]:
        if not rows:
            return int(fallback_count or 0), 0
        signatures = [_row_signature(row) for row in rows]
        unique_count = len(set(signatures))
        supporting_count = 0
        for row in rows:
            payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            supporting = payload.get("supporting_evidence") if isinstance(payload, dict) else None
            if isinstance(supporting, list):
                supporting_count += len(supporting)
        duplicate_count = max(0, len(signatures) - unique_count) + supporting_count
        return unique_count, duplicate_count

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
    def _all_candidates_rejected(rows: list[DFTResult], *, parsed_count: int) -> bool:
        return (
            parsed_count > 0
            and len(rows) == parsed_count
            and all(str(row.candidate_status or "").strip().lower() == "rejected" for row in rows)
        )

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
