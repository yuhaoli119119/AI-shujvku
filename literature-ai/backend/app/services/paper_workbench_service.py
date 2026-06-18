from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    MechanismClaim,
    Paper,
    PaperFigure,
    PaperNote,
    PaperRelationship,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_rescan_policy import (
    build_dft_dedupe_signature,
    normalize_numeric_value,
    normalize_source_document_type,
    normalize_unit,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_codes import ensure_paper_codes
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.artifact_status import build_paper_pdf_status
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.workbench_status import (
    EXTRACTION_PROTOCOL_VERSION,
    WORKBENCH_SCHEMA_VERSION,
    workflow_needs_human_confirmation,
    workflow_status_after_parsing,
)
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results
from app.utils.protocol_tracking import protocol_snapshot


FINALIZED_DFT_CANDIDATE_STATUSES = {
    "ML_Ready",
    "Rejected",
    "human_reviewed_needs_evidence",
    "Gemini_Verified",
    "Human_Confirmed",
    "Citation_Ready",
    "verified",
    "human_verified",
}


class PaperWorkbenchService:
    """Build the Codex-centered, evidence-first workspace for each paper."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

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
            ocr_enabled = bool(getattr(settings, "docling_do_ocr", False)) if settings else False
            status = "C_scan_clear"
            reason = "scan_or_image_pdf_requires_ocr"
            parse_allowed = ocr_enabled
        else:
            status, reason, parse_allowed = "D_scan_unclear", "too_little_text_or_image_signal", False

        return cls._quality_report(
            status=status,
            score=round(score, 4),
            reason=reason,
            metrics=metrics,
            parse_allowed=parse_allowed,
            created_at=now,
            ocr_enabled=bool(getattr(settings, "docling_do_ocr", False)) if settings else False,
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
            "parse_allowed": bool(parse_allowed),
            "needs_human_confirmation": not bool(parse_allowed),
            "markdown_trust": markdown_trust,
            "ocr_policy": {
                "ocr_enabled": bool(ocr_enabled),
                "ocr_required": status == "C_scan_clear",
                "ocr_text_must_be_marked": True,
            },
            "metrics": metrics,
        }

    def prepare_paper_workspace(self, paper_id: UUID, *, render_pages: bool = False) -> dict[str, Any]:
        return self._run_exclusive_prepare(
            paper_id,
            lambda: self._prepare_paper_workspace_unlocked(paper_id, render_pages=render_pages),
        )

    def _prepare_paper_workspace_unlocked(self, paper_id: UUID, *, render_pages: bool = False) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        ensure_paper_codes(self.session, [paper])
        pdf_path = self._paper_pdf_path(paper)
        quality_report = paper.pdf_quality_report
        if not isinstance(quality_report, dict) or not quality_report.get("quality_status"):
            quality_report = (
                self.assess_pdf_path(pdf_path, self.settings)
                if pdf_path is not None
                else self._quality_report(
                    status="Broken",
                    score=0.0,
                    reason="missing_pdf_reference",
                    metrics={"file_exists": False, "path": paper.pdf_path},
                    parse_allowed=False,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        self.apply_quality_report(paper, quality_report)
        workspace_root = self._workspace_root(paper.id)
        staging_root = workspace_root.with_name(f".{workspace_root.name}.staging-{uuid4().hex}")
        backup_root = workspace_root.with_name(f".{workspace_root.name}.backup-{uuid4().hex}")
        swapped = False
        try:
            dirs = self._ensure_workspace_dirs(staging_root)
            self._copy_source_pdf(pdf_path, staging_root)
            self._write_json(staging_root / "metadata.json", self._paper_metadata(paper))
            self._write_json(staging_root / "quality_report.json", quality_report)
            self._write_markdown_copy(paper, dirs["markdown"])
            self._write_docling_copy(paper, dirs["extraction"])
            self._write_evidence_files(paper, dirs)
            self._write_ai_reading_package(paper, dirs)
            self._write_extraction_files(paper, dirs)
            self._write_audit_files(paper, dirs)
            if render_pages and pdf_path is not None and quality_report.get("parse_allowed"):
                self._render_page_previews(pdf_path, dirs["pages"])
            if workspace_root.exists():
                workspace_root.replace(backup_root)
            staging_root.replace(workspace_root)
            swapped = True
        except Exception:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            if backup_root.exists() and not workspace_root.exists():
                backup_root.replace(workspace_root)
            raise
        paper.workspace_path = self._workspace_ref(workspace_root)
        if paper.workflow_status in (None, "", "Imported"):
            paper.workflow_status = "Quality_Checked"
        self.session.add(
            AuditLog(
                paper_id=paper.id,
                action="prepare_codex_workspace",
                source="codex_workbench",
                target_type="paper",
                target_id=str(paper.id),
                payload={
                    "workspace_path": paper.workspace_path,
                    "quality_status": paper.pdf_quality_status,
                    "parse_allowed": quality_report.get("parse_allowed"),
                    "schema_version": WORKBENCH_SCHEMA_VERSION,
                },
            )
        )
        self.session.add(paper)
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            if swapped and workspace_root.exists():
                shutil.rmtree(workspace_root, ignore_errors=True)
            if backup_root.exists():
                backup_root.replace(workspace_root)
            raise
        if backup_root.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
        self.session.refresh(paper)
        return self.workspace_summary(paper.id)

    def _run_exclusive_prepare(self, paper_id: UUID, callback) -> dict[str, Any]:
        owner = f"paper_operation:prepare_workspace:{uuid4().hex}"
        locks = ModuleWriteLockService(self.session)
        try:
            lock = locks.acquire(
                paper_id=paper_id,
                module_name="all_non_dft",
                locked_by=owner,
                ttl_minutes=60,
                meta={"operation": "prepare_workspace", "internal_operation_lock": True},
            )
            self.session.commit()
        except ValueError as exc:
            self.session.rollback()
            raise ValueError(f"paper_operation_conflict:prepare_workspace:{paper_id}:{exc}") from exc
        try:
            return callback()
        except Exception:
            self.session.rollback()
            raise
        finally:
            try:
                locks.release(lock_token=lock.lock_token, released_by=owner)
                self.session.commit()
            except Exception:
                self.session.rollback()

    def apply_quality_report(self, paper: Paper, quality_report: dict[str, Any]) -> None:
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

    def workspace_summary(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        workspace_root = self._workspace_root(paper.id)
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper.id)).all()
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        locators = self.session.scalars(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper.id)).all()
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "paper_id": str(paper.id),
            "paper_code": getattr(paper, "paper_code", None),
            "title": paper.title,
            "workflow_status": paper.workflow_status,
            "pdf_quality_status": paper.pdf_quality_status,
            "pdf_quality_score": paper.pdf_quality_score,
            "pdf_quality_report": paper.pdf_quality_report,
            "workspace_path": paper.workspace_path or self._workspace_ref(workspace_root),
            "workspace_abs_path": str(workspace_root.resolve()),
            "exists": workspace_root.exists(),
            "counts": {
                "figures": len(figures),
                "dft_candidates": len(dft_rows),
                "evidence_locators": len(locators),
            },
            "figure_crop_status_counts": dict(Counter(row.crop_status or "unknown" for row in figures)),
            "dft_candidate_status_counts": dict(Counter(row.candidate_status or "unknown" for row in dft_rows)),
        }

    def review_center(
        self,
        *,
        limit: int = 100,
        sort_by: str = "recent",
        library_name: str | None = None,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        paper_stmt = select(Paper)
        normalized_library = normalize_library_name(library_name) if library_name is not None else None
        if normalized_library:
            paper_stmt = paper_stmt.where(build_library_name_clause(Paper.library_name, normalized_library))
        papers = self.session.scalars(paper_stmt).all()
        if ensure_paper_codes(self.session, papers):
            self.session.commit()
        paper_ids = {paper.id for paper in papers}
        rows = []
        status_counts: Counter[str] = Counter()
        quality_counts: Counter[str] = Counter()
        auditor = None if summary_only else DFTCompletenessAuditor(self.session)
        reliability_auditor = None if summary_only else ArtifactReliabilityAuditService(self.session, self.settings)
        conflict_service = ReviewConflictAggregationService(self.session)
        conflict_total_counts = conflict_service.count_conflicts_by_paper(paper_ids)
        conflict_counts = ReviewAdjudicationService(self.session).count_actionable_conflicts_by_paper(paper_ids)
        dft_rows_by_paper: dict[UUID, list[DFTResult]] = {paper_id: [] for paper_id in paper_ids}
        for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(paper_ids))).all() if paper_ids else []:
            dft_rows_by_paper.setdefault(row.paper_id, []).append(row)
        figure_rows_by_paper: dict[UUID, list[PaperFigure]] = {paper_id: [] for paper_id in paper_ids}
        for figure in self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(paper_ids))).all() if paper_ids else []:
            figure_rows_by_paper.setdefault(figure.paper_id, []).append(figure)
        table_counts = {
            paper_id: count
            for paper_id, count in (
                self.session.execute(
                    select(PaperTable.paper_id, func.count(PaperTable.id))
                    .where(PaperTable.paper_id.in_(paper_ids))
                    .group_by(PaperTable.paper_id)
                ).all()
                if paper_ids
                else []
            )
        }
        evidence_counts = {
            paper_id: count
            for paper_id, count in (
                self.session.execute(
                    select(EvidenceLocator.paper_id, func.count(EvidenceLocator.id))
                    .where(EvidenceLocator.paper_id.in_(paper_ids))
                    .group_by(EvidenceLocator.paper_id)
                ).all()
                if paper_ids
                else []
            )
        }
        candidates_by_paper: dict[UUID, list[ExternalAnalysisCandidate]] = {paper_id: [] for paper_id in paper_ids}
        candidate_types = {"external_audit_opinion", "object_review_audit"}
        for candidate in (
            self.session.scalars(
                select(ExternalAnalysisCandidate)
                .where(ExternalAnalysisCandidate.paper_id.in_(paper_ids))
                .where(ExternalAnalysisCandidate.candidate_type.in_(candidate_types))
                .order_by(ExternalAnalysisCandidate.created_at.desc())
            ).all()
            if paper_ids
            else []
        ):
            candidates_by_paper.setdefault(candidate.paper_id, []).append(candidate)
        notes_by_paper: dict[UUID, list[PaperNote]] = {paper_id: [] for paper_id in paper_ids}
        for note in (
            self.session.scalars(
                select(PaperNote)
                .where(PaperNote.paper_id.in_(paper_ids))
                .order_by(PaperNote.created_at.desc())
            ).all()
            if paper_ids
            else []
        ):
            notes_by_paper.setdefault(note.paper_id, []).append(note)
        locator_reliability_by_paper = (
            {} if summary_only or reliability_auditor is None else reliability_auditor.paper_locator_reliability_summaries(paper_ids)
        )
        figure_reliability_by_paper = (
            {}
            if summary_only or reliability_auditor is None
            else reliability_auditor.paper_figure_reliability_summaries(
                paper_ids,
                check_asset_exists=False,
            )
        )
        all_dft_rows = [row for rows_for_paper in dft_rows_by_paper.values() for row in rows_for_paper]
        gate_by_id = {} if summary_only else bulk_export_gate_results(self.session, all_dft_rows, target_type="dft_results")
        exportable_counts: dict[UUID, int] = {}
        blocked_counts: dict[UUID, int] = {}
        for paper_id, rows_for_paper in dft_rows_by_paper.items():
            exportable = sum(1 for row in rows_for_paper if gate_by_id.get(str(row.id)) and gate_by_id[str(row.id)].eligible)
            exportable_counts[paper_id] = exportable
            blocked_counts[paper_id] = max(0, len(rows_for_paper) - exportable)
        dft_audits = (
            {}
            if summary_only or auditor is None
            else auditor.audit_papers(
                paper_ids,
                parsed_counts={paper_id: len(rows_for_paper) for paper_id, rows_for_paper in dft_rows_by_paper.items()},
                exportable_counts=exportable_counts,
                blocked_counts=blocked_counts,
            )
        )
        for paper in papers:
            status_counts[paper.workflow_status or "Imported"] += 1
            quality_counts[paper.pdf_quality_status or "unknown"] += 1
            dft_rows = dft_rows_by_paper.get(paper.id, [])
            dft_count = len(dft_rows)
            active_dft_count = self._count_active_dft_candidates(dft_rows)
            figures = figure_rows_by_paper.get(paper.id, [])
            figure_count = len(figures)
            table_count = table_counts.get(paper.id, 0)
            evidence_count = evidence_counts.get(paper.id, 0)
            paper_candidates = candidates_by_paper.get(paper.id, [])
            external_audit_candidates = [
                candidate for candidate in paper_candidates if candidate.candidate_type == "external_audit_opinion"
            ]
            object_review_candidates = [
                candidate for candidate in paper_candidates if candidate.candidate_type == "object_review_audit"
            ]
            external_audit_source_counts: Counter[str] = Counter()
            external_audit_opinions: list[dict[str, Any]] = []
            for candidate in external_audit_candidates:
                payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
                source = str(payload.get("source") or "unknown")
                external_audit_source_counts[source] += 1
                external_audit_opinions.append(
                    {
                        "candidate_id": str(candidate.id),
                        "candidate_type": candidate.candidate_type,
                        "status": candidate.status,
                        "source": source,
                        "verdict": payload.get("verdict"),
                        "recommended_action": payload.get("recommended_action"),
                        "verification_status": payload.get("verification_status", "unverified"),
                        "normalized_payload": payload,
                        "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
                    }
                )
            object_review_source_counts: Counter[str] = Counter()
            object_review_audits: list[dict[str, Any]] = []
            for candidate in object_review_candidates:
                payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
                source = str(payload.get("source") or "unknown")
                object_review_source_counts[source] += 1
                if len(object_review_audits) >= 5:
                    continue
                object_review_audits.append(
                    {
                        "candidate_id": str(candidate.id),
                        "candidate_type": candidate.candidate_type,
                        "status": candidate.status,
                        "target_type": payload.get("target_type"),
                        "target_id": payload.get("target_id"),
                        "field_name": payload.get("field_name"),
                        "source": source,
                        "source_label": payload.get("source_label"),
                        "agent_role": payload.get("agent_role"),
                        "model_name": payload.get("model_name"),
                        "decision": payload.get("decision") or payload.get("verdict"),
                        "recommended_action": payload.get("recommended_action"),
                        "verification_status": payload.get("verification_status", "unverified"),
                        "confidence": (
                            payload.get("confidence")
                            if payload.get("confidence") is not None
                            else candidate.confidence
                        ),
                        "reason": payload.get("reason") or payload.get("reviewer_note") or payload.get("summary"),
                        "evidence_checked": payload.get("evidence_checked"),
                        "evidence_location": payload.get("evidence_location"),
                        "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
                    }
                )
            latest_notes = [
                {
                    "id": str(note.id),
                    "source": note.source,
                    "field_name": note.field_name,
                    "page": note.page,
                    "section_title": note.section_title,
                    "content": note.content,
                    "created_at": note.created_at.isoformat() if note.created_at else None,
                }
                for note in (notes_by_paper.get(paper.id) or [])[:3]
            ]
            quality_report = paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {}
            needs_human_confirmation = workflow_needs_human_confirmation(paper.workflow_status, quality_report)
            figure_crop_status_counts = dict(
                Counter(
                    self._figure_crop_payload(figure)["crop_status"]
                    for figure in figures
                )
            )
            unreliable_figure_count = sum(
                figure_crop_status_counts.get(status, 0)
                for status in ("needs_recrop", "caption_only", "needs_review")
            )
            dft_candidate_status_counts = dict(Counter(row.candidate_status or "system_candidate" for row in dft_rows))
            exportable_count = exportable_counts.get(paper.id, 0)
            blocked_count = blocked_counts.get(paper.id, 0)
            dft_audit = dft_audits.get(str(paper.id)) or self._lightweight_dft_audit(
                paper,
                parsed_count=dft_count,
                exportable_count=exportable_count,
                blocked_count=blocked_count,
            )
            locator_reliability = locator_reliability_by_paper.get(str(paper.id)) or {
                "status": "reliable",
                "locator_count": 0,
                "issue_count": 0,
                "issue_counts": {},
                "top_issues": [],
            }
            figure_reliability = figure_reliability_by_paper.get(str(paper.id)) or {
                "status": "reliable",
                "figure_count": 0,
                "issue_count": 0,
                "issue_counts": {},
                "top_issues": [],
            }
            pdf_status = build_paper_pdf_status(paper, settings=self.settings)
            manual_review_progress = self._manual_review_progress(paper.comprehensive_analysis)
            comprehensive_analysis = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}
            parsed_analysis = {key: value for key, value in comprehensive_analysis.items() if key != "manual_review_progress"}
            has_parsed_content = bool(
                paper.abstract
                or parsed_analysis
                or dft_count
                or figure_count
                or table_count
                or evidence_count
            )
            rows.append(
                {
                    "paper_id": str(paper.id),
                    "paper_code": getattr(paper, "paper_code", None),
                    "paper_short_id": getattr(paper, "paper_code", None) or str(paper.id).split("-")[-1],
                    "created_at": paper.created_at.isoformat() if paper.created_at else None,
                    "title": paper.title,
                    "doi": paper.doi,
                    "year": paper.year,
                    "journal": paper.journal,
                    "workflow_status": paper.workflow_status,
                    "pdf_quality_status": paper.pdf_quality_status,
                    "pdf_quality_score": paper.pdf_quality_score,
                    "quality_reason": quality_report.get("reason"),
                    "pdf_artifact_status": pdf_status,
                    "pdf_exists": bool(pdf_status.get("pdf_exists")),
                    "pdf_file_size": pdf_status.get("pdf_file_size"),
                    "pdf_path_kind": pdf_status.get("pdf_path_kind"),
                    "has_parsed_content": has_parsed_content,
                    "manual_review_progress": manual_review_progress,
                    "pdf_url": f"/api/papers/{paper.id}/pdf" if pdf_status.get("pdf_exists") else None,
                    "needs_human_confirmation": needs_human_confirmation,
                    "has_dft_candidates": dft_count > 0,
                    "has_active_dft_candidates": active_dft_count > 0,
                    "active_dft_candidate_count": active_dft_count,
                    "dft_candidate_count": dft_count,
                    "dft_candidate_status_counts": dft_candidate_status_counts,
                    "dft_audit": dft_audit,
                    "dft_completeness_status": dft_audit["coverage_status"],
                    "dft_completeness_label": dft_audit["status_label"],
                    "suspected_missing_dft_count": dft_audit["suspected_missing_count"],
                    "figure_count": figure_count,
                    "figure_crop_status_counts": figure_crop_status_counts,
                    "unreliable_figure_count": unreliable_figure_count,
                    "figure_reliability": figure_reliability,
                    "figure_issue_count": figure_reliability["issue_count"],
                    "figure_issue_counts": figure_reliability["issue_counts"],
                    "top_figure_issues": figure_reliability["top_issues"],
                    "table_count": table_count,
                    "evidence_count": evidence_count,
                    "locator_reliability": locator_reliability,
                    "locator_issue_count": locator_reliability["issue_count"],
                    "locator_issue_counts": locator_reliability["issue_counts"],
                    "top_locator_issues": locator_reliability["top_issues"],
                    "external_audit_count": len(external_audit_candidates),
                    "external_audit_source_counts": dict(sorted(external_audit_source_counts.items())),
                    "external_audit_opinions": external_audit_opinions,
                    "object_review_audit_count": len(object_review_candidates),
                    "object_review_audit_source_counts": dict(sorted(object_review_source_counts.items())),
                    "object_review_audits": object_review_audits,
                    "paper_note_count": len(notes_by_paper.get(paper.id) or []),
                    "latest_paper_notes": latest_notes,
                    "review_conflict_count": conflict_counts.get(str(paper.id), 0),
                    "review_conflict_total_count": conflict_total_counts.get(str(paper.id), 0),
                    "workspace_path": paper.workspace_path,
                    "detail_url": f"../literature_library/index.html?paper_id={paper.id}&tab=review",
                    "dft_review_queue_url": f"../review_center/index.html?paper_id={paper.id}",
                }
            )
        sorted_rows = self._sort_review_center_rows(rows, sort_by=sort_by)
        total_rows = len(sorted_rows)
        rows = sorted_rows[:limit]
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "metadata": {
                "returned": len(rows),
                "total": total_rows,
                "limit": limit,
                "has_more": total_rows > len(rows),
                "sort_by": sort_by,
                "library_name": normalized_library,
                "status_counts": dict(sorted(status_counts.items())),
                "quality_counts": dict(sorted(quality_counts.items())),
            },
            "rows": rows,
        }

    @staticmethod
    def _is_active_dft_candidate(status: Any) -> bool:
        normalized = str(status or "system_candidate").strip()
        return normalized not in FINALIZED_DFT_CANDIDATE_STATUSES

    @classmethod
    def _count_active_dft_candidates(cls, rows: list[DFTResult]) -> int:
        if not rows:
            return 0
        finalized_signatures = {
            cls._dft_dedupe_signature(row)
            for row in rows
            if not cls._is_active_dft_candidate(row.candidate_status)
        }
        finalized_shadow_keys = {
            cls._dft_shadow_key(row)
            for row in rows
            if not cls._is_active_dft_candidate(row.candidate_status)
        }
        count = 0
        for row in rows:
            if not cls._is_active_dft_candidate(row.candidate_status):
                continue
            if cls._dft_dedupe_signature(row) in finalized_signatures:
                continue
            shadow_key = cls._dft_shadow_key(row)
            if shadow_key is not None and shadow_key in finalized_shadow_keys:
                continue
            count += 1
        return count

    @staticmethod
    def _dft_dedupe_signature(row: DFTResult) -> str:
        payload = dict(row.evidence_payload) if isinstance(row.evidence_payload, dict) else {}
        payload.update(
            {
                "paper_id": row.paper_id,
                "adsorbate": row.adsorbate,
                "property_type": row.property_type,
                "value": row.value,
                "unit": row.unit,
                "reaction_step": row.reaction_step,
            }
        )
        return str(payload.get("dedupe_signature") or build_dft_dedupe_signature(payload))

    @staticmethod
    def _dft_shadow_key(row: DFTResult) -> tuple[str, str, str, str, int] | None:
        payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        anchor = payload.get("material_binding", {}).get("evidence_anchor")
        if not isinstance(anchor, dict):
            anchor = {}
        page = payload.get("page")
        if page in (None, ""):
            page = anchor.get("page")
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            return None
        source_type = normalize_source_document_type(
            payload.get("source_document_type") or anchor.get("source_document_type")
        )
        source_bucket = "supporting_reference" if source_type == "supporting_reference" else "paper_owned"
        return (
            source_bucket,
            " ".join(str(row.property_type or "").strip().lower().split()),
            normalize_numeric_value(row.value),
            normalize_unit(row.unit),
            page_number,
        )

    @staticmethod
    def _sort_review_center_rows(rows: list[dict[str, Any]], *, sort_by: str) -> list[dict[str, Any]]:
        normalized_sort = str(sort_by or "recent").strip().lower()
        if normalized_sort == "year_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -(int(row["year"]) if row.get("year") is not None else -1),
                    str(row.get("paper_id") or ""),
                ),
            )
        if normalized_sort == "conflicts_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -int(row.get("review_conflict_total_count") or 0),
                    -int(row.get("review_conflict_count") or 0),
                    -int(row.get("locator_issue_count") or 0),
                    -int(row.get("figure_issue_count") or 0),
                    str(row.get("paper_id") or ""),
                ),
            )
        if normalized_sort == "suspected_missing_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -int(row.get("suspected_missing_dft_count") or 0),
                    0
                    if str(row.get("workflow_status") or "") == "Suspected_Missing"
                    else (1 if str(row.get("workflow_status") or "") == "Unparsed" else 2),
                    str(row.get("paper_id") or ""),
                ),
            )
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("paper_id") or ""),
            ),
            reverse=True,
        )

    @staticmethod
    def _manual_review_progress(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        source = data if isinstance(data, dict) else {}
        progress = source.get("manual_review_progress") if isinstance(source.get("manual_review_progress"), dict) else {}

        def normalize_entry(module: str) -> dict[str, Any]:
            raw = progress.get(module)
            if isinstance(raw, dict):
                return {
                    "completed": bool(raw.get("completed")),
                    "updated_at": raw.get("updated_at"),
                    "updated_by": raw.get("updated_by"),
                }
            return {
                "completed": bool(raw),
                "updated_at": None,
                "updated_by": None,
            }

        return {
            "content": normalize_entry("content"),
            "figures": normalize_entry("figures"),
            "dft": normalize_entry("dft"),
        }

    @staticmethod
    def _lightweight_dft_audit(
        paper: Paper,
        *,
        parsed_count: int,
        exportable_count: int,
        blocked_count: int,
    ) -> dict[str, Any]:
        status = "Unparsed"
        if parsed_count > 0:
            status = "DB_Ready" if exportable_count > 0 and blocked_count == 0 else "Initial_Parsed"
        if str(paper.workflow_status or "") == "Suspected_Missing":
            status = "Suspected_Missing"
        return {
            "schema_version": "dft_completeness_audit_v1_light",
            "coverage_status": status,
            "status_label": {
                "Unparsed": "未解析",
                "Initial_Parsed": "初步解析",
                "Suspected_Missing": "疑似漏提",
                "DB_Ready": "可入库",
            }.get(status, status),
            "detected_signal_count": parsed_count,
            "detected_sections": 0,
            "detected_tables": 0,
            "detected_figures": 0,
            "parsed_dft_count": parsed_count,
            "exportable_dft_count": exportable_count,
            "blocked_dft_count": blocked_count,
            "suspected_missing_count": 1 if status == "Suspected_Missing" else 0,
            "coverage_ratio": 1.0 if parsed_count and blocked_count == 0 else 0.0,
            "unique_candidate_count": parsed_count,
            "duplicate_evidence_count": 0,
            "rescan_recommended": status == "Suspected_Missing",
            "rescan_next_status": "Needs_IDE_Rescan" if status == "Suspected_Missing" else None,
            "low_recall_warning": status == "Suspected_Missing",
            "low_recall_reasons": [],
            "ide_ai_review_recommended": status in {"Initial_Parsed", "Suspected_Missing"},
            "signal_examples": [],
        }

    def _paper_pdf_path(self, paper: Paper) -> Path | None:
        raw_path = Path(paper.pdf_path) if paper.pdf_path else None
        if raw_path is not None and raw_path.is_absolute() and not raw_path.exists():
            return None
        return resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=self.settings,
        )

    def _workspace_root(self, paper_id: UUID) -> Path:
        return self.settings.storage_root / "by_id" / str(paper_id)

    def _workspace_ref(self, workspace_root: Path) -> str:
        try:
            return workspace_root.resolve().relative_to(self.settings.storage_root.resolve()).as_posix()
        except ValueError:
            return str(workspace_root.resolve())

    @staticmethod
    def _ensure_workspace_dirs(workspace_root: Path) -> dict[str, Path]:
        dirs = {
            "root": workspace_root,
            "pages": workspace_root / "pages",
            "figures": workspace_root / "figures",
            "tables": workspace_root / "tables",
            "markdown": workspace_root / "markdown",
            "ocr": workspace_root / "ocr",
            "evidence": workspace_root / "evidence",
            "extraction": workspace_root / "extraction",
            "audit": workspace_root / "audit",
        }
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _copy_source_pdf(self, pdf_path: Path | None, workspace_root: Path) -> None:
        if pdf_path is None or not pdf_path.exists():
            return
        destination = workspace_root / "original.pdf"
        if destination.exists() and destination.stat().st_size == pdf_path.stat().st_size:
            return
        if pdf_path.resolve() != destination.resolve():
            shutil.copy2(pdf_path, destination)

    def _write_markdown_copy(self, paper: Paper, markdown_dir: Path) -> None:
        markdown_path = resolve_persisted_artifact_path(
            paper.markdown_path,
            category="markdown",
            settings=self.settings,
        )
        if markdown_path is not None and markdown_path.exists():
            target = markdown_dir / "source.md"
            if markdown_path.resolve() != target.resolve():
                shutil.copy2(markdown_path, target)
        self._write_json(
            markdown_dir / "trust.json",
            {
                "markdown_trust": (paper.pdf_quality_report or {}).get("markdown_trust"),
                "pdf_quality_status": paper.pdf_quality_status,
                "policy": "Markdown is reading aid only; use evidence locators/PDF pages as source of truth.",
            },
        )

    def _write_docling_copy(self, paper: Paper, extraction_dir: Path) -> None:
        docling_path = resolve_persisted_artifact_path(
            paper.docling_json_path,
            category="docling_json",
            settings=self.settings,
        )
        if docling_path is not None and docling_path.exists():
            target = extraction_dir / "docling.json"
            if docling_path.resolve() != target.resolve():
                shutil.copy2(docling_path, target)

    def _write_evidence_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper.id)).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper.id)).all()
        locators = self.session.scalars(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper.id)).all()
        self._write_json(
            dirs["evidence"] / "sections.json",
            [
                {
                    "id": str(row.id),
                    "title": row.section_title,
                    "section_type": row.section_type,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "text": row.text,
                    "evidence_state": "parsed_source_text",
                }
                for row in sections
            ],
        )
        self._write_json(
            dirs["evidence"] / "tables.json",
            [
                {
                    "id": str(row.id),
                    "caption": row.caption,
                    "page": row.page,
                    "markdown_content": row.markdown_content,
                    "prov": row.prov,
                    "evidence_state": "table_candidate_unverified",
                }
                for row in tables
            ],
        )
        self._sync_figure_workspace_files(figures, dirs["figures"])
        self._write_json(
            dirs["evidence"] / "figures.json",
            [
                {
                    "id": str(row.id),
                    "caption": row.caption,
                    "page": row.page,
                    "image_path": row.image_path,
                    "figure_label": row.figure_label,
                    "crop_status": row.crop_status,
                    "crop_confidence": row.crop_confidence,
                    "crop_source": row.crop_source,
                    "prov": row.prov,
                }
                for row in figures
            ],
        )
        self._write_json(
            dirs["evidence"] / "locators.json",
            [
                {
                    "id": str(row.id),
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "field_name": row.field_name,
                    "source_type": row.source_type,
                    "page": row.page,
                    "bbox": row.bbox,
                    "section": row.section,
                    "figure_id": str(row.figure_id) if row.figure_id else None,
                    "table_id": str(row.table_id) if row.table_id else None,
                    "evidence_text": row.evidence_text,
                    "locator_status": row.locator_status,
                    "locator_confidence": row.locator_confidence,
                    "parser_source": row.parser_source,
                    "warning_reason": row.warning_reason,
                }
                for row in locators
            ],
        )

    def _write_extraction_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        self._write_json(
            dirs["extraction"] / "dft_candidates.json",
            [
                {
                    "record_id": str(row.id),
                    "candidate_status": row.candidate_status,
                    "adsorbate": row.adsorbate,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "reaction_step": row.reaction_step,
                    "source_section": row.source_section,
                    "source_figure": row.source_figure,
                    "evidence_text": row.evidence_text,
                    "confidence": row.confidence,
                    "extraction_protocol_version": row.extraction_protocol_version,
                    "evidence_payload": row.evidence_payload,
                }
                for row in rows
            ],
        )

    def _write_ai_reading_package(self, paper: Paper, dirs: dict[str, Path]) -> None:
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper.id)).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper.id)).all()
        dft_rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        dft_settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper.id)).all()
        catalyst_samples = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper.id)).all()
        electrochemical_items = self.session.scalars(
            select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id == paper.id)
        ).all()
        mechanism_claims = self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper.id)).all()
        writing_cards = self.session.scalars(select(WritingCard).where(WritingCard.paper_id == paper.id)).all()
        audit = DFTCompletenessAuditor(self.session).audit_paper(paper.id, parsed_count=len(dft_rows))
        source_documents = self._source_documents_for_ai(paper)
        content_coverage = self._build_content_coverage_summary(
            paper=paper,
            sections=sections,
            tables=tables,
            figures=figures,
            dft_settings=dft_settings,
            dft_rows=dft_rows,
            catalyst_samples=catalyst_samples,
            electrochemical_items=electrochemical_items,
            mechanism_claims=mechanism_claims,
            writing_cards=writing_cards,
        )

        relevant_sections = [
            {
                "id": str(section.id),
                "role": self._section_role_for_coverage(section),
                "title": section.section_title or section.section_type,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "text": section.text,
            }
            for section in sections
            if self._section_role_for_coverage(section) != "context" or any(
                example.get("source_id") == str(section.id) for example in audit.get("signal_examples", [])
            )
        ]
        self._write_json(
            dirs["extraction"] / "ai_reading_package.json",
            {
                "schema_version": "ai_reading_package_v1",
                "paper": self._paper_metadata(paper),
                "source_documents": source_documents,
                "abstract": paper.abstract,
                "llm_input_policy": {
                    "text_llm_scope": [
                        "paper metadata",
                        "abstract",
                        "text sections",
                        "parsed markdown tables",
                        "existing structured candidates",
                    ],
                    "excluded_from_text_llm": [
                        "figure images",
                        "figure crops",
                        "icons",
                        "chart visual value reading",
                    ],
                    "image_or_chart_review": "Use a human reviewer or IDE visual inspection; text-only AI must not infer values from images.",
                    "web_llm_extract": "disabled",
                    "required_workflow": (
                        "prepare-ai-context / codex-item -> IDE AI -> import_analysis. Non-DFT "
                        "metadata, sections, tables, figure metadata, writing_cards, mechanism_claims, "
                        "electrochemical_performance, catalyst_samples, notes, and relationships may be "
                        "auto-applied with PDF evidence anchors and module write locks. DFT results/settings "
                        "remain candidates until the review/export gate passes."
                    ),
                },
                "non_dft_direct_write_policy": {
                    "ai_can_apply_without_human_confirmation": [
                        "paper metadata",
                        "sections",
                        "tables",
                        "figure metadata/captions/content_summary",
                        "writing_cards",
                        "mechanism_claims",
                        "electrochemical_performance",
                        "catalyst_samples",
                        "notes",
                        "relationships",
                    ],
                    "must_not_auto_apply": [
                        "dft_results",
                        "dft_settings",
                        "DFT export verification",
                        "figure image recrop/create through import_analysis",
                    ],
                    "evidence_required": [
                        "page, section/section_title, quoted_text, table, figure, or bbox anchor",
                        "catalyst_samples require a material anchor beyond free-form evidence_text",
                        "section creation requires a strong text/section/table/figure/bbox anchor",
                    ],
                    "write_path": (
                        "Use import_analysis with auto_apply_review_rules=true plus a module write lock. "
                        "For figure image cropping, call recrop_figure or create_figure_from_bbox directly, "
                        "then read back the updated figure record."
                    ),
                },
                "content_coverage": content_coverage,
                "dft_completeness_audit": audit,
                "sections": relevant_sections,
                "tables": [
                    {
                        "id": str(table.id),
                        "caption": table.caption,
                        "page": table.page,
                        "markdown_content": table.markdown_content,
                        "prov": table.prov,
                    }
                    for table in tables
                ],
                "figures": [
                    {
                        "id": str(figure.id),
                        "figure_label": figure.figure_label,
                        "caption": figure.caption,
                        "page": figure.page,
                        "image_path": figure.image_path,
                        "text_llm_allowed": False,
                        "review_route": "human_or_ide_visual_only",
                        "text_llm_note": "Do not ask a text-only LLM to interpret this figure image/crop or read chart values.",
                        "crop_status": figure.crop_status,
                        "crop_confidence": figure.crop_confidence,
                        "prov": figure.prov,
                    }
                    for figure in figures
                ],
                "existing_structured_content": {
                    "dft_settings": [
                        {
                            "id": str(row.id),
                            "software": row.software,
                            "functional": row.functional,
                            "dispersion_correction": row.dispersion_correction,
                            "pseudopotential": row.pseudopotential,
                            "cutoff_energy_ev": row.cutoff_energy_ev,
                            "k_points": row.k_points,
                            "convergence_settings": row.convergence_settings,
                            "vacuum_thickness_a": row.vacuum_thickness_a,
                            "raw_json": row.raw_json,
                        }
                        for row in dft_settings
                    ],
                    "catalyst_samples": [
                        {
                            "id": str(row.id),
                            "name": row.name,
                            "catalyst_type": row.catalyst_type,
                            "metal_centers": row.metal_centers,
                            "coordination": row.coordination,
                            "support": row.support,
                            "synthesis_method": row.synthesis_method,
                            "evidence_strength": row.evidence_strength,
                        }
                        for row in catalyst_samples
                    ],
                    "electrochemical_performance": [
                        {
                            "id": str(row.id),
                            "sulfur_loading_mg_cm2": row.sulfur_loading_mg_cm2,
                            "sulfur_content_wt_percent": row.sulfur_content_wt_percent,
                            "electrolyte_sulfur_ratio": row.electrolyte_sulfur_ratio,
                            "capacity_value": row.capacity_value,
                            "cycle_number": row.cycle_number,
                            "rate": row.rate,
                            "decay_per_cycle": row.decay_per_cycle,
                            "evidence_text": row.evidence_text,
                        }
                        for row in electrochemical_items
                    ],
                    "mechanism_claims": [
                        {
                            "id": str(row.id),
                            "claim_type": row.claim_type,
                            "claim_text": row.claim_text,
                            "evidence_types": row.evidence_types,
                            "confidence": row.confidence,
                            "evidence_text": row.evidence_text,
                        }
                        for row in mechanism_claims
                    ],
                    "writing_cards": [
                        {
                            "id": str(row.id),
                            "paper_type": row.paper_type,
                            "research_gap": row.research_gap,
                            "proposed_solution": row.proposed_solution,
                            "core_hypothesis": row.core_hypothesis,
                            "evidence_chain": row.evidence_chain,
                            "section_strategy": row.section_strategy,
                            "figure_logic": row.figure_logic,
                            "abstract_logic": row.abstract_logic,
                            "introduction_logic": row.introduction_logic,
                            "discussion_logic": row.discussion_logic,
                        }
                        for row in writing_cards
                    ],
                },
                "system_candidates": [
                    {
                        "record_id": str(row.id),
                        "candidate_status": row.candidate_status or "system_candidate",
                        "adsorbate": row.adsorbate,
                        "property_type": row.property_type,
                        "value": row.value,
                        "unit": row.unit,
                        "reaction_step": row.reaction_step,
                        "source_section": row.source_section,
                        "source_figure": row.source_figure,
                        "evidence_text": row.evidence_text,
                        "confidence": row.confidence,
                        "evidence_payload": row.evidence_payload,
                    }
                    for row in dft_rows
                ],
                "ai_task": (
                    "Read the main text and any available supplementary_information source documents. First repair "
                    "non-DFT content directly through import_analysis when there is checkable PDF evidence: metadata, "
                    "sections, tables, figure metadata/summaries, writing_cards, mechanism_claims, "
                    "electrochemical_performance, catalyst_samples, notes, and relationships. For missing sections or "
                    "writing_cards, create objects with target_path=<collection>:new:create. For existing objects, use "
                    "replace corrections with target_path=<collection>:<id>:<field>. Figure image crop/create operations "
                    "must call recrop_figure or create_figure_from_bbox directly instead of import_analysis. Extract "
                    "DFT data using the explicit AI protocol only as candidates: SI data belongs to this main paper_id, "
                    "but each candidate must mark evidence_location.source_document_type=supplementary_information. "
                    "Merge repeated main-text/SI occurrences into one candidate using dedupe_signature and "
                    "supporting_evidence. Do not treat values from cited or supporting references as this paper's DFT "
                    "data; mark them source_document_type=supporting_reference and borrowed_from_reference=true or "
                    "record them only as relationship evidence. Do not estimate values from images, curves, or axis "
                    "ticks with a text-only model. DFT results/settings are not verified until the existing review/export "
                    "gate passes."
                ),
            },
        )

    def _build_content_coverage_summary(
        self,
        *,
        paper: Paper,
        sections: list[PaperSection],
        tables: list[PaperTable],
        figures: list[PaperFigure],
        dft_settings: list[DFTSetting],
        dft_rows: list[DFTResult],
        catalyst_samples: list[CatalystSample],
        electrochemical_items: list[ElectrochemicalPerformance],
        mechanism_claims: list[MechanismClaim],
        writing_cards: list[WritingCard],
    ) -> dict[str, Any]:
        role_counts = Counter(self._section_role_for_coverage(section) for section in sections)
        missing_core_sections: list[str] = []
        if not str(paper.abstract or "").strip():
            missing_core_sections.append("abstract")
        for role in ("introduction", "methods", "results_discussion", "conclusion"):
            if role_counts.get(role, 0) == 0:
                missing_core_sections.append(role)

        structured_counts = {
            "sections": len(sections),
            "tables": len(tables),
            "figures": len(figures),
            "dft_settings": len(dft_settings),
            "dft_results": len(dft_rows),
            "catalyst_samples": len(catalyst_samples),
            "electrochemical_performance": len(electrochemical_items),
            "mechanism_claims": len(mechanism_claims),
            "writing_cards": len(writing_cards),
        }
        missing_structured_modules = [
            module
            for module in (
                "writing_cards",
                "mechanism_claims",
                "electrochemical_performance",
                "catalyst_samples",
            )
            if structured_counts[module] == 0
        ]
        recommended_actions: list[str] = []
        if missing_core_sections:
            recommended_actions.append(
                "Create or replace sections with strong PDF anchors for missing abstract/introduction/methods/results/conclusion coverage."
            )
        if structured_counts["writing_cards"] == 0:
            recommended_actions.append(
                "Create evidence-backed writing_cards covering research_gap, proposed_solution, core_hypothesis, evidence_chain, section_strategy, figure_logic, abstract_logic, introduction_logic, and discussion_logic."
            )
        if structured_counts["mechanism_claims"] == 0:
            recommended_actions.append("Create mechanism_claims for shuttle suppression, catalytic conversion, adsorption, diffusion, or electronic-structure claims when the text supports them.")
        if structured_counts["electrochemical_performance"] == 0:
            recommended_actions.append("Create electrochemical_performance rows for loading, capacity, cycle, rate, decay, and electrolyte/sulfur conditions when evidence is present.")
        if structured_counts["catalyst_samples"] == 0:
            recommended_actions.append("Create catalyst_samples for named materials/comparators with material anchors before linking mechanism or performance claims.")

        return {
            "section_role_counts": dict(sorted(role_counts.items())),
            "structured_counts": structured_counts,
            "missing_core_sections": missing_core_sections,
            "missing_structured_modules": missing_structured_modules,
            "non_dft_modules_open_for_ai_write": [
                "metadata",
                "sections",
                "tables",
                "figures",
                "writing_cards",
                "mechanism_claims",
                "electrochemical_performance",
                "catalyst_samples",
                "notes",
                "relationships",
            ],
            "dft_modules_review_gated": ["dft_results", "dft_settings"],
            "recommended_ai_actions": recommended_actions,
            "rag_priority": [
                "Evidence-backed writing_cards for high-quality review writing",
                "Mechanism and electrochemical claims with page/quote anchors",
                "Normalized sections for reliable retrieval and citation insertion",
            ],
        }

    @staticmethod
    def _section_role_for_coverage(section: PaperSection) -> str:
        title = f"{section.section_title or ''} {section.section_type or ''}".lower()
        if re.search(r"\babstract\b", title):
            return "abstract"
        if re.search(r"intro|background", title):
            return "introduction"
        if re.search(r"method|comput|dft|calculation|experimental|synthesis", title):
            return "methods"
        if re.search(r"result|discussion|performance|mechanism|characteri[sz]ation", title):
            return "results_discussion"
        if re.search(r"conclusion|summary", title):
            return "conclusion"
        return "context"

    def _source_documents_for_ai(self, paper: Paper) -> list[dict[str, Any]]:
        pdf_path = self._paper_pdf_path(paper)
        workspace_root = self._workspace_root(paper.id)
        source_documents = [
            {
                "source_document_type": "main_text",
                "label": "Main PDF",
                "paper_id": str(paper.id),
                "path": str(pdf_path) if pdf_path is not None else str(workspace_root / "original.pdf"),
                "available": bool(pdf_path is not None and pdf_path.exists()),
            },
            {
                "source_document_type": "supplementary_information",
                "label": "SI",
                "paper_id": str(paper.id),
                "path": None,
                "available": False,
                "note": "SI is treated as a source document for this main paper, not as a separate library paper.",
            },
        ]
        source_documents.extend(self._supplementary_documents_for_ai(paper))
        return source_documents

    def _supplementary_documents_for_ai(self, paper: Paper) -> list[dict[str, Any]]:
        relationship_types = {"supplementary", "supplementary_information", "si"}
        relationships = self.session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == paper.id,
                PaperRelationship.relationship_type.in_(relationship_types),
            )
        ).all()
        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for relationship in relationships:
            target = self.session.get(Paper, relationship.target_paper_id)
            if target is None:
                continue
            target_path = self._paper_pdf_path(target)
            path_text = str(target_path) if target_path is not None else None
            dedupe_key = path_text or str(target.id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            documents.append(
                {
                    "source_document_type": "supplementary_information",
                    "label": target.title or "SI",
                    "paper_id": str(paper.id),
                    "related_paper_id": str(target.id),
                    "relationship_id": str(relationship.id),
                    "relationship_type": relationship.relationship_type,
                    "path": path_text,
                    "available": bool(target_path is not None and target_path.exists()),
                    "note": "Linked supplementary PDF is treated as source material for the main paper.",
                }
            )
        return documents

    def _write_audit_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        rows = self.session.scalars(
            select(AuditLog).where(AuditLog.paper_id == paper.id).order_by(AuditLog.created_at.asc())
        ).all()
        dft_count = self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)) or 0
        self._write_json(
            dirs["audit"] / "dft_completeness.json",
            DFTCompletenessAuditor(self.session).audit_paper(paper.id, parsed_count=int(dft_count)),
        )
        self._write_json(
            dirs["audit"] / "audit_log.json",
            [
                {
                    "id": str(row.id),
                    "action": row.action,
                    "source": row.source,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        )

    def _sync_figure_workspace_files(self, figures: list[PaperFigure], figure_dir: Path) -> None:
        for index, figure in enumerate(figures, start=1):
            label = self._figure_label(figure.caption, index)
            figure.figure_label = label
            crop_payload = self._figure_crop_payload(figure)
            figure.crop_status = crop_payload["crop_status"]
            figure.crop_confidence = crop_payload["crop_confidence"]
            figure.crop_source = crop_payload["crop_source"]
            src = resolve_persisted_artifact_path(
                figure.image_path,
                category="figures",
                settings=self.settings,
            )
            if src is not None and src.exists():
                safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._") or f"figure_{index}"
                target = figure_dir / f"{safe_label}{src.suffix.lower() or '.png'}"
                if src.resolve() != target.resolve():
                    shutil.copy2(src, target)
            self.session.add(figure)

    @staticmethod
    def _figure_label(caption: str | None, index: int) -> str:
        match = re.search(r"(?:figure|fig\.?|scheme)\s*([0-9]+[A-Za-z]?)", caption or "", re.IGNORECASE)
        return f"fig_{match.group(1)}" if match else f"fig_candidate_{index}"

    @staticmethod
    def _figure_crop_payload(figure: PaperFigure) -> dict[str, Any]:
        prov = figure.prov or []
        extraction = next(
            (item for item in reversed(prov) if isinstance(item, dict) and item.get("image_extraction")),
            None,
        )
        if figure.image_path and extraction:
            return {
                "crop_status": "candidate_crop",
                "crop_confidence": extraction.get("confidence"),
                "crop_source": extraction.get("source") or extraction.get("image_extraction"),
            }
        if figure.image_path:
            return {"crop_status": "needs_recrop", "crop_confidence": None, "crop_source": "legacy_image"}
        return {"crop_status": "caption_only", "crop_confidence": None, "crop_source": "caption"}

    def _render_page_previews(self, pdf_path: Path, pages_dir: Path) -> None:
        try:
            import fitz

            doc = fitz.open(str(pdf_path))
            try:
                for index, page in enumerate(doc, start=1):
                    out_path = pages_dir / f"page_{index:03d}.png"
                    if out_path.exists():
                        continue
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                    pix.save(str(out_path))
            finally:
                doc.close()
        except Exception:
            return

    @staticmethod
    def _paper_metadata(paper: Paper) -> dict[str, Any]:
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "paper_id": str(paper.id),
            "paper_code": getattr(paper, "paper_code", None),
            "library_name": paper.library_name,
            "serial_number": paper.serial_number,
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "journal": paper.journal,
            "authors": paper.authors,
            "pdf_path": paper.pdf_path,
            "markdown_path": paper.markdown_path,
            "docling_json_path": paper.docling_json_path,
            "workflow_status": paper.workflow_status,
            "pdf_quality_status": paper.pdf_quality_status,
        }

    @staticmethod
    def dft_evidence_payload(item: dict[str, Any]) -> dict[str, Any]:
        location = item.get("source_location") or {}
        payload = {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "protocol": protocol_snapshot("dft_ai_protocol", fallback_version=EXTRACTION_PROTOCOL_VERSION),
            "system_extractor_protocol": protocol_snapshot("dft_results", fallback_version=EXTRACTION_PROTOCOL_VERSION),
            "source_document_type": item.get("source_document_type") or location.get("source_document_type") or "main_text",
            "source_document_label": item.get("source_document_label") or location.get("source_document_label") or "Main PDF",
            "source_locator": location.get("source_locator") or location.get("locator"),
            "page": location.get("page"),
            "table": location.get("table"),
            "section": location.get("section"),
            "quoted_text": item.get("quoted_text") or item.get("evidence_text"),
            "supporting_evidence": item.get("supporting_evidence") or [],
            "field_sources": [
                {
                    "field_name": "value",
                    "source_type": item.get("parser_source") or "extraction",
                    "page": location.get("page"),
                    "section": location.get("section"),
                    "figure": location.get("figure"),
                    "table": location.get("table"),
                    "bbox": location.get("bbox"),
                    "excerpt": item.get("evidence_text"),
                    "confidence": item.get("confidence"),
                }
            ],
            "policy": "Candidate values require assigned AI/human review and confirmation before ML export.",
            "ai_protocol_policy": (
                "System rule extraction only creates system_candidate records. Final DFT/ML data must pass "
                "PDF evidence anchoring, AI protocol extraction/review, deduplication, completeness audit, "
                "and human or second-AI confirmation."
            ),
        }
        payload["dedupe_signature"] = item.get("dedupe_signature") or build_dft_dedupe_signature(
            {
                **item,
                "evidence_payload": payload,
                "paper_id": item.get("paper_id"),
            }
        )
        return payload
