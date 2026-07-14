from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    utcnow,
)
from app.services.dft_identity_service import (
    ATOM_PAIR_IDENTITY_ERRORS,
    AtomPairIdentity,
    DFTIdentityV2,
    build_dft_identity_v2,
)


DFT_AUDIT_ISSUE_PENDING_STATUSES = {
    "open",
    "needs_primary_ai",
    "needs_user_decision",
    "fixed_by_primary_ai",
}
DFT_AUDIT_ISSUE_TERMINAL_STATUSES = {"closed", "false_positive"}

DFT_IDENTITY_VERSION = 2
DFT_ISSUE_KEY_VERSION = 2
DFT_LIFECYCLE_VERSION = 2

DFT_LIFECYCLE_STAGES = {
    "discovered": "discovered",
    "pending_verification": "pending_verification",
    "verification_failed": "verification_failed",
    "binding_conflict": "binding_conflict",
    "verified": "verified",
    "rejected": "rejected",
    "closed": "closed",
}

DFT_RESOLUTION_CODES = {
    "verified": "verified",
    "rejected": "rejected",
    "exact_duplicate": "exact_duplicate",
    "binding_conflict": "binding_conflict",
    "scientific_conflict": "scientific_conflict",
    "invalid_identity": "invalid_identity",
    "identity_split": "identity_split",
}


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
        "value_upper",
        "value_kind",
        "unit",
        "reaction_step",
        "reaction_type",
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
        batch_issues = self.session.info.get("dft_import_active_issues_by_target")
        target_id_text = str(target_id)
        if target_type == "dft_results" and isinstance(batch_issues, dict) and target_id_text in batch_issues:
            return [
                issue
                for issue in batch_issues[target_id_text]
                if issue.status in DFT_AUDIT_ISSUE_PENDING_STATUSES
                and (
                    str(issue.result_id) == target_id_text
                    if issue.result_id is not None
                    else issue.target_type == target_type and str(issue.target_id) == target_id_text
                )
            ]
        target_match = DFTAuditIssue.target_id == target_id_text
        if target_type == "dft_results":
            try:
                result_id = UUID(target_id_text)
            except ValueError:
                result_id = None
            if result_id is not None:
                # V2 result_id is authoritative. Legacy target_id remains a
                # compatibility fallback only for historical rows with no v2 link.
                target_match = or_(
                    DFTAuditIssue.result_id == result_id,
                    (
                        DFTAuditIssue.result_id.is_(None)
                        & (DFTAuditIssue.target_type == target_type)
                        & (DFTAuditIssue.target_id == target_id_text)
                    ),
                )
        return list(
            self.session.scalars(
                select(DFTAuditIssue)
                .where(DFTAuditIssue.paper_id == paper_id)
                .where(target_match)
                .where(DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)))
                .order_by(DFTAuditIssue.created_at.asc(), DFTAuditIssue.id.asc())
            ).all()
        )

    @staticmethod
    def build_identity(*, paper_id: UUID, payload: dict[str, Any]) -> DFTIdentityV2:
        """Build the authoritative v2 identity with the root paper explicitly injected."""

        return build_dft_identity_v2({**dict(payload or {}), "paper_id": str(paper_id)})

    def identity_for_result(self, row: DFTResult) -> DFTIdentityV2:
        """Read stored v2 identity first; transiently derive it for legacy NULL rows.

        The legacy derivation is deliberately read-only. Callers must not persist it
        as an incidental backfill.
        """

        if row.identity_version == DFT_IDENTITY_VERSION and str(row.subject_key or "").strip():
            payload = row.identity_payload if isinstance(row.identity_payload, dict) else {}
            atom_payload = payload.get("atom_pair") if isinstance(payload.get("atom_pair"), dict) else {}
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            atom_error = self._text_or_none(atom_payload.get("error_code")) or next(
                (str(value) for value in errors if str(value) in ATOM_PAIR_IDENTITY_ERRORS),
                None,
            )
            atom_pair = AtomPairIdentity(
                canonical=self._text_or_none(atom_payload.get("canonical")),
                normalized_aliases=tuple(str(value) for value in atom_payload.get("normalized_aliases", []) if value),
                error_code=atom_error,
                required=bool(atom_payload.get("required")),
                symmetric=bool(atom_payload.get("symmetric")),
            )
            return DFTIdentityV2(
                identity_version=DFT_IDENTITY_VERSION,
                subject_key=str(row.subject_key),
                observation_key=self._text_or_none(row.observation_key),
                identity_payload=payload,
                atom_pair=atom_pair,
                error_codes=tuple(str(value) for value in errors),
                dedupe_allowed=bool(row.observation_key),
            )
        return self.build_identity(
            paper_id=row.paper_id,
            payload=self.authoritative_payload_for_result(row),
        )

    def authoritative_payload_for_result(
        self,
        row: DFTResult,
        *,
        catalyst_sample: CatalystSample | None = None,
    ) -> dict[str, Any]:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        material_identity = self._text_or_none(evidence.get("material_identity"))
        if catalyst_sample is not None:
            sample = catalyst_sample
        elif row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
        else:
            sample = None
        if sample is not None and str(sample.name or "").strip():
            material_identity = str(sample.name).strip()
        return {
            "corrected_value": {
                "material_identity": material_identity,
                "property_type": row.property_type,
                "adsorbate": row.adsorbate,
                "reaction_step": row.reaction_step,
                "reaction_type": row.reaction_type,
                "value": row.value,
                "value_upper": row.value_upper,
                "value_kind": row.value_kind,
                "unit": row.unit,
                **{
                    key: evidence.get(key)
                    for key in (
                        "property_subtype",
                        "active_site_instance_key",
                        "atom_pair",
                        "bond_pair",
                        "bond",
                        "interaction_pair",
                        "site_label",
                        "state_context",
                        "method",
                        "functional",
                    )
                    if evidence.get(key) not in (None, "", [])
                },
            },
            "evidence_payload": evidence,
        }

    @staticmethod
    def apply_result_identity(row: DFTResult, identity: DFTIdentityV2) -> None:
        row.identity_version = identity.identity_version
        row.subject_key = identity.subject_key
        row.observation_key = identity.observation_key
        row.identity_payload = identity.identity_payload

    def clear_result_observation_key_for_rekey(self, row: DFTResult) -> None:
        """Temporarily release the v2 observation key before an atomic batch rekey."""

        row.observation_key = None
        self.session.add(row)

    def classify_result_identity(
        self,
        *,
        paper_id: UUID,
        identity: DFTIdentityV2,
        exclude_result_id: UUID | None = None,
        rows: list[DFTResult] | None = None,
        identity_cache: dict[UUID, DFTIdentityV2] | None = None,
    ) -> tuple[DFTResult | None, list[DFTResult]]:
        """Return exact duplicate and scientific conflicts using only v2 semantics."""

        if not identity.observation_key:
            return None, []
        exact: DFTResult | None = None
        conflicts: list[DFTResult] = []
        candidate_rows = rows
        if candidate_rows is None:
            candidate_rows = list(
                self.session.scalars(
                    select(DFTResult).where(DFTResult.paper_id == paper_id)
                ).all()
            )
        for row in candidate_rows:
            if exclude_result_id is not None and row.id == exclude_result_id:
                continue
            row_identity = (identity_cache or {}).get(row.id)
            if row_identity is None:
                row_identity = self.identity_for_result(row)
                if identity_cache is not None:
                    identity_cache[row.id] = row_identity
            if not row_identity.observation_key:
                continue
            if row_identity.observation_key == identity.observation_key:
                exact = exact or row
            elif row_identity.subject_key == identity.subject_key:
                conflicts.append(row)
        return exact, conflicts

    @staticmethod
    def issue_key(*, paper_id: UUID, issue_type: str, identity: DFTIdentityV2) -> str | None:
        if not identity.observation_key:
            return None
        canonical = json.dumps(
            [
                "dft-issue-v2",
                str(paper_id),
                str(issue_type),
                identity.subject_key,
                identity.observation_key,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def initialize_issue_identity(
        self,
        issue: DFTAuditIssue,
        *,
        identity: DFTIdentityV2,
        row: DFTResult | None = None,
        lifecycle_stage: str = DFT_LIFECYCLE_STAGES["discovered"],
        resolution_code: str | None = None,
        last_error_code: str | None = None,
    ) -> DFTAuditIssue:
        if not identity.observation_key:
            lifecycle_stage = DFT_LIFECYCLE_STAGES["verification_failed"]
            resolution_code = DFT_RESOLUTION_CODES["invalid_identity"]
            last_error_code = identity.error_code or last_error_code or "invalid_v2_result_identity"
        issue.issue_key_version = DFT_ISSUE_KEY_VERSION
        issue.issue_key = self.issue_key(
            paper_id=issue.paper_id,
            issue_type=issue.issue_type,
            identity=identity,
        )
        issue.lifecycle_version = DFT_LIFECYCLE_VERSION
        issue.lifecycle_stage = lifecycle_stage
        issue.resolution_code = resolution_code
        issue.last_error_code = last_error_code or identity.error_code
        issue.retry_count = int(issue.retry_count or 0)
        if row is not None:
            if row.paper_id != issue.paper_id:
                raise ValueError("DFT audit issue and DFT result belong to different papers.")
            if issue.result_id is not None and issue.result_id != row.id:
                raise ValueError("dft_audit_issue_bound_to_different_result")
            issue.result_id = row.id
            # Legacy double write only; v2 result_id is authoritative.
            issue.target_type = "dft_results"
            issue.target_id = str(row.id)
        self.session.add(issue)
        return issue

    def bind_source_candidate(self, issue: DFTAuditIssue, candidate: ExternalAnalysisCandidate) -> bool:
        if candidate.paper_id != issue.paper_id:
            raise ValueError("DFT audit issue and candidate belong to different papers.")
        legacy_ids = [str(value) for value in (issue.source_candidate_ids or []) if str(value).strip()]
        if str(candidate.id) not in legacy_ids:
            legacy_ids.append(str(candidate.id))
            issue.source_candidate_ids = legacy_ids
            self.session.add(issue)
        key = {"issue_id": issue.id, "candidate_id": candidate.id}
        if self.session.get(DFTAuditIssueSource, key) is not None:
            return False
        self.session.add(DFTAuditIssueSource(**key))
        self.session.flush()
        return True

    def bind_known_source_candidates(self, issue: DFTAuditIssue) -> int:
        candidate_ids: list[UUID] = []
        for value in issue.source_candidate_ids or []:
            try:
                candidate_ids.append(UUID(str(value)))
            except ValueError:
                continue
        if not candidate_ids:
            return 0
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.id.in_(candidate_ids),
                ExternalAnalysisCandidate.paper_id == issue.paper_id,
            )
        ).all()
        return sum(1 for candidate in candidates if self.bind_source_candidate(issue, candidate))

    def reconcile_candidate_binding(
        self,
        *,
        candidate: ExternalAnalysisCandidate,
        issue: DFTAuditIssue,
        row: DFTResult,
        identity: DFTIdentityV2,
        repaired_by: str,
        resolution_note: str,
        candidate_payload_snapshot: dict[str, Any] | None = None,
    ) -> tuple[ExternalAnalysisCandidate, DFTAuditIssue]:
        """Lock, revalidate, and double-write one candidate/issue/result binding."""

        locked_candidate, locked_issue = self.lock_candidate_issue_for_reconcile(
            candidate=candidate,
            issue=issue,
            row=row,
            identity=identity,
            candidate_payload_snapshot=candidate_payload_snapshot,
        )
        if self.is_terminal_issue(locked_issue):
            return locked_candidate, locked_issue
        self.bind_candidate_to_result(locked_candidate, row)
        self.bind_missing_issue_to_result(
            locked_issue,
            row,
            repaired_by=repaired_by,
            resolution_note=resolution_note,
            identity=identity,
        )
        self.bind_source_candidate(locked_issue, locked_candidate)
        self.session.flush()
        return locked_candidate, locked_issue

    def lock_candidate_issue_for_reconcile(
        self,
        *,
        candidate: ExternalAnalysisCandidate,
        issue: DFTAuditIssue,
        row: DFTResult | None,
        identity: DFTIdentityV2,
        candidate_payload_snapshot: dict[str, Any] | None = None,
    ) -> tuple[ExternalAnalysisCandidate, DFTAuditIssue]:
        """Lock and revalidate the pre-parsed candidate snapshot before writes."""

        locked_candidate = self.session.scalar(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.id == candidate.id)
            .with_for_update()
        )
        locked_issue = self.session.scalar(
            select(DFTAuditIssue).where(DFTAuditIssue.id == issue.id).with_for_update()
        )
        if locked_candidate is None or locked_issue is None:
            raise ValueError("dft_binding_snapshot_drift")
        if candidate_payload_snapshot is not None:
            live_payload = (
                locked_candidate.normalized_payload
                if isinstance(locked_candidate.normalized_payload, dict)
                else {}
            )
            if self._canonical_json(live_payload) != self._canonical_json(candidate_payload_snapshot):
                raise ValueError("dft_candidate_snapshot_drift")
            locked_identity = self.build_identity(
                paper_id=locked_candidate.paper_id,
                payload=live_payload,
            )
            if (
                locked_identity.subject_key != identity.subject_key
                or locked_identity.observation_key != identity.observation_key
                or locked_identity.error_codes != identity.error_codes
            ):
                raise ValueError("dft_candidate_identity_snapshot_drift")
        self.assert_candidate_binding_compatible(locked_candidate, row)
        self.assert_missing_issue_binding_compatible(locked_issue, row)
        if row is not None:
            row_identity = self.identity_for_result(row)
            if (
                row_identity.subject_key != identity.subject_key
                or row_identity.observation_key != identity.observation_key
            ):
                raise ValueError("dft_result_identity_mismatch")
        return locked_candidate, locked_issue

    def bind_missing_issue_to_result(
        self,
        issue: DFTAuditIssue,
        row: DFTResult,
        *,
        repaired_by: str,
        resolution_note: str | None = None,
        identity: DFTIdentityV2 | None = None,
    ) -> DFTAuditIssue:
        if issue.paper_id != row.paper_id:
            raise ValueError("DFT audit issue and DFT result belong to different papers.")
        current_result_id = issue.result_id
        current_target = str(issue.target_id or "").strip()
        if current_result_id is not None:
            if current_result_id != row.id:
                raise ValueError("dft_audit_issue_bound_to_different_result")
        elif current_target and current_target.lower() != "new":
            if issue.target_type != "dft_results" or current_target != str(row.id):
                raise ValueError("dft_audit_issue_bound_to_different_result")
        # Ordinary imports must not mutate terminal issues or their source
        # relations. Compatibility is checked first so a different target still
        # fails deterministically instead of being silently ignored.
        if issue.status in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            return issue
        resolved_identity = identity or self.identity_for_result(row)
        identity_is_invalid = not resolved_identity.observation_key
        self.initialize_issue_identity(
            issue,
            identity=resolved_identity,
            row=row,
            lifecycle_stage=(
                DFT_LIFECYCLE_STAGES["verification_failed"]
                if identity_is_invalid
                else DFT_LIFECYCLE_STAGES["pending_verification"]
            ),
            resolution_code=(
                DFT_RESOLUTION_CODES["invalid_identity"]
                if identity_is_invalid
                else None
            ),
            last_error_code=(
                resolved_identity.error_code or "invalid_v2_result_identity"
                if identity_is_invalid
                else None
            ),
        )
        self.bind_known_source_candidates(issue)
        issue.status = "fixed_by_primary_ai"
        issue.current_snapshot = self.snapshot_dft_result(row)
        issue.resolved_by = None
        issue.resolved_at = None
        issue.resolution_note = resolution_note or f"bound_dft_result:{row.id}"
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
        return issue

    def assert_missing_issue_binding_compatible(
        self,
        issue: DFTAuditIssue,
        row: DFTResult | None,
    ) -> None:
        if issue.result_id is not None:
            if row is None or issue.result_id != row.id:
                raise ValueError("dft_audit_issue_bound_to_different_result")
            return
        current_target = str(issue.target_id or "").strip()
        if not current_target or current_target.lower() == "new":
            return
        if row is None or issue.target_type != "dft_results" or current_target != str(row.id):
            raise ValueError("dft_audit_issue_bound_to_different_result")

    def assert_candidate_binding_compatible(
        self,
        candidate: ExternalAnalysisCandidate,
        row: DFTResult | None,
    ) -> None:
        current_type = str(candidate.materialized_target_type or "").strip()
        current_id = str(candidate.materialized_target_id or "").strip()
        if not current_type and not current_id:
            return
        if row is None or current_type != "dft_results" or current_id != str(row.id):
            raise ValueError("dft_candidate_bound_to_different_result")

    def bind_candidate_to_result(
        self,
        candidate: ExternalAnalysisCandidate,
        row: DFTResult,
    ) -> bool:
        if candidate.paper_id != row.paper_id:
            raise ValueError("DFT candidate and DFT result belong to different papers.")
        current_type = str(candidate.materialized_target_type or "").strip()
        current_id = str(candidate.materialized_target_id or "").strip()
        if current_type or current_id:
            if current_type == "dft_results" and current_id == str(row.id):
                if candidate.status != "materialized":
                    candidate.status = "materialized"
                    self.session.add(candidate)
                    self.session.flush()
                return False
            raise ValueError("dft_candidate_bound_to_different_result")
        candidate.materialized_target_type = "dft_results"
        candidate.materialized_target_id = str(row.id)
        candidate.status = "materialized"
        self.session.add(candidate)
        self.session.flush()
        return True

    def release_candidate_for_identity_split(
        self,
        *,
        candidate: ExternalAnalysisCandidate,
        old_result: DFTResult,
        parent_issue: DFTAuditIssue,
        child_issue: DFTAuditIssue,
        candidate_identity: DFTIdentityV2,
    ) -> ExternalAnalysisCandidate:
        """Release one false-deduped candidate under an explicit split lineage.

        This is intentionally narrower than a general unbind operation.  It is
        only valid after the terminal parent records ``identity_split`` and the
        identity-specific child/source relation already exists.
        """

        locked = self.session.scalar(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.id == candidate.id)
            .with_for_update()
        )
        if locked is None:
            raise ValueError("dft_candidate_snapshot_drift")
        if parent_issue.status not in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            raise ValueError("identity_split_parent_must_be_terminal")
        if parent_issue.resolution_code != DFT_RESOLUTION_CODES["identity_split"]:
            raise ValueError("identity_split_parent_resolution_missing")
        if child_issue.parent_issue_id != parent_issue.id:
            raise ValueError("identity_split_child_parent_mismatch")
        if child_issue.status in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            raise ValueError("identity_split_child_must_be_pending")
        if child_issue.issue_key != self.issue_key(
            paper_id=child_issue.paper_id,
            issue_type=child_issue.issue_type,
            identity=candidate_identity,
        ):
            raise ValueError("identity_split_child_issue_key_mismatch")
        relation = self.session.get(
            DFTAuditIssueSource,
            {"issue_id": child_issue.id, "candidate_id": locked.id},
        )
        if relation is None or child_issue.source_candidate_ids != [str(locked.id)]:
            raise ValueError("identity_split_child_source_relation_missing")
        if locked.paper_id != old_result.paper_id or child_issue.paper_id != locked.paper_id:
            raise ValueError("identity_split_cross_paper_release")
        if (
            locked.materialized_target_type != "dft_results"
            or str(locked.materialized_target_id or "") != str(old_result.id)
        ):
            raise ValueError("identity_split_candidate_old_binding_mismatch")
        old_identity = self.identity_for_result(old_result)
        if not candidate_identity.observation_key or not old_identity.observation_key:
            raise ValueError("identity_split_requires_valid_v2_identity")
        if candidate_identity.subject_key == old_identity.subject_key:
            raise ValueError("identity_split_subject_not_distinct")
        locked.materialized_target_type = None
        locked.materialized_target_id = None
        locked.status = "candidate"
        locked.mapping_reason = "identity_split_reconciliation"
        self.session.add(locked)
        self.session.flush()
        return locked

    def mark_pending(
        self,
        issue: DFTAuditIssue,
        *,
        status: str,
        note: str | None = None,
    ) -> DFTAuditIssue:
        if status not in DFT_AUDIT_ISSUE_PENDING_STATUSES:
            raise ValueError(f"Unsupported pending DFT audit issue status: {status}")
        if issue.status in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            return issue
        issue.status = status
        issue.resolved_by = None
        issue.resolved_at = None
        issue.resolution_note = note
        issue.lifecycle_version = DFT_LIFECYCLE_VERSION
        if "binding_conflict" in str(note or "") or "bound_to_different_result" in str(note or ""):
            issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["binding_conflict"]
            issue.resolution_code = DFT_RESOLUTION_CODES["binding_conflict"]
            issue.last_error_code = "binding_conflict"
        elif "conflicting_dft_observation" in str(note or ""):
            issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["binding_conflict"]
            issue.resolution_code = DFT_RESOLUTION_CODES["scientific_conflict"]
            issue.last_error_code = "scientific_conflict"
        elif issue.issue_key_version == DFT_ISSUE_KEY_VERSION and issue.issue_key is None and issue.resolution_code == DFT_RESOLUTION_CODES["invalid_identity"]:
            issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["verification_failed"]
            issue.last_error_code = issue.last_error_code or "invalid_v2_result_identity"
        elif "invalid" in str(note or "") or "missing_" in str(note or ""):
            issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["verification_failed"]
            issue.resolution_code = DFT_RESOLUTION_CODES["invalid_identity"]
            issue.last_error_code = str(note or "invalid_identity").split(":", 1)[0]
        else:
            issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["pending_verification"]
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
        resolution_code: str | None = None,
    ) -> bool:
        if status not in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal DFT audit issue status: {status}")
        if issue.status in DFT_AUDIT_ISSUE_TERMINAL_STATUSES:
            return False
        issue.status = status
        issue.resolved_by = resolved_by
        issue.resolved_at = utcnow()
        issue.resolution_note = resolution_note
        issue.lifecycle_version = DFT_LIFECYCLE_VERSION
        issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["closed"]
        issue.resolution_code = resolution_code or issue.resolution_code or DFT_RESOLUTION_CODES["verified"]
        issue.last_error_code = None
        issue.retry_count = 0
        issue.next_retry_at = None
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
        return True

    def apply_verify(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
        actor_type: str,
        export_gate_passed: bool = True,
    ) -> list[DFTAuditIssue]:
        normalized_actor = str(actor_type or "").strip().lower()
        if normalized_actor not in {"human", "ai"}:
            raise ValueError("actor_type must be human or ai")
        resolution_note = "human_verified" if normalized_actor == "human" else "ai_verified"
        row = self.session.get(DFTResult, result_id)
        result_identity = self.identity_for_result(row) if row is not None and row.paper_id == paper_id else None
        closed: list[DFTAuditIssue] = []
        for issue in self.active_issues_for_target(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=result_id,
        ):
            if issue.issue_type == "source_scope_error":
                continue
            if issue.issue_type == "missing_dft_result" and (
                result_identity is None or not result_identity.observation_key
            ):
                error_code = (
                    result_identity.error_code
                    if result_identity is not None
                    else "missing_v2_result_identity"
                )
                self.mark_pending(
                    issue,
                    status="fixed_by_primary_ai",
                    note=error_code or "invalid_v2_result_identity",
                )
                issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["verification_failed"]
                issue.resolution_code = DFT_RESOLUTION_CODES["invalid_identity"]
                issue.last_error_code = error_code or "invalid_v2_result_identity"
                issue.retry_count = int(issue.retry_count or 0) + 1
                self.session.add(issue)
                continue
            if not export_gate_passed and issue.issue_type == "missing_dft_result":
                self.mark_pending(
                    issue,
                    status="fixed_by_primary_ai",
                    note="export_gate_failed_after_verification",
                )
                issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["verification_failed"]
                issue.last_error_code = "export_gate_failed"
                issue.retry_count = int(issue.retry_count or 0) + 1
                self.session.add(issue)
                continue
            missing_target_matches = (
                issue.result_id == result_id
                if issue.result_id is not None
                else issue.target_type == "dft_results" and str(issue.target_id) == str(result_id)
            )
            if issue.issue_type == "missing_dft_result" and missing_target_matches:
                if self.close_issue(
                    issue,
                    resolved_by=reviewer,
                    resolution_note=resolution_note,
                    resolution_code=DFT_RESOLUTION_CODES["verified"],
                ):
                    closed.append(issue)
                continue
            if issue.issue_type in self.VERIFY_CLOSE_TYPES:
                if self.close_issue(
                    issue,
                    resolved_by=reviewer,
                    resolution_note=resolution_note,
                    resolution_code=DFT_RESOLUTION_CODES["verified"],
                ):
                    closed.append(issue)
        return closed

    def apply_human_verify(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
    ) -> list[DFTAuditIssue]:
        return self.apply_verify(
            paper_id=paper_id,
            result_id=result_id,
            reviewer=reviewer,
            actor_type="human",
        )

    def apply_reject(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
        actor_type: str,
    ) -> list[DFTAuditIssue]:
        normalized_actor = str(actor_type or "").strip().lower()
        if normalized_actor not in {"human", "ai"}:
            raise ValueError("actor_type must be human or ai")
        resolution_note = "target_rejected" if normalized_actor == "human" else "ai_rejected"
        closed: list[DFTAuditIssue] = []
        for issue in self.active_issues_for_target(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=result_id,
        ):
            if issue.issue_type == "source_scope_error":
                continue
            if issue.issue_type in self.REJECT_CLOSE_TYPES:
                if self.close_issue(
                    issue,
                    resolved_by=reviewer,
                    resolution_note=resolution_note,
                    resolution_code=DFT_RESOLUTION_CODES["rejected"],
                ):
                    issue.lifecycle_stage = DFT_LIFECYCLE_STAGES["rejected"]
                    closed.append(issue)
        return closed

    def apply_human_reject(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        reviewer: str,
    ) -> list[DFTAuditIssue]:
        return self.apply_reject(
            paper_id=paper_id,
            result_id=result_id,
            reviewer=reviewer,
            actor_type="human",
        )

    def live_snapshot_for_issue(self, issue: DFTAuditIssue) -> dict[str, Any] | None:
        if issue.result_id is not None:
            row_id = issue.result_id
        else:
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
            "result_id": str(issue.result_id) if issue.result_id else None,
            "issue_type": issue.issue_type,
            "issue_key_version": issue.issue_key_version,
            "issue_key": issue.issue_key,
            "lifecycle_version": issue.lifecycle_version,
            "lifecycle_stage": issue.lifecycle_stage,
            "resolution_code": issue.resolution_code,
            "last_error_code": issue.last_error_code,
            "retry_count": issue.retry_count,
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
            "value_upper": row.value_upper,
            "value_kind": row.value_kind,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "candidate_status": row.candidate_status,
            "evidence_payload": row.evidence_payload,
            "identity_version": row.identity_version,
            "subject_key": row.subject_key,
            "observation_key": row.observation_key,
            "identity_payload": row.identity_payload,
        }

    @staticmethod
    def _text_or_none(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, dict):
            return {str(key): DFTAuditIssueLifecycleService._value_key(val) for key, val in sorted(value.items())}
        if isinstance(value, list):
            return [DFTAuditIssueLifecycleService._value_key(item) for item in value]
        return str(value or "").strip().lower()

    @staticmethod
    def is_terminal_issue(issue: DFTAuditIssue) -> bool:
        return str(issue.status or "").strip().lower() in DFT_AUDIT_ISSUE_TERMINAL_STATUSES
