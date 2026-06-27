from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import ExternalAnalysisCandidate, ExtractionFieldReview, PaperCorrection
from app.services.review_target_resolver import canonical_target_type


CORRECTION_STATUSES = {"pending", "rejected", "approved"}


class ReviewConflictOpinionMixin:
    """Opinion collection helpers for review conflict aggregation."""

    def _collect_opinions(
        self,
        *,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
    ) -> list[dict[str, Any]]:
        opinions: list[dict[str, Any]] = []
        opinions.extend(self._review_opinions(paper_id, target_type, target_id, field_name))
        opinions.extend(self._external_audit_opinions(paper_id, target_type, target_id, field_name))
        opinions.extend(self._correction_opinions(paper_id, target_type, target_id, field_name))
        return opinions

    def _review_opinions(
        self,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
    ) -> list[dict[str, Any]]:
        stmt = select(ExtractionFieldReview)
        if paper_id:
            stmt = stmt.where(ExtractionFieldReview.paper_id == paper_id)
        if target_type:
            stmt = stmt.where(ExtractionFieldReview.target_type == self._safe_canonical_target_type(target_type))
        if target_id:
            stmt = stmt.where(ExtractionFieldReview.target_id == str(target_id))
        if field_name:
            stmt = stmt.where(ExtractionFieldReview.field_name == field_name)
        rows = self.session.scalars(stmt).all()
        opinions: list[dict[str, Any]] = []
        for row in rows:
            payload = row.review_payload if isinstance(row.review_payload, dict) else {}
            audits = payload.get("ai_audits") if isinstance(payload, dict) else None
            for index, audit in enumerate(audits if isinstance(audits, list) else []):
                if not isinstance(audit, dict):
                    continue
                opinions.append(
                    self._opinion(
                        paper_id=row.paper_id,
                        target_type=row.target_type,
                        target_id=row.target_id,
                        field_name=row.field_name,
                        source_type="extraction_field_review",
                        source=audit.get("source") or audit.get("reviewer") or row.reviewer,
                        source_label=audit.get("source_label"),
                        reviewer=audit.get("reviewer") or row.reviewer,
                        agent_role=audit.get("agent_role"),
                        model_name=audit.get("model_name"),
                        decision=audit.get("decision") or row.reviewer_status,
                        status=audit.get("review_status") or row.reviewer_status,
                        confidence=audit.get("confidence"),
                        value=audit.get("corrected_value", audit.get("proposed_value", row.reviewed_value)),
                        unit=audit.get("unit", row.unit),
                        evidence=audit.get("evidence_payload") or {
                            "evidence_text": row.evidence_text,
                            "locator": audit.get("evidence_location") or audit.get("locator"),
                        },
                        reason=audit.get("reviewer_note") or audit.get("reason") or row.reviewer_note,
                        raw_payload=audit,
                        source_id=f"{row.id}:ai_audits:{index}",
                        created_at=row.updated_at,
                    )
                )
            if not audits:
                opinions.append(
                    self._opinion(
                        paper_id=row.paper_id,
                        target_type=row.target_type,
                        target_id=row.target_id,
                        field_name=row.field_name,
                        source_type="extraction_field_review",
                        source=row.reviewer or "field_review",
                        source_label=None,
                        reviewer=row.reviewer,
                        agent_role=None,
                        model_name=None,
                        decision=row.reviewer_status,
                        status=row.reviewer_status,
                        confidence=None,
                        value=row.reviewed_value,
                        unit=row.unit,
                        evidence={"evidence_text": row.evidence_text},
                        reason=row.reviewer_note,
                        raw_payload=payload,
                        source_id=str(row.id),
                        created_at=row.updated_at,
                    )
                )
        return opinions

    def _external_audit_opinions(
        self,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
    ) -> list[dict[str, Any]]:
        stmt = select(ExternalAnalysisCandidate).where(
            ExternalAnalysisCandidate.candidate_type.in_(("external_audit_opinion", "object_review_audit"))
        )
        if paper_id:
            stmt = stmt.where(ExternalAnalysisCandidate.paper_id == paper_id)
        rows = self.session.scalars(stmt).all()
        opinions: list[dict[str, Any]] = []
        for row in rows:
            payload = row.normalized_payload if isinstance(row.normalized_payload, dict) else {}
            object_items = [payload] if row.candidate_type == "object_review_audit" and payload else self._external_object_items(payload)
            for item in object_items:
                if self._is_ephemeral_new_candidate_audit(item):
                    continue
                parsed = self._object_target(item, default_paper_id=row.paper_id)
                if parsed is None:
                    continue
                p_id, t_type, t_id, f_name = parsed
                if not self._matches_filters(p_id, t_type, t_id, f_name, paper_id, target_type, target_id, field_name):
                    continue
                opinions.append(
                    self._opinion(
                        paper_id=p_id,
                        target_type=t_type,
                        target_id=t_id,
                        field_name=f_name,
                        source_type=row.candidate_type,
                        source=item.get("source") or payload.get("source"),
                        source_label=item.get("source_label") or payload.get("source_label"),
                        reviewer=item.get("reviewer"),
                        agent_role=item.get("agent_role") or payload.get("agent_role"),
                        model_name=item.get("model_name") or payload.get("model_name"),
                        decision=item.get("decision") or item.get("verdict") or payload.get("verdict"),
                        status=row.status or item.get("status") or payload.get("status"),
                        confidence=item.get("confidence", row.confidence),
                        value=item.get("corrected_value", item.get("proposed_value", item.get("value"))),
                        unit=item.get("unit"),
                        evidence=item.get("evidence_payload") or item.get("evidence_location") or item.get("evidence") or payload.get("evidence_examples"),
                        reason=item.get("reason") or item.get("reviewer_note") or item.get("mapping_reason") or row.mapping_reason,
                        raw_payload=item,
                        source_id=str(row.id),
                        created_at=row.created_at,
                    )
                )
        return opinions

    @staticmethod
    def _is_ephemeral_new_candidate_audit(item: dict[str, Any]) -> bool:
        try:
            target_type = canonical_target_type(str(item.get("target_type") or ""))
        except ValueError:
            return False
        decision = str(item.get("decision") or item.get("verdict") or "").strip().lower()
        target_id = str(
            item.get("target_id")
            or item.get("dft_result_id")
            or item.get("record_id")
            or ""
        ).strip().lower()
        return target_type == "dft_results" and decision == "new_candidate" and target_id == "new"

    def _correction_opinions(
        self,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperCorrection).where(PaperCorrection.status.in_(CORRECTION_STATUSES))
        if paper_id:
            stmt = stmt.where(PaperCorrection.paper_id == paper_id)
        rows = self._current_correction_rows(self.session.scalars(stmt).all())
        opinions: list[dict[str, Any]] = []
        for row in rows:
            if self._is_superseded_pending_correction(row, rows):
                continue
            parsed = self._correction_target(row)
            if parsed is None:
                continue
            p_id, t_type, t_id, f_name = parsed
            if not self._matches_filters(p_id, t_type, t_id, f_name, paper_id, target_type, target_id, field_name):
                continue
            evidence = row.evidence_payload if row.evidence_payload is not None else {}
            opinions.append(
                self._opinion(
                    paper_id=p_id,
                    target_type=t_type,
                    target_id=t_id,
                    field_name=f_name,
                    source_type="paper_correction",
                    source=row.source,
                    source_label=(evidence.get("source_label") if isinstance(evidence, dict) else None),
                    reviewer=row.reviewed_by,
                    agent_role=(evidence.get("agent_role") if isinstance(evidence, dict) else None),
                    model_name=(evidence.get("model_name") if isinstance(evidence, dict) else None),
                    decision=self._decision_for_correction(row.status),
                    status=row.status,
                    confidence=(evidence.get("confidence") if isinstance(evidence, dict) else None),
                    value=row.proposed_value,
                    unit=(row.proposed_value if f_name == "unit" else (evidence.get("unit") if isinstance(evidence, dict) else None)),
                    evidence=evidence,
                    reason=row.reason,
                    raw_payload={
                        "operation": row.operation,
                        "target_path": row.target_path,
                        "reviewed_by": row.reviewed_by,
                    },
                    source_id=str(row.id),
                    created_at=row.created_at,
                )
            )
        return opinions

    def _current_correction_rows(self, rows: list[PaperCorrection]) -> list[PaperCorrection]:
        grouped: dict[tuple[str, str, str, str, str], list[PaperCorrection]] = defaultdict(list)
        passthrough: list[PaperCorrection] = []
        for row in rows:
            parsed = self._correction_target(row)
            if parsed is None:
                passthrough.append(row)
                continue
            p_id, t_type, t_id, f_name = parsed
            grouped[(str(p_id), t_type, t_id, f_name, str(row.source or ""))].append(row)

        current = list(passthrough)
        for items in grouped.values():
            approved = [item for item in items if item.status == "approved"]
            pool = approved or items
            current.append(max(pool, key=lambda item: item.created_at or datetime.min))
        return current

    @staticmethod
    def _external_object_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        stack: list[Any] = [payload]
        raw = payload.get("raw_payload")
        if raw is not None:
            stack.append(raw)
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if any(key in item for key in ("target_type", "target_id", "field_name", "target_path")):
                    items.append(item)
                for key in ("candidates", "reviews", "audits", "opinions", "items", "field_reviews", "corrections"):
                    value = item.get(key)
                    if isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, dict):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
        return items

    def _is_superseded_pending_correction(
        self,
        row: PaperCorrection,
        rows: list[PaperCorrection],
    ) -> bool:
        if row.status != "pending":
            return False
        row_value = self._value_key(row.proposed_value)
        for other in rows:
            if other.id == row.id or other.status != "approved":
                continue
            if other.paper_id != row.paper_id or other.source != row.source:
                continue
            if other.field_name != row.field_name or other.target_path != row.target_path:
                continue
            if self._value_key(other.proposed_value) != row_value:
                continue
            if other.created_at and row.created_at and other.created_at < row.created_at:
                continue
            return True
        return False
