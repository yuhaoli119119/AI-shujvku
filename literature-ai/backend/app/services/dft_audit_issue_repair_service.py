from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTAuditIssue, DFTResult, utcnow
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_rescan_policy import build_dft_dedupe_signature, normalize_source_document_type
from app.utils.evidence_anchors import has_evidence_anchor


AI_PRIMARY_APPLIED_STATUS = "ai_primary_applied"
FINAL_DFT_STATUSES = {"ml_ready", "human_verified", "verified", "final_user_submitted"}


class DFTAuditIssueRepairService:
    ACTIONS = {
        "create_missing_dft",
        "update_dft_fields",
        "link_existing_duplicate",
        "mark_needs_user_decision",
        "mark_false_positive",
    }
    AUTO_REPAIR_BLOCKED_ISSUE_TYPES = {
        "source_scope_error",
        "negative_consensus",
        "uncertain",
    }
    UPDATE_ISSUE_TYPES = {
        "wrong_value",
        "wrong_unit",
        "wrong_adsorbate",
        "wrong_reaction_step",
        "wrong_property_type",
        "missing_evidence",
        "wrong_material",
    }
    UPDATE_FIELDS = {
        "value",
        "unit",
        "property_type",
        "adsorbate",
        "reaction_step",
        "evidence_text",
        "evidence_payload",
        "catalyst_sample_id",
        "material_identity",
    }

    def __init__(self, session: Session):
        self.session = session

    def repair_issue(
        self,
        *,
        issue_id: UUID,
        action: str,
        repair_payload: dict[str, Any] | None,
        reason: str | None,
        evidence_payload: dict[str, Any] | list[Any] | None,
        repaired_by: str,
    ) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in self.ACTIONS:
            raise ValueError(f"Unsupported DFT audit issue repair action: {action}")
        issue = self.session.get(DFTAuditIssue, issue_id)
        if issue is None:
            raise LookupError("DFT audit issue not found.")
        payload = repair_payload if isinstance(repair_payload, dict) else {}
        result = self._dispatch(
            issue=issue,
            action=action,
            repair_payload=payload,
            reason=str(reason or "").strip(),
            evidence_payload=evidence_payload,
            repaired_by=repaired_by,
        )
        self._audit_repair(
            issue=issue,
            action=action,
            result=result,
            repaired_by=repaired_by,
        )
        self.session.flush()
        return result

    def _dispatch(
        self,
        *,
        issue: DFTAuditIssue,
        action: str,
        repair_payload: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        repaired_by: str,
    ) -> dict[str, Any]:
        if action == "mark_needs_user_decision":
            self._require_reason(reason)
            return self._mark_issue(
                issue,
                status="needs_user_decision",
                repaired_by=repaired_by,
                note=reason,
                action_result="needs_user_decision",
            )
        if action == "mark_false_positive":
            self._require_reason(reason)
            return self._mark_issue(
                issue,
                status="false_positive",
                repaired_by=repaired_by,
                note=reason,
                action_result="false_positive",
            )
        if issue.issue_type in self.AUTO_REPAIR_BLOCKED_ISSUE_TYPES:
            self._mark_issue(
                issue,
                status="needs_user_decision",
                repaired_by=repaired_by,
                note=reason or f"{issue.issue_type} requires user decision before primary-AI repair.",
                action_result="needs_user_decision",
            )
            return {
                "status": "needs_user_decision",
                "reason": "auto_repair_not_allowed_for_issue_type",
                "issue_id": str(issue.id),
                "issue_type": issue.issue_type,
                "writes_final_truth": False,
            }
        if action == "create_missing_dft":
            return self._create_missing_dft(
                issue=issue,
                repair_payload=repair_payload,
                reason=reason,
                evidence_payload=evidence_payload,
                repaired_by=repaired_by,
            )
        if action == "update_dft_fields":
            return self._update_dft_fields(
                issue=issue,
                repair_payload=repair_payload,
                reason=reason,
                evidence_payload=evidence_payload,
                repaired_by=repaired_by,
            )
        if action == "link_existing_duplicate":
            return self._link_existing_duplicate(
                issue=issue,
                repair_payload=repair_payload,
                reason=reason,
                repaired_by=repaired_by,
            )
        raise ValueError(f"Unsupported DFT audit issue repair action: {action}")

    def _create_missing_dft(
        self,
        *,
        issue: DFTAuditIssue,
        repair_payload: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        repaired_by: str,
    ) -> dict[str, Any]:
        if issue.issue_type != "missing_dft_result":
            raise ValueError("create_missing_dft is only allowed for missing_dft_result issues.")
        suggested = self._merged_suggested_dft(issue, repair_payload)
        evidence = self._merged_evidence(issue, evidence_payload)
        if self._is_supporting_reference(evidence, suggested):
            self._mark_issue(
                issue,
                status="needs_user_decision",
                repaired_by=repaired_by,
                note=reason or "supporting_reference DFT data cannot be written as main-paper DFTResult.",
                action_result="needs_user_decision",
            )
            return {
                "status": "needs_user_decision",
                "reason": "supporting_reference_not_main_paper_data",
                "issue_id": str(issue.id),
                "writes_final_truth": False,
            }
        self._validate_missing_suggestion(suggested, evidence)
        existing = self._find_equivalent_dft_result(
            paper_id=issue.paper_id,
            suggested=suggested,
            evidence=evidence,
        )
        if existing is not None:
            self._mark_issue(
                issue,
                status="fixed_by_primary_ai",
                repaired_by=repaired_by,
                note=f"linked_existing_dft_result:{existing.id}; {reason}".strip("; "),
                action_result="linked_existing",
            )
            return {
                "status": "linked_existing",
                "issue_id": str(issue.id),
                "dft_result_id": str(existing.id),
                "changed_fields": [],
                "writes_final_truth": False,
            }
        sample = self._get_or_create_catalyst_sample(issue.paper_id, suggested["material_identity"])
        row = DFTResult(
            paper_id=issue.paper_id,
            catalyst_sample_id=sample.id,
            adsorbate=self._first_text(suggested.get("adsorbate")),
            property_type=self._first_text(suggested.get("property_type")),
            value=self._float_or_raise(suggested.get("value"), "value"),
            unit=self._first_text(suggested.get("unit")),
            reaction_step=self._first_text(suggested.get("reaction_step")),
            source_section=self._source_section_from_evidence(evidence),
            source_figure=self._first_text(evidence.get("figure"), evidence.get("table")),
            evidence_text=self._first_text(evidence.get("quoted_text"), evidence.get("evidence_text"), reason),
            candidate_status=AI_PRIMARY_APPLIED_STATUS,
            evidence_payload=self._repair_evidence_payload(
                issue=issue,
                evidence=evidence,
                suggested=suggested,
            ),
            extraction_protocol_version="dft_audit_issue_primary_repair_v1",
            candidate_identity=self._candidate_identity_for_issue(issue, suggested, evidence),
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            winner = self.session.scalar(
                select(DFTResult).where(
                    DFTResult.paper_id == issue.paper_id,
                    DFTResult.candidate_identity == row.candidate_identity,
                )
            )
            if winner is None:
                raise
            row = winner
        self._mark_issue(
            issue,
            status="fixed_by_primary_ai",
            repaired_by=repaired_by,
            note=f"created_dft_result:{row.id}; {reason}".strip("; "),
            action_result="created",
        )
        return {
            "status": "created",
            "issue_id": str(issue.id),
            "dft_result_id": str(row.id),
            "catalyst_sample_id": str(sample.id),
            "changed_fields": ["dft_results"],
            "writes_final_truth": False,
        }

    def _update_dft_fields(
        self,
        *,
        issue: DFTAuditIssue,
        repair_payload: dict[str, Any],
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        repaired_by: str,
    ) -> dict[str, Any]:
        if issue.issue_type not in self.UPDATE_ISSUE_TYPES:
            raise ValueError(f"update_dft_fields is not allowed for issue_type={issue.issue_type}.")
        target_id = str(issue.target_id or "").strip()
        if not target_id or target_id.lower() == "new":
            raise ValueError("update_dft_fields requires an existing DFTResult target_id.")
        row = self.session.get(DFTResult, UUID(target_id))
        if row is None or row.paper_id != issue.paper_id:
            raise LookupError("Target DFT result not found for issue.")
        if str(row.candidate_status or "").strip().lower() in FINAL_DFT_STATUSES:
            return self._needs_user_for_final_row(issue=issue, repaired_by=repaired_by, reason=reason, row=row)
        fields = self._repair_fields(repair_payload)
        unknown_fields = set(fields) - self.UPDATE_FIELDS
        if unknown_fields:
            raise ValueError(f"Unsupported DFT repair field(s): {', '.join(sorted(unknown_fields))}")
        if "candidate_status" in repair_payload or "candidate_status" in fields:
            raise ValueError("DFT audit issue repair cannot set candidate_status directly.")
        if not fields:
            raise ValueError("update_dft_fields requires at least one whitelisted field.")
        if self._fields_already_match(row, fields):
            self._mark_issue(
                issue,
                status="fixed_by_primary_ai",
                repaired_by=repaired_by,
                note=f"idempotent_update:{row.id}; {reason}".strip("; "),
                action_result="idempotent",
            )
            return {
                "status": "idempotent",
                "issue_id": str(issue.id),
                "dft_result_id": str(row.id),
                "changed_fields": [],
                "writes_final_truth": False,
            }
        stale = self._stale_fields(issue, row)
        if stale:
            self._mark_issue(
                issue,
                status="needs_user_decision",
                repaired_by=repaired_by,
                note=f"stale_issue:{','.join(stale)}; {reason}".strip("; "),
                action_result="stale_issue",
            )
            return {
                "status": "stale_issue",
                "issue_id": str(issue.id),
                "dft_result_id": str(row.id),
                "stale_fields": stale,
                "changed_fields": [],
                "writes_final_truth": False,
            }
        evidence = self._merged_evidence(issue, evidence_payload)
        if "evidence_payload" in fields:
            evidence = self._json_payload(fields["evidence_payload"])
        if not has_evidence_anchor(evidence):
            raise ValueError("update_dft_fields requires structured evidence with page/table/figure/quoted_text/evidence_text.")
        changed_fields: list[str] = []
        for field, value in fields.items():
            if field == "material_identity":
                sample = self._get_or_create_catalyst_sample(issue.paper_id, value)
                if row.catalyst_sample_id != sample.id:
                    row.catalyst_sample_id = sample.id
                    changed_fields.append("catalyst_sample_id")
                continue
            if field == "catalyst_sample_id":
                sample = self.session.get(CatalystSample, UUID(str(value)))
                if sample is None or sample.paper_id != issue.paper_id:
                    raise LookupError("Catalyst sample not found for this paper.")
                if row.catalyst_sample_id != sample.id:
                    row.catalyst_sample_id = sample.id
                    changed_fields.append(field)
                continue
            if field == "value":
                value = self._float_or_raise(value, "value")
            if field == "evidence_payload":
                value = self._repair_evidence_payload(issue=issue, evidence=evidence, suggested={})
            current = getattr(row, field)
            if self._value_key(current) != self._value_key(value):
                setattr(row, field, value)
                changed_fields.append(field)
        if "evidence_payload" not in fields:
            row.evidence_payload = self._repair_evidence_payload(
                issue=issue,
                evidence=evidence,
                suggested={},
                base=row.evidence_payload if isinstance(row.evidence_payload, dict) else {},
            )
            if "evidence_payload" not in changed_fields:
                changed_fields.append("evidence_payload")
        row.candidate_status = AI_PRIMARY_APPLIED_STATUS
        self.session.add(row)
        self._mark_issue(
            issue,
            status="fixed_by_primary_ai",
            repaired_by=repaired_by,
            note=f"updated_dft_result:{row.id}; changed_fields={','.join(changed_fields)}; {reason}".strip("; "),
            action_result="updated",
        )
        self.session.flush()
        issue.current_snapshot = DFTAuditIssueService.snapshot_dft_result(row)
        self.session.add(issue)
        return {
            "status": "updated",
            "issue_id": str(issue.id),
            "dft_result_id": str(row.id),
            "changed_fields": changed_fields,
            "candidate_status": row.candidate_status,
            "writes_final_truth": False,
        }

    def _link_existing_duplicate(
        self,
        *,
        issue: DFTAuditIssue,
        repair_payload: dict[str, Any],
        reason: str,
        repaired_by: str,
    ) -> dict[str, Any]:
        target = repair_payload.get("dft_result_id") or repair_payload.get("target_id")
        if not target:
            raise ValueError("link_existing_duplicate requires repair_payload.dft_result_id.")
        row = self.session.get(DFTResult, UUID(str(target)))
        if row is None or row.paper_id != issue.paper_id:
            raise LookupError("Linked DFT result not found for this paper.")
        self._mark_issue(
            issue,
            status="fixed_by_primary_ai",
            repaired_by=repaired_by,
            note=f"linked_duplicate_dft_result:{row.id}; {reason}".strip("; "),
            action_result="linked_duplicate",
        )
        return {
            "status": "linked_duplicate",
            "issue_id": str(issue.id),
            "dft_result_id": str(row.id),
            "changed_fields": [],
            "writes_final_truth": False,
        }

    def _needs_user_for_final_row(
        self,
        *,
        issue: DFTAuditIssue,
        repaired_by: str,
        reason: str,
        row: DFTResult,
    ) -> dict[str, Any]:
        self._mark_issue(
            issue,
            status="needs_user_decision",
            repaired_by=repaired_by,
            note=reason or f"target DFTResult {row.id} already has final status {row.candidate_status}.",
            action_result="needs_user_decision",
        )
        return {
            "status": "needs_user_decision",
            "reason": "target_dft_result_has_final_status",
            "issue_id": str(issue.id),
            "dft_result_id": str(row.id),
            "changed_fields": [],
            "writes_final_truth": False,
        }

    def _mark_issue(
        self,
        issue: DFTAuditIssue,
        *,
        status: str,
        repaired_by: str,
        note: str,
        action_result: str,
    ) -> dict[str, Any]:
        issue.status = status
        issue.resolved_by = repaired_by
        issue.resolved_at = utcnow()
        issue.resolution_note = note
        issue.updated_at = utcnow()
        self.session.add(issue)
        return {
            "status": action_result,
            "issue_id": str(issue.id),
            "writes_final_truth": False,
        }

    def _audit_repair(
        self,
        *,
        issue: DFTAuditIssue,
        action: str,
        result: dict[str, Any],
        repaired_by: str,
    ) -> None:
        self.session.add(
            AuditLog(
                paper_id=issue.paper_id,
                action="repair_dft_audit_issue",
                source=repaired_by,
                target_type="dft_audit_issues",
                target_id=str(issue.id),
                payload={
                    "action": action,
                    "result": result.get("status"),
                    "dft_result_id": result.get("dft_result_id"),
                    "changed_fields": result.get("changed_fields") or [],
                    "writes_final_truth": False,
                },
            )
        )

    def _merged_suggested_dft(self, issue: DFTAuditIssue, repair_payload: dict[str, Any]) -> dict[str, Any]:
        suggested = dict(issue.suggested_dft or {})
        payload_suggested = repair_payload.get("suggested_dft")
        if isinstance(payload_suggested, dict):
            suggested.update({key: value for key, value in payload_suggested.items() if value not in (None, "")})
        allowed_direct = {"material_identity", "property_type", "adsorbate", "reaction_step", "value", "unit"}
        suggested.update({key: repair_payload[key] for key in allowed_direct if repair_payload.get(key) not in (None, "")})
        return suggested

    def _merged_evidence(
        self,
        issue: DFTAuditIssue,
        evidence_payload: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any]:
        base = issue.evidence_payload if isinstance(issue.evidence_payload, dict) else {}
        extra = evidence_payload if isinstance(evidence_payload, dict) else {}
        merged = {**base, **extra}
        if "source_document_type" not in merged:
            merged["source_document_type"] = normalize_source_document_type(merged.get("source_type"))
        else:
            merged["source_document_type"] = normalize_source_document_type(merged.get("source_document_type"))
        return merged

    def _repair_evidence_payload(
        self,
        *,
        issue: DFTAuditIssue,
        evidence: dict[str, Any],
        suggested: dict[str, Any],
        base: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(base or {})
        merged.update(evidence)
        merged.update(
            {
                "issue_id": str(issue.id),
                "source_candidate_ids": list(issue.source_candidate_ids or []),
                "material_identity": suggested.get("material_identity") or merged.get("material_identity"),
                "repair_policy": "dft_audit_issue_primary_repair_v1",
            }
        )
        merged["source_document_type"] = normalize_source_document_type(
            merged.get("source_document_type") or merged.get("source_type")
        )
        return {key: value for key, value in merged.items() if value not in (None, "")}

    def _validate_missing_suggestion(self, suggested: dict[str, Any], evidence: dict[str, Any]) -> None:
        for field in ("material_identity", "property_type", "value", "unit"):
            if suggested.get(field) in (None, "", []):
                raise ValueError(f"create_missing_dft requires suggested_dft.{field}.")
        if suggested.get("adsorbate") in (None, "", []) and suggested.get("reaction_step") in (None, "", []):
            raise ValueError("create_missing_dft requires at least adsorbate or reaction_step.")
        if not has_evidence_anchor(evidence):
            raise ValueError("create_missing_dft requires evidence with page/table/figure/quoted_text/evidence_text.")

    def _find_equivalent_dft_result(
        self,
        *,
        paper_id: UUID,
        suggested: dict[str, Any],
        evidence: dict[str, Any],
    ) -> DFTResult | None:
        desired_signature = self._dedupe_signature_for_suggested(paper_id=paper_id, suggested=suggested, evidence=evidence)
        for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all():
            if self._dedupe_signature_for_row(row) == desired_signature:
                return row
        return None

    def _dedupe_signature_for_suggested(self, *, paper_id: UUID, suggested: dict[str, Any], evidence: dict[str, Any]) -> str:
        return build_dft_dedupe_signature(
            {
                "paper_id": str(paper_id),
                "corrected_value": {
                    "material": suggested.get("material_identity"),
                    "adsorbate": suggested.get("adsorbate"),
                    "property_type": suggested.get("property_type"),
                    "reaction_step": suggested.get("reaction_step"),
                    "value": suggested.get("value"),
                    "unit": suggested.get("unit"),
                },
                "evidence_payload": evidence,
            }
        )

    def _dedupe_signature_for_row(self, row: DFTResult) -> str:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        material_identity = self._material_identity_for_row(row)
        return build_dft_dedupe_signature(
            {
                "paper_id": str(row.paper_id),
                "corrected_value": {
                    "material": material_identity,
                    "adsorbate": row.adsorbate,
                    "property_type": row.property_type,
                    "reaction_step": row.reaction_step,
                    "value": row.value,
                    "unit": row.unit,
                },
                "evidence_payload": evidence,
            }
        )

    def _material_identity_for_row(self, row: DFTResult) -> str | None:
        if row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            if sample is not None and str(sample.name or "").strip():
                return str(sample.name).strip()
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        return self._first_text(evidence.get("material_identity"), evidence.get("material"), evidence.get("catalyst"))

    def _get_or_create_catalyst_sample(self, paper_id: UUID, material_identity: Any) -> CatalystSample:
        name = self._first_text(material_identity)
        if not name:
            raise ValueError("material_identity is required.")
        rows = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
        for row in rows:
            if str(row.name or "").strip().lower() == name.lower():
                return row
        sample = CatalystSample(paper_id=paper_id, name=name, catalyst_type="unknown")
        self.session.add(sample)
        self.session.flush()
        return sample

    def _candidate_identity_for_issue(self, issue: DFTAuditIssue, suggested: dict[str, Any], evidence: dict[str, Any]) -> str:
        signature = self._dedupe_signature_for_suggested(paper_id=issue.paper_id, suggested=suggested, evidence=evidence)
        return hashlib.sha256(f"primary_ai_repair:{signature}".encode("utf-8")).hexdigest()

    def _stale_fields(self, issue: DFTAuditIssue, row: DFTResult) -> list[str]:
        snapshot = issue.current_snapshot if isinstance(issue.current_snapshot, dict) else {}
        stale: list[str] = []
        for field in ("catalyst_sample_id", "adsorbate", "property_type", "value", "unit", "reaction_step", "evidence_payload"):
            if field not in snapshot:
                continue
            current = str(row.catalyst_sample_id) if field == "catalyst_sample_id" and row.catalyst_sample_id else getattr(row, field)
            if self._value_key(current) != self._value_key(snapshot.get(field)):
                stale.append(field)
        return stale

    def _fields_already_match(self, row: DFTResult, fields: dict[str, Any]) -> bool:
        comparable = {key: value for key, value in fields.items() if key in {"value", "unit", "property_type", "adsorbate", "reaction_step", "evidence_text"}}
        if not comparable:
            return False
        return all(self._value_key(getattr(row, key)) == self._value_key(value) for key, value in comparable.items())

    @staticmethod
    def _repair_fields(repair_payload: dict[str, Any]) -> dict[str, Any]:
        fields = repair_payload.get("fields") if isinstance(repair_payload.get("fields"), dict) else repair_payload
        return {str(key): value for key, value in dict(fields or {}).items() if value is not None}

    @staticmethod
    def _json_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"items": value}
        return {"value": value}

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, dict):
            return {key: DFTAuditIssueRepairService._value_key(val) for key, val in sorted(value.items())}
        if isinstance(value, list):
            return [DFTAuditIssueRepairService._value_key(item) for item in value]
        return str(value or "").strip().lower()

    @staticmethod
    def _float_or_raise(value: Any, field: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric.") from exc

    @staticmethod
    def _source_section_from_evidence(evidence: dict[str, Any]) -> str | None:
        return DFTAuditIssueRepairService._first_text(
            evidence.get("section"),
            evidence.get("section_title"),
            f"Page {evidence.get('page')}" if evidence.get("page") not in (None, "") else None,
        )

    @staticmethod
    def _is_supporting_reference(evidence: dict[str, Any], suggested: dict[str, Any]) -> bool:
        return normalize_source_document_type(
            evidence.get("source_document_type") or evidence.get("source_type") or suggested.get("source_document_type")
        ) == "supporting_reference"

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not str(reason or "").strip():
            raise ValueError("reason is required for this DFT audit issue repair action.")

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None
