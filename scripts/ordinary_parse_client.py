#!/usr/bin/env python3
"""Durable workbench client for ordinary extraction; no per-paper import scripts.

Examples: --job JOB --paper UUID get /api/rebuild/papers/UUID
          --job JOB --paper UUID write /api/rebuild/rows/import batch.json
          --job JOB --paper UUID checkpoint FILE_ID read completed
Credentials remain local. Journals contain request bytes and receipts, never cookies.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import time
from uuid import uuid4
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'outputs/stable-parsing-command-20261023/implementation/jobs'


class WorkbenchClient:
    def __init__(self, base, *, opener=None, sleep=time.sleep):
        self.base = base.rstrip('/')
        self.sleep = sleep
        self.csrf = None
        if opener is None:
            self.cookies = http.cookiejar.CookieJar()
            # IP entry uses the project's existing IP certificate. Trust settings
            # stay local to this explicitly configured private server connection.
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        else:
            self.opener = opener

    def request(self, path, body=None, *, method=None, idempotent=False):
        url = urllib.parse.urlsplit(path)
        if url.scheme or url.netloc or not path.startswith('/api/'):
            raise ValueError('only_relative_workbench_api_paths_allowed')
        headers = {'Content-Type': 'application/json', 'Origin': self.base}
        if self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        for attempt in range(4):
            try:
                req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method or ('POST' if body else 'GET'))
                with self.opener.open(req, timeout=60) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors='replace')
                retryable_lock = exc.code == 409 and 'paper_lock_busy_retry_same_request' in raw
                if (exc.code not in (429, 502, 503, 504) and not retryable_lock) or attempt == 3 or (body is not None and not idempotent):
                    raise RuntimeError(f'HTTP {exc.code}: {raw[:1500]}') from exc
                try:
                    delay = min(60, max(1, float(exc.headers.get('Retry-After', 5 * 2 ** attempt))))
                except (ValueError, TypeError):
                    delay = 5 * 2 ** attempt
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == 3 or (body is not None and not idempotent):
                    raise
                delay = 5 * 2 ** attempt
            self.sleep(delay)
        raise RuntimeError('retry_budget_exhausted')

    def login(self, username, password):
        result = self.request('/api/auth/login', json.dumps({'username': username, 'password': password}).encode())
        self.csrf = result['csrf_token']


def publish_json(path, value):
    """Publish only fully durable JSON; interrupted temporary files remain recoverable."""
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    with temporary.open('x') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def durable_write(client, directory, path, payload):
    """Write the object to the live database through the formal API."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    return client.request(path, body, method='PUT' if '/checkpoints' in path else 'POST', idempotent=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', default='direct')
    parser.add_argument('--paper', required=True)
    parser.add_argument('--credentials', type=Path, default=ROOT / '.env.workbench-login.local')
    commands = parser.add_subparsers(dest='command', required=True)
    get = commands.add_parser('get'); get.add_argument('path')
    write = commands.add_parser('write'); write.add_argument('path'); write.add_argument('json_file', type=Path)
    cp = commands.add_parser('checkpoint'); cp.add_argument('file_id'); cp.add_argument('stage', choices=['read', 'assets', 'rows']); cp.add_argument('status', choices=['running', 'completed', 'partial', 'failed']); cp.add_argument('--reason')
    args = parser.parse_args()
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in args.job):
        parser.error('invalid job identity')
    credentials = json.loads(args.credentials.read_text())
    base = credentials.get('base_url') or credentials.get('url') or credentials.get('origin') or 'https://47.94.252.147:49004'
    client = WorkbenchClient(base)
    client.login(credentials.get('username') or credentials.get('account'), credentials['password'])
    directory = (ROOT / 'outputs/stable-parsing-command-20261023/detail-reading/jobs' if args.job.startswith('aiext-guide-') else OUTPUT) / args.job
    if args.command == 'get':
        result = client.request(args.path)
    elif args.command == 'write':
        payload = json.loads(args.json_file.read_text())
        seed = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        payload.setdefault('request_id', hashlib.sha256((args.job + args.path + seed).encode()).hexdigest())
        if not args.path.endswith('/reading-assistance'):
            payload.setdefault('task_id', args.job)
        if args.path == '/api/rebuild/rows/import':
            payload.setdefault('paper_id', args.paper)
        allowed = {f'/api/rebuild/papers/{args.paper}/assets/import', '/api/rebuild/rows/import', f'/api/rebuild/papers/{args.paper}/reading-assistance'}
        if args.path not in allowed or ('paper_id' in payload and payload['paper_id'] != args.paper):
            parser.error('write outside selected paper or supported idempotent imports')
        result = durable_write(client, directory, args.path, payload)
    else:
        path = f'/api/rebuild/papers/{args.paper}/ai-extract/jobs/{args.job}'
        job = client.request(path)
        # Same semantic checkpoint has one persisted revision/body across restarts.
        identity = hashlib.sha256(json.dumps([args.file_id, args.stage, args.status, args.reason]).encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        intent = directory / (identity + '.checkpoint.json')
        if intent.exists():
            payload = json.loads(intent.read_text())
        else:
            payload = {'request_id': identity, 'file_id': args.file_id, 'stage': args.stage,
                       'status': args.status, 'reason': args.reason, 'expected_revision': job['checkpoint_revision']}
            publish_json(intent, payload)
            payload = json.loads(intent.read_text())
        result = durable_write(client, directory, path + '/checkpoints', payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
