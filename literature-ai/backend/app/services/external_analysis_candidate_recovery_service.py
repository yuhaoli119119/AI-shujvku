from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisCandidateRecovery,
    ExternalAnalysisRun,
)
from app.services.external_analysis_candidates import build_object_review_candidate_values
from app.services.external_analysis_models import ExternalObjectReviewAuditModel


RECOVERY_VERSION = "external_analysis_candidate_recovery_v1"


class ExternalAnalysisCandidateRecoveryError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _payload_sha256(values: dict[str, Any]) -> str:
    payload = {
        key: values.get(key)
        for key in (
            "run_id",
            "paper_id",
            "candidate_type",
            "normalized_payload",
            "confidence",
            "mapping_reason",
            "evidence_payload",
            "status",
            "materialized_target_type",
            "materialized_target_id",
        )
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _normalized_evidence_token(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def _evidence_manifest(value: Any) -> dict[str, list[str]]:
    collected: dict[str, set[str]] = {
        "evidence_ids": set(),
        "pages": set(),
        "figures": set(),
        "tables": set(),
        "sections": set(),
        "source_document_types": set(),
        "quoted_texts": set(),
    }
    key_map = {
        "evidence_id": "evidence_ids",
        "evidence_ids": "evidence_ids",
        "page": "pages",
        "pages": "pages",
        "figure": "figures",
        "figures": "figures",
        "table": "tables",
        "tables": "tables",
        "section": "sections",
        "sections": "sections",
        "source_document_type": "source_document_types",
        "quoted_text": "quoted_texts",
    }

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                target = key_map.get(str(key))
                if target:
                    values = child if isinstance(child, list) else [child]
                    for item in values:
                        if item is None or isinstance(item, (dict, list)):
                            continue
                        token = _normalized_evidence_token(item)
                        if token:
                            collected[target].add(token)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return {key: sorted(values) for key, values in collected.items()}


def _compare_evidence(issue_payload: Any, source_payload: Any) -> dict[str, Any]:
    issue = _evidence_manifest(issue_payload)
    source = _evidence_manifest(source_payload)
    mismatches: list[dict[str, Any]] = []
    for field, expected_values in issue.items():
        if not expected_values:
            continue
        source_values = source[field]
        if not source_values:
            mismatches.append(
                {
                    "field": field,
                    "reason": "source_evidence_missing",
                    "issue_values": expected_values,
                    "source_values": [],
                }
            )
            continue
        if not set(expected_values).intersection(source_values):
            mismatches.append(
                {
                    "field": field,
                    "reason": "source_evidence_conflict",
                    "issue_values": expected_values,
                    "source_values": source_values,
                }
            )
    return {
        "equivalent": not mismatches,
        "issue": issue,
        "source": source,
        "mismatches": mismatches,
    }


def _walk_candidate_items(value: Any, candidate_id: str) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if (
            str(value.get("candidate_id") or "") == candidate_id
            and str(value.get("action") or "").strip() == "apply_imported_dft_opinion"
            and str(value.get("candidate_status") or "").strip()
            and str(value.get("record_id") or "").strip()
        ):
            yield value
        for child in value.values():
            yield from _walk_candidate_items(child, candidate_id)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_candidate_items(child, candidate_id)


def _walk_object_review_audits(value: Any) -> Iterable[ExternalObjectReviewAuditModel]:
    if isinstance(value, dict):
        raw_audits = value.get("object_review_audits")
        if isinstance(raw_audits, list):
            for raw_audit in raw_audits:
                try:
                    yield ExternalObjectReviewAuditModel.model_validate(raw_audit)
                except Exception:
                    continue
        for key, child in value.items():
            if key != "object_review_audits":
                yield from _walk_object_review_audits(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_object_review_audits(child)


class ExternalAnalysisCandidateRecoveryService:
    """Recover deleted candidate UUIDs only from deterministic persisted evidence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def analyze(self, paper_ids: Iterable[UUID]) -> dict[str, Any]:
        ids = sorted({UUID(str(value)) for value in paper_ids}, key=str)
        per_paper = {str(paper_id): self._analyze_paper(paper_id) for paper_id in ids}
        errors = [
            {"paper_id": paper_id, **error}
            for paper_id, paper in per_paper.items()
            for error in paper["errors"]
        ]
        return {
            "recovery_version": RECOVERY_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "dry_run",
            "paper_ids": [str(value) for value in ids],
            "paper_count": len(ids),
            "expected_references": sum(row["expected_references"] for row in per_paper.values()),
            "distinct_candidate_ids": sum(row["distinct_candidate_ids"] for row in per_paper.values()),
            "existing_candidates": sum(row["existing_candidates"] for row in per_paper.values()),
            "missing_candidates": sum(row["missing_candidates"] for row in per_paper.values()),
            "recoverable_candidates": sum(row["recoverable_candidates"] for row in per_paper.values()),
            "conflict_count": len(errors),
            "errors": errors,
            "per_paper": per_paper,
            "database_writes": 0,
            "status": "blocked" if errors else "validated",
        }

    def _analyze_paper(self, paper_id: UUID) -> dict[str, Any]:
        issues = self.session.scalars(
            select(DFTAuditIssue)
            .where(DFTAuditIssue.paper_id == paper_id)
            .order_by(DFTAuditIssue.id)
        ).all()
        issue_refs: dict[UUID, list[DFTAuditIssue]] = defaultdict(list)
        errors: list[dict[str, Any]] = []
        expected_references = 0
        for issue in issues:
            values = issue.source_candidate_ids
            if not isinstance(values, list):
                errors.append(
                    {
                        "reason": "invalid_source_candidate_ids_shape",
                        "issue_id": str(issue.id),
                    }
                )
                continue
            for raw in values:
                expected_references += 1
                try:
                    candidate_id = UUID(str(raw))
                except (TypeError, ValueError, AttributeError):
                    errors.append(
                        {
                            "reason": "invalid_candidate_uuid",
                            "issue_id": str(issue.id),
                            "candidate_id": raw,
                        }
                    )
                    continue
                if issue not in issue_refs[candidate_id]:
                    issue_refs[candidate_id].append(issue)

        candidate_ids = set(issue_refs)
        existing = {
            candidate.id: candidate
            for candidate in self.session.scalars(
                select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.id.in_(candidate_ids))
            ).all()
        } if candidate_ids else {}
        recoveries = {
            row.candidate_id: row
            for row in self.session.scalars(
                select(ExternalAnalysisCandidateRecovery).where(
                    ExternalAnalysisCandidateRecovery.candidate_id.in_(candidate_ids)
                )
            ).all()
        } if candidate_ids else {}
        runs = self.session.scalars(
            select(ExternalAnalysisRun)
            .where(ExternalAnalysisRun.paper_id == paper_id)
            .order_by(ExternalAnalysisRun.created_at, ExternalAnalysisRun.id)
        ).all()
        logs = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == paper_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
        candidates_in_runs = self.session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper_id)
        ).all()

        items: list[dict[str, Any]] = []
        used_source_audits: dict[tuple[str, int], str] = {}
        for candidate_id in sorted(candidate_ids, key=str):
            referenced_issues = issue_refs[candidate_id]
            if candidate_id in existing:
                candidate = existing[candidate_id]
                if candidate.paper_id != paper_id:
                    errors.append(
                        {
                            "reason": "existing_candidate_cross_paper",
                            "candidate_id": str(candidate_id),
                            "candidate_paper_id": str(candidate.paper_id),
                        }
                    )
                    continue
                items.append(
                    {
                        "candidate_id": str(candidate_id),
                        "issue_ids": sorted(str(issue.id) for issue in referenced_issues),
                        "status": "already_recovered" if candidate_id in recoveries else "present",
                        "run_id": str(candidate.run_id),
                    }
                )
                continue
            try:
                item = self._recoverable_item(
                    paper_id=paper_id,
                    candidate_id=candidate_id,
                    issues=referenced_issues,
                    runs=runs,
                    logs=logs,
                    candidates_in_runs=candidates_in_runs,
                )
            except ExternalAnalysisCandidateRecoveryError as exc:
                errors.extend(exc.report["errors"])
                continue
            source_key = (item["run_id"], int(item["source_audit_index"]))
            previous = used_source_audits.get(source_key)
            if previous is not None:
                errors.append(
                    {
                        "reason": "source_audit_reused_by_multiple_candidate_ids",
                        "candidate_id": str(candidate_id),
                        "other_candidate_id": previous,
                        "run_id": source_key[0],
                        "source_audit_index": source_key[1],
                    }
                )
                continue
            used_source_audits[source_key] = str(candidate_id)
            items.append(item)

        return {
            "paper_id": str(paper_id),
            "expected_references": expected_references,
            "distinct_candidate_ids": len(candidate_ids),
            "existing_candidates": len(existing),
            "missing_candidates": len(candidate_ids - set(existing)),
            "recoverable_candidates": sum(1 for item in items if item["status"] == "recoverable"),
            "items": items,
            "errors": errors,
            "status": "blocked" if errors else "validated",
        }

    def _recoverable_item(
        self,
        *,
        paper_id: UUID,
        candidate_id: UUID,
        issues: list[DFTAuditIssue],
        runs: list[ExternalAnalysisRun],
        logs: list[AuditLog],
        candidates_in_runs: list[ExternalAnalysisCandidate],
        ignore_candidate_id: UUID | None = None,
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        if len(issues) != 1:
            errors.append(
                {
                    "reason": "candidate_referenced_by_multiple_issues",
                    "candidate_id": str(candidate_id),
                    "issue_ids": sorted(str(issue.id) for issue in issues),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})
        occurrence_map: dict[tuple[str, str], tuple[AuditLog, dict[str, Any]]] = {}
        for log in logs:
            for item in _walk_candidate_items(log.payload, str(candidate_id)):
                occurrence_map[(str(log.id), _canonical(item))] = (log, item)
        occurrences = list(occurrence_map.values())
        if len(occurrences) != 1:
            errors.append(
                {
                    "reason": "candidate_audit_log_match_not_unique",
                    "candidate_id": str(candidate_id),
                    "match_count": len(occurrences),
                    "audit_log_ids": sorted({str(log.id) for log, _item in occurrences}),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})
        log, event = occurrences[0]
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        source_label = str(result.get("source_label") or "").strip()
        record_id = str(event.get("record_id") or result.get("dft_result_id") or "").strip()
        event_status = str(event.get("candidate_status") or "").strip()
        if not source_label or not record_id or not event_status:
            errors.append(
                {
                    "reason": "candidate_audit_log_state_incomplete",
                    "candidate_id": str(candidate_id),
                    "audit_log_id": str(log.id),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        try:
            event_result_id = UUID(record_id)
        except (TypeError, ValueError, AttributeError):
            event_result_id = None
        event_result = self.session.get(DFTResult, event_result_id) if event_result_id else None
        event_target_exists = bool(event_result is not None and event_result.paper_id == paper_id)

        for issue in issues:
            if str(issue.status or "").strip().lower() != "closed":
                continue
            closed_target = issue.result_id
            if closed_target is None:
                try:
                    closed_target = UUID(str(issue.target_id))
                except (TypeError, ValueError, AttributeError):
                    closed_target = None
            closed_result = self.session.get(DFTResult, closed_target) if closed_target else None
            if closed_result is None or closed_result.paper_id != paper_id:
                errors.append(
                    {
                        "reason": "closed_issue_result_missing",
                        "candidate_id": str(candidate_id),
                        "issue_id": str(issue.id),
                        "target_id": str(issue.target_id or ""),
                        "result_id": str(issue.result_id or ""),
                    }
                )
        if errors:
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        expected_values = {
            _canonical(
                (issue.suggested_dft or {}).get("raw_corrected_value")
                if isinstance(issue.suggested_dft, dict)
                else None
            )
            for issue in issues
        }
        if len(expected_values) != 1 or expected_values == {_canonical(None)}:
            errors.append(
                {
                    "reason": "issue_scientific_payload_not_unique",
                    "candidate_id": str(candidate_id),
                    "issue_ids": sorted(str(issue.id) for issue in issues),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})
        expected_value = next(iter(expected_values))
        audit_matches: list[tuple[ExternalAnalysisRun, int, ExternalObjectReviewAuditModel]] = []
        for run in runs:
            if str(run.source_label or "").strip() != source_label or run.created_at > log.created_at:
                continue
            normalized = run.normalized_payload if isinstance(run.normalized_payload, dict) else {}
            raw_audits = normalized.get("object_review_audits")
            if not isinstance(raw_audits, list):
                continue
            for index, raw_audit in enumerate(raw_audits):
                try:
                    audit = ExternalObjectReviewAuditModel.model_validate(raw_audit)
                except Exception:
                    continue
                decision = str(audit.decision or "").strip().lower()
                if str(audit.target_type or "").strip().lower() not in {"dft_result", "dft_results"}:
                    continue
                if str(audit.target_id or "").strip().lower() != "new" or decision != "new_candidate":
                    continue
                if _canonical(audit.corrected_value) == expected_value:
                    audit_matches.append((run, index, audit))
        if len(audit_matches) != 1:
            errors.append(
                {
                    "reason": "source_run_payload_match_not_unique",
                    "candidate_id": str(candidate_id),
                    "source_label": source_label,
                    "match_count": len(audit_matches),
                    "matches": [
                        {"run_id": str(run.id), "source_audit_index": index}
                        for run, index, _audit in audit_matches
                    ],
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        run, source_audit_index, audit = audit_matches[0]
        source_identities = {
            str(identity).strip()
            for issue in issues
            for identity in (issue.source_identities or [])
            if str(identity).strip()
        }
        if source_identities and str(run.source_identity or "").strip() not in source_identities:
            errors.append(
                {
                    "reason": "source_identity_conflict",
                    "candidate_id": str(candidate_id),
                    "issue_source_identities": sorted(source_identities),
                    "run_source_identity": run.source_identity,
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        evidence_comparison = _compare_evidence(
            issues[0].evidence_payload,
            {
                "evidence_ids": audit.evidence_ids,
                "evidence_location": audit.evidence_location,
                "supporting_evidence": audit.supporting_evidence,
            },
        )
        if not evidence_comparison["equivalent"]:
            errors.append(
                {
                    "reason": "source_evidence_mismatch",
                    "candidate_id": str(candidate_id),
                    "issue_id": str(issues[0].id),
                    "comparison": evidence_comparison,
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        raw_audit_matches = []
        for raw_audit in _walk_object_review_audits(run.raw_payload):
            raw_evidence = _compare_evidence(
                issues[0].evidence_payload,
                {
                    "evidence_ids": raw_audit.evidence_ids,
                    "evidence_location": raw_audit.evidence_location,
                    "supporting_evidence": raw_audit.supporting_evidence,
                },
            )
            if (
                str(raw_audit.target_type or "").strip().lower() in {"dft_result", "dft_results"}
                and str(raw_audit.target_id or "").strip().lower() == "new"
                and str(raw_audit.decision or "").strip().lower() == "new_candidate"
                and _canonical(raw_audit.corrected_value) == expected_value
                and raw_evidence["equivalent"]
            ):
                raw_audit_matches.append(raw_audit)
        if len(raw_audit_matches) != 1:
            errors.append(
                {
                    "reason": "source_raw_payload_match_not_unique",
                    "candidate_id": str(candidate_id),
                    "run_id": str(run.id),
                    "match_count": len(raw_audit_matches),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        values = build_object_review_candidate_values(run, audit)
        restored_status = event_status if event_target_exists else "candidate"
        restored_target_type = "dft_results" if event_target_exists else None
        restored_target_id = record_id if event_target_exists else None
        values.update(
            {
                "status": restored_status,
                "materialized_target_type": restored_target_type,
                "materialized_target_id": restored_target_id,
                "created_at": run.created_at,
            }
        )
        duplicate_ids = [
            str(candidate.id)
            for candidate in candidates_in_runs
            if candidate.id != ignore_candidate_id
            and candidate.run_id == run.id
            and _canonical(candidate.normalized_payload) == _canonical(values["normalized_payload"])
        ]
        if duplicate_ids:
            errors.append(
                {
                    "reason": "source_audit_already_has_different_candidate",
                    "candidate_id": str(candidate_id),
                    "run_id": str(run.id),
                    "existing_candidate_ids": sorted(duplicate_ids),
                }
            )
            raise ExternalAnalysisCandidateRecoveryError("recovery_match_failed", {"errors": errors})

        payload_hash = _payload_sha256(values)
        return {
            "candidate_id": str(candidate_id),
            "issue_ids": sorted(str(issue.id) for issue in issues),
            "status": "recoverable",
            "run_id": str(run.id),
            "source_label": source_label,
            "source_audit_index": source_audit_index,
            "audit_log_ids": [str(log.id)],
            "payload_sha256": payload_hash,
            "restored_state": {
                "status": restored_status,
                "materialized_target_type": restored_target_type,
                "materialized_target_id": restored_target_id,
                "created_at": run.created_at.isoformat(),
                "historical_audit_status": event_status,
                "historical_audit_target_id": record_id,
                "historical_audit_target_exists": event_target_exists,
            },
            "match_manifest": {
                "candidate_uuid_source": "dft_audit_issues.source_candidate_ids",
                "state_source": f"audit_logs:{log.id}",
                "scientific_payload_source": (
                    f"external_analysis_runs:{run.id}:normalized_payload.object_review_audits"
                    f"[{source_audit_index}]"
                ),
                "match_fields": [
                    "paper_id",
                    "source_label",
                    "run_created_at<=audit_log_created_at",
                    "target_type=dft_results",
                    "target_id=new",
                    "decision=new_candidate",
                    "corrected_value=issue.suggested_dft.raw_corrected_value",
                    "source_identity=issue.source_identities",
                    "evidence_ids/page/figure/table/section/source_document_type/quoted_text",
                    "normalized_payload_sha256",
                    "closed_issue_target_exists_when_status=closed",
                    "historical_materialized_target_exists_or_restore_as_candidate",
                ],
                "audit_log_created_at": log.created_at.isoformat(),
                "source_audit_payload_sha256": hashlib.sha256(
                    _canonical(audit.model_dump(mode="json")).encode("utf-8")
                ).hexdigest(),
                "source_raw_audit_payload_sha256": hashlib.sha256(
                    _canonical(raw_audit_matches[0].model_dump(mode="json")).encode("utf-8")
                ).hexdigest(),
                "issue_current_snapshot_sha256": hashlib.sha256(
                    _canonical(issues[0].current_snapshot).encode("utf-8")
                ).hexdigest(),
                "evidence_comparison": evidence_comparison,
            },
            "_values": values,
        }

    @staticmethod
    def _public_report(report: dict[str, Any]) -> dict[str, Any]:
        clean = json.loads(json.dumps(report, ensure_ascii=False, default=str))
        for paper in clean.get("per_paper", {}).values():
            for item in paper.get("items", []):
                item.pop("_values", None)
        return clean

    def public_analyze(self, paper_ids: Iterable[UUID]) -> dict[str, Any]:
        return self._public_report(self.analyze(paper_ids))

    def apply(
        self,
        paper_ids: Iterable[UUID],
        *,
        actor: str,
        reason: str,
        fault_after_insert: int | None = None,
    ) -> dict[str, Any]:
        report = self.analyze(paper_ids)
        report["mode"] = "apply"
        if report["errors"]:
            raise ExternalAnalysisCandidateRecoveryError(
                "candidate_recovery_validation_failed",
                self._public_report(report),
            )
        inserted_ids: list[str] = []
        audit_rows = 0
        for paper_id, paper in report["per_paper"].items():
            paper_inserted: list[str] = []
            for item in paper["items"]:
                if item["status"] != "recoverable":
                    continue
                values = item["_values"]
                candidate = ExternalAnalysisCandidate(
                    id=UUID(item["candidate_id"]),
                    **values,
                )
                self.session.add(candidate)
                self.session.flush()
                self.session.add(
                    ExternalAnalysisCandidateRecovery(
                        candidate_id=candidate.id,
                        paper_id=UUID(paper_id),
                        run_id=UUID(item["run_id"]),
                        issue_ids=item["issue_ids"],
                        audit_log_ids=item["audit_log_ids"],
                        source_audit_index=item["source_audit_index"],
                        recovery_version=RECOVERY_VERSION,
                        payload_sha256=item["payload_sha256"],
                        match_manifest=item["match_manifest"],
                        restored_state=item["restored_state"],
                        reason=reason,
                        actor=actor,
                    )
                )
                self.session.flush()
                inserted_ids.append(item["candidate_id"])
                paper_inserted.append(item["candidate_id"])
                if fault_after_insert is not None and len(inserted_ids) >= fault_after_insert:
                    raise RuntimeError("fault_after_candidate_recovery_insert")
            if paper_inserted:
                self.session.add(
                    AuditLog(
                        paper_id=UUID(paper_id),
                        action="recover_external_analysis_candidates",
                        source=actor,
                        target_type="paper",
                        target_id=paper_id,
                        payload={
                            "recovery_version": RECOVERY_VERSION,
                            "reason": reason,
                            "restored_candidate_ids": paper_inserted,
                            "restored_candidate_count": len(paper_inserted),
                        },
                    )
                )
                audit_rows += 1
        self.session.flush()
        public = self._public_report(report)
        public["database_writes"] = len(inserted_ids)
        public["recovery_audit_rows"] = len(inserted_ids)
        public["summary_audit_log_rows"] = audit_rows
        public["restored_candidate_ids"] = inserted_ids
        public["status"] = "applied" if inserted_ids else "noop"
        return public

    def analyze_existing_states(self, paper_ids: Iterable[UUID]) -> dict[str, Any]:
        ids = sorted({UUID(str(value)) for value in paper_ids}, key=str)
        per_paper: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        for paper_id in ids:
            recoveries = self.session.scalars(
                select(ExternalAnalysisCandidateRecovery)
                .where(ExternalAnalysisCandidateRecovery.paper_id == paper_id)
                .order_by(ExternalAnalysisCandidateRecovery.candidate_id)
            ).all()
            runs = self.session.scalars(
                select(ExternalAnalysisRun)
                .where(ExternalAnalysisRun.paper_id == paper_id)
                .order_by(ExternalAnalysisRun.created_at, ExternalAnalysisRun.id)
            ).all()
            logs = self.session.scalars(
                select(AuditLog)
                .where(AuditLog.paper_id == paper_id)
                .order_by(AuditLog.created_at, AuditLog.id)
            ).all()
            candidates = self.session.scalars(
                select(ExternalAnalysisCandidate).where(
                    ExternalAnalysisCandidate.paper_id == paper_id
                )
            ).all()
            items: list[dict[str, Any]] = []
            for recovery in recoveries:
                candidate = self.session.get(ExternalAnalysisCandidate, recovery.candidate_id)
                issues = [
                    self.session.get(DFTAuditIssue, UUID(str(issue_id)))
                    for issue_id in (recovery.issue_ids or [])
                ]
                if candidate is None or any(issue is None for issue in issues):
                    error = {
                        "paper_id": str(paper_id),
                        "candidate_id": str(recovery.candidate_id),
                        "reason": "recovery_lineage_missing",
                    }
                    errors.append(error)
                    continue
                try:
                    desired = self._recoverable_item(
                        paper_id=paper_id,
                        candidate_id=candidate.id,
                        issues=[issue for issue in issues if issue is not None],
                        runs=runs,
                        logs=logs,
                        candidates_in_runs=candidates,
                        ignore_candidate_id=candidate.id,
                    )
                except ExternalAnalysisCandidateRecoveryError as exc:
                    errors.extend(
                        {"paper_id": str(paper_id), **error}
                        for error in exc.report["errors"]
                    )
                    continue
                if (
                    desired["run_id"] != str(recovery.run_id)
                    or desired["source_audit_index"] != recovery.source_audit_index
                ):
                    errors.append(
                        {
                            "paper_id": str(paper_id),
                            "candidate_id": str(candidate.id),
                            "reason": "recovery_source_lineage_changed",
                        }
                    )
                    continue
                desired_values = desired["_values"]
                immutable_fields = (
                    "run_id",
                    "paper_id",
                    "candidate_type",
                    "normalized_payload",
                    "confidence",
                    "mapping_reason",
                    "evidence_payload",
                )
                immutable_mismatches = [
                    field
                    for field in immutable_fields
                    if _canonical(getattr(candidate, field))
                    != _canonical(desired_values[field])
                ]
                if immutable_mismatches:
                    errors.append(
                        {
                            "paper_id": str(paper_id),
                            "candidate_id": str(candidate.id),
                            "reason": "recovered_candidate_payload_changed",
                            "fields": immutable_mismatches,
                        }
                    )
                    continue
                current_state = {
                    "status": candidate.status,
                    "materialized_target_type": candidate.materialized_target_type,
                    "materialized_target_id": candidate.materialized_target_id,
                }
                desired_state = {
                    "status": desired_values["status"],
                    "materialized_target_type": desired_values["materialized_target_type"],
                    "materialized_target_id": desired_values["materialized_target_id"],
                }
                items.append(
                    {
                        "candidate_id": str(candidate.id),
                        "status": "reconcile" if current_state != desired_state else "noop",
                        "current_state": current_state,
                        "desired_state": desired_state,
                        "payload_sha256": desired["payload_sha256"],
                        "match_manifest": desired["match_manifest"],
                        "restored_state": desired["restored_state"],
                    }
                )
            per_paper[str(paper_id)] = {
                "recovery_rows": len(recoveries),
                "reconcile_count": sum(item["status"] == "reconcile" for item in items),
                "items": items,
            }
        return {
            "recovery_version": RECOVERY_VERSION,
            "mode": "reconcile_existing_dry_run",
            "paper_ids": [str(value) for value in ids],
            "per_paper": per_paper,
            "reconcile_count": sum(
                paper["reconcile_count"] for paper in per_paper.values()
            ),
            "error_count": len(errors),
            "errors": errors,
            "database_writes": 0,
            "status": "blocked" if errors else "validated",
        }

    def reconcile_existing_states(
        self,
        paper_ids: Iterable[UUID],
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        report = self.analyze_existing_states(paper_ids)
        report["mode"] = "reconcile_existing_apply"
        if report["errors"]:
            raise ExternalAnalysisCandidateRecoveryError(
                "candidate_recovery_state_reconciliation_failed",
                report,
            )
        changed_ids: list[str] = []
        audit_rows = 0
        now = datetime.now(timezone.utc).isoformat()
        for paper_id, paper in report["per_paper"].items():
            paper_changed: list[str] = []
            for item in paper["items"]:
                if item["status"] != "reconcile":
                    continue
                candidate = self.session.get(
                    ExternalAnalysisCandidate,
                    UUID(item["candidate_id"]),
                )
                recovery = self.session.get(
                    ExternalAnalysisCandidateRecovery,
                    UUID(item["candidate_id"]),
                )
                candidate.status = item["desired_state"]["status"]
                candidate.materialized_target_type = item["desired_state"][
                    "materialized_target_type"
                ]
                candidate.materialized_target_id = item["desired_state"][
                    "materialized_target_id"
                ]
                previous_manifest = dict(recovery.match_manifest or {})
                reconciliations = list(previous_manifest.get("state_reconciliations") or [])
                reconciliations.append(
                    {
                        "executed_at": now,
                        "actor": actor,
                        "reason": reason,
                        "before": item["current_state"],
                        "after": item["desired_state"],
                    }
                )
                recovery.match_manifest = {
                    **previous_manifest,
                    **item["match_manifest"],
                    "state_reconciliations": reconciliations,
                }
                recovery.restored_state = item["restored_state"]
                recovery.payload_sha256 = item["payload_sha256"]
                changed_ids.append(item["candidate_id"])
                paper_changed.append(item["candidate_id"])
            if paper_changed:
                self.session.add(
                    AuditLog(
                        paper_id=UUID(paper_id),
                        action="reconcile_external_analysis_candidate_recovery_state",
                        source=actor,
                        target_type="paper",
                        target_id=paper_id,
                        payload={
                            "recovery_version": RECOVERY_VERSION,
                            "reason": reason,
                            "candidate_ids": paper_changed,
                            "candidate_count": len(paper_changed),
                        },
                    )
                )
                audit_rows += 1
        self.session.flush()
        report["database_writes"] = len(changed_ids)
        report["summary_audit_log_rows"] = audit_rows
        report["changed_candidate_ids"] = changed_ids
        report["status"] = "applied" if changed_ids else "noop"
        return report
