from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DFTAuditIssue, DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, utcnow
from app.services.dft_rescan_policy import normalize_dft_reaction_step_for_identity, normalize_source_document_type


DFT_AUDIT_ISSUE_OPEN_STATUSES = {"open", "needs_primary_ai", "needs_user_decision"}


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
    ) -> DFTAuditIssue:
        issue_type = self._checked(issue_type, self.ISSUE_TYPES, "issue_type")
        severity = self._checked(severity, self.SEVERITIES, "severity")
        status = self._checked(status, self.STATUSES, "status")
        target_type = str(target_type or "dft_results").strip() or "dft_results"
        target_id = str(target_id).strip() if target_id not in (None, "") else "new"
        fingerprint = str(fingerprint or "").strip()
        if not fingerprint:
            raise ValueError("DFT audit issue fingerprint is required.")

        existing = self.session.scalar(
            select(DFTAuditIssue).where(
                DFTAuditIssue.paper_id == paper_id,
                DFTAuditIssue.target_type == target_type,
                DFTAuditIssue.target_id == target_id,
                DFTAuditIssue.issue_type == issue_type,
                DFTAuditIssue.fingerprint == fingerprint,
            )
        )
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
        return self.upsert_issue(
            paper_id=paper_id,
            target_id=str(row.id),
            issue_type=issue_type,
            status=status,
            severity="medium" if not negative else "high",
            current_snapshot=self.snapshot_dft_result(row),
            suggested_value=suggested,
            evidence_payload=evidence,
            source_identity=source_identity or str(opinion.get("source_identity") or opinion.get("source_label") or opinion.get("source") or ""),
            source_candidate_id=source_candidate_id or str(opinion.get("candidate_id") or ""),
            fingerprint=fingerprint,
            resolution_note="DFT audit consensus recorded as an issue; underlying DFTResult was not verified, rejected, or edited.",
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
        issue_type = "source_scope_error" if is_supporting_reference else "missing_dft_result"
        status = "closed" if is_supporting_reference else "needs_primary_ai"
        suggested_dft = self._suggested_dft_from_payload(payload)
        fingerprint = self.fingerprint_missing_issue(
            paper_id=paper_id,
            payload=payload,
            issue_type=issue_type,
        )
        return self.upsert_issue(
            paper_id=paper_id,
            target_id="new",
            issue_type=issue_type,
            status=status,
            severity="low" if is_supporting_reference else "high",
            suggested_dft=suggested_dft,
            evidence_payload=evidence,
            source_identity=str(payload.get("source_label") or run.source_label or payload.get("source") or run.source or ""),
            source_candidate_id=str(candidate.id),
            fingerprint=fingerprint,
            resolution_note=(
                "Supporting-reference DFT finding is tracked as source_scope_error, not as a main-paper missing result."
                if is_supporting_reference
                else "Missing DFT result draft queued for primary AI or user-controlled follow-up."
            ),
        )

    def close_issue(self, issue_id: UUID, *, status: str, resolved_by: str, resolution_note: str | None = None) -> DFTAuditIssue:
        status = self._checked(status, {"fixed_by_primary_ai", "false_positive", "closed"}, "status")
        issue = self.session.get(DFTAuditIssue, issue_id)
        if issue is None:
            raise LookupError("DFT audit issue not found.")
        issue.status = status
        issue.resolved_by = resolved_by
        issue.resolved_at = utcnow()
        issue.resolution_note = resolution_note
        issue.updated_at = utcnow()
        self.session.add(issue)
        self.session.flush()
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

    @classmethod
    def serialize_issue(cls, issue: DFTAuditIssue) -> dict[str, Any]:
        return {
            "id": str(issue.id),
            "paper_id": str(issue.paper_id),
            "target_type": issue.target_type,
            "target_id": issue.target_id,
            "issue_type": issue.issue_type,
            "severity": issue.severity,
            "status": issue.status,
            "current_snapshot": issue.current_snapshot,
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

    @classmethod
    def snapshot_dft_result(cls, row: DFTResult) -> dict[str, Any]:
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
    ) -> str:
        suggested = self._suggested_dft_from_payload(payload)
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        evidence_dict = evidence if isinstance(evidence, dict) else {}
        source_bucket = normalize_source_document_type(
            payload.get("source_document_type")
            or payload.get("source_type")
            or evidence_dict.get("source_document_type")
            or evidence_dict.get("source_type")
        )
        reaction_step = normalize_dft_reaction_step_for_identity(
            suggested.get("reaction_step"),
            property_type=suggested.get("property_type"),
            adsorbate=suggested.get("adsorbate"),
            material=suggested.get("material_identity"),
        )
        return self._hash_parts(
            [
                "dft_missing_issue_v1",
                str(paper_id),
                issue_type,
                source_bucket,
                self._normalized_part(suggested.get("material_identity")),
                self._normalized_part(suggested.get("property_type")),
                self._normalized_part(suggested.get("adsorbate")),
                self._value_key(suggested.get("value")),
                self._normalized_part(suggested.get("unit")),
                self._normalized_part(reaction_step),
                self._evidence_anchor(evidence_dict),
            ]
        )

    def _issue_type_for_field(self, field_name: str, opinion: dict[str, Any]) -> str:
        decision = str(opinion.get("decision") or "").strip().upper()
        if decision == "PASS":
            return "consensus_ready"
        return self.DFT_FIELD_ISSUES.get(str(field_name or "").strip(), "uncertain")

    def _suggested_dft_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
        return {
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
            "unit": self._first_text(corrected.get("unit")),
            "raw_corrected_value": corrected,
        }

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
