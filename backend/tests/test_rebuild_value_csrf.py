"""Real session/Origin/double-submit checks without global middleware as a safety net.

All science rows live in the isolated PostgreSQL test schema.
"""
import copy
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select

from app.api import rebuild
from app.db.models import AuditLog, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.db.session import get_db_session
from app.schemas.rebuild import RebuildDataRowInput
from app.services.rebuild_mutation_audit import safe_json
from app.services.rebuild_workflow_service import import_data_row, list_rows, RebuildWorkflowError
from test_rebuild_mutation_audit import audit_env


@pytest.fixture
def value_env(audit_env):
    env = audit_env
    with env['factory']() as session:
        import_data_row(session, env['paper'], RebuildDataRowInput(
            material='synthetic Fe-N4', reaction='HER', data_type='dft', active_site_type='single_atom',
            values=[dict(field_name='adsorption_energy_ev', raw_value='-1.230', numeric_value=-1.23,
                         unit='eV', precision_digits=3, sources=[dict(source_kind='table',
                         file_id=str(env['file']), page_number=2, label='Synthetic table', quote='-1.230 eV')])]))
        session.commit()
        cell = list_rows(session, paper_id=env['paper'])['items'][0]['values'][0]
    bare_app = FastAPI()
    bare_app.include_router(rebuild.router, prefix='/api/rebuild')
    def db():
        with env['factory']() as session:
            yield session
    bare_app.dependency_overrides[get_db_session] = db
    with TestClient(bare_app, raise_server_exceptions=False) as client:
        client.cookies.update(env['client'].cookies)
        client.headers.update({'Origin': 'http://testserver',
                               'X-CSRF-Token': env['client'].headers['X-CSRF-Token']})
        body = dict(raw_value='-1.240', numeric_value=-1.24, unit='eV', precision_digits=3,
                    expected_revision=cell['value_revision'], request_id=str(uuid4()),
                    correction_reason='Synthetic test correction', source=dict(source_kind='table',
                    file_id=str(env['file']), page_number=2, label='Synthetic corrected table', quote='-1.240 eV'))
        yield {**env, 'client': client, 'body': body, 'path': '/api/rebuild/values/' + cell['id']}


def snapshot(env):
    with env['factory']() as session:
        return {model.__tablename__: {str(row.id): safe_json({col.key: getattr(row, col.key)
                    for col in inspect(model).column_attrs}) for row in session.scalars(select(model))}
                for model in (RebuildDataRow, RebuildDataValue, RebuildValueSource, AuditLog)}


def watch_writes(env):
    writes = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)
    event.listen(env['engine'], 'before_cursor_execute', record)
    return writes, record


def test_valid_session_origin_csrf_and_zero_write_replay(value_env):
    env = value_env
    response = env['client'].put(env['path'], json=env['body'])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['raw_value'] == '-1.240'
    assert result['conflict']['previous']['raw_value'] == '-1.230'
    assert result['conflict']['correction_requests'][0]['actor'] == 'writer'
    before = snapshot(env)
    writes, handler = watch_writes(env)
    try:
        replay = env['client'].put(env['path'], json=env['body'])
        assert replay.status_code == 200 and replay.json() == result
        assert writes == []
    finally:
        event.remove(env['engine'], 'before_cursor_execute', handler)
    assert snapshot(env) == before
    assert env['client'].get(env['path']).json() == result


@pytest.mark.parametrize('case,status,detail', [
    ('missing_session', 401, 'not_authenticated'),
    ('invalid_session', 401, 'not_authenticated'),
    ('missing_header', 403, 'csrf_token_invalid'),
    ('wrong_header', 403, 'csrf_token_invalid'),
    ('missing_cookie', 403, 'csrf_token_invalid'),
    ('forged_pair', 403, 'csrf_token_invalid'),
    ('bad_origin', 403, 'cross_origin_request_blocked'),
    ('null_origin', 403, 'cross_origin_request_blocked'),
    ('stale', 409, 'value_revision_conflict'),
])
def test_rejection_does_not_write(value_env, case, status, detail):
    env = value_env; client = env['client']; body = copy.deepcopy(env['body'])
    if case == 'missing_session': client.cookies.delete('litai_session')
    elif case == 'invalid_session': client.cookies.set('litai_session', 'invalid')
    elif case == 'missing_header': client.headers.pop('X-CSRF-Token')
    elif case == 'wrong_header': client.headers['X-CSRF-Token'] = 'wrong'
    elif case == 'missing_cookie': client.cookies.delete('litai_csrf')
    elif case == 'forged_pair':
        client.cookies.set('litai_csrf', 'forged'); client.headers['X-CSRF-Token'] = 'forged'
    elif case == 'bad_origin': client.headers['Origin'] = 'https://untrusted.invalid'
    elif case == 'null_origin': client.headers['Origin'] = 'null'
    elif case == 'stale': body['expected_revision'] = '0' * 64
    before = snapshot(env); writes, handler = watch_writes(env)
    try:
        response = client.put(env['path'], json=body)
        assert response.status_code == status, response.text
        assert response.json()['detail'] == detail
        assert writes == []
    finally:
        event.remove(env['engine'], 'before_cursor_execute', handler)
    assert snapshot(env) == before


def test_body_contract_and_reused_id_remain_unchanged(value_env):
    env = value_env; client = env['client']; before = snapshot(env)
    assert client.put(env['path'], json={**env['body'], 'extra': True}).status_code == 422
    assert client.put(env['path'], json={k: v for k, v in env['body'].items() if k != 'expected_revision'}).status_code == 422
    nonfinite = client.put(env['path'], content='{"numeric_value": NaN}', headers={'Content-Type': 'application/json'})
    assert nonfinite.status_code == 422
    assert nonfinite.json()['detail'] == 'identity_body_must_be_finite_json'
    assert snapshot(env) == before
    assert client.put(env['path'], json=env['body']).status_code == 200
    before = snapshot(env)
    response = client.put(env['path'], json={**env['body'], 'notes': 'different body'})
    assert response.status_code == 409 and response.json()['detail'] == 'value_request_id_reused'
    assert snapshot(env) == before


@pytest.mark.parametrize('failure', ['workflow', 'database'])
def test_failed_correction_rolls_back_all_rows_and_request_id(value_env, monkeypatch, failure):
    env = value_env; before = snapshot(env)
    original = rebuild.update_data_value
    if failure == 'workflow':
        def fail_after_flush(*args, **kwargs):
            original(*args, **kwargs)
            raise RebuildWorkflowError('synthetic_failure_after_flush')
        monkeypatch.setattr(rebuild, 'update_data_value', fail_after_flush)
        response = env['client'].put(env['path'], json=env['body'])
        assert response.status_code == 400
        monkeypatch.setattr(rebuild, 'update_data_value', original)
    else:
        def fail_update(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE rebuild_data_values'):
                raise RuntimeError('synthetic database failure after source insertion')
        event.listen(env['engine'], 'before_cursor_execute', fail_update)
        try:
            response = env['client'].put(env['path'], json=env['body'])
            assert response.status_code == 500
        finally:
            event.remove(env['engine'], 'before_cursor_execute', fail_update)
    assert snapshot(env) == before
    # Rollback must not reserve the failed request ID or leave a new source.
    assert env['client'].put(env['path'], json=env['body']).status_code == 200
