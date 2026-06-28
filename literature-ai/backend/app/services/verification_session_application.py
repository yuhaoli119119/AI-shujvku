from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import (
    CatalystSample,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    PaperCorrection,
    PaperTable,
)
from app.services.dft_review_service import DFTResultReviewService
from app.services.review_conflict_service import DECISION_NEGATIVE, DECISION_POSITIVE
from app.services.review_service import ReviewService
from app.services.review_target_resolver import canonical_target_type
from app.utils.evidence_anchors import has_evidence_anchor


class VerificationSessionReviewApplicationMixin:
    def _settle_high_risk_targets(
        self,
        *,
        paper_ids: list[UUID],
        primary_label: str,
        secondary_label: str,
        scope: str,
        reviewer: str,
    ) -> dict[str, Any]:
        target_types = self.HIGH_RISK_SCOPES.get(scope, set())
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisRun.paper_id.in_(paper_ids),
                ExternalAnalysisRun.source_label.in_([primary_label, secondary_label]),
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
        ).all()
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip()
            if target_type not in target_types:
                continue
            key = (
                str(candidate.paper_id),
                target_type,
                str(payload.get("target_id") or ""),
                str(payload.get("field_name") or ""),
            )
            grouped[key].append(
                {
                    "candidate": candidate,
                    "candidate_id": str(candidate.id),
                    "paper_id": str(candidate.paper_id),
                    "target_type": target_type,
                    "target_id": str(payload.get("target_id") or ""),
                    "field_name": str(payload.get("field_name") or ""),
                    "decision": str(payload.get("decision") or "").upper(),
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "source_label": run.source_label,
                    "source_id": str(candidate.id),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "human_confirmation_required": bool(payload.get("human_confirmation_required", True)),
                    "raw_payload": payload,
                }
            )
        auto_applied: list[dict[str, Any]] = []
        pending_conflicts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for (paper_id_text, target_type, target_id, field_name), opinions in grouped.items():
            decision = self._consensus_opinion(
                opinions,
                primary_label=primary_label,
                secondary_label=secondary_label,
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
            )
            if decision["status"] != "consensus":
                pending_conflicts.append(
                    {
                        "paper_id": paper_id_text,
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": decision["reason"],
                        "opinion_count": len(opinions),
                    }
                )
                continue
            adopted = self._apply_selected_opinion(
                paper_id=UUID(paper_id_text),
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=decision["opinion"],
                dual_ai_consensus=True,
            )
            materialized_target_type, materialized_target_id = self._materialized_target_ref(adopted)
            for opinion in opinions:
                candidate = opinion.get("candidate")
                if candidate is None:
                    continue
                candidate.status = "materialized"
                candidate.materialized_target_type = materialized_target_type
                candidate.materialized_target_id = materialized_target_id
                self.session.add(candidate)
            if target_type == "dft_results":
                for opinion in opinions:
                    candidate = opinion.get("candidate")
                    if candidate is None:
                        continue
                    candidate.status = self._object_review_candidate_status_for_result(adopted)
                    candidate.materialized_target_type = None
                    candidate.materialized_target_id = None
                    self.session.add(candidate)
                pending_conflicts.append(
                    {
                        "paper_id": paper_id_text,
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": "dual_ai_dft_audit_consensus_ready",
                        "opinion_count": len(opinions),
                        "result": adopted,
                    }
                )
            else:
                auto_applied.append(adopted)
        self.session.flush()
        missing_dual = max(0, len(grouped) - len(auto_applied) - len(pending_conflicts))
        if missing_dual:
            skipped.append({"reason": "insufficient_dual_ai_pairs", "count": missing_dual})
        return {
            "candidate_group_count": len(grouped),
            "auto_applied_count": len(auto_applied),
            "manual_conflict_count": len(pending_conflicts),
            "skipped_count": sum(int(item.get("count", 1)) for item in skipped),
            "auto_applied_items": auto_applied,
            "manual_conflicts": pending_conflicts,
            "skipped_items": skipped,
        }

    def _retire_skipped_new_dft_candidate(
        self,
        candidate: ExternalAnalysisCandidate,
        *,
        reason: str,
    ) -> None:
        candidate.status = "ignored" if reason == "borrowed_supporting_reference" else "requires_resolution"
        self.session.add(candidate)

    def _consensus_opinion(
        self,
        opinions: list[dict[str, Any]],
        *,
        primary_label: str,
        secondary_label: str,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> dict[str, Any]:
        by_label = {item.get("source_label"): item for item in opinions if item.get("source_label") in {primary_label, secondary_label}}
        if primary_label not in by_label or secondary_label not in by_label:
            return {"status": "pending", "reason": "awaiting_both_ai_reviews"}
        primary = by_label[primary_label]
        secondary = by_label[secondary_label]
        if not self._opinion_has_anchor(primary) or not self._opinion_has_anchor(secondary):
            return {"status": "manual", "reason": "missing_evidence_anchor"}
        if target_type == "dft_results" and (
            not self._dft_has_material_identity(primary, target_id=target_id, field_name=field_name)
            or not self._dft_has_material_identity(secondary, target_id=target_id, field_name=field_name)
        ):
            return {"status": "manual", "reason": "missing_dft_material_identity"}
        if str(primary.get("decision") or "") != str(secondary.get("decision") or ""):
            return {"status": "manual", "reason": "decision_conflict"}
        if self._value_key(primary.get("corrected_value")) != self._value_key(secondary.get("corrected_value")):
            return {"status": "manual", "reason": "value_conflict"}
        if target_type == "dft_results" and self._dft_identity_key(primary, target_id=target_id, field_name=field_name) != self._dft_identity_key(
            secondary,
            target_id=target_id,
            field_name=field_name,
        ):
            return {"status": "manual", "reason": "identity_conflict"}
        adopted = primary if (primary.get("confidence") or 0) >= (secondary.get("confidence") or 0) else secondary
        return {"status": "consensus", "reason": "dual_ai_match", "opinion": adopted}

    def _apply_selected_opinion(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        reviewer: str,
        opinion: dict[str, Any],
        dual_ai_consensus: bool = False,
        adjudicated_by_third_ai: bool = False,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        decision = str(opinion.get("decision") or "").upper()
        evidence_payload = self._materialize_evidence_payload(opinion)
        if target_type == "dft_results":
            if decision in {"REJECT", "REJECTED", "BLOCK"} and opinion.get("corrected_value") in (None, ""):
                return self._apply_reject_all(paper_id=paper_id, target_type=target_type, target_id=target_id, reviewer=reviewer)
            if dual_ai_consensus or adjudicated_by_third_ai:
                return self._record_dft_audit_consensus(
                    paper_id=paper_id,
                    target_id=target_id,
                    field_name=field_name,
                    opinion=opinion,
                    adjudicated_by_third_ai=adjudicated_by_third_ai,
                )
            return self._apply_dft_opinion(
                paper_id=paper_id,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=opinion,
                dual_ai_consensus=dual_ai_consensus,
                adjudicated_by_third_ai=adjudicated_by_third_ai,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
        if target_type in {"tables", "figures"} and decision in DECISION_POSITIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "mark_reviewed", "target_type": target_type, "target_id": target_id}
        if decision in DECISION_NEGATIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "reject", "target_type": target_type, "target_id": target_id}
        proposed_value = opinion.get("corrected_value", opinion.get("value"))
        if target_type == "tables":
            table = self.session.get(PaperTable, UUID(str(target_id)))
            if (
                table is not None
                and table.paper_id == paper_id
                and field_name in ReviewService.STRUCTURED_TARGETS["tables"].allowed_fields
                and getattr(table, field_name) == proposed_value
            ):
                return {
                    "action": "idempotent_noop",
                    "target_type": "tables",
                    "target_id": target_id,
                    "field_name": field_name,
                    "proposed_value": proposed_value,
                    "idempotent": True,
                    "candidate_status": "ai_applied",
                }
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            reviewer=reviewer,
            proposed_value=proposed_value,
            evidence_payload=evidence_payload,
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
            write_lock_tokens=write_lock_tokens,
        )

    def _apply_reject_all(self, *, paper_id: UUID, target_type: str, target_id: str, reviewer: str) -> dict[str, Any]:
        if target_type != "dft_results":
            raise ValueError("reject_all is currently only supported for DFT result candidates.")
        self._mark_dft_audit_candidates(
            paper_id=paper_id,
            target_id=target_id,
            status="ai_reviewed",
        )
        return {
            "action": "audit_opinion_rejected",
            "target_type": target_type,
            "target_id": target_id,
            "auto_applied": False,
            "writes_final_truth": False,
            "candidate_status": "ai_reviewed",
            "result": {
                "status": "audit_opinion_rejected",
                "needs_user_decision": False,
                "message": "Rejected AI audit opinions without changing the underlying DFT result.",
            },
        }

    def _record_dft_audit_consensus(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_name: str,
        opinion: dict[str, Any],
        adjudicated_by_third_ai: bool = False,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, UUID(str(target_id)))
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for adjudication.")
        return {
            "action": "record_dft_audit_consensus",
            "target_type": "dft_results",
            "target_id": target_id,
            "field_name": self.DFT_FIELD_ALIASES.get(field_name, field_name),
            "proposed_value": opinion.get("corrected_value", opinion.get("value")),
            "auto_applied": False,
            "writes_final_truth": False,
            "candidate_status": "requires_resolution",
            "result": {
                "status": "needs_user_decision",
                "reason": "third_ai_audit_opinion" if adjudicated_by_third_ai else "dual_ai_dft_audit_consensus",
                "message": "DFT AI audit consensus was recorded as an opinion only; it did not verify, reject, or edit the DFT result.",
            },
        }

    def _mark_dft_audit_candidates(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        status: str,
    ) -> None:
        rows = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
        ).all()
        for candidate in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            if self._normalize_object_review_target_type(payload.get("target_type")) != "dft_results":
                continue
            payload_target_id = str(payload.get("target_id") or "").strip()
            materialized_target_id = str(candidate.materialized_target_id or "").strip()
            if payload_target_id != str(target_id) and materialized_target_id != str(target_id):
                continue
            if candidate.status not in {"candidate", "pending", "requires_resolution", "materialized"}:
                continue
            candidate.status = status
            self.session.add(candidate)

    def _apply_dft_opinion(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_name: str,
        reviewer: str,
        opinion: dict[str, Any],
        dual_ai_consensus: bool,
        adjudicated_by_third_ai: bool,
        evidence_payload: Any,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, UUID(str(target_id)))
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for adjudication.")
        mapped_field = self.DFT_FIELD_ALIASES.get(field_name, field_name)
        proposed_value = opinion.get("corrected_value", opinion.get("value"))
        current_value = getattr(row, mapped_field, None) if hasattr(row, mapped_field) else None
        if not (mapped_field == "catalyst_sample_id" and proposed_value not in (None, "")):
            self._apply_dft_material_binding_if_needed(
                row=row,
                opinion=opinion,
                reviewer=reviewer,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
            self.session.flush()
            self.session.refresh(row)
        note = self._materialization_note(
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
        )
        if mapped_field == "value" and self._value_key(proposed_value) == self._value_key(current_value):
            result = DFTResultReviewService(self.session).verify_result(
                paper_id=paper_id,
                result_id=UUID(str(target_id)),
                confirm_reviewed_against_pdf=True,
                reviewer=reviewer,
                reviewer_note=note,
                field_names=["value"],
                expected_write_versions=self._current_dft_review_versions(
                    paper_id=paper_id,
                    target_id=target_id,
                    field_names=["value"],
                ),
                evidence_payload=evidence_payload,
                commit=False,
            )
            return {"action": "verify", "target_type": "dft_results", "target_id": target_id, "result": result}
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=target_id,
            field_name=mapped_field,
            reviewer=reviewer,
            proposed_value=proposed_value,
            evidence_payload=evidence_payload,
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
            write_lock_tokens=write_lock_tokens,
        )

    def _current_dft_review_versions(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_names: list[str] | None = None,
    ) -> dict[str, int]:
        stmt = select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == "dft_results",
            ExtractionFieldReview.target_id == str(target_id),
        )
        if field_names:
            stmt = stmt.where(ExtractionFieldReview.field_name.in_(field_names))
        reviews = self.session.scalars(stmt).all()
        return {
            str(review.field_name): int(review.write_version or 1)
            for review in reviews
        }

    def _apply_structured_correction(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        reviewer: str,
        proposed_value: Any,
        evidence_payload: Any,
        dual_ai_consensus: bool,
        adjudicated_by_third_ai: bool,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        target_collection = self._correction_collection_name(target_type)
        if target_collection == "paper":
            correction = PaperCorrection(
                paper_id=paper_id,
                source=reviewer,
                field_name=field_name,
                target_path=field_name,
                operation="replace",
                proposed_value=proposed_value,
                reason=self._materialization_note(
                    dual_ai_consensus=dual_ai_consensus,
                    adjudicated_by_third_ai=adjudicated_by_third_ai,
                ),
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
                "action": "approve_correction",
                "target_type": "paper",
                "target_id": str(paper_id),
                "correction_id": str(approved.id),
                "field_name": field_name,
                "proposed_value": proposed_value,
                "result": {"status": approved.status, "reviewed_by": approved.reviewed_by},
            }
        is_sample_create = (
            target_collection == "catalyst_samples"
            and str(target_id).strip().lower() in {"new", "create"}
            and str(field_name).strip().lower() == "create"
        )
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name=target_collection,
            target_path="catalyst_samples:new:create" if is_sample_create else f"{target_collection}:{target_id}:{field_name}",
            operation="create" if is_sample_create else "replace",
            proposed_value=proposed_value,
            reason=self._materialization_note(
                dual_ai_consensus=dual_ai_consensus,
                adjudicated_by_third_ai=adjudicated_by_third_ai,
            ),
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
        sample_resolution = (
            (approved.evidence_payload or {}).get("sample_resolution")
            if isinstance(approved.evidence_payload, dict)
            else None
        )
        resolved_target_id = (
            sample_resolution.get("catalyst_sample_id")
            if isinstance(sample_resolution, dict)
            else target_id
        )
        return {
            "action": "approve_correction",
            "target_type": target_collection,
            "target_id": resolved_target_id,
            "correction_id": str(approved.id),
            "field_name": field_name,
            "proposed_value": proposed_value,
            "result": {"status": approved.status, "reviewed_by": approved.reviewed_by},
        }

    def _apply_dft_material_binding_if_needed(
        self,
        *,
        row: DFTResult,
        opinion: dict[str, Any],
        reviewer: str,
        evidence_payload: Any,
        write_lock_tokens: list[str] | None = None,
    ) -> None:
        corrected_value = opinion.get("corrected_value")
        raw_payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        material_identity = self._first_text(
            corrected_value.get("material_identity") if isinstance(corrected_value, dict) else None,
            corrected_value.get("material") if isinstance(corrected_value, dict) else None,
            corrected_value.get("catalyst") if isinstance(corrected_value, dict) else None,
            opinion.get("normalized_material"),
            opinion.get("normalized_material_or_catalyst"),
            raw_payload.get("normalized_material"),
            raw_payload.get("normalized_material_or_catalyst"),
            raw_payload.get("material"),
            raw_payload.get("catalyst"),
        )
        if not material_identity and not row.catalyst_sample_id:
            return
        DFTResultReviewService(self.session)._apply_material_binding(  # noqa: SLF001 - reuse existing safe binding flow
            row=row,
            material_identity=material_identity,
            reviewer=reviewer,
            reason=str(opinion.get("reason") or "").strip() or "Applied AI-reviewed DFT material binding through the verification safety gate.",
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            write_lock_tokens=write_lock_tokens,
        )

    @staticmethod
    def _correction_collection_name(target_type: str) -> str:
        lowered = str(target_type or "").strip().lower()
        if lowered == "paper":
            return "paper"
        if lowered in {"figure", "figures"}:
            return "figures"
        if lowered in {"table", "tables"}:
            return "tables"
        return lowered

    @staticmethod
    def _normalize_object_review_target_type(value: Any) -> str:
        lowered = str(value or "").strip().lower()
        if lowered == "paper":
            return "paper"
        return canonical_target_type(lowered)

    @staticmethod
    def _materialized_target_ref(result: dict[str, Any]) -> tuple[str | None, str | None]:
        action = str(result.get("action") or "").strip()
        if action == "approve_correction" and result.get("target_type") == "catalyst_samples":
            return ("catalyst_sample", str(result.get("target_id") or "") or None)
        if action == "approve_correction":
            return ("paper_correction", str(result.get("correction_id") or "") or None)
        target_type = str(result.get("target_type") or "").strip() or None
        target_id = str(result.get("target_id") or "") or None
        return (target_type, target_id)

    @staticmethod
    def _object_review_candidate_status_for_result(result: dict[str, Any]) -> str:
        explicit_status = str(result.get("candidate_status") or "").strip()
        if explicit_status:
            return explicit_status
        action = str(result.get("action") or "").strip().lower()
        if action == "approve_correction":
            return "ai_applied"
        if action in {"mark_reviewed", "reject"}:
            return "ai_reviewed"
        return "materialized"

    @staticmethod
    def _correction_candidate_has_anchor(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        evidence_payload = payload.get("evidence_payload")
        if VerificationSessionReviewApplicationMixin._opinion_has_anchor(
            {"evidence_payload": evidence_payload}
        ):
            return True
        return VerificationSessionReviewApplicationMixin._opinion_has_anchor(
            {"evidence_payload": candidate.evidence_payload}
        )

    @staticmethod
    def _note_has_anchor(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        if payload.get("page") is not None:
            return True
        if str(payload.get("section_title") or "").strip():
            return True
        if str(payload.get("quoted_text") or "").strip():
            return True
        evidence_payload = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
        return any(
            evidence_payload.get(key) is not None and str(evidence_payload.get(key)).strip()
            for key in ("page", "section", "locator", "figure", "table", "evidence_text")
        )

    @staticmethod
    def _opinion_has_anchor(opinion: dict[str, Any]) -> bool:
        return has_evidence_anchor(opinion.get("evidence_payload"))

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return value

    def _review_consensus_key(
        self,
        opinion: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> tuple[Any, ...]:
        key: tuple[Any, ...] = (
            str(opinion.get("decision") or ""),
            self._value_key(opinion.get("corrected_value")),
        )
        if target_type == "dft_results":
            key = key + self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)
        return key

    def _consensus_disagreement_reason(
        self,
        opinions: list[dict[str, Any]],
        *,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> str:
        if target_type != "dft_results":
            return "ai_disagreement"
        value_keys = {
            (str(item.get("decision") or ""), self._value_key(item.get("corrected_value")))
            for item in opinions
        }
        if len(value_keys) > 1:
            return "ai_disagreement"
        identity_keys = {
            self._dft_identity_key(item, target_id=target_id, field_name=field_name)
            for item in opinions
        }
        if len(identity_keys) > 1:
            return "ai_identity_disagreement"
        return "ai_disagreement"

    def _dft_identity_key(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> tuple[Any, ...]:
        payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        if not payload:
            payload = opinion
        row = None
        if target_id:
            try:
                row = self.session.get(DFTResult, UUID(str(target_id)))
            except (TypeError, ValueError):
                row = None
        mapped_field = self.DFT_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
        corrected_value = opinion.get("corrected_value")

        def pick(field: str, *keys: str, fallback: Any = None) -> Any:
            if mapped_field == field and corrected_value not in (None, ""):
                return corrected_value
            for key in keys:
                value = payload.get(key)
                if value not in (None, "", []):
                    return value
            return fallback

        row_material = None
        if isinstance(row, DFTResult) and row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            row_material = sample.name if sample and sample.name else str(row.catalyst_sample_id)
        material_identity = pick(
            "catalyst_sample_id",
            "catalyst_sample_id",
            "normalized_material",
            "normalized_material_or_catalyst",
            "material",
            "catalyst",
            fallback=row_material,
        )
        property_type = pick(
            "property_type",
            "normalized_energy_type",
            "property_type",
            "energy_type",
            fallback=row.property_type if isinstance(row, DFTResult) else None,
        )
        structure_name = pick("structure_name", "structure_name")
        adsorbate = pick("adsorbate", "adsorbate", fallback=row.adsorbate if isinstance(row, DFTResult) else None)
        reaction_step = pick(
            "reaction_step",
            "reaction_step",
            fallback=row.reaction_step if isinstance(row, DFTResult) else None,
        )
        return tuple(
            self._normalized_identity_part(value)
            for value in (property_type, material_identity, structure_name, adsorbate, reaction_step)
        )

    @staticmethod
    def _normalized_identity_part(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).strip().lower()
        return str(value or "").strip().lower()

    def _dft_has_material_identity(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> bool:
        identity = self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)
        return len(identity) > 1 and bool(identity[1])

    @classmethod
    def _is_project_library_v4_opinion(cls, opinion: dict[str, Any]) -> bool:
        markers = list(cls._iter_payload_markers(opinion))
        text = " ".join(markers)
        has_context = "li_s_sac_dac" in text
        has_v4_contract = (
            "project_library_ml_export_v4" in text
            or "project_library_bundles_v1" in text
            or "project_library_v4" in text
        )
        user_submit_only = "database_write_authority=user_submit_only" in text
        auto_adopt_disabled = "ai_consensus_auto_adopt_allowed=false" in text
        return has_context and (has_v4_contract or user_submit_only or auto_adopt_disabled)

    @classmethod
    def _iter_payload_markers(cls, value: Any, *, prefix: str = ""):
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key or "").strip().lower()
                next_prefix = f"{prefix}.{key_text}" if prefix else key_text
                if isinstance(nested, (dict, list, tuple)):
                    yield from cls._iter_payload_markers(nested, prefix=next_prefix)
                elif nested is not None:
                    nested_text = str(nested).strip().lower()
                    if nested_text:
                        yield nested_text
                        yield f"{key_text}={nested_text}"
                        yield f"{next_prefix}={nested_text}"
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from cls._iter_payload_markers(item, prefix=prefix)

    @staticmethod
    def _materialization_note(*, dual_ai_consensus: bool, adjudicated_by_third_ai: bool) -> str:
        if adjudicated_by_third_ai:
            return "Third-AI adjudication adopted this opinion through the existing verify/correction safety gate."
        if dual_ai_consensus:
            return "Dual-AI consensus auto-adopted through the existing verify/correction safety gate."
        return "Manual adjudication adopted this AI opinion through the existing verify/correction safety gate."

    @staticmethod
    def _materialize_evidence_payload(opinion: dict[str, Any]) -> Any:
        payload = opinion.get("evidence_payload")
        if not isinstance(payload, dict):
            return payload
        merged = dict(payload)
        extra = {
            "adjudication_role": opinion.get("adjudication_role"),
            "adjudication_scope": opinion.get("adjudication_scope"),
            "selected_source_ids": opinion.get("selected_source_ids"),
            "review_decision": opinion.get("decision"),
            "review_source": opinion.get("source"),
            "review_source_label": opinion.get("source_label"),
        }
        merged.update({key: value for key, value in extra.items() if value not in (None, "", [])})
        return merged
