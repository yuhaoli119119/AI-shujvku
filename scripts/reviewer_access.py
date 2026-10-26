#!/usr/bin/env python3
"""Request scoped review access. Captures secrets privately; never dispatches AI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def workbench_opener(base, cookies, transport='direct'):
    """Keep URL/TLS/session identity; optionally select the known relay TCP path."""
    import http.client
    import ssl
    import urllib.request
    handlers = [urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(cookies)]
    if transport == 'aliyun-easytier':
        if base != 'https://47.94.252.147:49004':
            raise RuntimeError('trusted_relay_requires_known_https_workbench')
        class RelayConnection(http.client.HTTPSConnection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connect = self._create_connection
                def selected(address, *a, **k):
                    if address == ('47.94.252.147', 49004):
                        address = ('10.144.144.11', 49004)
                    return connect(address, *a, **k)
                self._create_connection = selected
        class RelayHandler(urllib.request.HTTPSHandler):
            def https_open(self, request):
                return self.do_open(RelayConnection, request, context=self._context,
                                    check_hostname=self._check_hostname)
        handlers.append(RelayHandler(context=ssl.create_default_context()))
    elif transport != 'direct':
        raise RuntimeError('unknown_workbench_transport')
    return urllib.request.build_opener(*handlers)


def private_write(path, data):
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode() + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def runtime(operation, request):
    source = (ROOT / 'backend/scripts/prepare_reviewer_access.py').read_bytes()
    result = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'backend', 'python', '-', operation,
         json.dumps(request, ensure_ascii=False)], input=source, capture_output=True,
        cwd=ROOT, timeout=180, check=False)
    if result.returncode:
        # Never print request, token, traceback or environment on failure.
        tail = result.stderr.decode(errors='replace').strip().splitlines()
        detail = tail[-1].split(': ', 1)[-1] if tail else 'runtime_failed'
        if not re.fullmatch(r'[A-Za-z0-9_,.:-]{1,300}', detail):
            detail = 'runtime_failed_check_private_stderr'
        raise RuntimeError(detail)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper-id', required=True)
    parser.add_argument('--coordinator-thread', required=True, help='current snapshot registrar, not historical author')
    parser.add_argument('--reviewer-thread', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--coverage-file', type=Path, help='actual checked coverage array; omitted means ZERO checked pages')
    args = parser.parse_args()
    from uuid import UUID
    for value in (args.paper_id, args.coordinator_thread, args.reviewer_thread):
        UUID(value)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,55}', args.request_id):
        parser.error('invalid request-id')
    if args.coordinator_thread == args.reviewer_thread:
        parser.error('independent_reviewer_required')
    owner = json.loads((ROOT / '.env.workbench-login.local').read_text())['username']
    private = ROOT / 'outputs' / 'reviewer-access-private' / args.request_id
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private.is_symlink() or private.resolve() != private.absolute():
        raise RuntimeError('private_path_must_not_be_symlink')
    os.chmod(private.parent, 0o700)
    os.chmod(private, 0o700)
    evidence = ROOT / 'data' / 'reviewer-evidence' / args.request_id
    evidence.mkdir(parents=True, exist_ok=True)
    if evidence.is_symlink() or evidence.resolve() != evidence.absolute():
        raise RuntimeError('evidence_path_must_not_be_symlink')
    request = {'paper_id': args.paper_id, 'owner': owner,
               'coordinator_thread_id': args.coordinator_thread,
               'reviewer_thread_id': args.reviewer_thread,
               'request_id': args.request_id,
               'coverage': json.loads(args.coverage_file.read_text()) if args.coverage_file else None,
               'evidence_root': '/data/' + str(evidence.relative_to(ROOT / 'data'))}
    request_path = private / 'request.json'
    if request_path.exists():
        old = json.loads(request_path.read_text())
        if old != request:
            raise RuntimeError('request_id_reused_with_different_identity_or_coverage')
    else:
        private_write(request_path, request)
    # Verify the directory from inside the actual backend before registering a
    # freeze or issuing a token. Host-side existence is insufficient in Docker.
    runtime('check_evidence', request)
    # Durable backup precedes any formal freeze/grant write.
    backup = private / 'before.json'
    if not backup.exists():
        private_write(backup, runtime('snapshot', request))
    before = json.loads(backup.read_text())
    result = runtime('prepare', {**request, 'expected_revision': before['snapshot']['revision']})
    credential = private / 'authorization.json'
    if credential.exists():
        if json.loads(credential.read_text())['authorization'] != result['authorization']:
            raise RuntimeError('credential_changed_use_new_request_id')
    else:
        private_write(credential, result)
    print(json.dumps({'status': 'ready', 'paper_id': args.paper_id,
                      'freeze_id': result['freeze']['job_id'],
                      'revision': result['freeze']['revision'],
                      'authorization_file': str(credential),
                      'evidence_directory': str(evidence),
                      'expires_at': result['verified']['expires_at'],
                      'scientific_review_submitted': False}, ensure_ascii=False))


def submit():
    """Save one reviewer-authored payload; no content generation or dispatch."""
    import http.cookiejar
    import urllib.error
    import urllib.request
    import sys
    parser = argparse.ArgumentParser(description='Submit one existing reviewer payload with scoped credentials')
    parser.add_argument('--authorization-file', required=True, type=Path)
    parser.add_argument('--body', required=True, type=Path)
    parser.add_argument('--kind', choices=['review', 'progress'], default='review')
    parser.add_argument('--transport', choices=['direct', 'aliyun-easytier'], default='direct',
                        help='Known relay TCP path only; logical HTTPS URL and TLS validation stay unchanged')
    parser.add_argument('--readback-only', action='store_true', help='Recover an interrupted submission without repeating its POST')
    parser.add_argument('--recover-not-saved-evidence', type=Path,
                        help='Explicit one-time recovery after maintenance verified original request terminal and not saved')
    args = parser.parse_args(sys.argv[2:])
    credential_path = args.authorization_file.resolve()
    if not credential_path.is_relative_to(ROOT / 'outputs/reviewer-access-private'):
        raise RuntimeError('authorization_file_outside_private_root')
    credential = json.loads(credential_path.read_text())
    body = json.loads(args.body.read_text())
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', str(body.get('request_id', ''))):
        raise RuntimeError('invalid_submit_request_id')
    frozen = credential['freeze']
    request = json.loads((credential_path.parent / 'request.json').read_text())
    if (body.get('reviewer_task_id') != request['reviewer_thread_id'] or
            body.get('expected_revision') != frozen['revision']):
        raise RuntimeError('review_payload_identity_or_revision_mismatch')
    output = credential_path.parent / ('submit-' + body['request_id'] + '.json')
    state_path = credential_path.parent / ('submit-' + body['request_id'] + '-state.json')
    identity = {'paper_id': request['paper_id'], 'freeze_id': frozen['job_id'],
                'kind': args.kind, 'body': body}
    state = {'identity': identity, 'stage': 'prepared', 'saved': False}
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state['identity'] != identity:
            raise RuntimeError('submit_request_id_reused_with_different_payload')
    def persist():
        # Replacement is atomic and private; a crash never destroys the last receipt.
        import tempfile
        fd, name = tempfile.mkstemp(prefix='.submit-', dir=credential_path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    recovery = None
    if args.recover_not_saved_evidence:
        import hashlib
        from datetime import datetime, timezone
        if args.readback_only or state.get('recovery_attempted') or state.get('saved') is not None:
            raise RuntimeError('explicit_recovery_not_applicable_or_already_consumed')
        evidence_path = args.recover_not_saved_evidence.resolve()
        if not evidence_path.is_relative_to(ROOT / 'outputs'):
            raise RuntimeError('recovery_evidence_outside_project_outputs')
        evidence_raw = evidence_path.read_bytes()
        evidence = json.loads(evidence_raw)
        identity_sha = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest()
        required = {'version': 'review_submit_terminal_evidence_v1', 'identity_sha256': identity_sha,
                    'paper_id': request['paper_id'], 'freeze_id': frozen['job_id'],
                    'request_id': body['request_id'], 'kind': args.kind, 'outcome': 'not_saved',
                    'original_process_terminal': True, 'original_turn_terminal': True,
                    'audit_count': 0, 'active_transactions': 0, 'prepared_transactions': 0}
        # Exact JSON type matching keeps false/bools from standing in for counts.
        if any(type(evidence.get(k)) is not type(v) or evidence.get(k) != v for k, v in required.items()):
            raise RuntimeError('recovery_terminal_evidence_identity_or_outcome_invalid')
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(evidence['checked_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('recovery_terminal_evidence_time_invalid') from None
        if not 0 <= age <= 900:
            raise RuntimeError('recovery_terminal_evidence_expired')
        recovery = {'evidence_file': str(evidence_path),
                    'evidence_sha256': hashlib.sha256(evidence_raw).hexdigest(),
                    'identity_sha256': identity_sha}
    if (state.get('saved') is None or state.get('stage') in {'posting', 'unknown'}) and not args.readback_only and not recovery:
        raise RuntimeError('submit_result_unknown_use_readback_only_no_repost')
    persist()
    config = json.loads((ROOT / '.env.workbench-login.local').read_text())
    base = config['base_url'].rstrip('/')
    cookies = http.cookiejar.CookieJar()
    # This server-side client talks to the explicitly configured workbench.
    # Do not inherit model/download proxy settings: those can interrupt a write
    # after it has been sent and leave its outcome unknown.
    opener = workbench_opener(base, cookies, args.transport)
    secrets = [credential['authorization'], config['password']]
    def redact(value):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    def call(path, value=None, extra=None):
        headers = {'Content-Type': 'application/json', 'Origin': base, **(extra or {})}
        raw = json.dumps(value, ensure_ascii=False).encode() if value is not None else None
        req = urllib.request.Request(base + path, data=raw, headers=headers)
        try:
            with opener.open(req, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            state['error'] = {'stage': state['stage'], 'http_status': error.code,
                              'path': path, 'method': req.get_method(),
                              'body': redact(error.read(65536).decode(errors='replace'))}
            # A failed POST is never automatically retried, including HTTP 5xx.
            if state['stage'] == 'posting':
                state['stage'] = 'unknown' if error.code >= 500 else 'rejected'
                state['saved'] = None if error.code >= 500 else False
            persist()
            raise RuntimeError('review_submit_http_' + str(error.code)) from None
        except (OSError, TimeoutError, ValueError) as error:
            state['error'] = {'stage': state['stage'], 'path': path, 'method': req.get_method(),
                              'code': 'transport_or_response_error',
                              'exception_type': type(error).__name__,
                              'reason_type': type(getattr(error, 'reason', error)).__name__,
                              'message': redact(str(error))[:1000]}
            if state['stage'] == 'posting':
                state.update(stage='unknown', saved=None)
            persist()
            raise RuntimeError('review_submit_transport_error_check_private_state') from None
    login = call('/api/auth/login', {'username': config['username'], 'password': config['password']})
    secrets.append(login['csrf_token'])
    endpoint = f"/api/rebuild/papers/{request['paper_id']}/acceptance/{frozen['job_id']}"
    if not state.get('saved') and not args.readback_only:
        if state['stage'] == 'rejected':
            raise RuntimeError('submit_previously_rejected_check_private_state_use_new_request_id')
        if recovery:
            # Consume before attempting the write. A second interruption never
            # turns this explicit maintenance decision into a retry loop.
            # Exclusive creation also fences two processes that both loaded
            # the same old unknown state before either persisted its decision.
            consumed = state_path.with_name(state_path.stem + '-recovery-consumed.json')
            try:
                private_write(consumed, recovery)
            except FileExistsError:
                raise RuntimeError('explicit_recovery_already_consumed') from None
            state.update(recovery_attempted=True, recovery=recovery)
        state.update(stage='posting', saved=None)
        persist()
        result = call(endpoint + ('/reviews' if args.kind == 'review' else '/review-progress'), body,
                      {'X-CSRF-Token': login['csrf_token'],
                       'X-Reviewer-Authorization': credential['authorization']})
        state.update(stage='saved', saved=True, response=result)
        persist()  # POST success must survive any subsequent readback failure.
    state['stage'] = 'reading_back'
    persist()
    try:
        current = call(f"/api/rebuild/papers/{request['paper_id']}/acceptance")
    except RuntimeError:
        # Preserve the successful POST and give an explicit recovery instruction.
        if state.get('saved'):
            print(json.dumps({'saved': True, 'readback_verified': False,
                              'state_file': str(state_path), 'recovery': 'repeat with --readback-only'}))
            return
        raise
    state['readback'] = current
    observed = current.get('acceptance') or {}
    matched = (args.kind == 'review'
            and observed.get('job_id') == frozen['job_id']
            and observed.get('revision') == frozen['revision']
            and all((observed.get('review') or {}).get(k) == v for k, v in body.items()))
    if not state.get('saved') and matched:
        state.update(saved=True, response=observed)
    state['readback_verified'] = matched
    state['stage'] = 'readback_complete' if state.get('saved') else 'unknown'
    persist()
    if not state.get('saved'):
        print(json.dumps({'saved': None, 'readback_verified': False, 'state_file': str(state_path),
                          'recovery': 'result remains unknown; do not repeat POST'}))
        return
    result = state['response']
    receipt = {'response': result, 'readback': current}
    if output.exists():
        output = output.with_name(output.stem + '-readback.json')
    if not output.exists():
        private_write(output, receipt)
    print(json.dumps({'saved': True, 'readback_verified': matched, 'receipt_file': str(output),
                      'current_acceptance_status': current['acceptance']['status']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        import sys
        submit() if len(sys.argv) > 1 and sys.argv[1] == 'submit' else main()
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else
                         'runtime_timeout_result_unknown_retry_same_request_only')
