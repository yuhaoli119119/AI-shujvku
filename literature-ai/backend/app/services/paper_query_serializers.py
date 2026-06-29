from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    DFTResult,
    MechanismClaim,
    Paper,
    PaperFigure,
    PaperImpactMetadata,
    PaperNote,
    PaperRelationship,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.domain.catalyst_basic_info import catalyst_basic_info_payload
from app.schemas.api import (
    CatalystSampleResponse,
    DFTResultResponse,
    MechanismClaimResponse,
    PaperCountsResponse,
    PaperFigureResponse,
    PaperListItemResponse,
    PaperRelationshipItemResponse,
    PaperSectionResponse,
    PaperTableResponse,
    WritingCardResponse,
)
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.services.paper_query_storage import cached_pdf_size_for_storage
from app.utils.artifact_status import build_paper_pdf_status
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference, resolve_persisted_artifact_path
from app.utils.evidence_anchors import first_evidence_anchor
from app.utils.figure_delete_policy import direct_delete_eligibility, normalized_figure_identity
from app.utils.figure_reliability import build_figure_image_review
from app.utils.figure_summary import normalize_figure_key_elements
from app.utils.library_names import normalize_library_name
from app.utils.review_safety import writing_card_gate
from app.utils.text_cleaning import repair_mojibake_text


class PaperQuerySerializationMixin:
    @staticmethod
    def _serialize_paper_note(item: PaperNote) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "source": item.source,
            "content": repair_mojibake_text(item.content) or "",
            "field_name": repair_mojibake_text(item.field_name),
            "page": item.page,
            "section_title": repair_mojibake_text(item.section_title),
            "quoted_text": repair_mojibake_text(item.quoted_text),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }


    @classmethod
    def _is_display_body_section(cls, section: PaperSection) -> bool:
        return cls._is_display_body_section_values(section.section_type, section.section_title, section.text)


    @classmethod
    def _is_display_body_section_values(
        cls,
        section_type_value: str | None,
        section_title: str | None,
        section_text: str | None,
    ) -> bool:
        section_type = (section_type_value or "").strip().lower()
        if section_type in {"table", "figure", "figure_caption", "caption", "reference", "references", "deprecated_stale"}:
            return False

        title = cls._compact_section_text(section_title)
        text = cls._compact_section_text(section_text)
        title_lower = title.lower()
        text_lower = text.lower()
        if re.match(r"^page\s+\d+\b", title_lower):
            return False
        if title_lower.startswith("[deprecated]") or "replaced by" in title_lower:
            return False
        if re.match(r"^(fig(?:ure)?\.?|scheme|table)\s*\d+", title_lower):
            return False

        prefix = text_lower[:500]
        table_like_markers = (
            "donor nbo",
            "acceptor nbo",
            "homo",
            "lumo",
            "e homo",
            "e lumo",
            "gibbs free energy",
            "enthalpy",
            "entropy",
            "row:",
        )
        if title_lower in {"system", "row", "entry"} and any(marker in prefix for marker in table_like_markers):
            return False
        if prefix.count(" | ") >= 3:
            return False
        return bool(text)


    @staticmethod
    def _compact_section_text(value: str | None) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()


    @classmethod
    def _section_display_sort_key(cls, section: PaperSection) -> tuple[int, int, str]:
        section_type = (section.section_type or "").strip().lower()
        title = cls._compact_section_text(section.section_title).lower()
        type_rank = {
            "abstract": 0,
            "introduction": 1,
            "methods": 2,
            "method": 2,
            "experimental": 2,
            "computational": 2,
            "results": 3,
            "discussion": 3,
            "results and discussion": 3,
            "body": 4,
            "conclusion": 9,
            "conclusions": 9,
        }.get(section_type, 5)
        if "introduction" in title:
            type_rank = min(type_rank, 1)
        elif "method" in title or "computational" in title or "calculation" in title:
            type_rank = min(type_rank, 2)
        elif "result" in title or "discussion" in title:
            type_rank = min(type_rank, 3)
        elif "conclusion" in title:
            type_rank = 9
        page_rank = section.page_start if section.page_start is not None else 9999
        return (type_rank, page_rank, title)


    @classmethod
    def _serialize_section(cls, item: PaperSection) -> PaperSectionResponse:
        return PaperSectionResponse(
            id=item.id,
            section_title=cls._clean_pdf_text(item.section_title),
            section_type=item.section_type,
            text=cls._clean_pdf_text(item.text) or "",
            page_start=item.page_start,
            page_end=item.page_end,
            section_level=item.section_level,
            section_number=item.section_number,
            parent_heading=cls._clean_pdf_text(item.parent_heading),
            heading_path=[cls._clean_pdf_text(value) or "" for value in (item.heading_path or []) if cls._clean_pdf_text(value)],
        )


    @classmethod
    def _serialize_table(
        cls,
        item: PaperTable,
        *,
        object_review_audits: list[dict[str, Any]] | None = None,
        corrections: list[dict[str, Any]] | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> PaperTableResponse:
        payload = PaperTableResponse.model_validate(item)
        audits = object_review_audits or []
        table_corrections = corrections or []
        updates = {
            "caption": cls._clean_pdf_text(payload.caption),
            "markdown_content": cls._clean_pdf_layout_text(payload.markdown_content),
            "table_review_status": cls._table_review_status(
                audits,
                table_corrections,
                has_content=bool(str(payload.markdown_content or "").strip()),
            ),
            "object_review_audit_count": len(audits),
            "object_review_audits": audits[:5],
            "latest_object_review_audit": audits[0] if audits else None,
        }
        if source_metadata:
            updates.update(source_metadata)
        return payload.model_copy(
            update=updates
        )


    @staticmethod
    def _table_review_status(
        audits: list[dict[str, Any]],
        corrections: list[dict[str, Any]] | None = None,
        *,
        has_content: bool = True,
    ) -> str:
        corrections = corrections or []
        if corrections:
            statuses = {str(item.get("status") or "").strip().lower() for item in corrections}
            if "approved" in statuses:
                return "verified"
            if statuses & {"pending", "requires_resolution"}:
                return "pending_correction"
            if "rejected" in statuses and not audits:
                return "rejected"
        if not audits:
            return "unreviewed"
        finalized_statuses = {"ai_reviewed", "materialized", "ai_applied"}
        negative_decisions = {"REJECT", "REJECTED", "BLOCK"}
        has_finalized_positive = False
        for audit in audits:
            decision = str(audit.get("decision") or "").strip().upper()
            status = str(audit.get("status") or "").strip().lower()
            if PaperQuerySerializationMixin._is_positive_review_decision(decision) and status in finalized_statuses:
                has_finalized_positive = True
            if decision in negative_decisions and status in finalized_statuses:
                return "rejected"
        if has_finalized_positive:
            if not has_content:
                return "reviewed_empty_content"
            return "verified"
        return "review_candidate"


    @staticmethod
    def _is_positive_review_decision(decision: Any) -> bool:
        normalized = str(decision or "").strip().upper()
        return normalized in {"PASS", "APPROVE", "APPROVED", "ACCEPT", "ACCEPTED", "VERIFIED", "OK"}


    @classmethod
    def _figure_display_sort_key(cls, item: PaperFigure) -> tuple[int, int, str]:
        fig_num, sub_rank = cls._extract_figure_sort_parts(item.figure_label)
        if fig_num is None:
            fig_num, sub_rank = cls._extract_figure_sort_parts(item.caption)
        return (
            item.page if item.page is not None else 999999,
            fig_num if fig_num is not None else 999999,
            sub_rank if sub_rank is not None else 999999,
            str(item.id),
        )


    @staticmethod
    def _extract_figure_sort_parts(value: str | None) -> tuple[int | None, int | None]:
        if not value:
            return None, None
        match = re.search(
            r"(?:fig(?:ure)?|scheme)[_\s.\-]*(\d+)(?:\s*[\(\[]?\s*([a-z])\s*[\)\]]?)?",
            str(value),
            flags=re.IGNORECASE,
        )
        if not match:
            return None, None
        try:
            fig_num = int(match.group(1))
        except ValueError:
            return None, None
        sub_label = (match.group(2) or "").strip().lower()
        sub_rank = ord(sub_label[0]) - 96 if sub_label else None
        return fig_num, sub_rank


    @classmethod
    def _serialize_figure(
        cls,
        item: PaperFigure,
        *,
        approved_corrections: list[dict[str, Any]] | None = None,
        pending_corrections: list[dict[str, Any]] | None = None,
        object_review_audits: list[dict[str, Any]] | None = None,
        field_conflicts: list[dict[str, Any]] | None = None,
        duplicate_group_size: int = 1,
    ) -> PaperFigureResponse:
        payload = PaperFigureResponse.model_validate(item)
        canonical_image_path = cls._canonical_figure_image_path(payload, paper_id=item.paper_id)
        if canonical_image_path:
            payload = payload.model_copy(update={"image_path": canonical_image_path})
        image_review = cls._figure_image_review_payload(payload, paper_id=item.paper_id)
        figure_reliability = ArtifactReliabilityAuditService.figure_reliability_from_review(payload, image_review)
        key_elements, key_elements_detail = cls._normalize_figure_key_elements(payload.key_elements)
        corrections = approved_corrections or []
        correction_fields = sorted(
            {
                str(correction.get("field_name") or "").strip()
                for correction in corrections
                if str(correction.get("field_name") or "").strip()
            }
        )
        pending = pending_corrections or []
        pending_fields = sorted(
            {
                str(correction.get("field_name") or "").strip()
                for correction in pending
                if str(correction.get("field_name") or "").strip()
            }
        )
        pending_delete_count = sum(
            1 for correction in pending
            if str(correction.get("field_name") or "").strip().lower() == "delete"
        )
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        direct_delete_allowed, direct_delete_reason = direct_delete_eligibility(
            {
                "caption": payload.caption,
                "content_summary": payload.content_summary,
                "figure_label": payload.figure_label,
                "figure_role": payload.figure_role,
                "crop_status": payload.crop_status,
                "flags": image_review.get("flags") if isinstance(image_review, dict) else [],
                "figure_reliability_warnings": figure_reliability.get("warnings") if isinstance(figure_reliability, dict) else [],
                "key_elements": key_elements or [],
            },
            duplicate_group_size=duplicate_group_size,
        )
        return payload.model_copy(
            update={
                "caption": cls._clean_pdf_text(payload.caption),
                "content_summary": cls._clean_pdf_text(payload.content_summary),
                "key_elements": key_elements,
                "key_elements_detail": key_elements_detail,
                "asset_url": f"/api/papers/assets/{payload.image_path}" if payload.image_path else None,
                "image_review": image_review,
                "review_required": image_review["review_required"],
                "flags": image_review["flags"],
                "figure_reliability_status": figure_reliability["status"],
                "figure_reliability_warnings": figure_reliability["warnings"],
                "approved_correction_count": len(corrections),
                "approved_correction_fields": correction_fields,
                "pending_correction_count": len(pending),
                "pending_correction_fields": pending_fields,
                "pending_delete_proposal_count": pending_delete_count,
                "direct_delete_eligible": direct_delete_allowed,
                "direct_delete_reason": direct_delete_reason,
                "object_review_audit_count": len(audits),
                "object_review_audits": audits[:5],
                "latest_object_review_audit": audits[0] if audits else None,
                "conflict_count": len(conflicts),
                "field_conflicts": conflicts[:5],
            }
        )


    @classmethod
    def _normalize_figure_key_elements(cls, value: Any) -> tuple[list[str] | None, dict[str, Any] | None]:
        return normalize_figure_key_elements(value)


    @staticmethod
    def _canonical_figure_image_path(payload: PaperFigureResponse, *, paper_id: UUID | None = None) -> str | None:
        if not payload.image_path:
            return None
        settings = get_settings()
        resolved = resolve_persisted_artifact_path(
            payload.image_path,
            category="figures",
            settings=settings,
            must_exist=True,
            trusted_persisted_reference=True,
        )
        if resolved is None:
            figure_basename = f"{payload.figure_label}.png" if payload.figure_label else None
            if paper_id and figure_basename:
                fallback_roots = [
                    settings.storage_root,
                    Path(__file__).resolve().parents[2] / "data" / "storage",
                ]
                for root in fallback_roots:
                    candidate = root / "by_id" / str(paper_id) / "figures" / figure_basename
                    if candidate.exists():
                        resolved = candidate
                        break
        canonical = canonicalize_persisted_artifact_reference(
            resolved or payload.image_path,
            category="figures",
            settings=settings,
        )
        return canonical or payload.image_path


    @staticmethod
    def _figure_image_review_payload(payload: PaperFigureResponse, paper_id: UUID | None = None) -> dict[str, Any]:
        figure_payload: dict[str, Any] = payload.model_dump(mode="json")
        if paper_id is not None:
            figure_payload["paper_id"] = str(paper_id)
        return build_figure_image_review(figure_payload, settings=get_settings(), check_asset_exists=True)


    @staticmethod
    def _serialize_catalyst_sample(item: CatalystSample) -> CatalystSampleResponse:
        normalized = catalyst_basic_info_payload(
            name=item.name,
            catalyst_type=item.catalyst_type,
            metal_centers=item.metal_centers or [],
            coordination=item.coordination,
            support=item.support,
            synthesis_method=item.synthesis_method,
            evidence_strength=item.evidence_strength,
        )
        descriptor_payload = normalized["metal_descriptors"]
        return CatalystSampleResponse(
            id=item.id,
            name=item.name,
            catalyst_type=normalized["fields"]["catalyst_type"] or item.catalyst_type,
            metal_centers=normalized["fields"]["metal_centers"],
            coordination=item.coordination,
            support=normalized["fields"]["support"] or item.support,
            synthesis_method=item.synthesis_method,
            evidence_strength=item.evidence_strength,
            support_raw=normalized["raw"]["support"],
            support_normalized=normalized["fields"]["support"],
            catalyst_type_raw=normalized["raw"]["catalyst_type"],
            normalization_source=normalized["normalization_source"],
            metal_descriptor_summary=descriptor_payload["metal_descriptor_summary"],
            metal_1_descriptors=descriptor_payload["metal_1_descriptors"],
            metal_2_descriptors=descriptor_payload["metal_2_descriptors"],
            dac_combined_descriptors=descriptor_payload["dac_combined_descriptors"],
            descriptor_blockers=descriptor_payload["descriptor_blockers"],
        )


    @staticmethod
    def _catalyst_summary(item: CatalystSample) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "name": item.name,
            "catalyst_type": item.catalyst_type,
            "metal_centers": item.metal_centers or [],
            "coordination": item.coordination,
            "support": item.support,
            "evidence_strength": item.evidence_strength,
        }


    @classmethod
    def _serialize_dft_result(
        cls,
        item: DFTResult,
        *,
        catalyst_by_id: dict[str, CatalystSample] | None = None,
        object_review_audits: list[dict[str, Any]] | None = None,
        field_conflicts: list[dict[str, Any]] | None = None,
        review_gate: Any | None = None,
    ) -> DFTResultResponse:
        payload = DFTResultResponse.model_validate(item)
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        conflict_field_names = cls._aggregate_conflict_field_names(conflicts)
        ai_review_display = (
            DFTReviewQueueService.build_ai_review_display_status(
                gate=review_gate,
                object_review_audits=audits,
                conflicts=conflicts,
            )
            if review_gate is not None
            else None
        )
        linked_catalyst = (
            catalyst_by_id.get(str(item.catalyst_sample_id))
            if catalyst_by_id and item.catalyst_sample_id is not None
            else None
        )
        has_catalyst_identity = bool(
            linked_catalyst
            and any(
                (
                    bool((linked_catalyst.name or "").strip()),
                    bool((linked_catalyst.catalyst_type or "").strip()),
                    bool(linked_catalyst.metal_centers),
                    bool((linked_catalyst.coordination or "").strip()),
                    bool((linked_catalyst.support or "").strip()),
                )
            )
        )
        binding_status = (
            "bound_with_identity"
            if has_catalyst_identity
            else ("bound_missing_identity" if linked_catalyst else "unbound")
        )
        binding_payload = (
            (item.evidence_payload or {}).get("material_binding")
            if isinstance(item.evidence_payload, dict)
            else None
        )
        return payload.model_copy(
            update={
                "material_binding_status": binding_status,
                "bound_catalyst_sample": cls._catalyst_summary(linked_catalyst) if linked_catalyst else None,
                "binding_evidence_anchor": first_evidence_anchor(binding_payload),
                "reaction_step": cls._clean_pdf_text(payload.reaction_step),
                "source_section": cls._clean_pdf_text(payload.source_section),
                "source_figure": cls._clean_pdf_text(payload.source_figure),
                "evidence_text": cls._clean_pdf_text(payload.evidence_text),
                "object_review_audit_count": len(audits),
                "object_review_audits": audits[:5],
                "latest_object_review_audit": audits[0] if audits else None,
                "conflict_count": len(conflicts),
                "field_conflicts": conflicts[:5],
                "affected_field_names": conflict_field_names,
                "conflict_field_names": conflict_field_names,
                "ai_review_display_status": ai_review_display["status"] if ai_review_display else None,
                "ai_review_display_label": ai_review_display["label"] if ai_review_display else None,
                "ai_review_display_reason": ai_review_display["reason"] if ai_review_display else None,
                "ai_review_display_class": ai_review_display["class_name"] if ai_review_display else None,
            }
        )


    @staticmethod
    def _figure_duplicate_group_size(figures: list[PaperFigure], item: PaperFigure) -> int:
        identity = normalized_figure_identity(item)
        if not identity:
            return 1
        count = 0
        for figure in figures:
            if normalized_figure_identity(figure) == identity:
                count += 1
        return max(1, count)


    @staticmethod
    def _aggregate_conflict_field_names(conflicts: list[dict[str, Any]]) -> list[str]:
        names: list[str] = []
        for conflict in conflicts or []:
            candidates = conflict.get("affected_field_names") or conflict.get("conflict_field_names") or []
            if isinstance(candidates, list) and candidates:
                for candidate in candidates:
                    value = str(candidate or "").strip()
                    if value and value not in names:
                        names.append(value)
                continue
            fallback = str(conflict.get("field_name") or "").strip()
            if fallback and fallback not in names:
                names.append(fallback)
        return names


    @staticmethod
    def _clean_pdf_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = PaperQuerySerializationMixin._replace_pdf_text_artifacts(str(value))
        text = repair_mojibake_text(text) or ""
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


    @staticmethod
    def _clean_pdf_layout_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = PaperQuerySerializationMixin._replace_pdf_text_artifacts(str(value))
        text = repair_mojibake_text(text) or ""
        return text.strip()


    @staticmethod
    def _replace_pdf_text_artifacts(text: str) -> str:
        replacements = {
            "/uniFB00": "ff",
            "/uniFB01": "fi",
            "/uniFB02": "fl",
            "/uniFB03": "ffi",
            "/uniFB04": "ffl",
            "\u00ee\u0084\u0080": "ff",
            "\u00ee\u0084\u0081": "fi",
            "\u00ee\u0084\u0082": "fl",
            "\u00ee\u0084\u0083": "fi",
            "\u00ee\u0084\u0084": "fl",
            "\ue100": "ff",
            "\ue101": "fi",
            "\ue102": "fl",
            "\ue103": "fi",
            "\ue104": "fl",
            "顒僩": "fi",
            "顒僣": "fic",
            "顒僴": "fi",
            "顒剈": "flu",
            "顒價": "fir",
            "鈻?": "",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text


    def _build_list_item_with_counts(
        self,
        paper: Paper,
        counts: dict[str, int],
        relationship_summary: dict[str, int] | None = None,
        *,
        impact_metadata: PaperImpactMetadata | None = None,
        review_status: dict[str, Any] | None = None,
        include_heavy: bool = False,
    ) -> PaperListItemResponse:
        c = PaperCountsResponse(**counts)
        localized = self._localized_metadata(paper)
        status = review_status if isinstance(review_status, dict) else {}
        pdf_status = status.get("pdf_artifact_status")
        if not isinstance(pdf_status, dict):
            pdf_status = build_paper_pdf_status(paper)
        pdf_size = self._cached_pdf_size(paper.pdf_path)

        return PaperListItemResponse(
            id=paper.id,
            paper_id=paper.id,
            serial_number=paper.serial_number,
            paper_code=getattr(paper, "paper_code", None),
            library_name=normalize_library_name(paper.library_name),
            doi=paper.doi,
            title=paper.title,
            title_zh=localized.get("title_zh"),
            year=paper.year,
            journal=paper.journal,
            impact_factor=impact_metadata.impact_factor if impact_metadata else None,
            impact_factor_source=impact_metadata.impact_factor_source if impact_metadata else None,
            impact_factor_year=impact_metadata.impact_factor_year if impact_metadata else None,
            authors=paper.authors or [],
            abstract=paper.abstract if include_heavy else self._clip_list_text(paper.abstract, 700),
            abstract_zh=localized.get("abstract_zh") if include_heavy else self._clip_list_text(localized.get("abstract_zh"), 420),
            full_translation_zh=localized.get("full_translation_zh"),
            pdf_path=paper.pdf_path,
            pdf_size=pdf_size,
            oa_status=paper.oa_status,
            license=paper.license,
            tei_path=paper.tei_path,
            docling_json_path=paper.docling_json_path,
            markdown_path=paper.markdown_path,
            paper_type=getattr(paper, "paper_type", None),
            type_confidence=getattr(paper, "type_confidence", None),
            classification_source=getattr(paper, "classification_source", None),
            workflow_status=getattr(paper, "workflow_status", "Imported"),
            pdf_quality_status=getattr(paper, "pdf_quality_status", None),
            pdf_quality_score=getattr(paper, "pdf_quality_score", None),
            pdf_quality_report=getattr(paper, "pdf_quality_report", None) if include_heavy else None,
            pdf_artifact_status=pdf_status,
            pdf_exists=bool(status.get("pdf_exists", pdf_status.get("pdf_exists", False))),
            pdf_file_size=status.get("pdf_file_size", pdf_status.get("pdf_file_size")),
            pdf_path_kind=status.get("pdf_path_kind", pdf_status.get("pdf_path_kind")),
            has_parsed_content=bool(status.get("has_parsed_content", False)),
            manual_review_progress=(
                status.get("manual_review_progress")
                if isinstance(status.get("manual_review_progress"), dict)
                else {}
            ),
            needs_human_confirmation=bool(status.get("needs_human_confirmation", False)),
            has_active_dft_candidates=bool(status.get("has_active_dft_candidates", False)),
            active_dft_candidate_count=int(status.get("active_dft_candidate_count") or 0),
            dft_review_conflict_count=int(status.get("dft_review_conflict_count") or 0),
            dft_review_conflict_total_count=int(status.get("dft_review_conflict_total_count") or 0),
            visual_review_conflict_count=int(status.get("visual_review_conflict_count") or 0),
            visual_review_conflict_total_count=int(status.get("visual_review_conflict_total_count") or 0),
            content_review_conflict_count=int(status.get("content_review_conflict_count") or 0),
            content_review_conflict_total_count=int(status.get("content_review_conflict_total_count") or 0),
            workspace_path=getattr(paper, "workspace_path", None),
            comprehensive_analysis=paper.comprehensive_analysis if include_heavy else None,
            created_at=paper.created_at,
            counts=c,
            relationship_summary=relationship_summary or {},
        )


    def _localized_metadata(self, paper: Paper) -> dict[str, str | None]:
        data = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}
        return {
            "title_zh": data.get("title_zh") if isinstance(data.get("title_zh"), str) else None,
            "abstract_zh": data.get("abstract_zh") if isinstance(data.get("abstract_zh"), str) else None,
            "full_translation_zh": None,
        }


    @staticmethod
    def _clip_list_text(value: str | None, max_chars: int) -> str | None:
        if not value:
            return None
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 1)].rstrip() + "..."


    @staticmethod
    def _cached_pdf_size(stored_path: str | None) -> int | None:
        if not stored_path:
            return None
        settings = get_settings()
        return cached_pdf_size_for_storage(stored_path, str(settings.storage_root))


    @staticmethod
    def _serialize_writing_card(
        item: WritingCard,
        *,
        object_review_audits: list[dict[str, Any]] | None = None,
        field_conflicts: list[dict[str, Any]] | None = None,
    ) -> WritingCardResponse:
        figure_logic = item.figure_logic
        if isinstance(figure_logic, str):
            try:
                figure_logic = json.loads(figure_logic)
            except json.JSONDecodeError:
                pass
        gate = writing_card_gate(item)
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        return WritingCardResponse(
            id=item.id,
            paper_type=item.paper_type,
            research_gap=item.research_gap,
            proposed_solution=item.proposed_solution,
            core_hypothesis=item.core_hypothesis,
            evidence_chain=item.evidence_chain,
            section_strategy=item.section_strategy,
            figure_logic=figure_logic,
            abstract_logic=item.abstract_logic,
            introduction_logic=item.introduction_logic,
            discussion_logic=item.discussion_logic,
            evidence_chain_status=gate.evidence_chain_status,
            review_gate_status=gate.review_gate_status,
            can_use_for_writing=gate.can_use_for_writing,
            blocked_reasons=list(gate.blocked_reasons),
            evidence_status=gate.evidence_chain_status,
            safety_status=gate.review_gate_status,
            safe_verified=gate.can_use_for_writing and gate.review_gate_status == "safe_verified",
            object_review_audit_count=len(audits),
            object_review_audits=audits[:5],
            latest_object_review_audit=audits[0] if audits else None,
            conflict_count=len(conflicts),
            field_conflicts=conflicts[:5],
        )


    @staticmethod
    def _serialize_mechanism_claim(
        item: MechanismClaim,
        *,
        object_review_audits: list[dict[str, Any]] | None = None,
        field_conflicts: list[dict[str, Any]] | None = None,
    ) -> MechanismClaimResponse:
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        evidence_status = "present" if str(item.evidence_text or "").strip() else "missing"
        confidence = item.confidence
        if confidence is None:
            confidence_status = "missing"
        elif confidence >= 0.8:
            confidence_status = "high"
        elif confidence >= 0.5:
            confidence_status = "medium"
        else:
            confidence_status = "low"
        return MechanismClaimResponse(
            id=item.id,
            catalyst_sample_id=item.catalyst_sample_id,
            claim_type=item.claim_type,
            claim_text=item.claim_text,
            evidence_types=item.evidence_types or [],
            confidence=confidence,
            evidence_text=item.evidence_text,
            evidence_status=evidence_status,
            locator_status="text_only" if evidence_status == "present" else "missing_locator",
            confidence_status=confidence_status,
            object_review_audit_count=len(audits),
            object_review_audits=audits[:5],
            latest_object_review_audit=audits[0] if audits else None,
            conflict_count=len(conflicts),
            field_conflicts=conflicts[:5],
        )


    @staticmethod
    def _is_supplementary_relationship(relationship_type: Any) -> bool:
        normalized = str(relationship_type or "").strip().lower()
        return normalized in {"supplementary", "supplementary_information", "supporting_information", "si"}


    @staticmethod
    def _manual_review_progress(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        source = data if isinstance(data, dict) else {}
        progress = source.get("manual_review_progress") if isinstance(source.get("manual_review_progress"), dict) else {}

        def normalize_entry(module: str) -> dict[str, Any]:
            raw = progress.get(module)
            if isinstance(raw, dict):
                return {
                    "completed": bool(raw.get("completed")),
                    "updated_at": raw.get("updated_at"),
                    "updated_by": raw.get("updated_by"),
                }
            return {
                "completed": bool(raw),
                "updated_at": None,
                "updated_by": None,
            }

        return {
            "content": normalize_entry("content"),
            "figures": normalize_entry("figures"),
            "dft": normalize_entry("dft"),
        }


    @staticmethod
    def _serialize_relationship(item: PaperRelationship, related_paper: Paper | None) -> PaperRelationshipItemResponse:
        return PaperRelationshipItemResponse(
            id=item.id,
            source_paper_id=item.source_paper_id,
            target_paper_id=item.target_paper_id,
            relationship_type=item.relationship_type,
            note=item.note,
            confidence=getattr(item, "confidence", None),
            created_by=item.created_by,
            created_at=item.created_at,
            related_paper_code=related_paper.paper_code if related_paper is not None else None,
            related_paper_title=related_paper.title if related_paper is not None else None,
            related_manual_review_progress=(
                PaperQuerySerializationMixin._manual_review_progress(related_paper.comprehensive_analysis)
                if related_paper is not None
                else {}
            ),
        )
