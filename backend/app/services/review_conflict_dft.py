from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import CatalystSample, DFTResult


DFT_EXPLICIT_BLANK = "__explicit_blank__"


class ReviewConflictDftMixin:
    """DFT-specific comparison helpers for review conflict aggregation."""

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
