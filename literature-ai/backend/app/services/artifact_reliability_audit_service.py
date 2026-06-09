from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import EvidenceLocator, Paper, PaperFigure, PaperTable
from app.utils.figure_reliability import build_figure_image_review, first_bbox
from app.utils.locator_degradation import locator_degradation


class ArtifactReliabilityAuditService:
    """Read-only report for artifact, crop, table, and locator reliability."""

    schema_version = "artifact_reliability_audit_v1"
    example_limit = 5

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def audit_paper(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper_id)).all()
        locators = self.session.scalars(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper_id)).all()

        figure_issue_counts: Counter[str] = Counter()
        table_issue_counts: Counter[str] = Counter()
        locator_issue_counts: Counter[str] = Counter()
        examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for figure in figures:
            review = build_figure_image_review(figure, settings=self.settings, check_asset_exists=True)
            issues = self._figure_issues(figure, review)
            for issue in issues:
                figure_issue_counts[issue] += 1
                self._add_example(
                    examples,
                    issue,
                    {
                        "object_type": "figure",
                        "id": str(figure.id),
                        "page": figure.page,
                        "caption": self._clip(figure.caption, 180),
                        "status": review.get("crop_status") or figure.crop_status,
                        "reason": issue,
                    },
                )

        for table in tables:
            issues = self._table_issues(table)
            for issue in issues:
                table_issue_counts[issue] += 1
                self._add_example(
                    examples,
                    issue,
                    {
                        "object_type": "table",
                        "id": str(table.id),
                        "page": table.page,
                        "caption": self._clip(table.caption, 180),
                        "status": table.extraction_source or "table_candidate",
                        "reason": issue,
                    },
                )

        for locator in locators:
            degradation = locator_degradation(
                page=locator.page,
                locator_status=locator.locator_status,
                evidence_text=locator.evidence_text,
                bbox=locator.bbox,
                warning_reason=locator.warning_reason,
            )
            issues = self._locator_issues(locator, degradation.locator_status)
            for issue in issues:
                locator_issue_counts[issue] += 1
                self._add_example(
                    examples,
                    issue,
                    {
                        "object_type": "locator",
                        "id": str(locator.id),
                        "page": locator.page,
                        "caption": self._clip(locator.evidence_text, 180),
                        "target_type": locator.target_type,
                        "target_id": locator.target_id,
                        "field_name": locator.field_name,
                        "status": degradation.locator_status,
                        "reason": degradation.warning_reason or issue,
                    },
                )

        total_issue_count = sum(figure_issue_counts.values()) + sum(table_issue_counts.values()) + sum(locator_issue_counts.values())
        return {
            "schema_version": self.schema_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "paper_id": str(paper.id),
            "title": paper.title,
            "report_policy": {
                "read_only": True,
                "does_not_modify_parser_outputs": True,
                "does_not_recrop_figures": True,
                "does_not_trust_ocr_or_bbox_automatically": True,
                "does_not_verify_or_approve": True,
            },
            "summary": {
                "status": "needs_review" if total_issue_count else "candidate_reliable",
                "figure_count": len(figures),
                "table_count": len(tables),
                "locator_count": len(locators),
                "total_issue_count": total_issue_count,
            },
            "figure_count": len(figures),
            "table_count": len(tables),
            "locator_count": len(locators),
            "figure_issue_counts": dict(sorted(figure_issue_counts.items())),
            "table_issue_counts": dict(sorted(table_issue_counts.items())),
            "locator_issue_counts": dict(sorted(locator_issue_counts.items())),
            "examples": dict(sorted(examples.items())),
        }

    def audit_library(self, *, limit: int = 100) -> dict[str, Any]:
        papers = self.session.scalars(select(Paper).order_by(Paper.created_at.desc()).limit(limit)).all()
        rows = [self.audit_paper(paper.id) for paper in papers]
        figure_issue_counts: Counter[str] = Counter()
        table_issue_counts: Counter[str] = Counter()
        locator_issue_counts: Counter[str] = Counter()
        for row in rows:
            figure_issue_counts.update(row.get("figure_issue_counts") or {})
            table_issue_counts.update(row.get("table_issue_counts") or {})
            locator_issue_counts.update(row.get("locator_issue_counts") or {})
        return {
            "schema_version": self.schema_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "returned": len(rows),
                "read_only": True,
            },
            "summary": {
                "paper_count": len(rows),
                "figure_issue_counts": dict(sorted(figure_issue_counts.items())),
                "table_issue_counts": dict(sorted(table_issue_counts.items())),
                "locator_issue_counts": dict(sorted(locator_issue_counts.items())),
            },
            "rows": rows,
        }

    @staticmethod
    def _figure_issues(figure: PaperFigure, review: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        flags = set(review.get("flags") or [])
        if "missing_image_path" in flags or "missing_image_file" in flags:
            issues.append("missing_image")
        if (figure.crop_status or "").lower() == "caption_only" or not figure.image_path:
            issues.append("caption_only")
        if "small_crop_or_subfigure" in flags:
            issues.append("small_crop")
        if "extreme_aspect_ratio" in flags:
            issues.append("extreme_aspect_ratio")
        if "missing_parser_bbox" in flags:
            issues.append("missing_bbox")
        if "missing_full_page_snapshot" in flags:
            issues.append("missing_full_page_snapshot")
        if "missing_pdf_page" in flags:
            issues.append("missing_page")
        return list(dict.fromkeys(issues))

    @staticmethod
    def _table_issues(table: PaperTable) -> list[str]:
        issues: list[str] = []
        if table.page is None:
            issues.append("missing_page")
        if first_bbox(table.prov) is None:
            issues.append("missing_bbox")
        if not str(table.caption or "").strip():
            issues.append("missing_caption")
        return issues

    @staticmethod
    def _locator_issues(locator: EvidenceLocator, status: str) -> list[str]:
        issues: list[str] = []
        if status == "text_only":
            issues.append("text_only_locator")
        elif status == "missing_page":
            issues.append("missing_page")
        elif status == "missing_locator":
            issues.append("missing_locator")
        elif status == "approximate":
            issues.append("approximate_locator")
        elif status == "unresolved":
            issues.append("unresolved_locator")
        if status == "exact_page" and locator.bbox is None:
            issues.append("missing_bbox")
        return issues

    @classmethod
    def _add_example(cls, examples: dict[str, list[dict[str, Any]]], issue: str, payload: dict[str, Any]) -> None:
        if len(examples[issue]) < cls.example_limit:
            examples[issue].append(payload)

    @staticmethod
    def _clip(value: Any, max_chars: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rstrip() + "..."
