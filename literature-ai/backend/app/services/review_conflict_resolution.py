from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import DFTResult, ExtractionFieldReview, PaperCorrection


ACTIVE_EXTERNAL_AUDIT_STATUSES = {"candidate", "pending", "requires_resolution"}
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


class ReviewConflictResolutionMixin:
    """Conflict collapsing, settlement, and type-classification helpers."""

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

    def _group_opinions(self, opinions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in opinions:
            key = "|".join(
                [
                    str(item["paper_id"]),
                    str(item["target_type"]),
                    str(item["target_id"]),
                    str(item["field_name"]),
                ]
            )
            groups.setdefault(key, []).append(item)
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
