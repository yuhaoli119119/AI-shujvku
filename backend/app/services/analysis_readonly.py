"""Stored observations for the mature analysis UI; no legacy review execution.

The source families remain separate. Approval is unknown, while finite values
with explicit compatible units may be explored with local exclusions and sources.
No method writes, commits, fills metadata, or calls a review/export safety gate.
"""
from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
import pickle
from functools import lru_cache
from contextlib import contextmanager
from threading import RLock
import csv
import io
import json
import math
import hashlib
from types import SimpleNamespace
from typing import Any

from sqlalchemy import case, select, text
from sqlalchemy.orm import Session

from app.db import models as m
from app.normalizers.chemistry_normalizer import get_property_taxonomy
from app.services.catalyst_analysis_service import CatalystAnalysisService, _ReadyRow, _candidate_groups, _stats
from app.services.dft_ml_policy import analysis_entity_id
from app.services.rebuild_analysis_fields import DEFINITIONS, PRESET_FIELDS, project_rows, source_ids
from app.services.rebuild_analysis_pairing import pair_rows, catalyst_entity_id, context as pair_context
from app.utils.library_names import normalize_library_name


# Two detached projections per worker, never an ORM identity map/session.
# Pickle bytes are generated here from DB projections only, never from a file
# or request, and deserialized to independent object graphs for each caller. Every
# reuse is checked against the actual database content, not a TTL or row count.
_PROJECTIONS = OrderedDict()
_PROJECTION_LOCK = RLock()
_PROJECTION_BUILDS = {}
_REVISION_RESULTS = OrderedDict()
_COMPARE_RESULTS = OrderedDict()
_ANALYSIS_OUTPUTS = OrderedDict()
_ANALYSIS_OUTPUT_BYTES = 32 * 1024 * 1024
_COMPARE_CACHE_BYTES = 128 * 1024 * 1024



@contextmanager
def _projection_build(key):
    # Only identical cold requests wait on each other. Unrelated scopes never
    # hold the cache mutex during database reads or projection work.
    with _PROJECTION_LOCK:
        entry = _PROJECTION_BUILDS.setdefault(key, [RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _PROJECTION_LOCK:
            entry[1] -= 1
            if not entry[1]:
                del _PROJECTION_BUILDS[key]



@lru_cache(maxsize=512)
def _taxonomy(prop):
    return get_property_taxonomy(prop)


def _content_revision(session):
    # One MVCC snapshot of visible row versions detects inserts, deletes and
    # same-count corrections without detoasting/serializing all source quotes.
    # xmin changes on every UPDATE; ctid also detects multiple updates within a
    # transaction. Include table incarnation for truncate/rewrite. Paper fields
    # retain their content hash so unrelated processing-status writes do not
    # evict scientific projections. No statistics estimates or TTL are used.
    parts = ["(SELECT md5(COALESCE(string_agg(md5(row_to_json(r)::text), '' ORDER BY r.id), '')) FROM (SELECT id, library_name, paper_code, title, doi, journal, year FROM papers) r)"]
    for table in ('rebuild_data_rows', 'rebuild_data_values', 'rebuild_value_sources'):
        parts.append("(SELECT md5(COALESCE(string_agg(id::text || ':' || xmin::text || ':' || ctid::text, ',' ORDER BY id), '')) || ':' || pg_relation_filenode('" + table + "'::regclass)::text FROM " + table + ")")
    return tuple(session.execute(text('SELECT ' + ', '.join(parts))).one())


POLICY = 'Stored observations for exploratory analysis; approval not evaluated. Missing, estimated, conflicting and incompatible cells remain local exclusions; source families are never merged.'
ENERGY = {'ev': 1., 'mev': .001, 'hartree': 27.211386245988, 'ha': 27.211386245988,
          'kj/mol': 1 / 96.4853321233, 'kcal/mol': 1 / 23.0605478306}
LENGTH = {'å': 1., 'a': 1., 'angstrom': 1., 'angstroms': 1., 'nm': 10., 'pm': .01}


def unresolved_conflict(conflict):
    # Lifecycle declarations are provenance audit, not scientific disagreement.
    # Project locally: retain all stored keys and preserve conservative handling
    # of every other legacy/unknown conflict shape. Both value eligibility and
    # adsorbate-cell selection use this classifier.
    if isinstance(conflict, dict):
        conflict = {key: value for key, value in conflict.items() if key not in {'source_lifecycle', 'source_page_identities'}}
    if not conflict:
        return False
    if isinstance(conflict, dict) and conflict.get('status') in {'evidence_history', 'corrected_previous_retained'}:
        return bool(conflict.get('legacy_unresolved'))
    return True


def plain(obj):
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


def normalize_numeric(prop, value, unit):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None, None, 'missing_value'
    if not math.isfinite(value):
        return None, None, 'nonfinite_value'
    key = str(unit or '').strip().casefold().replace(' ', '')
    canonical = _taxonomy(prop).get('canonical_property_type') or prop
    prop = str(prop or '').lower()
    if prop.endswith('_ev') or any(token in prop for token in ('energy', 'barrier', 'band_center', 'delta_g')) or canonical in {'adsorption_energy', 'reaction_barrier', 'activation_energy', 'gibbs_free_energy_change', 'd_band_center'}:
        factor = ENERGY.get(key)
        return (value * factor, 'eV', None) if factor is not None else (None, None, 'unknown_or_incompatible_energy_unit')
    if 'bond' in prop or 'length' in prop:
        factor = LENGTH.get(key)
        return (value * factor, 'Å', None) if factor is not None else (None, None, 'unknown_or_incompatible_length_unit')
    if 'bader' in prop or prop in {'charge_transfer','aggregate_charge_transfer'}:
        return (value, 'e', None) if key in {'e', '|e|', 'electron', 'electrons'} else (None, None, 'unknown_or_incompatible_charge_unit')
    if not key or key in {'unknown', 'n/a', 'none', 'null'}:
        return None, None, 'unknown_unit'
    # Other properties remain visible/exportable as observations. They are not
    # mapped into the fixed catalyst field registry or pooled across units.
    return value, str(unit).strip(), None


class AnalysisReadonlyService(CatalystAnalysisService):
    def __init__(self, session: Session, *, source='rebuild', library_name=None, filters=None, include_estimated=False, include_unlabelled=False):
        super().__init__(session)
        if source not in {'legacy', 'rebuild'}:
            raise ValueError('source must be legacy or rebuild; sources cannot be merged')
        self.source = source
        self.library_name = library_name
        self.filters = filters or {}
        self.include_estimated = include_estimated
        self.include_unlabelled = include_unlabelled
        self._loaded = None
        self.records = []
        self._candidate_cache = {}
        self._matrix_mode = False

    def cached_output(self, name, parameters, build):
        """Cache detached scientific output only; never authentication/policy."""
        if self.source != 'rebuild' or self.session.get_bind().dialect.name != 'postgresql':
            return build()
        key = (str(self.session.get_bind().url), _content_revision(self.session), name,
               self.library_name, json.dumps(self.filters, sort_keys=True, default=str),
               self.include_estimated, self.include_unlabelled,
               json.dumps(parameters, sort_keys=True, default=str))
        with _projection_build(('output', key)):
            with _PROJECTION_LOCK:
                cached = _ANALYSIS_OUTPUTS.get(key)
                if cached is not None:
                    _ANALYSIS_OUTPUTS.move_to_end(key)
            if cached is not None:
                return pickle.loads(cached)
            result = build()
            if key[1] == _content_revision(self.session):
                detached = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
                if len(detached) <= _ANALYSIS_OUTPUT_BYTES:
                    with _PROJECTION_LOCK:
                        _ANALYSIS_OUTPUTS[key] = detached
                        while len(_ANALYSIS_OUTPUTS) > 32 or sum(map(len, _ANALYSIS_OUTPUTS.values())) > _ANALYSIS_OUTPUT_BYTES:
                            _ANALYSIS_OUTPUTS.popitem(last=False)
            return result

    def _papers(self):
        columns = [getattr(m.Paper, k) for k in ('id','library_name','paper_code','title','doi','journal','year')]
        # Normalize the stored name without migrations or registry side effects.
        if self.source == 'legacy':
            papers = list(self.session.scalars(select(m.Paper).order_by(m.Paper.id)))
        else:
            query = select(*columns).order_by(m.Paper.id)
            papers = [SimpleNamespace(**r) for r in self.session.execute(query).mappings()]
        if self.library_name is not None:
            papers = [p for p in papers if normalize_library_name(p.library_name) == normalize_library_name(self.library_name)]
        return {p.id: p for p in papers if (not self.filters.get('paper_id') or str(p.id) == str(self.filters['paper_id'])) and (self.filters.get('year_min') is None or p.year is not None and p.year >= self.filters['year_min']) and (self.filters.get('year_max') is None or p.year is not None and p.year <= self.filters['year_max'])}

    def _matches(self, record):
        f = self.filters
        for key in ('property_type', 'adsorbate', 'catalyst_name', 'catalyst_type', 'reaction', 'material_family', 'data_type'):
            needle = f.get(key)
            if not needle:
                continue
            if key == 'property_type':
                wanted = _taxonomy(needle).get('canonical_property_type') or needle
                if str(record['canonical_property_type']).casefold() != str(wanted).casefold() and str(record['property_type']).casefold() != str(needle).casefold():
                    return False
            elif key == 'data_type' and needle != record.get('data_type'):
                return False
            elif str(needle).casefold() not in str(record.get(key) or '').casefold():
                return False
        minimum = f.get('min_confidence')
        if minimum is not None and (record['confidence'] is None or record['confidence'] < minimum):
            return False
        if f.get('paper_id') and str(record['paper_id']) != str(f['paper_id']):
            return False
        return True

    def _load_rows(self, library_name=None, *, pair_analysis=False):
        if self._loaded is not None:
            return self._loaded
        if self.source != 'rebuild' or self.session.get_bind().dialect.name != 'postgresql':
            return self._read_rows()
        # Validate outside the lock; warm requests can deserialize concurrently.
        revision = _content_revision(self.session)
        key = (str(self.session.get_bind().url), revision,
               normalize_library_name(self.library_name) if self.library_name is not None else None,
               json.dumps(self.filters, sort_keys=True, default=str), self.include_estimated, self.include_unlabelled)
        with _PROJECTION_LOCK:
            cached = _PROJECTIONS.get(key)
            if cached is not None:
                _PROJECTIONS.move_to_end(key)
        if cached is not None:
            self.records, self._loaded = pickle.loads(cached)
            return self._loaded
        # Serialize cold builds so simultaneous fields/matrix/revision requests
        # share one projection. Each caller receives an independent object graph.
        with _projection_build(key):
            with _PROJECTION_LOCK:
                cached = _PROJECTIONS.get(key)
                if cached is not None:
                    _PROJECTIONS.move_to_end(key)
            if cached is None:
                loaded = self._read_rows()
                # Never publish a concurrent, mixed readback under an old token.
                if revision == _content_revision(self.session):
                    detached = pickle.dumps((self.records, loaded), protocol=pickle.HIGHEST_PROTOCOL)
                    with _PROJECTION_LOCK:
                        _PROJECTIONS[key] = detached
                        while len(_PROJECTIONS) > 2:
                            _PROJECTIONS.popitem(last=False)
                return loaded
        self.records, self._loaded = pickle.loads(cached)
        return self._loaded

    def _read_rows(self, *, index_only=False, value_ids=None):
        with self.session.no_autoflush:
            papers = self._papers()
            ids = list(papers)
            catalysts = list(self.session.scalars(select(m.CatalystSample).where(m.CatalystSample.paper_id.in_(ids)))) if ids and self.source == 'legacy' else []
            cat_by_id = {c.id: c for c in catalysts}
            settings = list(self.session.scalars(select(m.DFTSetting).where(m.DFTSetting.paper_id.in_(ids)))) if ids and self.source == 'legacy' else []
            settings_by_paper = defaultdict(list)
            for item in settings:
                settings_by_paper[item.paper_id].append(item)
            raw = []
            if self.source == 'legacy' and ids:
                rows = list(self.session.scalars(select(m.DFTResult).where(m.DFTResult.paper_id.in_(ids)).order_by(m.DFTResult.id)))
                locators = list(self.session.scalars(select(m.EvidenceLocator).where(m.EvidenceLocator.paper_id.in_(ids), m.EvidenceLocator.target_type.in_(['dft_results', 'dft_result']))))
                by_target = defaultdict(list)
                for loc in locators:
                    by_target[str(loc.target_id)].append({k: v for k, v in plain(loc).items() if k not in {'created_at','updated_at'}})
                for row in rows:
                    cat = cat_by_id.get(row.catalyst_sample_id)
                    payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
                    context = payload.get('context') if isinstance(payload.get('context'), dict) else {}
                    display_name = cat.name if cat else payload.get('material_identity') or context.get('material_identity') or payload.get('material')
                    raw.append((row, papers[row.paper_id], cat, {'source_family': 'legacy', 'sources': by_target[str(row.id)], 'catalyst_name': display_name, 'estimated': bool(payload.get('is_estimated') or payload.get('estimated') or row.value_kind in {'estimated', 'digitized'}), 'conflict': payload.get('conflict'), 'analysis_context': {'source_family':'legacy', 'reaction_type': row.reaction_type}}))
            elif self.source == 'rebuild' and ids:
                row_query = select(m.RebuildDataRow.__table__).where(m.RebuildDataRow.paper_id.in_(ids))
                # Apply the exact row-level predicate before fetching cells and
                # evidence. Keep adsorbate/alias filters in the existing mapper.
                if self.filters.get('data_type'):
                    row_query = row_query.where(m.RebuildDataRow.data_type == self.filters['data_type'])
                if value_ids is not None:
                    row_query = row_query.where(m.RebuildDataRow.id.in_(select(m.RebuildDataValue.row_id).where(m.RebuildDataValue.id.in_(value_ids))))
                rows = [SimpleNamespace(**r) for r in self.session.execute(row_query).mappings()]
                by_row = {r.id: r for r in rows}
                value_columns = [m.RebuildDataValue.__table__]
                if index_only:
                    v = m.RebuildDataValue
                    value_columns = [v.id, v.row_id, v.field_name, v.numeric_value,
                                     case((v.field_name == 'adsorbate', v.raw_value)).label('raw_value'),
                                     case((v.field_name == 'adsorbate', v.conflict)).label('conflict'), v.is_estimated]
                value_query = select(*value_columns).where(m.RebuildDataValue.row_id.in_(by_row)).order_by(m.RebuildDataValue.id)
                if value_ids is not None:
                    value_query = value_query.where((m.RebuildDataValue.id.in_(value_ids)) | (m.RebuildDataValue.field_name == 'adsorbate'))
                values = [SimpleNamespace(**r) for r in self.session.execute(value_query).mappings()] if by_row else []
                source_columns = [m.RebuildValueSource.id, m.RebuildValueSource.value_id] if index_only else [m.RebuildValueSource.__table__]
                source_query = select(*source_columns).where(m.RebuildValueSource.row_id.in_(by_row))
                if index_only:
                    # Sorting/filtering needs only verified adsorbate cells, not
                    # the source quotes of every measurement in the library.
                    source_query = source_query.where(m.RebuildValueSource.value_id.in_([v.id for v in values if v.field_name == 'adsorbate']))
                elif value_ids is not None:
                    source_query = source_query.where(m.RebuildValueSource.value_id.in_([v.id for v in values]))
                sources = [dict(r) for r in self.session.execute(source_query).mappings()] if by_row else []
                from app.services.rebuild_source_lifecycle import source_is_current
                value_map = {v.id: v for v in values}
                by_value = defaultdict(list)
                for item in sources:
                    if item['value_id'] not in value_map or not source_is_current(value_map[item['value_id']], item['id']):
                        continue
                    identity = ((value_map[item['value_id']].conflict or {}).get('source_page_identities') or {}).get(str(item['id']), {})
                    by_value[item['value_id']].append({**{k: v for k, v in item.items() if k != 'created_at'}, **identity,
                        'page_identity_status': 'verified' if identity else 'legacy_unverified'})
                adsorbate_cells = {v.row_id: v.raw_value for v in values if v.field_name == 'adsorbate' and isinstance(v.raw_value, str) and v.raw_value and by_value[v.id] and not unresolved_conflict(v.conflict) and not v.is_estimated}
                index_records = []
                for value in values:
                    if value_ids is not None and str(value.id) not in value_ids:
                        continue
                    r = by_row[value.row_id]
                    # These are transient projections, never ORM changes. Known
                    # rebuild identity remains within its own paper and row.
                    prop, ads = value.field_name, (r.properties or {}).get('adsorbate') or (r.condition or {}).get('adsorbate') or adsorbate_cells.get(r.id)
                    # Field aliases describe the quantity, not evidence for its
                    # object. Keep explicit/unknown/missing adsorbates intact so
                    # project_rows can detect dedicated-field contradictions.
                    if prop.endswith('_adsorption_energy'):
                        prop = 'adsorption_energy'
                    elif prop == 'li2s_bader_charge_transfer':
                        prop = 'bader_charge_transfer'
                    if index_only:
                        record = {'record_id':str(value.id),'paper_id':str(r.paper_id),
                                  'property_type':prop,'canonical_property_type':_taxonomy(prop).get('canonical_property_type') or prop,
                                  'value':value.numeric_value,'adsorbate':ads,'catalyst_name':r.material,
                                  'catalyst_type':r.active_site_type,'reaction':r.reaction,
                                  'material_family':r.material_family,'data_type':r.data_type,'confidence':None}
                        if self._matches(record):
                            index_records.append(record)
                        continue
                    identity = {'subject': {'property_context': {'configuration':r.configuration}, 'canonical_atom_pair': 'Li1-S' if value.field_name == 'li1_s_bond_length' else 'Li2-S' if value.field_name == 'li2_s_bond_length' else None}}
                    row = SimpleNamespace(id=value.id, paper_id=r.paper_id, catalyst_sample_id=None, adsorbate=ads, property_type=prop, value=value.numeric_value, unit=value.unit, reaction_step=(r.properties or {}).get('reaction_step'), reaction_type=r.reaction, source_section=None, source_figure=None, evidence_text='\n'.join(s.get('quote') or '' for s in by_value[value.id]), confidence=None, candidate_status='stored_rebuild', evidence_payload={'analysis_entity_id':str(r.id)}, identity_payload=identity, identity_version=2, value_kind=value.value_type)
                    cat = SimpleNamespace(id='rebuild:' + str(r.id), paper_id=r.paper_id, name=r.material, catalyst_type=r.active_site_type, metal_centers=[], coordination=r.active_site, support=r.support)
                    raw.append((row,papers[r.paper_id],cat,{'source_family':'rebuild','sources':by_value[value.id], 'catalyst_name':r.material,'active_site':r.active_site,'support':r.support,'configuration':r.configuration,'material_family':r.material_family,'estimated':value.is_estimated,'conflict':value.conflict,'missing_reason':value.missing_reason,'identity_pending':bool((r.properties or {}).get('_rebuild_import', {}).get('pending_identity_fields')),'raw_value':value.raw_value,'rebuild_row_id':str(r.id),'analysis_context':{'source_family':'rebuild','reaction_type':r.reaction,'data_type':r.data_type,'configuration':r.configuration,'condition_key':json.dumps(r.condition or {},sort_keys=True,ensure_ascii=False)}, 'field_name':value.field_name,'data_type':r.data_type}))
                if index_only:
                    return index_records
            if index_only:
                return []
            ready, exclusions, records = [], Counter(), []
            for row,paper,cat,extra in raw:
                taxonomy = _taxonomy(row.property_type)
                normalized_value, normalized_unit, reason = normalize_numeric(row.property_type,row.value,row.unit)
                unit_unlabelled = reason == 'unknown_unit'
                if unit_unlabelled and self.include_unlabelled:
                    normalized_value, normalized_unit, reason = float(row.value), None, None
                if extra.get('estimated') and not self.include_estimated:
                    reason = 'estimated_excluded'
                elif unresolved_conflict(extra.get('conflict')):
                    reason = 'unresolved_conflict'
                elif extra.get('identity_pending'):
                    reason = 'identity_pending'
                elif self.source == 'rebuild' and not extra['sources'] and row.value is not None:
                    reason = 'missing_source'
                elif row.value_kind in {'range','interval','upper_bound','lower_bound'}:
                    reason = 'non_scalar_value'
                record = {'record_id':str(row.id),'paper_id':str(paper.id),'paper_code':paper.paper_code,'title':paper.title,'doi':paper.doi,'journal':paper.journal,'year':paper.year,'library_name':normalize_library_name(paper.library_name),'property_type':row.property_type,'canonical_property_type':taxonomy.get('canonical_property_type') or row.property_type,'normalized_property_type':taxonomy.get('canonical_property_type') or row.property_type,'property_subtype':taxonomy.get('property_subtype'),'adsorbate':row.adsorbate,'reaction':row.reaction_type,'reaction_step':row.reaction_step,'value':row.value,'raw_value':extra.get('raw_value',row.value),'unit':row.unit,'raw_unit':row.unit,'confidence':row.confidence,'candidate_status':row.candidate_status,'assessment_status':'not_evaluated','review_status':'not_evaluated','validation_status':'not_evaluated','is_exportable':None,'is_ml_ready':None,'export_safety':{'eligible':None,'assessment_status':'not_evaluated'},'evidence_text':row.evidence_text,'evidence_payload':row.evidence_payload,'source_section':row.source_section,'source_figure':row.source_figure,'catalyst_name':extra['catalyst_name'],'display_catalyst_name':extra['catalyst_name'],'catalyst_type':cat.catalyst_type if cat else None,'catalysts':[{'id':str(cat.id),'name':cat.name,'type':cat.catalyst_type,'support':cat.support,'coordination':cat.coordination,'metal_centers':cat.metal_centers}] if cat else [],'material_binding_status':'bound' if row.catalyst_sample_id else 'stored_rebuild_identity' if self.source=='rebuild' else 'derived_from_evidence' if extra['catalyst_name'] else 'unbound','target':{'normalized_value':normalized_value,'normalized_unit':normalized_unit,'normalization_status':'normalized' if reason is None else reason},'exploratory_eligible':reason is None,'exclusion_reason':reason,'analysis_entity_id':analysis_entity_id(row),**extra}
                record['unit_unlabelled'] = unit_unlabelled
                if unit_unlabelled and reason is None:
                    record['target']['normalization_status'] = 'unit_unlabelled'
                if not self._matches(record):
                    continue
                records.append(record)
                if reason:
                    exclusions[reason] += 1
                    continue
                possible = settings_by_paper[row.paper_id]
                explicit = (row.identity_payload or {}).get('dft_setting_id') or (row.evidence_payload or {}).get('dft_setting_id')
                chosen = next((s for s in possible if str(s.id)==str(explicit)), None) if explicit else possible[0] if len(possible)==1 else None
                record['linked_dft_setting'] = {'dft_setting_id':str(chosen.id),'functional':chosen.functional,'dispersion_correction':chosen.dispersion_correction} if chosen else {}
                ready.append(_ReadyRow(row,paper,record,cat, analysis_entity_id=record['analysis_entity_id']))
            self.records = records
            counts = {'total_dft_rows':len(records),'numeric_records':sum(isinstance(r['value'],(int,float)) and math.isfinite(r['value']) for r in records),'exploratory_numeric_records':len(ready),'pair_analysis_ready_numeric_rows':len(ready),'distinct_exploratory_catalysts':len({catalyst_entity_id(r.record) if self.source=='rebuild' else str(r.catalyst.id) if r.catalyst else r.analysis_entity_id for r in ready}),'distinct_exportable_catalysts':None,'exportable_dft_rows':None,'v2_row_ready_numeric_rows':None,'contributing_papers':len({str(r.paper.id) for r in ready}),'legacy_pair_analysis_rows':sum(r.row.identity_version!=2 for r in ready),'assessment_status':'not_evaluated','source':self.source}
            self._loaded = ready,exclusions,counts
            return self._loaded

    def summary(self):
        if hasattr(self, "_summary_cache"):
            return dict(self._summary_cache)
        _,excluded,counts = self._load_rows()
        physical_ids={i for fields in self._rebuild_fields().values() for cell in fields.values() if cell['exploratory_eligible'] for i in source_ids(cell)} if self.source=='rebuild' else set()
        self._summary_cache = {**counts,'physical_numeric_records':len(physical_ids) if self.source=='rebuild' else counts['numeric_records'],'total_records':counts['total_dft_rows'],'approved_count':None,'excluded_reasons':dict(excluded),'selection_policy':POLICY}
        return dict(self._summary_cache)

    def data_revision(self):
        """Fingerprint the current readback, including evidence and identities.

        Every new request validates database content before reusing a detached
        projection. Corrections that leave record counts unchanged are included.
        """
        cache_key = None
        if self.source == 'rebuild' and self.session.get_bind().dialect.name == 'postgresql':
            cache_key = (str(self.session.get_bind().url), _content_revision(self.session),
                         self.library_name, json.dumps(self.filters, sort_keys=True, default=str),
                         self.include_estimated, self.include_unlabelled)
            with _PROJECTION_LOCK:
                cached = _REVISION_RESULTS.get(cache_key)
                if cached is not None:
                    _REVISION_RESULTS.move_to_end(cache_key)
                    return dict(cached)
        self._load_rows()
        keys=('record_id','paper_id','field_name','catalyst_name','active_site','support',
              'configuration','reaction','adsorbate','raw_value','raw_unit','target',
              'exclusion_reason','analysis_context','sources')
        snapshot=[{k:r.get(k) for k in keys} for r in sorted(self.records,key=lambda r:r['record_id'])]
        digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
        result = {'source':self.source,'revision':digest,'record_count':len(snapshot)}
        if cache_key is not None and cache_key[1] == _content_revision(self.session):
            with _PROJECTION_LOCK:
                _REVISION_RESULTS[cache_key] = result
                while len(_REVISION_RESULTS) > 16:
                    _REVISION_RESULTS.popitem(last=False)
        return dict(result)

    def decorate(self,payload):
        payload.update(source=self.source,source_family=self.source,assessment_status='not_evaluated',export_mode='exploratory',selection_policy=POLICY)
        if self.include_unlabelled:
            payload['selection_policy'] = 'Exploratory observations include explicitly flagged values with unlabelled units; these values are not normalized and must not be pooled as physical measurements. Approval not evaluated.'
        referenced=set()
        def collect(obj):
            if isinstance(obj,dict):
                for key,value in obj.items():
                    if key in {'source_record_ids','x_source_record_ids','y_source_record_ids'}:
                        referenced.update(str(v) for v in value or [])
                    elif key in {'source_record_id','record_id'} and value:
                        referenced.add(str(value))
                    elif key not in {'record_sources','evidence_payload','sources'}:
                        collect(value)
            elif isinstance(obj,list):
                for item in obj:collect(item)
        collect(payload)
        payload['record_sources']={r['record_id']:{k:r.get(k) for k in ('record_id','source_family','paper_id','paper_code','sources','evidence_text','source_section','source_figure','raw_value','raw_unit','target','conflict','exclusion_reason','rebuild_row_id')} for r in self.records if r['record_id'] in referenced}
        return payload

    def _analysis_candidate_groups(self,field,rows):
        key=(field,tuple(str(r.row.id) for r in rows))
        if key not in self._candidate_cache:
            self._candidate_cache[key]=_candidate_groups(field,rows)
        return self._candidate_cache[key]

    def field_registry(self):
        if self.source == 'legacy':
            return super().field_registry()
        self._load_rows()
        coverage=Counter(key for fields in self._rebuild_fields().values() for key, cell in fields.items() if cell['exploratory_eligible'])
        return [{**definition, 'available_count':coverage[key], 'available':bool(coverage[key]),
                 'selection_policy':POLICY, 'selection_rule':'Exact physical aliases; same rebuild row; source and compatible unit required'}
                for key,definition in DEFINITIONS.items() if coverage[key] or key in PRESET_FIELDS]

    def _rebuild_fields(self):
        self._load_rows()
        if not hasattr(self, '_rebuild_fields_cache'):
            self._rebuild_fields_cache = project_rows(self.records)
        return self._rebuild_fields_cache

    def catalyst_dataset(self, library_name=None):
        if self.source == 'rebuild':
            self._load_rows()
            definitions=self.field_registry()
            fields=[f['key'] for f in definitions]
            grouped=defaultdict(list)
            for record in self.records:
                grouped[record['rebuild_row_id']].append(record)
            rows=[]; manifest={}
            for identity,records in sorted(grouped.items()):
                first=records[0]
                row={'analysis_entity_id':'rebuild:'+identity,'catalyst_sample_id':None,'catalyst_name':first['catalyst_name'],'paper_id':first['paper_id'],'paper_code':first['paper_code'],'doi':first['doi'],'source_family':'rebuild'}
                field_manifest={}
                by_field=self._rebuild_fields()[identity]
                for field in fields:
                    r=by_field.get(field)
                    valid=bool(r and r['exploratory_eligible'])
                    row[field]=r['target']['normalized_value'] if valid else None
                    field_manifest[field]={'selected_value':row[field],'unit':r['target']['normalized_unit'] if valid else None,'source_record_ids':source_ids(r) if r else [],'candidates':[{'value':r['raw_value'],'unit':r['raw_unit'],'source_record_ids':source_ids(r),'derivation':r.get('derivation'),'conflict':r.get('conflict')}] if r else [],'exclusion_reason':r['exclusion_reason'] if r else 'missing_value'}
                rows.append(row)
                manifest[row['analysis_entity_id']]={'paper':{'paper_id':first['paper_id'],'paper_code':first['paper_code'],'title':first['title']},'fields':field_manifest}
            columns=['analysis_entity_id','catalyst_sample_id','catalyst_name','paper_id','paper_code','doi','source_family']+fields
            return self.decorate({'schema_version':'rebuild_catalyst_exploratory_v1','columns':columns,'field_definitions':definitions,'rows':rows,'row_count':len(rows),'paper_count':len({r['paper_id'] for r in rows}),'manifest':{'catalysts':manifest,'selection_policy':POLICY},'source_counts':self.summary(),'excluded':{'source_row_reasons':self.summary()['excluded_reasons']},'warnings':['approval_not_evaluated']})
        return self.decorate(super().catalyst_dataset(library_name))

    def catalyst_dataset_csv(self, library_name=None):
        payload=self.catalyst_dataset(library_name)
        stream=io.StringIO(newline='')
        columns=['source_family','assessment_status']+[c for c in payload['columns'] if c not in {'source_family','assessment_status'}]
        writer=csv.DictWriter(stream,fieldnames=columns,lineterminator='\r\n');writer.writeheader()
        for row in payload['rows']:
            writer.writerow({c: self.source if c=='source_family' else 'not_evaluated' if c=='assessment_status' else row.get(c) for c in columns})
        return '\ufeff'+stream.getvalue(),payload

    def _rebuild_correlation(self, x_field,y_field,min_n):
        self._load_rows()
        fields=DEFINITIONS
        # Absent numeric fields still return a truthful local empty result;
        # invalid fields are client errors, rather than invented observations.
        if x_field not in DEFINITIONS or y_field not in DEFINITIONS:
            raise ValueError('unknown rebuild analysis field')
        grouped=self._rebuild_fields()
        pairs, excluded, lefts, rights = pair_rows(grouped, x_field, y_field)
        points=[];details=[];reasons=Counter()
        for left,candidates,reason in excluded:
            reasons[reason]+=1
            details.append({'analysis_entity_id':'rebuild:'+left['rebuild_row_id'],'reason':reason,'x_candidates':[left],'y_candidates':candidates})
        for left,right in pairs:
            identity=left['rebuild_row_id']+':'+right['rebuild_row_id']
            points.append({'analysis_entity_id':catalyst_entity_id(left),'pair_observation_id':'rebuild:'+identity,'catalyst_entity_id':catalyst_entity_id(left),'catalyst_name':left['catalyst_name'],'catalyst_sample_id':None,'catalyst':{'catalyst_name':left['catalyst_name']},'paper':{k:left.get(k) for k in ('paper_id','paper_code','title','doi','journal','year')},'x':{'value':left['target']['normalized_value'],'unit':left['target']['normalized_unit'],'source_record_ids':source_ids(left),'candidates':[left]},'y':{'value':right['target']['normalized_value'],'unit':right['target']['normalized_unit'],'source_record_ids':source_ids(right),'candidates':[right]},'x_source_record_ids':source_ids(left),'y_source_record_ids':source_ids(right),'pairing_rule':'same_row' if left['rebuild_row_id']==right['rebuild_row_id'] else 'same_paper_material_compatible_identity_and_method','semantic_context':pair_context(left,right)})
        distributions={axis:[{'value':c['target']['normalized_value'],'unit':c['target']['normalized_unit'],'catalyst_name':c['catalyst_name'],'paper_code':c.get('paper_code'),'source_record_ids':source_ids(c)} for c in cells if c['exploratory_eligible']] for axis,cells in [('x',lefts),('y',rights)]}
        stats=_stats(points) if len(points)>=min_n else {k:None for k in ('pearson','spearman','r_squared','slope','intercept')}
        stats.update(pearson_r=stats['pearson'],spearman_rho=stats['spearman'],r2=stats['r_squared'],ready=len(points)>=min_n,min_n=min_n)
        return {'schema_version':'rebuild_correlation_exploratory_v1','x':fields.get(x_field,{'key':x_field}),'y':fields.get(y_field,{'key':y_field}),'x_field':x_field,'y_field':y_field,'min_n':min_n,'points':points,'distribution':distributions if not self._matrix_mode else {},'n_catalysts':len(points),'n_papers':len({p['paper']['paper_id'] for p in points}),'paper_ids':sorted({p['paper']['paper_id'] for p in points}),'excluded_reasons':dict(reasons),'excluded_details':details,'excluded_count':len(details),'analysis_row_counts':self.summary(),'warnings':['approval_not_evaluated'],'statistics':stats,**stats}

    def correlation(self, *, library_name=None,x_field,y_field,min_n=3):
        payload = self._rebuild_correlation(x_field,y_field,min_n) if self.source == 'rebuild' else super().correlation(library_name=library_name,x_field=x_field,y_field=y_field,min_n=min_n)
        groups=defaultdict(list)
        for point in payload['points']:
            context=point['semantic_context']
            # Individual catalyst ids/settings ids cannot identify cross-material
            # comparability; recorded method/reaction/condition dimensions do.
            key=json.dumps({k:context.get(k) for k in ('paper_id','functional','dispersion_correction','reaction_type','data_type','condition_key','facet','coverage','termination','x_method','y_method','x_reaction_step','y_reaction_step','x_adsorbate','y_adsorbate','x_unit','y_unit')},sort_keys=True,ensure_ascii=False)
            groups[key].append(point)
            point.update(source_family=self.source,assessment_status='not_evaluated',comparability_group=key,group_id=hashlib.sha256(key.encode()).hexdigest()[:12])
        payload['comparability_groups']=[{'group_id':hashlib.sha256(key.encode()).hexdigest()[:12],'label':f"{points[0].get('paper',{}).get('paper_code') or '文献'} · 条件组 {index+1}",'context':json.loads(key),'n':len(points),'statistics':_stats(points) if len(points)>=min_n else None} for index,(key,points) in enumerate(groups.items())]
        payload['group_fit_count']=sum(g['statistics'] is not None and g['statistics']['slope'] is not None for g in payload['comparability_groups'])
        if len(groups)>1:
            payload['ready']=False
            payload['insufficient_reason']='mixed_calculation_or_reaction_contexts; inspect comparability_groups'
            payload['warnings'].append('global_fit_suppressed_mixed_contexts')
            for key in ('pearson','pearson_r','spearman','spearman_rho','r_squared','r2','slope','intercept'):
                payload[key]=None
                payload['statistics'][key]=None
            payload['statistics']['ready']=False
        elif payload.get('slope') is None or payload.get('intercept') is None:
            payload['ready']=False
            payload['statistics']['ready']=False
            payload['insufficient_reason']='有效配对未达到最少样本数。' if len(payload['points']) < min_n else 'X 字段没有有效变化，无法计算线性拟合。'
        if any(not p['semantic_context'].get('functional') for p in payload['points']):
            payload['warnings'].append('calculation_context_unknown_exploratory_only')
        return payload if self._matrix_mode else self.decorate(payload)

    def correlation_matrix(self, library_name=None, *, min_n=3,fields=None):
        definitions={f['key']:f for f in self.field_registry() if f['type']=='number'}
        available=list(definitions)
        if fields is None:
            counts=Counter(k for row in self._rebuild_fields().values() for k,r in row.items() if r['exploratory_eligible']) if self.source=='rebuild' else Counter()
            # Generic adsorption/charge fields have unresolved species. They
            # remain available as observations, but are not independent matrix
            # axes beside the resolved species-specific variables.
            matrix_fields = [f for f in available if f not in {'adsorption_energy', 'bader_charge_transfer'}] if self.source=='rebuild' else available
            fields=sorted(matrix_fields,key=lambda k:(-counts[k],k)) if self.source=='rebuild' else matrix_fields
        if not set(fields)<=set(available):
            raise ValueError('unknown matrix field')
        cells=[]
        self._matrix_mode=True
        for index,left in enumerate(fields):
            for right in fields[index:]:
                result=self.correlation(library_name=library_name,x_field=left,y_field=right,min_n=min_n)
                valid_groups=[g for g in result['comparability_groups'] if g['statistics'] and g['statistics']['pearson'] is not None]
                selected=max(valid_groups,key=lambda g:g['n']) if valid_groups else None
                stats=selected['statistics'] if selected else result
                n=selected['n'] if selected else result['n_catalysts']
                cell={'x_property':left,'y_property':right,'descriptor':left,'target_property':right,'n':n,'total_paired_n':result['n_catalysts'],'pearson_r':stats.get('pearson_r',stats.get('pearson')),'spearman_rho':stats.get('spearman_rho',stats.get('spearman')),'slope':stats.get('slope'),'intercept':stats.get('intercept'),'status':'grouped' if selected and len(result['comparability_groups'])>1 else 'sufficient' if selected else 'insufficient_paired_data','group_id':selected['group_id'] if selected else None,'group_label':selected['label'] if selected else None,'group_statistics':result['comparability_groups'],'source':self.source}
                cells.append(cell)
                if left!=right:
                    cells.append({**cell,'x_property':right,'y_property':left,'descriptor':right,'target_property':left})
        self._matrix_mode=False
        return {'schema_version':'stored_exploratory_matrix_v1','variables':[definitions[f] for f in fields],'cells':cells,'available_field_count':len(available),'omitted_fields':[f for f in available if f not in fields],'selection_policy':'Curated physical fields excluding unresolved generic adsorption/charge axes; one canonical variable per observation; same-source alias comparisons excluded; pair split rows within a paper and material identity. Display separately identified comparable groups; raw observations retain original extracted fields.','min_n':min_n}

    def observation_dataset(self, *, limit=None, offset=0, dataset_profile=None):
        self._load_rows()
        rows=[r for r in self.records if r['exploratory_eligible']]
        before=len(rows)
        profiles={'dac_lis_ml':'dual_atom','bimetallic_lis_ml':'dual_atom','dac_lis':'dual_atom','sac_lis_ml':'single_atom'}
        if dataset_profile:
            if dataset_profile not in profiles:
                raise ValueError('unsupported dataset_profile')
            wanted=profiles[dataset_profile]
            allowed={'dual_atom','DAC','dac'} if wanted=='dual_atom' else {'single_atom','SAC','sac'}
            rows=[r for r in rows if r['catalyst_type'] in allowed and str(r['reaction'] or '').lower() in {'li-s','lis','li_s','lithium_sulfur'}]
        included=len(rows)
        rows=rows[offset:offset+limit] if limit is not None else rows[offset:]
        return self.decorate({'schema_version':'stored_dft_exploratory_v1','records':rows,'metadata':{'total_records':len(self.records),'exploratory_numeric_records':before,'approved_count':None,'assessment_status':'not_evaluated','safety_gate':'not_evaluated_exploratory_only','dataset_profile':dataset_profile,'profile_included_count_before_limit':included,'profile_excluded_count':before-included,'exported_count_after_limit':len(rows),'offset':offset,'limit':limit}})

    @staticmethod
    def _sort_compare(rows, sort):
        if sort == 'value':
            rows.sort(key=lambda r:(r['value'] is None,r['value'] or 0,r['record_id']))
        else:
            rows.sort(key=lambda r:(str(r['catalyst_name'] or '').casefold(),r['paper_id'],r['property_type'] or '',r['adsorbate'] or '',r['record_id']))

    def compare(self, *, offset=0,limit=25,sort='catalyst_group'):
        if self.source == 'rebuild' and self.session.get_bind().dialect.name == 'postgresql':
            key = (str(self.session.get_bind().url), _content_revision(self.session),
                   self.library_name, json.dumps(self.filters, sort_keys=True, default=str),
                   self.include_estimated, self.include_unlabelled, sort)
            with _PROJECTION_LOCK:
                ordered_ids = _COMPARE_RESULTS.get(key)
                if ordered_ids is not None:
                    _COMPARE_RESULTS.move_to_end(key)
            if ordered_ids is None:
                with _projection_build(('compare', key)):
                    with _PROJECTION_LOCK:
                        ordered_ids = _COMPARE_RESULTS.get(key)
                    if ordered_ids is None:
                        index = self._read_rows(index_only=True)
                        self._sort_compare(index, sort)
                        ordered_ids = tuple(r['record_id'] for r in index)
                        if key[1] == _content_revision(self.session):
                            with _PROJECTION_LOCK:
                                _COMPARE_RESULTS[key] = ordered_ids
                                while len(_COMPARE_RESULTS) > 8 or sum(len(v) * 80 for v in _COMPARE_RESULTS.values()) > _COMPARE_CACHE_BYTES:
                                    _COMPARE_RESULTS.popitem(last=False)
            page_ids = ordered_ids[offset:offset+limit]
            if not page_ids:
                return self._compare_response([], len(ordered_ids), offset, limit)
            # Exact original projection, including all sources/legacy fields,
            # but only for the selected page. Reads never change stored data.
            self._read_rows(value_ids=set(page_ids))
            by_id = {r['record_id']:r for r in self.records}
            items = [by_id[rid] for rid in page_ids if rid in by_id]
            return self._compare_response(items, len(ordered_ids), offset, limit)
        self._load_rows()
        rows = list(self.records)
        self._sort_compare(rows, sort)
        return self._compare_response(rows[offset:offset+limit], len(rows), offset, limit)

    def _compare_response(self, items, total, offset, limit):
        return {'source':self.source,'assessment_status':'not_evaluated','items':items,'total':total,'has_more':offset+limit<total,'offset':offset,'limit':limit,'stats':{'count':total},'selection_policy':POLICY}

    def csv(self, *, limit=None,offset=0,dataset_profile=None):
        payload=self.observation_dataset(limit=limit,offset=offset,dataset_profile=dataset_profile)
        columns=['source_family','assessment_status','record_id','paper_id','paper_code','doi','catalyst_name','property_type','adsorbate','raw_value','raw_unit','estimated','unit_unlabelled','evidence_text','source_section','source_figure']
        stream=io.StringIO(newline='')
        writer=csv.DictWriter(stream,fieldnames=columns,lineterminator='\r\n'); writer.writeheader()
        for row in payload['records']:
            # Preserve raw precision; guard spreadsheet formula interpretation.
            writer.writerow({k: ("'"+str(row.get(k)) if str(row.get(k) or '').startswith(('=','+','@')) else row.get(k)) for k in columns})
        return '\ufeff'+stream.getvalue(),payload
