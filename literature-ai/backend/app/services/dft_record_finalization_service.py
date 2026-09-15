from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.utils.review_safety import is_export_eligible_extraction


class DFTRecordFinalizationService:
    """Close the backfill audit issue of a record the AI has *fully* verified.

    Deliberately narrow and explicit:

    * scope is an explicit set of issue ids (or the pending ``missing_dft_result``
      issues of explicitly named records) - never "all issues of this type";
    * every ``repair_awaiting_verification`` condition is re-evaluated *after* a
      transaction lock, against freshly read rows, and the export gate is resolved
      inside the lock - no compare-once-after-a-plain-read;
    * the precondition is the platform's own record-level export gate, which
      requires an authoritative verified review for *every* required field, so a
      single accepted field can never close a record's issue;
    * a record whose gate fails is left completely untouched (which also avoids
      bumping the lifecycle service's retry counters on a record that is simply not
      ready yet);
    * idempotent: an already-terminal issue is reported as such and nothing is
      written.
    """

    CLOSABLE_ISSUE_TYPES = frozenset({"missing_dft_result"})

    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = DFTAuditIssueLifecycleService(session)

    def finalize(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        result_ids: list[Any] | tuple[Any, ...] = (),
        issue_ids: list[Any] | tuple[Any, ...] = (),
    ) -> dict[str, Any]:
        targets = self._resolve_issue_ids(paper_id=paper_id, result_ids=result_ids, issue_ids=issue_ids)
        outcomes: list[dict[str, Any]] = []
        for issue_id in targets:
            outcome = self.lifecycle.close_repair_issue(
                issue_id=issue_id,
                reviewer=reviewer,
                export_gate_resolver=self._export_gate,
                expected_paper_id=paper_id,
            )
            if outcome.get("closed"):
                self._record_audit(paper_id=paper_id, reviewer=reviewer, outcome=outcome)
            outcomes.append(outcome)
        self.session.flush()
        return {
            "paper_id": str(paper_id),
            "reviewer": reviewer,
            "result_ids": [str(value) for value in (result_ids or [])],
            "issue_ids": [str(value) for value in targets],
            "issues": outcomes,
            "closed_total": sum(1 for outcome in outcomes if outcome.get("closed")),
            "refused_total": sum(1 for outcome in outcomes if not outcome.get("closed")),
        }

    # ----------------------------------------------------------------- helpers
    def _export_gate(self, row: DFTResult):
        return is_export_eligible_extraction(self.session, row, target_type="dft_results")

    def _resolve_issue_ids(
        self,
        *,
        paper_id: UUID,
        result_ids: list[Any] | tuple[Any, ...],
        issue_ids: list[Any] | tuple[Any, ...],
    ) -> list[UUID]:
        resolved: list[UUID] = []
        seen: set[UUID] = set()

        def _remember(value: UUID) -> None:
            if value not in seen:
                seen.add(value)
                resolved.append(value)

        for raw in issue_ids or []:
            try:
                _remember(raw if isinstance(raw, UUID) else UUID(str(raw)))
            except (TypeError, ValueError):
                continue
        for raw in result_ids or []:
            try:
                result_id = raw if isinstance(raw, UUID) else UUID(str(raw))
            except (TypeError, ValueError):
                continue
            for issue in self.lifecycle.active_issues_for_target(
                paper_id=paper_id,
                target_type="dft_results",
                target_id=result_id,
            ):
                if issue.issue_type in self.CLOSABLE_ISSUE_TYPES:
                    _remember(issue.id)
        return resolved

    def _record_audit(self, *, paper_id: UUID, reviewer: str, outcome: dict[str, Any]) -> None:
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="finalize_ai_verified_dft_record",
                source="single_ai_verification",
                target_type="dft_results",
                target_id=str(outcome.get("result_id") or ""),
                payload={
                    "actor_type": "ai",
                    "reviewer": reviewer,
                    "issue_id": outcome.get("issue_id"),
                    "result_id": outcome.get("result_id"),
                    "closed_issue_ids": [outcome.get("issue_id")] if outcome.get("closed") else [],
                    "export_gate_eligible": bool(outcome.get("export_gate_eligible")),
                    "materialized_candidates": list(outcome.get("materialized_candidates") or []),
                    "closable_issue_types": sorted(self.CLOSABLE_ISSUE_TYPES),
                },
            )
        )
