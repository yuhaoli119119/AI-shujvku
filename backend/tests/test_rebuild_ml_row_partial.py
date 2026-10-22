"""Offline synthetic graph: packet UUID scope is not scientific authorization."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest
from sqlalchemy.engine import Engine

from app.services.rebuild_ml_dataset import (ACCEPTANCE_VERSION, PARTIAL_ACCEPTANCE_VERSION,
    MODELS, VERSION, build_dataset, digest, paper_revision)

pytestmark = pytest.mark.no_test_database
SCOPE = json.loads((Path(__file__).parent / 'fixtures/b046_row_partial_scope.json').read_text())


def uid(label):
    return str(uuid5(NAMESPACE_URL, 'offline-b046/' + label))


def fixture_graph():
    pid = SCOPE['paper_id']; fid = uid('file'); aid = uid('asset')
    shapes = {x['value_id']: x for x in SCOPE['cells']}
    graph = {k: [] for k in MODELS}; graph['file_lifecycle'] = []
    graph['papers'] = [dict(id=pid, doi='10.0000/offline-fixture', paper_code='B046')]
    graph['files'] = [dict(id=fid, paper_id=pid, sha256='a'*64)]
    graph['assets'] = [dict(id=aid, paper_id=pid, file_id=fid, asset_type='figure', page_numbers=[1])]
    graph['rows'] = [dict(id=rid, paper_id=pid, data_type='experimental', material='synthetic',
        active_site='Fe', condition={'functional': 'fixture'}) for rid in SCOPE['row_ids']]
    for n, vid in enumerate(SCOPE['value_ids'] + [uid('unauthorized-value'), uid('null-value')]):
        shape = shapes.get(vid, dict(row_id=SCOPE['row_ids'][0], field_name='energy', unit='eV'))
        rid = shape['row_id']; sid = uid('source-' + vid)
        missing = vid == uid('null-value')
        value = dict(id=vid, paper_id=pid, row_id=rid, field_name=shape['field_name'],
            raw_value=None if missing else '-2.40', numeric_value=None if missing else -2.4,
            unit=None if missing else shape['unit'], precision_digits=None if missing else 2,
            value_type='missing' if missing else 'explicit', is_estimated=False,
            missing_reason='not_reported' if missing else None, preferred_source_id=sid,
            conflict={'source_page_identities': {sid: dict(page_basis='source_pdf',
                source_sha256='a'*64, reading_pdf_sha256='a'*64)}})
        graph['values'].append(value)
        graph['sources'].append(dict(id=sid, paper_id=pid, row_id=rid, value_id=vid,
            file_id=fid, asset_id=aid, source_kind='figure', page_number=1,
            table_row=None, table_column=None, quote='Synthetic fixture evidence'))
    snap = dict(schema_version=VERSION, graph=graph, scope_paper_ids=[pid],
        transaction={'isolation': 'repeatable read', 'read_only': 'on'},
        query_bundle_sha256=digest('offline-no-query'), snapshot_sha256=digest(graph))
    return snap


def authorization():
    return dict(authorized=True, reviewer='synthetic-fixture-reviewer',
        receipt='offline-fixture-only', evidence_sha256='c'*64)


def manifest_for(snap):
    m = dict(schema_version=PARTIAL_ACCEPTANCE_VERSION, scope_paper_ids=snap['scope_paper_ids'],
        policy=dict(data_type='experimental', required_identity=['material', 'active_site', 'condition.functional'],
            fields={k: dict(kind='numeric', role='feature', allowed_units=['eV'])
                    for k in ('energy', 'unreported_energy')}))
    for cell in SCOPE['cells']:
        m['policy']['fields'][cell['field_name']] = dict(kind='numeric', role='feature',
            allowed_units=[cell['unit'] or '1'])
    for kind in MODELS:
        m[kind] = {x['id']: dict(status='eligible', sha256=digest(x),
            reviewer_authorization=authorization()) for x in snap['graph'][kind]}
    for pid, d in m['papers'].items():
        d.update(status='partial', graph_revision=paper_revision(snap, pid),
            review_receipt='offline-review', review_evidence_sha256='c'*64)
    for rid, d in m['rows'].items():
        row_values = [v for v in snap['graph']['values'] if v['row_id'] == rid]
        missing = {field: 'not_reported' for field in m['policy']['fields'] if not any(
            v['field_name'] == field and v['numeric_value'] is not None for v in row_values)
            or any(v['field_name'] == field and v['numeric_value'] is None for v in row_values)}
        d.update(status='row_partial', missing_cells=missing,
            eligible_value_mask={v['id']: v['id'] in SCOPE['value_ids']
                for v in snap['graph']['values'] if v['row_id'] == rid})
    for vid, d in m['values'].items():
        d.update(conflict='clear', leakage='clear')
        if vid in SCOPE['value_ids'] and next(v for v in snap['graph']['values'] if v['id'] == vid)['unit'] is None:
            d['unit_overlay'] = dict(stored_unit=None, analysis_unit='1', reason='synthetic explicit dimensionless review')
    # Candidate accepted notes alone must never authorize this value.
    m['values'][uid('unauthorized-value')].pop('reviewer_authorization')
    return m


def build(s, m): return build_dataset(s, m, code_revision='offline-fixture-code', seed=31)
def lines(p, name='formal_numeric.jsonl'): return [json.loads(x) for x in p[name].splitlines()]


@pytest.fixture
def offline(monkeypatch):
    def blocked(*args, **kwargs): raise AssertionError('Database connections forbidden')
    monkeypatch.setattr(Engine, 'connect', blocked)
    snap = fixture_graph()
    return snap, manifest_for(snap)


def test_exact_31_uuid_scope_nulls_determinism_and_no_promotion(offline):
    snap, m = offline; before = deepcopy((snap, m))
    out = build(snap, m)
    assert out == build(snap, m) and (snap, m) == before
    formal = lines(out)
    assert len(formal) == 31 and {x['id'] for x in formal} == set(SCOPE['value_ids'])
    assert all(str(UUID(x['id'])) == x['id'] for x in formal)
    assert {x['row_id'] for x in formal} == set(SCOPE['row_ids'])
    assert all(x['row_partial'] and x['value_eligible_mask'] for x in formal)
    assert all(x['paper_acceptance']['status'] == 'partial' for x in formal)
    null = next(x for x in lines(out, 'exploratory.jsonl') if x['id'] == uid('null-value'))
    assert null['numeric_value'] is null['raw_value'] is null['unit'] is None
    assert null['missing_mask']['reason'] == 'not_reported' and not null['formal_eligible']
    assert all(x['missing_cells']['unreported_energy'] == 'not_reported' for x in formal)
    overlay = [x for x in formal if x['unit'] is None]
    assert len(overlay) == 10 and all(x['analysis_unit'] == '1' for x in overlay)
    stats = json.loads(out['build_manifest.json'])['statistics']
    assert stats['formal_partial_rows'] == 11 and stats['reviewed_eligible_rows_with_formal_values'] == 0


@pytest.mark.parametrize('kind', ['papers', 'rows', 'values', 'files', 'sources', 'assets'])
@pytest.mark.parametrize('case', ['absent', 'false', 'empty_reviewer', 'empty_receipt', 'bad_hash', 'stale'])
def test_every_admitted_object_requires_authorization_and_freshness(offline, kind, case):
    snap, m = offline
    key = next(iter(m[kind])); d = m[kind][key]
    if case == 'absent': d.pop('reviewer_authorization')
    elif case == 'false': d['reviewer_authorization']['authorized'] = False
    elif case == 'empty_reviewer': d['reviewer_authorization']['reviewer'] = ' '
    elif case == 'empty_receipt': d['reviewer_authorization']['receipt'] = ''
    elif case == 'bad_hash': d['reviewer_authorization']['evidence_sha256'] = 'bad'
    else: d['sha256'] = '0'*64
    result = lines(build(snap, m))
    assert len(result) < 31
    assert {x['id'] for x in result} <= set(SCOPE['value_ids'])


@pytest.mark.parametrize('case', ['mask_false', 'value_rejected', 'value_partial', 'row_partial_legacy',
    'identity', 'conflict', 'leakage', 'page', 'source', 'paper_mismatch', 'null_numeric', 'leak_field'])
def test_machine_and_scope_gates_cannot_be_bypassed(offline, case):
    snap, m = offline; v = snap['graph']['values'][0]; vid = v['id']; rid = v['row_id']
    if case == 'mask_false': m['rows'][rid]['eligible_value_mask'][vid] = False
    elif case == 'value_rejected': m['values'][vid]['status'] = 'rejected'
    elif case == 'value_partial': m['values'][vid]['status'] = 'partial'
    elif case == 'row_partial_legacy': m['rows'][rid]['status'] = 'partial'
    else:
        if case == 'identity': next(r for r in snap['graph']['rows'] if r['id'] == rid)['active_site'] = None
        if case == 'conflict': v['conflict']['status'] = 'value_conflict'
        if case == 'leakage': m['values'][vid]['leakage'] = 'unresolved'
        if case == 'page': v['conflict']['source_page_identities'] = {}
        if case == 'source': snap['graph']['sources'][0]['source_kind'] = 'unsupported'
        if case == 'paper_mismatch': snap['graph']['sources'][0]['paper_id'] = uid('other-paper')
        if case == 'null_numeric': v['numeric_value'] = None
        if case == 'leak_field':
            v['field_name'] = 'predicted_energy'
            m['policy']['fields']['predicted_energy'] = m['policy']['fields']['energy']
        snap['snapshot_sha256'] = digest(snap['graph'])
        fresh = manifest_for(snap)
        for k in MODELS:
            for key, d in m[k].items(): d['sha256'] = fresh[k][key]['sha256']
        m['papers'][SCOPE['paper_id']]['graph_revision'] = paper_revision(snap, SCOPE['paper_id'])
        for row in m['rows'].values():
            row['missing_cells'].update({f: 'not_reported' for f in m['policy']['fields']})
    assert vid not in {x['id'] for x in lines(build(snap, m))}


@pytest.mark.parametrize('case', ['missing_mask', 'unknown_uuid', 'foreign_row_value', 'bool_int',
    'missing_reasons', 'empty_reason', 'unscoped_reason', 'whole_row_lie'])
def test_malformed_row_contract_fails_closed(offline, case):
    snap, m = offline; row = m['rows'][SCOPE['row_ids'][0]]
    if case == 'missing_mask': row.pop('eligible_value_mask')
    if case == 'unknown_uuid': row['eligible_value_mask'][uid('unknown')] = True
    if case == 'foreign_row_value': row['eligible_value_mask'][SCOPE['value_ids'][1]] = True
    if case == 'bool_int': row['eligible_value_mask'][next(iter(row['eligible_value_mask']))] = 1
    if case == 'missing_reasons': row['missing_cells'] = {}
    if case == 'empty_reason': row['missing_cells']['unreported_energy'] = ' '
    if case == 'unscoped_reason': row['missing_cells']['other'] = 'absent'
    if case == 'whole_row_lie': row['status'] = 'eligible'
    with pytest.raises(ValueError): build(snap, m)


def test_no_mask_only_promotion_of_unreviewed_candidate(offline):
    snap, m = offline; vid = uid('unauthorized-value')
    v = next(x for x in snap['graph']['values'] if x['id'] == vid)
    m['rows'][v['row_id']]['eligible_value_mask'][vid] = True
    assert vid not in {x['id'] for x in lines(build(snap, m))}


def test_v1_retains_legacy_dispositions_and_rejects_v2_extensions(offline):
    snap, m = offline; m['schema_version'] = ACCEPTANCE_VERSION
    with pytest.raises(ValueError): build(snap, m)
    for kind in MODELS:
        for d in m[kind].values():
            d.pop('reviewer_authorization', None)
            d.pop('unit_overlay', None)
            d.pop('eligible_value_mask', None); d.pop('missing_cells', None)
            if kind == 'rows': d['status'] = 'eligible'
    out = build(snap, m)
    assert len(lines(out)) == 22  # Ten null-unit cells remain excluded under v1. Legacy explicit eligible dispositions retain semantics.
    assert all('row_partial' not in x for x in lines(out))
    for d in m['rows'].values(): d['status'] = 'partial'
    assert not lines(build(snap, m))


@pytest.mark.parametrize('case', ['absent', 'bad_target', 'empty_reason', 'bad_source_unit', 'missing_auth'])
def test_null_unit_overlay_is_explicit_value_scoped_and_never_fills_numbers(offline, case):
    snap, m = offline
    vid = next(x['id'] for x in snap['graph']['values'] if x['id'] in SCOPE['value_ids'] and x['unit'] is None)
    d = m['values'][vid]
    if case == 'absent': d.pop('unit_overlay')
    if case == 'bad_target': d['unit_overlay']['analysis_unit'] = 'eV'
    if case == 'empty_reason': d['unit_overlay']['reason'] = ''
    if case == 'bad_source_unit': d['unit_overlay']['stored_unit'] = 'eV'
    if case == 'missing_auth': d.pop('reviewer_authorization')
    if case in {'bad_target', 'empty_reason', 'bad_source_unit'}:
        with pytest.raises(ValueError, match='unit overlay'): build(snap, m)
    else:
        out = build(snap, m)
        assert vid not in {x['id'] for x in lines(out)}
        excluded = next(x for x in lines(out, 'exploratory.jsonl') if x['id'] == vid)
        assert excluded['unit'] is None and excluded['analysis_unit'] is None
        assert len(lines(out)) == 30


def test_eligible_value_cannot_promote_an_unreviewed_row(offline):
    snap, m = offline
    row = deepcopy(snap['graph']['rows'][0]); row['id'] = uid('unreviewed-row')
    snap['graph']['rows'].append(row)
    v = deepcopy(snap['graph']['values'][0]); v.update(id=uid('unreviewed-row-value'), row_id=row['id'])
    src = deepcopy(snap['graph']['sources'][0]); src.update(id=uid('unreviewed-row-source'), row_id=row['id'], value_id=v['id'])
    v['preferred_source_id'] = src['id']
    v['conflict']['source_page_identities'] = {src['id']: dict(page_basis='source_pdf',
        source_sha256='a'*64, reading_pdf_sha256='a'*64)}
    snap['graph']['values'].append(v); snap['graph']['sources'].append(src)
    snap['snapshot_sha256'] = digest(snap['graph']); m = manifest_for(snap)
    del m['rows'][row['id']]
    assert {x['id'] for x in lines(build(snap, m))} == set(SCOPE['value_ids'])


def test_snapshot_and_graph_revision_still_fail_closed(offline):
    snap, m = offline
    m['papers'][SCOPE['paper_id']]['graph_revision'] = '0'*64
    assert not lines(build(snap, m))
    snap['snapshot_sha256'] = '0'*64
    with pytest.raises(ValueError, match='snapshot'): build(snap, m)


def test_v2_export_schema_and_checksums_are_explicit(offline):
    import hashlib
    snap, m = offline; out = build(snap, m)
    schema = json.loads(out['schema.json']); info = json.loads(out['build_manifest.json'])
    assert schema['schema_version'] == info['schema_version'] == 'rebuild_ml_v2'
    assert json.loads(out['snapshot.json'])['schema_version'] == 'rebuild_ml_v1'
    assert 'formal_rows' not in info['statistics']
    assert info['statistics']['formal_rows_with_eligible_values'] == 11
    for line in out['SHA256SUMS'].decode().splitlines():
        sha, name = line.split('  ')
        assert hashlib.sha256(out[name]).hexdigest() == sha
    for name, sha in info['files'].items(): assert hashlib.sha256(out[name]).hexdigest() == sha
