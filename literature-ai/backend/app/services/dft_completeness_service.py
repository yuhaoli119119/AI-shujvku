from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
)
from app.services.dft_audit_issue_lifecycle_service import DFT_AUDIT_ISSUE_PENDING_STATUSES
from app.services.dft_review_bundle_service import DFTReviewBundleService, FIGURE_TABLE_REVIEW_READY_STATUSES
from app.services.evidence_review_bundle_service import FINALIZED_REVIEW_STATUSES


class DFTCompletenessService:
    """Single read-only source for paper-level DFT completeness semantics."""

    EXPLICIT_CANDIDATE_DISPOSITIONS = (set(FINALIZED_REVIEW_STATUSES) - {"ai_reviewed"}) | {
        "ignored",
        "rejected",
        "ai_rejected",
        "rejected_by_local_ai",
    }
    MATERIALIZED_CANDIDATE_DISPOSITIONS = {"materialized", "ai_applied"}
    VERIFIED_RESULT_STATUSES = {"ml_ready", "ai_verified_ml_ready"}
    CONFLICT_ISSUE_TYPES = {"duplicate_suspected", "negative_consensus"}

    def __init__(self, session: Session) -> None:
        self.session = session

    def evaluate(
        self,
        paper_id: UUID,
        *,
        exported_verified_rows: int = 0,
        excluded_rows: int = 0,
    ) -> dict[str, Any]:
        lifecycle = self._evaluate_lifecycle(paper_id)
        scope = self._evaluate_review_scope(paper_id)
        lifecycle_reconciled = not lifecycle["lifecycle_blockers"]
        review_scope_complete = not scope["review_scope_blockers"]
        return {
            "paper_id": str(paper_id),
            "lifecycle_reconciled": lifecycle_reconciled,
            "review_scope_complete": review_scope_complete,
            "is_complete": lifecycle_reconciled and review_scope_complete,
            "exported_verified_rows": int(exported_verified_rows),
            "excluded_rows": int(excluded_rows),
            "lifecycle_blockers": lifecycle["lifecycle_blockers"],
            "completeness_blockers": lifecycle["lifecycle_blockers"],
            "review_scope_blockers": scope["review_scope_blockers"],
            "lifecycle_counts": lifecycle["lifecycle_counts"],
            "lifecycle_ids": lifecycle["lifecycle_ids"],
            "review_scope_details": scope["review_scope_details"],
            "identity_version": 2,
            "source_snapshot_fingerprint": scope["source_snapshot_fingerprint"],
        }

    def evaluate_export(
        self,
        *,
        paper_id: UUID | None,
        exported_verified_rows: int,
        excluded_rows: int,
    ) -> dict[str, Any]:
        if paper_id is not None:
            return self.evaluate(
                paper_id,
                exported_verified_rows=exported_verified_rows,
                excluded_rows=excluded_rows,
            )
        return {
            "lifecycle_reconciled": False,
            "review_scope_complete": False,
            "is_complete": False,
            "exported_verified_rows": int(exported_verified_rows),
            "excluded_rows": int(excluded_rows),
            "lifecycle_blockers": ["paper_scope_not_provided"],
            "completeness_blockers": ["paper_scope_not_provided"],
            "review_scope_blockers": ["paper_scope_not_provided"],
            "identity_version": 2,
            "source_snapshot_fingerprint": None,
        }

    def _evaluate_lifecycle(self, paper_id: UUID) -> dict[str, Any]:
        candidates = [
            candidate
            for candidate in self.session.scalars(
                select(ExternalAnalysisCandidate).where(
                    ExternalAnalysisCandidate.paper_id == paper_id,
                    ExternalAnalysisCandidate.candidate_type == "object_review_audit",
                )
            ).all()
            if self._is_discovered_dft_candidate(candidate)
        ]
        results = {
            str(row.id): row
            for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        }
        issues = list(
            self.session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper_id)).all()
        )
        issue_by_id = {issue.id: issue for issue in issues}
        source_issue_ids: dict[UUID, set[UUID]] = defaultdict(set)
        candidate_ids = [candidate.id for candidate in candidates]
        if candidate_ids:
            for source in self.session.scalars(
                select(DFTAuditIssueSource).where(DFTAuditIssueSource.candidate_id.in_(candidate_ids))
            ).all():
                source_issue_ids[source.candidate_id].add(source.issue_id)

        unhandled_ids: list[str] = []
        materialized_unbound_ids: list[str] = []
        for candidate in candidates:
            status = str(candidate.status or "").strip().lower()
            target_type = str(candidate.materialized_target_type or "").strip().lower()
            target_id = str(candidate.materialized_target_id or "").strip()
            appears_materialized = status in self.MATERIALIZED_CANDIDATE_DISPOSITIONS or bool(target_type or target_id)
            row = results.get(target_id) if target_type == "dft_results" else None
            linked = False
            if row is not None:
                for issue_id in source_issue_ids.get(candidate.id, set()):
                    issue = issue_by_id.get(issue_id)
                    if issue is not None and self._issue_result_id(issue) == target_id:
                        linked = True
                        break
            if not self._has_explicit_candidate_disposition(candidate, row_exists=row is not None, linked=linked):
                unhandled_ids.append(str(candidate.id))
            if appears_materialized and (row is None or not linked):
                materialized_unbound_ids.append(str(candidate.id))

        pending_issues = [
            issue for issue in issues if issue.status in DFT_AUDIT_ISSUE_PENDING_STATUSES
        ]
        verified_open_missing_ids: list[str] = []
        for issue in pending_issues:
            if issue.issue_type != "missing_dft_result":
                continue
            result_id = self._issue_result_id(issue)
            row = results.get(result_id or "")
            if row is not None and str(row.candidate_status or "").strip().lower() in self.VERIFIED_RESULT_STATUSES:
                verified_open_missing_ids.append(str(issue.id))

        conflict_ids = [
            str(issue.id)
            for issue in pending_issues
            if self._is_identity_or_binding_conflict(issue)
        ]
        blockers: list[str] = []
        if unhandled_ids:
            blockers.append("unhandled_dft_candidates")
        if materialized_unbound_ids:
            blockers.append("materialized_dft_candidates_unbound")
        if verified_open_missing_ids:
            blockers.append("verified_results_with_open_missing_dft_result")
        if conflict_ids:
            blockers.append("identity_or_binding_conflicts_open")
        return {
            "lifecycle_blockers": blockers,
            "lifecycle_counts": {
                "discovered_candidate_count": len(candidates),
                "unhandled_candidate_count": len(unhandled_ids),
                "materialized_unbound_count": len(materialized_unbound_ids),
                "verified_open_missing_count": len(verified_open_missing_ids),
                "identity_binding_conflict_count": len(conflict_ids),
            },
            "lifecycle_ids": {
                "unhandled_candidate_ids": unhandled_ids,
                "materialized_unbound_candidate_ids": materialized_unbound_ids,
                "verified_open_missing_issue_ids": verified_open_missing_ids,
                "identity_binding_conflict_issue_ids": conflict_ids,
            },
        }

    def _evaluate_review_scope(self, paper_id: UUID) -> dict[str, Any]:
        blockers: list[str] = []
        details: dict[str, Any] = {}
        fingerprint: str | None = None
        try:
            snapshot = DFTReviewBundleService(
                self.session,
                get_settings(),
            ).get_completeness_snapshot(paper_id)
        except (LookupError, OSError, ValueError) as exc:
            return {
                "review_scope_blockers": ["review_scope_snapshot_not_available"],
                "review_scope_details": {"snapshot_error": str(exc)},
                "source_snapshot_fingerprint": None,
            }

        fingerprint = str(snapshot.get("source_snapshot_fingerprint") or "").strip() or None
        inventory = snapshot.get("source_pdf_inventory") if isinstance(snapshot.get("source_pdf_inventory"), list) else []
        main_sources = [item for item in inventory if item.get("role") == "main"]
        if len(main_sources) != 1:
            blockers.append("source_pdf_inventory_ambiguous")
        missing_main = [str(item.get("paper_id")) for item in main_sources if not item.get("pdf_available")]
        missing_si = [
            str(item.get("paper_id"))
            for item in inventory
            if item.get("role") != "main" and not item.get("pdf_available")
        ]
        omitted = [
            str(item.get("paper_id"))
            for item in inventory
            if item.get("pdf_available") and not item.get("included_in_bundle")
        ]
        if missing_main:
            blockers.append("main_source_pdf_missing")
        if missing_si:
            blockers.append("linked_si_source_pdf_missing")
        if omitted:
            blockers.append("source_pdf_inventory_incomplete")
        details["source_pdf_inventory"] = {
            "count": len(inventory),
            "missing_main_paper_ids": missing_main,
            "missing_si_paper_ids": missing_si,
            "omitted_paper_ids": omitted,
        }

        gate = snapshot.get("review_gate") if isinstance(snapshot.get("review_gate"), dict) else {}
        stage = str(gate.get("stage_status") or "").strip()
        current_chart = str(gate.get("current_snapshot_fingerprint") or "").strip()
        completed_chart = str(gate.get("completed_snapshot_fingerprint") or "").strip()
        if stage not in FIGURE_TABLE_REVIEW_READY_STATUSES:
            blockers.append("chart_review_scope_not_complete")
        if not current_chart or not completed_chart or current_chart != completed_chart:
            blockers.append("chart_review_scope_stale")
        rag_status = str(gate.get("rag_quality_status") or "ready").strip()
        if rag_status != "ready":
            blockers.append("chart_review_scope_not_complete")
        details["chart_review"] = {
            "stage_status": stage or None,
            "current_snapshot_fingerprint": current_chart or None,
            "completed_snapshot_fingerprint": completed_chart or None,
            "rag_quality_status": rag_status or None,
        }

        discovery = self._latest_gap_discovery(paper_id)
        if discovery is None:
            blockers.append("dft_gap_discovery_not_current")
        else:
            metadata = discovery["metadata"]
            coverage = discovery["coverage"]
            if (
                str(metadata.get("review_mode") or "") != "comprehensive_review"
                or str(metadata.get("overall_status") or "") != "completed"
                or coverage.get("missing_data_search_complete") is not True
            ):
                blockers.append("dft_gap_discovery_not_current")
            discovery_fingerprint = str(metadata.get("bundle_fingerprint") or "").strip()
            discovery_chart = str(metadata.get("figure_table_completed_snapshot_fingerprint") or "").strip()
            if not fingerprint or discovery_fingerprint != fingerprint:
                blockers.append("source_snapshot_mismatch")
            if completed_chart and discovery_chart != completed_chart:
                blockers.append("source_snapshot_mismatch")
            details["dft_gap_discovery"] = {
                "run_id": discovery["run_id"],
                "bundle_fingerprint": discovery_fingerprint or None,
                "missing_data_search_complete": coverage.get("missing_data_search_complete") is True,
            }
        return {
            "review_scope_blockers": list(dict.fromkeys(blockers)),
            "review_scope_details": details,
            "source_snapshot_fingerprint": fingerprint,
        }

    def _latest_gap_discovery(self, paper_id: UUID) -> dict[str, Any] | None:
        runs = self.session.scalars(
            select(ExternalAnalysisRun)
            .where(ExternalAnalysisRun.paper_id == paper_id)
            .order_by(ExternalAnalysisRun.created_at.desc(), ExternalAnalysisRun.id.desc())
        ).all()
        for run in runs:
            payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
            metadata = payload.get("review_metadata") if isinstance(payload.get("review_metadata"), dict) else {}
            if str(metadata.get("review_mode") or "") != "comprehensive_review":
                continue
            coverage = payload.get("coverage_acknowledgement")
            return {
                "run_id": str(run.id),
                "metadata": metadata,
                "coverage": coverage if isinstance(coverage, dict) else {},
            }
        return None

    @staticmethod
    def _is_discovered_dft_candidate(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        target_type = str(payload.get("target_type") or "").strip().lower()
        target_id = str(payload.get("target_id") or "").strip().lower()
        decision = str(payload.get("decision") or "").strip().lower()
        return target_type in {"dft_result", "dft_results", "dftresult"} and (
            decision == "new_candidate" or target_id == "new"
        )

    @classmethod
    def _has_explicit_candidate_disposition(
        cls,
        candidate: ExternalAnalysisCandidate,
        *,
        row_exists: bool,
        linked: bool,
    ) -> bool:
        status = str(candidate.status or "").strip().lower()
        if status in cls.EXPLICIT_CANDIDATE_DISPOSITIONS:
            return True
        if status != "ai_reviewed":
            return False
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        decision = str(payload.get("decision") or "").strip().lower()
        recommended = str(payload.get("recommended_action") or payload.get("action") or "").strip().lower()
        explicit_nonmaterialized = decision in {"reject", "rejected", "mark_reviewed"} or recommended in {
            "reject",
            "rejected",
            "mark_reviewed",
            "ignore",
            "ignored",
        }
        return explicit_nonmaterialized or (row_exists and linked)

    @staticmethod
    def _issue_result_id(issue: DFTAuditIssue) -> str | None:
        if issue.result_id is not None:
            return str(issue.result_id)
        target_type = str(issue.target_type or "").strip().lower()
        target_id = str(issue.target_id or "").strip()
        return target_id if target_type == "dft_results" and target_id.lower() != "new" else None

    @classmethod
    def _is_identity_or_binding_conflict(cls, issue: DFTAuditIssue) -> bool:
        values = {
            str(issue.lifecycle_stage or "").strip().lower(),
            str(issue.resolution_code or "").strip().lower(),
            str(issue.last_error_code or "").strip().lower(),
        }
        if values & {"binding_conflict", "scientific_conflict", "identity_conflict"}:
            return True
        return issue.issue_type in cls.CONFLICT_ISSUE_TYPES and issue.result_id is not None
