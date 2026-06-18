from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DFTResult,
    ExternalAnalysisCandidate,
    ExtractionFieldReview,
    MechanismClaim,
    PaperFigure,
    PaperCorrection,
    PaperTable,
    WritingCard,
)
from app.services.review_target_resolver import canonical_target_type


DECISION_POSITIVE = {"PASS", "ACCEPT", "APPROVE", "APPROVED", "VERIFIED", "OK"}
DECISION_NEGATIVE = {"REVISE", "FLAG", "INSUFFICIENT", "REJECT", "REJECTED", "NEEDS_FIX", "FIX", "BLOCK", "PROPOSED"}
CORRECTION_STATUSES = {"pending", "rejected", "approved"}


class ReviewConflictAggregationService:
    """Read-only aggregation of multi-reviewer disagreements for extracted fields."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._target_cache: dict[tuple[str, str], Any] = {}

    def list_conflicts(
        self,
        *,
        paper_id: UUID | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        field_name: str | None = None,
        include_non_conflicts: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        opinions = self._collect_opinions(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
        )
        groups = self._group_opinions(opinions)
        rows = []
        for key, items in sorted(groups.items(), key=lambda item: item[0]):
            enriched_items = [self._enrich_opinion(item) for item in items]
            conflict_types = self._conflict_types(enriched_items)
            if not conflict_types and not include_non_conflicts:
                continue
            paper, target, target_value, field = key.split("|", 3)
            target_summary = self._build_target_summary(target, target_value, enriched_items)
            anchor_summary = self._build_anchor_summary(enriched_items)
            rows.append(
                {
                    "paper_id": paper,
                    "target_type": target,
                    "target_id": target_value,
                    "field_name": field,
                    "reviewer_count": len(items),
                    "source_count": len({self._norm(item.get("source")) for item in enriched_items if item.get("source")}),
                    "conflict": bool(conflict_types),
                    "conflict_types": conflict_types,
                    "target_summary": target_summary,
                    "anchor_summary": anchor_summary,
                    "opinions": enriched_items,
                }
            )
        rows = rows[: max(1, min(limit, 1000))]
        conflict_type_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            for conflict_type in row["conflict_types"]:
                conflict_type_counts[conflict_type] += 1
        return {
            "schema_version": "review_conflicts_v2",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "paper_id": str(paper_id) if paper_id else None,
                "target_type": self._safe_canonical_target_type(target_type) if target_type else None,
                "target_id": str(target_id) if target_id else None,
                "field_name": field_name,
                "include_non_conflicts": include_non_conflicts,
            },
            "conflict_count": len(rows),
            "conflict_type_counts": dict(sorted(conflict_type_counts.items())),
            "rows": rows,
        }

    def count_conflicts_by_paper(self, paper_ids: set[UUID]) -> dict[str, int]:
        if not paper_ids:
            return {}
        counts = {str(paper_id): 0 for paper_id in paper_ids}
        for paper_id in paper_ids:
            counts[str(paper_id)] = self.list_conflicts(paper_id=paper_id, limit=1000)["conflict_count"]
        return counts

    def conflicts_by_target(
        self,
        *,
        paper_ids: set[UUID],
        target_type: str,
        target_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not paper_ids or not target_ids:
            return {}
        canonical = self._safe_canonical_target_type(target_type)
        payload = self.list_conflicts(
            paper_id=None,
            target_type=canonical,
            include_non_conflicts=False,
            limit=1000,
        )
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        allowed_papers = {str(item) for item in paper_ids}
        allowed_targets = {str(item) for item in target_ids}
        for row in payload["rows"]:
            if row["paper_id"] in allowed_papers and row["target_id"] in allowed_targets:
                by_target[row["target_id"]].append(row)
        return dict(by_target)

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
                        status=item.get("status") or payload.get("status") or row.status,
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

    def _correction_target(self, row: PaperCorrection) -> tuple[UUID, str, str, str] | None:
        target_path = str(row.target_path or "")
        match = re.match(r"^([^:]+):([^:]+):([^:]+)$", target_path)
        if match:
            collection = self._safe_canonical_target_type(match.group(1))
            target_id = match.group(2)
            field_name = match.group(3)
            if target_id == "new" and field_name == "create":
                structured_target = self._structured_create_target_id(row)
                target_id = structured_target or f"correction:{row.id}"
            return (row.paper_id, collection, target_id, field_name)
        if row.field_name:
            return None
        return None

    @staticmethod
    def _structured_create_target_id(row: PaperCorrection) -> str | None:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        structured = evidence.get("structured_create") if isinstance(evidence, dict) else None
        if isinstance(structured, dict) and structured.get("target_id"):
            return str(structured["target_id"])
        return None

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

    def _object_target(self, item: dict[str, Any], *, default_paper_id: UUID) -> tuple[UUID, str, str, str] | None:
        target_path = item.get("target_path")
        if isinstance(target_path, str):
            match = re.match(r"^([^:]+):([^:]+):([^:]+)$", target_path)
            if match:
                return (
                    UUID(str(item.get("paper_id") or default_paper_id)),
                    self._safe_canonical_target_type(match.group(1)),
                    str(match.group(2)),
                    str(match.group(3)),
                )
        target_type = item.get("target_type")
        target_id = (
            item.get("target_id")
            or item.get("dft_result_id")
            or item.get("mechanism_claim_id")
            or item.get("record_id")
        )
        field_name = item.get("field_name") or item.get("field")
        if not target_type or not target_id or not field_name:
            return None
        return (
            UUID(str(item.get("paper_id") or default_paper_id)),
            self._safe_canonical_target_type(str(target_type)),
            str(target_id),
            str(field_name),
        )

    def _group_opinions(self, opinions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in opinions:
            key = "|".join(
                [
                    str(item["paper_id"]),
                    str(item["target_type"]),
                    str(item["target_id"]),
                    str(item["field_name"]),
                ]
            )
            groups[key].append(item)
        return groups

    def _conflict_types(self, opinions: list[dict[str, Any]]) -> list[str]:
        if len(opinions) < 2:
            return []
        conflict_types: list[str] = []
        value_keys = {self._value_key(item.get("value")) for item in opinions if not self._is_blank(item.get("value"))}
        unit_keys = {self._norm(item.get("unit")) for item in opinions if not self._is_blank(item.get("unit"))}
        decision_keys = {self._decision_bucket(item.get("decision") or item.get("status")) for item in opinions}
        locator_keys = {self._locator_key(item.get("evidence")) for item in opinions if self._locator_key(item.get("evidence"))}
        mapping_keys = {
            "|".join([str(item.get("target_type") or ""), str(item.get("target_id") or ""), str(item.get("field_name") or "")])
            for item in opinions
        }
        identity_keys = {
            "|".join(
                [
                    self._norm((item.get("identity") or {}).get("normalized_energy_type")),
                    self._norm((item.get("identity") or {}).get("normalized_material")),
                    self._norm((item.get("identity") or {}).get("structure_name")),
                    self._norm((item.get("identity") or {}).get("adsorbate")),
                    self._norm((item.get("identity") or {}).get("reaction_step")),
                ]
            )
            for item in opinions
            if any((item.get("identity") or {}).get(key) for key in ("normalized_energy_type", "normalized_material", "structure_name", "adsorbate", "reaction_step"))
        }
        if len(value_keys) > 1:
            conflict_types.append("value_conflict")
        if len(unit_keys) > 1:
            conflict_types.append("unit_conflict")
        if len({key for key in decision_keys if key != "neutral"}) > 1:
            conflict_types.append("decision_conflict")
        if len(locator_keys) > 1:
            conflict_types.append("locator_conflict")
        if len(mapping_keys) > 1:
            conflict_types.append("mapping_conflict")
        if len(identity_keys) > 1:
            conflict_types.append("identity_conflict")
        return conflict_types

    def _enrich_opinion(self, opinion: dict[str, Any]) -> dict[str, Any]:
        item = dict(opinion)
        raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
        evidence = item.get("evidence")
        locator = self._extract_locator_payload(evidence)
        identity = {
            "normalized_energy_type": raw_payload.get("normalized_energy_type") or raw_payload.get("property_type"),
            "normalized_material": raw_payload.get("normalized_material"),
            "structure_name": raw_payload.get("structure_name"),
            "adsorbate": raw_payload.get("adsorbate"),
            "reaction_step": raw_payload.get("reaction_step"),
            "source_section": raw_payload.get("source_section") or locator.get("section") or raw_payload.get("section_title"),
            "source_figure": raw_payload.get("source_figure") or locator.get("figure") or locator.get("figure_id"),
            "source_table": raw_payload.get("source_table") or locator.get("table") or locator.get("table_id"),
        }
        identity["object_label"] = self._object_label(identity, item.get("field_name"))
        item["identity"] = identity
        item["anchor_summary"] = self._single_anchor_summary(evidence)
        if raw_payload.get("adjudication_role"):
            item["adjudication_role"] = raw_payload.get("adjudication_role")
        if raw_payload.get("adjudication_scope"):
            item["adjudication_scope"] = raw_payload.get("adjudication_scope")
        selected_source_ids = raw_payload.get("selected_source_ids")
        if isinstance(selected_source_ids, list):
            item["selected_source_ids"] = [str(value) for value in selected_source_ids if str(value).strip()]
        return item

    def _build_target_summary(self, target_type: str, target_id: str, opinions: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "target_type": target_type,
            "target_id": target_id,
        }
        target_row = self._load_target_row(target_type, target_id)
        if isinstance(target_row, DFTResult):
            summary.update(
                {
                    "object_label": self._join_bits(
                        [target_row.property_type, target_row.adsorbate, target_row.reaction_step, target_row.source_section]
                    ),
                    "property_type": target_row.property_type,
                    "adsorbate": target_row.adsorbate,
                    "reaction_step": target_row.reaction_step,
                    "source_section": target_row.source_section,
                    "current_value": target_row.value,
                    "current_unit": target_row.unit,
                }
            )
        elif isinstance(target_row, PaperFigure):
            summary.update(
                {
                    "object_label": self._join_bits([target_row.figure_label, target_row.caption]),
                    "figure_label": target_row.figure_label,
                    "caption": target_row.caption,
                    "page": target_row.page,
                    "content_summary": target_row.content_summary,
                }
            )
        elif isinstance(target_row, PaperTable):
            summary.update(
                {
                    "object_label": self._join_bits([target_row.caption, f"page {target_row.page}" if target_row.page else None]),
                    "caption": target_row.caption,
                    "page": target_row.page,
                }
            )
        elif isinstance(target_row, MechanismClaim):
            summary.update(
                {
                    "object_label": self._join_bits([target_row.claim_type, target_row.claim_text]),
                    "claim_type": target_row.claim_type,
                    "claim_text": target_row.claim_text,
                }
            )
        elif isinstance(target_row, WritingCard):
            summary.update(
                {
                    "object_label": self._join_bits([target_row.paper_type, "writing card"]),
                    "paper_type": target_row.paper_type,
                }
            )
        first_identity = self._first_non_blank_identity(opinions)
        summary.update({key: value for key, value in first_identity.items() if value and key not in summary})
        if not summary.get("object_label"):
            summary["object_label"] = self._join_bits(
                [
                    summary.get("normalized_energy_type"),
                    summary.get("normalized_material"),
                    summary.get("structure_name"),
                    summary.get("adsorbate"),
                    summary.get("reaction_step"),
                ]
            ) or target_id
        return summary

    def _build_anchor_summary(self, opinions: list[dict[str, Any]]) -> dict[str, Any]:
        anchors = [self._single_anchor_summary(item.get("evidence")) for item in opinions]
        best = next(
            (
                anchor
                for anchor in anchors
                if any(anchor.get(key) for key in ("page", "section", "quoted_text", "table", "figure", "locator_status"))
            ),
            {},
        )
        return {
            "page": best.get("page"),
            "section": best.get("section"),
            "table": best.get("table"),
            "figure": best.get("figure"),
            "quoted_text": best.get("quoted_text"),
            "locator_status": best.get("locator_status"),
            "source_count": len([anchor for anchor in anchors if any(anchor.values())]),
        }

    def _single_anchor_summary(self, evidence: Any) -> dict[str, Any]:
        payload = evidence[0] if isinstance(evidence, list) and evidence else evidence
        if not isinstance(payload, dict):
            return {}
        locator = self._extract_locator_payload(payload)
        return {
            "page": locator.get("page"),
            "section": locator.get("section") or payload.get("section_title"),
            "table": locator.get("table") or locator.get("table_id"),
            "figure": locator.get("figure") or locator.get("figure_id"),
            "quoted_text": payload.get("quoted_text") or payload.get("evidence_text") or payload.get("excerpt") or payload.get("text"),
            "locator_status": locator.get("locator_status"),
        }

    def _load_target_row(self, target_type: str, target_id: str) -> Any:
        key = (target_type, target_id)
        if key in self._target_cache:
            return self._target_cache[key]
        model = {
            "dft_results": DFTResult,
            "figures": PaperFigure,
            "tables": PaperTable,
            "mechanism_claims": MechanismClaim,
            "writing_cards": WritingCard,
        }.get(target_type)
        row = None
        if model is not None:
            try:
                row = self.session.get(model, UUID(str(target_id)))
            except (ValueError, TypeError):
                row = None
        self._target_cache[key] = row
        return row

    @staticmethod
    def _extract_locator_payload(evidence: Any) -> dict[str, Any]:
        payload = evidence[0] if isinstance(evidence, list) and evidence else evidence
        if not isinstance(payload, dict):
            return {}
        locator = payload.get("locator") if isinstance(payload.get("locator"), dict) else payload.get("evidence_location")
        if isinstance(locator, dict):
            return locator
        return payload

    @staticmethod
    def _first_non_blank_identity(opinions: list[dict[str, Any]]) -> dict[str, Any]:
        for opinion in opinions:
            identity = opinion.get("identity") or {}
            if any(identity.get(key) for key in identity):
                return identity
        return {}

    @classmethod
    def _object_label(cls, identity: dict[str, Any], field_name: Any) -> str:
        return cls._join_bits(
            [
                identity.get("normalized_energy_type"),
                identity.get("normalized_material"),
                identity.get("structure_name"),
                identity.get("adsorbate"),
                identity.get("reaction_step"),
                identity.get("source_section"),
                field_name,
            ]
        )

    @staticmethod
    def _join_bits(values: list[Any]) -> str:
        bits = [str(value).strip() for value in values if value is not None and str(value).strip()]
        return " | ".join(bits)

    @staticmethod
    def _opinion(
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        source_type: str,
        source: Any,
        source_label: Any,
        reviewer: Any,
        agent_role: Any,
        model_name: Any,
        decision: Any,
        status: Any,
        confidence: Any,
        value: Any,
        unit: Any,
        evidence: Any,
        reason: Any,
        raw_payload: Any,
        source_id: str,
        created_at: datetime | None,
    ) -> dict[str, Any]:
        return {
            "paper_id": str(paper_id),
            "target_type": target_type,
            "target_id": str(target_id),
            "field_name": field_name,
            "source_type": source_type,
            "source_id": source_id,
            "source": source,
            "source_label": source_label,
            "reviewer": reviewer,
            "agent_role": agent_role,
            "model_name": model_name,
            "decision": decision,
            "status": status,
            "confidence": confidence,
            "value": value,
            "unit": unit,
            "evidence": evidence,
            "reason": reason,
            "raw_payload": raw_payload,
            "created_at": created_at.replace(tzinfo=timezone.utc).isoformat() if created_at else None,
        }

    @staticmethod
    def _decision_for_correction(status: str) -> str:
        normalized = str(status or "").lower()
        if normalized == "approved":
            return "APPROVED"
        if normalized == "rejected":
            return "REJECTED"
        return "PROPOSED"

    @staticmethod
    def _decision_bucket(decision: Any) -> str:
        normalized = str(decision or "").strip().upper()
        if normalized in DECISION_POSITIVE:
            return "positive"
        if normalized in DECISION_NEGATIVE:
            return "negative"
        return "neutral"

    @staticmethod
    def _value_key(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.8g}"
        try:
            if isinstance(value, str) and value.strip() and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip()):
                return f"{float(value):.8g}"
        except ValueError:
            pass
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).strip().lower()

    @staticmethod
    def _locator_key(evidence: Any) -> str:
        payload = evidence
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict):
            return ""
        location = payload.get("evidence_location") or payload.get("locator") or payload
        if isinstance(location, str):
            return location.strip().lower()
        if not isinstance(location, dict):
            return ""
        keys = ("page", "section", "figure", "figure_id", "table", "table_id", "bbox")
        parts = [ReviewConflictAggregationService._value_key(location.get(key)) for key in keys if location.get(key) is not None]
        return "|".join(parts)

    @staticmethod
    def _norm(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    @staticmethod
    def _is_blank(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _safe_canonical_target_type(value: str | None) -> str:
        if not value:
            return ""
        try:
            return canonical_target_type(value)
        except ValueError:
            return str(value).strip().lower()

    def _matches_filters(
        self,
        p_id: UUID,
        t_type: str,
        t_id: str,
        f_name: str,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
    ) -> bool:
        if paper_id and p_id != paper_id:
            return False
        if target_type and t_type != self._safe_canonical_target_type(target_type):
            return False
        if target_id and t_id != str(target_id):
            return False
        if field_name and f_name != field_name:
            return False
        return True
