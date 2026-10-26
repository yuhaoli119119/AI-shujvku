"""Management-client recovery never replays a result-unknown POST."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.no_test_database


@pytest.fixture
def client_case(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / 'scripts/reviewer_access.py'
    spec = importlib.util.spec_from_file_location('review_client_recovery', path)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    monkeypatch.setattr(host, 'ROOT', tmp_path)
    private = tmp_path / 'outputs/reviewer-access-private/access'
    private.mkdir(parents=True)
    token = 'private-fixture-token'
    (private / 'authorization.json').write_text(json.dumps({'authorization': token,
        'freeze': {'revision': 'rev', 'job_id': 'freeze'}}))
    (private / 'request.json').write_text(json.dumps({'paper_id': 'paper', 'reviewer_thread_id': 'review'}))
    (tmp_path / '.env.workbench-login.local').write_text(json.dumps({'base_url': 'https://fixture.invalid',
        'username': 'fixture', 'password': 'private-password'}))
    body = {'request_id': 'save-1', 'reviewer_task_id': 'review', 'expected_revision': 'rev'}
    payload = tmp_path / 'body.json'
    payload.write_text(json.dumps(body))
    argv = ['reviewer_access.py', 'submit', '--authorization-file', str(private / 'authorization.json'),
            '--body', str(payload)]
    monkeypatch.setattr(sys, 'argv', argv)
    return host, private, body, argv, token


def test_saved_post_survives_get_failure_and_next_invocation_only_reads(client_case, monkeypatch, capsys):
    host, private, body, argv, token = client_case
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            if request.get_method() == 'POST':
                return io.BytesIO(b'{"status":"review_incomplete"}')
            raise urllib.error.URLError('network down')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    host.submit()
    printed = json.loads(capsys.readouterr().out)
    assert printed['saved'] is True and printed['readback_verified'] is False
    state = json.loads((private / 'submit-save-1-state.json').read_text())
    assert state['saved'] is True and state['response']['status'] == 'review_incomplete'
    assert (private / 'submit-save-1-state.json').stat().st_mode & 0o777 == 0o600
    class Recovery:
        def open(self, request, timeout):
            seen.append(request)
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            assert request.get_method() == 'GET'
            return io.BytesIO(json.dumps({'acceptance': {'status': 'review_incomplete', 'job_id': 'freeze', 'revision': 'rev', 'review': body}}).encode())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Recovery())
    monkeypatch.setattr(sys, 'argv', argv + ['--readback-only'])
    host.submit()
    assert json.loads(capsys.readouterr().out)['readback_verified'] is True
    assert len([r for r in seen if r.get_method() == 'POST' and not r.full_url.endswith('/login')]) == 1


def test_unknown_post_cannot_repeat_and_readback_can_reconcile_exact_receipt(client_case, monkeypatch, capsys):
    host, private, body, argv, token = client_case
    class Opener:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            raise TimeoutError('unknown outcome')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(RuntimeError, match='transport_error'):
        host.submit()
    with pytest.raises(RuntimeError, match='unknown_use_readback_only_no_repost'):
        host.submit()
    class Recovery:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            assert request.get_method() == 'GET'
            return io.BytesIO(json.dumps({'acceptance': {'status': 'review_incomplete', 'job_id': 'freeze',
                'revision': 'rev', 'review': body}}).encode())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Recovery())
    monkeypatch.setattr(sys, 'argv', argv + ['--readback-only'])
    host.submit()
    assert json.loads(capsys.readouterr().out)['saved'] is True


def test_http_error_is_private_actionable_and_redacted(client_case, monkeypatch, capsys):
    host, private, body, argv, token = client_case
    class Opener:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            raise urllib.error.HTTPError(request.full_url, 409, 'Conflict', {},
                io.BytesIO(json.dumps({'detail': 'independent_task_authorization_invalid',
                    'token': token, 'password': 'private-password', 'csrf': 'csrf'}).encode()))
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(RuntimeError, match='review_submit_http_409'):
        host.submit()
    raw = (private / 'submit-save-1-state.json').read_text()
    state = json.loads(raw)
    assert state['stage'] == 'rejected' and state['saved'] is False
    assert state['error']['http_status'] == 409
    assert 'independent_task_authorization_invalid' in state['error']['body']
    assert token not in raw and 'private-password' not in raw
    with pytest.raises(RuntimeError, match='previously_rejected'):
        host.submit()
    assert token not in capsys.readouterr().out


def test_changed_payload_rejected_before_login(client_case, monkeypatch):
    host, private, body, argv, token = client_case
    class Opener:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            raise TimeoutError()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(RuntimeError):
        host.submit()
    payload = Path(argv[-1]); payload.write_text(json.dumps({**body, 'message': 'changed'}))
    with pytest.raises(RuntimeError, match='reused_with_different_payload'):
        host.submit()
