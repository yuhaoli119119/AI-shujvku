"""Stored paper contents for the mature reader, without legacy repair/review jobs.

Reading a paper never backfills codes, syncs tasks, computes approval, or writes.
Unavailable review projections keep their schema defaults; stored candidates
retain their actual status and are never promoted to approved scientific data.
"""
from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models
from app.schemas import api
from app.services.paper_list_readonly import COUNT_MODELS
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.library_names import normalize_library_name


def _stored(row, schema):
    """Project stored fields only; do not manufacture computed review fields."""
    columns = {column.key for column in row.__table__.columns}
    return schema(**{name: getattr(row, name) for name, field in schema.model_fields.items()
                     if name in columns and not (getattr(row,name) is None and field.default_factory is not None)})


class PaperDetailReadonlyService:
    def __init__(self, session: Session):
        self.session = session

    def _rows(self, model, paper_id):
        return list(self.session.scalars(select(model).where(model.paper_id == paper_id).order_by(model.id)))

    def get_paper_detail(self, paper_id: UUID, *, compact=False, chart_run_id=None,
                         include_expensive_status=True, include_dft_payload=True,
                         include_mechanism_claims_payload=True):
        with self.session.no_autoflush:
            paper = self.session.get(models.Paper, paper_id)
            if paper is None:
                return None
            base = {name:getattr(paper,name) for name in api.PaperListItemResponse.model_fields
                    if name in {c.key for c in paper.__table__.columns}}
            base.update(paper_id=paper.id, library_name=normalize_library_name(paper.library_name))
            if base.get('authors') is None:
                base['authors'] = []
            analysis = paper.comprehensive_analysis or {}
            for key in ('title_zh', 'abstract_zh'):
                base[key] = analysis.get(key) if isinstance(analysis.get(key), str) else None
            counts = {key: int(self.session.scalar(select(func.count(model.id)).where(model.paper_id == paper_id)) or 0)
                      for key, model in COUNT_MODELS.items()}
            counts['comprehensive_analysis'] = int(bool(analysis))
            status = build_paper_artifact_status(paper, settings=get_settings())
            base.update(counts=counts, artifact_status=status, pdf_artifact_status=status,
                        pdf_exists=status['pdf_exists'], pdf_size=status['pdf_file_size'],
                        pdf_file_size=status['pdf_file_size'], pdf_path_kind=status['pdf_path_kind'],
                        has_parsed_content=bool(paper.abstract or any(counts[k] for k in ('sections','tables','figures','dft_results'))),
                        reading_guide=paper.reading_guide)
            metadata = self.session.scalar(select(models.PaperImpactMetadata).where(models.PaperImpactMetadata.paper_id == paper_id))
            if metadata:
                base.update(impact_factor=metadata.impact_factor, impact_factor_source=metadata.impact_factor_source,
                            impact_factor_year=metadata.impact_factor_year)
            figures = [_stored(row, api.PaperFigureResponse) for row in self._rows(models.PaperFigure, paper_id)]
            for figure in figures:
                reference = canonicalize_persisted_artifact_reference(figure.image_path, category='figures',
                              settings=get_settings()) if figure.image_path else None
                figure.asset_url = '/api/papers/assets/' + quote(reference, safe='/') if reference else None
            base['figures'] = sorted(figures, key=lambda f: (f.page is None, f.page or 0, f.figure_label or '', str(f.id)))
            outgoing = list(self.session.scalars(select(models.PaperRelationship).where(models.PaperRelationship.source_paper_id == paper_id)))
            incoming = list(self.session.scalars(select(models.PaperRelationship).where(models.PaperRelationship.target_paper_id == paper_id)))
            related_ids = {r.target_paper_id for r in outgoing} | {r.source_paper_id for r in incoming}
            related = {p.id: p for p in self.session.scalars(select(models.Paper).where(models.Paper.id.in_(related_ids)))} if related_ids else {}
            def relationship(row, other_id):
                item = _stored(row, api.PaperRelationshipItemResponse)
                other = related.get(other_id)
                return item.model_copy(update={'related_paper_code': other.paper_code if other else None,
                                               'related_paper_title': other.title if other else None})
            base['outgoing_relationships'] = [relationship(r, r.target_paper_id) for r in outgoing]
            base['incoming_relationships'] = [relationship(r, r.source_paper_id) for r in incoming]
            base['relationship_summary'] = {kind: sum(r.relationship_type == kind for r in outgoing) for kind in {r.relationship_type for r in outgoing}}
            tables = self._rows(models.PaperTable, paper_id)
            supplementary = {r.target_paper_id for r in outgoing if r.relationship_type in {'supplementary','supplementary_information','supporting_information','si'}}
            if supplementary:
                tables += list(self.session.scalars(select(models.PaperTable).where(models.PaperTable.paper_id.in_(supplementary)).order_by(models.PaperTable.id)))
            base['tables'] = []
            for row in tables:
                item = _stored(row, api.PaperTableResponse)
                if row.paper_id != paper_id:
                    other = related.get(row.paper_id)
                    item = item.model_copy(update={'source_document_type':'supplementary_information',
                        'related_paper_id':row.paper_id, 'related_paper_code':other.paper_code if other else None,
                        'related_paper_title':other.title if other else None, 'writeback_paper_id':paper_id})
                base['tables'].append(item)
            if not compact:
                base['sections'] = [_stored(row, api.PaperSectionResponse) for row in self._rows(models.PaperSection, paper_id)]
                base['references'] = [_stored(row, api.ReferenceEntryResponse) for row in self._rows(models.ReferenceEntry, paper_id)]
                notes = list(self.session.scalars(select(models.PaperNote).where(models.PaperNote.paper_id == paper_id).order_by(models.PaperNote.created_at.desc(), models.PaperNote.id).limit(30)))
                base['paper_notes'] = [{c.key: getattr(n,c.key) for c in n.__table__.columns} for n in notes if n.source != 'translation_preview']
                translation = self.session.scalar(select(models.PaperNote.content).where(models.PaperNote.paper_id == paper_id,
                    models.PaperNote.source == 'translation_preview', models.PaperNote.field_name == 'full_translation_preview').order_by(models.PaperNote.created_at.desc()).limit(1))
                base['full_translation_zh'] = translation
                for field, model, schema in [('writing_cards_items',models.WritingCard,api.WritingCardResponse),
                    ('figure_data_points_items',models.FigureDataPoint,api.FigureDataPointResponse)]:
                    base[field] = [_stored(row,schema) for row in self._rows(model,paper_id)]
                if include_mechanism_claims_payload:
                    base['mechanism_claims_items'] = [_stored(row, api.MechanismClaimResponse) for row in self._rows(models.MechanismClaim,paper_id)]
                if include_dft_payload:
                    for field, model, schema in [('dft_settings_items',models.DFTSetting,api.DFTSettingResponse),
                        ('catalyst_samples_items',models.CatalystSample,api.CatalystSampleResponse),
                        ('electrochemical_performance_items',models.ElectrochemicalPerformance,api.ElectrochemicalPerformanceResponse)]:
                        base[field] = [_stored(row,schema) for row in self._rows(model,paper_id)]
                    page = self.get_dft_results_page(paper_id)
                    base['dft_results_items'] = page['items']
                    base['dft_results_page'] = {k:v for k,v in page.items() if k != 'items'}
            return api.PaperDetailResponse(**base)

    def get_paper_dft_detail(self, paper_id):
        return self.get_paper_detail(paper_id, include_mechanism_claims_payload=False)

    def get_dft_results_page(self, paper_id, *, offset=0, limit=50, result_id=None):
        with self.session.no_autoflush:
            if self.session.get(models.Paper,paper_id) is None:
                return None
            query = select(models.DFTResult).where(models.DFTResult.paper_id == paper_id)
            total = int(self.session.scalar(select(func.count(models.DFTResult.id)).where(models.DFTResult.paper_id == paper_id)) or 0)
            query = query.where(models.DFTResult.id == result_id) if result_id else query.order_by(models.DFTResult.id).offset(offset).limit(limit)
            rows = list(self.session.scalars(query))
            sample_ids = {row.catalyst_sample_id for row in rows if row.catalyst_sample_id is not None}
            samples = {row.id:row for row in self.session.scalars(select(models.CatalystSample).where(models.CatalystSample.id.in_(sample_ids)))} if sample_ids else {}
            items = []
            for row in rows:
                sample = samples.get(row.catalyst_sample_id)
                payload = _stored(row,api.DFTResultResponse).model_copy(update={
                    'material_binding_status':'bound' if sample else ('invalid_reference' if row.catalyst_sample_id else 'unbound'),
                    'bound_catalyst_sample':_stored(sample,api.CatalystSampleResponse).model_dump(exclude_unset=True) if sample else None,
                    'dft_workflow_state':'not_evaluated', 'dft_workflow_label':'未评估',
                    'dft_workflow_reason':'当前只读详情未计算历史出口审核。',
                    'export_safety':{'status':'not_evaluated','eligible':None,'is_exportable':None,
                                     'reason':'当前只读详情未计算历史出口审核。'},
                })
                items.append(payload)
            return {'paper_id':str(paper_id),'items':items,'offset':offset,'limit':limit,
                    'returned':len(items),'total':total,'has_more':result_id is None and offset+len(items)<total}
