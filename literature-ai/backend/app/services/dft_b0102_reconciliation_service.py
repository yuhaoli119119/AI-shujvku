from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    Paper,
)
from app.services.dft_audit_issue_lifecycle_service import (
    DFT_AUDIT_ISSUE_PENDING_STATUSES,
    DFTAuditIssueLifecycleService,
)
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_identity_dry_run_service import (
    B0102_EXPECTED,
    B0102_SPLIT_EXPECTATIONS,
    DFTIdentityDryRunService,
    canonical_json,
    canonical_sha256,
    file_sha256,
)
from app.services.verification_session_service import VerificationSessionService
from app.utils.evidence_anchors import has_evidence_anchor
from app.utils.review_safety import bulk_export_gate_results


B0102_RECONCILIATION_VERSION = "b0102_dft_reconciliation_v1"
B0102_MANIFEST_CANONICAL_SHA256 = "715e38595add57f6664233d8a77d90a08ef4632cee11470ea2fd954409b843fa"
B0102_INPUT_BACKUP_SHA256 = "4F41DBDCC6ABE78771075CA6A7A733DC224541776AE3420EAD76CC8ECEBFA2CF"
B0102_PRE_APPLY_DATABASE_FINGERPRINT = "d484c4614e7eb066cc9510c12d7b274b317d98a88420344062a72e3281fa2edc"
B0102_PDF_SNAPSHOT_FINGERPRINT = "d239d0316a3b57ba7022cdee1dd95656774536bee3719e7d002ca131b6abe166"
B0102_ACTOR = "b0102_reconciliation_ai"
B0102_AUDIT_ACTION = "reconciled_materialized_verified_result_v2"
B0102_SPLIT_AUDIT_ACTION = "reconciled_identity_split_v2"
B0102_CHILD_NAMESPACE = UUID("0d8b8988-42cc-4b2a-950d-701b20435c5f")

B0102_SPLIT_FIXED = (
    {
        **B0102_SPLIT_EXPECTATIONS[0],
        "li1_observation_key": "dft-observation-v2:8e8d680f7c35827a05499fae12d9b7711e228803d246b9da8cb4d2315ae28863",
        "li2_observation_key": "dft-observation-v2:bbb91e02208564d2b81d4c774f28be1810c2ab53e8523fcb8e398a3529ac751b",
    },
    {
        **B0102_SPLIT_EXPECTATIONS[1],
        "li1_observation_key": "dft-observation-v2:1d1d10f921f370f43a415f61f9611f24add7011a12be089e7e816c2608b41978",
        "li2_observation_key": "dft-observation-v2:75bcbbb18e99b2e5ff686803e33cf6778df8b1198dc1ef0a54adaa0d437bf48c",
    },
)


class B0102ReconciliationError(RuntimeError):
    pass


class DFTB0102ReconciliationService:
    """Strict, one-transaction reconciliation of the accepted B0102 manifest."""

    def __init__(self, session: Session):
        self.session = session
        self.lifecycle = DFTAuditIssueLifecycleService(session)
        self.dry_run = DFTIdentityDryRunService(session)
        self._write_events: list[dict[str, Any]] = []

    @staticmethod
    def load_and_validate_manifest(
        manifest_path: Path,
        *,
        expected_manifest_sha256: str,
        backup_path: Path,
        expected_backup_sha256: str,
        expected_database_fingerprint: str,
        expected_pdf_fingerprint: str,
        paper_code: str,
    ) -> dict[str, Any]:
        if paper_code != "B0102":
            raise B0102ReconciliationError("paper_code_must_be_B0102")
        expected_manifest_sha256 = DFTB0102ReconciliationService._confirmed_sha256(
            expected_manifest_sha256,
            "manifest",
        )
        expected_backup_sha256 = DFTB0102ReconciliationService._confirmed_sha256(
            expected_backup_sha256,
            "backup",
        )
        expected_database_fingerprint = DFTB0102ReconciliationService._confirmed_sha256(
            expected_database_fingerprint,
            "database_fingerprint",
        )
        expected_pdf_fingerprint = DFTB0102ReconciliationService._confirmed_sha256(
            expected_pdf_fingerprint,
            "pdf_fingerprint",
        )
        if expected_pdf_fingerprint.casefold() != B0102_PDF_SNAPSHOT_FINGERPRINT:
            raise B0102ReconciliationError("unexpected_pdf_fingerprint_confirmation")

        path = manifest_path.resolve(strict=True)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise B0102ReconciliationError("invalid_manifest_json") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("canonical_payload"), dict):
            raise B0102ReconciliationError("invalid_manifest_shape")
        calculated = canonical_sha256(manifest["canonical_payload"])
        stored = str(manifest.get("canonical_sha256") or "")
        if calculated != stored or stored.casefold() != expected_manifest_sha256:
            raise B0102ReconciliationError(f"manifest_canonical_sha256_mismatch:{calculated}")

        backup = backup_path.resolve(strict=True)
        actual_backup_sha = file_sha256(backup).upper()
        if actual_backup_sha.casefold() != expected_backup_sha256:
            raise B0102ReconciliationError(f"backup_sha256_mismatch:{actual_backup_sha}")
        payload = manifest["canonical_payload"]
        manifest_backup = payload.get("backup") or {}
        preconditions = payload.get("preconditions_for_apply") or {}
        paper = payload.get("paper_reconciliation") or {}
        checks = {
            "manifest_backup_sha": str(manifest_backup.get("sha256") or "").casefold()
            == actual_backup_sha.casefold()
            == expected_backup_sha256,
            "paper_code": preconditions.get("paper_code") == "B0102" == paper.get("paper_code"),
            "database_fingerprint": str(preconditions.get("database_data_fingerprint") or "").casefold()
            == expected_database_fingerprint,
            "pdf_fingerprint": str(preconditions.get("pdf_snapshot_fingerprint") or "").casefold()
            == expected_pdf_fingerprint,
            "expected_counts": paper.get("actual") == B0102_EXPECTED,
            "safe_mapping_count": len(paper.get("safe_single_targets") or []) == 366,
            "split_mapping_count": len(paper.get("identity_split_parent_issues") or []) == 2,
            "no_unmapped": not paper.get("unmapped"),
            "no_unknown_multi": not paper.get("unknown_multi_target"),
            "no_unsafe": not paper.get("unsafe_single_targets"),
        }
        if not all(checks.values()):
            raise B0102ReconciliationError(
                f"manifest_precondition_mismatch:{canonical_json({key: value for key, value in checks.items() if not value})}"
            )
        return manifest

    @staticmethod
    def _confirmed_sha256(value: str, label: str) -> str:
        normalized = str(value or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise B0102ReconciliationError(f"invalid_{label}_sha256_confirmation")
        return normalized

    def assert_pdf_preflight(self, *, data_root: Path, expected_pdf_fingerprint: str) -> dict[str, Any]:
        snapshot = self.dry_run.pdf_snapshot(paper_code="B0102", data_root=data_root)
        if snapshot["sha256"] != expected_pdf_fingerprint:
            raise B0102ReconciliationError(
                f"pdf_snapshot_fingerprint_mismatch:{snapshot['sha256']}"
            )
        return snapshot

    def reconcile(
        self,
        *,
        manifest: dict[str, Any],
        expected_manifest_sha256: str,
        expected_database_fingerprint: str,
        expected_pdf_fingerprint: str,
        pdf_preflight_fingerprint: str,
        fault_after: str | None = None,
    ) -> dict[str, Any]:
        expected_manifest_sha256 = self._confirmed_sha256(
            expected_manifest_sha256,
            "manifest",
        )
        expected_database_fingerprint = self._confirmed_sha256(
            expected_database_fingerprint,
            "database_fingerprint",
        )
        expected_pdf_fingerprint = self._confirmed_sha256(
            expected_pdf_fingerprint,
            "pdf_fingerprint",
        )
        if expected_pdf_fingerprint != B0102_PDF_SNAPSHOT_FINGERPRINT:
            raise B0102ReconciliationError("unexpected_pdf_fingerprint_confirmation")
        if pdf_preflight_fingerprint != expected_pdf_fingerprint:
            raise B0102ReconciliationError("pdf_preflight_fingerprint_mismatch")
        calculated_manifest_sha = canonical_sha256(manifest["canonical_payload"])
        stored_manifest_sha = str(manifest.get("canonical_sha256") or "").casefold()
        if (
            calculated_manifest_sha != stored_manifest_sha
            or stored_manifest_sha != expected_manifest_sha256
        ):
            raise B0102ReconciliationError("manifest_changed_after_preflight")

        self._assert_transaction_active()
        self.session.flush()
        before_fingerprint = self.dry_run.database_data_fingerprint()
        already = self.readback(require_final=False)
        if already["is_exact_final_state"]:
            return {
                "status": "legacy_final_state_detected",
                "database_writes": 0,
                "writes_final_truth": False,
                "needs_ai_reverification": True,
                "blocked_reason": "legacy_ai_verified_state_requires_single_ai_reverification",
                "database_fingerprint_before": before_fingerprint,
                "database_fingerprint_after": before_fingerprint,
                "readback": already,
                "write_events": [],
            }
        if before_fingerprint["sha256"] != expected_database_fingerprint:
            raise B0102ReconciliationError(
                f"pre_apply_database_fingerprint_mismatch:{before_fingerprint['sha256']}"
            )

        # The historical reconciliation applied AI opinions as final verified
        # truth. Keep its strict preflight/readback capability, but never enter
        # the mutation pipeline. Candidates must pass the dedicated single-AI
        # verification capability and the current deterministic evidence gates.
        return {
            "status": "pending_ai_verification",
            "database_writes": 0,
            "writes_final_truth": False,
            "needs_ai_reverification": True,
            "blocked_reason": "requires_ai_verify_content",
            "database_fingerprint_before": before_fingerprint,
            "database_fingerprint_after": before_fingerprint,
            "readback": already,
            "write_events": [],
        }

        expected_reconciliation = manifest["canonical_payload"]["paper_reconciliation"]
        live_reconciliation = self._live_authoritative_reconciliation()
        if canonical_json(live_reconciliation) != canonical_json(expected_reconciliation):
            raise B0102ReconciliationError("authoritative_manifest_mapping_drift")
        other_before = self._other_papers_fingerprint()
        old_science = self._fixed_old_result_science()
        audit_before = self._audit_action_counts()

        for entry in expected_reconciliation["safe_single_targets"]:
            self._reconcile_safe_entry(entry)
        self._fault(fault_after, "after_safe_366")

        split_reports = [
            self._reconcile_split(entry, fixed)
            for fixed in B0102_SPLIT_FIXED
            for entry in expected_reconciliation["identity_split_parent_issues"]
            if entry["issue_id"] == fixed["issue_id"]
        ]
        if len(split_reports) != 2:
            raise B0102ReconciliationError("split_manifest_mapping_incomplete")
        self._fault(fault_after, "after_splits")

        self.session.flush()
        final = self.readback(require_final=True)
        if self._fixed_old_result_science() != old_science:
            raise B0102ReconciliationError("old_li1_scientific_data_changed")
        other_after = self._other_papers_fingerprint()
        if other_after != other_before:
            raise B0102ReconciliationError("other_papers_changed")
        audit_after = self._audit_action_counts()
        audit_delta = {
            key: audit_after.get(key, 0) - audit_before.get(key, 0)
            for key in sorted(set(audit_before) | set(audit_after))
        }
        expected_audit_delta = {
            B0102_AUDIT_ACTION: 368,
            B0102_SPLIT_AUDIT_ACTION: 2,
        }
        if audit_delta != expected_audit_delta:
            raise B0102ReconciliationError(
                f"reconciliation_audit_delta_mismatch:{canonical_json(audit_delta)}"
            )
        after_fingerprint = self.dry_run.database_data_fingerprint()
        return {
            "status": "reconciled",
            "database_writes": len(self._write_events),
            "database_fingerprint_before": before_fingerprint,
            "database_fingerprint_after": after_fingerprint,
            "safe_366": {
                "processed": 366,
                "resolution_code": "exact_duplicate",
                "audit_action": B0102_AUDIT_ACTION,
            },
            "splits": split_reports,
            "audit_counts_before": audit_before,
            "audit_counts_after": audit_after,
            "audit_counts_delta": audit_delta,
            "other_papers_unchanged": True,
            "other_papers_fingerprint": other_after,
            "final_readback": final,
            "write_events": self._write_events,
        }

    def _live_authoritative_reconciliation(self) -> dict[str, Any]:
        results, identities, _ = self.dry_run._global_result_identity_analysis()
        candidate_analysis = self.dry_run._candidate_issue_analysis()
        return self.dry_run._paper_reconciliation(
            paper_code="B0102",
            results=results,
            result_identities=identities,
            candidate_analysis=candidate_analysis,
        )

    def _reconcile_safe_entry(self, entry: dict[str, Any]) -> None:
        issue = self._required(DFTAuditIssue, entry["issue_id"], "safe_issue")
        candidate_ids = [UUID(value) for value in entry.get("candidate_ids") or []]
        if len(candidate_ids) != 1 or entry.get("candidate_id") != str(candidate_ids[0]):
            raise B0102ReconciliationError(f"safe_candidate_cardinality_mismatch:{issue.id}")
        candidate = self._required(ExternalAnalysisCandidate, candidate_ids[0], "safe_candidate")
        row = self._required(DFTResult, entry["result_id"], "safe_result")
        identity = self.lifecycle.build_identity(
            paper_id=candidate.paper_id,
            payload=candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {},
        )
        conditions = entry.get("conditions") or {}
        if set(conditions) != {
            "candidate_result_same_paper",
            "unique_target",
            "identity_observation_matches",
            "ai_verified_ml_ready",
            "currently_exportable",
            "not_rejected",
            "no_conflict_issue",
        } or not all(conditions.values()):
            raise B0102ReconciliationError(f"safe_manifest_conditions_failed:{issue.id}")
        if identity.observation_key != entry.get("observation_key") or not identity.observation_key:
            raise B0102ReconciliationError(f"safe_identity_mismatch:{issue.id}")
        gate = bulk_export_gate_results(self.session, [row], target_type="dft_results").get(str(row.id))
        if (
            issue.status not in DFT_AUDIT_ISSUE_PENDING_STATUSES
            or issue.issue_type != "missing_dft_result"
            or candidate.paper_id != row.paper_id
            or issue.paper_id != row.paper_id
            or str(row.candidate_status or "").casefold() != "ai_verified_ml_ready"
            or gate is None
            or not gate.eligible
        ):
            raise B0102ReconciliationError(f"safe_live_precondition_failed:{issue.id}")
        self.lifecycle.reconcile_candidate_binding(
            candidate=candidate,
            issue=issue,
            row=row,
            identity=identity,
            repaired_by=B0102_ACTOR,
            resolution_note=f"b0102_exact_duplicate:{row.id}",
            candidate_payload_snapshot=dict(candidate.normalized_payload or {}),
        )
        if not self.lifecycle.close_issue(
            issue,
            resolved_by=B0102_ACTOR,
            resolution_note="b0102_reconciled_exact_duplicate_v2",
            resolution_code="exact_duplicate",
        ):
            raise B0102ReconciliationError(f"safe_issue_not_closed:{issue.id}")
        self._add_audit(
            action=B0102_AUDIT_ACTION,
            target_type="dft_audit_issues",
            target_id=str(issue.id),
            payload={
                "issue_id": str(issue.id),
                "candidate_id": str(candidate.id),
                "result_id": str(row.id),
                "observation_key": identity.observation_key,
                "resolution_code": "exact_duplicate",
            },
        )
        self._write_events.append({"kind": "safe_issue", "issue_id": str(issue.id)})

    def _reconcile_split(self, entry: dict[str, Any], fixed: dict[str, Any]) -> dict[str, Any]:
        parent = self._required(DFTAuditIssue, fixed["issue_id"], "split_parent")
        old_result = self._required(DFTResult, fixed["old_result_id"], "split_old_result")
        li1 = self._required(ExternalAnalysisCandidate, fixed["li1_candidate_id"], "split_li1")
        li2 = self._required(ExternalAnalysisCandidate, fixed["li2_candidate_id"], "split_li2")
        candidates = {item["candidate_id"]: item for item in entry.get("candidates") or []}
        if set(candidates) != {str(li1.id), str(li2.id)} or entry.get("old_result_id") != str(old_result.id):
            raise B0102ReconciliationError(f"split_manifest_identity_mismatch:{parent.id}")
        li1_identity = self.lifecycle.build_identity(
            paper_id=li1.paper_id,
            payload=li1.normalized_payload if isinstance(li1.normalized_payload, dict) else {},
        )
        li2_identity = self.lifecycle.build_identity(
            paper_id=li2.paper_id,
            payload=li2.normalized_payload if isinstance(li2.normalized_payload, dict) else {},
        )
        if (
            li1_identity.observation_key != fixed["li1_observation_key"]
            or li2_identity.observation_key != fixed["li2_observation_key"]
            or li1_identity.atom_pair.canonical != "li1-s"
            or li2_identity.atom_pair.canonical != "li2-s"
            or li1_identity.subject_key == li2_identity.subject_key
        ):
            raise B0102ReconciliationError(f"split_fixed_identity_mismatch:{parent.id}")
        if parent.status not in DFT_AUDIT_ISSUE_PENDING_STATUSES:
            raise B0102ReconciliationError(f"split_parent_not_pending:{parent.id}")

        li1_child = self._create_split_child(
            parent=parent,
            candidate=li1,
            identity=li1_identity,
            row=old_result,
        )
        li2_child = self._create_split_child(
            parent=parent,
            candidate=li2,
            identity=li2_identity,
            row=None,
        )
        if not self.lifecycle.close_issue(
            parent,
            resolved_by=B0102_ACTOR,
            resolution_note="b0102_identity_split_into_v2_children",
            resolution_code="identity_split",
        ):
            raise B0102ReconciliationError(f"split_parent_not_closed:{parent.id}")
        self.lifecycle.reconcile_candidate_binding(
            candidate=li1,
            issue=li1_child,
            row=old_result,
            identity=li1_identity,
            repaired_by=B0102_ACTOR,
            resolution_note=f"b0102_split_li1_exact_duplicate:{old_result.id}",
            candidate_payload_snapshot=dict(li1.normalized_payload or {}),
        )
        if not self.lifecycle.close_issue(
            li1_child,
            resolved_by=B0102_ACTOR,
            resolution_note="b0102_split_li1_exact_duplicate",
            resolution_code="exact_duplicate",
        ):
            raise B0102ReconciliationError(f"li1_child_not_closed:{li1_child.id}")

        self.lifecycle.release_candidate_for_identity_split(
            candidate=li2,
            old_result=old_result,
            parent_issue=parent,
            child_issue=li2_child,
            candidate_identity=li2_identity,
        )
        materialized = VerificationSessionService(
            self.session,
            get_settings(),
        )._materialize_new_dft_candidates(
            paper_id=parent.paper_id,
            reviewer=B0102_ACTOR,
            candidate_ids={li2.id},
        )
        if materialized.get("materialized_count") != 1 or materialized.get("skipped_count") != 0:
            raise B0102ReconciliationError(
                f"li2_standard_materialization_failed:{canonical_json(materialized)}"
            )
        item = materialized["materialized_items"][0]
        if item.get("candidate_id") != str(li2.id) or item.get("issue_id") != str(li2_child.id):
            raise B0102ReconciliationError("li2_materializer_used_wrong_candidate_or_issue")
        new_result = self._required(DFTResult, item["dft_result_id"], "split_li2_result")
        new_identity = self.lifecycle.identity_for_result(new_result)
        if (
            new_identity.observation_key != fixed["li2_observation_key"]
            or new_identity.atom_pair.canonical != "li2-s"
            or new_result.id == old_result.id
        ):
            raise B0102ReconciliationError("li2_materialized_identity_mismatch")
        evidence = self._candidate_evidence(li2)
        if not has_evidence_anchor(evidence):
            raise B0102ReconciliationError(f"li2_pdf_evidence_anchor_missing:{li2.id}")
        li2_child.status = "pending_ai_verification"
        li2_child.result_id = new_result.id
        li2_child.target_id = str(new_result.id)
        li2_child.resolution_code = None
        li2_child.resolution_note = "b0102_materialized_candidate_requires_single_ai_verification"
        self.session.add(li2_child)
        verification = {
            "status": "pending_ai_verification",
            "writes_final_truth": False,
            "export_safety": {
                "is_exportable": False,
                "blocked_reasons": ["ai_verify_content_required"],
            },
        }
        self.session.flush()

        mapping = {
            "parent_issue_id": str(parent.id),
            "li1": {
                "child_issue_id": str(li1_child.id),
                "candidate_id": str(li1.id),
                "result_id": str(old_result.id),
                "observation_key": li1_identity.observation_key,
                "resolution_code": "exact_duplicate",
            },
            "li2": {
                "child_issue_id": str(li2_child.id),
                "candidate_id": str(li2.id),
                "result_id": str(new_result.id),
                "observation_key": new_identity.observation_key,
                "resolution_code": li2_child.resolution_code,
                "actor": "ai_candidate",
                "evidence_anchor": evidence,
                "export_gate": verification["export_safety"],
                "writes_final_truth": False,
            },
        }
        self._add_audit(
            action=B0102_SPLIT_AUDIT_ACTION,
            target_type="dft_audit_issues",
            target_id=str(parent.id),
            payload=mapping,
        )
        self._add_audit(
            action=B0102_AUDIT_ACTION,
            target_type="dft_results",
            target_id=str(new_result.id),
            payload={**mapping, "actor": "ai"},
        )
        self._write_events.append({"kind": "identity_split", **mapping})
        return mapping

    def _create_split_child(
        self,
        *,
        parent: DFTAuditIssue,
        candidate: ExternalAnalysisCandidate,
        identity: Any,
        row: DFTResult | None,
    ) -> DFTAuditIssue:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        fingerprint = DFTAuditIssueService(self.session).fingerprint_missing_issue(
            paper_id=parent.paper_id,
            payload=payload,
            issue_type="missing_dft_result",
            candidate_id=str(candidate.id),
        )
        child_id = uuid5(
            B0102_CHILD_NAMESPACE,
            f"{parent.id}:missing_dft_result:{candidate.id}:{identity.observation_key}",
        )
        existing = self.session.get(DFTAuditIssue, child_id)
        duplicate = self.session.scalar(
            select(DFTAuditIssue).where(
                DFTAuditIssue.paper_id == parent.paper_id,
                DFTAuditIssue.issue_type == "missing_dft_result",
                DFTAuditIssue.fingerprint == fingerprint,
            )
        )
        if existing is not None or duplicate is not None:
            raise B0102ReconciliationError(f"split_child_preexists_before_apply:{child_id}")
        evidence = self._candidate_evidence(candidate)
        child = DFTAuditIssue(
            id=child_id,
            paper_id=parent.paper_id,
            target_type="dft_results",
            target_id=str(row.id) if row is not None else "new",
            result_id=row.id if row is not None else None,
            issue_type="missing_dft_result",
            severity=parent.severity,
            status="needs_primary_ai",
            source_identities=list(parent.source_identities or []),
            source_candidate_ids=[str(candidate.id)],
            fingerprint=fingerprint,
            parent_issue_id=parent.id,
            evidence_payload=evidence,
            suggested_dft=parent.suggested_dft,
            resolution_note="b0102_identity_specific_split_child",
        )
        self.lifecycle.initialize_issue_identity(child, identity=identity, row=row)
        self.session.add(child)
        self.session.flush()
        self.lifecycle.bind_source_candidate(child, candidate)
        if child.issue_key is None or child.parent_issue_id != parent.id:
            raise B0102ReconciliationError("split_child_identity_initialization_failed")
        return child

    def readback(self, *, require_final: bool) -> dict[str, Any]:
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == "B0102"))
        if paper is None:
            raise B0102ReconciliationError("paper_code_not_found:B0102")
        results = list(self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all())
        statuses = Counter(str(row.candidate_status or "").casefold() for row in results)
        missing = list(
            self.session.scalars(
                select(DFTAuditIssue).where(
                    DFTAuditIssue.paper_id == paper.id,
                    DFTAuditIssue.issue_type == "missing_dft_result",
                )
            ).all()
        )
        children = [row for row in missing if row.parent_issue_id is not None]
        new_candidates = list(
            self.session.scalars(
                select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id)
            ).all()
        )
        new_candidates = [row for row in new_candidates if self.dry_run._is_new_candidate(row)]
        result_by_id = {str(row.id): row for row in results}
        bound = [
            row
            for row in new_candidates
            if row.materialized_target_type == "dft_results"
            and str(row.materialized_target_id or "") in result_by_id
        ]
        materialized_unbound = len(new_candidates) - len(bound)
        binding_conflicts = 0
        false_dedupe = 0
        by_target: dict[str, list[Any]] = defaultdict(list)
        for candidate in bound:
            identity = self.lifecycle.build_identity(
                paper_id=candidate.paper_id,
                payload=candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {},
            )
            target = result_by_id[str(candidate.materialized_target_id)]
            target_identity = self.lifecycle.identity_for_result(target)
            if identity.observation_key != target_identity.observation_key:
                binding_conflicts += 1
            by_target[str(target.id)].append(identity)
        for identities in by_target.values():
            if len({row.subject_key for row in identities}) > 1:
                false_dedupe += 1
        open_missing = [row for row in missing if row.status in DFT_AUDIT_ISSUE_PENDING_STATUSES]
        verified_ids = {
            row.id for row in results if str(row.candidate_status or "").casefold() == "ai_verified_ml_ready"
        }
        verified_open = sum(1 for row in open_missing if row.result_id in verified_ids)
        pending_issues = list(
            self.session.scalars(
                select(DFTAuditIssue).where(
                    DFTAuditIssue.paper_id == paper.id,
                    DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
                )
            ).all()
        )
        conflict_issue_types = {"duplicate_suspected", "negative_consensus", "uncertain"}
        conflict_resolution_codes = {"scientific_conflict", "binding_conflict", "invalid_identity"}
        conflict_stages = {"binding_conflict"}
        conflict_error_tokens = ("identity", "binding_conflict", "scientific_conflict", "false_dedupe")
        identity_conflict_rows = [
            row
            for row in pending_issues
            if row.issue_type in conflict_issue_types
            or row.resolution_code in conflict_resolution_codes
            or row.lifecycle_stage in conflict_stages
            or any(token in str(row.last_error_code or "").casefold() for token in conflict_error_tokens)
        ]
        identity_conflicts = len(identity_conflict_rows)
        observation_groups: dict[str, list[str]] = defaultdict(list)
        for row in results:
            identity = self.lifecycle.identity_for_result(row)
            if identity.dedupe_allowed and identity.observation_key:
                observation_groups[identity.observation_key].append(str(row.id))
        duplicate_observation_keys = {
            key: values for key, values in observation_groups.items() if len(values) > 1
        }
        gates = bulk_export_gate_results(self.session, results, target_type="dft_results")
        non_rejected = [row for row in results if str(row.candidate_status or "").casefold() != "rejected"]
        rejected = [row for row in results if str(row.candidate_status or "").casefold() == "rejected"]
        exportable_non_rejected = sum(bool(gates.get(str(row.id)) and gates[str(row.id)].eligible) for row in non_rejected)
        blocked_rejected = sum(bool(gates.get(str(row.id)) and not gates[str(row.id)].eligible) for row in rejected)

        child_payload = []
        for child in sorted(children, key=lambda row: str(row.id)):
            sources = list(
                self.session.scalars(
                    select(DFTAuditIssueSource).where(DFTAuditIssueSource.issue_id == child.id)
                ).all()
            )
            child_payload.append(
                {
                    "issue_id": str(child.id),
                    "parent_issue_id": str(child.parent_issue_id),
                    "status": child.status,
                    "resolution_code": child.resolution_code,
                    "result_id": str(child.result_id) if child.result_id else None,
                    "source_candidate_ids": sorted(str(row.candidate_id) for row in sources),
                }
            )
        split_lineage = self._split_lineage_readback(children, results)
        audit_counts = self._audit_action_counts()
        payload = {
            "dft_result_total": len(results),
            "ai_verified_ml_ready": statuses.get("ai_verified_ml_ready", 0),
            "rejected": statuses.get("rejected", 0),
            "open_missing_dft_result": len(open_missing),
            "missing_issue_total": len(missing),
            "missing_issue_closed": sum(row.status == "closed" for row in missing),
            "child_issue_count": len(children),
            "child_issues": child_payload,
            "new_candidate": len(new_candidates),
            "distinct_bound_targets": len({str(row.materialized_target_id) for row in bound}),
            "materialized_but_unbound": materialized_unbound,
            "verified_with_open_missing": verified_open,
            "false_scientific_dedupe": false_dedupe,
            "binding_conflict": binding_conflicts,
            "identity_conflict": identity_conflicts,
            "identity_conflict_issue_ids": sorted(str(row.id) for row in identity_conflict_rows),
            "duplicate_observation_keys": duplicate_observation_keys,
            "export_gate": {
                "non_rejected_total": len(non_rejected),
                "non_rejected_exportable": exportable_non_rejected,
                "rejected_total": len(rejected),
                "rejected_blocked": blocked_rejected,
            },
            "fixed_split_science": self._fixed_split_science_readback(results),
            "split_lineage": split_lineage,
            "reconciliation_audit_counts": audit_counts,
            "lifecycle_reconciled": (
                materialized_unbound == 0
                and verified_open == 0
                and false_dedupe == 0
                and binding_conflicts == 0
                and identity_conflicts == 0
                and not open_missing
            ),
            "review_scope_complete": None,
            "review_scope_gate_status": "not_available_in_current_schema",
            "is_complete": None,
        }
        payload["is_exact_final_state"] = self._is_exact_final_readback(payload)
        if require_final and not payload["is_exact_final_state"]:
            raise B0102ReconciliationError(f"final_readback_mismatch:{canonical_json(payload)}")
        return payload

    @staticmethod
    def _is_exact_final_readback(payload: dict[str, Any]) -> bool:
        export = payload["export_gate"]
        return (
            payload["dft_result_total"] == 377
            and payload["ai_verified_ml_ready"] == 375
            and payload["rejected"] == 2
            and payload["open_missing_dft_result"] == 0
            and payload["missing_issue_total"] == 372
            and payload["missing_issue_closed"] == 372
            and payload["child_issue_count"] == 4
            and payload["new_candidate"] == 370
            and payload["distinct_bound_targets"] == 370
            and payload["materialized_but_unbound"] == 0
            and payload["verified_with_open_missing"] == 0
            and payload["false_scientific_dedupe"] == 0
            and payload["binding_conflict"] == 0
            and payload["identity_conflict"] == 0
            and not payload["duplicate_observation_keys"]
            and export["non_rejected_total"] == 375
            and export["non_rejected_exportable"] == 375
            and export["rejected_total"] == 2
            and export["rejected_blocked"] == 2
            and payload["fixed_split_science"]["valid"] is True
            and payload["split_lineage"]["valid"] is True
            and payload["reconciliation_audit_counts"] == {
                B0102_AUDIT_ACTION: 368,
                B0102_SPLIT_AUDIT_ACTION: 2,
            }
            and payload["lifecycle_reconciled"] is True
        )

    def _split_lineage_readback(
        self,
        children: list[DFTAuditIssue],
        results: list[DFTResult],
    ) -> dict[str, Any]:
        child_by_id = {row.id: row for row in children}
        result_by_observation: dict[str, list[DFTResult]] = defaultdict(list)
        for row in results:
            identity = self.lifecycle.identity_for_result(row)
            if identity.observation_key:
                result_by_observation[identity.observation_key].append(row)
        expected_rows: list[dict[str, Any]] = []
        valid = True
        for fixed in B0102_SPLIT_FIXED:
            parent_id = UUID(fixed["issue_id"])
            parent = self.session.get(DFTAuditIssue, parent_id)
            li2_results = result_by_observation.get(fixed["li2_observation_key"], [])
            li2_result_id = li2_results[0].id if len(li2_results) == 1 else None
            for lane, candidate_key, observation_key, resolution_code, expected_result_id in (
                (
                    "li1",
                    "li1_candidate_id",
                    fixed["li1_observation_key"],
                    "exact_duplicate",
                    UUID(fixed["old_result_id"]),
                ),
                (
                    "li2",
                    "li2_candidate_id",
                    fixed["li2_observation_key"],
                    "verified",
                    li2_result_id,
                ),
            ):
                candidate_id = UUID(fixed[candidate_key])
                child_id = uuid5(
                    B0102_CHILD_NAMESPACE,
                    f"{parent_id}:missing_dft_result:{candidate_id}:{observation_key}",
                )
                child = child_by_id.get(child_id)
                sources = (
                    list(
                        self.session.scalars(
                            select(DFTAuditIssueSource).where(
                                DFTAuditIssueSource.issue_id == child_id
                            )
                        ).all()
                    )
                    if child is not None
                    else []
                )
                row_valid = bool(
                    parent is not None
                    and parent.status == "closed"
                    and parent.resolution_code == "identity_split"
                    and child is not None
                    and child.parent_issue_id == parent_id
                    and child.status == "closed"
                    and child.resolution_code == resolution_code
                    and expected_result_id is not None
                    and child.result_id == expected_result_id
                    and child.target_type == "dft_results"
                    and child.target_id == str(expected_result_id)
                    and child.source_candidate_ids == [str(candidate_id)]
                    and [row.candidate_id for row in sources] == [candidate_id]
                )
                valid = valid and row_valid
                expected_rows.append(
                    {
                        "lane": lane,
                        "parent_issue_id": str(parent_id),
                        "parent_status": parent.status if parent else None,
                        "parent_resolution_code": parent.resolution_code if parent else None,
                        "child_issue_id": str(child_id),
                        "candidate_id": str(candidate_id),
                        "result_id": str(expected_result_id) if expected_result_id else None,
                        "status": child.status if child else None,
                        "resolution_code": child.resolution_code if child else None,
                        "source_candidate_ids": sorted(str(row.candidate_id) for row in sources),
                        "valid": row_valid,
                    }
                )
        return {
            "valid": valid and len(child_by_id) == 4 and len(expected_rows) == 4,
            "rows": expected_rows,
        }

    def _fixed_split_science_readback(self, results: list[DFTResult]) -> dict[str, Any]:
        by_observation: dict[str, list[DFTResult]] = defaultdict(list)
        for row in results:
            identity = self.lifecycle.identity_for_result(row)
            if identity.observation_key:
                by_observation[identity.observation_key].append(row)
        rows: list[dict[str, Any]] = []
        valid = True
        new_ids: set[UUID] = set()
        old_ids = {UUID(fixed["old_result_id"]) for fixed in B0102_SPLIT_FIXED}
        for fixed in B0102_SPLIT_FIXED:
            old = self.session.get(DFTResult, UUID(fixed["old_result_id"]))
            matches = by_observation.get(fixed["li2_observation_key"], [])
            new = matches[0] if len(matches) == 1 else None
            old_identity = self.lifecycle.identity_for_result(old) if old is not None else None
            new_identity = self.lifecycle.identity_for_result(new) if new is not None else None
            row_valid = bool(
                old is not None
                and new is not None
                and new.id not in old_ids
                and new.id not in new_ids
                and old_identity is not None
                and old_identity.observation_key == fixed["li1_observation_key"]
                and old_identity.atom_pair.canonical == "li1-s"
                and new_identity is not None
                and new_identity.observation_key == fixed["li2_observation_key"]
                and new_identity.atom_pair.canonical == "li2-s"
                and str(new.property_type or "").casefold() == "bond_length"
                and str(new.unit or "") == fixed["unit"]
                and format(float(new.value), ".12g") == fixed["value"]
            )
            if new is not None:
                new_ids.add(new.id)
            valid = valid and row_valid
            rows.append(
                {
                    "parent_issue_id": fixed["issue_id"],
                    "old_li1_result_id": fixed["old_result_id"],
                    "old_li1_observation_key": old_identity.observation_key if old_identity else None,
                    "old_li1_atom_pair": old_identity.atom_pair.canonical if old_identity else None,
                    "new_li2_result_id": str(new.id) if new else None,
                    "new_li2_observation_key": new_identity.observation_key if new_identity else None,
                    "new_li2_atom_pair": new_identity.atom_pair.canonical if new_identity else None,
                    "property_type": new.property_type if new else None,
                    "value": new.value if new else None,
                    "unit": new.unit if new else None,
                    "valid": row_valid,
                }
            )
        return {"valid": valid and len(new_ids) == 2, "rows": rows}

    def _other_papers_fingerprint(self) -> dict[str, Any]:
        columns = self.session.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name='paper_id' ORDER BY table_name COLLATE \"C\""
            )
        ).scalars().all()
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == "B0102"))
        quote = self.session.get_bind().dialect.identifier_preparer.quote
        entries: list[dict[str, Any]] = []
        for raw_table in columns:
            table_name = str(raw_table)
            rows = self.session.execute(
                text(
                    f"SELECT paper_id::text, to_jsonb(t)::text FROM public.{quote(table_name)} AS t "
                    "WHERE paper_id IS NOT NULL AND paper_id <> :paper_id ORDER BY paper_id::text, to_jsonb(t)::text"
                ),
                {"paper_id": paper.id},
            ).all()
            grouped: dict[str, list[str]] = defaultdict(list)
            for paper_id, row_json in rows:
                grouped[str(paper_id)].append(str(row_json))
            for paper_id, values in sorted(grouped.items()):
                entries.append(
                    {
                        "table": table_name,
                        "paper_id": paper_id,
                        "row_count": len(values),
                        "sha256": hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest(),
                    }
                )
        return {"entries": entries, "sha256": canonical_sha256(entries)}

    def _fixed_old_result_science(self) -> dict[str, Any]:
        fields = (
            "paper_id",
            "catalyst_sample_id",
            "adsorbate",
            "property_type",
            "value",
            "value_upper",
            "value_kind",
            "unit",
            "reaction_step",
            "reaction_type",
            "source_section",
            "source_figure",
            "evidence_text",
            "evidence_payload",
        )
        return {
            fixed["old_result_id"]: {
                field: getattr(self._required(DFTResult, fixed["old_result_id"], "old_result"), field)
                for field in fields
            }
            for fixed in B0102_SPLIT_FIXED
        }

    def _audit_action_counts(self) -> dict[str, int]:
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == "B0102"))
        rows = self.session.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .where(
                AuditLog.paper_id == paper.id,
                AuditLog.action.in_([B0102_AUDIT_ACTION, B0102_SPLIT_AUDIT_ACTION]),
            )
            .group_by(AuditLog.action)
        ).all()
        return {str(action): int(count) for action, count in rows}

    def _add_audit(self, *, action: str, target_type: str, target_id: str, payload: dict[str, Any]) -> None:
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == "B0102"))
        self.session.add(
            AuditLog(
                paper_id=paper.id,
                action=action,
                source=B0102_ACTOR,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
            )
        )
        self.session.flush()

    @staticmethod
    def _candidate_evidence(candidate: ExternalAnalysisCandidate) -> dict[str, Any] | list[Any] | None:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        return payload.get("evidence_location") or payload.get("evidence_payload") or candidate.evidence_payload

    def _required(self, model: Any, value: str | UUID, label: str) -> Any:
        try:
            row_id = value if isinstance(value, UUID) else UUID(str(value))
        except ValueError as exc:
            raise B0102ReconciliationError(f"invalid_{label}_id:{value}") from exc
        row = self.session.get(model, row_id)
        if row is None:
            raise B0102ReconciliationError(f"missing_{label}:{row_id}")
        return row

    def _assert_transaction_active(self) -> None:
        self.session.connection()
        if not self.session.in_transaction():
            raise B0102ReconciliationError("explicit_transaction_required")
        read_only = str(self.session.scalar(text("SHOW transaction_read_only"))).casefold()
        if read_only == "on":
            raise B0102ReconciliationError("reconciliation_transaction_must_be_writable")

    @staticmethod
    def _fault(configured: str | None, stage: str) -> None:
        if configured == stage:
            raise B0102ReconciliationError(f"injected_fault:{stage}")
