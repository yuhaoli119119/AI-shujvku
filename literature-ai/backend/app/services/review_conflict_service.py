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
    CatalystSample,
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
from app.utils.figure_summary import normalize_figure_key_elements


DECISION_POSITIVE = {"PASS", "ACCEPT", "APPROVE", "APPROVED", "VERIFIED", "OK"}
DECISION_NEGATIVE = {"REVISE", "FLAG", "INSUFFICIENT", "REJECT", "REJECTED", "NEEDS_FIX", "FIX", "BLOCK", "PROPOSED"}
CORRECTION_STATUSES = {"pending", "rejected", "approved"}
ACTIVE_EXTERNAL_AUDIT_STATUSES = {"candidate", "pending", "requires_resolution"}
DFT_EXPLICIT_BLANK = "__explicit_blank__"
DFT_CONFLICT_FIELD_MAP = {
    "value_conflict": "value",
    "unit_conflict": "unit",
    "property_conflict": "property_type",
    "material_conflict": "catalyst",
    "structure_name_conflict": "structure_name",
    "adsorbate_conflict": "adsorbate",
    "reaction_step_conflict": "reaction_step",
}
DFT_SETTLED_REVIEW_STATUSES = {"verified", "rejected"}
DFT_SAFE_REVIEW_RESOLUTION_STATUSES = {"active", "remapped"}
DFT_SETTLEMENT_FIELD_ALIASES = {
    "property": "energy_type",
    "property_type": "energy_type",
    "energy": "energy_type",
    "energy_type": "energy_type",
    "value": "value",
    "unit": "unit",
    "catalyst": "catalyst",
    "catalyst_id": "catalyst",
    "catalyst_sample": "catalyst",
    "catalyst_sample_id": "catalyst",
    "material": "catalyst",
    "material_identity": "catalyst",
    "structure_name": "catalyst",
    "adsorbate": "adsorbate",
    "reaction_step": "reaction_step",
}


class ReviewConflictAggregationService:
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

    def _collapse_adopted_opinions(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        collapsed = self._collapse_repeated_source_opinions(opinions)
        if len(collapsed) < 2:
            return collapsed
        approved_corrections = [
            item for item in collapsed
            if str(item.get("source_type") or "") == "paper_correction"
            and str(item.get("status") or "").strip().lower() == "approved"
        ]
        if approved_corrections:
            adopted_source_ids: set[str] = set()
            for correction in approved_corrections:
                for opinion in collapsed:
                    if opinion is correction:
                        continue
                    if str(opinion.get("source_type") or "") == "paper_correction":
                        continue
                    if self._approved_correction_adopts_opinion(correction, opinion):
                        adopted_source_ids.add(str(opinion.get("source_id") or ""))
            if adopted_source_ids:
                collapsed = [
                    item for item in collapsed
                    if str(item.get("source_id") or "") not in adopted_source_ids
                ]
        collapsed = self._collapse_pending_corrections_absorbed_by_approved_corrections(collapsed)
        collapsed = self._collapse_dft_target_state_adopted_opinions(collapsed)
        return self._collapse_rejected_dft_replacement_adopted_opinions(collapsed)

    def _active_opinions(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item
            for item in opinions
            if str(item.get("source_type") or "") not in {"external_audit_opinion", "object_review_audit"}
            or str(item.get("status") or "").strip().lower() in ACTIVE_EXTERNAL_AUDIT_STATUSES
        ]

    def _exclude_rejected_dft_targets(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for item in opinions:
            if self._safe_canonical_target_type(str(item.get("target_type") or "")) != "dft_results":
                active.append(item)
                continue
            target_id = str(item.get("target_id") or "").strip()
            target_row = self._load_target_row("dft_results", target_id) if target_id else None
            if (
                isinstance(target_row, DFTResult)
                and str(target_row.candidate_status or "").strip().lower() == "rejected"
            ):
                continue
            active.append(item)
        return active

    def _dft_conflict_group_is_settled(
        self,
        *,
        target_type: str,
        target_id: str,
        field_name: str,
        affected_field_names: list[str],
        conflict_types: list[str],
    ) -> bool:
        if self._safe_canonical_target_type(target_type) != "dft_results":
            return False
        target_row = self._load_target_row("dft_results", target_id)
        if not isinstance(target_row, DFTResult):
            return False
        if str(getattr(target_row, "candidate_status", "") or "").strip().lower() == "rejected":
            return True
        fields = self._dft_settlement_fields(
            field_name=field_name,
            affected_field_names=affected_field_names,
            conflict_types=conflict_types,
        )
        if not fields:
            return False
        settled_fields = self._settled_dft_review_fields(target_row, fields)
        if fields.issubset(settled_fields):
            return True
        settled_fields.update(self._approved_dft_correction_fields(target_row, fields))
        return fields.issubset(settled_fields)

    def _dft_settlement_fields(
        self,
        *,
        field_name: str,
        affected_field_names: list[str],
        conflict_types: list[str],
    ) -> set[str]:
        raw_fields: list[str] = []
        for name in affected_field_names:
            raw_fields.append(str(name or "").strip())
        for conflict_type in conflict_types:
            mapped = DFT_CONFLICT_FIELD_MAP.get(str(conflict_type or "").strip())
            if mapped:
                raw_fields.append(mapped)
        if field_name and field_name != "dft_results":
            raw_fields.append(field_name)

        fields: set[str] = set()
        for raw in raw_fields:
            normalized = DFT_SETTLEMENT_FIELD_ALIASES.get(str(raw or "").strip().lower())
            if normalized:
                fields.add(normalized)
        return fields

    def _settled_dft_review_fields(self, target_row: DFTResult, fields: set[str]) -> set[str]:
        rows = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == target_row.paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(target_row.id),
                ExtractionFieldReview.field_name.in_(sorted(fields)),
            )
        ).all()
        settled: set[str] = set()
        for row in rows:
            status = str(row.reviewer_status or "").strip().lower()
            resolution_status = str(row.target_resolution_status or "").strip().lower()
            if status in DFT_SETTLED_REVIEW_STATUSES and resolution_status in DFT_SAFE_REVIEW_RESOLUTION_STATUSES:
                normalized = DFT_SETTLEMENT_FIELD_ALIASES.get(str(row.field_name or "").strip().lower())
                if normalized:
                    settled.add(normalized)
        return settled

    def _approved_dft_correction_fields(self, target_row: DFTResult, fields: set[str]) -> set[str]:
        rows = self.session.scalars(
            select(PaperCorrection).where(
                PaperCorrection.paper_id == target_row.paper_id,
                PaperCorrection.status == "approved",
            )
        ).all()
        settled: set[str] = set()
        for row in rows:
            parsed = self._correction_target(row)
            if parsed is None:
                continue
            _paper_id, target_type, target_id, correction_field = parsed
            if target_type != "dft_results" or target_id != str(target_row.id):
                continue
            normalized = DFT_SETTLEMENT_FIELD_ALIASES.get(str(correction_field or "").strip().lower())
            if normalized in fields:
                settled.add(normalized)
        return settled

    def _collapse_repeated_source_opinions(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_by_source: dict[str, dict[str, Any]] = {}
        for item in opinions:
            source_identity = next(
                (
                    self._norm(value)
                    for value in (
                        item.get("source_label"),
                        item.get("source"),
                        item.get("reviewer"),
                        item.get("model_name"),
                    )
                    if self._norm(value)
                ),
                str(item.get("source_id") or id(item)),
            )
            current = latest_by_source.get(source_identity)
            if current is None or self._opinion_recency_key(item) >= self._opinion_recency_key(current):
                latest_by_source[source_identity] = item
        return list(latest_by_source.values())

    def _collapse_active_dft_adjudication(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not opinions:
            return opinions
        target_type = self._safe_canonical_target_type(str(opinions[0].get("target_type") or ""))
        if target_type != "dft_results":
            return opinions
        adjudications = [
            item
            for item in opinions
            if str((item.get("raw_payload") or {}).get("adjudication_role") or item.get("adjudication_role") or "").strip().lower()
            == "third_ai"
        ]
        if not adjudications:
            return opinions
        return [max(adjudications, key=self._opinion_recency_key)]

    @staticmethod
    def _opinion_recency_key(opinion: dict[str, Any]) -> tuple[str, float, str]:
        return (
            str(opinion.get("created_at") or ""),
            float(opinion.get("confidence") or 0),
            str(opinion.get("source_id") or ""),
        )

    def _collapse_pending_corrections_absorbed_by_approved_corrections(
        self,
        opinions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if len(opinions) < 2:
            return opinions
        approved_corrections = [
            item for item in opinions
            if str(item.get("source_type") or "") == "paper_correction"
            and str(item.get("status") or "").strip().lower() == "approved"
        ]
        if not approved_corrections:
            return opinions
        adopted_source_ids: set[str] = set()
        for correction in approved_corrections:
            if not self._opinion_matches_current_target_state(correction):
                continue
            for opinion in opinions:
                if opinion is correction:
                    continue
                if str(opinion.get("source_type") or "") != "paper_correction":
                    continue
                if str(opinion.get("status") or "").strip().lower() != "pending":
                    continue
                if not self._same_opinion_target(correction, opinion):
                    continue
                if not self._opinion_values_match(correction, opinion):
                    continue
                if not self._dft_scalar_correction_can_adopt_opinion(correction, opinion):
                    continue
                adopted_source_ids.add(str(opinion.get("source_id") or ""))
        if not adopted_source_ids:
            return opinions
        return [
            item for item in opinions
            if str(item.get("source_id") or "") not in adopted_source_ids
        ]

    def _collapse_dft_target_state_adopted_opinions(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(opinions) < 2:
            return opinions
        first = opinions[0]
        target_type = self._safe_canonical_target_type(str(first.get("target_type") or ""))
        if target_type != "dft_results":
            return opinions
        target_id = str(first.get("target_id") or "").strip()
        if not target_id or any(str(item.get("target_id") or "").strip() != target_id for item in opinions):
            return opinions
        target_row = self._load_target_row(target_type, target_id)
        if not isinstance(target_row, DFTResult):
            return opinions
        has_finalized_review = any(
            str(item.get("source_type") or "") == "extraction_field_review"
            and self._decision_bucket(item.get("decision") or item.get("status")) == "positive"
            for item in opinions
        )
        if not has_finalized_review:
            return opinions
        adopted_source_ids = {
            str(item.get("source_id") or "")
            for item in opinions
            if str(item.get("source_type") or "") in {"external_audit_opinion", "object_review_audit"}
            and self._dft_target_matches_opinion(target_row, item)
        }
        if not adopted_source_ids:
            return opinions
        return [
            item for item in opinions
            if str(item.get("source_id") or "") not in adopted_source_ids
        ]

    def _collapse_rejected_dft_replacement_adopted_opinions(self, opinions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(opinions) < 2:
            return opinions
        first = opinions[0]
        target_type = self._safe_canonical_target_type(str(first.get("target_type") or ""))
        if target_type != "dft_results":
            return opinions
        target_id = str(first.get("target_id") or "").strip()
        if not target_id or any(str(item.get("target_id") or "").strip() != target_id for item in opinions):
            return opinions
        target_row = self._load_target_row(target_type, target_id)
        if not isinstance(target_row, DFTResult):
            return opinions
        if str(getattr(target_row, "candidate_status", "") or "").strip().lower() != "rejected":
            return opinions
        adopted_source_ids = {
            str(item.get("source_id") or "")
            for item in opinions
            if str(item.get("source_type") or "") in {"external_audit_opinion", "object_review_audit"}
            and self._dft_opinion_matches_replacement_row(opinion=item, target_row=target_row)
        }
        if not adopted_source_ids:
            return opinions
        return [
            item for item in opinions
            if str(item.get("source_id") or "") not in adopted_source_ids
        ]

    def _approved_correction_adopts_opinion(self, correction: dict[str, Any], opinion: dict[str, Any]) -> bool:
        evidence = correction.get("evidence")
        if not isinstance(evidence, dict):
            return False
        selected_source_ids = evidence.get("selected_source_ids")
        selected = isinstance(selected_source_ids, list) and str(opinion.get("source_id") or "") in {str(value) for value in selected_source_ids}
        if not selected:
            fallback_settled = (
                self._same_opinion_target(correction, opinion)
                and self._opinion_matches_current_target_state(correction)
                and self._opinion_matches_current_target_state(opinion)
            )
            review_source = str(evidence.get("review_source") or "").strip().lower()
            review_source_label = str(evidence.get("review_source_label") or "").strip().lower()
            review_decision = str(evidence.get("review_decision") or "").strip().upper()
            if not review_source and not review_source_label and not review_decision:
                # Older approved corrections do not always persist the adopted
                # source metadata. If the approved value already matches both
                # the current target state and the opinion, treat it as settled.
                if not fallback_settled:
                    return False
            else:
                opinion_source = str(opinion.get("source") or "").strip().lower()
                opinion_source_label = str(opinion.get("source_label") or "").strip().lower()
                opinion_decision = str(opinion.get("decision") or "").strip().upper()
                metadata_matches = True
                if review_source and review_source != opinion_source:
                    metadata_matches = False
                if review_source_label and review_source_label != opinion_source_label:
                    metadata_matches = False
                if review_decision and review_decision != opinion_decision:
                    metadata_matches = False
                if not metadata_matches and not fallback_settled:
                    return False
        if not self._opinion_values_match(correction, opinion):
            return False
        if not self._dft_scalar_correction_can_adopt_opinion(correction, opinion):
            return False
        correction_created = correction.get("created_at")
        opinion_created = opinion.get("created_at")
        if correction_created and opinion_created and correction_created < opinion_created:
            return False
        return True

    def _dft_opinion_matches_replacement_row(self, *, opinion: dict[str, Any], target_row: DFTResult) -> bool:
        if not self._is_structured_dft_value_opinion(opinion):
            return False
        siblings = self.session.scalars(
            select(DFTResult).where(
                DFTResult.paper_id == target_row.paper_id,
                DFTResult.id != target_row.id,
            )
        ).all()
        for sibling in siblings:
            candidate_status = str(getattr(sibling, "candidate_status", "") or "").strip().lower()
            if candidate_status in {"rejected", "system_candidate", "needs_human_confirmation"}:
                continue
            if self._dft_target_matches_opinion(sibling, opinion):
                return True
            if self._dft_replacement_row_semantically_matches_opinion(sibling, opinion):
                return True
        return False

    def _dft_replacement_row_semantically_matches_opinion(self, row: DFTResult, opinion: dict[str, Any]) -> bool:
        if not self._dft_value_matches_row(opinion, target_row=row):
            return False
        if not self._dft_replacement_field_compatible(opinion, "property", row):
            return False
        if not self._dft_replacement_field_compatible(opinion, "adsorbate", row):
            return False
        if not self._dft_replacement_field_compatible(opinion, "reaction_step", row):
            return False
        return True

    def _dft_replacement_field_compatible(self, opinion: dict[str, Any], field_name: str, row: DFTResult) -> bool:
        present, opinion_value = self._dft_field_state(opinion, field_name, target_row=None)
        if not present:
            return True
        row_value = self._dft_row_field_key(row, field_name)
        if opinion_value == row_value:
            return True
        if field_name in {"adsorbate", "reaction_step"}:
            return self._normalized_dft_text_overlap(opinion_value, row_value)
        return False

    @staticmethod
    def _normalized_dft_text_overlap(left: str, right: str) -> bool:
        def tokens(value: str) -> set[str]:
            normalized = normalize(value)
            return {
                token
                for token in normalized.replace("-", " ").split()
                if len(token) >= 3
            }

        def normalize(value: str) -> str:
            text = str(value or "").strip().lower()
            text = text.replace("*", "")
            text = " ".join(text.split())
            return text

        left_norm = normalize(left)
        right_norm = normalize(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm or left_norm in right_norm or right_norm in left_norm:
            return True
        overlap = tokens(left_norm) & tokens(right_norm)
        return len(overlap) >= 2

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
                        # The persisted candidate lifecycle is authoritative. Raw
                        # payloads often retain their original "candidate" value
                        # after the opinion has already been materialized.
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
        dft_target = self._dft_conflict_target(opinions)
        if dft_target is not None:
            value_keys = {item["value_key"] for item in dft_target if item["value_key"]}
            unit_keys = {item["unit_key"] for item in dft_target if item["unit_key"]}
            decision_keys = {item["decision_bucket"] for item in dft_target}
        else:
            value_keys = {self._value_key(self._comparison_value(item)) for item in opinions if not self._is_blank(self._comparison_value(item))}
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
        is_dft_target = self._safe_canonical_target_type(str(opinions[0].get("target_type") or "")) == "dft_results"
        if not is_dft_target and len(locator_keys) > 1:
            conflict_types.append("locator_conflict")
        if len(mapping_keys) > 1:
            conflict_types.append("mapping_conflict")
        if dft_target is not None:
            target_id = str(opinions[0].get("target_id") or "").strip()
            target_row = self._load_target_row("dft_results", target_id) if target_id else None
            has_finalized_truth = any(
                (
                    str(item.get("source_type") or "") == "extraction_field_review"
                    and self._decision_bucket(item.get("decision") or item.get("status")) == "positive"
                )
                or (
                    str(item.get("source_type") or "") == "paper_correction"
                    and str(item.get("status") or "").strip().lower() == "approved"
                )
                for item in opinions
            )
            for field_name, conflict_name in (
                ("property", "property_conflict"),
                ("material", "material_conflict"),
                ("structure_name", "structure_name_conflict"),
                ("adsorbate", "adsorbate_conflict"),
                ("reaction_step", "reaction_step_conflict"),
            ):
                field_keys = {item[field_name] for item in dft_target if item[field_name]}
                if isinstance(target_row, DFTResult) and has_finalized_truth and field_keys:
                    field_keys.add(self._dft_row_field_key(target_row, field_name))
                if len(field_keys) > 1:
                    conflict_types.append(conflict_name)
        if len(identity_keys) > 1:
            conflict_types.append("identity_conflict")
        return conflict_types

    def _affected_field_names(self, target_type: str, field_name: str, conflict_types: list[str]) -> list[str]:
        canonical_target = self._safe_canonical_target_type(target_type)
        if canonical_target != "dft_results":
            return [field_name] if field_name else []
        names: list[str] = []
        for conflict_type in conflict_types:
            mapped = DFT_CONFLICT_FIELD_MAP.get(str(conflict_type or "").strip())
            if mapped and mapped not in names:
                names.append(mapped)
        if not names and field_name:
            names.append(field_name)
        return names

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

    def _prefetch_target_rows(self, opinions: list[dict[str, Any]]) -> None:
        models = {
            "dft_results": DFTResult,
            "figures": PaperFigure,
            "tables": PaperTable,
            "mechanism_claims": MechanismClaim,
            "writing_cards": WritingCard,
        }
        target_ids_by_type: dict[str, set[UUID]] = defaultdict(set)
        for opinion in opinions:
            target_type = self._safe_canonical_target_type(str(opinion.get("target_type") or ""))
            target_id = str(opinion.get("target_id") or "").strip()
            if target_type not in models or not target_id:
                continue
            try:
                target_ids_by_type[target_type].add(UUID(target_id))
            except (TypeError, ValueError):
                self._target_cache[(target_type, target_id)] = None
        for target_type, ids in target_ids_by_type.items():
            missing_ids = {
                target_id
                for target_id in ids
                if (target_type, str(target_id)) not in self._target_cache
            }
            if not missing_ids:
                continue
            model = models[target_type]
            rows = self.session.scalars(select(model).where(model.id.in_(missing_ids))).all()
            found_ids = set()
            for row in rows:
                row_id = getattr(row, "id", None)
                if row_id is None:
                    continue
                found_ids.add(row_id)
                self._target_cache[(target_type, str(row_id))] = row
            for missing_id in missing_ids - found_ids:
                self._target_cache[(target_type, str(missing_id))] = None
        catalyst_ids = {
            row.catalyst_sample_id
            for (target_type, _target_id), row in self._target_cache.items()
            if target_type == "dft_results"
            and isinstance(row, DFTResult)
            and row.catalyst_sample_id is not None
            and str(row.catalyst_sample_id) not in self._catalyst_cache
        }
        if catalyst_ids:
            catalysts = self.session.scalars(
                select(CatalystSample).where(CatalystSample.id.in_(catalyst_ids))
            ).all()
            found_catalyst_ids = set()
            for sample in catalysts:
                found_catalyst_ids.add(sample.id)
                self._catalyst_cache[str(sample.id)] = sample
            for missing_id in catalyst_ids - found_catalyst_ids:
                self._catalyst_cache[str(missing_id)] = None

    def _dft_target_matches_opinion(self, row: DFTResult, opinion: dict[str, Any]) -> bool:
        proposed = self._dft_structured_value_payload(opinion)
        proposed_value = proposed.get("value") if isinstance(proposed, dict) else opinion.get("value")
        if not self._numeric_values_match(getattr(row, "value", None), proposed_value):
            return False

        proposed_unit = self._dft_unit_value(opinion, target_row=row)
        if proposed_unit and self._norm(getattr(row, "unit", None)) != proposed_unit:
            return False

        for field_name in ("property", "adsorbate", "reaction_step", "structure_name", "material"):
            if not self._dft_field_matches_row(opinion, field_name, target_row=row):
                return False
        return True

    def _opinion_matches_current_target_state(self, opinion: dict[str, Any]) -> bool:
        target_type = self._safe_canonical_target_type(str(opinion.get("target_type") or ""))
        target_id = str(opinion.get("target_id") or "").strip()
        field_name = str(opinion.get("field_name") or "").strip().lower()
        if not target_type or not target_id or not field_name:
            return False
        target_row = self._load_target_row(target_type, target_id)
        if target_row is None:
            return False
        if target_type == "dft_results":
            if field_name == "value":
                return self._dft_value_matches_row(opinion, target_row=target_row)
            if field_name == "unit":
                return self._norm(getattr(target_row, "unit", None)) == self._norm(opinion.get("value"))
            if field_name in {"property", "property_type"}:
                return self._dft_row_field_key(target_row, "property") == self._dft_field_key_from_value(opinion.get("value"))
            if field_name == "adsorbate":
                return self._dft_row_field_key(target_row, "adsorbate") == self._dft_field_key_from_value(opinion.get("value"))
            if field_name == "reaction_step":
                return self._dft_row_field_key(target_row, "reaction_step") == self._dft_field_key_from_value(opinion.get("value"))
            if field_name == "structure_name":
                return self._dft_row_field_key(target_row, "structure_name") == self._dft_field_key_from_value(opinion.get("value"))
        return self._value_key(getattr(target_row, field_name, None)) == self._value_key(self._comparison_value(opinion))

    @staticmethod
    def _same_opinion_target(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return (
            str(left.get("paper_id") or "") == str(right.get("paper_id") or "")
            and str(left.get("target_type") or "") == str(right.get("target_type") or "")
            and str(left.get("target_id") or "") == str(right.get("target_id") or "")
            and str(left.get("field_name") or "") == str(right.get("field_name") or "")
        )

    def _opinion_values_match(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        target_type = self._safe_canonical_target_type(str(left.get("target_type") or right.get("target_type") or ""))
        field_name = str(left.get("field_name") or right.get("field_name") or "").strip().lower()
        if target_type == "dft_results" and field_name == "value":
            left_value = self._dft_numeric_value(left)
            right_value = self._dft_numeric_value(right)
            if self._is_blank(left_value) or self._is_blank(right_value):
                return self._value_key(left.get("value")) == self._value_key(right.get("value"))
            if not self._numeric_values_match(left_value, right_value):
                return False
            left_unit = self._dft_unit_value(left)
            right_unit = self._dft_unit_value(right)
            return self._dft_units_compatible(left_unit, right_unit)
        return self._value_key(self._comparison_value(left)) == self._value_key(self._comparison_value(right))

    def _comparison_value(self, opinion: dict[str, Any]) -> Any:
        target_type = self._safe_canonical_target_type(str(opinion.get("target_type") or ""))
        field_name = str(opinion.get("field_name") or "").strip().lower()
        value = opinion.get("value")
        if target_type == "figures" and field_name == "key_elements":
            normalized, _detail = normalize_figure_key_elements(value)
            return normalized
        return value

    def _dft_scalar_correction_can_adopt_opinion(self, correction: dict[str, Any], opinion: dict[str, Any]) -> bool:
        target_type = self._safe_canonical_target_type(str(correction.get("target_type") or opinion.get("target_type") or ""))
        field_name = str(correction.get("field_name") or opinion.get("field_name") or "").strip().lower()
        if target_type != "dft_results" or field_name != "value":
            return True
        if not self._is_structured_dft_value_opinion(opinion):
            return True
        if not self._dft_opinion_has_non_value_fields(opinion):
            return True
        target_id = str(correction.get("target_id") or opinion.get("target_id") or "").strip()
        target_row = self._load_target_row("dft_results", target_id) if target_id else None
        if not isinstance(target_row, DFTResult):
            return False
        return self._dft_non_value_fields_match_row(opinion, target_row=target_row)

    def _dft_conflict_target(self, opinions: list[dict[str, Any]]) -> list[dict[str, str]] | None:
        first = opinions[0]
        if self._safe_canonical_target_type(str(first.get("target_type") or "")) != "dft_results":
            return None
        if str(first.get("field_name") or "").strip().lower() not in {"value", "dft_results"}:
            return None
        target_id = str(first.get("target_id") or "").strip()
        target_row = self._load_target_row("dft_results", target_id) if target_id else None
        payloads: list[dict[str, str]] = []
        for opinion in opinions:
            value_key = self._dft_numeric_key(opinion)
            unit_key = self._dft_unit_value(opinion, target_row=target_row)
            payloads.append(
                {
                    "value_key": value_key,
                    "unit_key": unit_key,
                    "property": self._dft_explicit_field_value(opinion, "property"),
                    "material": self._dft_explicit_field_value(opinion, "material"),
                    "structure_name": self._dft_explicit_field_value(opinion, "structure_name"),
                    "adsorbate": self._dft_explicit_field_value(opinion, "adsorbate"),
                    "reaction_step": self._dft_explicit_field_value(opinion, "reaction_step"),
                    "decision_bucket": self._conflict_decision_bucket(opinion, target_row=target_row),
                }
            )
        return payloads

    def _dft_explicit_field_value(self, opinion: dict[str, Any], field_name: str) -> str:
        present, value = self._dft_field_state(opinion, field_name, target_row=None)
        return value if present else ""

    def _conflict_decision_bucket(self, opinion: dict[str, Any], *, target_row: DFTResult | None) -> str:
        normalized = str(opinion.get("decision") or opinion.get("status") or "").strip().upper()
        if (
            normalized == "PROPOSED"
            and self._is_structured_dft_value_opinion(opinion)
            and self._dft_value_matches_row(opinion, target_row=target_row)
            and self._dft_opinion_has_non_value_fields(opinion)
        ):
            return "neutral"
        return self._decision_bucket(normalized)

    @staticmethod
    def _structured_value_payload(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _dft_structured_value_payload(self, opinion: dict[str, Any]) -> dict[str, Any]:
        payload = self._structured_value_payload(opinion.get("value"))
        if payload:
            return payload
        raw_payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        return self._structured_value_payload(raw_payload.get("corrected_value"))

    def _is_structured_dft_value_opinion(self, opinion: dict[str, Any]) -> bool:
        return bool(self._dft_structured_value_payload(opinion))

    def _dft_numeric_value(self, opinion: dict[str, Any]) -> Any:
        structured = self._dft_structured_value_payload(opinion)
        if structured and "value" in structured:
            return structured.get("value")
        return opinion.get("value")

    def _dft_numeric_key(self, opinion: dict[str, Any]) -> str:
        value = self._dft_numeric_value(opinion)
        if self._is_blank(value):
            return ""
        return self._value_key(value)

    def _dft_unit_value(self, opinion: dict[str, Any], *, target_row: DFTResult | None = None) -> str:
        structured = self._dft_structured_value_payload(opinion)
        for candidate in (
            structured.get("unit") if structured else None,
            opinion.get("unit"),
            (getattr(target_row, "unit", None) if target_row is not None else None),
        ):
            normalized = self._norm(candidate)
            if normalized:
                return normalized
        return ""

    def _dft_units_compatible(self, left: Any, right: Any) -> bool:
        left_norm = self._norm(left)
        right_norm = self._norm(right)
        return not left_norm or not right_norm or left_norm == right_norm

    def _dft_value_matches_row(self, opinion: dict[str, Any], *, target_row: DFTResult | None) -> bool:
        if target_row is None:
            return False
        if not self._numeric_values_match(getattr(target_row, "value", None), self._dft_numeric_value(opinion)):
            return False
        return self._dft_units_compatible(self._dft_unit_value(opinion, target_row=None), getattr(target_row, "unit", None))

    def _dft_opinion_has_non_value_fields(self, opinion: dict[str, Any]) -> bool:
        for field_name in ("property", "material", "structure_name", "adsorbate", "reaction_step"):
            if self._dft_field_is_present(opinion, field_name):
                return True
        return False

    def _dft_field_value(
        self,
        opinion: dict[str, Any],
        field_name: str,
        *,
        target_row: DFTResult | None,
    ) -> str:
        _, value = self._dft_field_state(opinion, field_name, target_row=target_row)
        return value

    def _dft_field_is_present(self, opinion: dict[str, Any], field_name: str) -> bool:
        present, _ = self._dft_field_state(opinion, field_name, target_row=None)
        return present

    def _dft_field_matches_row(self, opinion: dict[str, Any], field_name: str, *, target_row: DFTResult) -> bool:
        present, value = self._dft_field_state(opinion, field_name, target_row=None)
        if not present:
            return True
        row_value = self._dft_row_field_key(target_row, field_name)
        if field_name == "material" and not row_value:
            return True
        return value == row_value

    def _dft_non_value_fields_match_row(self, opinion: dict[str, Any], *, target_row: DFTResult) -> bool:
        return all(
            self._dft_field_matches_row(opinion, field_name, target_row=target_row)
            for field_name in ("property", "material", "structure_name", "adsorbate", "reaction_step")
        )

    def _dft_field_state(
        self,
        opinion: dict[str, Any],
        field_name: str,
        *,
        target_row: DFTResult | None,
    ) -> tuple[bool, str]:
        structured = self._dft_structured_value_payload(opinion)
        raw_payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        structured_keys, raw_keys = self._dft_field_keys(field_name)
        present, value = self._dft_payload_field_state(structured, structured_keys, allow_blank=True)
        if present:
            return True, value
        present, value = self._dft_payload_field_state(
            raw_payload,
            raw_keys,
            allow_blank=not self._dft_has_nested_structured_payload(opinion),
        )
        if present:
            return True, value
        if target_row is None:
            return False, ""
        return False, self._dft_row_field_key(target_row, field_name)

    @staticmethod
    def _dft_field_keys(field_name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if field_name == "property":
            return (("property", "property_type"), ("normalized_energy_type", "property_type"))
        if field_name == "material":
            return (
                ("material_identity", "material", "normalized_material", "normalized_material_or_catalyst", "catalyst"),
                ("normalized_material", "normalized_material_or_catalyst", "material", "catalyst", "material_identity"),
            )
        if field_name == "structure_name":
            return (("structure_name",), ("structure_name",))
        if field_name == "adsorbate":
            return (("adsorbate",), ("adsorbate",))
        if field_name == "reaction_step":
            return (("reaction_step",), ("reaction_step",))
        return ((), ())

    def _dft_has_nested_structured_payload(self, opinion: dict[str, Any]) -> bool:
        if isinstance(opinion.get("value"), dict):
            return True
        raw_payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        return isinstance(raw_payload.get("corrected_value"), dict)

    @classmethod
    def _dft_payload_field_state(
        cls,
        payload: dict[str, Any],
        keys: tuple[str, ...],
        *,
        allow_blank: bool,
    ) -> tuple[bool, str]:
        if not isinstance(payload, dict):
            return False, ""
        for key in keys:
            if key in payload:
                value = cls._dft_field_key_from_value(payload.get(key))
                if allow_blank or value != DFT_EXPLICIT_BLANK:
                    return True, value
        return False, ""

    def _dft_row_field_key(self, row: DFTResult, field_name: str) -> str:
        if field_name == "property":
            return self._dft_field_key_from_value(getattr(row, "property_type", None))
        if field_name == "material":
            material_value = None
            catalyst_sample_id = getattr(row, "catalyst_sample_id", None)
            if catalyst_sample_id:
                sample_key = str(catalyst_sample_id)
                if sample_key in self._catalyst_cache:
                    sample = self._catalyst_cache[sample_key]
                else:
                    sample = self.session.get(CatalystSample, catalyst_sample_id)
                    self._catalyst_cache[sample_key] = sample
                material_value = sample.name if sample is not None else str(catalyst_sample_id)
            return self._norm(material_value)
        if field_name == "structure_name":
            return self._dft_field_key_from_value(getattr(row, "structure_name", None))
        if field_name == "adsorbate":
            return self._dft_field_key_from_value(getattr(row, "adsorbate", None))
        if field_name == "reaction_step":
            return self._dft_field_key_from_value(getattr(row, "reaction_step", None))
        return ""

    @classmethod
    def _dft_field_key_from_value(cls, value: Any) -> str:
        if value is None:
            return DFT_EXPLICIT_BLANK
        if isinstance(value, str):
            normalized = cls._norm(value)
            return normalized if normalized else DFT_EXPLICIT_BLANK
        if isinstance(value, (dict, list)):
            return cls._value_key(value)
        return cls._norm(value)

    @staticmethod
    def _numeric_values_match(left: Any, right: Any) -> bool:
        try:
            left_num = float(left)
            right_num = float(right)
        except (TypeError, ValueError):
            return False
        tolerance = max(1e-9, abs(left_num) * 1e-6, abs(right_num) * 1e-6)
        return abs(left_num - right_num) <= tolerance

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
