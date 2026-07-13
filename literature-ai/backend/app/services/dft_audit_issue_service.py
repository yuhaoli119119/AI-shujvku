from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DFTAuditIssue, DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, utcnow
from app.services.dft_audit_issue_lifecycle_service import (
    DFT_AUDIT_ISSUE_PENDING_STATUSES,
    DFTAuditIssueLifecycleService,
)
from app.services.dft_rescan_policy import normalize_dft_reaction_step_for_identity, normalize_source_document_type
from app.services.dft_identity_service import DFTIdentityV2
from app.services.external_analysis_identity import (
    UNTRUSTED_LEGACY_SOURCE_IDENTITY,
    review_source_identity,
)


DFT_AUDIT_ISSUE_OPEN_STATUSES = set(DFT_AUDIT_ISSUE_PENDING_STATUSES)


class DFTAuditIssueService:
    ISSUE_TYPES = {
        "missing_dft_result",
        "wrong_value",
        "wrong_unit",
        "wrong_material",
        "wrong_adsorbate",
        "wrong_reaction_step",
        "wrong_property_type",
        "missing_evidence",
        "duplicate_suspected",
        "source_scope_error",
        "consensus_ready",
        "negative_consensus",
        "uncertain",
    }
    STATUSES = {
        "open",
        "needs_primary_ai",
        "needs_user_decision",
        "fixed_by_primary_ai",
        "false_positive",
        "closed",
    }
    SEVERITIES = {"low", "medium", "high", "critical"}
    DFT_FIELD_ISSUES = {
        "value": "wrong_value",
        "value_upper": "wrong_value",
        "value_kind": "wrong_value",
        "value_type": "wrong_value",
        "unit": "wrong_unit",
        "catalyst_sample_id": "wrong_material",
        "material": "wrong_material",
        "material_identity": "wrong_material",
        "adsorbate": "wrong_adsorbate",
        "reaction_step": "wrong_reaction_step",
        "property_type": "wrong_property_type",
        "energy_type": "wrong_property_type",
        "normalized_energy_type": "wrong_property_type",
    }

    def __init__(self, session: Session):
        self.session = session
        self._batch_issues: dict[tuple[str, str, str, str], DFTAuditIssue] | None = None

    def begin_import_batch(self, *, paper_id: UUID) -> None:
        self._batch_issues = {
            (str(issue.target_type), str(issue.target_id), str(issue.issue_type), str(issue.fingerprint)): issue
            for issue in self.session.scalars(
                select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper_id)
            ).all()
        }

    def upsert_issue(
        self,
        *,
        paper_id: UUID,
        issue_type: str,
        fingerprint: str,
        target_id: str | None = None,
        target_type: str = "dft_results",
        severity: str = "medium",
        status: str = "open",
        current_snapshot: dict[str, Any] | None = None,
        suggested_value: Any = None,
        suggested_dft: dict[str, Any] | None = None,
        evidence_payload: Any = None,
        source_identity: str | None = None,
        source_candidate_id: str | None = None,
        resolution_note: str | None = None,
        identity: DFTIdentityV2 | None = None,
        result_row: DFTResult | None = None,
    ) -> DFTAuditIssue:
        issue_type = self._checked(issue_type, self.ISSUE_TYPES, "issue_type")
        severity = self._checked(severity, self.SEVERITIES, "severity")
        status = self._checked(status, self.STATUSES, "status")
        target_type = str(target_type or "dft_results").strip() or "dft_results"
        target_id = str(target_id).strip() if target_id not in (None, "") else "new"
        fingerprint = str(fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("DFT audit issue fingerprint is required.")

        issue_key = (target_type, target_id, issue_type, fingerprint)
        existing = self._batch_issues.get(issue_key) if self._batch_issues is not None else None
        if self._batch_issues is None:
            existing = self.session.scalar(
                select(DFTAuditIssue).where(
                    DFTAuditIssue.paper_id == paper_id,
                    DFTAuditIssue.target_type == target_type,
                    DFTAuditIssue.target_id == target_id,
                    DFTAuditIssue.issue_type == issue_type,
                    DFTAuditIssue.fingerprint == fingerprint,
                )
            )
        existing_was_found = existing is not None
        if existing is None:
            existing = DFTAuditIssue(
                paper_id=paper_id,
                target_type=target_type,
                target_id=target_id,
                issue_type=issue_type,
                severity=severity,
                status=status,
                current_snapshot=current_snapshot,
                suggested_value=suggested_value,
                suggested_dft=suggested_dft,
                evidence_payload=self._json_payload(evidence_payload),
                source_identities=self._merged_list([], source_identity),
                source_candidate_ids=self._merged_list([], source_candidate_id),
                fingerprint=fingerprint,
                resolution_note=resolution_note,
            )
            if identity is not None:
                DFTAuditIssueLifecycleService(self.session).initialize_issue_identity(
                    existing,
                    identity=identity,
                    row=result_row,
                )
            if self._batch_issues is not None:
                self.session.add(existing)
                self.session.flush()
                self._batch_issues[issue_key] = existing
            else:
                try:
                    with self.session.begin_nested():
                        self.session.add(existing)
                        self.session.flush()
                except IntegrityError:
                    winner = self.session.scalar(
                        select(DFTAuditIssue).where(
                            DFTAuditIssue.paper_id == paper_id,
                            DFTAuditIssue.target_type == target_type,
                            DFTAuditIssue.target_id == target_id,
                            DFTAuditIssue.issue_type == issue_type,
                            DFTAuditIssue.fingerprint == fingerprint,
                        )
                    )
                    if winner is None:
                        raise
                    existing = winner
                    existing_was_found = True
        if existing_was_found and existing.status in {"closed", "false_positive"}:
            return existing
        changed = False
        merged_identities = self._merged_list(existing.source_identities or [], source_identity)
        if merged_identities != (existing.source_identities or []):
            existing.source_identities = merged_identities
            changed = True
        merged_candidate_ids = self._merged_list(existing.source_candidate_ids or [], source_candidate_id)
        if merged_candidate_ids != (existing.source_candidate_ids or []):
            existing.source_candidate_ids = merged_candidate_ids
            changed = True
        if existing.status in DFT_AUDIT_ISSUE_OPEN_STATUSES and status in {"needs_primary_ai", "needs_user_decision"}:
            if existing.status != status and existing.status == "open":
                existing.status = status
                changed = True
        if current_snapshot is not None and existing.current_snapshot != current_snapshot:
            existing.current_snapshot = current_snapshot
            changed = True
        if suggested_value is not None and existing.suggested_value != suggested_value:
            existing.suggested_value = suggested_value
            changed = True
        if suggested_dft is not None and existing.suggested_dft != suggested_dft:
            existing.suggested_dft = suggested_dft
            changed = True
        normalized_evidence = self._json_payload(evidence_payload)
        if normalized_evidence is not None and existing.evidence_payload != normalized_evidence:
            existing.evidence_payload = normalized_evidence
            changed = True
        if resolution_note and existing.resolution_note != resolution_note:
            existing.resolution_note = resolution_note
            changed = True
        if changed:
            existing.updated_at = utcnow()
            self.session.add(existing)
            self.session.flush()
        if identity is not None:
            DFTAuditIssueLifecycleService(self.session).initialize_issue_identity(
                existing,
                identity=identity,
                row=result_row,
                lifecycle_stage=existing.lifecycle_stage or "discovered",
                resolution_code=existing.resolution_code,
                last_error_code=existing.last_error_code,
            )
            self.session.flush()
        if source_candidate_id:
            try:
                candidate_uuid = UUID(str(source_candidate_id))
            except ValueError:
                candidate_uuid = None
            if candidate_uuid is not None:
                candidate = self.session.get(ExternalAnalysisCandidate, candidate_uuid)
                if candidate is not None:
                    DFTAuditIssueLifecycleService(self.session).bind_source_candidate(existing, candidate)
        return existing

    def create_or_update_consensus_issue(
        self,
        *,
        paper_id: UUID,
        row: DFTResult,
        field_name: str,
        opinion: dict[str, Any],
        source_identity: str | None = None,
        source_candidate_id: str | None = None,
        negative: bool = False,
        adjudicated_by_third_ai: bool = False,
    ) -> DFTAuditIssue:
        mapped_field = str(field_name or "value").strip() or "value"
        issue_type = "negative_consensus" if negative else self._issue_type_for_field(mapped_field, opinion)
        status = "needs_user_decision" if negative or adjudicated_by_third_ai else "needs_primary_ai"
        suggested = opinion.get("corrected_value", opinion.get("value"))
        evidence = opinion.get("evidence_payload") or opinion.get("evidence_location")
        fingerprint = self.fingerprint_existing_result_issue(
            paper_id=paper_id,
            row=row,
            issue_type=issue_type,
            field_name=mapped_field,
            suggested_value=suggested,
            evidence_payload=evidence,
        )
        identity = DFTAuditIssueLifecycleService(self.session).identity_for_result(row)
        return self.upsert_issue(
            paper_id=paper_id,
            target_id=str(row.id),
            issue_type=issue_type,
            status=status,
            severity="medium" if not negative else "high",
            current_snapshot=self.snapshot_dft_result(row),
            suggested_value=suggested,
            evidence_payload=evidence,
            source_identity=source_identity
            or review_source_identity(
                opinion.get("source_identity"),
                opinion.get("source_identity_verified"),
            ),
            source_candidate_id=source_candidate_id or str(opinion.get("candidate_id") or ""),
            fingerprint=fingerprint,
            resolution_note="DFT audit consensus recorded as an issue; underlying DFTResult was not verified, rejected, or edited.",
            identity=identity,
            result_row=row,
        )

    def create_or_update_missing_issue(
        self,
        *,
        paper_id: UUID,
        candidate: ExternalAnalysisCandidate,
        run: ExternalAnalysisRun,
        payload: dict[str, Any],
    ) -> DFTAuditIssue:
        corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
        evidence = payload.get("evidence_location") or payload.get("evidence_payload") or candidate.evidence_payload
        is_supporting_reference = self._is_supporting_reference_payload(payload, corrected, evidence)
        ml_predicted = payload.get("ml_predicted", corrected.get("ml_predicted"))
        is_ml_predicted = ml_predicted is True or str(ml_predicted or "").strip().lower() in {"1", "true", "yes"}
        issue_type = "source_scope_error" if is_supporting_reference or is_ml_predicted else "missing_dft_result"
        status = "closed" if is_supporting_reference else "needs_user_decision" if is_ml_predicted else "needs_primary_ai"
        suggested_dft = self._suggested_dft_from_payload(payload)
        fingerprint = self.fingerprint_missing_issue(
            paper_id=paper_id,
            payload=payload,
            issue_type=issue_type,
            candidate_id=str(candidate.id),
        )
        existing = self._missing_issue_by_fingerprint(
            paper_id=paper_id,
            issue_type=issue_type,
            fingerprint=fingerprint,
        )
        if existing is not None and self._batch_issues is not None:
            self._batch_issues[
                (str(existing.target_type), str(existing.target_id), str(existing.issue_type), str(existing.fingerprint))
            ] = existing
        identity = DFTAuditIssueLifecycleService.build_identity(
            paper_id=paper_id,
            payload=payload,
        )
        return self.upsert_issue(
            paper_id=paper_id,
            target_id=existing.target_id if existing is not None else "new",
            issue_type=issue_type,
            status=status,
            severity="low" if is_supporting_reference else "medium" if is_ml_predicted else "high",
            suggested_dft=suggested_dft,
            evidence_payload=evidence,
            source_identity=review_source_identity(
                run.source_identity,
                run.source_identity_verified,
                default_untrusted=UNTRUSTED_LEGACY_SOURCE_IDENTITY,
            ),
            source_candidate_id=str(candidate.id),
            fingerprint=fingerprint,
            resolution_note=None if existing is not None and existing.status in {"closed", "false_positive"} else (
                "Supporting-reference DFT finding is tracked as source_scope_error, not as a main-paper missing result."
                if is_supporting_reference
                else "ML-predicted value is outside the DFTResult lane and requires user-controlled prediction-data review."
                if is_ml_predicted
                else "Missing DFT result draft queued for authorized AI or user-controlled follow-up."
            ),
            identity=identity,
        )

    def close_issue(self, issue_id: UUID, *, status: str, resolved_by: str, resolution_note: str | None = None) -> DFTAuditIssue:
        status = self._checked(status, {"fixed_by_primary_ai", "false_positive", "closed"}, "status")
        issue = self.session.get(DFTAuditIssue, issue_id)
        if issue is None:
            raise LookupError("DFT audit issue not found.")
        if status == "fixed_by_primary_ai":
            DFTAuditIssueLifecycleService(self.session).mark_pending(
                issue,
                status=status,
                note=resolution_note,
            )
        else:
            DFTAuditIssueLifecycleService(self.session).close_issue(
                issue,
                status=status,
                resolved_by=resolved_by,
                resolution_note=resolution_note or status,
            )
        return issue

    def list_issues(
        self,
        *,
        paper_id: UUID | None = None,
        statuses: set[str] | None = None,
        limit: int = 200,
    ) -> list[DFTAuditIssue]:
        unknown_statuses = set(statuses or []) - self.STATUSES
        if unknown_statuses:
            raise ValueError(f"Unsupported DFT audit issue status: {', '.join(sorted(unknown_statuses))}")
        stmt = select(DFTAuditIssue).order_by(DFTAuditIssue.created_at.desc(), DFTAuditIssue.id.desc())
        if paper_id is not None:
            stmt = stmt.where(DFTAuditIssue.paper_id == paper_id)
        if statuses:
            stmt = stmt.where(DFTAuditIssue.status.in_(sorted(statuses)))
        return list(self.session.scalars(stmt.limit(max(1, min(limit, 1000)))).all())

    def serialize_issue(self, issue: DFTAuditIssue) -> dict[str, Any]:
        return DFTAuditIssueLifecycleService(self.session).serialize_issue(issue)

    @classmethod
    def snapshot_dft_result(cls, row: DFTResult) -> dict[str, Any]:
        return DFTAuditIssueLifecycleService.snapshot_dft_result(row)

    def fingerprint_existing_result_issue(
        self,
        *,
        paper_id: UUID,
        row: DFTResult,
        issue_type: str,
        field_name: str,
        suggested_value: Any,
        evidence_payload: Any,
    ) -> str:
        evidence = evidence_payload if isinstance(evidence_payload, dict) else {}
        return self._hash_parts(
            [
                "dft_audit_issue_v1",
                str(paper_id),
                "dft_results",
                str(row.id),
                issue_type,
                "" if issue_type == "consensus_ready" else self._normalized_part(field_name),
                self._value_key(suggested_value),
                self._evidence_anchor(evidence),
            ]
        )

    def fingerprint_missing_issue(
        self,
        *,
        paper_id: UUID,
        payload: dict[str, Any],
        issue_type: str = "missing_dft_result",
        candidate_id: str | None = None,
    ) -> str:
        identity = DFTAuditIssueLifecycleService.build_identity(paper_id=paper_id, payload=payload)
        return self._hash_parts(
            [
                "dft_missing_issue_identity_v2",
                str(paper_id),
                issue_type,
                identity.subject_key,
                identity.observation_key,
                list(identity.error_codes),
                str(candidate_id or "") if not identity.observation_key else "",
            ]
        )

    def _missing_issue_by_fingerprint(
        self,
        *,
        paper_id: UUID,
        issue_type: str,
        fingerprint: str,
    ) -> DFTAuditIssue | None:
        if self._batch_issues is not None:
            match = next(
                (
                    issue
                    for issue in self._batch_issues.values()
                    if issue.paper_id == paper_id
                    and issue.issue_type == issue_type
                    and issue.fingerprint == fingerprint
                ),
                None,
            )
            if match is not None:
                return match
        return self.session.scalar(
            select(DFTAuditIssue)
            .where(DFTAuditIssue.paper_id == paper_id)
            .where(DFTAuditIssue.issue_type == issue_type)
            .where(DFTAuditIssue.fingerprint == fingerprint)
            .order_by(DFTAuditIssue.created_at.asc())
        )

    def _issue_type_for_field(self, field_name: str, opinion: dict[str, Any]) -> str:
        decision = str(opinion.get("decision") or "").strip().upper()
        if decision == "PASS":
            return "consensus_ready"
        return self.DFT_FIELD_ISSUES.get(str(field_name or "").strip(), "uncertain")

    def _suggested_dft_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        evidence = evidence if isinstance(evidence, dict) else {}
        suggested = {
            "material_identity": self._first_text(
                corrected.get("material_identity"),
                corrected.get("material"),
                corrected.get("catalyst"),
                payload.get("normalized_material"),
                payload.get("normalized_material_or_catalyst"),
            ),
            "property_type": self._first_text(
                corrected.get("property_type"),
                corrected.get("property"),
                corrected.get("energy_type"),
                payload.get("normalized_energy_type"),
            ),
            "adsorbate": self._first_text(corrected.get("adsorbate"), payload.get("adsorbate")),
            "reaction_step": self._first_text(corrected.get("reaction_step"), payload.get("reaction_step")),
            "value": corrected.get("value"),
            "value_upper": corrected.get("value_upper"),
            "value_kind": self._first_text(corrected.get("value_kind"), corrected.get("value_type")),
            "unit": self._first_text(corrected.get("unit")),
            "raw_corrected_value": corrected,
        }
        for alias in ("atom_pair", "bond_pair", "bond", "interaction_pair"):
            value = self._first_text(corrected.get(alias), payload.get(alias), evidence.get(alias))
            if value:
                suggested[alias] = value
        return suggested

    @staticmethod
    def _is_supporting_reference_payload(payload: dict[str, Any], corrected: dict[str, Any], evidence: Any) -> bool:
        evidence_dict = evidence if isinstance(evidence, dict) else {}
        source_type = normalize_source_document_type(
            payload.get("source_document_type")
            or payload.get("source_type")
            or evidence_dict.get("source_document_type")
            or evidence_dict.get("source_type")
            or corrected.get("source_document_type")
            or corrected.get("source_type")
        )
        return bool(payload.get("borrowed_from_reference")) or source_type == "supporting_reference"

    @staticmethod
    def _evidence_anchor(evidence: dict[str, Any]) -> dict[str, str]:
        return {
            "page": str(evidence.get("page") or "").strip().lower(),
            "table": str(evidence.get("table") or "").strip().lower(),
            "figure": str(evidence.get("figure") or "").strip().lower(),
            "quoted_text": str(evidence.get("quoted_text") or evidence.get("evidence_text") or "").strip().lower(),
            "source_document_type": normalize_source_document_type(evidence.get("source_document_type") or evidence.get("source_type")),
        }

    @staticmethod
    def _hash_parts(parts: list[Any]) -> str:
        canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return f"{value:.8g}"
        if isinstance(value, dict):
            return {str(key): DFTAuditIssueService._value_key(val) for key, val in sorted(value.items())}
        if isinstance(value, list):
            return [DFTAuditIssueService._value_key(item) for item in value]
        return str(value or "").strip().lower()

    @staticmethod
    def _json_payload(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if value in (None, ""):
            return None
        return {"value": value}

    @staticmethod
    def _merged_list(existing: list[Any], value: Any) -> list[str]:
        merged = [str(item).strip() for item in existing if str(item).strip()]
        text = str(value or "").strip()
        if text and text not in merged:
            merged.append(text)
        return merged

    @staticmethod
    def _checked(value: str, allowed: set[str], field_name: str) -> str:
        text = str(value or "").strip()
        if text not in allowed:
            raise ValueError(f"Unsupported DFT audit issue {field_name}: {text}")
        return text

    @staticmethod
    def _normalized_part(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).strip().lower()
        return str(value or "").strip().lower()

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None
