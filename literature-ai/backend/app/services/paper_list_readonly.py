"""Legacy library list projection, without repair, task sync or review gates.

Stored row counts are not scientific approval. Unavailable review fields remain
unset and are omitted by the list/SSE serializers instead of inventing zeroes.
"""
from __future__ import annotations

from sqlalchemy import Numeric, String, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models
from app.schemas.api import PaperListFilterParams, PaperListItemResponse
from app.services.paper_query_storage import cached_pdf_size_for_storage
from app.utils.library_names import DEFAULT_LIBRARY_ALIASES, DEFAULT_LIBRARY_NAME, normalize_library_name


COUNT_MODELS = {
    "sections": models.PaperSection, "tables": models.PaperTable,
    "figures": models.PaperFigure, "dft_settings": models.DFTSetting,
    "catalyst_samples": models.CatalystSample, "dft_results": models.DFTResult,
    "electrochemical_performance": models.ElectrochemicalPerformance,
    "mechanism_claims": models.MechanismClaim, "writing_cards": models.WritingCard,
    "figure_data_points": models.FigureDataPoint,
}


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _library_column():
    name = func.trim(func.coalesce(models.Paper.library_name, ""))
    return case((or_(name == "", name.in_(DEFAULT_LIBRARY_ALIASES),
                     name.contains("姒涙"), name.contains("茅禄聵")), DEFAULT_LIBRARY_NAME), else_=name)


def readonly_library_clause(name):
    """Use exactly the same normalized library membership for list and SSE."""
    return _library_column() == normalize_library_name(name)


def _ordering(filters):
    p = models.Paper
    direction = lambda c: (c.desc() if filters.sort_order == "desc" else c.asc()).nulls_last()
    # CASE guards malformed codes before numeric conversion; arbitrary precision
    # avoids integer overflow. Unknown/missing codes sort after valid codes.
    valid = p.paper_code.op("~")(r"^[A-Za-z]+[0-9]+$")
    prefix = func.substring(p.paper_code, r"^([A-Za-z]+)")
    number = case((valid, cast(func.substring(p.paper_code, r"([0-9]+)$"), Numeric)), else_=None)
    code = (valid.desc().nulls_last(), direction(func.lower(prefix)), direction(number), direction(p.paper_code))
    if filters.sort_by in {"paper_code", "paper_code_numeric"}:
        order = code
    elif filters.sort_by == "title":
        order = (direction(p.title),)
    elif filters.sort_by == "created_at":
        order = (direction(p.created_at),)
    elif filters.sort_by == "serial_number":
        order = (direction(p.serial_number),)
    else:
        order = (direction(p.year), p.serial_number.asc().nulls_last(), *code)
    return (*order, p.id.asc())


class PaperListReadonlyService:
    def __init__(self, session: Session):
        self.session = session

    def list_papers(self, filters: PaperListFilterParams | None = None) -> list[PaperListItemResponse]:
        filters = filters or PaperListFilterParams()
        p = models.Paper
        query = select(p)
        if filters.library_name is not None:
            query = query.where(readonly_library_clause(filters.library_name))
        for name in ("source_path", "year"):
            if (value := getattr(filters, name)) is not None:
                query = query.where(getattr(p, name) == value)
        if filters.journal is not None:
            query = query.where(p.journal.ilike("%" + _like(filters.journal) + "%", escape="\\"))
        if filters.paper_type is not None:
            query = query.where(p.paper_type.ilike(_like(filters.paper_type) + "%", escape="\\"))
        for token in (filters.q or "").split():
            term = "%" + _like(token) + "%"
            section = select(models.PaperSection.id).where(models.PaperSection.paper_id == p.id,
                or_(models.PaperSection.section_title.ilike(term, escape="\\"),
                    models.PaperSection.text.ilike(term, escape="\\"))).exists()
            query = query.where(or_(*(c.ilike(term, escape="\\") for c in
                (p.title, p.paper_code, p.doi, p.journal, p.abstract, cast(p.authors, String),
                 p.comprehensive_analysis["title_zh"].as_string(), p.comprehensive_analysis["abstract_zh"].as_string())), section))
        # Compatibility filters mean stored DFT/content rows, not review approval.
        for attr, model in (("has_dft_results", models.DFTResult), ("has_writing_cards", models.WritingCard)):
            if (expected := getattr(filters, attr)) is not None:
                query = query.where(select(model.id).where(model.paper_id == p.id).exists().is_(expected))
        query = query.order_by(*_ordering(filters))
        with self.session.no_autoflush:
            papers = self.session.scalars(query if filters.has_pdf is not None else
                                          query.offset(filters.offset).limit(filters.limit)).all()
            size = lambda paper: cached_pdf_size_for_storage(paper.pdf_path, str(get_settings().storage_root))
            if filters.has_pdf is not None:
                papers = [paper for paper in papers if (size(paper) is not None) == filters.has_pdf]
                papers = papers[filters.offset:filters.offset + filters.limit]
            if not papers:
                return []
            ids = [paper.id for paper in papers]
            counts = {pid: {} for pid in ids}
            for field, model in COUNT_MODELS.items():
                values = dict(self.session.execute(select(model.paper_id, func.count(model.id))
                    .where(model.paper_id.in_(ids)).group_by(model.paper_id)).all())
                for pid in ids:
                    counts[pid][field] = int(values.get(pid, 0))
            impact = {item.paper_id: item for item in self.session.scalars(
                select(models.PaperImpactMetadata).where(models.PaperImpactMetadata.paper_id.in_(ids)))}
            relationships = {pid: {} for pid in ids}
            for pid, kind, count in self.session.execute(select(models.PaperRelationship.source_paper_id,
                models.PaperRelationship.relationship_type, func.count(models.PaperRelationship.id))
                .where(models.PaperRelationship.source_paper_id.in_(ids))
                .group_by(models.PaperRelationship.source_paper_id, models.PaperRelationship.relationship_type)):
                relationships[pid][kind] = int(count)
            result = []
            for paper in papers:
                analysis = paper.comprehensive_analysis if isinstance(paper.comprehensive_analysis, dict) else {}
                counts[paper.id]["comprehensive_analysis"] = int(bool(analysis))
                file_size = size(paper)
                payload = {name: getattr(paper, name) for name in
                    ("id", "doi", "title", "year", "journal", "authors", "abstract", "pdf_path", "oa_status",
                     "license", "tei_path", "docling_json_path", "markdown_path", "paper_type", "type_confidence",
                     "classification_source", "workflow_status", "pdf_quality_status", "pdf_quality_score",
                     "pdf_quality_report", "workspace_path", "created_at", "serial_number", "paper_code")}
                payload.update(paper_id=paper.id, library_name=normalize_library_name(paper.library_name),
                    title_zh=analysis.get("title_zh") if isinstance(analysis.get("title_zh"), str) else None,
                    abstract_zh=analysis.get("abstract_zh") if isinstance(analysis.get("abstract_zh"), str) else None,
                    comprehensive_analysis=paper.comprehensive_analysis, counts=counts[paper.id],
                    pdf_exists=file_size is not None, pdf_size=file_size, pdf_file_size=file_size,
                    pdf_artifact_status={"pdf_exists": file_size is not None, "pdf_file_size": file_size},
                    has_parsed_content=bool(paper.abstract or any(counts[paper.id][k] for k in ("sections", "tables", "figures", "dft_results"))),
                    relationship_summary=relationships[paper.id])
                if (metadata := impact.get(paper.id)) is not None:
                    payload.update(impact_factor=metadata.impact_factor, impact_factor_source=metadata.impact_factor_source,
                                   impact_factor_year=metadata.impact_factor_year)
                result.append(PaperListItemResponse(**payload))
            return result
