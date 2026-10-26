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


def test_relay_tcp_selection_keeps_logical_tls_host_and_required_verification(monkeypatch):
    import http.client
    import http.cookiejar
    import ssl
    path = Path(__file__).parents[2] / 'scripts/reviewer_access.py'
    spec = importlib.util.spec_from_file_location('review_relay_client', path)
    host = importlib.util.module_from_spec(spec); spec.loader.exec_module(host)
    calls = []
    class Socket:
        def setsockopt(self, *args):
            pass
    monkeypatch.setattr(http.client.socket, 'create_connection',
                        lambda address, *args, **kwargs: calls.append(address) or Socket())
    opener = host.workbench_opener('https://47.94.252.147:49004', http.cookiejar.CookieJar(), 'aliyun-easytier')
    handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
    assert handler._context.verify_mode == ssl.CERT_REQUIRED
    assert handler._context.check_hostname is True
    tls_hosts = []
    monkeypatch.setattr(handler._context, 'wrap_socket',
                        lambda sock, server_hostname: tls_hosts.append(server_hostname) or sock)
    connections = []
    def do_open(factory, request, **kwargs):
        connection = factory(request.host, **kwargs)
        connections.append(connection)
        connection.connect()
        return connection
    monkeypatch.setattr(handler, 'do_open', do_open)
    req = urllib.request.Request('https://47.94.252.147:49004/api/health',
                                 headers={'Origin': 'https://47.94.252.147:49004'})
    handler.https_open(req)
    assert calls == [('10.144.144.11', 49004)]
    assert tls_hosts == ['47.94.252.147']
    assert connections[0].host == '47.94.252.147'
    assert req.full_url == 'https://47.94.252.147:49004/api/health'
    assert req.get_header('Origin') == 'https://47.94.252.147:49004'
    handler.https_open(urllib.request.Request('https://other.invalid/api/health'))
    assert calls[-1] == ('other.invalid', 443)
    assert tls_hosts[-1] == 'other.invalid'


@pytest.mark.parametrize('base', ['http://47.94.252.147:49004', 'https://other.invalid',
                                 'https://47.94.252.147:443'])
def test_relay_option_rejects_unknown_or_plaintext_entry(client_case, base):
    import http.cookiejar
    host = client_case[0]
    with pytest.raises(RuntimeError, match='known_https_workbench'):
        host.workbench_opener(base, http.cookiejar.CookieJar(), 'aliyun-easytier')


@pytest.fixture
def artifact_case(client_case, monkeypatch):
    host, private, body, argv, token = client_case
    root = host.ROOT / 'data/reviewer-evidence/current'
    root.mkdir(parents=True)
    artifact = root / 'coverage.json'
    artifact.write_bytes(b'{"observations":[]}')
    request = {'evidence_root': '/data/reviewer-evidence/current'}
    payload = {'artifact_path': 'coverage.json', 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    seen = []
    def runtime(operation, value):
        assert operation == 'check_artifact'
        seen.append(value)
        return {'evidence_root': value['evidence_root'], 'artifact_sha256': value['artifact_sha256'],
                'container_readable': True}
    monkeypatch.setattr(host, 'runtime', runtime)
    return host, root, artifact, request, payload, seen


@pytest.mark.parametrize('absolute', [False, True])
def test_progress_preflight_checks_host_then_container_same_bytes(artifact_case, absolute):
    host, root, artifact, request, body, seen = artifact_case
    if absolute:
        body['artifact_path'] = request['evidence_root'] + '/coverage.json'
    result = host.progress_artifact_preflight(request, body)
    assert result['artifact_sha256'] == body['artifact_sha256']
    assert seen[0]['artifact_path'] == 'coverage.json'
    assert set(seen[0]) == {'evidence_root', 'artifact_path', 'artifact_sha256'}


@pytest.mark.parametrize('path', ['/data/reviewer-evidence/old/coverage.json', '../old.json'])
def test_progress_preflight_rejects_previous_grant_and_traversal_before_runtime(artifact_case, path):
    host, root, artifact, request, body, seen = artifact_case
    body['artifact_path'] = path
    with pytest.raises(RuntimeError, match='outside_current_grant_root'):
        host.progress_artifact_preflight(request, body)
    assert seen == []


@pytest.mark.parametrize('problem,code', [('missing', 'not_found'), ('hash', 'hash_mismatch'),
                                        ('oversize', 'too_large'), ('symlink', 'outside_current_grant_root')])
def test_progress_preflight_bad_file_stops_before_runtime(artifact_case, problem, code):
    host, root, artifact, request, body, seen = artifact_case
    if problem == 'missing': artifact.unlink()
    elif problem == 'hash': body['artifact_sha256'] = '0' * 64
    elif problem == 'oversize': artifact.write_bytes(b'x' * 2_000_001)
    else:
        outside = root.parent / 'outside.json'; outside.write_bytes(artifact.read_bytes())
        artifact.unlink(); artifact.symlink_to(outside)
    with pytest.raises(RuntimeError, match=code):
        host.progress_artifact_preflight(request, body)
    assert seen == []


def test_progress_preflight_runtime_visibility_mismatch_is_not_success(artifact_case, monkeypatch):
    host, root, artifact, request, body, seen = artifact_case
    monkeypatch.setattr(host, 'runtime', lambda *args: {'artifact_sha256': 'wrong'})
    with pytest.raises(RuntimeError, match='runtime_contract_mismatch'):
        host.progress_artifact_preflight(request, body)


def test_bad_progress_input_records_not_sent_before_login(client_case, monkeypatch):
    host, private, body, argv, token = client_case
    req_path = private / 'request.json'; request = json.loads(req_path.read_text())
    request['evidence_root'] = '/data/reviewer-evidence/current'; req_path.write_text(json.dumps(request))
    body.update(artifact_path='/data/reviewer-evidence/old/coverage.json', artifact_sha256='0' * 64)
    Path(argv[-1]).write_text(json.dumps(body))
    monkeypatch.setattr(sys, 'argv', argv + ['--kind', 'progress'])
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: pytest.fail('login must not start'))
    with pytest.raises(RuntimeError, match='no POST sent'):
        host.submit()
    state = json.loads((private / 'submit-save-1-state.json').read_text())
    assert state['saved'] is False
    assert state['error']['stage'] == 'artifact_preflight'
    assert state['error']['business_post_sent'] is False


@pytest.mark.parametrize('error,code', [(PermissionError('private/path'), 'file_unreadable'),
    (TimeoutError('private/path'), 'file_unreadable')])
def test_preflight_filesystem_error_stays_not_sent(client_case, monkeypatch, error, code):
    host, private, body, argv, token = client_case
    def fail(*args): raise error
    monkeypatch.setattr(host, 'progress_artifact_preflight', fail)
    monkeypatch.setattr(sys, 'argv', argv + ['--kind', 'progress'])
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: pytest.fail('no login'))
    with pytest.raises(RuntimeError, match=code): host.submit()
    state = json.loads((private / 'submit-save-1-state.json').read_text())
    assert state['saved'] is False and state['error']['business_post_sent'] is False
    assert 'private/path' not in json.dumps(state['error'])


@pytest.mark.parametrize('runtime_root', ['/outputs', '/outputs/legacy'])
def test_progress_preflight_preserves_legacy_outputs_root_and_subdirectory(artifact_case, runtime_root):
    host, root, artifact, request, body, seen = artifact_case
    host_root = host.ROOT / runtime_root.lstrip('/')
    host_root.mkdir(parents=True, exist_ok=True)
    (host_root / 'coverage.json').write_bytes(artifact.read_bytes())
    request['evidence_root'] = runtime_root
    result = host.progress_artifact_preflight(request, body)
    assert result['evidence_root'] == runtime_root
    assert result['artifact_sha256'] == body['artifact_sha256']
