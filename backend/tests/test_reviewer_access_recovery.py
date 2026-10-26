"""Management-client recovery never replays a result-unknown POST."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import hashlib
from datetime import datetime, timezone, timedelta

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


def test_workbench_transport_ignores_environment_proxies_and_keeps_private_error(client_case, monkeypatch):
    host, private, body, argv, token = client_case
    monkeypatch.setenv('HTTPS_PROXY', 'http://unrelated-proxy.invalid:9999')
    handlers = []
    class Opener:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            raise urllib.error.URLError(OSError('TLS EOF ' + token + ' private-password csrf'))
    def build(*args):
        handlers.extend(args)
        return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    with pytest.raises(RuntimeError, match='transport_error'):
        host.submit()
    assert isinstance(handlers[0], urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    raw = (private / 'submit-save-1-state.json').read_text()
    state = json.loads(raw)
    assert state['stage'] == 'unknown' and state['saved'] is None
    assert state['error']['exception_type'] == 'URLError'
    assert state['error']['reason_type'] == 'OSError'
    assert 'TLS EOF' in state['error']['message']
    assert token not in raw and 'private-password' not in raw and 'csrf' not in raw


def terminal_evidence(private):
    state_path = private / 'submit-save-1-state.json'
    state = json.loads(state_path.read_text())
    identity = state['identity']
    evidence = {'version': 'review_submit_terminal_evidence_v1',
        'identity_sha256': hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest(),
        'paper_id': identity['paper_id'], 'freeze_id': identity['freeze_id'], 'request_id': 'save-1',
        'kind': identity['kind'], 'outcome': 'not_saved', 'original_process_terminal': True,
        'original_turn_terminal': True, 'audit_count': 0, 'active_transactions': 0,
        'prepared_transactions': 0, 'checked_at': datetime.now(timezone.utc).isoformat()}
    path = private / 'terminal-evidence.json'
    path.write_text(json.dumps(evidence))
    return path, evidence


def make_unknown(host, monkeypatch):
    class Unknown:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            raise TimeoutError('unknown')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Unknown())
    with pytest.raises(RuntimeError, match='transport_error'):
        host.submit()


def test_explicit_terminal_recovery_uses_exact_original_post_once(client_case, monkeypatch, capsys):
    host, private, body, argv, token = client_case
    make_unknown(host, monkeypatch)
    path, evidence = terminal_evidence(private)
    requests = []
    class Recovered:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            requests.append(request)
            state = json.loads((private / 'submit-save-1-state.json').read_text())
            assert state['recovery_attempted'] is True
            if request.get_method() == 'POST':
                assert json.loads(request.data) == body
                return io.BytesIO(b'{"status":"review_incomplete"}')
            return io.BytesIO(json.dumps({'acceptance': {'status': 'review_incomplete', 'job_id': 'freeze',
                'revision': 'rev', 'review': body}}).encode())
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Recovered())
    monkeypatch.setattr(sys, 'argv', argv + ['--recover-not-saved-evidence', str(path)])
    host.submit()
    assert json.loads(capsys.readouterr().out)['saved'] is True
    assert sum(r.get_method() == 'POST' for r in requests) == 1
    with pytest.raises(RuntimeError, match='already_consumed'):
        host.submit()


def test_explicit_recovery_interruption_cannot_loop(client_case, monkeypatch):
    host, private, body, argv, token = client_case
    make_unknown(host, monkeypatch)
    path, evidence = terminal_evidence(private)
    monkeypatch.setattr(sys, 'argv', argv + ['--recover-not-saved-evidence', str(path)])
    with pytest.raises(RuntimeError, match='transport_error'):
        host.submit()
    state = json.loads((private / 'submit-save-1-state.json').read_text())
    assert state['stage'] == 'unknown' and state['recovery_attempted'] is True
    with pytest.raises(RuntimeError, match='already_consumed'):
        host.submit()


def test_two_simultaneous_recoveries_issue_at_most_one_post(client_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    host, private, body, argv, token = client_case
    make_unknown(host, monkeypatch)
    path, evidence = terminal_evidence(private)
    monkeypatch.setattr(sys, 'argv', argv + ['--recover-not-saved-evidence', str(path)])
    both_loaded_original_state = threading.Barrier(2)
    posts = []
    class Concurrent:
        def open(self, request, timeout):
            if request.full_url.endswith('/login'):
                both_loaded_original_state.wait(timeout=5)
                return io.BytesIO(b'{"csrf_token":"csrf"}')
            if request.get_method() == 'POST':
                posts.append(json.loads(request.data))
                raise TimeoutError('second unknown outcome')
            pytest.fail('no readback after unknown POST')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Concurrent())
    def invoke():
        try:
            host.submit()
        except RuntimeError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))
    assert posts == [body]
    assert any('already_consumed' in result for result in results)
    marker = private / 'submit-save-1-state-recovery-consumed.json'
    assert marker.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('field,value', [('identity_sha256', 'wrong'), ('audit_count', 1),
    ('original_turn_terminal', False), ('active_transactions', False),
    ('checked_at', (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())])
def test_unverified_recovery_evidence_rejected_before_network(client_case, monkeypatch, field, value):
    host, private, body, argv, token = client_case
    make_unknown(host, monkeypatch)
    path, evidence = terminal_evidence(private)
    evidence[field] = value
    path.write_text(json.dumps(evidence))
    monkeypatch.setattr(sys, 'argv', argv + ['--recover-not-saved-evidence', str(path)])
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: pytest.fail('network not allowed'))
    with pytest.raises(RuntimeError, match='recovery_terminal_evidence'):
        host.submit()
