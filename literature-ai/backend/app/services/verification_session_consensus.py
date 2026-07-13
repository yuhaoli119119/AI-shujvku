from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun


class VerificationSessionDFTConsensusMixin:
    """Apply the latest evidence-backed DFT opinion without AI identity voting."""

    DFT_ACTIVE_AUDIT_STATUSES = {"candidate", "pending", "requires_resolution", "materialized", "ai_reviewed", "ai_applied"}
    DFT_APPLICABLE_DECISIONS = {
        "PASS",
        "PROPOSED",
        "REVISE",
        "NEW_CANDIDATE",
        "REJECT",
        "REJECTED",
        "BLOCK",
        "DENY",
        "DROP",
    }

    def _paper_dft_audit_candidates(
        self,
        paper_id: UUID,
        *,
        candidate_run_id: UUID | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        stmt = (
            select(
                ExternalAnalysisCandidate,
                ExternalAnalysisRun.id,
                ExternalAnalysisRun.source,
                ExternalAnalysisRun.source_label,
            )
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
                ExternalAnalysisCandidate.status.in_(sorted(self.DFT_ACTIVE_AUDIT_STATUSES)),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc(), ExternalAnalysisCandidate.id.asc())
        )
        if candidate_run_id is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.run_id == candidate_run_id)

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate, run_id, run_source, run_source_label in self.session.execute(stmt).all():
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            if self._normalize_object_review_target_type(payload.get("target_type")) != "dft_results":
                continue
            decision = self._normalize_dft_decision(payload.get("decision"))
            target_id = str(payload.get("target_id") or "").strip()
            if (
                (target_id.lower() == "new" or decision == "NEW_CANDIDATE")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                target_id = str(candidate.materialized_target_id).strip()
            if not target_id or target_id.lower() == "new":
                continue
            grouped[target_id].append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate": candidate,
                    "run_id": str(run_id),
                    "created_at": candidate.created_at,
                    "target_id": target_id,
                    "field_name": str(payload.get("field_name") or "").strip() or "dft_results",
                    "decision": decision,
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "normalized_material": payload.get("normalized_material"),
                    "normalized_material_or_catalyst": payload.get("normalized_material_or_catalyst"),
                    "normalized_energy_type": payload.get("normalized_energy_type"),
                    "source_label": str(payload.get("source_label") or run_source_label or run_source or "").strip(),
                    "source": str(payload.get("source") or run_source or "").strip(),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
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
            "field_name": "dft_results",
            "property_type": row.property_type,
            "value": row.value,
            "unit": row.unit,
        }
        active = [audit for audit in audits if audit.get("status") in self.DFT_ACTIVE_AUDIT_STATUSES]
        if not active:
            row_ref.update(status="skipped", reason="no_pending_ai_opinion")
            return row_ref

        opinion = active[-1]
        decision = self._normalize_dft_decision(opinion.get("decision"))
        row_ref["candidate_id"] = opinion.get("candidate_id")
        row_ref["decision"] = decision

        if decision in {"NEEDS_HUMAN", "NEEDS_MANUAL", "MANUAL"}:
            return self._hold_dft_opinion(
                row_ref=row_ref,
                opinion=opinion,
                status="needs_human",
                reason="ai_requested_human_review",
            )
        if decision not in self.DFT_APPLICABLE_DECISIONS:
            return self._hold_dft_opinion(
                row_ref=row_ref,
                opinion=opinion,
                status="need_repair",
                reason="unsupported_dft_review_decision",
            )
        if not self._opinion_has_pdf_anchor(opinion):
            return self._hold_dft_opinion(
                row_ref=row_ref,
                opinion=opinion,
                status="rejected",
                reason="missing_pdf_evidence_anchor",
            )
        if decision in {"PROPOSED", "REVISE"} and opinion.get("corrected_value") in (None, ""):
            return self._hold_dft_opinion(
                row_ref=row_ref,
                opinion=opinion,
                status="need_repair",
                reason="missing_corrected_value",
            )
        if decision not in {"REJECT", "REJECTED", "BLOCK", "DENY", "DROP"} and not self._dft_has_material_identity(
            opinion,
            target_id=str(row.id),
            field_name=str(opinion.get("field_name") or ""),
        ):
            return self._hold_dft_opinion(
                row_ref=row_ref,
                opinion=opinion,
                status="need_repair",
                reason="missing_dft_material_identity",
            )

        result = self._apply_selected_opinion(
            paper_id=row.paper_id,
            target_type="dft_results",
            target_id=str(row.id),
            field_name=str(opinion.get("field_name") or "dft_results"),
            reviewer=reviewer,
            opinion=opinion,
            write_lock_tokens=write_lock_tokens,
        )
        for audit in active:
            audit["candidate"].status = self._object_review_candidate_status_for_result(result)
            self.session.add(audit["candidate"])
        row_ref.update(result)
        row_ref["status"] = "auto_applied"
        return row_ref

    def _hold_dft_opinion(
        self,
        *,
        row_ref: dict[str, Any],
        opinion: dict[str, Any],
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        candidate = opinion["candidate"]
        candidate.status = "rejected_by_local_ai" if status == "rejected" else "requires_resolution"
        candidate.mapping_reason = reason
        self.session.add(candidate)
        row_ref.update(status=status, reason=reason)
        return row_ref

    def _dft_settlement_counts(self, paper_id: UUID) -> dict[str, Any]:
        from app.services.dft_review_queue_service import DFTReviewQueueService

        queue = DFTReviewQueueService(self.session).list_queue(paper_id=paper_id, status="all", limit=1000)
        rows = list(queue.get("rows") or [])
        blocked_reason_counts = dict((queue.get("metadata") or {}).get("blocked_reasons") or {})
        needs_human_count = 0
        need_repair_count = 0
        for row in rows:
            if row.get("is_exportable"):
                continue
            state = str((row.get("dft_workflow") or {}).get("state") or "")
            if state == "needs_human":
                needs_human_count += 1
            else:
                need_repair_count += 1
        return {
            "exportable_count": sum(1 for row in rows if row.get("is_exportable")),
            "blocked_reason_counts": blocked_reason_counts,
            "needs_human_count": needs_human_count,
            "need_repair_count": need_repair_count,
        }

    @staticmethod
    def _normalize_dft_decision(value: Any) -> str:
        decision = str(value or "").strip().upper()
        aliases = {
            "CONFIRMED": "PASS",
            "ACCEPT": "PASS",
            "ACCEPTED": "PASS",
            "APPROVED": "PASS",
            "VERIFIED": "PASS",
            "OK": "PASS",
            "CONFIRMED_WITH_CORRECTIONS": "REVISE",
            "CORRECTED": "REVISE",
            "REVISION": "REVISE",
        }
        return aliases.get(decision, decision)
