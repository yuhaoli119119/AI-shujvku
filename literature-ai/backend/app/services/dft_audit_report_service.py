from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTAuditIssue, utcnow
from app.mcp.auth import parse_mcp_api_keys, validate_mcp_capability_assignments
from app.services.dft_audit_issue_service import DFT_AUDIT_ISSUE_OPEN_STATUSES


REPORT_ACTIVE_STATUSES = set(DFT_AUDIT_ISSUE_OPEN_STATUSES) | {"fixed_by_primary_ai"}
EXPECTED_REPAIR_CAPABILITY = "repair_dft_issues"
EXPECTED_REPAIR_ROLES = {"primary_ai_repair", "dft_issue_repair"}


class DFTAuditReportService:
    def __init__(self, session: Session):
        self.session = session

    def build_report(
        self,
        *,
        paper_id: UUID | None = None,
        days: int = 30,
        include_closed: bool = False,
        mcp_api_keys: str = "",
    ) -> dict[str, Any]:
        days = max(1, min(int(days or 30), 365))
        start_at = utcnow() - timedelta(days=days)
        issue_rows = self._issue_rows(paper_id=paper_id, start_at=start_at, include_closed=include_closed)
        repair_logs = self._repair_logs(paper_id=paper_id, start_at=start_at)
        issue_by_id = {str(issue.id): issue for issue in issue_rows}
        missing_issue_ids = {
            str(log.target_id)
            for log in repair_logs
            if str(log.target_id or "") and str(log.target_id) not in issue_by_id
        }
        if missing_issue_ids:
            valid_missing_issue_ids: list[UUID] = []
            for item in missing_issue_ids:
                try:
                    valid_missing_issue_ids.append(UUID(item))
                except ValueError:
                    continue
            extra_stmt = select(DFTAuditIssue).where(DFTAuditIssue.id.in_(valid_missing_issue_ids))
            if paper_id is not None:
                extra_stmt = extra_stmt.where(DFTAuditIssue.paper_id == paper_id)
            if valid_missing_issue_ids:
                for issue in self.session.scalars(extra_stmt).all():
                    issue_by_id[str(issue.id)] = issue

        repair_action_counts: Counter[str] = Counter()
        repair_actor_counts: Counter[tuple[str, str, str]] = Counter()
        repair_issue_type_counts: Counter[str] = Counter()
        suspect_warnings: list[dict[str, Any]] = []
        writes_final_truth_count = 0

        for log in repair_logs:
            payload = log.payload if isinstance(log.payload, dict) else {}
            action = self._text(payload.get("action"), "unknown")
            repair_action_counts[action] += 1

            actor = payload.get("repair_actor") if isinstance(payload.get("repair_actor"), dict) else {}
            source_prefix = self._text(payload.get("source_prefix"), actor.get("source_prefix"), log.source, "unknown")
            actor_role = self._text(payload.get("actor_role"), actor.get("actor_role"), "missing")
            capability_used = self._text(
                payload.get("capability_used"),
                payload.get("required_capability"),
                actor.get("required_capability"),
                "missing",
            )
            repair_actor_counts[(source_prefix, actor_role, capability_used)] += 1

            issue = issue_by_id.get(str(log.target_id or ""))
            repair_issue_type_counts[issue.issue_type if issue is not None else "unknown"] += 1

            if payload.get("writes_final_truth") is True:
                writes_final_truth_count += 1
                suspect_warnings.append(
                    self._warning(
                        code="repair_writes_final_truth",
                        message="repair_dft_audit_issue AuditLog unexpectedly reports writes_final_truth=true",
                        source_prefix=source_prefix,
                        actor_role=actor_role,
                        capability_used=capability_used,
                        audit_log_id=str(log.id),
                        issue_id=str(log.target_id) if log.target_id else None,
                    )
                )
            if capability_used != EXPECTED_REPAIR_CAPABILITY:
                suspect_warnings.append(
                    self._warning(
                        code="unexpected_repair_capability",
                        message="repair_dft_audit_issue should use repair_dft_issues capability",
                        source_prefix=source_prefix,
                        actor_role=actor_role,
                        capability_used=capability_used,
                        audit_log_id=str(log.id),
                        issue_id=str(log.target_id) if log.target_id else None,
                    )
                )
            if actor_role not in EXPECTED_REPAIR_ROLES:
                suspect_warnings.append(
                    self._warning(
                        code="unexpected_repair_actor_role",
                        message="repair_dft_audit_issue should be performed by a primary repair actor role",
                        source_prefix=source_prefix,
                        actor_role=actor_role,
                        capability_used=capability_used,
                        audit_log_id=str(log.id),
                        issue_id=str(log.target_id) if log.target_id else None,
                    )
                )

        status_counts = Counter(issue.status for issue in issue_rows)
        issue_type_counts = Counter(issue.issue_type for issue in issue_rows)

        return {
            "schema_version": "dft_audit_report_v1",
            "filters": {
                "paper_id": str(paper_id) if paper_id else None,
                "days": days,
                "include_closed": include_closed,
                "start_at": start_at.isoformat(),
            },
            "issue_status_counts": dict(sorted(status_counts.items())),
            "issue_type_counts": dict(sorted(issue_type_counts.items())),
            "open_needs_user_decision_count": status_counts.get("needs_user_decision", 0),
            "open_needs_primary_ai_count": status_counts.get("needs_primary_ai", 0),
            "fixed_by_primary_ai_pending_review_count": status_counts.get("fixed_by_primary_ai", 0),
            "repair_action_counts": dict(sorted(repair_action_counts.items())),
            "repair_actor_counts": [
                {
                    "source_prefix": source_prefix,
                    "actor_role": actor_role,
                    "capability_used": capability_used,
                    "count": count,
                }
                for (source_prefix, actor_role, capability_used), count in sorted(repair_actor_counts.items())
            ],
            "repair_issue_type_counts": dict(sorted(repair_issue_type_counts.items())),
            "repair_writes_final_truth_count": writes_final_truth_count,
            "suspect_repair_actor_warnings": suspect_warnings,
            "mcp_capability_warnings": validate_mcp_capability_assignments(parse_mcp_api_keys(mcp_api_keys)),
        }

    def _issue_rows(self, *, paper_id: UUID | None, start_at, include_closed: bool) -> list[DFTAuditIssue]:
        stmt = select(DFTAuditIssue).where(DFTAuditIssue.created_at >= start_at)
        if paper_id is not None:
            stmt = stmt.where(DFTAuditIssue.paper_id == paper_id)
        if not include_closed:
            stmt = stmt.where(DFTAuditIssue.status.in_(REPORT_ACTIVE_STATUSES))
        return list(self.session.scalars(stmt).all())

    def _repair_logs(self, *, paper_id: UUID | None, start_at) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == "repair_dft_audit_issue")
            .where(AuditLog.created_at >= start_at)
        )
        if paper_id is not None:
            stmt = stmt.where(AuditLog.paper_id == paper_id)
        return list(self.session.scalars(stmt).all())

    @staticmethod
    def _text(*values: Any) -> str:
        for value in values:
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _warning(**kwargs: Any) -> dict[str, Any]:
        return {key: value for key, value in kwargs.items() if value is not None}
