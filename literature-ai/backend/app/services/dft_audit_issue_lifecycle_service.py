from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DFTAuditIssue, DFTResult, utcnow


DFT_AUDIT_ISSUE_PENDING_STATUSES = {
    "open",
    "needs_primary_ai",
    "needs_user_decision",
    "fixed_by_primary_ai",
}
DFT_AUDIT_ISSUE_TERMINAL_STATUSES = {"closed", "false_positive"}


class DFTAuditIssueLifecycleService:
    """Centralized lifecycle rules for DFT audit issues."""

    VERIFY_CLOSE_TYPES = {
        "wrong_value",
        "wrong_unit",
        "wrong_material",
        "wrong_adsorbate",
        "wrong_reaction_step",
        "wrong_property_type",
        "missing_evidence",
        "consensus_ready",
    }
    REJECT_CLOSE_TYPES = VERIFY_CLOSE_TYPES | {
        "missing_dft_result",
        "duplicate_suspected",
        "uncertain",
        "negative_consensus",
    }

    SNAPSHOT_FIELDS = (
        "id",
        "paper_id",
        "catalyst_sample_id",
        "adsorbate",
        "property_type",
        "value",
        "unit",
        "reaction_step",
        "candidate_status",
        "evidence_payload",
    )

    def __init__(self, session: Session):
        self.session = session

    def active_issues_for_target(
        self,
        *,
        paper_id: UUID,
        target_type: str = "dft_results",
        target_id: str | UUID,
    ) -> list[DFTAuditIssue]:
        return list(
            self.session.scalars(
                select(DFTAuditIssue)
                .where(DFTAuditIssue.paper_id == paper_id)
                .where(DFTAuditIssue.target_type == target_type)
                .where(DFTAuditIssue.target_id == str(target_id))
                .where(DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)))
                .order_by(DFTAuditIssue.created_at.asc(), DFTAuditIssue.id.asc())
            ).all()
        )

    def bind_missing_issue_to_result(
        self,
        issue: DFTAuditIssue,
        row: DFTResult,
        *,
        repaired_by: str,
        resolution_note: str | None = None,
    ) -> DFTAuditIssue:
        if issue.paper_id != row.paper_id:
            raise ValueError("DFT audit issue and DFT result belong to different papers.")
        issue.target_type = "dft_results"
        issue.target_id = str(row.id)
        issue.status = "fixed_by_primary_ai"
        issue.current_snapshot = self.snapshot_dft_result(row)
        issue.resolved_by = None
        issue.resolved_at = None
        issue.resolution_note = resolution_note or f"bound_dft_result:{row.id}"
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
        return issue

    def mark_pending(
        self,
        issue: DFTAuditIssue,
        *,
        status: str,
        note: str | None = None,
    ) -> DFTAuditIssue:
        if status not in DFT_AUDIT_ISSUE_PENDING_STATUSES:
            raise ValueError(f"Unsupported pending DFT audit issue status: {status}")
        issue.status = status
        issue.resolved_by = None
        issue.resolved_at = None
        issue.resolution_note = note
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
        return issue

    def close_issue(
        self,
        issue: DFTAuditIssue,
        *,
        resolved_by: str,
        resolution_note: str,
        status: str = "closed",
    ) -> bool:
        if status not in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal DFT audit issue status: {status}")
        if issue.status in DFT_AUDIT_ISSUE_TERMINAL_STATUSES and issue.resolution_note == resolution_note:
            return False
        issue.status = status
        issue.resolved_by = resolved_by
        issue.resolved_at = utcnow()
        issue.resolution_note = resolution_note
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
        return True

    def apply_human_verify(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
    ) -> list[DFTAuditIssue]:
        closed: list[DFTAuditIssue] = []
        for issue in self.active_issues_for_target(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=result_id,
        ):
            if issue.issue_type == "source_scope_error":
                continue
            if issue.issue_type == "missing_dft_result" and str(issue.target_id) == str(result_id):
                if self.close_issue(issue, resolved_by=reviewer, resolution_note="human_verified"):
                    closed.append(issue)
                continue
            if issue.issue_type in self.VERIFY_CLOSE_TYPES:
                if self.close_issue(issue, resolved_by=reviewer, resolution_note="human_verified"):
                    closed.append(issue)
        return closed

    def apply_human_reject(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
    ) -> list[DFTAuditIssue]:
        closed: list[DFTAuditIssue] = []
        for issue in self.active_issues_for_target(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=result_id,
        ):
            if issue.issue_type == "source_scope_error":
                continue
            if issue.issue_type in self.REJECT_CLOSE_TYPES:
                if self.close_issue(issue, resolved_by=reviewer, resolution_note="target_rejected"):
                    closed.append(issue)
        return closed

    def live_snapshot_for_issue(self, issue: DFTAuditIssue) -> dict[str, Any] | None:
        if issue.target_type != "dft_results":
            return None
        target_id = str(issue.target_id or "").strip()
        if not target_id or target_id.lower() == "new":
            return None
        try:
            row_id = UUID(target_id)
        except ValueError:
            return None
        row = self.session.get(DFTResult, row_id)
        if row is None or row.paper_id != issue.paper_id:
            return None
        return self.snapshot_dft_result(row)

    def stale_fields(self, issue: DFTAuditIssue, live_snapshot: dict[str, Any] | None) -> list[str]:
        if live_snapshot is None:
            return []
        stored = issue.current_snapshot if isinstance(issue.current_snapshot, dict) else {}
        fields: list[str] = []
        for field in self.SNAPSHOT_FIELDS:
            if field not in stored:
                continue
            if self._value_key(stored.get(field)) != self._value_key(live_snapshot.get(field)):
                fields.append(field)
        return fields

    def serialize_issue(self, issue: DFTAuditIssue) -> dict[str, Any]:
        live_snapshot = self.live_snapshot_for_issue(issue)
        stale_fields = self.stale_fields(issue, live_snapshot)
        return {
            "id": str(issue.id),
            "paper_id": str(issue.paper_id),
            "target_type": issue.target_type,
            "target_id": issue.target_id,
            "issue_type": issue.issue_type,
            "severity": issue.severity,
            "status": issue.status,
            "current_snapshot": issue.current_snapshot,
            "live_snapshot": live_snapshot,
            "is_stale": bool(stale_fields),
            "stale_fields": stale_fields,
            "suggested_value": issue.suggested_value,
            "suggested_dft": issue.suggested_dft,
            "evidence_payload": issue.evidence_payload,
            "source_identities": issue.source_identities or [],
            "source_candidate_ids": issue.source_candidate_ids or [],
            "fingerprint": issue.fingerprint,
            "resolution_note": issue.resolution_note,
            "resolved_by": issue.resolved_by,
            "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
            "created_at": issue.created_at.isoformat() if issue.created_at else None,
            "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        }

    @staticmethod
    def snapshot_dft_result(row: DFTResult) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "paper_id": str(row.paper_id),
            "catalyst_sample_id": str(row.catalyst_sample_id) if row.catalyst_sample_id else None,
            "adsorbate": row.adsorbate,
            "property_type": row.property_type,
            "value": row.value,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "candidate_status": row.candidate_status,
            "evidence_payload": row.evidence_payload,
        }

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, dict):
            return {str(key): DFTAuditIssueLifecycleService._value_key(val) for key, val in sorted(value.items())}
        if isinstance(value, list):
            return [DFTAuditIssueLifecycleService._value_key(item) for item in value]
        return str(value or "").strip().lower()
