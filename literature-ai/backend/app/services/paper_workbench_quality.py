from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db.models import Paper
from app.utils.workbench_status import WORKBENCH_SCHEMA_VERSION, workflow_status_after_parsing


class PaperWorkbenchQualityMixin:
    """PDF quality assessment helpers for the paper workbench."""

    @classmethod
    def assess_pdf_path(cls, pdf_path: Path, settings: Settings | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        if not pdf_path.exists() or not pdf_path.is_file():
            return cls._quality_report(
                status="Broken",
                score=0.0,
                reason="pdf_file_missing",
                metrics={"file_exists": False, "path": str(pdf_path)},
                parse_allowed=False,
                created_at=now,
            )

        metrics: dict[str, Any] = {
            "file_exists": True,
            "path": str(pdf_path),
            "file_size_bytes": pdf_path.stat().st_size,
        }
        try:
            import fitz

            doc = fitz.open(str(pdf_path))
            try:
                page_count = len(doc)
                text_chars_by_page: list[int] = []
                image_blocks_by_page: list[int] = []
                raster_images_by_page: list[int] = []
                for page in doc:
                    text = page.get_text("text") or ""
                    text_chars_by_page.append(len(text.strip()))
                    try:
                        blocks = page.get_text("dict").get("blocks") or []
                        image_blocks_by_page.append(sum(1 for block in blocks if block.get("type") == 1))
                    except Exception:
                        image_blocks_by_page.append(0)
                    try:
                        raster_images_by_page.append(len(page.get_images(full=True)))
                    except Exception:
                        raster_images_by_page.append(0)
            finally:
                doc.close()
        except Exception as exc:
            return cls._quality_report(
                status="Broken",
                score=0.0,
                reason=f"pdf_open_failed:{type(exc).__name__}",
                metrics={**metrics, "error": str(exc)},
                parse_allowed=False,
                created_at=now,
            )

        total_text_chars = sum(text_chars_by_page)
        text_pages = sum(1 for count in text_chars_by_page if count >= 80)
        image_pages = sum(
            1
            for image_blocks, raster_images in zip(image_blocks_by_page, raster_images_by_page)
            if image_blocks > 0 or raster_images > 0
        )
        avg_text_chars = total_text_chars / max(page_count, 1)
        text_page_ratio = text_pages / max(page_count, 1)
        image_page_ratio = image_pages / max(page_count, 1)
        score = min(1.0, (avg_text_chars / 1200.0) * 0.65 + text_page_ratio * 0.35)
        metrics.update(
            {
                "page_count": page_count,
                "total_text_chars": total_text_chars,
                "avg_text_chars_per_page": round(avg_text_chars, 2),
                "text_pages": text_pages,
                "text_page_ratio": round(text_page_ratio, 4),
                "image_pages": image_pages,
                "image_page_ratio": round(image_page_ratio, 4),
                "text_chars_by_page": text_chars_by_page,
                "image_blocks_by_page": image_blocks_by_page,
                "raster_images_by_page": raster_images_by_page,
            }
        )

        if page_count <= 0:
            status, reason, parse_allowed = "Broken", "pdf_has_no_pages", False
        elif total_text_chars >= max(1800, page_count * 450) and text_page_ratio >= 0.7:
            status, reason, parse_allowed = "A_text_readable", "native_text_is_readable", True
        elif total_text_chars >= max(600, page_count * 120) and text_page_ratio >= 0.35:
            status, reason, parse_allowed = "B_text_partial", "native_text_is_partial", True
        elif image_page_ratio >= 0.5:
            status = "C_scan_clear"
            reason = "scan_or_image_pdf_requires_ocr"
            parse_allowed = False
        else:
            status, reason, parse_allowed = "D_scan_unclear", "too_little_text_or_image_signal", False

        return cls._quality_report(
            status=status,
            score=round(score, 4),
            reason=reason,
            metrics=metrics,
            parse_allowed=parse_allowed,
            created_at=now,
            ocr_enabled=bool(
                getattr(settings, "docling_do_ocr", False)
                or (status == "C_scan_clear" and getattr(settings, "docling_auto_ocr", False))
            ) if settings else False,
        )

    @staticmethod
    def _quality_report(
        *,
        status: str,
        score: float,
        reason: str,
        metrics: dict[str, Any],
        parse_allowed: bool,
        created_at: str,
        ocr_enabled: bool = False,
    ) -> dict[str, Any]:
        initial_parse_allowed = bool(
            parse_allowed and status in {"A_text_readable", "B_text_partial"}
        )
        markdown_trust = {
            "A_text_readable": "high_native_text",
            "B_text_partial": "medium_native_text",
            "C_scan_clear": "ocr_required_candidate",
            "D_scan_unclear": "untrusted",
            "Broken": "unavailable",
        }.get(status, "unknown")
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "created_at": created_at,
            "quality_status": status,
            "quality_score": score,
            "reason": reason,
            "parse_allowed": initial_parse_allowed,
            "needs_human_confirmation": not initial_parse_allowed,
            "markdown_trust": markdown_trust,
            "ocr_policy": {
                "ocr_enabled": bool(ocr_enabled),
                "ocr_required": status == "C_scan_clear",
                "ocr_text_must_be_marked": True,
            },
            "metrics": metrics,
        }

    def apply_quality_report(self, paper: Paper, quality_report: dict[str, Any]) -> None:
        missing_pdf_reference = (
            str(quality_report.get("reason") or "").strip() == "missing_pdf_reference"
            and (not str(getattr(paper, "pdf_path", "") or "").strip() or getattr(paper, "oa_status", None) == "metadata_only")
        )
        if missing_pdf_reference:
            paper.pdf_quality_status = None
            paper.pdf_quality_score = None
            paper.pdf_quality_report = None
            return
        paper.pdf_quality_status = quality_report.get("quality_status")
        paper.pdf_quality_score = quality_report.get("quality_score")
        paper.pdf_quality_report = quality_report
        if quality_report.get("needs_human_confirmation"):
            paper.workflow_status = "Needs_Human_Confirmation"
        elif paper.workflow_status in (None, "", "Imported"):
            paper.workflow_status = "Quality_Checked"

    def mark_parsed_ready(self, paper: Paper, *, candidate_count: int) -> None:
        if paper.workflow_status == "Needs_Human_Confirmation":
            return
        paper.workflow_status = workflow_status_after_parsing(has_candidates=candidate_count > 0)
