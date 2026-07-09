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
)
from app.services.dft_review_service import DFTResultReviewService
from app.services.review_conflict_service import DECISION_NEGATIVE, DECISION_POSITIVE
from app.services.review_service import ReviewService
from app.services.review_target_resolver import canonical_target_type
from app.utils.evidence_anchors import has_evidence_anchor, has_pdf_evidence_anchor


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
                    "confirmation_required": bool(payload.get("confirmation_required", True)),
                    "raw_payload": payload,
                }
            )
        auto_applied: list[dict[str, Any]] = []
        pending_conflicts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for (paper_id_text, target_type, target_id, field_name), opinions in grouped.items():
            if target_type == "dft_results":
                anchored = [opinion for opinion in opinions if self._opinion_has_pdf_anchor(opinion)]
                decision = (
                    {"status": "consensus", "reason": "single_ai_dft_review", "opinion": anchored[-1]}
                    if anchored
                    else {"status": "rejected", "reason": "missing_pdf_evidence_anchor"}
                )
            else:
                decision = self._consensus_opinion(
                    opinions,
                    primary_label=primary_label,
                    secondary_label=secondary_label,
                    target_type=target_type,
                    target_id=target_id,
                    field_name=field_name,
                )
            if decision["status"] != "consensus":
                if decision["status"] == "rejected" and target_type == "dft_results":
                    for opinion in opinions:
                        candidate = opinion.get("candidate")
                        if candidate is None:
                            continue
                        candidate.status = "rejected_by_local_ai"
                        candidate.mapping_reason = decision["reason"]
                        self.session.add(candidate)
                    skipped.append(
                        {
                            "paper_id": paper_id_text,
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": decision["reason"],
                            "count": len(opinions),
                        }
                    )
                    continue
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
            if target_type == "tables" and adopted.get("action") == "requires_direct_table_tool":
                for opinion in opinions:
                    candidate = opinion.get("candidate")
                    if candidate is None:
                        continue
                    candidate.status = "requires_resolution"
                    candidate.materialized_target_type = None
                    candidate.materialized_target_id = None
                    candidate.mapping_reason = str(adopted.get("reason") or "requires_direct_table_tool")
                    self.session.add(candidate)
                pending_conflicts.append(
                    {
                        "paper_id": paper_id_text,
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": adopted.get("reason") or "requires_direct_table_tool",
                        "opinion_count": len(opinions),
                        "result": adopted,
                    }
                )
                continue
            materialized_target_type, materialized_target_id = self._materialized_target_ref(adopted)
            for opinion in opinions:
                candidate = opinion.get("candidate")
                if candidate is None:
                    continue
                candidate.status = "materialized"
                candidate.materialized_target_type = materialized_target_type
                candidate.materialized_target_id = materialized_target_id
                self.session.add(candidate)
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
        if reason == "borrowed_supporting_reference":
            candidate.status = "ignored"
        elif reason in {
            "missing_pdf_evidence_anchor",
            "missing_material_identity",
            "missing_property_type",
            "missing_value",
            "missing_unit",
            "ml_predicted_not_dft_result",
        }:
            candidate.status = "rejected_by_local_ai"
        else:
            candidate.status = "requires_resolution"
        candidate.mapping_reason = reason
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
        if str(primary.get("decision") or "") != str(secondary.get("decision") or ""):
            return {"status": "manual", "reason": "decision_conflict"}
        if self._value_key(primary.get("corrected_value")) != self._value_key(secondary.get("corrected_value")):
            return {"status": "manual", "reason": "value_conflict"}
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
            return self._apply_dft_opinion(
                paper_id=paper_id,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=opinion,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
        if target_type in {"tables", "figures"} and decision in DECISION_POSITIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "mark_reviewed", "target_type": target_type, "target_id": target_id}
        if decision in DECISION_NEGATIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "reject", "target_type": target_type, "target_id": target_id}
        proposed_value = opinion.get("corrected_value", opinion.get("value"))
        if target_type == "tables":
            return {
                "action": "requires_direct_table_tool",
                "target_type": "tables",
                "target_id": target_id,
                "field_name": field_name,
                "proposed_value": proposed_value,
                "candidate_status": "requires_resolution",
                "reason": "table_audit_corrected_value_not_applied",
                "recommended_tool": "update_table",
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

    def _apply_dft_opinion(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_name: str,
        reviewer: str,
        opinion: dict[str, Any],
        evidence_payload: Any,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        imported_opinion = {
            **opinion,
            "field_name": field_name,
            "evidence_payload": evidence_payload,
        }
        result = DFTResultReviewService(self.session).apply_imported_opinion(
            paper_id=paper_id,
            result_id=UUID(str(target_id)),
            opinion=imported_opinion,
            reviewer=reviewer,
            expected_write_versions=self._current_dft_review_versions(
                paper_id=paper_id,
                target_id=target_id,
            ),
            write_lock_tokens=write_lock_tokens,
            commit=False,
        )
        return {
            "action": "apply_imported_dft_opinion",
            "target_type": "dft_results",
            "target_id": target_id,
            "auto_applied": True,
            "writes_final_truth": True,
            "candidate_status": "ai_applied",
            "result": result,
        }

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
    def _opinion_has_pdf_anchor(opinion: dict[str, Any]) -> bool:
        return has_pdf_evidence_anchor(opinion.get("evidence_payload"))

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
        return key

    def _consensus_disagreement_reason(
        self,
        opinions: list[dict[str, Any]],
        *,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> str:
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
