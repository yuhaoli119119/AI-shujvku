from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from app.db.models import Paper, PaperFigure, PaperTable
from app.services.dft_rescan_policy import build_dft_dedupe_signature, normalize_source_document_type
from app.services.external_analysis_models import (
    ExternalAnalysisNormalizedModel,
    ExternalCorrectionProposalModel,
    ExternalObjectReviewAuditModel,
    ExternalReviewNoteModel,
    ExternalSupportingPaperModel,
)
from app.services.paper_identity import PaperIdentityService
from app.utils.library_names import build_library_name_clause


class ExternalAnalysisNormalizationMixin:
    def _normalize_input(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source_paper: Paper | None = None,
    ) -> tuple[ExternalAnalysisNormalizedModel | None, str, str | None]:
        parsed = self._extract_structured_payload(raw_text=raw_text, raw_payload=raw_payload)
        if isinstance(parsed, dict):
            try:
                normalized = ExternalAnalysisNormalizedModel.model_validate(parsed)
                return self._post_process_normalized(normalized, parsed, source_paper=source_paper), "normalized", None
            except Exception:
                llm_normalized = self._llm_normalize(raw_text=raw_text, raw_payload=parsed)
                if llm_normalized:
                    return self._post_process_normalized(llm_normalized, parsed, source_paper=source_paper), "normalized_with_llm", None
                return self._post_process_normalized(
                    self._heuristic_normalize(parsed),
                    source_paper=source_paper,
                ), "heuristic", None

        if isinstance(parsed, list):
            return self._post_process_normalized(
                self._heuristic_normalize({"unmapped_items": parsed}),
                source_paper=source_paper,
            ), "heuristic", None

        if isinstance(parsed, str) and parsed.strip():
            llm_normalized = self._llm_normalize(raw_text=parsed, raw_payload=None)
            if llm_normalized:
                return self._post_process_normalized(llm_normalized, source_paper=source_paper), "normalized_with_llm", None
            return ExternalAnalysisNormalizedModel(
                review_notes=[ExternalReviewNoteModel(content=parsed, mapping_reason="Fallback free-text note import")]
            ), "free_text_fallback", None

        return ExternalAnalysisNormalizedModel(), "empty", None

    def _post_process_normalized(
        self,
        normalized: ExternalAnalysisNormalizedModel,
        raw_payload: dict[str, Any] | None = None,
        source_paper: Paper | None = None,
    ) -> ExternalAnalysisNormalizedModel:
        supporting = []
        for item in normalized.supporting_papers:
            resolved_target, resolution_reason = self._resolve_target_paper_id(item, source_paper=source_paper)
            mapping_reason = item.mapping_reason
            if resolution_reason and not resolved_target:
                mapping_reason = resolution_reason
            supporting.append(
                item.model_copy(
                    update={
                        "target_paper_id": resolved_target or item.target_paper_id,
                        "mapping_reason": mapping_reason,
                    }
                )
            )
        object_reviews = list(normalized.object_review_audits)
        if raw_payload is not None:
            existing_keys = {
                self._object_review_key(item.model_dump(mode="json")): index
                for index, item in enumerate(object_reviews)
            }
            for item in self._extract_object_review_audits(raw_payload):
                key = self._object_review_key(item)
                if key in existing_keys:
                    index = existing_keys[key]
                    if object_reviews[index].raw_payload is None:
                        object_reviews[index] = object_reviews[index].model_copy(update={"raw_payload": item})
                    continue
                object_reviews.append(ExternalObjectReviewAuditModel.model_validate(item))
                existing_keys[key] = len(object_reviews) - 1
        corrections = self._normalize_legacy_codex_item_corrections(
            normalized.correction_proposals,
            source_paper=source_paper,
        )
        return normalized.model_copy(
            update={
                "supporting_papers": supporting,
                "object_review_audits": object_reviews,
                "correction_proposals": corrections,
            }
        )

    def _normalize_legacy_codex_item_corrections(
        self,
        corrections: list[ExternalCorrectionProposalModel],
        *,
        source_paper: Paper | None,
    ) -> list[ExternalCorrectionProposalModel]:
        if not corrections or source_paper is None:
            return corrections
        normalized: list[ExternalCorrectionProposalModel] = []
        for correction in corrections:
            target_path = str(correction.target_path or "").strip()
            field_name = str(correction.field_name or "").strip()
            if not target_path.lower().startswith("codex_item:"):
                normalized.append(correction)
                continue
            item_id = target_path.split(":", 1)[1].strip()
            mapped = self._resolve_legacy_codex_item_target(
                paper_id=source_paper.id,
                item_id=item_id,
                field_name=field_name,
            )
            if mapped is None:
                normalized.append(correction)
                continue
            collection, attribute = mapped
            normalized.append(
                correction.model_copy(
                    update={
                        "field_name": collection,
                        "target_path": f"{collection}:{item_id}:{attribute}",
                        "mapping_reason": (
                            correction.mapping_reason
                            or f"Normalized legacy codex_item target to structured {collection} correction."
                        ),
                    }
                )
            )
        return normalized

    def _resolve_legacy_codex_item_target(
        self,
        *,
        paper_id: UUID,
        item_id: str,
        field_name: str,
    ) -> tuple[str, str] | None:
        normalized_field = str(field_name or "").strip().lower()
        try:
            item_uuid = UUID(item_id)
        except (TypeError, ValueError):
            return None
        table = self.session.get(PaperTable, item_uuid)
        if table is not None and table.paper_id == paper_id:
            table_fields = {"caption", "markdown_content", "page", "extraction_source", "prov"}
            return ("tables", normalized_field if normalized_field in table_fields else "markdown_content")
        figure = self.session.get(PaperFigure, item_uuid)
        if figure is not None and figure.paper_id == paper_id:
            figure_fields = {
                "caption",
                "image_path",
                "page",
                "figure_label",
                "figure_role",
                "role_confidence",
                "content_summary",
                "key_elements",
                "prov",
                "crop_status",
                "crop_confidence",
                "crop_source",
            }
            return ("figures", normalized_field if normalized_field in figure_fields else "content_summary")
        return None

    def _resolve_target_paper_id(
        self,
        relationship: ExternalSupportingPaperModel,
        *,
        source_paper: Paper | None = None,
    ) -> tuple[str | None, str | None]:
        if relationship.target_paper_id:
            return relationship.target_paper_id, None

        conditions = []
        if relationship.target_doi:
            normalized_doi = PaperIdentityService.normalize_doi(relationship.target_doi)
            if normalized_doi:
                conditions.append(Paper.doi == normalized_doi)
        if relationship.target_title:
            conditions.append(Paper.title.ilike(relationship.target_title))
        if not conditions:
            return None, None

        stmt = select(Paper).where(or_(*conditions))
        if source_paper is not None:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, source_paper.library_name))
            stmt = stmt.where(Paper.id != source_paper.id)
        stmt = stmt.order_by(Paper.created_at.asc(), Paper.id.asc()).limit(2)
        targets = self.session.scalars(stmt).all()
        if len(targets) == 1:
            return str(targets[0].id), None
        if len(targets) > 1:
            return None, "Relationship target is ambiguous within the source paper library; keep target_paper_id unresolved."
        return None, None

    def _extract_structured_payload(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
    ) -> dict[str, Any] | list[Any] | str | None:
        if isinstance(raw_payload, (dict, list)):
            return raw_payload
        if isinstance(raw_payload, str) and raw_payload.strip():
            parsed = self._try_parse_json(raw_payload)
            return parsed if parsed is not None else raw_payload
        if raw_text and raw_text.strip():
            parsed = self._try_parse_json(raw_text)
            return parsed if parsed is not None else raw_text
        return None

    def _llm_normalize(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | None,
    ) -> ExternalAnalysisNormalizedModel | None:
        source_blob = raw_text if raw_text else json.dumps(raw_payload, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a scientific data mapping assistant. Convert external AI analysis output into a safe intermediate "
            "schema with review_notes, correction_proposals, supporting_papers, and unmapped_items. "
            "Do not invent record ids. Only emit target_path when the input clearly specifies it. "
            "If a relationship target paper cannot be matched, keep target_paper_id null and preserve title/doi clues."
        )
        user_prompt = f"Normalize this external analysis output:\n\n{source_blob}"
        return self.llm.structured_extract(system_prompt, user_prompt, ExternalAnalysisNormalizedModel)

    def _heuristic_normalize(self, payload: dict[str, Any]) -> ExternalAnalysisNormalizedModel:
        notes = payload.get("review_notes") or payload.get("notes") or []
        corrections = payload.get("correction_proposals") or payload.get("corrections") or []
        supporting = payload.get("supporting_papers") or payload.get("relationships") or []
        object_reviews = self._extract_object_review_audits(payload)
        unmapped = payload.get("unmapped_items") or []
        candidate_items = payload.get("candidates")
        if isinstance(candidate_items, dict):
            candidate_items = [candidate_items]
        if isinstance(candidate_items, list):
            notes = list(notes) if isinstance(notes, list) else [notes]
            corrections = list(corrections) if isinstance(corrections, list) else [corrections]
            supporting = list(supporting) if isinstance(supporting, list) else [supporting]
            unmapped = list(unmapped) if isinstance(unmapped, list) else [unmapped]
            for item in candidate_items:
                if not isinstance(item, dict):
                    unmapped.append({"raw_item": str(item), "mapping_reason": "Unsupported candidates item"})
                    continue
                kind = str(item.get("candidate_type") or item.get("type") or "").strip().lower()
                if kind in {"paper_note", "note", "review_note"}:
                    notes.append(
                        {
                            "content": item.get("content") or item.get("summary") or item.get("reason") or "",
                            "field_name": item.get("field_name"),
                            "page": item.get("page"),
                            "section_title": item.get("section_title"),
                            "quoted_text": item.get("quoted_text"),
                            "confidence": item.get("confidence"),
                            "mapping_reason": item.get("mapping_reason") or "Mapped from raw_payload.candidates",
                        }
                    )
                elif kind in {"correction", "correction_proposal"}:
                    corrections.append(item)
                elif kind in {"relationship", "supporting_paper"}:
                    supporting.append(item)
                elif kind in {"object_review_audit", "object_review", "field_review"} and self._is_object_review_item(item):
                    object_reviews.append(self._normalize_object_review_item(item))
                else:
                    unmapped.append({"raw_payload": item, "mapping_reason": "Unrecognized raw_payload.candidates item"})

        if not any([notes, corrections, supporting, object_reviews, unmapped]):
            unmapped = [{"raw_payload": payload}]

        return ExternalAnalysisNormalizedModel(
            review_notes=[
                ExternalReviewNoteModel.model_validate(item if isinstance(item, dict) else {"content": str(item)})
                for item in notes
            ],
            correction_proposals=[
                ExternalCorrectionProposalModel.model_validate(item) for item in corrections if isinstance(item, dict)
            ],
            supporting_papers=[
                ExternalSupportingPaperModel.model_validate(item) for item in supporting if isinstance(item, dict)
            ],
            object_review_audits=[
                ExternalObjectReviewAuditModel.model_validate(item) for item in object_reviews if isinstance(item, dict)
            ],
            unmapped_items=[item if isinstance(item, dict) else {"raw_item": str(item)} for item in unmapped],
        )

    @staticmethod
    def _extract_object_review_audits(payload: dict[str, Any]) -> list[dict[str, Any]]:
        explicit = payload.get("object_review_audits") or payload.get("object_reviews") or payload.get("field_reviews")
        if isinstance(explicit, dict):
            explicit = [explicit]
        if isinstance(explicit, list):
            return [
                ExternalAnalysisNormalizationMixin._normalize_object_review_item(item)
                for item in explicit
                if isinstance(item, dict) and ExternalAnalysisNormalizationMixin._is_object_review_item(item)
            ]

        candidates: list[dict[str, Any]] = []
        for key in ("reviews", "audits", "opinions", "items"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and ExternalAnalysisNormalizationMixin._is_object_review_item(item):
                    candidates.append(ExternalAnalysisNormalizationMixin._normalize_object_review_item(item))
        if ExternalAnalysisNormalizationMixin._is_object_review_item(payload):
            candidates.append(ExternalAnalysisNormalizationMixin._normalize_object_review_item(payload))
        return candidates

    @classmethod
    def _is_unrecognized_object_review_container(cls, key: str, value: Any) -> bool:
        normalized_key = str(key or "").strip().lower()
        if normalized_key in cls.OBJECT_REVIEW_CONTAINER_KEYS | cls.GENERIC_OBJECT_REVIEW_CONTAINER_KEYS:
            return False
        if not isinstance(value, (dict, list)) or not value:
            return False
        suspicious = normalized_key in {
            "dft_result_audits",
            "dft_results_audits",
            "dft_audits",
            "dft_result_reviews",
            "dft_results_reviews",
            "dft_reviews",
        } or normalized_key.endswith(("_audits", "_reviews"))
        if not suspicious:
            return False
        items = value if isinstance(value, list) else [value]
        return any(isinstance(item, dict) for item in items)

    @staticmethod
    def _normalize_dft_warning_target_type(value: Any) -> str:
        target_type = str(value or "").strip().lower()
        if target_type in {"dft_result", "dft_results", "dftresult"}:
            return "dft_results"
        return target_type

    @classmethod
    def _normalize_dft_review_decision_for_warning(cls, value: Any) -> str:
        decision = str(value or "").strip().upper()
        return cls.DFT_REVIEW_DECISION_ALIASES.get(decision, decision)

    @staticmethod
    def _is_object_review_item(item: dict[str, Any]) -> bool:
        decision = str(item.get("decision") or item.get("verdict") or "").strip().lower()
        target_type = str(item.get("target_type") or "").strip().lower()
        if target_type in {"dft_results", "dft_result"} and decision == "new_candidate" and item.get("corrected_value"):
            return True
        return bool(
            (item.get("target_type") or item.get("target_path"))
            and (item.get("target_id") or item.get("target_path") or item.get("dft_result_id") or item.get("record_id"))
            and (item.get("field_name") or item.get("target_path") or item.get("field"))
            and any(key in item for key in ("decision", "verdict", "recommended_action", "corrected_value", "proposed_value", "evidence_checked"))
        )

    @staticmethod
    def _normalize_object_review_item(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        target_path = normalized.get("target_path")
        if isinstance(target_path, str):
            match = re.match(r"^([^:]+):([^:]+):([^:]+)$", target_path)
            if match:
                normalized.setdefault("target_type", match.group(1))
                normalized.setdefault("target_id", match.group(2))
                normalized.setdefault("field_name", match.group(3))
        if "field_name" not in normalized and "field" in normalized:
            normalized["field_name"] = normalized.get("field")
        if "target_id" not in normalized:
            normalized["target_id"] = normalized.get("dft_result_id") or normalized.get("record_id")
        decision_text = str(normalized.get("decision") or normalized.get("verdict") or "").strip().lower()
        target_type_text = str(normalized.get("target_type") or "").strip().lower()
        if not normalized.get("target_id") and target_type_text in {"dft_results", "dft_result"} and decision_text == "new_candidate":
            normalized["target_id"] = "new"
        if "field_name" not in normalized and target_type_text in {"dft_results", "dft_result"} and decision_text == "new_candidate":
            normalized["field_name"] = "dft_results"
        if "decision" not in normalized and "verdict" in normalized:
            normalized["decision"] = normalized.get("verdict")
        evidence_location = normalized.get("evidence_location")
        corrected_value = normalized.get("corrected_value") or normalized.get("proposed_value")
        if isinstance(evidence_location, dict):
            evidence_location = dict(evidence_location)
            evidence_location["source_document_type"] = normalize_source_document_type(
                evidence_location.get("source_document_type")
            )
            normalized["evidence_location"] = evidence_location
        if target_type_text in {"dft_results", "dft_result"}:
            source_type = normalize_source_document_type(
                (evidence_location or {}).get("source_document_type") if isinstance(evidence_location, dict) else None
            )
            if source_type == "supporting_reference":
                normalized["borrowed_from_reference"] = True
            signature_payload = {
                **normalized,
                "corrected_value": corrected_value if isinstance(corrected_value, dict) else {},
                "evidence_location": evidence_location if isinstance(evidence_location, dict) else {},
            }
            normalized["dedupe_signature"] = normalized.get("dedupe_signature") or build_dft_dedupe_signature(
                signature_payload
            )
        if "corrected_value" not in normalized and "proposed_value" in normalized:
            normalized["corrected_value"] = normalized.get("proposed_value")
        if "reason" not in normalized:
            normalized["reason"] = normalized.get("reviewer_note") or normalized.get("mapping_reason")
        blocking = normalized.get("blocking_errors") or normalized.get("blocking_error") or []
        if isinstance(blocking, (str, int, float, bool)):
            blocking = [blocking]
        normalized["blocking_errors"] = blocking if isinstance(blocking, list) else []
        normalized["writes_final_truth"] = False
        normalized["human_confirmation_required"] = True
        normalized["verification_status"] = "unverified"
        normalized["status"] = "candidate"
        normalized.setdefault("raw_payload", item)
        return normalized

    @staticmethod
    def _object_review_key(item: dict[str, Any]) -> str:
        return "|".join(
            str(item.get(key) or "")
            for key in ("paper_id", "target_type", "target_id", "field_name", "decision", "corrected_value")
        )

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S)
        candidates.extend(item.strip() for item in fenced if item.strip())
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None
