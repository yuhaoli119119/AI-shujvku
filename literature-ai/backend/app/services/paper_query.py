from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
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
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import writing_card_gate


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

        sections = self.session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == paper_id)
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
        ).all()
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
        base = self._build_list_item_with_counts(paper, base_counts, relationship_summary)
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
            sections=[PaperSectionResponse.model_validate(item) for item in sections],
            tables=[PaperTableResponse.model_validate(item) for item in tables],
            figures=[PaperFigureResponse.model_validate(item) for item in figures],
            dft_settings_items=[DFTSettingResponse.model_validate(item) for item in dft_settings],
            catalyst_samples_items=[CatalystSampleResponse.model_validate(item) for item in catalyst_samples],
            dft_results_items=[DFTResultResponse.model_validate(item) for item in dft_results],
            electrochemical_performance_items=[
                ElectrochemicalPerformanceResponse.model_validate(item) for item in electrochemical_items
            ],
            mechanism_claims_items=[MechanismClaimResponse.model_validate(item) for item in mechanism_claims],
            writing_cards_items=[self._serialize_writing_card(item) for item in writing_cards],
            outgoing_relationships=[
                self._serialize_relationship(item, related_titles.get(item.target_paper_id)) for item in outgoing_relationships
            ],
            incoming_relationships=[
                self._serialize_relationship(item, related_titles.get(item.source_paper_id)) for item in incoming_relationships
            ],
            references=[ReferenceEntryResponse.model_validate(item) for item in references],
            figure_data_points_items=[FigureDataPointResponse.model_validate(item) for item in figure_data_points],
        )

    def _build_list_item_with_counts(
        self,
        paper: Paper,
        counts: dict[str, int],
        relationship_summary: dict[str, int] | None = None,
    ) -> PaperListItemResponse:
        c = PaperCountsResponse(**counts)
        localized = self._localized_metadata(paper)
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
            abstract=paper.abstract,
            abstract_zh=localized.get("abstract_zh"),
            full_translation_zh=localized.get("full_translation_zh"),
            pdf_path=paper.pdf_path,
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
            pdf_quality_report=getattr(paper, "pdf_quality_report", None),
            workspace_path=getattr(paper, "workspace_path", None),
            comprehensive_analysis=paper.comprehensive_analysis,
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
    def _serialize_writing_card(item: WritingCard) -> WritingCardResponse:
        figure_logic = item.figure_logic
        if isinstance(figure_logic, str):
            try:
                figure_logic = json.loads(figure_logic)
            except json.JSONDecodeError:
                pass
        gate = writing_card_gate(item)
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
