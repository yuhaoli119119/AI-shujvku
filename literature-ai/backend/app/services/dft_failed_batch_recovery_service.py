from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import (
    ActiveSiteMetal,
    AuditLog,
    CatalystSample,
    ContentEvidenceItem,
    DFTAuditIssue,
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
)
from app.services.dft_identity_service import resolve_atom_pair_identity


RECOVERY_PAPER_CODE = "B0102"
RECOVERY_ACTION = "revert_failed_b0102_dft_batch"
MANIFEST_SCHEMA_VERSION = "b0102_failed_batch_recovery_v1"
FAILED_CANDIDATE_STATUS = "ai_repair_failed_not_imported"
REPAIR_PROTOCOL = "dft_audit_issue_primary_repair_v1"
EXPECTED_BACKUP_COUNTS = {
    "dft_total": 375,
    "dft_ai_ready": 373,
    "dft_rejected": 2,
    "open_missing_issues": 368,
    "closed_missing_issues": 0,
    "new_candidates": 370,
    "distinct_bound_targets": 368,
}
EXPECTED_LIVE_COUNTS = {
    "dft_total": 400,
    "dft_ai_ready": 373,
    "dft_rejected": 2,
    "open_missing_issues": 343,
    "closed_missing_issues": 25,
    "new_candidates": 370,
    "distinct_bound_targets": 368,
}


class RecoveryRefused(RuntimeError):
    """Raised when any recovery safety invariant is not satisfied."""


@dataclass(frozen=True)
class DatabaseSnapshot:
    paper: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]

    @property
    def fingerprint(self) -> str:
        return canonical_sha256({"paper": self.paper, "tables": self.tables})


SNAPSHOT_MODELS = (
    DFTResult,
    DFTAuditIssue,
    ExtractionFieldReview,
    EvidenceSpan,
    EvidenceLocator,
    EvidenceClaim,
    AuditLog,
    CatalystSample,
    ActiveSiteMetal,
    MechanismClaim,
    ElectrochemicalPerformance,
    ExternalAnalysisCandidate,
    ContentEvidenceItem,
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def manifest_sha256(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return canonical_sha256(unsigned)


def verify_manifest_integrity(manifest: dict[str, Any]) -> None:
    expected = str(manifest.get("manifest_sha256") or "")
    actual = manifest_sha256(manifest)
    if not expected or expected != actual:
        raise RecoveryRefused("manifest_missing_or_tampered")


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RecoveryRefused("manifest_file_missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryRefused("manifest_unreadable") from exc
    if not isinstance(manifest, dict):
        raise RecoveryRefused("manifest_not_an_object")
    verify_manifest_integrity(manifest)
    return manifest


class DFTFailedBatchRecoveryService:
    """Build and apply the narrow B0102 failed-batch compensation plan.

    Dry-run uses repeatable-read, read-only transactions for both databases.
    Apply uses one serializable transaction and never deletes audit history.
    """

    def __init__(
        self,
        *,
        backup_engine: Engine,
        live_engine: Engine,
        backup_sha256: str,
        paper_code: str = RECOVERY_PAPER_CODE,
    ) -> None:
        if paper_code != RECOVERY_PAPER_CODE:
            raise RecoveryRefused("paper_code_not_allowed")
        self.backup_engine = backup_engine
        self.live_engine = live_engine
        self.backup_sha256 = backup_sha256.upper()
        self.paper_code = paper_code

    def build_dry_run_manifest(self, *, output_path: Path | None = None) -> dict[str, Any]:
        with _transactional_session(self.backup_engine, read_only=True) as backup_session:
            backup = _snapshot(backup_session, self.paper_code)
        with _transactional_session(self.live_engine, read_only=True) as live_session:
            live = _snapshot(live_session, self.paper_code)

        manifest = self._compare(backup, live)
        manifest["manifest_sha256"] = manifest_sha256(manifest)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return manifest

    def apply_manifest(
        self,
        manifest: dict[str, Any],
        *,
        confirm_paper_code: str,
        expected_backup_sha256: str,
        expected_live_fingerprint: str,
    ) -> dict[str, Any]:
        verify_manifest_integrity(manifest)
        if confirm_paper_code != RECOVERY_PAPER_CODE or manifest.get("paper", {}).get("paper_code") != RECOVERY_PAPER_CODE:
            raise RecoveryRefused("paper_confirmation_mismatch")
        if expected_backup_sha256.upper() != self.backup_sha256:
            raise RecoveryRefused("backup_sha256_mismatch")
        if str(manifest.get("backup", {}).get("dump_sha256", "")).upper() != self.backup_sha256:
            raise RecoveryRefused("manifest_backup_sha256_mismatch")
        if expected_live_fingerprint != manifest.get("live", {}).get("fingerprint"):
            raise RecoveryRefused("expected_live_fingerprint_mismatch")

        issue_ids = [str(value) for value in manifest.get("issue_ids", [])]
        result_ids = [str(value) for value in manifest.get("result_ids", [])]
        if len(issue_ids) != len(result_ids):
            raise RecoveryRefused("manifest_item_count_mismatch")
        if not issue_ids and manifest.get("status") == "already_recovered":
            return {"status": "already_recovered", "deleted": {}}
        if len(issue_ids) != 25 or len(set(issue_ids)) != 25 or len(set(result_ids)) != 25:
            raise RecoveryRefused("manifest_must_contain_exactly_25_items")
        if manifest.get("eligibility", {}).get("eligible_count") != 25:
            raise RecoveryRefused("manifest_not_fully_eligible")

        connection = self.live_engine.connect().execution_options(isolation_level="SERIALIZABLE")
        transaction = connection.begin()
        session = Session(bind=connection, autoflush=False, expire_on_commit=False)
        try:
            paper = session.scalar(sa.select(Paper).where(Paper.paper_code == RECOVERY_PAPER_CODE).with_for_update())
            if paper is None:
                raise RecoveryRefused("paper_not_found")
            locked_issues = session.scalars(
                sa.select(DFTAuditIssue)
                .where(DFTAuditIssue.id.in_([UUID(value) for value in issue_ids]))
                .order_by(DFTAuditIssue.id)
                .with_for_update()
            ).all()
            locked_results = session.scalars(
                sa.select(DFTResult)
                .where(DFTResult.id.in_([UUID(value) for value in result_ids]))
                .order_by(DFTResult.id)
                .with_for_update()
            ).all()
            if len(locked_issues) != 25 or len(locked_results) != 25:
                raise RecoveryRefused("locked_row_count_mismatch")

            current = _snapshot(session, RECOVERY_PAPER_CODE)
            if current.fingerprint != expected_live_fingerprint:
                raise RecoveryRefused("live_fingerprint_drift")

            items_by_issue = {str(item["issue_id"]): item for item in manifest["items"]}
            result_id_set = set(result_ids)
            for issue in locked_issues:
                item = items_by_issue.get(str(issue.id))
                if item is None:
                    raise RecoveryRefused("manifest_issue_item_missing")
                _restore_model_from_row(issue, item["backup_issue"], preserve=("id",))

            review_ids = _dependency_ids(manifest, "extraction_field_reviews")
            span_ids = _dependency_ids(manifest, "evidence_spans")
            locator_ids = _dependency_ids(manifest, "evidence_locators")
            _delete_locked_ids(session, ExtractionFieldReview, review_ids)
            _delete_locked_ids(session, EvidenceSpan, span_ids)
            _delete_locked_ids(session, EvidenceLocator, locator_ids)

            samples_to_delete = [
                UUID(row["sample_id"])
                for row in manifest.get("candidate_sample_analysis", [])
                if row.get("delete_allowed")
            ]
            for result in locked_results:
                session.delete(result)
            session.flush()

            for sample_id in samples_to_delete:
                sample = session.scalar(
                    sa.select(CatalystSample).where(CatalystSample.id == sample_id).with_for_update()
                )
                if sample is None:
                    continue
                _assert_sample_unreferenced(session, sample_id, result_id_set)
                session.delete(sample)

            session.add(
                AuditLog(
                    paper_id=paper.id,
                    action=RECOVERY_ACTION,
                    source="maintenance_cli",
                    target_type="dft_failed_batch",
                    target_id=manifest["manifest_sha256"],
                    payload={
                        "paper_code": RECOVERY_PAPER_CODE,
                        "manifest_sha256": manifest["manifest_sha256"],
                        "backup_sha256": self.backup_sha256,
                        "reverted_issue_ids": issue_ids,
                        "deleted_result_ids": result_ids,
                        "preserved_audit_log_ids": manifest.get("audit_logs", {}).get("preserve_ids", []),
                    },
                )
            )
            session.flush()

            after_counts = _counts(session, paper.id)
            if after_counts != EXPECTED_BACKUP_COUNTS:
                raise RecoveryRefused(f"post_apply_count_assertion_failed:{after_counts}")
            transaction.commit()
            return {
                "status": "recovered",
                "deleted": {
                    "dft_results": len(result_ids),
                    "extraction_field_reviews": len(review_ids),
                    "evidence_spans": len(span_ids),
                    "evidence_locators": len(locator_ids),
                    "catalyst_samples": len(samples_to_delete),
                },
                "restored_issues": len(issue_ids),
                "after_counts": after_counts,
            }
        except Exception:
            transaction.rollback()
            raise
        finally:
            session.close()
            connection.close()

    def _compare(self, backup: DatabaseSnapshot, live: DatabaseSnapshot) -> dict[str, Any]:
        if backup.paper["id"] != live.paper["id"]:
            raise RecoveryRefused("paper_identity_mismatch")
        paper_id = backup.paper["id"]
        backup_counts = _counts_from_snapshot(backup)
        live_counts = _counts_from_snapshot(live)
        if backup_counts != EXPECTED_BACKUP_COUNTS:
            raise RecoveryRefused(f"unexpected_backup_counts:{backup_counts}")

        backup_results = _by_id(backup, "dft_results")
        live_results = _by_id(live, "dft_results")
        backup_issues = _by_id(backup, "dft_audit_issues")
        live_issues = _by_id(live, "dft_audit_issues")
        live_bad_by_issue: dict[str, list[dict[str, Any]]] = {}
        for result in live.tables["dft_results"]:
            issue_id = (result.get("evidence_payload") or {}).get("issue_id")
            if issue_id:
                live_bad_by_issue.setdefault(str(issue_id), []).append(result)

        comparisons: list[dict[str, Any]] = []
        eligible_items: list[dict[str, Any]] = []
        for issue_id in sorted(backup_issues):
            backup_issue = backup_issues[issue_id]
            live_issue = live_issues.get(issue_id)
            linked = live_bad_by_issue.get(issue_id, [])
            reasons: list[str] = []
            result = linked[0] if len(linked) == 1 else None
            if live_issue is None:
                reasons.append("live_issue_missing")
            if len(linked) != 1:
                reasons.append(f"linked_result_count:{len(linked)}")
            if backup_issue.get("issue_type") != "missing_dft_result":
                reasons.append("backup_issue_type_not_missing_dft_result")
            if backup_issue.get("status") == "closed":
                reasons.append("backup_issue_not_open")
            if live_issue and live_issue.get("status") != "closed":
                reasons.append("live_issue_not_closed")
            if result is not None:
                result_id = str(result["id"])
                checks = {
                    "paper_matches": result.get("paper_id") == paper_id,
                    "backup_result_absent": result_id not in backup_results,
                    "issue_bound_to_result": bool(live_issue and str(live_issue.get("target_id")) == result_id),
                    "repair_protocol_matches": result.get("extraction_protocol_version") == REPAIR_PROTOCOL,
                    "failed_status_matches": result.get("candidate_status") == FAILED_CANDIDATE_STATUS,
                    "property_type_matches": str(result.get("property_type") or "").casefold() == "bond_length",
                    "canonical_atom_pair_missing": _result_missing_atom_pair(result),
                }
                reasons.extend(key for key, passed in checks.items() if not passed)
                safety = _item_safety(backup, live, backup_issue, live_issue, result)
                reasons.extend(safety["blocking_reasons"])
            else:
                checks = {}
                safety = {"blocking_reasons": ["no_single_result"], "references": {}, "dependencies": {}}

            comparison = {
                "issue_id": issue_id,
                "result_id": str(result["id"]) if result else None,
                "eligible": not reasons,
                "reasons": sorted(set(reasons)),
            }
            comparisons.append(comparison)
            if not reasons and result is not None and live_issue is not None:
                eligible_items.append(
                    {
                        "issue_id": issue_id,
                        "result_id": str(result["id"]),
                        "backup_issue": backup_issue,
                        "live_issue": live_issue,
                        "live_result": result,
                        "dependencies": safety["dependencies"],
                        "reference_check": safety["references"],
                        "new_audit_logs": safety["new_audit_logs"],
                        "eligibility_checks": checks,
                    }
                )

        eligible_items.sort(key=lambda row: row["issue_id"])
        issue_ids = [row["issue_id"] for row in eligible_items]
        result_ids = [row["result_id"] for row in eligible_items]
        already_recovered = live_counts == EXPECTED_BACKUP_COUNTS and not any(
            row.get("candidate_status") == FAILED_CANDIDATE_STATUS for row in live.tables["dft_results"]
        )
        if not already_recovered and live_counts != EXPECTED_LIVE_COUNTS:
            raise RecoveryRefused(f"unexpected_live_counts:{live_counts}")
        if not already_recovered and len(eligible_items) != 25:
            blocked = [row for row in comparisons if row["result_id"] and not row["eligible"]]
            raise RecoveryRefused(f"eligible_item_count:{len(eligible_items)} blocked:{blocked}")

        selected_sample_ids = sorted(
            {str(row["live_result"]["catalyst_sample_id"]) for row in eligible_items if row["live_result"].get("catalyst_sample_id")}
        )
        sample_analysis = [
            _sample_analysis(backup, live, sample_id, set(result_ids)) for sample_id in selected_sample_ids
        ]
        audit_rows = _audit_rows_for_ids(live, set(issue_ids) | set(result_ids))
        dependency_counts = {
            name: sum(len(item["dependencies"][name]["delete_rows"]) for item in eligible_items)
            for name in ("extraction_field_reviews", "evidence_spans", "evidence_locators")
        }
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "already_recovered" if already_recovered else "dry_run_ready",
            "mode": "dry_run",
            "action": RECOVERY_ACTION,
            "generated_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "paper": {"paper_code": self.paper_code, "paper_id": paper_id},
            "backup": {"dump_sha256": self.backup_sha256, "fingerprint": backup.fingerprint},
            "live": {"fingerprint": live.fingerprint, "transaction_mode": "REPEATABLE READ READ ONLY"},
            "issue_ids": issue_ids,
            "result_ids": result_ids,
            "identified_issue_count": len(issue_ids),
            "identified_result_count": len(result_ids),
            "items": eligible_items,
            "eligibility": {
                "compared_issue_count": len(comparisons),
                "eligible_count": len(eligible_items),
                "row_comparisons": comparisons,
            },
            "dependent_record_counts": dependency_counts,
            "audit_logs": {
                "policy": "preserve_all_history_and_append_compensation_audit_on_apply",
                "preserve_ids": [str(row["id"]) for row in audit_rows],
                "preserve_rows": audit_rows,
                "preserved_count": len(audit_rows),
            },
            "candidate_sample_analysis": sample_analysis,
            "before_counts_from_backup": backup_counts,
            "current_live_counts": live_counts,
            "expected_after_counts": EXPECTED_BACKUP_COUNTS,
            "apply_contract": {
                "single_transaction": True,
                "isolation_level": "SERIALIZABLE",
                "locks": ["dft_audit_issues", "dft_results", "selected dependencies", "deletable catalyst_samples"],
                "recalculate_live_fingerprint": True,
                "rollback_on_any_error": True,
                "required_cli_flags": [
                    "--apply",
                    "--confirm-paper-code B0102",
                    "--expected-backup-sha256",
                    "--expected-live-fingerprint",
                    "--manifest-path",
                ],
            },
        }
        return manifest


@contextmanager
def _transactional_session(engine: Engine, *, read_only: bool) -> Iterator[Session]:
    connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        if read_only:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _snapshot(session: Session, paper_code: str) -> DatabaseSnapshot:
    paper = session.scalar(sa.select(Paper).where(Paper.paper_code == paper_code))
    if paper is None:
        raise RecoveryRefused(f"paper_not_found:{paper_code}")
    tables: dict[str, list[dict[str, Any]]] = {}
    for model in SNAPSHOT_MODELS:
        rows = session.scalars(
            sa.select(model).where(model.paper_id == paper.id).order_by(model.id)
        ).all()
        tables[model.__tablename__] = [_model_row(row) for row in rows]
    return DatabaseSnapshot(paper=_model_row(paper), tables=tables)


def _model_row(row: Any) -> dict[str, Any]:
    return {
        column.key: _jsonable(getattr(row, column.key))
        for column in sa.inspect(type(row)).columns
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return value


def _by_id(snapshot: DatabaseSnapshot, table: str) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in snapshot.tables[table]}


def _counts_from_snapshot(snapshot: DatabaseSnapshot) -> dict[str, int]:
    results = snapshot.tables["dft_results"]
    issues = [row for row in snapshot.tables["dft_audit_issues"] if row["issue_type"] == "missing_dft_result"]
    candidates = [
        row
        for row in snapshot.tables["external_analysis_candidates"]
        if isinstance(row.get("normalized_payload"), dict)
        and row["normalized_payload"].get("decision") == "new_candidate"
    ]
    return {
        "dft_total": len(results),
        "dft_ai_ready": sum(str(row.get("candidate_status") or "").casefold() == "ai_verified_ml_ready" for row in results),
        "dft_rejected": sum(str(row.get("candidate_status") or "").casefold() == "rejected" for row in results),
        "open_missing_issues": sum(row.get("status") != "closed" for row in issues),
        "closed_missing_issues": sum(row.get("status") == "closed" for row in issues),
        "new_candidates": len(candidates),
        "distinct_bound_targets": len({row.get("materialized_target_id") for row in candidates if row.get("materialized_target_id")}),
    }


def _counts(session: Session, paper_id: UUID) -> dict[str, int]:
    return _counts_from_snapshot(_snapshot(session, RECOVERY_PAPER_CODE))


def _result_missing_atom_pair(result: dict[str, Any]) -> bool:
    identity = resolve_atom_pair_identity(
        {
            "property_type": result.get("property_type"),
            "evidence_payload": result.get("evidence_payload") or {},
        }
    )
    return identity.canonical is None and identity.error_code == "missing_atom_pair_identity"


def _item_safety(
    backup: DatabaseSnapshot,
    live: DatabaseSnapshot,
    backup_issue: dict[str, Any],
    live_issue: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    result_id = str(result["id"])
    issue_id = str(backup_issue["id"])
    blocking: list[str] = []
    dependencies: dict[str, Any] = {}
    selectors = {
        "extraction_field_reviews": lambda row: row.get("target_id") == result_id,
        "evidence_spans": lambda row: row.get("object_id") == result_id,
        "evidence_locators": lambda row: row.get("target_id") == result_id,
    }
    for table, predicate in selectors.items():
        backup_rows = _by_id(backup, table)
        live_rows = [row for row in live.tables[table] if predicate(row)]
        delete_rows = [row for row in live_rows if str(row["id"]) not in backup_rows]
        historical_rows = [row for row in live_rows if str(row["id"]) in backup_rows]
        if historical_rows:
            blocking.append(f"historical_dependency_present:{table}")
        dependencies[table] = {"delete_rows": delete_rows, "preserve_rows": historical_rows}

    reviews = dependencies["extraction_field_reviews"]["delete_rows"]
    if any(
        row.get("reviewer") != "local_ide"
        or ((row.get("review_payload") or {}).get("ai_verification") or {}).get("source_label") != "mcp_fast_batch"
        for row in reviews
    ):
        blocking.append("review_has_non_batch_or_human_edit")

    relevant_audits = _audit_rows_for_ids(live, {issue_id, result_id})
    backup_audit_ids = set(_by_id(backup, "audit_logs"))
    new_audits = [row for row in relevant_audits if str(row["id"]) not in backup_audit_ids]
    allowed_audits = {
        ("repair_dft_audit_issue", "local_ide"),
        ("verify_dft_result", "local_ide"),
    }
    if any((row.get("action"), row.get("source")) not in allowed_audits for row in new_audits):
        blocking.append("post_batch_audit_indicates_manual_or_unknown_edit")
    if {row.get("action") for row in new_audits} != {"repair_dft_audit_issue", "verify_dft_result"}:
        blocking.append("expected_batch_audit_pair_missing")

    reference_tables = {
        "dft_results": lambda row: str(row.get("support_writeback_dft_result_id") or "") == result_id,
        "dft_audit_issues": lambda row: str(row.get("target_id") or "") == result_id and str(row.get("id")) != issue_id,
        "evidence_claims": lambda row: str(row.get("target_id") or "") == result_id,
        "content_evidence_items": lambda row: str(row.get("source_id") or "") == result_id,
        "external_analysis_candidates": lambda row: str(row.get("materialized_target_id") or "") == result_id,
    }
    references: dict[str, Any] = {}
    for table, predicate in reference_tables.items():
        backup_rows = _by_id(backup, table)
        live_rows = [row for row in live.tables[table] if predicate(row)]
        new_rows = [row for row in live_rows if backup_rows.get(str(row["id"])) != row]
        references[table] = {"rows": live_rows, "new_or_changed_rows": new_rows}
        if new_rows:
            blocking.append(f"new_business_reference:{table}")

    if live_issue is None or live_issue.get("resolved_by") != "local_ide" or live_issue.get("resolution_note") != "ai_verified":
        blocking.append("issue_resolution_not_expected_batch_shape")
    verification = result.get("local_ai_verification_payload") or {}
    if verification.get("source_label") != "mcp_fast_batch" or verification.get("final_decision") != "repair_failed_not_exportable":
        blocking.append("result_verification_not_expected_batch_shape")
    return {
        "blocking_reasons": sorted(set(blocking)),
        "dependencies": dependencies,
        "references": references,
        "new_audit_logs": new_audits,
    }


def _audit_rows_for_ids(snapshot: DatabaseSnapshot, identifiers: set[str]) -> list[dict[str, Any]]:
    return [
        row
        for row in snapshot.tables["audit_logs"]
        if str(row.get("target_id") or "") in identifiers or _contains_identifier(row.get("payload"), identifiers)
    ]


def _contains_identifier(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_identifier(item, identifiers) for item in value.values())
    if isinstance(value, list):
        return any(_contains_identifier(item, identifiers) for item in value)
    return str(value) in identifiers


def _sample_analysis(
    backup: DatabaseSnapshot,
    live: DatabaseSnapshot,
    sample_id: str,
    result_ids: set[str],
) -> dict[str, Any]:
    backup_sample = _by_id(backup, "catalyst_samples").get(sample_id)
    live_sample = _by_id(live, "catalyst_samples").get(sample_id)
    references = {
        "dft_results": [row for row in live.tables["dft_results"] if str(row.get("catalyst_sample_id") or "") == sample_id],
        "active_site_metals": [row for row in live.tables["active_site_metals"] if str(row.get("catalyst_sample_id") or "") == sample_id],
        "mechanism_claims": [row for row in live.tables["mechanism_claims"] if str(row.get("catalyst_sample_id") or "") == sample_id],
        "electrochemical_performance": [row for row in live.tables["electrochemical_performance"] if str(row.get("catalyst_sample_id") or "") == sample_id],
    }
    non_result_refs = sum(len(rows) for table, rows in references.items() if table != "dft_results")
    other_result_refs = [row for row in references["dft_results"] if str(row["id"]) not in result_ids]
    delete_allowed = backup_sample is None and not other_result_refs and non_result_refs == 0
    return {
        "sample_id": sample_id,
        "backup_exists": backup_sample is not None,
        "live_exists": live_sample is not None,
        "delete_allowed": delete_allowed,
        "decision": "delete" if delete_allowed else "preserve",
        "reference_counts": {table: len(rows) for table, rows in references.items()},
        "other_result_reference_ids": [str(row["id"]) for row in other_result_refs],
        "non_result_reference_count": non_result_refs,
    }


def _restore_model_from_row(model: Any, row: dict[str, Any], *, preserve: tuple[str, ...]) -> None:
    for column in sa.inspect(type(model)).columns:
        if column.key in preserve:
            continue
        value = row.get(column.key)
        if isinstance(column.type, sa.Uuid) and value is not None:
            value = UUID(str(value))
        elif isinstance(column.type, sa.DateTime) and value is not None:
            value = datetime.fromisoformat(str(value))
        setattr(model, column.key, value)


def _dependency_ids(manifest: dict[str, Any], table: str) -> list[UUID]:
    values: list[UUID] = []
    for item in manifest.get("items", []):
        for row in item.get("dependencies", {}).get(table, {}).get("delete_rows", []):
            values.append(UUID(str(row["id"])))
    return values


def _delete_locked_ids(session: Session, model: Any, ids: list[UUID]) -> None:
    if not ids:
        return
    rows = session.scalars(sa.select(model).where(model.id.in_(ids)).with_for_update()).all()
    if len(rows) != len(ids):
        raise RecoveryRefused(f"dependency_row_count_drift:{model.__tablename__}")
    for row in rows:
        session.delete(row)


def _assert_sample_unreferenced(session: Session, sample_id: UUID, deleting_result_ids: set[str]) -> None:
    other_results = session.scalar(
        sa.select(sa.func.count()).select_from(DFTResult).where(
            DFTResult.catalyst_sample_id == sample_id,
            sa.cast(DFTResult.id, sa.String).not_in(deleting_result_ids),
        )
    )
    other_relations = sum(
        session.scalar(sa.select(sa.func.count()).select_from(model).where(model.catalyst_sample_id == sample_id)) or 0
        for model in (ActiveSiteMetal, MechanismClaim, ElectrochemicalPerformance)
    )
    if other_results or other_relations:
        raise RecoveryRefused("catalyst_sample_reference_drift")
