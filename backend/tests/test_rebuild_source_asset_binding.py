"""Asset-only legacy repair against isolated PostgreSQL, never production."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildVisualAsset
from app.db.session import get_db_session
from app.schemas.rebuild import RebuildSourceAssetBindingRequest, RebuildSourceLifecycleRequest
from app.services.rebuild_source_correction import bind_source_asset, deactivate_source, source_state
from app.services.rebuild_workflow_service import RebuildWorkflowError, _source_key, serialize_source
from test_rebuild_source_correction import fixture, snapshot


def request(s, ids, **changes):
    return RebuildSourceAssetBindingRequest(**{
        'paper_id': ids.paper, 'expected_revision': source_state(s, ids.source)['source_revision'],
        'request_id': uuid4(), 'target_asset_id': ids.assets[0],
        'reason': 'Bind verified legacy table without inventing coordinates', **changes})


def columns(obj):
    return deepcopy({c.name: getattr(obj, c.name) for c in obj.__table__.columns})


@pytest.mark.parametrize('coords', [(None, None), (0, 1), (None, 2)])
def test_preserves_all_other_columns_and_history_and_replays_current_state(fixture, coords):
    factory, _, ids = fixture
    with factory() as s:
        source = s.get(RebuildValueSource, ids.source)
        source.table_row, source.table_column = coords
        value = s.get(RebuildDataValue, ids.value)
        # Exercise preserved page metadata and unrelated lifecycle/conflict data.
        value.conflict = {**value.conflict,
            'source_page_identities': {str(source.id): {'page_basis': 'derived_pdf',
                'source_sha256': 'a' * 64, 'reading_pdf_sha256': 'b' * 64}},
            'source_lifecycle': [{'source_id': str(uuid4()), 'action': 'deactivate'}]}
        s.commit()
        before_source, before_value = columns(source), columns(value)
        row = s.get(RebuildDataRow, source.row_id)
        before_row = columns(row)
        before_state = source_state(s, source.id)
        req = request(s, ids)
        result = bind_source_asset(s, ids.source, req, actor='trusted-login'); s.commit()
        assert (result['table_row'], result['table_column']) == coords
        assert source.asset_id == ids.assets[0]
        assert result['source_key'] == _source_key(serialize_source(source))
        assert result['source_revision'] != before_state['source_revision']
        assert {k: v for k, v in columns(source).items() if k not in {'asset_id', 'source_key'}} == {
            k: v for k, v in before_source.items() if k not in {'asset_id', 'source_key'}}
        assert {k: v for k, v in columns(value).items() if k not in {'conflict', 'updated_at'}} == {
            k: v for k, v in before_value.items() if k not in {'conflict', 'updated_at'}}
        assert columns(row) == before_row
        assert {k: v for k, v in value.conflict.items() if k != 'source_corrections'} == before_value['conflict']
        history = deepcopy(result['source_corrections'])
        assert len(history) == 1
        audit = history[0]
        assert audit['before'] == {k: before_state[k] for k in audit['before']}
        assert audit['after'] == {k: result[k] for k in audit['after']}
        assert audit['actor'] == 'trusted-login' and audit['reason'] == req.reason
        assert audit['action'] == 'asset_binding'
        stored = snapshot(s)
        assert bind_source_asset(s, ids.source, req, actor='trusted-login') == result
        assert snapshot(s) == stored and not s.dirty
        # A later successful binding must not change the original audit; replay
        # returns the latest source, even though the original revision is stale.
        asset = RebuildVisualAsset(paper_id=ids.paper, file_id=ids.files[0], asset_type='table', asset_key='second')
        s.add(asset); s.commit()
        later = request(s, ids, target_asset_id=asset.id)
        current = bind_source_asset(s, ids.source, later, actor='trusted-login'); s.commit()
        assert current['source_corrections'][:1] == history
        assert bind_source_asset(s, ids.source, req, actor='trusted-login') == current
        assert not s.dirty


@pytest.mark.parametrize('case,error', [
    ('paper', 'source_paper_mismatch'), ('file', 'source_asset_file_mismatch'),
    ('asset_paper', 'source_asset_belongs_to_other_paper'),
    ('asset_type', 'source_asset_binding_requires_table_asset'),
    ('source_kind', 'source_asset_binding_requires_table_source'),
    ('missing_source', 'source_not_found'), ('missing_asset', 'asset_not_found'),
    ('inactive', 'source_inactive'), ('stale', 'source_revision_conflict'),
    ('collision', 'source_key_conflict'), ('legacy_collision', 'source_key_conflict'),
    ('reason', 'source_asset_binding_reason_required'), ('nul', 'source_asset_binding_reason_required'),
    ('actor', 'source_actor_required'),
])
def test_preflight_rejection_has_zero_mutations(fixture, case, error):
    factory, _, ids = fixture
    with factory() as s:
        args = {}
        if case == 'paper': args['paper_id'] = ids.other
        if case == 'file': args['target_asset_id'] = ids.assets[2]
        if case == 'asset_paper': args['target_asset_id'] = ids.assets[1]
        if case == 'missing_asset': args['target_asset_id'] = uuid4()
        if case == 'stale': args['expected_revision'] = '0' * 64
        if case == 'reason': args['reason'] = '   '
        if case == 'nul': args['reason'] = 'bad\x00reason'
        if case == 'asset_type': s.get(RebuildVisualAsset, ids.assets[0]).asset_type = 'figure'
        if case == 'source_kind': s.get(RebuildValueSource, ids.source).source_kind = 'text'
        if case == 'inactive':
            deactivate_source(s, ids.source, RebuildSourceLifecycleRequest(
                paper_id=ids.paper, expected_revision=source_state(s, ids.source)['source_revision'],
                request_id=uuid4(), reason='Retire fixture', action='deactivate'), actor='trusted-login')
        if case in {'collision', 'legacy_collision'}:
            src = s.get(RebuildValueSource, ids.source)
            data = {**serialize_source(src), 'asset_id': str(ids.assets[0])}
            clone = {c.name: getattr(src, c.name) for c in src.__table__.columns if c.name not in {'id', 'created_at'}}
            clone.update(asset_id=ids.assets[0], source_key=_source_key(data) if case == 'collision' else 'legacy-key')
            s.add(RebuildValueSource(**clone))
        s.commit()
        req = request(s, ids, **args)
        before = snapshot(s)
        with pytest.raises(RebuildWorkflowError, match='^' + error + '$'):
            bind_source_asset(s, uuid4() if case == 'missing_source' else ids.source,
                              req, actor='' if case == 'actor' else 'trusted-login')
        assert snapshot(s) == before and not s.dirty
        s.rollback()
        assert snapshot(s) == before


@pytest.mark.parametrize('change', ['reason', 'target_asset_id', 'expected_revision', 'actor', 'source_id'])
def test_request_id_reuse_is_rejected(fixture, change):
    factory, _, ids = fixture
    with factory() as s:
        req = request(s, ids)
        bind_source_asset(s, ids.source, req, actor='trusted-login'); s.commit()
        source_id = ids.source
        updates = {change: {'reason': 'Changed', 'target_asset_id': ids.assets[2], 'expected_revision': '0'*64}.get(change)}
        if change == 'source_id':
            src = s.get(RebuildValueSource, ids.source)
            clone = {c.name: getattr(src, c.name) for c in src.__table__.columns if c.name not in {'id', 'created_at'}}
            clone['source_key'] = 'other'; other = RebuildValueSource(**clone); s.add(other); s.commit()
            source_id = other.id
        altered = req.model_copy(update=updates) if change not in {'actor', 'source_id'} else req
        before = snapshot(s)
        with pytest.raises(RebuildWorkflowError, match='^source_request_id_reused$'):
            bind_source_asset(s, source_id, altered, actor='other' if change == 'actor' else 'trusted-login')
        assert snapshot(s) == before and not s.dirty


@pytest.mark.parametrize('same_request', [False, True])
def test_concurrent_cas_and_replay(fixture, same_request):
    factory, _, ids = fixture
    with factory() as s: req = request(s, ids)
    gate = Barrier(2)
    def run(actor):
        with factory() as s:
            gate.wait()
            try:
                bind_source_asset(s, ids.source, req if same_request else req.model_copy(update={'request_id': uuid4()}),
                                  actor='same-actor' if same_request else actor)
                s.commit(); return 'ok'
            except RebuildWorkflowError as exc:
                s.rollback(); return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, ['first', 'second'])) == (['ok', 'ok'] if same_request else ['ok', 'source_revision_conflict'])
    with factory() as s: assert len(source_state(s, ids.source)['source_corrections']) == 1


def test_http_auth_schema_error_contract_and_discovery(fixture, monkeypatch):
    from app.api import rebuild
    factory, settings, ids = fixture
    app = FastAPI(); app.include_router(rebuild.router, prefix='/api/rebuild')
    def db():
        with factory() as s: yield s
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_settings] = lambda: settings
    with factory() as s: req = request(s, ids).model_dump(mode='json'); stored = snapshot(s)
    path = '/api/rebuild/sources/' + str(ids.source) + '/asset-binding'
    with TestClient(app) as client:
        for headers in ({}, {'Authorization': 'Bearer test-primary-repair-key'}, {'X-Share-Token': 'share-only'}):
            assert client.post(path, json=req, headers=headers).status_code == 401
        client.cookies.set('litai_session', 'tampered.invalid')
        assert client.post(path, json=req).status_code == 401
        client.cookies.clear()
        async def logged(*args, **kwargs): return SimpleNamespace(username='verified-login', csrf='test-token')
        monkeypatch.setattr(rebuild, 'require_session', logged)
        assert client.post(path, json=req).status_code == 403
        client.cookies.set('litai_csrf', 'test-token'); client.headers['X-CSRF-Token'] = 'wrong'
        assert client.post(path, json=req).status_code == 403
        client.headers['X-CSRF-Token'] = 'test-token'
        assert client.post(path, json=req, headers={'Origin': 'https://evil.invalid'}).status_code == 403
        for change in ({'table_row': 0}, {'source': {}}, {'target_asset_id': None}, {'expected_revision': 'bad'}, {'reason': ''}):
            assert client.post(path, json={**req, **change}).status_code == 422
        assert client.post(path, json={**req, 'reason': '  '}).status_code == 400
        assert client.post(path, json={**req, 'target_asset_id': str(ids.assets[2])}).status_code == 400
        assert client.post(path, json={**req, 'target_asset_id': str(uuid4())}).status_code == 404
        assert client.post('/api/rebuild/sources/' + str(uuid4()) + '/asset-binding', json=req).status_code == 404
        assert client.post(path, json={**req, 'expected_revision': '0'*64}).status_code == 409
        with factory() as s: assert snapshot(s) == stored
        response = client.post(path, json=req)
        assert response.status_code == 200, response.text
        assert response.json()['table_row'] is None and response.json()['table_column'] is None
        assert response.json()['source_corrections'][0]['actor'] == 'verified-login'
        assert client.post(path, json=req).json() == response.json()
        assert client.post(path, json={**req, 'reason': 'Changed'}).status_code == 409
        with factory() as s: assert len(source_state(s, ids.source)['source_corrections']) == 1
        package = client.get('/api/rebuild/papers/' + str(ids.paper) + '/ai-work-package')
        assert package.status_code == 200, package.text
        assert package.json()['write_endpoints']['source_asset_binding'].endswith('/asset-binding')
        assert package.json()['source_correction_contract']['asset_binding']['write'].startswith('POST ')
