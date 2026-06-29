from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, String, and_, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, load_only

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    MechanismClaim,
    Paper,
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
    DFTSettingResponse,
    ElectrochemicalPerformanceResponse,
    PaperDetailResponse,
    PaperListFilterParams,
    PaperListItemResponse,
    ReferenceEntryResponse,
    FigureDataPointResponse,
)
from app.services.paper_codes import ensure_paper_codes
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.artifact_status import build_paper_artifact_status
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results
from app.utils.workbench_status import workflow_needs_human_confirmation
from app.rag.quality import build_rag_quality_summary
from app.services.paper_query_reviews import PaperQueryReviewMixin
from app.services.paper_query_serializers import PaperQuerySerializationMixin
from app.services.paper_query_storage import cached_pdf_size_for_storage as _cached_pdf_size_for_storage

__all__ = ["PaperQueryService", "_cached_pdf_size_for_storage"]

DFT_DETAIL_PAGE_SIZE = 28


def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_search_token(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[\u2010-\u2015\u2212]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _prepare_search_query(value: str) -> str:
    normalized = _normalize_search_token(value)
    normalized = re.sub(r"(?i)(li\s*-?\s*s)(?=电池)", r"\1 ", normalized)
    for term in ("锂-硫", "锂硫", "多硫化物", "硫还原", "电池"):
        normalized = normalized.replace(term, f" {term} ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _expand_search_token(token: str) -> list[tuple[str, str]]:
    normalized = _normalize_search_token(token).replace(" ", "")
    if not normalized:
        return []
    if normalized in {"lis", "li-s", "锂硫", "锂-硫", "lithium-sulfur", "lithiumsulfur"}:
        return [
            ("regex", r"(^|[^a-z0-9])li[-\s\u2010-\u2015\u2212]?s([^a-z0-9]|$)"),
            ("like", "lithium-sulfur"),
            ("like", "lithium sulfur"),
            ("like", "lithiumsulfur"),
            ("like", "锂硫"),
            ("like", "锂-硫"),
        ]
    if normalized in {"电池", "battery", "batteries", "cell", "cells"}:
        return [
            ("like", "battery"),
            ("like", "batteries"),
            ("like", "cell"),
            ("like", "cells"),
            ("like", "电池"),
        ]
    if normalized in {"多硫化物", "polysulfide", "polysulfides", "lips", "lithiumpolysulfide"}:
        return [
            ("like", "polysulfide"),
            ("like", "polysulfides"),
            ("like", "lithium polysulfide"),
            ("like", "lips"),
            ("like", "多硫化物"),
        ]
    if normalized in {"硫还原", "sulfurreduction", "sulfur-redox", "sulfurredox", "srr"}:
        return [
            ("like", "sulfur reduction"),
            ("like", "sulfur redox"),
            ("like", "sulfur reduction reaction"),
            ("like", "srr"),
            ("like", "硫还原"),
        ]
    return [("like", token)]


def _iter_search_groups(query: str) -> list[list[tuple[str, str]]]:
    return [
        aliases
        for token in _prepare_search_query(query).split()
        if (aliases := _expand_search_token(token))
    ]


def _build_keyword_group_clause(aliases: list[tuple[str, str]]):
    author_text = cast(Paper.authors, String)
    columns = (Paper.title, Paper.paper_code, Paper.doi, Paper.journal, Paper.abstract, author_text)
    clauses = []
    for match_type, value in aliases:
        if match_type == "regex":
            column_clauses = [column.op("~*")(value) for column in columns]
            section_sub = (
                select(PaperSection.paper_id)
                .where(
                    PaperSection.paper_id == Paper.id,
                    or_(
                        PaperSection.section_title.op("~*")(value),
                        PaperSection.text.op("~*")(value),
                    ),
                )
                .correlate(Paper)
                .exists()
            )
        else:
            keyword = f"%{_escape_like(value)}%"
            column_clauses = [column.ilike(keyword, escape="\\") for column in columns]
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
        clauses.append(or_(*column_clauses, section_sub))
    return or_(*clauses)


class PaperQueryService(PaperQueryReviewMixin, PaperQuerySerializationMixin):
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_papers(self, filters: PaperListFilterParams | None = None) -> list[PaperListItemResponse]:
        filters = filters or PaperListFilterParams()
        query = select(Paper)

        if filters.library_name:
            query = query.where(build_library_name_clause(Paper.library_name, filters.library_name))
        if filters.q:
            for group in _iter_search_groups(filters.q):
                query = query.where(_build_keyword_group_clause(group))
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
        active_dft_counts: dict[UUID, int] = {paper_id: 0 for paper_id in paper_ids}
        for paper_id, candidate_status, count in self.session.execute(
            select(DFTResult.paper_id, DFTResult.candidate_status, func.count(DFTResult.id))
            .where(DFTResult.paper_id.in_(paper_ids))
            .group_by(DFTResult.paper_id, DFTResult.candidate_status)
        ).all():
            if PaperWorkbenchService._is_active_dft_candidate(candidate_status):
                active_dft_counts[paper_id] += int(count or 0)
        workbench_status_map = {
            str(paper.id): {
                "manual_review_progress": PaperWorkbenchService._manual_review_progress(
                    paper.comprehensive_analysis
                ),
                "needs_human_confirmation": workflow_needs_human_confirmation(
                    paper.workflow_status,
                    paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {},
                ),
                "has_active_dft_candidates": active_dft_counts.get(paper.id, 0) > 0,
                "active_dft_candidate_count": active_dft_counts.get(paper.id, 0),
            }
            for paper in papers
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

        incoming_supplementary_rows = [
            row
            for row in self.session.scalars(
                select(PaperRelationship).where(PaperRelationship.target_paper_id.in_(paper_ids))
            ).all()
            if self._is_supplementary_relationship(row.relationship_type)
        ]
        supplementary_main_ids = {row.source_paper_id for row in incoming_supplementary_rows}
        supplementary_main_papers = {}
        if supplementary_main_ids:
            supplementary_main_papers = {
                item.id: item
                for item in self.session.scalars(
                    select(Paper).where(Paper.id.in_(list(supplementary_main_ids)))
                ).all()
            }
        supplementary_review_progress_map: dict[UUID, dict[str, Any]] = {}
        for row in incoming_supplementary_rows:
            main_paper = supplementary_main_papers.get(row.source_paper_id)
            if main_paper is None:
                continue
            supplementary_review_progress_map[row.target_paper_id] = self._manual_review_progress(
                main_paper.comprehensive_analysis
            )

        impact_metadata_map = {
            row.paper_id: row
            for row in self.session.scalars(
                select(PaperImpactMetadata).where(PaperImpactMetadata.paper_id.in_(paper_ids))
            ).all()
        }

        items: list[PaperListItemResponse] = []
        for paper in papers:
            review_status = dict(workbench_status_map.get(str(paper.id)) or {})
            if paper.id in supplementary_review_progress_map:
                review_status["manual_review_progress"] = supplementary_review_progress_map[paper.id]
            items.append(
                self._build_list_item_with_counts(
                    paper,
                    {**counts_map[paper.id], "comprehensive_analysis": 1 if paper.comprehensive_analysis else 0},
                    relationship_summary_map.get(paper.id, {}),
                    impact_metadata=impact_metadata_map.get(paper.id),
                    review_status=review_status,
                )
            )
        return items

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
        if compact:
            return self._get_light_paper_detail(paper)

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
        dft_result_count = int(
            self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)) or 0
        )
        dft_results = self.session.scalars(
            select(DFTResult)
            .where(DFTResult.paper_id == paper_id)
            .order_by(DFTResult.id.asc())
            .limit(DFT_DETAIL_PAGE_SIZE)
        ).all()
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
        related_paper_ids = {row.target_paper_id for row in outgoing_relationships} | {
            row.source_paper_id for row in incoming_relationships
        }
        related_papers = {}
        if related_paper_ids:
            related_papers = {
                item.id: item
                for item in self.session.scalars(select(Paper).where(Paper.id.in_(list(related_paper_ids)))).all()
            }
        outgoing_supplementary_relationships = [
            row for row in outgoing_relationships if self._is_supplementary_relationship(row.relationship_type)
        ]
        incoming_supplementary_relationships = [
            row for row in incoming_relationships if self._is_supplementary_relationship(row.relationship_type)
        ]
        tables_for_response = list(tables)
        table_source_metadata: dict[str, dict[str, Any]] = {}
        table_review_paper_id = paper_id
        table_correction_paper_ids: set[UUID] = {paper_id}

        if outgoing_supplementary_relationships:
            supplementary_paper_ids = [row.target_paper_id for row in outgoing_supplementary_relationships]
            table_correction_paper_ids.update(supplementary_paper_ids)
            supplementary_tables = self.session.scalars(
                select(PaperTable)
                .where(PaperTable.paper_id.in_(supplementary_paper_ids))
                .order_by(PaperTable.paper_id.asc(), PaperTable.page.asc().nulls_last())
            ).all()
            tables_for_response.extend(supplementary_tables)
            for table in supplementary_tables:
                source_paper = related_papers.get(table.paper_id)
                table_source_metadata[str(table.id)] = {
                    "source_document_type": "supplementary_information",
                    "related_paper_id": table.paper_id,
                    "related_paper_code": source_paper.paper_code if source_paper is not None else None,
                    "related_paper_title": source_paper.title if source_paper is not None else None,
                    "writeback_paper_id": paper_id,
                }
        elif incoming_supplementary_relationships:
            main_relationship = incoming_supplementary_relationships[0]
            table_review_paper_id = main_relationship.source_paper_id
            table_correction_paper_ids = {main_relationship.source_paper_id}
            for table in tables:
                table_source_metadata[str(table.id)] = {
                    "source_document_type": "supplementary_information",
                    "related_paper_id": paper_id,
                    "related_paper_code": paper.paper_code,
                    "related_paper_title": paper.title,
                    "writeback_paper_id": main_relationship.source_paper_id,
                }

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
            table_ids = {str(table.id) for table in tables_for_response}
            table_audits = self._object_review_audits_by_target(
                table_review_paper_id,
                table_ids,
                target_types={"table", "tables", "paper_table", "paper_tables"},
            )
            table_corrections = self._table_corrections_by_target(table_correction_paper_ids, table_ids)
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
        dft_gate_by_id = bulk_export_gate_results(self.session, dft_results, target_type="dft_results")
        catalyst_by_id = {str(item.id): item for item in catalyst_samples}

        base_counts = {
            "sections": len(sections),
            "tables": len(tables),
            "figures": len(figures),
            "dft_settings": len(dft_settings),
            "catalyst_samples": len(catalyst_samples),
            "dft_results": dft_result_count,
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
                    source_metadata=table_source_metadata.get(str(item.id)),
                )
                for item in tables_for_response
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
            catalyst_samples_items=[self._serialize_catalyst_sample(item) for item in catalyst_samples],
            dft_results_items=[
                self._serialize_dft_result(
                    item,
                    catalyst_by_id=catalyst_by_id,
                    object_review_audits=dft_result_audits.get(str(item.id), []),
                    field_conflicts=dft_result_conflicts.get(str(item.id), []),
                    review_gate=dft_gate_by_id.get(str(item.id)),
                )
                for item in dft_results
            ],
            dft_results_page={
                "offset": 0,
                "limit": DFT_DETAIL_PAGE_SIZE,
                "returned": len(dft_results),
                "total": dft_result_count,
                "has_more": len(dft_results) < dft_result_count,
            },
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
                self._serialize_relationship(item, related_papers.get(item.target_paper_id))
                for item in outgoing_relationships
            ],
            incoming_relationships=[
                self._serialize_relationship(item, related_papers.get(item.source_paper_id))
                for item in incoming_relationships
            ],
            references=[ReferenceEntryResponse.model_validate(item) for item in references],
            figure_data_points_items=[FigureDataPointResponse.model_validate(item) for item in figure_data_points],
            artifact_status=build_paper_artifact_status(paper),
            rag_quality=build_rag_quality_summary(
                self.session,
                figures=figures,
                dft_results=dft_results,
                writing_cards=writing_cards,
                dft_gate_by_id=dft_gate_by_id,
            ),
            **review_status,
        )

    def _get_light_paper_detail(self, paper: Paper) -> PaperDetailResponse:
        paper_id = paper.id
        count_models = (
            ("sections", PaperSection),
            ("tables", PaperTable),
            ("figures", PaperFigure),
            ("dft_settings", DFTSetting),
            ("catalyst_samples", CatalystSample),
            ("dft_results", DFTResult),
            ("electrochemical_performance", ElectrochemicalPerformance),
            ("mechanism_claims", MechanismClaim),
            ("writing_cards", WritingCard),
            ("figure_data_points", FigureDataPoint),
        )
        count_query = union_all(
            *[
                select(literal(name).label("name"), func.count(model.id).label("count"))
                .select_from(model)
                .where(model.paper_id == paper_id)
                for name, model in count_models
            ]
        )
        counts = {name: int(count or 0) for name, count in self.session.execute(count_query).all()}
        counts["comprehensive_analysis"] = 1 if paper.comprehensive_analysis else 0
        relationship_summary = {
            relationship_type: int(count or 0)
            for relationship_type, count in self.session.execute(
                select(PaperRelationship.relationship_type, func.count(PaperRelationship.id))
                .where(PaperRelationship.source_paper_id == paper_id)
                .group_by(PaperRelationship.relationship_type)
            ).all()
        }
        dft_status_counts = {
            str(status or "system_candidate"): int(count or 0)
            for status, count in self.session.execute(
                select(DFTResult.candidate_status, func.count(DFTResult.id))
                .where(DFTResult.paper_id == paper_id)
                .group_by(DFTResult.candidate_status)
            ).all()
        }
        active_dft_count = sum(
            count
            for status, count in dft_status_counts.items()
            if PaperWorkbenchService._is_active_dft_candidate(status)
        )
        normalized_dft_statuses = {status.strip().lower() for status in dft_status_counts}
        if not dft_status_counts:
            dft_review_status = "missing"
        elif "needs_human_confirmation" in normalized_dft_statuses:
            dft_review_status = "conflict"
        elif normalized_dft_statuses.intersection(
            {"ml_ready", "human_reviewed_needs_evidence", "gemini_verified", "rejected", "verified", "human_verified"}
        ):
            dft_review_status = "reviewed"
        else:
            dft_review_status = "candidate"
        review_status = {
            "manual_review_progress": PaperWorkbenchService._manual_review_progress(paper.comprehensive_analysis),
            "has_parsed_content": bool(
                paper.abstract
                or counts.get("sections")
                or counts.get("tables")
                or counts.get("figures")
                or counts.get("dft_results")
            ),
            "has_active_dft_candidates": active_dft_count > 0,
            "active_dft_candidate_count": active_dft_count,
        }
        base = self._build_list_item_with_counts(
            paper,
            counts,
            relationship_summary,
            impact_metadata=self.session.get(PaperImpactMetadata, paper_id),
            review_status=review_status,
            include_heavy=False,
        )
        return PaperDetailResponse(
            **base.model_dump(),
            artifact_status=build_paper_artifact_status(paper),
            dft_review_status=dft_review_status,
            dft_results_page={
                "offset": 0,
                "limit": DFT_DETAIL_PAGE_SIZE,
                "returned": 0,
                "total": counts.get("dft_results", 0),
                "has_more": bool(counts.get("dft_results")),
            },
        )

    def get_dft_results_page(
        self,
        paper_id: UUID,
        *,
        offset: int = 0,
        limit: int = DFT_DETAIL_PAGE_SIZE,
        result_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        if self.session.get(Paper, paper_id) is None:
            return None
        total = int(self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)) or 0)
        stmt = select(DFTResult).where(DFTResult.paper_id == paper_id)
        if result_id is not None:
            stmt = stmt.where(DFTResult.id == result_id)
        else:
            stmt = stmt.order_by(DFTResult.id.asc()).offset(offset).limit(limit)
        rows = list(self.session.scalars(stmt).all())
        result_ids = {str(row.id) for row in rows}
        catalyst_ids = {row.catalyst_sample_id for row in rows if row.catalyst_sample_id is not None}
        catalyst_by_id = (
            {
                str(row.id): row
                for row in self.session.scalars(
                    select(CatalystSample).where(CatalystSample.id.in_(catalyst_ids))
                ).all()
            }
            if catalyst_ids
            else {}
        )
        audits = (
            self._object_review_audits_by_target(
                paper_id,
                result_ids,
                target_types={"dft_result", "dft_results"},
            )
            if result_ids
            else {}
        )
        conflicts = (
            ReviewConflictAggregationService(self.session).conflicts_by_target(
                paper_ids={paper_id},
                target_type="dft_results",
                target_ids=result_ids,
            )
            if result_ids
            else {}
        )
        gates = bulk_export_gate_results(self.session, rows, target_type="dft_results")
        items = [
            self._serialize_dft_result(
                row,
                catalyst_by_id=catalyst_by_id,
                object_review_audits=audits.get(str(row.id), []),
                field_conflicts=conflicts.get(str(row.id), []),
                review_gate=gates.get(str(row.id)),
            )
            for row in rows
        ]
        return {
            "paper_id": str(paper_id),
            "items": items,
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "total": total,
            "has_more": result_id is None and offset + len(items) < total,
        }

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
