"""Synthetic isolated PostgreSQL only; no production data or sessions."""
import copy
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.db.models import Base, Paper, RebuildDataRow, RebuildDataValue, RebuildPaperFile, RebuildValueSource, AuditLog
from app.main import app
from test_rebuild_mutation_audit import audit_env, row_body

ENDPOINT = '/api/rebuild/rows/import'


def snapshot(env):
    # Compare complete rows of EVERY mapped table, not only scientific counts.
    with env['engine'].connect() as connection:
        return {table.name: sorted(connection.execute(text(
            f'SELECT row_to_json(t)::text FROM "{table.name}" AS t')).scalars())
            for table in Base.metadata.sorted_tables}


def files(env):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in env['settings'].storage_root.rglob('*') if path.is_file()}


def preflight(env, body):
    response = env['client'].post(ENDPOINT + '/preflight', json=body)
    assert response.status_code == 200, response.text
    output = response.json()
    assert output['persisted'] is False and output['simulation'] is True
    assert 'audit' not in output and 'recovery' not in output
    encoded = json.dumps(output)
    assert '"row_id"' not in encoded and '"value_id"' not in encoded
    assert '"audit_event_ids"' not in encoded
    return output


def signature(result):
    return (result['outcome'], result['complete'], [(r['outcome'], r.get('identity_status'),
        [(v['outcome'], v.get('field'), v.get('error')) for v in r.get('values', [])]) for r in result['results']])


def imported(env, body):
    response = env['client'].post(ENDPOINT, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_mixed_batch_rollback_auth_repeated_and_id_not_reserved(audit_env):
    env = audit_env; body = row_body(env)
    good = body['rows'][0]
    good['values'][0].update(raw_value='−7.2 × 10^−7', numeric_value=-7.2e-7)
    good['values'].append({'field_name': 'bad', 'raw_value': '−1.20', 'numeric_value': 1.2,
                           'sources': good['values'][0]['sources']})
    body['rows'] += [copy.deepcopy(good), {'material': 'invalid'}]
    body['rows'][1]['values'][0].update(raw_value='2.00', numeric_value=2)
    before = snapshot(env); disk = files(env)
    first = preflight(env, body); second = preflight(env, body)
    assert signature(first) == signature(second)
    assert snapshot(env) == before and files(env) == disk
    actual = imported(env, body)
    assert signature(first) == signature(actual)
    assert actual['summary']['rows'] == {'submitted': 3, 'persisted': 2, 'not_persisted': 1, 'empty_persisted': 0, 'pending': 0}
    assert actual['summary']['values']['persisted'] == 2
    assert actual['summary']['values']['not_persisted'] == 2
    assert actual['summary']['values']['conflict'] == 1
    assert actual['outcome'] == 'partial_success'
    assert actual['errors'][0]['row_id'] == actual['results'][0]['row_id']
    after = snapshot(env)
    assert imported(env, body)['audit']['replayed']
    assert snapshot(env) == after
    changed = copy.deepcopy(body); changed['rows'][0]['material'] = 'changed'
    assert env['client'].post(ENDPOINT, json=changed).status_code == 409
    # Preflight never creates/reserves/replays audit requests, even reused IDs.
    preflight(env, changed)
    assert snapshot(env) == after


@pytest.mark.parametrize('mutation,error', [
    ('range', 'source_page_out_of_range'), ('other_paper', 'source_file_belongs_to_other_paper'),
    ('empty', 'raw_value_required'), ('projection', 'numeric_value_disagrees_with_raw'),
    ('unknown_file', 'file_not_found'),
])
def test_source_empty_and_numeric_parity(audit_env, mutation, error):
    env = audit_env; body = row_body(env); cell = body['rows'][0]['values'][0]
    if mutation == 'range': cell['sources'][0]['page_number'] = 3
    if mutation == 'unknown_file': cell['sources'][0]['file_id'] = str(uuid4())
    if mutation == 'other_paper':
        with env['factory']() as session:
            paper = Paper(title='Other synthetic', library_name='默认文献库', paper_code='OTHER', pdf_path='fixture.pdf')
            session.add(paper); session.flush()
            file = RebuildPaperFile(paper_id=paper.id, role='si', storage_path='fixture.pdf', original_filename='fixture.pdf', sha256='0' * 64, file_size=0)
            session.add(file); session.commit(); cell['sources'][0]['file_id'] = str(file.id)
    if mutation == 'empty': cell.update(raw_value='', numeric_value=None)
    if mutation == 'projection': cell.update(raw_value='−1.20', numeric_value=1.2)
    before = snapshot(env); simulated = preflight(env, body)
    assert snapshot(env) == before
    result = imported(env, body)
    assert signature(result) == signature(simulated)
    assert error in result['results'][0]['values'][0]['error']
    assert result['summary']['rows']['empty_persisted'] == 1
    assert result['summary']['values']['not_persisted'] == 1


def test_same_batch_duplicate_pending_and_all_rejected_summary(audit_env):
    env = audit_env; body = row_body(env)
    body['rows'].append(copy.deepcopy(body['rows'][0]))
    pending = copy.deepcopy(body['rows'][0]); pending['row_key'] = 'pending'; pending['material'] = 'pending'
    pending['values'].append({'field_name': 'adsorbate', 'raw_value': '*S', 'numeric_value': 1,
                               'sources': pending['values'][0]['sources']})
    body['rows'].append(pending)
    sim = preflight(env, body); result = imported(env, body)
    assert signature(sim) == signature(result)
    assert result['rows_duplicate'] == 1
    assert result['summary']['values']['duplicate'] == 1
    assert result['summary']['values']['pending'] == 1
    assert result['summary']['rows']['pending'] == 1
    assert result['recovery']['items'][0]['status'] == 'blocked'
    assert 'identity_pending' in result['recovery']['items'][0]['reason']
    rejected = {'paper_id': body['paper_id'], 'rows': [{'bad': 'shape'}, None]}
    sim = preflight(env, rejected); result = imported(env, rejected)
    assert signature(sim) == signature(result) and result['outcome'] == 'all_rejected'
    assert result['summary']['values']['submitted'] == 0
    assert result['summary']['rows']['not_persisted'] == 2


def test_recovery_keeps_dimensions_only_failed_values_and_exact_audit(audit_env):
    env = audit_env; body = row_body(env); row = body['rows'][0]
    row['values'] += [dict(field_name='adsorbate', raw_value='*S', sources=row['values'][0]['sources']),
                      dict(field_name='broken', raw_value='−2.40', numeric_value=2.4, sources=row['values'][0]['sources'])]
    row['properties'] = {'identity_context': 'synthetic-sample', 'kept': 'original'}
    result = imported(env, body)
    item = result['recovery']['items'][0]
    assert item['status'] == 'needs_explicit_correction'
    candidate = item['row_template']
    assert candidate['row_key'] == result['results'][0]['row_key']
    assert candidate['condition'] == {'adsorbate': '*S', 'identity_context': 'synthetic-sample'}
    assert len(candidate['values']) == 1 and candidate['values'][0] == row['values'][2]
    assert 'properties' not in candidate
    with env['factory']() as session:
        old_value = session.scalar(select(RebuildDataValue).where(RebuildDataValue.field_name == 'adsorption_energy_ev'))
        old_id = old_value.id; old_raw = old_value.raw_value
        old_sources = set(session.scalars(select(RebuildValueSource.id)))
    candidate['values'][0]['numeric_value'] = -2.4
    fixed = {'paper_id': body['paper_id'], 'request_id': 'explicit-repair-new-id', 'rows': [candidate]}
    sim = preflight(env, fixed); result2 = imported(env, fixed)
    assert signature(sim) == signature(result2) and result2['outcome'] == 'all_success'
    assert result2['results'][0]['row_id'] == result['results'][0]['row_id']
    assert result2['audit']['event_ids']
    with env['factory']() as session:
        assert len(list(session.scalars(select(RebuildDataRow)))) == 1
        assert len(list(session.scalars(select(RebuildDataValue)))) == 3
        assert len(list(session.scalars(select(RebuildValueSource)))) == 3
        assert session.get(RebuildDataValue, old_id).raw_value == old_raw
        assert old_sources <= set(session.scalars(select(RebuildValueSource.id)))
        row_db = session.scalar(select(RebuildDataRow))
        assert row_db.properties['kept'] == 'original'
        receipts = list(session.scalars(select(AuditLog).where(AuditLog.action == 'rebuild_mutation_receipt')))
        assert len(receipts) == 2
    after = snapshot(env); assert imported(env, fixed)['audit']['replayed']; assert snapshot(env) == after


def test_later_batch_write_blocks_recovery_and_state_change_after_preflight(audit_env):
    env = audit_env; body = row_body(env); original = copy.deepcopy(body['rows'][0])
    body['rows'][0]['values'][0]['numeric_value'] = 10
    body['rows'].append(original)
    sim = preflight(env, body); result = imported(env, body)
    assert signature(sim) == signature(result)
    assert result['recovery']['items'][0]['status'] == 'blocked'
    assert 'now_exists' in result['recovery']['items'][0]['reason']
    new = copy.deepcopy(row_body(env)); new['request_id'] = 'after-preflight'
    new['rows'][0]['values'][0].update(raw_value='2.0', numeric_value=2)
    # Current state changes between preview and write; the actual conflict is returned.
    clean = row_body(env); clean['rows'][0]['row_key'] = 'new-key'; clean['rows'][0]['material'] = 'new'
    assert preflight(env, clean)['complete']
    intervening = copy.deepcopy(clean); intervening['request_id'] = 'intervening'
    intervening['rows'][0]['values'][0].update(raw_value='3.0', numeric_value=3)
    assert imported(env, intervening)['complete']
    assert not imported(env, clean)['complete']
    assert imported(env, new)['recovery']['items'][0]['status'] == 'blocked'


def test_exception_rolls_back_all_tables_files_and_next_session_works(audit_env, monkeypatch):
    from app.api import rebuild
    env = audit_env; body = row_body(env); before = snapshot(env); disk = files(env)
    real = rebuild.import_data_row
    def fail_after_write(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('synthetic-after-write')
    monkeypatch.setattr(rebuild, 'import_data_row', fail_after_write)
    assert env['client'].post(ENDPOINT + '/preflight', json=body).status_code == 500
    assert snapshot(env) == before and files(env) == disk
    monkeypatch.setattr(rebuild, 'import_data_row', real)
    assert preflight(env, body)['complete']
    assert imported(env, body)['complete']


def test_preflight_unauthorized_csrf_origin_and_invalid_envelope(audit_env):
    env = audit_env; body = row_body(env); before = snapshot(env)
    with TestClient(app) as client:
        assert client.post(ENDPOINT + '/preflight', json=body).status_code == 401
    csrf = env['client'].headers.pop('X-CSRF-Token')
    assert env['client'].post(ENDPOINT + '/preflight', json=body).status_code == 403
    env['client'].headers['X-CSRF-Token'] = csrf
    assert env['client'].post(ENDPOINT + '/preflight', json=body, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert env['client'].post(ENDPOINT + '/preflight', json={'paper_id': body['paper_id'], 'rows': []}).status_code == 422
    assert snapshot(env) == before


def test_representative_preflight_overhead(audit_env):
    env = audit_env; body = row_body(env); base = body['rows'][0]
    body['rows'] = [{**copy.deepcopy(base), 'material': f'synthetic-{i}', 'row_key': f'measure-{i}'} for i in range(20)]
    start = perf_counter(); sim = preflight(env, body); preview_seconds = perf_counter() - start
    start = perf_counter(); actual = imported(env, body); import_seconds = perf_counter() - start
    assert signature(sim) == signature(actual)
    print('PERFORMANCE', json.dumps({'synthetic_rows': 20, 'preflight_seconds': preview_seconds,
                                    'audited_import_seconds': import_seconds, 'ratio': preview_seconds / import_seconds}))


def test_dependency_session_is_not_flushed_or_rolled_back(audit_env):
    import asyncio
    from app.api.rebuild import preflight_rebuild_rows
    from app.schemas.rebuild import RebuildRowsImportRequest
    env = audit_env; body = row_body(env); before = snapshot(env)
    with env['factory']() as dependency:
        pending = Paper(title='Unflushed fixture only', pdf_path='')
        dependency.add(pending)
        result = asyncio.run(preflight_rebuild_rows(RebuildRowsImportRequest.model_validate(body),
                                                    request=type("Request", (), {"headers": {}})(), session=dependency, auth=None))
        assert result['complete']
        assert pending in dependency.new and pending.id is None
        assert not dependency.in_transaction() or dependency.is_active
        assert snapshot(env) == before
        dependency.rollback()


def test_bad_field_identity_never_breaks_batch_summary(audit_env):
    env = audit_env; body = row_body(env)
    body['rows'][0]['values'].append({'field_name': {'malformed': True}, 'raw_value': '2'})
    sim = preflight(env, body); actual = imported(env, body)
    assert signature(sim) == signature(actual)
    assert actual['rows_partial'] == 1
    assert actual['recovery']['items'][0]['status'] == 'blocked'


def test_summary_empty_row_counts_actual_state_not_just_submission(audit_env):
    env = audit_env; body = row_body(env); imported(env, body)
    bad = copy.deepcopy(body); bad['request_id'] = 'bad-only-new-id'
    bad['rows'][0]['values'] = [{'field_name': 'new-bad', 'raw_value': '−2.0', 'numeric_value': 2.0,
                               'sources': body['rows'][0]['values'][0]['sources']}]
    result = imported(env, bad)
    assert result['summary']['rows']['empty_persisted'] == 0
    assert result['summary']['values']['persisted'] == 0
    assert result['summary']['values']['not_persisted'] == 1


def test_property_conflict_is_preserved_and_blocks_recovery(audit_env):
    env = audit_env; body = row_body(env); body['rows'][0]['properties'] = {'verified_label': 'original'}
    assert imported(env, body)['complete']
    changed = copy.deepcopy(body); changed['request_id'] = 'property-conflict'
    changed['rows'][0]['properties']['verified_label'] = 'candidate'
    changed['rows'][0]['values'].append({'field_name': 'bad', 'raw_value': '2', 'numeric_value': -2,
                                       'sources': body['rows'][0]['values'][0]['sources']})
    before = snapshot(env); sim = preflight(env, changed); assert snapshot(env) == before
    result = imported(env, changed)
    assert signature(sim) == signature(result)
    assert result['summary']['properties']['conflict'] == 1
    assert result['recovery']['items'][0]['status'] == 'blocked'
    with env['factory']() as session:
        row = session.scalar(select(RebuildDataRow))
        assert row.properties['verified_label'] == 'original'
        assert row.properties['_rebuild_import']['property_conflicts']['verified_label'] == ['original', 'candidate']


def test_preflight_validates_request_header_metadata_without_receipt(audit_env):
    env = audit_env; body = row_body(env); before = snapshot(env)
    response = env['client'].post(ENDPOINT + '/preflight', json=body, headers={'X-Request-ID': 'different'})
    assert response.status_code == 400 and response.json()['detail'] == 'mutation_request_id_mismatch'
    assert snapshot(env) == before
