from __future__ import annotations

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
)
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_review_helpers import (
    material_identity_parts_compatible,
    normalize_dft_value_for_comparison,
    same_normalized_dft_value,
)
from app.services.external_analysis_identity import (
    UNTRUSTED_LEGACY_SOURCE_IDENTITY,
    review_source_identity,
    review_submission_identity,
)
from app.utils.review_safety import is_safe_verified_review


class VerificationSessionDFTConsensusMixin:
    def _paper_dft_audit_candidates(self, paper_id: UUID) -> dict[str, list[dict[str, Any]]]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc(), ExternalAnalysisCandidate.id.asc())
        ).all()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            target_id = str(payload.get("target_id") or "").strip()
            decision = str(payload.get("decision") or "").strip().lower()
            if (
                target_type == "dft_results"
                and (target_id.lower() == "new" or decision == "new_candidate")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                # Repeated new_candidate imports for the same missing row should
                # participate in later DFT settlement against the materialized row.
                target_id = str(candidate.materialized_target_id).strip()
            if target_type != "dft_results" or not target_id or target_id.lower() == "new":
                continue
            grouped[target_id].append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate": candidate,
                    "target_id": target_id,
                    "field_name": str(payload.get("field_name") or "").strip(),
                    "decision": str(payload.get("decision") or "").strip().upper(),
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "normalized_material": payload.get("normalized_material"),
                    "normalized_material_or_catalyst": payload.get("normalized_material_or_catalyst"),
                    "material": payload.get("material"),
                    "catalyst": payload.get("catalyst"),
                    "structure_name": payload.get("structure_name"),
                    "adsorbate": payload.get("adsorbate"),
                    "reaction_step": payload.get("reaction_step"),
                    "normalized_energy_type": payload.get("normalized_energy_type"),
                    "source_label": str(payload.get("source_label") or run.source_label or run.source or "").strip(),
                    "source": str(payload.get("source") or run.source or "").strip(),
                    "source_identity": review_source_identity(
                        run.source_identity,
                        run.source_identity_verified,
                        default_untrusted=UNTRUSTED_LEGACY_SOURCE_IDENTITY,
                    ),
                    "source_identity_verified": bool(run.source_identity_verified),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "adjudication_role": payload.get("adjudication_role"),
                    "adjudication_scope": payload.get("adjudication_scope"),
                    "selected_source_ids": payload.get("selected_source_ids"),
                    "raw_payload": payload,
                    "status": candidate.status,
                }
            )
        return grouped

    def _settle_dft_row_from_existing_audits(
        self,
        *,
        row: DFTResult,
        audits: list[dict[str, Any]],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        row_ref = {
            "record_id": str(row.id),
            "field_name": "value",
            "property_type": row.property_type,
            "value": row.value,
            "unit": row.unit,
        }
        if not audits:
            row_ref["reason"] = "awaiting_two_ai_reviews"
            row_ref["status"] = "waiting_second_ai"
            return row_ref

        deduped: dict[str, dict[str, Any]] = {}
        for audit in audits:
            if audit["status"] not in {"candidate", "pending", "requires_resolution", "materialized"}:
                continue
            submission_id = self._dft_review_submission_identity(audit)
            if not submission_id:
                continue
            current = deduped.get(submission_id)
            if current is None or (audit.get("confidence") or 0) >= (current.get("confidence") or 0):
                deduped[submission_id] = audit
        opinions = list(deduped.values())
        opinions = [self._inherit_selected_dft_evidence(opinion, opinions) for opinion in opinions]
        anchored = [audit for audit in opinions if self._opinion_has_anchor(audit)]
        if opinions and not anchored:
            row_ref["reason"] = "missing_evidence_anchor"
            row_ref["status"] = "need_repair"
            return row_ref

        if any(self._is_project_library_v4_opinion(audit) for audit in anchored):
            row_ref["reason"] = self.PROJECT_LIBRARY_V4_USER_SUBMIT_REASON
            row_ref["eligible_opinion_count"] = len(anchored)
            row_ref["status"] = "need_repair"
            return row_ref

        third_ai = [
            audit for audit in anchored
            if str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
        ]
        if third_ai:
            adopted = max(third_ai, key=lambda item: item.get("confidence") or 0)
            if self._is_negative_dft_decision(adopted.get("decision")):
                result = self._apply_reject_all(
                    paper_id=row.paper_id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    reviewer=reviewer,
                    opinion=adopted,
                )
            else:
                adopted = self._complete_dft_third_ai_adjudication(row, adopted, anchored)
                result = self._apply_dft_consensus_outcome(
                    row=row,
                    adopted=adopted,
                    reviewer=reviewer,
                    write_lock_tokens=write_lock_tokens,
                )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(
                row=row,
                audits=anchored,
                result=result,
                negative=result.get("action") == "audit_opinion_rejected",
            )
            row_ref.update(result)
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref

        if len(anchored) < 2:
            row_ref["reason"] = "awaiting_two_ai_reviews"
            row_ref["eligible_opinion_count"] = len(anchored)
            row_ref["status"] = "waiting_second_ai"
            return row_ref

        if all(self._is_negative_dft_decision(audit.get("decision")) for audit in anchored):
            adopted = max(anchored, key=lambda item: item.get("confidence") or 0)
            result = self._apply_reject_all(
                paper_id=row.paper_id,
                target_type="dft_results",
                target_id=str(row.id),
                reviewer=reviewer,
                opinion=adopted,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(row=row, audits=anchored, result=result, negative=True)
            row_ref.update(
                {
                    "action": result.get("action"),
                    "review_result": result.get("result"),
                    "auto_applied": False,
                    "writes_final_truth": False,
                }
            )
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref

        has_reject = any(self._is_negative_dft_decision(audit.get("decision")) for audit in anchored)
        has_positive = any(str(audit.get("decision") or "").strip().upper() in {"PASS", "PROPOSED"} for audit in anchored)
        if has_reject and has_positive:
            row_ref["reason"] = "decision_conflict"
            row_ref["status"] = "need_third_ai"
            return row_ref

        whole_row = self._latest_dft_whole_row_proposal(anchored)
        supporting_pass = self._supporting_pass_for_row(row, anchored, whole_row)
        if whole_row and supporting_pass and self._all_nonnegative_dft_opinions_match(row, anchored):
            result = self._apply_dft_whole_row_consensus(
                row=row,
                proposal=whole_row,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(row=row, audits=anchored, result=result)
            row_ref.update(result)
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref
        if whole_row:
            row_ref["reason"] = "value_conflict"
            row_ref["status"] = "need_third_ai"
            return row_ref

        supported_field_proposal = self._latest_supported_dft_field_proposal(row, anchored)
        if supported_field_proposal is not None:
            proposal, supporting_pass = supported_field_proposal
            result = self._apply_dft_whole_row_consensus(
                row=row,
                proposal=self._synthesize_dft_whole_row_proposal(
                    row=row,
                    proposal=proposal,
                    supporting_pass=supporting_pass,
                ),
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(row=row, audits=anchored, result=result)
            row_ref.update(result)
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref

        pass_values = [audit for audit in anchored if str(audit.get("decision") or "").strip().upper() == "PASS"]
        if len(pass_values) >= 2 and self._all_pass_opinions_match(row, pass_values):
            if not self._dft_material_identities_compatible(
                pass_values,
                target_id=str(row.id),
                field_name="value",
            ):
                row_ref["reason"] = "material_identity_conflict"
                row_ref["status"] = "need_repair"
                return row_ref
            adopted = max(pass_values, key=lambda item: item.get("confidence") or 0)
            result = self._apply_dft_consensus_outcome(
                row=row,
                adopted=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(row=row, audits=anchored, result=result)
            row_ref.update(result)
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref

        if any(not self._dft_has_material_identity(audit, target_id=str(row.id), field_name=str(audit.get("field_name") or "")) for audit in anchored):
            row_ref["reason"] = "missing_dft_material_identity"
            row_ref["status"] = "need_repair"
            return row_ref

        identity_keys = {
            self._dft_identity_key(
                audit,
                target_id=str(row.id),
                field_name=str(audit.get("field_name") or ""),
            )
            for audit in anchored
        }
        if len(identity_keys) > 1:
            row_ref["reason"] = "material_identity_conflict"
            row_ref["status"] = "need_repair"
            return row_ref

        same_field_consensus = {
            self._review_consensus_key(
                audit,
                target_type="dft_results",
                target_id=str(row.id),
                field_name=str(audit.get("field_name") or ""),
            ): audit
            for audit in anchored
            if str(audit.get("field_name") or "").strip() not in {"", "dft_results"}
            and not self._is_negative_dft_decision(audit.get("decision"))
        }
        if len(same_field_consensus) == 1 and len(anchored) >= 2:
            adopted = max(same_field_consensus.values(), key=lambda item: item.get("confidence") or 0)
            result = self._apply_dft_consensus_outcome(
                row=row,
                adopted=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            self._merge_dft_issue_sources(row=row, audits=anchored, result=result)
            row_ref.update(result)
            row_ref["status"] = self._dft_settlement_status_for_result(result)
            return row_ref

        row_ref["reason"] = "value_conflict"
        row_ref["status"] = "need_third_ai"
        return row_ref

    def _merge_dft_issue_sources(
        self,
        *,
        row: DFTResult,
        audits: list[dict[str, Any]],
        result: dict[str, Any],
        negative: bool = False,
    ) -> None:
        if result.get("action") not in {"record_dft_audit_consensus", "audit_opinion_rejected"}:
            return
        issue_service = DFTAuditIssueService(self.session)
        field_name = str(result.get("field_name") or "dft_results")
        for audit in audits:
            if negative and not self._is_negative_dft_decision(audit.get("decision")):
                continue
            issue_service.create_or_update_consensus_issue(
                paper_id=row.paper_id,
                row=row,
                field_name=field_name,
                opinion=audit,
                negative=negative,
                adjudicated_by_third_ai=str(audit.get("adjudication_role") or "").strip().lower() == "third_ai",
            )

    def _apply_dft_consensus_outcome(
        self,
        *,
        row: DFTResult,
        adopted: dict[str, Any],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        field_name = str(adopted.get("field_name") or "value").strip() or "value"
        if field_name == "dft_results":
            return self._apply_dft_whole_row_consensus(
                row=row,
                proposal=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
        result = self._record_dft_audit_consensus(
            paper_id=row.paper_id,
            target_id=str(row.id),
            field_name=field_name,
            opinion=adopted,
            adjudicated_by_third_ai=str(adopted.get("adjudication_role") or "").strip().lower() == "third_ai",
        )
        return {
            "action": result.get("action"),
            "review_result": result.get("result"),
            "auto_applied": False,
            "writes_final_truth": False,
            "candidate_status": result.get("candidate_status"),
        }

    def _apply_dft_whole_row_consensus(
        self,
        *,
        row: DFTResult,
        proposal: dict[str, Any],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        corrected = proposal.get("corrected_value")
        if not isinstance(corrected, dict):
            raise ValueError("Whole-row DFT consensus is missing corrected_value.")
        result = self._record_dft_audit_consensus(
            paper_id=row.paper_id,
            target_id=str(row.id),
            field_name="dft_results",
            opinion=proposal,
            adjudicated_by_third_ai=str(proposal.get("adjudication_role") or "").strip().lower() == "third_ai",
        )
        return {
            "action": result.get("action"),
            "review_result": result.get("result"),
            "auto_applied": False,
            "writes_final_truth": False,
            "candidate_status": result.get("candidate_status"),
        }

    @staticmethod
    def _dft_settlement_status_for_result(result: dict[str, Any]) -> str:
        action = str(result.get("action") or "").strip()
        if action in {"record_dft_audit_consensus", "audit_opinion_rejected"}:
            return "audit_consensus_ready"
        return "auto_applied"

    def _dft_settlement_counts(self, paper_id: UUID) -> dict[str, Any]:
        from app.services.dft_review_queue_service import DFTReviewQueueService

        queue = DFTReviewQueueService(self.session).list_queue(
            paper_id=paper_id,
            status="all",
            limit=1000,
        )
        rows = list(queue.get("rows") or [])
        blocked_reason_counts = dict((queue.get("metadata") or {}).get("blocked_reasons") or {})
        need_repair_count = 0
        need_third_ai_count = 0
        waiting_second_ai_count = 0
        for row in rows:
            reasons = set(row.get("blocked_reasons") or [])
            audits = row.get("object_review_audits") or []
            anchored = [audit for audit in audits if self._opinion_has_anchor({"evidence_payload": audit.get("evidence_location")})]
            if row.get("is_exportable"):
                continue
            if reasons & {"missing_material_identity", "missing_evidence", "missing_evidence_text", "unsafe_locator"}:
                need_repair_count += 1
                continue
            if len(anchored) < 2 and audits:
                waiting_second_ai_count += 1
                continue
            if audits:
                need_third_ai_count += 1
        return {
            "exportable_count": sum(1 for row in rows if row.get("is_exportable")),
            "blocked_reason_counts": blocked_reason_counts,
            "need_third_ai_count": need_third_ai_count,
            "need_repair_count": need_repair_count,
            "waiting_second_ai_count": waiting_second_ai_count,
        }

    def _has_settled_dft_review(self, *, paper_id: UUID, target_id: str) -> bool:
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(target_id),
            )
        ).all()
        return any(
            is_safe_verified_review(review)
            or str(review.reviewer_status or "").strip().lower() == "rejected"
            for review in reviews
        )

    @staticmethod
    def _has_pending_dft_adjudication(audits: list[dict[str, Any]]) -> bool:
        return any(
            str(audit.get("status") or "").strip().lower() in {"candidate", "pending", "requires_resolution"}
            and str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
            for audit in audits
        )

    def _consume_matching_settled_dft_adjudication(
        self,
        *,
        row: DFTResult,
        audits: list[dict[str, Any]],
    ) -> bool:
        eligible = [
            audit
            for audit in audits
            if str(audit.get("status") or "").strip().lower()
            in {"candidate", "pending", "requires_resolution", "materialized"}
        ]
        adjudications = [
            audit
            for audit in eligible
            if str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
        ]
        if not adjudications:
            return False
        adopted = max(adjudications, key=lambda item: item.get("confidence") or 0)
        adopted = self._complete_dft_third_ai_adjudication(row, adopted, eligible)
        if not self._dft_adjudication_matches_row(row=row, adjudication=adopted):
            return False
        for audit in audits:
            if str(audit.get("status") or "").strip().lower() not in {"candidate", "pending", "requires_resolution"}:
                continue
            candidate = audit.get("candidate")
            if candidate is None:
                continue
            candidate.status = "ai_reviewed"
            self.session.add(candidate)
        return True

    def _dft_adjudication_matches_row(self, *, row: DFTResult, adjudication: dict[str, Any]) -> bool:
        corrected = adjudication.get("corrected_value")
        if not isinstance(corrected, dict):
            return False
        current_target = {"corrected_value": {"value": row.value, "unit": row.unit}, "field_name": "dft_results"}
        proposed_target = self._normalized_dft_audit_target(row, adjudication)
        if not self._same_normalized_dft_value(
            self._normalized_dft_audit_target(row, current_target),
            proposed_target,
        ):
            return False
        for source_fields, target_field in (
            (("property_type", "property", "energy_type"), "property_type"),
            (("adsorbate",), "adsorbate"),
            (("reaction_step",), "reaction_step"),
        ):
            proposed_value = next((corrected.get(key) for key in source_fields if key in corrected), None)
            if self._value_key(proposed_value) != self._value_key(getattr(row, target_field, None)):
                return False
        proposed_material = self._dft_material_identity_value(
            adjudication,
            target_id=str(row.id),
            field_name="dft_results",
        )
        if proposed_material:
            current_material = ""
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                current_material = str(sample.name or "").strip() if sample is not None else ""
            if not current_material or not self._material_identity_values_compatible(proposed_material, current_material):
                return False
        return True

    @staticmethod
    def _is_negative_dft_decision(decision: Any) -> bool:
        return str(decision or "").strip().upper() in {"REJECT", "REJECTED", "BLOCK", "DENY", "DROP"}

    @staticmethod
    def _latest_dft_whole_row_proposal(audits: list[dict[str, Any]]) -> dict[str, Any] | None:
        proposals = [
            audit for audit in audits
            if str(audit.get("decision") or "").strip().upper() in {"PROPOSED", "REVISE", "NEW_CANDIDATE"}
            and str(audit.get("field_name") or "").strip() == "dft_results"
            and isinstance(audit.get("corrected_value"), dict)
        ]
        if not proposals:
            return None
        proposals.sort(
            key=lambda item: (item.get("confidence") or 0, str(item.get("candidate_id") or "")),
            reverse=True,
        )
        return proposals[0]

    def _complete_dft_third_ai_adjudication(
        self,
        row: DFTResult,
        adopted: dict[str, Any],
        audits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_ids = {
            str(item).strip()
            for item in (adopted.get("selected_source_ids") or [])
            if str(item).strip()
        }
        selected = [
            audit for audit in audits
            if selected_ids & {
                str(audit.get("candidate_id") or "").strip(),
                str(audit.get("source_identity") or "").strip(),
                str(audit.get("source_label") or "").strip(),
            }
        ]
        corrected: dict[str, Any] = {
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "reaction_step": row.reaction_step,
            "value": row.value,
            "unit": row.unit,
        }
        for opinion in selected:
            payload = opinion.get("corrected_value")
            if isinstance(payload, dict):
                corrected.update({key: value for key, value in payload.items() if value not in (None, "")})
        adopted_corrected = adopted.get("corrected_value")
        if isinstance(adopted_corrected, dict):
            corrected.update({key: value for key, value in adopted_corrected.items() if value not in (None, "")})
        elif adopted_corrected not in (None, ""):
            adopted_field = str(adopted.get("field_name") or "").strip()
            mapped_field = self.DFT_FIELD_ALIASES.get(adopted_field, adopted_field)
            if mapped_field in corrected:
                corrected[mapped_field] = adopted_corrected

        material_identity = next(
            (
                value for value in (
                    corrected.get("material_identity"),
                    corrected.get("material"),
                    corrected.get("catalyst"),
                    adopted.get("normalized_material"),
                    adopted.get("normalized_material_or_catalyst"),
                )
                if value not in (None, "")
            ),
            None,
        )
        if material_identity is None and row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            if sample is not None and str(sample.name or "").strip():
                corrected["material_identity"] = sample.name

        evidence_payload = adopted.get("evidence_payload")
        if not self._opinion_has_anchor({"evidence_payload": evidence_payload}):
            evidence_payload = next(
                (audit.get("evidence_payload") for audit in selected if self._opinion_has_anchor(audit)),
                adopted.get("evidence_payload"),
            )
        return {
            **adopted,
            "field_name": "dft_results",
            "corrected_value": corrected,
            "evidence_payload": evidence_payload,
        }

    def _inherit_selected_dft_evidence(
        self,
        opinion: dict[str, Any],
        opinions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._opinion_has_anchor(opinion):
            return opinion
        if str(opinion.get("adjudication_role") or "").strip().lower() != "third_ai":
            return opinion
        selected_ids = {
            str(item).strip()
            for item in (opinion.get("selected_source_ids") or [])
            if str(item).strip()
        }
        if not selected_ids:
            return opinion
        selected = next(
            (
                candidate for candidate in opinions
                if candidate is not opinion
                and self._opinion_has_anchor(candidate)
                and selected_ids & {
                    str(candidate.get("candidate_id") or "").strip(),
                    str(candidate.get("source_identity") or "").strip(),
                    str(candidate.get("source_label") or "").strip(),
                }
            ),
            None,
        )
        if selected is None:
            return opinion
        return {**opinion, "evidence_payload": selected.get("evidence_payload")}

    def _supporting_pass_for_row(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if proposal is None:
            return None
        proposed_target = self._normalized_dft_audit_target(row, proposal)
        for audit in audits:
            decision = str(audit.get("decision") or "").strip().upper()
            if decision not in {"PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"}:
                continue
            if self._same_dft_review_submission(audit, proposal):
                continue
            field_name = str(audit.get("field_name") or "").strip()
            if field_name == "value":
                if not self._same_normalized_dft_value(proposed_target, self._normalized_dft_audit_target(row, audit)):
                    continue
            elif field_name == "dft_results":
                if not self._same_normalized_dft_value(
                    proposed_target,
                    self._normalized_dft_audit_target(row, audit),
                ):
                    continue
            else:
                continue
            left_material = self._dft_material_identity_value(proposal, target_id=str(row.id))
            right_material = self._dft_material_identity_value(audit, target_id=str(row.id))
            if left_material and right_material and not self._material_identity_values_compatible(
                left_material,
                right_material,
            ):
                continue
            return audit
        return None

    def _latest_supported_dft_field_proposal(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        proposals = [
            audit for audit in audits
            if str(audit.get("decision") or "").strip().upper() in {"PROPOSED", "NEW_CANDIDATE"}
            and str(audit.get("field_name") or "").strip() not in {"", "dft_results"}
        ]
        proposals.sort(
            key=lambda item: (item.get("confidence") or 0, str(item.get("candidate_id") or "")),
            reverse=True,
        )
        for proposal in proposals:
            supporting_pass = self._supporting_value_pass_for_field_proposal(row, audits, proposal)
            if supporting_pass is not None:
                return proposal, supporting_pass
        return None

    def _supporting_value_pass_for_field_proposal(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
        proposal: dict[str, Any],
    ) -> dict[str, Any] | None:
        proposal_field = str(proposal.get("field_name") or "").strip()
        row_value_target = {"value": row.value, "unit": str(row.unit or "").strip().lower().replace(" ", "")}
        for audit in audits:
            if str(audit.get("decision") or "").strip().upper() != "PASS":
                continue
            if str(audit.get("field_name") or "").strip() != "value":
                continue
            if self._same_dft_review_submission(audit, proposal):
                continue
            if not self._material_identity_values_compatible(
                self._dft_material_identity_value(proposal, target_id=str(row.id)),
                self._dft_material_identity_value(audit, target_id=str(row.id)),
            ):
                continue
            if proposal_field == "value":
                if not self._same_normalized_dft_value(
                    self._normalized_dft_audit_target(row, proposal),
                    self._normalized_dft_audit_target(row, audit),
                ):
                    continue
            elif not self._same_normalized_dft_value(row_value_target, self._normalized_dft_audit_target(row, audit)):
                continue
            return audit
        return None

    @staticmethod
    def _same_dft_review_submission(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_identity = VerificationSessionDFTConsensusMixin._dft_review_submission_identity(left)
        right_identity = VerificationSessionDFTConsensusMixin._dft_review_submission_identity(right)
        if left_identity and right_identity:
            return left_identity == right_identity
        left_id = str(left.get("candidate_id") or "").strip()
        right_id = str(right.get("candidate_id") or "").strip()
        if left_id and right_id:
            return left_id == right_id
        return left is right

    @staticmethod
    def _dft_review_submission_identity(audit: dict[str, Any]) -> str:
        return review_submission_identity(audit)

    def _synthesize_dft_whole_row_proposal(
        self,
        *,
        row: DFTResult,
        proposal: dict[str, Any],
        supporting_pass: dict[str, Any],
    ) -> dict[str, Any]:
        corrected = {
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "reaction_step": row.reaction_step,
            "unit": row.unit,
            "value": row.value,
        }
        supporting_corrected = supporting_pass.get("corrected_value")
        if isinstance(supporting_corrected, dict):
            if supporting_corrected.get("unit") not in (None, ""):
                corrected["unit"] = supporting_corrected.get("unit")
            if supporting_corrected.get("value") not in (None, ""):
                corrected["value"] = supporting_corrected.get("value")
        elif supporting_corrected not in (None, ""):
            corrected["value"] = supporting_corrected
        mapped_field = self.DFT_FIELD_ALIASES.get(str(proposal.get("field_name") or "").strip(), str(proposal.get("field_name") or "").strip())
        corrected[mapped_field] = proposal.get("corrected_value")
        return {
            **proposal,
            "field_name": "dft_results",
            "corrected_value": corrected,
        }

    def _all_pass_opinions_match(self, row: DFTResult, audits: list[dict[str, Any]]) -> bool:
        normalized = [self._normalized_dft_audit_target(row, audit) for audit in audits]
        first = next((item for item in normalized if item.get("value") is not None), None)
        if first is None:
            return False
        return all(self._same_normalized_dft_value(first, item) for item in normalized if item.get("value") is not None)

    def _all_nonnegative_dft_opinions_match(self, row: DFTResult, audits: list[dict[str, Any]]) -> bool:
        opinions = [audit for audit in audits if not self._is_negative_dft_decision(audit.get("decision"))]
        if len(opinions) < 2:
            return False
        normalized = [self._normalized_dft_audit_target(row, audit) for audit in opinions]
        if any(item.get("value") is None for item in normalized):
            return False
        if not all(self._same_normalized_dft_value(normalized[0], item) for item in normalized[1:]):
            return False
        return self._dft_material_identities_compatible(opinions, target_id=str(row.id), field_name="dft_results")

    def _dft_material_identities_compatible(
        self,
        audits: list[dict[str, Any]],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> bool:
        materials = [
            self._dft_material_identity_value(audit, target_id=target_id, field_name=field_name)
            for audit in audits
        ]
        materials = [material for material in materials if material]
        if len(materials) < 2:
            return True
        first = materials[0]
        return all(self._material_identity_values_compatible(first, material) for material in materials[1:])

    def _dft_material_identity_value(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> str:
        return str(self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)[1] or "")

    def _material_identity_values_compatible(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        return self._material_identity_parts_compatible(left, right)

    @staticmethod
    def _material_identity_parts_compatible(left: str, right: str) -> bool:
        return material_identity_parts_compatible(left, right)

    @staticmethod
    def _normalized_dft_audit_target(row: DFTResult, audit: dict[str, Any]) -> dict[str, Any]:
        corrected = audit.get("corrected_value")
        field_name = str(audit.get("field_name") or "").strip()
        if isinstance(corrected, dict):
            value = corrected.get("value")
            unit = corrected.get("unit")
        elif corrected not in (None, ""):
            value = corrected
            unit = row.unit
        elif field_name in {"value", "dft_results"}:
            value = row.value
            unit = row.unit
        else:
            value = None
            unit = row.unit
        return normalize_dft_value_for_comparison(value, unit)

    @staticmethod
    def _same_normalized_dft_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return same_normalized_dft_value(left, right)
