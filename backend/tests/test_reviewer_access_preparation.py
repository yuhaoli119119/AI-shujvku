"""Real isolated DB + HTTP; native identities alone are synthetic fixtures."""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import RebuildDataValue, WorkflowJob
from app.services.codex_web_dispatch import ThreadStatus
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.utils import project_paths
from test_rebuild_acceptance import base, freeze_body, review_body
from test_rebuild_mutation_audit import audit_env

spec = importlib.util.spec_from_file_location(
    'review_access_management', Path(__file__).parents[1] / 'scripts/prepare_reviewer_access.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def setup(env, monkeypatch):
    root = env['settings'].storage_root.parent / 'access-fixture'
    evidence = root / 'outputs' / 'evidence'
    evidence.mkdir(parents=True)
    monkeypatch.setattr(project_paths, 'PROJECT_ROOT', root)
    key = root / 'authority.key'
    key.write_bytes(b'private-fixture-key-with-more-than-32bytes')
    key.chmod(0o600)
    monkeypatch.setenv('LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE', str(key))
    get_settings.cache_clear()
    settings = get_settings()
    request = {'paper_id': str(env['paper']), 'owner': 'writer',
               'coordinator_thread_id': 'coordinator-native', 'reviewer_thread_id': 'review-native',
               'request_id': 'prepare-1', 'evidence_root': str(evidence), 'coverage': None}
    with env['factory']() as session:
        request['expected_revision'] = tool.snapshot(session, request)['snapshot']['revision']
    return request, settings


def native_for(request, *, scope=True):
    async def native(tid, **kwargs):
        return ThreadStatus('completed', None, None, None, thread_id=tid, turn_id=tid+'-turn',
                            cwd='/opt/literature-ai', thread_status='idle',
                            input_text=request['paper_id'] if scope else 'unrelated-paper')
    return native


def run(env, request, settings, **kwargs):
    with env['factory']() as session:
        return asyncio.run(tool.prepare(session, request, settings,
                                        native_read=native_for(request, **kwargs)))


def test_preparation_persists_scoped_access_but_claims_no_coverage_or_acceptance(audit_env, monkeypatch):
    env = audit_env
    req, settings = setup(env, monkeypatch)
    first = run(env, req, settings)
    second = run(env, req, settings)
    assert first['authorization'] == second['authorization'] and second['replayed']
    assert first['registrar_is_not_historical_author'] and not first['scientific_review_submitted']
    assert all(x['covered_pages'] == [] for x in first['freeze']['coverage'])
    with env['factory']() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataValue)) == 0
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 2
    assert env['client'].get(base(env)).json()['acceptance']['status'] != 'accepted'


def test_review_after_real_coverage_uses_formal_http_authorization(audit_env, monkeypatch):
    env = audit_env
    req, settings = setup(env, monkeypatch)
    req['coverage'] = freeze_body(env)['coverage']
    result = run(env, req, settings)
    body = review_body(env, result['freeze'])
    body['reviewer_task_id'] = req['reviewer_thread_id']
    endpoint = base(env) + '/' + result['freeze']['job_id'] + '/reviews'
    assert env['client'].post(endpoint, json=body).status_code == 409
    response = env['client'].post(endpoint, json=body,
                                 headers={'X-Reviewer-Authorization': result['authorization']})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'accepted'


@pytest.mark.parametrize('case', ['same_thread', 'wrong_scope', 'changed_revision', 'active_writer'])
def test_invalid_delegation_fails_without_freeze_or_grant(audit_env, monkeypatch, case):
    env = audit_env
    req, settings = setup(env, monkeypatch)
    if case == 'same_thread':
        req['reviewer_thread_id'] = req['coordinator_thread_id']
    if case == 'changed_revision':
        req['expected_revision'] = '0'*64
    if case == 'active_writer':
        with env['factory']() as session:
            session.add(WorkflowJob(job_id='existing-unknown', type='ai_extract_rebuild', status='unknown',
                                    payload={'paper_id': req['paper_id']}))
            session.commit()
    with pytest.raises(RebuildWorkflowError):
        run(env, req, settings, scope=case != 'wrong_scope')
    with env['factory']() as session:
        assert not session.scalars(select(WorkflowJob).where(
            WorkflowJob.type.in_(['rebuild_acceptance', 'rebuild_task_authorization']))).all()


def test_coverage_change_requires_new_request_identity(audit_env, monkeypatch):
    env = audit_env
    req, settings = setup(env, monkeypatch)
    run(env, req, settings)
    req['coverage'] = freeze_body(env)['coverage']
    with pytest.raises(RebuildWorkflowError, match='acceptance_request_id_reused'):
        run(env, req, settings)


def test_other_review_task_cannot_take_over_existing_grant(audit_env, monkeypatch):
    env = audit_env
    req, settings = setup(env, monkeypatch)
    run(env, req, settings)
    req = {**req, 'request_id': 'another-request', 'reviewer_thread_id': 'another-review'}
    with pytest.raises(RebuildWorkflowError, match='other_reviewer_occupied'):
        run(env, req, settings)


@pytest.mark.no_test_database
def test_submit_hides_bearer_and_persists_formal_readback(tmp_path, monkeypatch, capsys):
    import io
    import json
    import sys
    import urllib.request
    host_spec = importlib.util.spec_from_file_location(
        'review_access_client', Path(__file__).parents[2] / 'scripts/reviewer_access.py')
    host = importlib.util.module_from_spec(host_spec)
    host_spec.loader.exec_module(host)
    monkeypatch.setattr(host, 'ROOT', tmp_path)
    private = tmp_path / 'outputs/reviewer-access-private/request-1'
    private.mkdir(parents=True)
    token = 'fixture-bearer-not-to-print'
    (private / 'authorization.json').write_text(json.dumps(
        {'authorization': token, 'freeze': {'revision': 'rev', 'job_id': 'freeze'}}))
    (private / 'request.json').write_text(json.dumps({'paper_id': 'paper', 'reviewer_thread_id': 'review'}))
    (tmp_path / '.env.workbench-login.local').write_text(json.dumps(
        {'base_url': 'https://fixture.invalid', 'username': 'fixture', 'password': 'fixture-only'}))
    body = tmp_path / 'review.json'
    body.write_text(json.dumps({'request_id': 'receipt-1', 'reviewer_task_id': 'review', 'expected_revision': 'rev'}))
    monkeypatch.setattr(sys, 'argv', ['reviewer_access.py', 'submit', '--authorization-file',
                                    str(private / 'authorization.json'), '--body', str(body)])
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"fixture-csrf"}')
            if request.get_method() == 'POST':
                assert request.get_header('X-reviewer-authorization') == token
                assert request.get_header('X-csrf-token') == 'fixture-csrf'
                return io.BytesIO(b'{"status":"review_incomplete"}')
            return io.BytesIO(b'{"acceptance":{"status":"review_incomplete"}}')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    host.submit()
    printed = capsys.readouterr().out
    assert token not in printed and 'fixture-only' not in printed
    receipt = private / 'submit-receipt-1.json'
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text())['readback']['acceptance']['status'] == 'review_incomplete'
    assert len(seen) == 3
