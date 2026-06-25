from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, String, and_, cast, func, or_, select
from sqlalchemy.orm import Session, load_only

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperImpactMetadata,
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
from app.services.paper_codes import ensure_paper_codes
from app.services.paper_workbench_service import PaperWorkbenchService
from app.config import get_settings
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference, resolve_persisted_artifact_path
from app.utils.figure_summary import normalize_figure_key_elements
from app.utils.figure_delete_policy import direct_delete_eligibility, normalized_figure_identity
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.evidence_anchors import first_evidence_anchor
from app.utils.figure_reliability import build_figure_image_review
from app.utils.text_cleaning import repair_mojibake_text
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import writing_card_gate
from app.rag.quality import build_rag_quality_summary


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
                keyword = f"%{_escape_like(kw)}%"
                author_text = cast(Paper.authors, String)
                section_sub = (
                    select(PaperSection.paper_id)
                    .where(
                        PaperSection.paper_id == Paper.id,
                        or_(
                            PaperSection.section_title.ilike(keyword, escape="\\"),
                            PaperSection.text.ilike(keyword, escape="\\"),
                        ),
                    )
                    .correlate(Paper)
                    .exists()
                )
                query = query.where(
                    or_(
                        Paper.title.ilike(keyword, escape="\\"),
                        Paper.paper_code.ilike(keyword, escape="\\"),
                        Paper.doi.ilike(keyword, escape="\\"),
                        Paper.journal.ilike(keyword, escape="\\"),
                        Paper.abstract.ilike(keyword, escape="\\"),
                        author_text.ilike(keyword, escape="\\"),
                        section_sub,
                    )
                )
        if filters.source_path:
            query = query.where(Paper.source_path == filters.source_path)
        if filters.year is not None:
            query = query.where(Paper.year == filters.year)
        if filters.journal is not None:
            query = query.where(Paper.journal.ilike(f"%{_escape_like(filters.journal)}%", escape="\\"))
        if getattr(filters, "paper_type", None) is not None:
            query = query.where(Paper.paper_type.ilike(f"{_escape_like(filters.paper_type)}%", escape="\\"))
        if filters.has_dft_results is not None:
            dft_sub = (
                select(DFTResult.paper_id)
                .where(DFTResult.paper_id == Paper.id)
                .correlate(Paper)
                .exists()
            )
            query = query.where(dft_sub.is_(filters.has_dft_results))
        if filters.has_writing_cards is not None:
            candidate_paper_ids = {
                paper_id
                for paper_id in self.session.scalars(
                    query.with_only_columns(Paper.id).order_by(None)
                ).all()
            }
            reviewed_writing_card_paper_ids = self._reviewed_writing_card_paper_ids(candidate_paper_ids)
            if filters.has_writing_cards:
                if not reviewed_writing_card_paper_ids:
                    query = query.where(Paper.id.is_(None))
                else:
                    query = query.where(Paper.id.in_(reviewed_writing_card_paper_ids))
            elif reviewed_writing_card_paper_ids:
                query = query.where(Paper.id.not_in(reviewed_writing_card_paper_ids))
        if filters.has_pdf is not None:
            pdf_available_clause = and_(
                Paper.pdf_path.is_not(None),
                func.trim(Paper.pdf_path) != "",
                func.coalesce(func.lower(Paper.oa_status), "").not_in(["metadata_only", "needs_upload"]),
            )
            query = query.where(pdf_available_clause if filters.has_pdf else ~pdf_available_clause)

        query = query.order_by(*self._list_ordering(filters))
        query = query.offset(filters.offset).limit(filters.limit)
        papers = self.session.scalars(query).all()
        if not papers:
            return []
        if ensure_paper_codes(self.session, papers):
            self.session.commit()

        paper_ids = [p.id for p in papers]
        normalized_library = normalize_library_name(filters.library_name) if filters.library_name else None
        workbench_rows = PaperWorkbenchService(self.session, get_settings()).review_center(
            limit=len(paper_ids),
            sort_by="recent",
            library_name=normalized_library,
            summary_only=True,
            paper_ids=paper_ids,
        ).get("rows", [])
        paper_id_set = {str(paper_id) for paper_id in paper_ids}
        workbench_status_map = {
            str(row.get("paper_id")): row
            for row in workbench_rows
            if str(row.get("paper_id") or "") in paper_id_set
        }
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

        impact_metadata_map = {
            row.paper_id: row
            for row in self.session.scalars(
                select(PaperImpactMetadata).where(PaperImpactMetadata.paper_id.in_(paper_ids))
            ).all()
        }

        return [
            self._build_list_item_with_counts(
                paper, 
                {**counts_map[paper.id], "comprehensive_analysis": 1 if paper.comprehensive_analysis else 0},
                relationship_summary_map.get(paper.id, {}),
                impact_metadata=impact_metadata_map.get(paper.id),
                review_status=workbench_status_map.get(str(paper.id)),
            ) for paper in papers
        ]

    @staticmethod
    def _list_ordering(filters: PaperListFilterParams) -> tuple:
        sort_by = (filters.sort_by or "year_serial").strip().lower()
        sort_order = (filters.sort_order or "desc").strip().lower()
        descending = sort_order == "desc"

        title_order = Paper.title.desc() if descending else Paper.title.asc()
        created_order = Paper.created_at.desc() if descending else Paper.created_at.asc()
        paper_code_numeric = cast(func.nullif(func.substr(Paper.paper_code, 2), ""), Integer)
        paper_code_text_order = Paper.paper_code.desc() if descending else Paper.paper_code.asc()

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

        if sort_by == "serial_number":
            serial_order = Paper.serial_number.desc() if descending else Paper.serial_number.asc()
            return (
                Paper.serial_number.is_(None).asc(),
                serial_order,
                Paper.title.is_(None).asc(),
                title_order,
                created_order,
            )

        if sort_by in {"paper_code", "paper_code_numeric"}:
            numeric_order = paper_code_numeric.desc() if descending else paper_code_numeric.asc()
            return (
                paper_code_numeric.is_(None).asc(),
                numeric_order,
                Paper.paper_code.is_(None).asc(),
                paper_code_text_order,
                Paper.title.is_(None).asc(),
                title_order,
                created_order,
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

    def get_paper_detail(self, paper_id: UUID, *, compact: bool = False) -> PaperDetailResponse | None:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            return None
        if ensure_paper_codes(self.session, [paper]):
            self.session.commit()

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
        figures = sorted(figures, key=self._figure_display_sort_key)
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

        if compact:
            references = []
            paper_notes = []
            full_translation = None
            table_audits: dict[str, list[dict[str, Any]]] = {}
            table_corrections: dict[str, list[dict[str, Any]]] = {}
            figure_audits: dict[str, list[dict[str, Any]]] = {}
            figure_approved_corrections: dict[str, list[dict[str, Any]]] = {}
            figure_pending_corrections: dict[str, list[dict[str, Any]]] = {}
            figure_conflicts: dict[str, list[dict[str, Any]]] = {}
            writing_card_audits: dict[str, list[dict[str, Any]]] = {}
            writing_card_conflicts: dict[str, list[dict[str, Any]]] = {}
            mechanism_claim_audits: dict[str, list[dict[str, Any]]] = {}
            mechanism_claim_conflicts: dict[str, list[dict[str, Any]]] = {}
            dft_result_ids = {str(item.id) for item in dft_results}
            dft_result_audits = self._object_review_audits_by_target(
                paper_id,
                dft_result_ids,
                target_types={"dft_result", "dft_results"},
            )
            dft_result_conflicts: dict[str, list[dict[str, Any]]] = {}
        else:
            references = self.session.scalars(
                select(ReferenceEntry).where(ReferenceEntry.paper_id == paper_id).order_by(ReferenceEntry.reference_number.asc().nulls_last(), ReferenceEntry.created_at.asc())
            ).all()
            paper_notes = self.session.scalars(
                select(PaperNote)
                .where(PaperNote.paper_id == paper_id)
                .where(PaperNote.source != "translation_preview")
                .order_by(PaperNote.created_at.desc())
                .limit(30)
            ).all()
            full_translation = self._latest_full_translation(paper_id)
            figure_ids = {str(figure.id) for figure in figures}
            table_ids = {str(table.id) for table in tables}
            table_audits = self._object_review_audits_by_target(
                paper_id,
                table_ids,
                target_types={"table", "tables", "paper_table", "paper_tables"},
            )
            table_corrections = self._table_corrections_by_target(paper_id, table_ids)
            figure_audits = self._figure_object_review_audits(paper_id, figure_ids)
            figure_approved_corrections = self._figure_approved_corrections(paper_id, figure_ids)
            figure_pending_corrections = self._figure_pending_corrections(paper_id, figure_ids)
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
            mechanism_claim_ids = {str(claim.id) for claim in mechanism_claims}
            mechanism_claim_audits = self._object_review_audits_by_target(
                paper_id,
                mechanism_claim_ids,
                target_types={"mechanism_claim", "mechanism_claims"},
            )
            mechanism_claim_conflicts = ReviewConflictAggregationService(self.session).conflicts_by_target(
                paper_ids={paper_id},
                target_type="mechanism_claims",
                target_ids=mechanism_claim_ids,
            )
            dft_result_ids = {str(item.id) for item in dft_results}
            dft_result_audits = self._object_review_audits_by_target(
                paper_id,
                dft_result_ids,
                target_types={"dft_result", "dft_results"},
            )
            dft_result_conflicts = ReviewConflictAggregationService(self.session).conflicts_by_target(
                paper_ids={paper_id},
                target_type="dft_results",
                target_ids=dft_result_ids,
            )
        catalyst_by_id = {str(item.id): item for item in catalyst_samples}

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
        base = self._build_list_item_with_counts(
            paper,
            base_counts,
            relationship_summary,
            impact_metadata=self.session.get(PaperImpactMetadata, paper.id),
            include_heavy=True,
        )
        if compact:
            outgoing_relationships = []
            incoming_relationships = []
            related_titles = {}
        else:
            related_paper_ids = {row.target_paper_id for row in outgoing_relationships} | {row.source_paper_id for row in incoming_relationships}
            related_titles = {}
            if related_paper_ids:
                related_titles = {
                    item.id: item.title
                    for item in self.session.scalars(select(Paper).where(Paper.id.in_(list(related_paper_ids)))).all()
                }
        base_payload = base.model_dump()
        base_payload["full_translation_zh"] = full_translation
        review_status = self._paper_detail_review_status(
            paper_id=paper_id,
            paper=paper,
            sections=sections,
            figures=figures,
            writing_cards=writing_cards,
            dft_results=dft_results,
            full_translation=full_translation,
            figure_audits=figure_audits,
            figure_conflicts=figure_conflicts,
            writing_card_audits=writing_card_audits,
            writing_card_conflicts=writing_card_conflicts,
            dft_result_audits=dft_result_audits,
            dft_result_conflicts=dft_result_conflicts,
        )
        return PaperDetailResponse(
            **base_payload,
            sections=[self._serialize_section(item) for item in sections],
            tables=[
                self._serialize_table(
                    item,
                    object_review_audits=table_audits.get(str(item.id), []),
                    corrections=table_corrections.get(str(item.id), []),
                )
                for item in tables
            ],
            figures=[
                self._serialize_figure(
                    item,
                    approved_corrections=figure_approved_corrections.get(str(item.id), []),
                    pending_corrections=figure_pending_corrections.get(str(item.id), []),
                    object_review_audits=figure_audits.get(str(item.id), []),
                    field_conflicts=figure_conflicts.get(str(item.id), []),
                    duplicate_group_size=self._figure_duplicate_group_size(figures, item),
                )
                for item in figures
            ],
            paper_notes=[self._serialize_paper_note(item) for item in paper_notes],
            dft_settings_items=[DFTSettingResponse.model_validate(item) for item in dft_settings],
            catalyst_samples_items=[CatalystSampleResponse.model_validate(item) for item in catalyst_samples],
            dft_results_items=[
                self._serialize_dft_result(
                    item,
                    catalyst_by_id=catalyst_by_id,
                    object_review_audits=dft_result_audits.get(str(item.id), []),
                    field_conflicts=dft_result_conflicts.get(str(item.id), []),
                )
                for item in dft_results
            ],
            electrochemical_performance_items=[
                ElectrochemicalPerformanceResponse.model_validate(item) for item in electrochemical_items
            ],
            mechanism_claims_items=[
                self._serialize_mechanism_claim(
                    item,
                    object_review_audits=mechanism_claim_audits.get(str(item.id), []),
                    field_conflicts=mechanism_claim_conflicts.get(str(item.id), []),
                )
                for item in mechanism_claims
            ],
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
            rag_quality=(
                {}
                if compact
                else build_rag_quality_summary(
                    self.session,
                    figures=figures,
                    dft_results=dft_results,
                    writing_cards=writing_cards,
                )
            ),
            **review_status,
        )

    def _paper_detail_review_status(
        self,
        *,
        paper_id: UUID,
        paper: Paper,
        sections: list[PaperSection],
        figures: list[PaperFigure],
        writing_cards: list[WritingCard],
        dft_results: list[DFTResult],
        full_translation: str | None,
        figure_audits: dict[str, list[dict[str, Any]]],
        figure_conflicts: dict[str, list[dict[str, Any]]],
        writing_card_audits: dict[str, list[dict[str, Any]]],
        writing_card_conflicts: dict[str, list[dict[str, Any]]],
        dft_result_audits: dict[str, list[dict[str, Any]]],
        dft_result_conflicts: dict[str, list[dict[str, Any]]],
    ) -> dict[str, str]:
        return {
            "abstract_review_status": self._scalar_content_review_status(paper_id, "abstract", bool(paper.abstract)),
            "sections_review_status": self._collection_review_status(paper_id, "sections", bool(sections)),
            "writing_cards_review_status": self._writing_cards_review_status(
                paper_id,
                writing_cards,
                writing_card_audits,
                writing_card_conflicts,
            ),
            "translation_review_status": "final_trusted" if full_translation else "missing",
            "figures_review_status": self._figures_review_status(
                paper_id,
                figures,
                figure_audits,
                figure_conflicts,
            ),
            "dft_review_status": self._dft_review_status(dft_results, dft_result_audits, dft_result_conflicts),
        }

    def _scalar_content_review_status(self, paper_id: UUID, field_name: str, has_content: bool) -> str:
        if not has_content:
            return "missing"
        if self._has_ai_applied_candidate(paper_id, field_names={field_name}, target_prefixes={field_name}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={field_name}, target_prefixes={field_name}):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={field_name}):
            return "ai_verified"
        return "raw_only"

    def _collection_review_status(self, paper_id: UUID, collection: str, has_content: bool) -> str:
        if not has_content:
            return "missing"
        if self._has_ai_applied_candidate(paper_id, field_names={collection}, target_prefixes={collection}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={collection}, target_prefixes={collection}):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={collection, collection.rstrip("s")}):
            return "ai_verified"
        return "raw_only"

    def _writing_cards_review_status(
        self,
        paper_id: UUID,
        writing_cards: list[WritingCard],
        audits_by_card: dict[str, list[dict[str, Any]]],
        conflicts_by_card: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not writing_cards:
            return "missing"
        if any(self._audit_list_marks_ai_verified(audits_by_card.get(str(card.id), [])) for card in writing_cards):
            return "ai_verified"
        if any(writing_card_gate(card).can_use_for_writing for card in writing_cards):
            return "ai_verified"
        if any(conflicts_by_card.get(str(card.id)) for card in writing_cards):
            return "raw_only"
        return self._collection_review_status(paper_id, "writing_cards", True)

    def _reviewed_writing_card_paper_ids(self, paper_ids: set[UUID]) -> set[UUID]:
        if not paper_ids:
            return set()

        writing_card_rows = self.session.execute(
            select(WritingCard.id, WritingCard.paper_id).where(WritingCard.paper_id.in_(paper_ids))
        ).all()
        if not writing_card_rows:
            return set()

        candidate_paper_ids = {paper_id for _, paper_id in writing_card_rows}
        reviewed_paper_ids: set[UUID] = set()
        expected_fields = {"writing_cards", "writing_card"}

        notes = self.session.scalars(
            select(PaperNote).where(PaperNote.paper_id.in_(candidate_paper_ids))
        ).all()
        for note in notes:
            field = str(note.field_name or "").strip().lower()
            if not self._review_field_matches(field, expected_fields):
                continue
            source = str(note.source or "").lower()
            content = str(note.content or "").lower()
            if source == "ide_ai" or "[ai_reviewed]" in content:
                reviewed_paper_ids.add(note.paper_id)

        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id.in_(candidate_paper_ids))
            .where(PaperCorrection.status == "approved")
        ).all()
        for correction in corrections:
            source = str(correction.source or "").lower()
            reviewer = str(correction.reviewed_by or "").lower()
            if source != "ide_ai" and "ide" not in reviewer:
                continue
            field = str(correction.field_name or "").strip().lower()
            target = str(correction.target_path or "").strip().lower()
            if self._review_field_matches(field, expected_fields) or self._review_field_matches(target, expected_fields):
                reviewed_paper_ids.add(correction.paper_id)

        applied_candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id.in_(candidate_paper_ids))
            .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
        ).all()
        for candidate in applied_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            field = str(payload.get("field_name") or "").strip().lower()
            target = str(payload.get("target_path") or "").strip().lower()
            if self._review_field_matches(field, expected_fields) or self._review_field_matches(target, expected_fields):
                reviewed_paper_ids.add(candidate.paper_id)

        audit_candidates = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id.in_(candidate_paper_ids))
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
        ).all()
        for candidate, run in audit_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip().lower()
            if target_type not in expected_fields:
                continue
            source = str(run.source or "").lower()
            source_label = str(run.source_label or "").lower()
            decision = str(payload.get("decision") or "").strip().lower()
            verification = str(payload.get("verification_status") or "").strip().lower()
            if ("ide_ai" in source or "ide" in source_label or "[ai_reviewed]" in json.dumps(payload, ensure_ascii=False).lower()) and (
                decision in {"approve", "approved", "accept", "verified", "revise", "update"}
                or verification in {"verified", "ai_verified", "reviewed"}
            ):
                reviewed_paper_ids.add(candidate.paper_id)

        return reviewed_paper_ids

    def _figures_review_status(
        self,
        paper_id: UUID,
        figures: list[PaperFigure],
        audits_by_figure: dict[str, list[dict[str, Any]]],
        conflicts_by_figure: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not figures:
            return "missing"
        if any(conflicts_by_figure.get(str(figure.id)) for figure in figures):
            return "risk"
        if any(self._figure_has_risk(figure) for figure in figures):
            return "risk"
        if any(self._audit_list_marks_ai_verified(audits_by_figure.get(str(figure.id), [])) for figure in figures):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={"figures", "figure"}):
            return "ai_verified"
        if self._has_ai_applied_candidate(paper_id, field_names={"figures"}, target_prefixes={"figures"}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={"figures"}, target_prefixes={"figures"}):
            return "ai_verified"
        return "raw_only"

    @staticmethod
    def _figure_has_risk(figure: PaperFigure) -> bool:
        crop_status = str(getattr(figure, "crop_status", "") or "").lower()
        role = str(getattr(figure, "figure_role", "") or "").lower()
        return (not getattr(figure, "image_path", None)) or crop_status in {"missing", "failed", "needs_review"} or role == "noise"

    @staticmethod
    def _dft_review_status(
        dft_results: list[DFTResult],
        audits_by_result: dict[str, list[dict[str, Any]]],
        conflicts_by_result: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not dft_results:
            return "missing"
        if any(conflicts_by_result.get(str(item.id)) for item in dft_results):
            return "conflict"
        if any(item.candidate_status == "Needs_Human_Confirmation" for item in dft_results):
            return "conflict"
        reviewed_statuses = {"ML_Ready", "human_reviewed_needs_evidence", "Gemini_Verified", "Rejected"}
        if any(item.candidate_status in reviewed_statuses for item in dft_results):
            return "reviewed"
        if any(audits_by_result.get(str(item.id)) for item in dft_results):
            return "reviewed"
        return "candidate"

    @staticmethod
    def _audit_list_marks_ai_verified(audits: list[dict[str, Any]]) -> bool:
        for audit in audits:
            source = str(audit.get("source") or "").lower()
            source_label = str(audit.get("source_label") or "").lower()
            decision = str(audit.get("decision") or "").lower()
            verification = str(audit.get("verification_status") or "").lower()
            if ("ide_ai" in source or "ide" in source_label or "[ai_reviewed]" in str(audit).lower()) and (
                decision in {"approve", "approved", "accept", "verified", "revise", "update"}
                or verification in {"verified", "ai_verified", "reviewed"}
            ):
                return True
        return False

    def _has_ai_review_note(self, paper_id: UUID, *, field_names: set[str]) -> bool:
        normalized = {item.lower() for item in field_names}
        notes = self.session.scalars(
            select(PaperNote)
            .where(PaperNote.paper_id == paper_id)
            .order_by(PaperNote.created_at.desc())
            .limit(100)
        ).all()
        for note in notes:
            source = str(note.source or "").lower()
            content = str(note.content or "").lower()
            field = str(note.field_name or "").strip().lower()
            if not self._review_field_matches(field, normalized):
                continue
            if source == "ide_ai" or "[ai_reviewed]" in content:
                return True
        return False

    def _has_ai_applied_candidate(
        self,
        paper_id: UUID,
        *,
        field_names: set[str],
        target_prefixes: set[str],
    ) -> bool:
        normalized_fields = {item.lower() for item in field_names}
        normalized_prefixes = {item.lower() for item in target_prefixes}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
            .order_by(ExternalAnalysisCandidate.created_at.desc())
            .limit(100)
        ).all()
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            field = str(payload.get("field_name") or "").strip().lower()
            target = str(payload.get("target_path") or "").strip().lower()
            if self._review_field_matches(field, normalized_fields):
                return True
            if self._review_field_matches(target, normalized_prefixes):
                return True
        return False

    def _has_ai_approved_correction(
        self,
        paper_id: UUID,
        *,
        field_names: set[str],
        target_prefixes: set[str],
    ) -> bool:
        normalized_fields = {item.lower() for item in field_names}
        normalized_prefixes = {item.lower() for item in target_prefixes}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status == "approved")
            .order_by(PaperCorrection.created_at.desc())
            .limit(200)
        ).all()
        for correction in corrections:
            source = str(correction.source or "").lower()
            reviewer = str(correction.reviewed_by or "").lower()
            if source != "ide_ai" and "ide" not in reviewer:
                continue
            field = str(correction.field_name or "").strip().lower()
            target = str(correction.target_path or "").strip().lower()
            if self._review_field_matches(field, normalized_fields):
                return True
            if self._review_field_matches(target, normalized_prefixes):
                return True
        return False

    @staticmethod
    def _review_field_matches(value: str, expected: set[str]) -> bool:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return False
        if normalized in expected:
            return True
        return any(
            normalized.startswith(prefix + ":")
            or normalized.startswith(prefix + "/")
            or normalized.startswith(prefix + ".")
            for prefix in expected
        )

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
    ) -> PaperTableResponse:
        payload = PaperTableResponse.model_validate(item)
        audits = object_review_audits or []
        table_corrections = corrections or []
        return payload.model_copy(
            update={
                "caption": cls._clean_pdf_text(payload.caption),
                "markdown_content": cls._clean_pdf_layout_text(payload.markdown_content),
                "table_review_status": cls._table_review_status(audits, table_corrections),
                "object_review_audit_count": len(audits),
                "object_review_audits": audits[:5],
                "latest_object_review_audit": audits[0] if audits else None,
            }
        )

    @staticmethod
    def _table_review_status(audits: list[dict[str, Any]], corrections: list[dict[str, Any]] | None = None) -> str:
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
            if PaperQueryService._is_positive_review_decision(decision) and status in finalized_statuses:
                has_finalized_positive = True
            if decision in negative_decisions and status in finalized_statuses:
                return "rejected"
        if has_finalized_positive:
            return "verified"
        return "review_candidate"

    @staticmethod
    def _is_positive_review_decision(decision: Any) -> bool:
        normalized = str(decision or "").strip().upper()
        return normalized in {"PASS", "APPROVE", "APPROVED", "ACCEPT", "ACCEPTED", "VERIFIED", "OK"}

    def _table_corrections_by_target(
        self,
        paper_id: UUID,
        table_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not table_ids:
            return {}
        corrections_by_table: dict[str, list[dict[str, Any]]] = {table_id: [] for table_id in table_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status.in_(["pending", "requires_resolution", "approved", "rejected"]))
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type == "codex_item" and target_id in table_ids:
                target_type = "tables"
                target_field = correction.field_name or target_field
            if target_type not in {"table", "tables", "paper_table", "paper_tables"}:
                continue
            if target_id not in table_ids:
                continue
            corrections_by_table.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(target_field or correction.field_name or "").strip(),
                    "status": correction.status,
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_table

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
        deduped_by_target: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {target_id: {} for target_id in target_ids}
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
            decision = str(payload.get("decision") or "").strip().lower()
            target_id = str(
                payload.get("target_id")
                or payload.get("figure_id")
                or payload.get("writing_card_id")
                or payload.get("mechanism_claim_id")
                or payload.get("record_id")
                or ""
            )
            if (
                target_type == "dft_results"
                and (target_id.lower() == "new" or decision == "new_candidate")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                target_id = str(candidate.materialized_target_id).strip()
            if target_id not in target_ids or target_type not in normalized_target_types:
                continue
            audit_payload = self._object_review_audit_payload(candidate, payload)
            dedupe_key = self._object_review_audit_dedupe_key(target_type, audit_payload)
            target_bucket = deduped_by_target.setdefault(target_id, {})
            existing = target_bucket.get(dedupe_key)
            if existing is None or self._object_review_audit_payload_rank(audit_payload) > self._object_review_audit_payload_rank(existing):
                target_bucket[dedupe_key] = audit_payload
        for target_id, deduped in deduped_by_target.items():
            audits_by_target[target_id] = sorted(
                deduped.values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:5]
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
            "target_id": (
                payload.get("target_id")
                or payload.get("figure_id")
                or payload.get("writing_card_id")
                or payload.get("mechanism_claim_id")
                or payload.get("record_id")
            ),
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
    def _object_review_audit_dedupe_key(target_type: str, payload: dict[str, Any]) -> tuple[Any, ...]:
        field_name = str(payload.get("field_name") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        if target_type == "dft_results" and decision == "new_candidate" and field_name in {"", "dft_results"}:
            field_name = "dft_results"
        evidence = payload.get("evidence_location")
        corrected = payload.get("corrected_value")
        return (
            str(payload.get("source_label") or payload.get("source") or "").strip().lower(),
            decision,
            field_name,
            json.dumps(corrected, sort_keys=True, ensure_ascii=False, default=str),
            json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str),
        )

    @staticmethod
    def _object_review_audit_payload_rank(payload: dict[str, Any]) -> tuple[int, int]:
        field_name = str(payload.get("field_name") or "").strip()
        corrected = payload.get("corrected_value")
        return (
            1 if field_name else 0,
            1 if corrected not in (None, "", [], {}) else 0,
        )

    @staticmethod
    def _figure_image_review_payload(payload: PaperFigureResponse, paper_id: UUID | None = None) -> dict[str, Any]:
        figure_payload: dict[str, Any] = payload.model_dump(mode="json")
        if paper_id is not None:
            figure_payload["paper_id"] = str(paper_id)
        return build_figure_image_review(figure_payload, settings=get_settings(), check_asset_exists=True)

    def _figure_approved_corrections(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not figure_ids:
            return {}
        corrections_by_figure: dict[str, list[dict[str, Any]]] = {figure_id: [] for figure_id in figure_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status == "approved")
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type not in {"figure", "figures", "paper_figure", "paper_figures"}:
                continue
            if target_id not in figure_ids:
                continue
            field_name = target_field or correction.field_name
            corrections_by_figure.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(field_name or "").strip(),
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_figure

    def _figure_pending_corrections(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not figure_ids:
            return {}
        corrections_by_figure: dict[str, list[dict[str, Any]]] = {figure_id: [] for figure_id in figure_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status.in_(["pending", "requires_resolution"]))
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type not in {"figure", "figures", "paper_figure", "paper_figures"}:
                continue
            if target_id not in figure_ids:
                continue
            field_name = target_field or correction.field_name
            corrections_by_figure.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(field_name or "").strip(),
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "status": correction.status,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_figure

    @staticmethod
    def _parse_correction_target_path(target_path: str | None) -> tuple[str, str, str | None]:
        parts = [part.strip() for part in str(target_path or "").split(":")]
        if len(parts) >= 2:
            target_type = parts[0].lower()
            target_id = parts[1]
            target_field = parts[2] if len(parts) >= 3 and parts[2] else None
            return target_type, target_id, target_field
        return "", "", None

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
    ) -> DFTResultResponse:
        payload = DFTResultResponse.model_validate(item)
        audits = object_review_audits or []
        conflicts = field_conflicts or []
        conflict_field_names = cls._aggregate_conflict_field_names(conflicts)
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
        text = PaperQueryService._replace_pdf_text_artifacts(str(value))
        text = repair_mojibake_text(text) or ""
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _clean_pdf_layout_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = PaperQueryService._replace_pdf_text_artifacts(str(value))
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
            pdf_artifact_status=status.get("pdf_artifact_status"),
            pdf_exists=bool(status.get("pdf_exists", False)),
            pdf_file_size=status.get("pdf_file_size"),
            pdf_path_kind=status.get("pdf_path_kind"),
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
