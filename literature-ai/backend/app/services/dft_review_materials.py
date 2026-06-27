from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.models import CatalystSample, DFTResult, PaperCorrection
from app.services.dft_review_helpers import existing_material_binding_name_matches
from app.services.review_service import ReviewService


class DFTMaterialBindingMixin:
    def _apply_material_binding(
        self,
        *,
        row: DFTResult,
        material_identity: str | None,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any] | None:
        if not self._has_anchor(evidence_payload):
            raise ValueError("Applying imported material binding requires a PDF evidence anchor.")
        target_sample_id = str(row.catalyst_sample_id) if row.catalyst_sample_id else None
        if not material_identity and target_sample_id:
            return None
        if material_identity:
            if self._existing_material_binding_matches(row=row, material_identity=material_identity):
                return None
            target_sample_id = self._resolve_or_create_catalyst_sample_id(
                paper_id=row.paper_id,
                material_identity=material_identity,
                reviewer=reviewer,
                reason=reason,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
        if not target_sample_id:
            return None
        return self._approve_dft_correction(
            paper_id=row.paper_id,
            result_id=row.id,
            field_name="catalyst_sample_id",
            proposed_value=target_sample_id,
            reviewer=reviewer,
            reason=reason,
            evidence_payload=evidence_payload,
            write_lock_tokens=write_lock_tokens,
        )

    def _existing_material_binding_matches(self, *, row: DFTResult, material_identity: str) -> bool:
        if not row.catalyst_sample_id:
            return False
        sample = self.session.get(CatalystSample, row.catalyst_sample_id)
        if sample is None:
            return False
        return existing_material_binding_name_matches(sample.name, material_identity)

    def _resolve_or_create_catalyst_sample_id(
        self,
        *,
        paper_id: UUID,
        material_identity: str,
        reviewer: str,
        reason: str,
        evidence_payload: dict[str, Any] | list[Any] | None,
        write_lock_tokens: list[str] | None = None,
    ) -> str:
        first_anchor = self._first_anchor(evidence_payload)
        proposed_value = {
            "name": material_identity,
            "structure_name": material_identity,
            "evidence_strength": self._first_text(
                first_anchor.get("quoted_text") if first_anchor else None,
                reason,
            ),
        }
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="catalyst_samples",
            target_path="catalyst_samples:new:create",
            operation="create",
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
        payload = approved.evidence_payload if isinstance(approved.evidence_payload, dict) else {}
        resolution = payload.get("sample_resolution") if isinstance(payload, dict) else {}
        sample_id = resolution.get("catalyst_sample_id") if isinstance(resolution, dict) else None
        if not sample_id:
            raise ValueError("Imported material identity could not be resolved to a catalyst sample.")
        return str(sample_id)
