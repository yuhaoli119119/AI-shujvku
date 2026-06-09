from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, load_only

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    ExternalAnalysisCandidate,
    MechanismClaim,
    Paper,
    PaperRelationship,
    PaperFigure,
    PaperNote,
    PaperSection,
    PaperTable,
    ReferenceEntry,
    WritingCard,
    FigureDataPoint,
)
from app.schemas.api import (
    CatalystSampleResponse,
    DFTResultResponse,
    DFTSettingResponse,
    ElectrochemicalPerformanceResponse,
    MechanismClaimResponse,
    PaperCountsResponse,
    PaperDetailResponse,
    PaperFigureResponse,
    PaperListFilterParams,
    PaperListItemResponse,
    PaperRelationshipItemResponse,
    PaperSectionResponse,
    PaperTableResponse,
    ReferenceEntryResponse,
    WritingCardResponse,
    FigureDataPointResponse,
)
from app.config import get_settings
from app.utils.artifact_status import build_paper_artifact_status
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import writing_card_gate


@lru_cache(maxsize=8192)
def _cached_pdf_size_for_storage(stored_path: str, storage_root: str) -> int | None:
    raw = str(stored_path or "").strip()
    if not raw:
        return None

    root = Path(storage_root)
    parts = [part for part in re.split(r"[\\/]+", raw) if part]
    lowered = [part.lower() for part in parts]
    basename = parts[-1] if parts else ""
    candidates: list[Path] = []
    raw_path = Path(raw)

    if raw_path.is_absolute():
        candidates.append(raw_path)
    if "storage" in lowered:
        idx = lowered.index("storage")
        candidates.append(root.parent / Path(*parts[idx:]))
    if "pdf" in lowered:
        idx = lowered.index("pdf")
        candidates.append(root / Path(*parts[idx + 1 :]))
    if basename:
        candidates.append(root / "pdf" / basename)
        candidates.append(root / basename)
    if not raw_path.is_absolute():
        candidates.append(root / raw_path)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if resolved.is_file():
                return int(resolved.stat().st_size)
        except OSError:
            continue
    return None


class PaperQueryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_papers(self, filters: PaperListFilterParams | None = None) -> list[PaperListItemResponse]:
        filters = filters or PaperListFilterParams()
        query = select(Paper)

        if filters.library_name:
            query = query.where(build_library_name_clause(Paper.library_name, filters.library_name))
        if filters.q:
            for kw in filters.q.strip().split():
                keyword = f"%{kw}%"
                author_text = cast(Paper.authors, String)
                section_sub = (
                    select(PaperSection.paper_id)
                    .where(
                        PaperSection.paper_id == Paper.id,
                        or_(
                            PaperSection.section_title.ilike(keyword),
                            PaperSection.text.ilike(keyword),
                        ),
                    )
                    .correlate(Paper)
                    .exists()
                )
                query = query.where(
                    or_(
                        Paper.title.ilike(keyword),
                        Paper.doi.ilike(keyword),
                        Paper.journal.ilike(keyword),
                        Paper.abstract.ilike(keyword),
                        author_text.ilike(keyword),
                        section_sub,
                    )
                )
        if filters.source_path:
            query = query.where(Paper.source_path == filters.source_path)
        if filters.year is not None:
            query = query.where(Paper.year == filters.year)
        if filters.journal is not None:
            query = query.where(Paper.journal.ilike(f"%{filters.journal}%"))
        if getattr(filters, "paper_type", None) is not None:
            query = query.where(Paper.paper_type.ilike(f"{filters.paper_type}%"))
        if filters.has_dft_results is not None:
            dft_sub = (
                select(DFTResult.paper_id)
                .where(DFTResult.paper_id == Paper.id)
                .correlate(Paper)
                .exists()
            )
            query = query.where(dft_sub.is_(filters.has_dft_results))
        if filters.has_writing_cards is not None:
            wc_sub = (
                select(WritingCard.paper_id)
                .where(WritingCard.paper_id == Paper.id)
                .correlate(Paper)
                .exists()
            )
            query = query.where(wc_sub.is_(filters.has_writing_cards))

        query = query.order_by(*self._list_ordering(filters))
        query = query.offset(filters.offset).limit(filters.limit)
        papers = self.session.scalars(query).all()
        if not papers:
            return []

        paper_ids = [p.id for p in papers]
        from collections import defaultdict
        
        models_to_count = {
            "sections": PaperSection,
            "tables": PaperTable,
            "figures": PaperFigure,
            "dft_settings": DFTSetting,
            "catalyst_samples": CatalystSample,
            "dft_results": DFTResult,
            "electrochemical_performance": ElectrochemicalPerformance,
            "mechanism_claims": MechanismClaim,
            "writing_cards": WritingCard,
            "figure_data_points": FigureDataPoint,
        }
        
        counts_map = defaultdict(lambda: {k: 0 for k in models_to_count.keys()})
        relationship_summary_map = defaultdict(dict)
        
        for key, model in models_to_count.items():
            stmt = (
                select(model.paper_id, func.count())
                .where(model.paper_id.in_(paper_ids))
                .group_by(model.paper_id)
            )
            for pid, count in self.session.execute(stmt):
                counts_map[pid][key] = count

        section_rows = self.session.execute(
            select(
                PaperSection.paper_id,
                PaperSection.section_type,
                PaperSection.section_title,
                func.substr(PaperSection.text, 1, 500),
            ).where(PaperSection.paper_id.in_(paper_ids))
        ).all()
        counts_map_by_section = defaultdict(int)
        for paper_id, section_type, section_title, text in section_rows:
            if self._is_display_body_section_values(section_type, section_title, text):
                counts_map_by_section[paper_id] += 1
        for paper_id in paper_ids:
            counts_map[paper_id]["sections"] = counts_map_by_section.get(paper_id, 0)

        relationship_rows = self.session.scalars(
            select(PaperRelationship).where(PaperRelationship.source_paper_id.in_(paper_ids))
        ).all()
        for row in relationship_rows:
            summary = relationship_summary_map[row.source_paper_id]
            summary[row.relationship_type] = summary.get(row.relationship_type, 0) + 1

        return [
            self._build_list_item_with_counts(
                paper, 
                {**counts_map[paper.id], "comprehensive_analysis": 1 if paper.comprehensive_analysis else 0},
                relationship_summary_map.get(paper.id, {}),
            ) for paper in papers
        ]

    @staticmethod
    def _list_ordering(filters: PaperListFilterParams) -> tuple:
        sort_by = (filters.sort_by or "year_serial").strip().lower()
        sort_order = (filters.sort_order or "desc").strip().lower()
        descending = sort_order == "desc"

        title_order = Paper.title.desc() if descending else Paper.title.asc()
        created_order = Paper.created_at.desc() if descending else Paper.created_at.asc()

        if sort_by == "created_at":
            return (created_order, title_order)

        if sort_by == "title":
            return (
                Paper.title.is_(None).asc(),
                title_order,
                Paper.year.is_(None).asc(),
                Paper.year.desc() if descending else Paper.year.asc(),
                Paper.serial_number.is_(None).asc(),
                Paper.serial_number.desc() if descending else Paper.serial_number.asc(),
            )

        year_order = Paper.year.desc() if descending else Paper.year.asc()
        serial_order = Paper.serial_number.asc()
        return (
            Paper.year.is_(None).asc(),
            year_order,
            Paper.serial_number.is_(None).asc(),
            serial_order,
            Paper.title.is_(None).asc(),
            title_order,
            created_order,
        )

    def get_paper_detail(self, paper_id: UUID) -> PaperDetailResponse | None:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            return None

        all_sections = self.session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == paper_id)
            .options(
                load_only(
                    PaperSection.id,
                    PaperSection.paper_id,
                    PaperSection.section_title,
                    PaperSection.section_type,
                    PaperSection.text,
                    PaperSection.page_start,
                    PaperSection.page_end,
                )
            )
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
        ).all()
        sections = sorted(
            [section for section in all_sections if self._is_display_body_section(section)],
            key=self._section_display_sort_key,
        )
        tables = self.session.scalars(
            select(PaperTable)
            .where(PaperTable.paper_id == paper_id)
            .order_by(PaperTable.page.asc().nulls_last())
        ).all()
        figures = self.session.scalars(
            select(PaperFigure)
            .where(PaperFigure.paper_id == paper_id)
            .order_by(PaperFigure.page.asc().nulls_last())
        ).all()
        dft_settings = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper_id)).all()
        catalyst_samples = self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
        dft_results = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        electrochemical_items = self.session.scalars(
            select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id == paper_id)
        ).all()
        mechanism_claims = self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper_id)).all()
        writing_cards = self.session.scalars(select(WritingCard).where(WritingCard.paper_id == paper_id)).all()
        figure_data_points = self.session.scalars(select(FigureDataPoint).where(FigureDataPoint.paper_id == paper_id)).all()
        outgoing_relationships = self.session.scalars(
            select(PaperRelationship).where(PaperRelationship.source_paper_id == paper_id)
        ).all()
        incoming_relationships = self.session.scalars(
            select(PaperRelationship).where(PaperRelationship.target_paper_id == paper_id)
        ).all()

        references = self.session.scalars(
            select(ReferenceEntry).where(ReferenceEntry.paper_id == paper_id).order_by(ReferenceEntry.reference_number.asc().nulls_last(), ReferenceEntry.created_at.asc())
        ).all()
        full_translation = self._latest_full_translation(paper_id)
        figure_ids = {str(figure.id) for figure in figures}
        figure_audits = self._figure_object_review_audits(paper_id, figure_ids)
        figure_conflicts = ReviewConflictAggregationService(self.session).conflicts_by_target(
            paper_ids={paper_id},
            target_type="figure",
            target_ids=figure_ids,
        )
        writing_card_ids = {str(card.id) for card in writing_cards}
        writing_card_audits = self._object_review_audits_by_target(
            paper_id,
            writing_card_ids,
            target_types={"writing_card", "writing_cards"},
        )
        writing_card_conflicts = ReviewConflictAggregationService(self.session).conflicts_by_target(
            paper_ids={paper_id},
            target_type="writing_cards",
            target_ids=writing_card_ids,
        )

        base_counts = {
            "sections": len(sections),
            "tables": len(tables),
            "figures": len(figures),
            "dft_settings": len(dft_settings),
            "catalyst_samples": len(catalyst_samples),
            "dft_results": len(dft_results),
            "electrochemical_performance": len(electrochemical_items),
            "mechanism_claims": len(mechanism_claims),
            "writing_cards": len(writing_cards),
            "comprehensive_analysis": 1 if paper.comprehensive_analysis else 0,
            "figure_data_points": len(figure_data_points),
        }
        relationship_summary = {}
        for row in outgoing_relationships:
            relationship_summary[row.relationship_type] = relationship_summary.get(row.relationship_type, 0) + 1
        base = self._build_list_item_with_counts(paper, base_counts, relationship_summary, include_heavy=True)
        related_paper_ids = {row.target_paper_id for row in outgoing_relationships} | {row.source_paper_id for row in incoming_relationships}
        related_titles = {}
        if related_paper_ids:
            related_titles = {
                item.id: item.title
                for item in self.session.scalars(select(Paper).where(Paper.id.in_(list(related_paper_ids)))).all()
            }
        base_payload = base.model_dump()
        base_payload["full_translation_zh"] = full_translation
        return PaperDetailResponse(
            **base_payload,
            sections=[self._serialize_section(item) for item in sections],
            tables=[self._serialize_table(item) for item in tables],
            figures=[
                self._serialize_figure(
                    item,
                    object_review_audits=figure_audits.get(str(item.id), []),
                    field_conflicts=figure_conflicts.get(str(item.id), []),
                )
                for item in figures
            ],
            dft_settings_items=[DFTSettingResponse.model_validate(item) for item in dft_settings],
            catalyst_samples_items=[CatalystSampleResponse.model_validate(item) for item in catalyst_samples],
            dft_results_items=[self._serialize_dft_result(item) for item in dft_results],
            electrochemical_performance_items=[
                ElectrochemicalPerformanceResponse.model_validate(item) for item in electrochemical_items
            ],
            mechanism_claims_items=[MechanismClaimResponse.model_validate(item) for item in mechanism_claims],
            writing_cards_items=[
                self._serialize_writing_card(
                    item,
                    object_review_audits=writing_card_audits.get(str(item.id), []),
                    field_conflicts=writing_card_conflicts.get(str(item.id), []),
                )
                for item in writing_cards
            ],
            outgoing_relationships=[
                self._serialize_relationship(item, related_titles.get(item.target_paper_id)) for item in outgoing_relationships
            ],
            incoming_relationships=[
                self._serialize_relationship(item, related_titles.get(item.source_paper_id)) for item in incoming_relationships
            ],
            references=[ReferenceEntryResponse.model_validate(item) for item in references],
            figure_data_points_items=[FigureDataPointResponse.model_validate(item) for item in figure_data_points],
            artifact_status=build_paper_artifact_status(paper),
        )

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
        if section_type in {"table", "figure", "figure_caption", "caption", "reference", "references"}:
            return False

        title = cls._compact_section_text(section_title)
        text = cls._compact_section_text(section_text)
        title_lower = title.lower()
        text_lower = text.lower()
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
        )

    @classmethod
    def _serialize_table(cls, item: PaperTable) -> PaperTableResponse:
        payload = PaperTableResponse.model_validate(item)
        return payload.model_copy(
            update={
                "caption": cls._clean_pdf_text(payload.caption),
                "markdown_content": cls._clean_pdf_layout_text(payload.markdown_content),
            }
        )

    @classmethod
    def _serialize_figure(
        cls,
        item: PaperFigure,
        *,
        object_review_audits: list[dict[str, Any]] | None = None,
        field_conflicts: list[dict[str, Any]] | None = None,
    ) -> PaperFigureResponse:
        payload = PaperFigureResponse.model_validate(item)
        image_review = cls._figure_image_review_payload(payload)
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        return payload.model_copy(
            update={
                "caption": cls._clean_pdf_text(payload.caption),
                "content_summary": cls._clean_pdf_text(payload.content_summary),
                "asset_url": f"/api/papers/assets/{payload.image_path}" if payload.image_path else None,
                "image_review": image_review,
                "review_required": image_review["review_required"],
                "flags": image_review["flags"],
                "object_review_audit_count": len(audits),
                "object_review_audits": audits[:5],
                "latest_object_review_audit": audits[0] if audits else None,
                "conflict_count": len(conflicts),
                "field_conflicts": conflicts[:5],
            }
        )

    def _figure_object_review_audits(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        return self._object_review_audits_by_target(
            paper_id,
            figure_ids,
            target_types={"figure", "figures", "paper_figure", "paper_figures"},
        )

    def _object_review_audits_by_target(
        self,
        paper_id: UUID,
        target_ids: set[str],
        *,
        target_types: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not target_ids:
            return {}
        audits_by_target: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in target_ids}
        normalized_target_types = {target_type.strip().lower() for target_type in target_types}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
            .order_by(ExternalAnalysisCandidate.created_at.desc())
        ).all()
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip().lower()
            target_id = str(
                payload.get("target_id")
                or payload.get("figure_id")
                or payload.get("writing_card_id")
                or payload.get("record_id")
                or ""
            )
            if target_id not in target_ids or target_type not in normalized_target_types:
                continue
            if len(audits_by_target.setdefault(target_id, [])) >= 5:
                continue
            audits_by_target[target_id].append(self._object_review_audit_payload(candidate, payload))
        return audits_by_target

    @staticmethod
    def _object_review_audit_payload(
        candidate: ExternalAnalysisCandidate,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "candidate_id": str(candidate.id),
            "candidate_type": candidate.candidate_type,
            "status": candidate.status,
            "target_type": payload.get("target_type"),
            "target_id": payload.get("target_id") or payload.get("figure_id") or payload.get("writing_card_id") or payload.get("record_id"),
            "field_name": payload.get("field_name") or payload.get("field"),
            "source": str(payload.get("source") or "unknown"),
            "source_label": payload.get("source_label"),
            "agent_role": payload.get("agent_role"),
            "model_name": payload.get("model_name"),
            "decision": payload.get("decision") or payload.get("verdict"),
            "recommended_action": payload.get("recommended_action"),
            "verification_status": payload.get("verification_status", "unverified"),
            "confidence": payload.get("confidence") if payload.get("confidence") is not None else candidate.confidence,
            "reason": payload.get("reason") or payload.get("reviewer_note") or payload.get("summary"),
            "evidence_checked": payload.get("evidence_checked"),
            "evidence_location": payload.get("evidence_location"),
            "blocking_errors": payload.get("blocking_errors") or [],
            "corrected_value": payload.get("corrected_value"),
            "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
        }

    @staticmethod
    def _figure_image_review_payload(payload: PaperFigureResponse) -> dict[str, Any]:
        flags: list[str] = []
        if not payload.image_path:
            flags.append("missing_image_path")
        if payload.page is None:
            flags.append("missing_pdf_page")
        if not payload.crop_status:
            flags.append("missing_crop_status")
        if payload.crop_status in {"needs_recrop", "caption_only", "needs_review"}:
            flags.append(payload.crop_status)
        if not any(isinstance(item, dict) and item.get("bbox") for item in (payload.prov or [])):
            flags.append("missing_parser_bbox")

        crop_status = payload.crop_status or ("candidate_crop" if payload.image_path else "caption_only")
        review_required = bool(flags) or crop_status in {"needs_recrop", "caption_only", "needs_review"}
        return {
            "crop_status": crop_status,
            "review_required": review_required,
            "flags": list(dict.fromkeys(flags)),
            "crop_confidence": payload.crop_confidence,
            "crop_source": payload.crop_source,
            "note": "Verify this figure against the PDF page before treating it as evidence."
            if review_required
            else "Parser crop is available; still treat it as an unverified figure candidate.",
        }

    @classmethod
    def _serialize_dft_result(cls, item: DFTResult) -> DFTResultResponse:
        payload = DFTResultResponse.model_validate(item)
        return payload.model_copy(
            update={
                "reaction_step": cls._clean_pdf_text(payload.reaction_step),
                "source_section": cls._clean_pdf_text(payload.source_section),
                "source_figure": cls._clean_pdf_text(payload.source_figure),
                "evidence_text": cls._clean_pdf_text(payload.evidence_text),
            }
        )

    @staticmethod
    def _clean_pdf_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = PaperQueryService._replace_pdf_text_artifacts(str(value))
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _clean_pdf_layout_text(value: str | None) -> str | None:
        if value is None:
            return None
        return PaperQueryService._replace_pdf_text_artifacts(str(value)).strip()

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
        include_heavy: bool = False,
    ) -> PaperListItemResponse:
        c = PaperCountsResponse(**counts)
        localized = self._localized_metadata(paper)
        
        pdf_size = self._cached_pdf_size(paper.pdf_path)

        return PaperListItemResponse(
            id=paper.id,
            serial_number=paper.serial_number,
            library_name=normalize_library_name(paper.library_name),
            doi=paper.doi,
            title=paper.title,
            title_zh=localized.get("title_zh"),
            year=paper.year,
            journal=paper.journal,
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
        return _cached_pdf_size_for_storage(stored_path, str(settings.storage_root))

    def _latest_full_translation(self, paper_id: UUID) -> str | None:
        note = self.session.scalars(
            select(PaperNote)
            .where(
                PaperNote.paper_id == paper_id,
                PaperNote.source == "translation_preview",
                PaperNote.field_name == "full_translation_preview",
            )
            .order_by(PaperNote.created_at.desc())
        ).first()
        return note.content if note and note.content else None

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
    def _serialize_relationship(item: PaperRelationship, related_paper_title: str | None) -> PaperRelationshipItemResponse:
        return PaperRelationshipItemResponse(
            id=item.id,
            source_paper_id=item.source_paper_id,
            target_paper_id=item.target_paper_id,
            relationship_type=item.relationship_type,
            note=item.note,
            created_by=item.created_by,
            created_at=item.created_at,
            related_paper_title=related_paper_title,
        )
