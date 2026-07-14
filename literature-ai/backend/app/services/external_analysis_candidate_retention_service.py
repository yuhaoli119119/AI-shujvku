from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    WorkflowJob,
)


def _uuid_paths(value: Any, expected: set[str], path: str = "$") -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            hits.extend(_uuid_paths(child, expected, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_uuid_paths(child, expected, f"{path}[{index}]"))
    elif isinstance(value, str) and value in expected:
        hits.append((value, path))
    return hits


class ReferencedExternalAnalysisCandidateError(ValueError):
    def __init__(self, references: dict[str, list[dict[str, Any]]]):
        self.references = references
        super().__init__("external_analysis_candidates_are_referenced")

    def api_detail(self) -> dict[str, Any]:
        return {
            "code": "external_analysis_candidates_are_referenced",
            "message": (
                "The analysis run contains candidates retained by DFT lineage or audit history. "
                "Delete was refused; archive or reconcile those references explicitly."
            ),
            "candidate_count": len(self.references),
            "references": self.references,
        }


class ExternalAnalysisCandidateRetentionService:
    """Protect candidate UUIDs referenced by normalized and legacy audit history."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_references(
        self,
        candidate_ids: Iterable[UUID],
    ) -> dict[str, list[dict[str, Any]]]:
        ids = {UUID(str(value)) for value in candidate_ids}
        if not ids:
            return {}
        id_text = {str(value) for value in ids}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.id.in_(ids))
        ).all()
        paper_ids = {candidate.paper_id for candidate in candidates}
        references: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for row in self.session.scalars(
            select(DFTAuditIssueSource).where(DFTAuditIssueSource.candidate_id.in_(ids))
        ).all():
            references[str(row.candidate_id)].append(
                {
                    "kind": "normalized_dft_issue_source",
                    "table": "dft_audit_issue_sources",
                    "row_id": str(row.issue_id),
                    "path": "candidate_id",
                }
            )

        if paper_ids:
            issues = self.session.scalars(
                select(DFTAuditIssue).where(DFTAuditIssue.paper_id.in_(paper_ids))
            ).all()
            for issue in issues:
                for column_name in (
                    "source_candidate_ids",
                    "current_snapshot",
                    "evidence_payload",
                ):
                    for candidate_id, path in _uuid_paths(
                        getattr(issue, column_name),
                        id_text,
                    ):
                        references[candidate_id].append(
                            {
                                "kind": "legacy_dft_issue_json",
                                "table": "dft_audit_issues",
                                "row_id": str(issue.id),
                                "path": f"{column_name}{path[1:]}",
                            }
                        )

            results = self.session.scalars(
                select(DFTResult).where(DFTResult.paper_id.in_(paper_ids))
            ).all()
            for result in results:
                for candidate_id, path in _uuid_paths(result.evidence_payload, id_text):
                    references[candidate_id].append(
                        {
                            "kind": "dft_result_evidence_json",
                            "table": "dft_results",
                            "row_id": str(result.id),
                            "path": f"evidence_payload{path[1:]}",
                        }
                    )

        # Audit envelopes are not guaranteed to carry paper_id. Scan all
        # persisted audit rows so cross-paper and paperless provenance cannot
        # be bypassed by deleting the candidate's owning run.
        for log in self.session.scalars(select(AuditLog)).all():
            if str(log.target_id or "") in id_text:
                references[str(log.target_id)].append(
                    {
                        "kind": "audit_log_target",
                        "table": "audit_logs",
                        "row_id": str(log.id),
                        "path": "target_id",
                    }
                )
            for candidate_id, path in _uuid_paths(log.payload, id_text):
                references[candidate_id].append(
                    {
                        "kind": "audit_log_payload",
                        "table": "audit_logs",
                        "row_id": str(log.id),
                        "path": f"payload{path[1:]}",
                    }
                )

        # Workflow jobs are cross-paper task/audit envelopes.  Scan their four
        # persisted JSON fields so a candidate cannot disappear while a saved
        # verification task still names it.
        for job in self.session.scalars(select(WorkflowJob)).all():
            for column_name in ("payload", "progress", "result", "runtime_context"):
                for candidate_id, path in _uuid_paths(
                    getattr(job, column_name),
                    id_text,
                ):
                    references[candidate_id].append(
                        {
                            "kind": "workflow_job_json",
                            "table": "workflow_jobs",
                            "row_id": str(job.job_id),
                            "path": f"{column_name}{path[1:]}",
                        }
                    )

        return {
            candidate_id: sorted(
                rows,
                key=lambda item: (item["table"], item["row_id"], item["path"]),
            )
            for candidate_id, rows in sorted(references.items())
            if rows
        }

    def assert_deletable(self, candidates: Iterable[ExternalAnalysisCandidate]) -> None:
        rows = list(candidates)
        references = self.find_references(candidate.id for candidate in rows)
        if references:
            raise ReferencedExternalAnalysisCandidateError(references)

    def archive_referenced_delete_unreferenced(
        self,
        candidates: Iterable[ExternalAnalysisCandidate],
        *,
        actor: str,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rows = list(candidates)
        references = self.find_references(candidate.id for candidate in rows)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        archived_ids: list[str] = []
        deleted_ids: list[str] = []
        already_archived_ids: list[str] = []
        for candidate in rows:
            candidate_id = str(candidate.id)
            if candidate_id in references:
                if candidate.archived_at is None:
                    candidate.archived_at = now
                    candidate.archived_by = actor
                    candidate.archive_reason = reason
                    candidate.archive_context = {
                        **(context or {}),
                        "references": references[candidate_id],
                    }
                    self.session.add(candidate)
                    archived_ids.append(candidate_id)
                else:
                    already_archived_ids.append(candidate_id)
                continue
            self.session.delete(candidate)
            deleted_ids.append(candidate_id)
        self.session.flush()
        return {
            "archived_referenced_candidates": len(archived_ids),
            "deleted_unreferenced_candidates": len(deleted_ids),
            "already_archived_candidates": len(already_archived_ids),
            "archived_candidate_ids": archived_ids,
            "deleted_candidate_ids": deleted_ids,
            "references": references,
        }
