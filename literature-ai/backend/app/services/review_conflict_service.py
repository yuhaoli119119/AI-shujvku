from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    PaperCorrection,
)
from app.services.review_conflict_dft import ReviewConflictDftMixin
from app.services.review_conflict_opinions import ReviewConflictOpinionMixin
from app.services.review_conflict_resolution import DFT_CONFLICT_FIELD_MAP, ReviewConflictResolutionMixin
from app.services.review_conflict_targets import ReviewConflictTargetMixin
from app.services.review_target_resolver import canonical_target_type


DECISION_POSITIVE = {"PASS", "ACCEPT", "APPROVE", "APPROVED", "VERIFIED", "OK"}
DECISION_NEGATIVE = {"REVISE", "FLAG", "INSUFFICIENT", "REJECT", "REJECTED", "NEEDS_FIX", "FIX", "BLOCK", "PROPOSED"}


class ReviewConflictAggregationService(
    ReviewConflictResolutionMixin,
    ReviewConflictTargetMixin,
    ReviewConflictOpinionMixin,
    ReviewConflictDftMixin,
):
    """Read-only aggregation of multi-reviewer disagreements for extracted fields."""

    VISUAL_TARGET_TYPES = {"figure", "figures", "table", "tables"}
    DFT_TARGET_TYPES = {"dft_result", "dft_results", "dft_setting", "dft_settings", "catalyst_sample", "catalyst_samples"}

    def __init__(self, session: Session) -> None:
        self.session = session
        self._target_cache: dict[tuple[str, str], Any] = {}
        self._catalyst_cache: dict[str, CatalystSample | None] = {}

    def list_conflicts(
        self,
        *,
        paper_id: UUID | None = None,
        paper_ids: set[UUID] | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        field_name: str | None = None,
        include_non_conflicts: bool = False,
        active_only: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        if target_type and not self._is_dft_target_type(target_type):
            return self._empty_payload(
                paper_id=paper_id,
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                include_non_conflicts=include_non_conflicts,
                active_only=active_only,
            )
        opinions = self._collect_opinions(
            paper_id=paper_id,
            paper_ids=paper_ids,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
        )
        self._prefetch_target_rows(opinions)
        opinions = [
            item
            for item in self._exclude_rejected_dft_targets(opinions)
            if self._is_dft_target_type(item.get("target_type"))
        ]
        if active_only:
            opinions = self._active_opinions(opinions)
        groups = self._group_opinions(opinions)
        rows = []
        for key, items in sorted(groups.items(), key=lambda item: item[0]):
            collapsed_items = self._collapse_adopted_opinions(items)
            if active_only:
                collapsed_items = self._collapse_active_dft_adjudication(collapsed_items)
            enriched_items = [self._enrich_opinion(item) for item in collapsed_items]
            conflict_types = self._conflict_types(enriched_items)
            if not conflict_types and not include_non_conflicts:
                continue
            paper, target, target_value, field = key.split("|", 3)
            affected_field_names = self._affected_field_names(target, field, conflict_types)
            if active_only and self._dft_conflict_group_is_settled(
                target_type=target,
                target_id=target_value,
                field_name=field,
                affected_field_names=affected_field_names,
                conflict_types=conflict_types,
            ):
                continue
            target_summary = self._build_target_summary(target, target_value, enriched_items)
            anchor_summary = self._build_anchor_summary(enriched_items)
            rows.append(
                {
                    "paper_id": paper,
                    "target_type": target,
                    "target_id": target_value,
                    "field_name": field,
                    "reviewer_count": len(enriched_items),
                    "source_count": len({self._norm(item.get("source")) for item in enriched_items if item.get("source")}),
                    "conflict": bool(conflict_types),
                    "conflict_types": conflict_types,
                    "affected_field_names": affected_field_names,
                    "conflict_field_names": affected_field_names,
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
                "active_only": active_only,
            },
            "conflict_count": len(rows),
            "conflict_type_counts": dict(sorted(conflict_type_counts.items())),
            "rows": rows,
        }

    def _empty_payload(
        self,
        *,
        paper_id: UUID | None,
        target_type: str | None,
        target_id: str | None,
        field_name: str | None,
        include_non_conflicts: bool,
        active_only: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": "review_conflicts_v2",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "paper_id": str(paper_id) if paper_id else None,
                "target_type": self._safe_canonical_target_type(target_type) if target_type else None,
                "target_id": str(target_id) if target_id else None,
                "field_name": field_name,
                "include_non_conflicts": include_non_conflicts,
                "active_only": active_only,
            },
            "conflict_count": 0,
            "conflict_type_counts": {},
            "rows": [],
        }

    def count_conflicts_by_paper(self, paper_ids: set[UUID]) -> dict[str, int]:
        if not paper_ids:
            return {}
        counts = {str(paper_id): 0 for paper_id in paper_ids}
        for paper_id in paper_ids:
            counts[str(paper_id)] = self.list_conflicts(paper_id=paper_id, limit=1000)["conflict_count"]
        return counts

    def count_conflicts_by_paper_and_module(self, paper_ids: set[UUID]) -> dict[str, dict[str, int]]:
        if not paper_ids:
            return {}
        counts = {
            str(paper_id): {"dft": 0, "visual": 0, "content": 0, "other": 0}
            for paper_id in paper_ids
        }
        for paper_id in paper_ids:
            payload = self.list_conflicts(paper_id=paper_id, limit=1000)
            summary = counts[str(paper_id)]
            for row in (payload.get("rows") or []):
                summary[self._module_for_target_type(row.get("target_type"))] += 1
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
            paper_ids=paper_ids,
            target_type=canonical,
            include_non_conflicts=False,
            active_only=True,
            limit=1000,
        )
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        allowed_papers = {str(item) for item in paper_ids}
        allowed_targets = {str(item) for item in target_ids}
        for row in payload["rows"]:
            if row["paper_id"] in allowed_papers and row["target_id"] in allowed_targets:
                by_target[row["target_id"]].append(row)
        return dict(by_target)

    @classmethod
    def _module_for_target_type(cls, target_type: Any) -> str:
        normalized = str(target_type or "").strip().lower()
        if normalized in cls.DFT_TARGET_TYPES:
            return "dft"
        if normalized in cls.VISUAL_TARGET_TYPES:
            return "visual"
        if normalized:
            return "content"
        return "other"

    @classmethod
    def _is_dft_target_type(cls, target_type: Any) -> bool:
        try:
            normalized = canonical_target_type(str(target_type or ""))
        except ValueError:
            normalized = str(target_type or "").strip().lower()
        return normalized in cls.DFT_TARGET_TYPES

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
