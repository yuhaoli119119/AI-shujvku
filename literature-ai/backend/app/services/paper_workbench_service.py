from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    Paper,
    PaperFigure,
    PaperNote,
    PaperRelationship,
    PaperTable,
)
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_codes import ensure_paper_codes
from app.services.paper_workbench_ai_package import (
    PaperWorkbenchAiPackageMixin,
    SUPPLEMENTARY_RELATIONSHIP_TYPES,
)
from app.services.paper_workbench_quality import PaperWorkbenchQualityMixin
from app.services.paper_workbench_review_center import PaperWorkbenchReviewCenterMixin
from app.services.paper_workbench_workspace import PaperWorkbenchWorkspaceMixin
from app.services.review_adjudication_service import ReviewAdjudicationService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.artifact_status import build_paper_pdf_status
from app.utils.workbench_status import (
    WORKBENCH_SCHEMA_VERSION,
    workflow_needs_human_confirmation,
)
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results


class PaperWorkbenchService(
    PaperWorkbenchQualityMixin,
    PaperWorkbenchAiPackageMixin,
    PaperWorkbenchWorkspaceMixin,
    PaperWorkbenchReviewCenterMixin,
):
    """Build the Codex-centered, evidence-first workspace for each paper."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

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
        if paper.workflow_status in (None, "", "Imported") and str(quality_report.get("reason") or "").strip() != "missing_pdf_reference":
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
        paper_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        paper_stmt = select(Paper)
        normalized_library = normalize_library_name(library_name) if library_name is not None else None
        if normalized_library:
            paper_stmt = paper_stmt.where(build_library_name_clause(Paper.library_name, normalized_library))
        requested_paper_ids = [paper_id for paper_id in (paper_ids or []) if paper_id is not None]
        if requested_paper_ids:
            requested_set = set(requested_paper_ids)
            related_pairs = self.session.execute(
                select(PaperRelationship.source_paper_id, PaperRelationship.target_paper_id).where(
                    PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                    or_(
                        PaperRelationship.source_paper_id.in_(requested_set),
                        PaperRelationship.target_paper_id.in_(requested_set),
                    ),
                )
            ).all()
            for source_id, target_id in related_pairs:
                requested_set.update({source_id, target_id})
            requested_paper_ids = list(requested_set)
        if requested_paper_ids:
            paper_stmt = paper_stmt.where(Paper.id.in_(requested_paper_ids))
        if summary_only and sort_by == "recent" and limit > 0 and not requested_paper_ids:
            paper_stmt = paper_stmt.order_by(Paper.created_at.desc()).limit(limit)
        papers = self.session.scalars(paper_stmt).all()
        if ensure_paper_codes(self.session, papers):
            self.session.commit()
        paper_ids = {paper.id for paper in papers}
        supplementary_relationships = (
            self.session.scalars(
                select(PaperRelationship).where(
                    PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                    or_(
                        PaperRelationship.source_paper_id.in_(paper_ids),
                        PaperRelationship.target_paper_id.in_(paper_ids),
                    ),
                )
            ).all()
            if paper_ids
            else []
        )
        group_main_by_paper: dict[UUID, UUID] = {}
        support_ids_by_main: dict[UUID, set[UUID]] = defaultdict(set)
        related_paper_ids: set[UUID] = set(paper_ids)
        for relationship in supplementary_relationships:
            main_id = relationship.source_paper_id
            support_id = relationship.target_paper_id
            group_main_by_paper[main_id] = main_id
            group_main_by_paper[support_id] = main_id
            support_ids_by_main[main_id].add(support_id)
            related_paper_ids.update({main_id, support_id})
        related_paper_meta: dict[UUID, dict[str, Any]] = {}
        if related_paper_ids:
            for related in self.session.execute(
                select(Paper.id, Paper.paper_code, Paper.title, Paper.paper_type).where(Paper.id.in_(related_paper_ids))
            ).all():
                related_paper_meta[related.id] = {
                    "paper_id": str(related.id),
                    "paper_code": related.paper_code,
                    "title": related.title,
                    "paper_type": related.paper_type,
                }
        group_dft_status_counts_by_paper: dict[UUID, dict[str, int]] = {paper_id: {} for paper_id in related_paper_ids}
        if related_paper_ids:
            for paper_id, candidate_status, count in self.session.execute(
                select(
                    DFTResult.paper_id,
                    DFTResult.candidate_status,
                    func.count(DFTResult.id),
                )
                .where(DFTResult.paper_id.in_(related_paper_ids))
                .group_by(DFTResult.paper_id, DFTResult.candidate_status)
            ).all():
                group_dft_status_counts_by_paper.setdefault(paper_id, {})[
                    str(candidate_status or "system_candidate")
                ] = int(count or 0)
        rows = []
        status_counts: Counter[str] = Counter()
        quality_counts: Counter[str] = Counter()
        auditor = None if summary_only else DFTCompletenessAuditor(self.session)
        reliability_auditor = None if summary_only else ArtifactReliabilityAuditService(self.session, self.settings)
        conflict_service = ReviewConflictAggregationService(self.session)
        conflict_total_counts_by_module = {
            str(paper_id): {"dft": 0, "visual": 0, "content": 0, "other": 0}
            for paper_id in paper_ids
        }
        allowed_conflict_paper_ids = {str(paper_id) for paper_id in paper_ids}
        conflict_payload: dict[str, Any] = {"rows": []}
        if paper_ids:
            conflict_payload = conflict_service.list_conflicts(paper_ids=paper_ids, limit=1000)
            for conflict_row in conflict_payload.get("rows") or []:
                pid = str(conflict_row.get("paper_id") or "")
                module = conflict_service._module_for_target_type(conflict_row.get("target_type"))
                conflict_total_counts_by_module.setdefault(pid, {"dft": 0, "visual": 0, "content": 0, "other": 0})[module] += 1
        conflict_total_counts = {
            paper_id: sum(module_counts.values())
            for paper_id, module_counts in conflict_total_counts_by_module.items()
        }
        adjudication_service = ReviewAdjudicationService(self.session)
        conflict_counts_by_module = {
            str(paper_id): {"dft": 0, "visual": 0, "content": 0, "other": 0}
            for paper_id in paper_ids
        }
        if paper_ids:
            adjudicated_rows = adjudication_service.enrich_rows(conflict_payload.get("rows") or [])
            for conflict_row in adjudicated_rows:
                pid = str(conflict_row.get("paper_id") or "")
                if not adjudication_service.is_actionable_conflict(conflict_row):
                    continue
                module = adjudication_service._module_for_target_type(conflict_row.get("target_type"))
                conflict_counts_by_module.setdefault(pid, {"dft": 0, "visual": 0, "content": 0, "other": 0})[module] += 1
        conflict_counts = {
            paper_id: sum(module_counts.values())
            for paper_id, module_counts in conflict_counts_by_module.items()
        }
        dft_rows_by_paper: dict[UUID, list[DFTResult]] = {paper_id: [] for paper_id in paper_ids}
        dft_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        dft_candidate_status_counts_by_paper: dict[UUID, dict[str, int]] = {paper_id: {} for paper_id in paper_ids}
        active_dft_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        if summary_only:
            dft_count_rows = (
                self.session.execute(
                    select(
                        DFTResult.paper_id,
                        DFTResult.candidate_status,
                        func.count(DFTResult.id),
                    )
                    .where(DFTResult.paper_id.in_(paper_ids))
                    .group_by(DFTResult.paper_id, DFTResult.candidate_status)
                ).all()
                if paper_ids
                else []
            )
            for paper_id, candidate_status, count in dft_count_rows:
                normalized_status = str(candidate_status or "system_candidate")
                count_int = int(count or 0)
                dft_count_by_paper[paper_id] = dft_count_by_paper.get(paper_id, 0) + count_int
                dft_candidate_status_counts_by_paper.setdefault(paper_id, {})[normalized_status] = count_int
                if self._is_active_dft_candidate(normalized_status):
                    active_dft_count_by_paper[paper_id] = active_dft_count_by_paper.get(paper_id, 0) + count_int
        else:
            for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id.in_(paper_ids))).all() if paper_ids else []:
                dft_rows_by_paper.setdefault(row.paper_id, []).append(row)
        figure_rows_by_paper: dict[UUID, list[PaperFigure]] = {paper_id: [] for paper_id in paper_ids}
        figure_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        figure_crop_status_counts_by_paper: dict[UUID, dict[str, int]] = {paper_id: {} for paper_id in paper_ids}
        if summary_only:
            figure_count_rows = (
                self.session.execute(
                    select(
                        PaperFigure.paper_id,
                        PaperFigure.crop_status,
                        func.count(PaperFigure.id),
                    )
                    .where(PaperFigure.paper_id.in_(paper_ids))
                    .group_by(PaperFigure.paper_id, PaperFigure.crop_status)
                ).all()
                if paper_ids
                else []
            )
            for paper_id, crop_status, count in figure_count_rows:
                normalized_status = str(crop_status or "unknown")
                count_int = int(count or 0)
                figure_count_by_paper[paper_id] = figure_count_by_paper.get(paper_id, 0) + count_int
                figure_crop_status_counts_by_paper.setdefault(paper_id, {})[normalized_status] = count_int
        else:
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
        external_audit_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        object_review_audit_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        if summary_only:
            candidate_count_rows = (
                self.session.execute(
                    select(
                        ExternalAnalysisCandidate.paper_id,
                        ExternalAnalysisCandidate.candidate_type,
                        func.count(ExternalAnalysisCandidate.id),
                    )
                    .where(ExternalAnalysisCandidate.paper_id.in_(paper_ids))
                    .where(
                        ExternalAnalysisCandidate.candidate_type.in_(
                            ("external_audit_opinion", "object_review_audit")
                        )
                    )
                    .group_by(
                        ExternalAnalysisCandidate.paper_id,
                        ExternalAnalysisCandidate.candidate_type,
                    )
                ).all()
                if paper_ids
                else []
            )
            for paper_id, candidate_type, count in candidate_count_rows:
                if candidate_type == "external_audit_opinion":
                    external_audit_count_by_paper[paper_id] = int(count or 0)
                elif candidate_type == "object_review_audit":
                    object_review_audit_count_by_paper[paper_id] = int(count or 0)
        else:
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
        paper_note_count_by_paper: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        if summary_only:
            note_count_rows = (
                self.session.execute(
                    select(PaperNote.paper_id, func.count(PaperNote.id))
                    .where(PaperNote.paper_id.in_(paper_ids))
                    .group_by(PaperNote.paper_id)
                ).all()
                if paper_ids
                else []
            )
            for paper_id, count in note_count_rows:
                paper_note_count_by_paper[paper_id] = int(count or 0)
        else:
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
            blocked_counts[paper_id] = sum(
                1
                for row in rows_for_paper
                if self._is_active_dft_candidate(row.candidate_status)
                and not (gate_by_id.get(str(row.id)) and gate_by_id[str(row.id)].eligible)
            )
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
            dft_count = (
                dft_count_by_paper.get(paper.id, 0)
                if summary_only
                else len(dft_rows)
            )
            active_dft_count = (
                active_dft_count_by_paper.get(paper.id, 0)
                if summary_only
                else self._count_active_dft_candidates(dft_rows)
            )
            figures = figure_rows_by_paper.get(paper.id, [])
            figure_count = (
                figure_count_by_paper.get(paper.id, 0)
                if summary_only
                else len(figures)
            )
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
            external_audit_count = (
                external_audit_count_by_paper.get(paper.id, 0)
                if summary_only
                else len(external_audit_candidates)
            )
            object_review_audit_count = (
                object_review_audit_count_by_paper.get(paper.id, 0)
                if summary_only
                else len(object_review_candidates)
            )
            paper_note_count = (
                paper_note_count_by_paper.get(paper.id, 0)
                if summary_only
                else len(notes_by_paper.get(paper.id) or [])
            )
            quality_report = paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {}
            needs_human_confirmation = workflow_needs_human_confirmation(paper.workflow_status, quality_report)
            figure_crop_status_counts = (
                figure_crop_status_counts_by_paper.get(paper.id, {})
                if summary_only
                else dict(
                    Counter(
                        self._figure_crop_payload(figure)["crop_status"]
                        for figure in figures
                    )
                )
            )
            unreliable_figure_count = sum(
                figure_crop_status_counts.get(status, 0)
                for status in ("needs_recrop", "caption_only", "needs_review")
            )
            dft_candidate_status_counts = (
                dft_candidate_status_counts_by_paper.get(paper.id, {})
                if summary_only
                else dict(Counter(row.candidate_status or "system_candidate" for row in dft_rows))
            )
            exportable_count = exportable_counts.get(paper.id, 0)
            blocked_count = blocked_counts.get(paper.id, 0)
            dft_audit = dft_audits.get(str(paper.id)) or self._lightweight_dft_audit(
                paper,
                parsed_count=dft_count,
                exportable_count=exportable_count,
                blocked_count=blocked_count,
                candidate_status_counts=dft_candidate_status_counts,
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
            if summary_only:
                raw_pdf_path = str(paper.pdf_path or "").strip()
                inferred_pdf_exists = bool(raw_pdf_path) and str(paper.oa_status or "").strip().lower() not in {
                    "metadata_only",
                    "needs_upload",
                }
                pdf_status = {
                    "pdf_exists": inferred_pdf_exists,
                    "pdf_file_size": None,
                    "pdf_path_kind": "storage_relative" if raw_pdf_path and not Path(raw_pdf_path).is_absolute() else (
                        "absolute" if raw_pdf_path else "missing"
                    ),
                    "blocking_errors": [] if inferred_pdf_exists else ["missing_pdf"],
                    "warnings": [],
                }
            else:
                pdf_status = build_paper_pdf_status(paper, settings=self.settings)
            manual_review_progress = self._manual_review_progress(paper.comprehensive_analysis)
            comprehensive_analysis = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}
            parsed_analysis = {key: value for key, value in comprehensive_analysis.items() if key != "manual_review_progress"}
            supplementary_group = self._supplementary_group_payload(
                paper.id,
                group_main_by_paper=group_main_by_paper,
                support_ids_by_main=support_ids_by_main,
                related_paper_meta=related_paper_meta,
                dft_status_counts_by_paper=group_dft_status_counts_by_paper,
            )
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
                    "library_name": paper.library_name,
                    "created_at": paper.created_at.isoformat() if paper.created_at else None,
                    "title": paper.title,
                    "doi": paper.doi,
                    "year": paper.year,
                    "journal": paper.journal,
                    "paper_type": paper.paper_type,
                    "supplementary_group": supplementary_group,
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
                    "external_audit_count": external_audit_count,
                    "external_audit_source_counts": dict(sorted(external_audit_source_counts.items())),
                    "external_audit_opinions": external_audit_opinions,
                    "object_review_audit_count": object_review_audit_count,
                    "object_review_audit_source_counts": dict(sorted(object_review_source_counts.items())),
                    "object_review_audits": object_review_audits,
                    "paper_note_count": paper_note_count,
                    "latest_paper_notes": latest_notes,
                    "review_conflict_count": conflict_counts.get(str(paper.id), 0),
                    "review_conflict_total_count": conflict_total_counts.get(str(paper.id), 0),
                    "dft_review_conflict_count": (conflict_counts_by_module.get(str(paper.id)) or {}).get("dft", 0),
                    "dft_review_conflict_total_count": (conflict_total_counts_by_module.get(str(paper.id)) or {}).get("dft", 0),
                    "visual_review_conflict_count": (conflict_counts_by_module.get(str(paper.id)) or {}).get("visual", 0),
                    "visual_review_conflict_total_count": (conflict_total_counts_by_module.get(str(paper.id)) or {}).get("visual", 0),
                    "content_review_conflict_count": (conflict_counts_by_module.get(str(paper.id)) or {}).get("content", 0),
                    "content_review_conflict_total_count": (conflict_total_counts_by_module.get(str(paper.id)) or {}).get("content", 0),
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
