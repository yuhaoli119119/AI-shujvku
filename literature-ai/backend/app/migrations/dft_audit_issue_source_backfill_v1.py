from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection


MIGRATION_VERSION = "007_dft_audit_issue_source_backfill_v1"


class DFTAuditIssueSourceBackfillError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def _table_exists(connection: Connection) -> bool:
    return bool(
        connection.scalar(
            text("SELECT to_regclass('dft_audit_issue_sources') IS NOT NULL")
        )
    )


def _issue_rows(connection: Connection, paper_id: UUID | None) -> list[dict[str, Any]]:
    paper_filter = ""
    parameters: dict[str, Any] = {}
    if paper_id is not None:
        paper_filter = "AND i.paper_id = CAST(:paper_id AS uuid)"
        parameters["paper_id"] = str(paper_id)
    return [
        dict(row)
        for row in connection.execute(
            text(
                f"""
                SELECT i.id, i.paper_id, p.paper_code, i.source_candidate_ids
                FROM dft_audit_issues AS i
                JOIN papers AS p ON p.id = i.paper_id
                WHERE i.source_candidate_ids IS NOT NULL
                  AND i.source_candidate_ids <> '[]'::jsonb
                  {paper_filter}
                ORDER BY i.paper_id, i.id
                """
            ),
            parameters,
        ).mappings()
    ]


def _candidate_papers(connection: Connection, candidate_ids: set[str]) -> dict[str, str]:
    if not candidate_ids:
        return {}
    rows = connection.execute(
        text(
            """
            SELECT id::text AS candidate_id, paper_id::text AS paper_id
            FROM external_analysis_candidates
            WHERE id::text = ANY(CAST(:candidate_ids AS text[]))
            """
        ),
        {"candidate_ids": sorted(candidate_ids)},
    ).mappings()
    return {row["candidate_id"]: row["paper_id"] for row in rows}


def _existing_relations(
    connection: Connection,
    issue_ids: set[str],
) -> set[tuple[str, str]]:
    if not issue_ids or not _table_exists(connection):
        return set()
    rows = connection.execute(
        text(
            """
            SELECT issue_id::text AS issue_id, candidate_id::text AS candidate_id
            FROM dft_audit_issue_sources
            WHERE issue_id::text = ANY(CAST(:issue_ids AS text[]))
            """
        ),
        {"issue_ids": sorted(issue_ids)},
    ).mappings()
    return {(row["issue_id"], row["candidate_id"]) for row in rows}


def analyze(connection: Connection, *, paper_id: UUID | None = None) -> dict[str, Any]:
    """Build a read-only, itemized comparison of legacy JSON and normalized relations."""

    issues = _issue_rows(connection, paper_id)
    parsed: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    raw_reference_count = 0

    for issue in issues:
        issue_id = str(issue["id"])
        issue_paper_id = str(issue["paper_id"])
        values = issue["source_candidate_ids"]
        if not isinstance(values, list):
            errors.append(
                {
                    "issue_id": issue_id,
                    "paper_id": issue_paper_id,
                    "paper_code": issue["paper_code"],
                    "candidate_id": None,
                    "reason": "invalid_source_candidate_ids_shape",
                }
            )
            continue
        for ordinal, raw_candidate_id in enumerate(values, start=1):
            raw_reference_count += 1
            try:
                if not isinstance(raw_candidate_id, str):
                    raise ValueError
                candidate_id = str(UUID(raw_candidate_id))
            except (ValueError, TypeError, AttributeError):
                errors.append(
                    {
                        "issue_id": issue_id,
                        "paper_id": issue_paper_id,
                        "paper_code": issue["paper_code"],
                        "candidate_id": raw_candidate_id,
                        "ordinal": ordinal,
                        "reason": "invalid_uuid",
                    }
                )
                continue
            relation = (issue_id, candidate_id)
            if relation in seen:
                errors.append(
                    {
                        "issue_id": issue_id,
                        "paper_id": issue_paper_id,
                        "paper_code": issue["paper_code"],
                        "candidate_id": candidate_id,
                        "ordinal": ordinal,
                        "reason": "duplicate_reference",
                    }
                )
                continue
            seen.add(relation)
            parsed.append(
                {
                    "issue_id": issue_id,
                    "paper_id": issue_paper_id,
                    "paper_code": str(issue["paper_code"]),
                    "candidate_id": candidate_id,
                }
            )

    candidate_papers = _candidate_papers(
        connection,
        {row["candidate_id"] for row in parsed},
    )
    eligible: set[tuple[str, str]] = set()
    paper_for_issue = {str(row["id"]): str(row["paper_id"]) for row in issues}
    code_for_paper = {str(row["paper_id"]): str(row["paper_code"]) for row in issues}
    for row in parsed:
        candidate_paper_id = candidate_papers.get(row["candidate_id"])
        if candidate_paper_id is None:
            errors.append({**row, "reason": "candidate_not_found"})
        elif candidate_paper_id != row["paper_id"]:
            errors.append(
                {
                    **row,
                    "candidate_paper_id": candidate_paper_id,
                    "reason": "cross_paper_candidate",
                }
            )
        else:
            eligible.add((row["issue_id"], row["candidate_id"]))

    issue_ids = {str(row["id"]) for row in issues}
    existing = _existing_relations(connection, issue_ids)
    unexpected = existing - eligible
    for issue_id, candidate_id in sorted(unexpected):
        issue_paper_id = paper_for_issue[issue_id]
        errors.append(
            {
                "issue_id": issue_id,
                "paper_id": issue_paper_id,
                "paper_code": code_for_paper[issue_paper_id],
                "candidate_id": candidate_id,
                "reason": "unexpected_normalized_relation",
            }
        )

    errors.sort(
        key=lambda item: (
            str(item.get("paper_code") or ""),
            str(item.get("issue_id") or ""),
            int(item.get("ordinal") or 0),
            str(item.get("candidate_id") or ""),
            str(item["reason"]),
        )
    )
    missing = eligible - existing
    counts_by_paper: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "paper_code": None,
            "issues_with_sources": 0,
            "expected_source_relations": 0,
            "distinct_candidates": set(),
            "existing_relations": 0,
            "missing_relations": 0,
            "error_count": 0,
        }
    )
    for issue in issues:
        key = str(issue["paper_id"])
        counts_by_paper[key]["paper_code"] = str(issue["paper_code"])
        counts_by_paper[key]["issues_with_sources"] += 1
        values = issue["source_candidate_ids"]
        if isinstance(values, list):
            counts_by_paper[key]["expected_source_relations"] += len(values)
    for row in parsed:
        counts_by_paper[row["paper_id"]]["distinct_candidates"].add(row["candidate_id"])
    for issue_id, _candidate_id in existing:
        counts_by_paper[paper_for_issue[issue_id]]["existing_relations"] += 1
    for issue_id, _candidate_id in missing:
        counts_by_paper[paper_for_issue[issue_id]]["missing_relations"] += 1
    for error in errors:
        counts_by_paper[str(error["paper_id"])]["error_count"] += 1

    per_paper = {}
    for key, item in sorted(counts_by_paper.items()):
        item["distinct_candidates"] = len(item["distinct_candidates"])
        item["eligible"] = item["error_count"] == 0
        per_paper[key] = item

    reason_counts = {
        reason: sum(1 for item in errors if item["reason"] == reason)
        for reason in sorted({item["reason"] for item in errors})
    }
    report = {
        "migration_version": MIGRATION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "scope": {"paper_id": str(paper_id) if paper_id else None},
        "relation_table_present": _table_exists(connection),
        "issues_with_sources": len(issues),
        "expected_source_relations": raw_reference_count,
        "eligible_source_relations": len(eligible),
        "distinct_candidates": len({candidate_id for _issue_id, candidate_id in eligible}),
        "existing_relations": len(existing),
        "missing_relations": len(missing),
        "invalid_references": reason_counts.get("invalid_uuid", 0),
        "missing_candidates": reason_counts.get("candidate_not_found", 0),
        "cross_paper_references": reason_counts.get("cross_paper_candidate", 0),
        "unexpected_relations": reason_counts.get("unexpected_normalized_relation", 0),
        "error_count": len(errors),
        "error_counts": reason_counts,
        "errors": errors,
        "blocked_papers": sorted(
            key for key, item in per_paper.items() if not item["eligible"]
        ),
        "per_paper": per_paper,
        "database_writes": 0,
        "eligible": not errors,
        "status": "validated" if not errors else "blocked",
    }
    report["_missing_pairs"] = sorted(missing)
    return report


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def upgrade(
    connection: Connection,
    *,
    paper_id: UUID | None = None,
    block_on_errors: bool = True,
    fault_after_insert: bool = False,
) -> dict[str, Any]:
    """Backfill one validated scope atomically; the caller owns the transaction."""

    if not _table_exists(connection):
        report = analyze(connection, paper_id=paper_id)
        report["mode"] = "apply"
        raise DFTAuditIssueSourceBackfillError("relation_table_not_installed", _public_report(report))

    report = analyze(connection, paper_id=paper_id)
    report["mode"] = "apply"
    if report["errors"]:
        report["status"] = "blocked"
        public = _public_report(report)
        if block_on_errors:
            raise DFTAuditIssueSourceBackfillError("source_reference_validation_failed", public)
        return public

    pairs = report["_missing_pairs"]
    inserted = 0
    if pairs:
        result = connection.execute(
            text(
                """
                INSERT INTO dft_audit_issue_sources (issue_id, candidate_id)
                SELECT source.issue_id, source.candidate_id
                FROM unnest(
                    CAST(:issue_ids AS uuid[]),
                    CAST(:candidate_ids AS uuid[])
                ) AS source(issue_id, candidate_id)
                ON CONFLICT (issue_id, candidate_id) DO NOTHING
                """
            ),
            {
                "issue_ids": [issue_id for issue_id, _candidate_id in pairs],
                "candidate_ids": [candidate_id for _issue_id, candidate_id in pairs],
            },
        )
        inserted = int(result.rowcount or 0)
    if fault_after_insert:
        raise RuntimeError("injected_fault:after_source_relation_insert")

    verified = analyze(connection, paper_id=paper_id)
    if verified["errors"] or verified["missing_relations"]:
        raise DFTAuditIssueSourceBackfillError(
            "source_relation_postcondition_failed",
            _public_report(verified),
        )
    verified.update(
        {
            "mode": "apply",
            "status": "completed",
            "inserted_relations": inserted,
            "database_writes": inserted,
        }
    )
    return _public_report(verified)
