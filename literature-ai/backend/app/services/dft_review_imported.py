from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.models import AuditLog, DFTResult, PaperCorrection
from app.services.dft_review_fields import DFT_CORRECTION_FIELD_ALIASES, DFT_REVIEW_FIELD_ALIASES
from app.services.dft_review_helpers import (
    first_anchor,
    first_text,
    imported_evidence_payload,
    normalize_imported_dft_value,
    normalized_text,
    numeric_key,
)
from app.services.review_service import ReviewService


class DFTImportedOpinionMixin:
    def apply_imported_opinion(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        opinion: dict[str, Any],
        reviewer: str | None = None,
        expected_row_state: dict[str, Any] | None = None,
        expected_write_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, result_id)
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for this paper.")
        if not isinstance(opinion, dict):
            raise ValueError("A structured imported opinion payload is required.")
        self._guard_expected_row_state(row=row, expected_row_state=expected_row_state)

        reviewer_name = reviewer or "codex_review"
        decision = str(opinion.get("decision") or opinion.get("status") or "").strip().upper()
        if not decision:
            raise ValueError("Imported opinion is missing a decision.")
        if decision in {"NEEDS_HUMAN", "NEEDS_MANUAL", "MANUAL"}:
            raise ValueError("NEEDS_HUMAN opinions cannot be auto-applied to DFT results.")

        evidence_payload = self._imported_evidence_payload(opinion)
        reason = str(opinion.get("reason") or "").strip() or "Applied imported AI opinion from the DFT review queue."
        source_label = str(opinion.get("source_label") or opinion.get("source") or "imported_ai").strip()

        if decision in self.IMPORTED_NEGATIVE_DECISIONS:
            rejected = self.reject_result(
                paper_id=paper_id,
                result_id=result_id,
                confirm_reject_candidate=True,
                reviewer=reviewer_name,
                reviewer_note=f"Applied imported AI rejection from {source_label}. {reason}".strip(),
            )
            return {
                "paper_id": str(paper_id),
                "dft_result_id": str(result_id),
                "action": "reject",
                "source_label": source_label,
                "applied_corrections": [],
                "review_result": rejected,
            }

        applied_corrections: list[dict[str, Any]] = []
        corrected_value = opinion.get("corrected_value")
        material_identity = self._first_text(
            corrected_value.get("material_identity") if isinstance(corrected_value, dict) else None,
            corrected_value.get("material") if isinstance(corrected_value, dict) else None,
            corrected_value.get("catalyst") if isinstance(corrected_value, dict) else None,
            opinion.get("normalized_material"),
            opinion.get("normalized_material_or_catalyst"),
        )

        if material_identity or row.catalyst_sample_id:
            binding = self._apply_material_binding(
                row=row,
                material_identity=material_identity,
                reviewer=reviewer_name,
                reason=reason,
                evidence_payload=evidence_payload,
            )
            if binding:
                applied_corrections.append(binding)

        for field_name, proposed_value in self._imported_field_updates(row=row, opinion=opinion).items():
            applied_corrections.append(
                self._approve_dft_correction(
                    paper_id=paper_id,
                    result_id=result_id,
                    field_name=field_name,
                    proposed_value=proposed_value,
                    reviewer=reviewer_name,
                    reason=reason,
                    evidence_payload=evidence_payload,
                )
            )

        verify_field_names = self._imported_verify_field_names(
            row=row,
            opinion=opinion,
            applied_corrections=applied_corrections,
        )
        verified = self.verify_result(
            paper_id=paper_id,
            result_id=result_id,
            confirm_reviewed_against_pdf=True,
            reviewer=reviewer_name,
            reviewer_note=f"Applied imported AI opinion from {source_label}. {reason}".strip(),
            field_names=verify_field_names or None,
            expected_write_versions=expected_write_versions or {},
            evidence_payload=evidence_payload,
        )
        audit = AuditLog(
            paper_id=paper_id,
            action="apply_imported_dft_opinion",
            source=reviewer_name,
            target_type="dft_results",
            target_id=str(result_id),
            payload={
                "source_label": source_label,
                "decision": decision,
                "applied_correction_fields": [item.get("field_name") for item in applied_corrections],
                "verified_field_names": verify_field_names,
                "expected_row_state": expected_row_state or {},
            },
        )
        self.session.add(audit)
        self.session.commit()
        self.session.refresh(audit)
        return {
            "paper_id": str(paper_id),
            "dft_result_id": str(result_id),
            "action": "verify",
            "source_label": source_label,
            "applied_corrections": applied_corrections,
            "review_result": verified,
            "audit_log_id": str(audit.id),
        }

    def _approve_dft_correction(
        self,
        *,
        paper_id: UUID,
        result_id: UUID,
        field_name: str,
        proposed_value: Any,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        canonical_field = DFT_CORRECTION_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="dft_results",
            target_path=f"dft_results:{result_id}:{canonical_field}",
            operation="replace",
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        approved = ReviewService(self.session).approve_correction(
            correction.id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
        )
        self.session.flush()
        return {
            "correction_id": str(approved.id),
            "field_name": canonical_field,
            "proposed_value": proposed_value,
            "status": approved.status,
        }

    def _imported_field_updates(self, *, row: DFTResult, opinion: dict[str, Any]) -> dict[str, Any]:
        corrected_value = opinion.get("corrected_value")
        updates: dict[str, Any] = {}
        if isinstance(corrected_value, dict):
            property_candidates = ("property_type", "property", "energy_type")
            property_present = any(candidate in corrected_value for candidate in property_candidates)
            property_value = (
                self._first_text(*(corrected_value.get(candidate) for candidate in property_candidates))
                if property_present
                else self._first_text(opinion.get("normalized_energy_type"))
            )
            if property_present and self._normalized_text(property_value) != self._normalized_text(row.property_type):
                updates["property_type"] = property_value

            value_present = "value" in corrected_value
            value = corrected_value.get("value")
            unit_present = "unit" in corrected_value
            unit = corrected_value.get("unit") if unit_present else None
            normalized_value, normalized_unit = self._normalize_imported_dft_value(
                value=value,
                unit=unit,
                property_type=property_value or row.property_type,
            )
            if value_present:
                numeric_value = normalized_value
                if self._numeric_key(numeric_value) != self._numeric_key(row.value):
                    updates["value"] = numeric_value

            if unit_present and self._normalized_text(normalized_unit) != self._normalized_text(row.unit):
                updates["unit"] = normalized_unit

            adsorbate_present = "adsorbate" in corrected_value
            adsorbate = self._first_text(corrected_value.get("adsorbate")) if adsorbate_present else None
            if adsorbate_present and self._normalized_text(adsorbate) != self._normalized_text(row.adsorbate):
                updates["adsorbate"] = adsorbate

            reaction_step_present = "reaction_step" in corrected_value
            reaction_step = self._first_text(corrected_value.get("reaction_step")) if reaction_step_present else None
            if reaction_step_present and self._normalized_text(reaction_step) != self._normalized_text(row.reaction_step):
                updates["reaction_step"] = reaction_step
            return updates

        field_name = DFT_CORRECTION_FIELD_ALIASES.get(
            str(opinion.get("field_name") or "").strip(),
            str(opinion.get("field_name") or "").strip(),
        )
        if field_name in ReviewService.ALLOWED_DFT_RESULT_FIELDS and corrected_value not in (None, ""):
            current_value = getattr(row, field_name, None)
            if field_name == "value":
                numeric_value, normalized_unit = self._normalize_imported_dft_value(
                    value=corrected_value,
                    unit=row.unit,
                    property_type=row.property_type,
                )
                if numeric_value is not None and self._numeric_key(numeric_value) != self._numeric_key(current_value):
                    updates["value"] = numeric_value
                if normalized_unit and self._normalized_text(normalized_unit) != self._normalized_text(row.unit):
                    updates["unit"] = normalized_unit
            elif self._normalized_text(corrected_value) != self._normalized_text(current_value):
                updates[field_name] = corrected_value
        return updates

    def _imported_verify_field_names(
        self,
        *,
        row: DFTResult,
        opinion: dict[str, Any],
        applied_corrections: list[dict[str, Any]],
    ) -> list[str]:
        preferred: list[str] = []
        corrected_fields = {str(item.get("field_name") or "").strip() for item in applied_corrections}
        if corrected_fields:
            for field_name in corrected_fields:
                mapped = self._review_field_name_from_correction_field(field_name)
                if mapped and mapped not in preferred:
                    preferred.append(mapped)
            return preferred

        corrected_value = opinion.get("corrected_value")
        if row.catalyst_sample_id:
            preferred.append("catalyst")
        if isinstance(corrected_value, dict):
            if any(key in corrected_value for key in ("value", "unit")):
                preferred.append("value")
            if "adsorbate" in corrected_value:
                preferred.append("adsorbate")
            if any(key in corrected_value for key in ("property_type", "property", "energy_type")):
                preferred.append("energy_type")
            if "reaction_step" in corrected_value:
                preferred.append("reaction_step")
        field_name = DFT_CORRECTION_FIELD_ALIASES.get(
            str(opinion.get("field_name") or "").strip(),
            str(opinion.get("field_name") or "").strip(),
        )
        mapped = self._review_field_name_from_correction_field(field_name)
        if mapped and mapped not in preferred:
            preferred.append(mapped)
        return preferred

    @staticmethod
    def _review_field_name_from_correction_field(field_name: str) -> str | None:
        normalized = str(field_name or "").strip()
        if normalized == "property_type":
            return "energy_type"
        if normalized == "catalyst_sample_id":
            return "catalyst"
        if normalized in {"value", "adsorbate", "reaction_step"}:
            return normalized
        return None

    def _guard_expected_row_state(
        self,
        *,
        row: DFTResult,
        expected_row_state: dict[str, Any] | None,
    ) -> None:
        if expected_row_state is None:
            return
        if not isinstance(expected_row_state, dict):
            raise ValueError("expected_row_state must be an object.")
        for field_name, expected_value in expected_row_state.items():
            current_value = self._dft_row_state_value(row, field_name)
            if field_name == "value":
                if self._numeric_key(current_value) != self._numeric_key(expected_value):
                    raise ValueError("write_conflict:dft_result_state_stale")
                continue
            if self._normalized_text(current_value) != self._normalized_text(expected_value):
                raise ValueError("write_conflict:dft_result_state_stale")

    @staticmethod
    def _dft_row_state_value(row: DFTResult, field_name: str) -> Any:
        normalized = str(field_name or "").strip()
        if normalized == "catalyst_sample_id":
            return str(row.catalyst_sample_id) if row.catalyst_sample_id else None
        if normalized in {
            "candidate_status",
            "property_type",
            "adsorbate",
            "reaction_step",
            "value",
            "unit",
            "source_section",
            "source_figure",
        }:
            return getattr(row, normalized, None)
        raise ValueError(f"Unsupported expected_row_state field: {field_name}")

    @staticmethod
    def _imported_evidence_payload(opinion: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
        return imported_evidence_payload(opinion)

    @staticmethod
    def _first_anchor(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
        return first_anchor(payload)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        return first_text(*values)

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return normalized_text(value)

    @staticmethod
    def _numeric_key(value: Any) -> str:
        return numeric_key(value)

    @staticmethod
    def _normalize_imported_dft_value(
        *,
        value: Any,
        unit: str | None,
        property_type: Any = None,
    ) -> tuple[float | None, str | None]:
        return normalize_imported_dft_value(value=value, unit=unit, property_type=property_type)

    def _select_review_fields(
        self,
        snapshot: dict[str, dict[str, Any]],
        field_names: list[str] | None,
    ) -> list[str]:
        if field_names:
            selected = []
            for field_name in field_names:
                canonical = DFT_REVIEW_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
                if canonical and canonical not in selected:
                    selected.append(canonical)
            return selected

        selected = ["value"] if not self._is_blank(snapshot.get("value", {}).get("value")) else []
        for field_name in ["adsorbate", "energy_type", "reaction_step"]:
            value = snapshot.get(field_name, {}).get("value")
            if not self._is_blank(value) and field_name not in selected:
                selected.append(field_name)
        return selected

    @staticmethod
    def _gate_payload(row: DFTResult, gate: Any) -> dict[str, Any]:
        return {
            "record_id": str(row.id),
            "candidate_status": row.candidate_status or "system_candidate",
            "is_exportable": gate.eligible,
            "eligible": gate.eligible,
            "blocked_reasons": list(gate.reasons),
            "review_status": gate.review_status,
            "review_gate_status": gate.review_gate_status,
            "provenance_level": gate.provenance_level,
            "locator_status": gate.locator_status,
        }

    @staticmethod
    def _correction_payload(correction: PaperCorrection) -> dict[str, Any]:
        return {
            "id": str(correction.id),
            "paper_id": str(correction.paper_id),
            "source": correction.source,
            "field_name": correction.field_name,
            "target_path": correction.target_path,
            "operation": correction.operation,
            "proposed_value": correction.proposed_value,
            "reason": correction.reason,
            "evidence_payload": correction.evidence_payload,
            "status": correction.status,
            "reviewed_at": correction.reviewed_at.isoformat() if correction.reviewed_at else None,
            "reviewed_by": correction.reviewed_by,
            "created_at": correction.created_at.isoformat() if correction.created_at else None,
        }

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict)):
            return len(value) == 0
        return False
