from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import CatalystSample, DFTResult, MechanismClaim, PaperFigure, PaperTable, WritingCard
from app.utils.figure_summary import normalize_figure_key_elements


class ReviewConflictTargetMixin:
    """Target loading and target-summary helpers for review conflict aggregation."""

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
