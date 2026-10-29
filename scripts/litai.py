#!/usr/bin/env python3
"""Literature AI owner-session API client. Python 3.9+, standard library only."""
import argparse
import hashlib
import http.cookiejar
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.private'
DATASET_FILE_PATH = re.compile(
    r'^/api/papers/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
    r'/datasets/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
    r'/files/([0-9a-fA-F]{64})$'
)
JSON_REPLAY_KEYS = frozenset({'method', 'path', 'body', 'request_id', 'task_id', 'server_base'})
RAW_UPLOAD_REPLAY_KEYS = frozenset({
    'mode', 'method', 'path', 'query', 'query_parameters', 'body_encoding',
    'source_file', 'source_filename', 'size_bytes', 'sha256', 'mime_type',
    'request_id', 'task_id', 'server_base',
})


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('xb') as f:
        os.chmod(tmp, 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_json(path, value):
    save(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


class ApiError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__('HTTP %s: %s' % (status, str(detail)[:1800]))
        self.status, self.detail = status, detail


class Client:
    def __init__(self):
        cfg = json.loads((PRIVATE / 'workbench.json').read_text())
        self.cfg = cfg
        self.base = cfg['base_url'].rstrip('/')
        if urllib.parse.urlsplit(self.base).scheme != 'https':
            raise ValueError('The credentialed API base must use HTTPS')
        self.jar = http.cookiejar.LWPCookieJar(str(PRIVATE / 'session.cookies'))
        try:
            self.jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError):
            pass
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None
        try:
            self.csrf = json.loads((PRIVATE / 'session.json').read_text())['csrf_token']
        except (OSError, ValueError, KeyError):
            pass

    def persist(self):
        self.jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(PRIVATE / 'session.cookies', 0o600)
        save_json(PRIVATE / 'session.json', {'csrf_token': self.csrf})

    def path(self, path):
        p = urllib.parse.urlsplit(path)
        if p.scheme or p.netloc or not path.startswith('/api/') or any(x in path for x in ['\r', '\n', '\\', '#']):
            raise ValueError('Use a relative /api/ path on the configured workbench')
        return path

    def raw(self, method, path, body=None, headers=None, safe=False):
        self.path(path)
        hdr = {'Origin': self.base, 'Accept': 'application/json', **(headers or {})}
        if self.csrf:
            hdr['X-CSRF-Token'] = self.csrf
        for attempt in range(4 if safe else 1):
            try:
                req = urllib.request.Request(self.base + path, data=body, method=method, headers=hdr)
                with self.opener.open(req, timeout=60) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors='replace')
                if safe and e.code in (429, 502, 503, 504) and attempt < 3:
                    try:
                        delay = min(30, max(1, float(e.headers.get('Retry-After', 2 ** (attempt+1)))))
                    except ValueError:
                        delay = 2 ** (attempt+1)
                    time.sleep(delay)
                    continue
                raise ApiError(e.code, detail) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if not safe or attempt == 3:
                    raise
                time.sleep(2 ** (attempt+1))

    def login(self):
        data = json.dumps({'username': self.cfg['username'], 'password': self.cfg['password'], 'remember': True}).encode()
        status, _, raw = self.raw('POST', '/api/auth/login', data, {'Content-Type': 'application/json'})
        result = json.loads(raw)
        self.csrf = result['csrf_token']
        self.persist()
        return {k: v for k, v in result.items() if k != 'csrf_token'}

    def request(self, method, path, body=None, headers=None, safe=False):
        if not self.csrf:
            self.login()
        try:
            return self.raw(method, path, body, headers, safe)
        except ApiError as e:
            if e.status != 401:
                raise
            self.login()
            return self.raw(method, path, body, headers, safe)

    def get(self, path):
        return json.loads(self.request('GET', path, safe=True)[2])

    def _json_mutation_plan(self, method, path, payload=None, task='385-liyuhao', request_id=None):
        if not isinstance(method, str) or not isinstance(path, str) or not isinstance(task, str):
            raise ValueError('JSON mutation method, path, and task_id must be strings')
        self.path(path)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() if payload is not None else None
        seed = hashlib.sha256(method.encode()+b'\0'+path.encode()+b'\0'+task.encode()+b'\0'+(body or b'')).hexdigest()
        rid = request_id or (payload.get('request_id') if isinstance(payload, dict) else None) or seed
        rid = str(rid)
        if isinstance(payload, dict) and payload.get('request_id') and str(payload['request_id']) != rid:
            raise ValueError('Request ID must match the JSON body')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', rid) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', task):
            raise ValueError('Invalid request_id or task_id')
        directory = ROOT / 'outputs' / 'requests' / seed
        envelope = {'method': method, 'path': path, 'body': body.decode() if body is not None else None,
                    'request_id': rid, 'task_id': task, 'server_base': self.base}
        reqfile = directory / (str(rid)+'.request.json')
        return {
            'body': body, 'payload': payload, 'request_id': rid, 'seed': seed,
            'directory': directory, 'request_file': reqfile, 'envelope': envelope,
        }

    def mutate(self, method, path, payload=None, task='385-liyuhao', request_id=None, allow_partial=False, require_existing=False):
        plan = self._json_mutation_plan(method, path, payload, task, request_id)
        body = plan['body']; rid = plan['request_id']; directory = plan['directory']; reqfile = plan['request_file']
        if reqfile.exists():
            if json.loads(reqfile.read_text()) != plan['envelope']:
                raise ValueError('Persisted request differs; use the exact original request')
        elif require_existing:
            raise ValueError('JSON replay original request is missing')
        else:
            save_json(reqfile, plan['envelope'])
        headers = {'Content-Type': 'application/json', 'X-Request-ID': str(rid), 'X-Task-ID': task}
        if method == 'POST' and path == '/api/rebuild/rows/import':
            preflight = json.loads(self.request('POST', path+'/preflight', body, headers, safe=True)[2])
            save_json(directory / (str(rid)+'.preflight.json'), preflight)
            # Keep valid partial imports possible through explicit --allow-partial.
            if not preflight.get('complete', False) and not allow_partial:
                raise ValueError('Preflight is partial/rejected: inspect %s; correct failed fields or use --allow-partial' % directory)
        try:
            status, hdr, raw = self.request(method, path, body, headers)
        except ApiError as e:
            save_json(directory / (str(rid)+'.error.json'), {'status': e.status, 'detail': e.detail})
            raise
        except Exception:
            save_json(directory / (str(rid)+'.unknown.json'), {'outcome': 'unknown', 'request': str(reqfile), 'instruction': 'Read back; do not generate a fresh identity. Replay only when endpoint contract permits.'})
            raise
        save(directory / (str(rid)+'.response'), raw)
        save_json(directory / (str(rid)+'.receipt.json'), {'status': status, 'request': str(reqfile), 'content_type': hdr.get('content-type')})
        try:
            return json.loads(raw)
        except ValueError:
            return {'status': status, 'response_file': str(directory / (str(rid)+'.response'))}

    def _validate_json_replay_record(self, request_file):
        candidate = Path(request_file).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        requests_root = (ROOT / 'outputs' / 'requests').resolve()
        try:
            relative = candidate.relative_to(requests_root)
        except ValueError:
            raise ValueError('JSON replay request must be inside the canonical outputs/requests directory') from None
        if len(relative.parts) != 2 or not re.fullmatch(r'[0-9a-f]{64}', relative.parts[0]):
            raise ValueError('JSON replay request path is not canonical outputs/requests/<seed>/<request_id>.request.json')
        if not candidate.is_file():
            raise ValueError('JSON replay original request is missing')
        try:
            envelope = json.loads(candidate.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError('JSON replay original request is unreadable: %s' % exc) from None
        if not isinstance(envelope, dict) or set(envelope) != JSON_REPLAY_KEYS:
            raise ValueError('Replay request matches neither the complete raw-upload nor complete JSON schema')
        if envelope.get('method') not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            raise ValueError('JSON replay method is not canonical')
        if not isinstance(envelope.get('body'), (str, type(None))):
            raise ValueError('JSON replay body must be a canonical string or null')
        if envelope.get('server_base') != self.base:
            raise ValueError('Server mismatch')
        try:
            payload = json.loads(envelope['body']) if envelope['body'] is not None else None
        except ValueError as exc:
            raise ValueError('JSON replay body is invalid JSON: %s' % exc) from None
        try:
            canonical_body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) if payload is not None else None
        except (TypeError, ValueError) as exc:
            raise ValueError('JSON replay body is not canonical JSON: %s' % exc) from None
        if canonical_body != envelope.get('body'):
            raise ValueError('JSON replay body is not canonical JSON')
        plan = self._json_mutation_plan(
            envelope.get('method'), envelope.get('path'), payload,
            envelope.get('task_id'), envelope.get('request_id'),
        )
        expected_path = plan['request_file'].resolve()
        expected_name = plan['request_id'] + '.request.json'
        if candidate != expected_path or relative.parts[1] != expected_name:
            raise ValueError('JSON replay request file, seed directory, or request_id is not the canonical original')
        if envelope != plan['envelope']:
            raise ValueError('JSON replay request differs from its complete canonical request description')

        anchored = 0
        for suffix in ('.receipt.json', '.unknown.json'):
            sidecar = plan['directory'] / (plan['request_id'] + suffix)
            if not sidecar.exists():
                continue
            try:
                evidence = json.loads(sidecar.read_text())
            except (OSError, ValueError) as exc:
                raise ValueError('JSON replay outcome evidence is unreadable: %s' % exc) from None
            if not isinstance(evidence, dict) or evidence.get('request') != str(plan['request_file']):
                raise ValueError('JSON replay outcome evidence is not bound to the canonical request')
            if suffix == '.unknown.json' and evidence.get('outcome') != 'unknown':
                raise ValueError('JSON replay unknown evidence has an invalid outcome')
            anchored += 1
        if not anchored:
            raise ValueError('JSON replay has no canonical receipt or unknown outcome; refusing an unanchored request')
        return plan

    def json_replay(self, request_file, allow_partial=False):
        plan = self._validate_json_replay_record(request_file)
        return self.mutate(
            plan['envelope']['method'], plan['envelope']['path'], plan['payload'],
            plan['envelope']['task_id'], plan['request_id'], allow_partial, True,
        )

    def _raw_upload_plan(self, path, file_path, task='385-liyuhao', request_id=None, mime_type=None, filename=None):
        if not isinstance(path, str) or not isinstance(task, str):
            raise ValueError('Raw upload path and task_id must be strings')
        parsed = urllib.parse.urlsplit(path)
        match = DATASET_FILE_PATH.fullmatch(parsed.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not match:
            raise ValueError('raw-upload requires a dataset file path without query: /api/papers/PAPER_UUID/datasets/DATASET_UUID/files/SHA256')
        expected_sha256 = match.group(3).lower()

        source = Path(file_path).expanduser()
        if not source.is_file():
            raise ValueError('Source file does not exist or is not a regular file: %s' % source)
        body = source.read_bytes()
        size_bytes = len(body)
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError('Source SHA-256 %s does not match path SHA-256 %s' % (actual_sha256, expected_sha256))

        if filename is not None and filename != source.name:
            raise ValueError('Raw upload filename must match the source file basename')
        source_filename = source.name
        if not source_filename or any(ord(c) < 32 or ord(c) == 127 for c in source_filename):
            raise ValueError('Filename must be non-empty and contain no control characters')
        detected_mime_type = mimetypes.guess_type(source_filename)[0] or 'application/octet-stream'
        if mime_type is not None and mime_type != detected_mime_type:
            raise ValueError('Raw upload MIME type must match the source filename')
        mime_type = detected_mime_type
        if any(ord(c) < 32 or ord(c) == 127 for c in mime_type):
            raise ValueError('MIME type contains control characters')

        query_values = {'filename': source_filename, 'size_bytes': size_bytes, 'mime_type': mime_type}
        query = urllib.parse.urlencode(query_values, safe='', quote_via=urllib.parse.quote)
        seed_material = '\0'.join([
            'PUT', path, task, query, actual_sha256, str(size_bytes), str(source.resolve())
        ]).encode()
        seed = hashlib.sha256(seed_material).hexdigest()
        rid = request_id or seed
        if not isinstance(rid, str):
            raise ValueError('Invalid request_id or task_id')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', str(rid)) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', task):
            raise ValueError('Invalid request_id or task_id')

        directory = ROOT / 'outputs' / 'requests' / seed
        reqfile = directory / (str(rid)+'.request.json')
        envelope = {
            'mode': 'raw-upload',
            'method': 'PUT',
            'path': path,
            'query': query,
            'query_parameters': query_values,
            'body_encoding': 'raw-file-bytes',
            'source_file': str(source.resolve()),
            'source_filename': source_filename,
            'size_bytes': size_bytes,
            'sha256': actual_sha256,
            'mime_type': mime_type,
            'request_id': rid,
            'task_id': task,
            'server_base': self.base,
        }
        return {
            'body': body, 'query': query, 'query_parameters': query_values,
            'size_bytes': size_bytes, 'sha256': actual_sha256, 'request_id': rid,
            'seed': seed, 'directory': directory, 'request_file': reqfile, 'envelope': envelope,
        }

    def _validate_raw_replay_record(self, request_file, plan=None):
        candidate = Path(request_file).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        requests_root = (ROOT / 'outputs' / 'requests').resolve()
        try:
            relative = candidate.relative_to(requests_root)
        except ValueError:
            raise ValueError('Raw replay request must be inside the canonical outputs/requests directory') from None
        if len(relative.parts) != 2 or not re.fullmatch(r'[0-9a-f]{64}', relative.parts[0]):
            raise ValueError('Raw replay request path is not canonical outputs/requests/<seed>/<request_id>.request.json')
        if not candidate.is_file():
            raise ValueError('Raw replay original request is missing')
        try:
            envelope = json.loads(candidate.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError('Raw replay original request is unreadable: %s' % exc) from None
        if not isinstance(envelope, dict) or set(envelope) != RAW_UPLOAD_REPLAY_KEYS:
            raise ValueError('Raw replay original request has missing or unexpected fields')
        if envelope.get('mode') != 'raw-upload' or envelope.get('method') != 'PUT' or envelope.get('body_encoding') != 'raw-file-bytes':
            raise ValueError('Raw replay original request has a noncanonical mode, method, or body encoding')
        if envelope.get('server_base') != self.base:
            raise ValueError('Server mismatch')
        if not isinstance(envelope.get('source_file'), str) or not isinstance(envelope.get('source_filename'), str):
            raise ValueError('Raw replay source fields must be strings')

        if plan is None:
            plan = self._raw_upload_plan(
                envelope.get('path'), envelope.get('source_file'), envelope.get('task_id'),
                envelope.get('request_id'), envelope.get('mime_type'), envelope.get('source_filename'),
            )
        expected_path = plan['request_file'].resolve()
        expected_name = envelope['request_id'] + '.request.json'
        if candidate != expected_path or relative.parts[1] != expected_name:
            raise ValueError('Raw replay request file, seed directory, or request_id is not the canonical original')
        if envelope != plan['envelope']:
            raise ValueError('Raw replay request differs from its complete canonical request description')
        if plan['request_file'] != candidate or not plan['request_file'].is_file():
            raise ValueError('Raw replay original request is missing')

        common = ('method', 'path', 'query', 'size_bytes', 'sha256', 'request_id', 'task_id')
        anchored = 0
        for suffix in ('.receipt.json', '.unknown.json'):
            sidecar = plan['directory'] / (envelope['request_id'] + suffix)
            if not sidecar.exists():
                continue
            try:
                evidence = json.loads(sidecar.read_text())
            except (OSError, ValueError) as exc:
                raise ValueError('Raw replay outcome evidence is unreadable: %s' % exc) from None
            if not isinstance(evidence, dict) or evidence.get('request') != str(plan['request_file']):
                raise ValueError('Raw replay outcome evidence is not bound to the canonical request')
            if any(evidence.get(key) != envelope.get(key) for key in common):
                raise ValueError('Raw replay outcome evidence differs from the canonical request')
            if suffix == '.unknown.json' and evidence.get('outcome') != 'unknown':
                raise ValueError('Raw replay unknown evidence has an invalid outcome')
            anchored += 1
        if not anchored:
            raise ValueError('Raw replay has no canonical receipt or unknown outcome; refusing an unanchored request')
        return plan

    def _send_raw_upload(self, plan):
        body = plan['body']; query = plan['query']; path = plan['envelope']['path']
        size_bytes = plan['size_bytes']; actual_sha256 = plan['sha256']
        rid = plan['request_id']; task = plan['envelope']['task_id']
        directory = plan['directory']; reqfile = plan['request_file']
        unknown_file = directory / (str(rid)+'.unknown.json')
        if reqfile.exists():
            if json.loads(reqfile.read_text()) != plan['envelope']:
                raise ValueError('Persisted raw request differs; use the exact original file, metadata, and request identity')
        headers = {
            'Content-Type': 'application/octet-stream',
            'X-Request-ID': str(rid),
            'X-Task-ID': task,
        }
        request_path = path + '?' + query
        try:
            status, hdr, raw = self.request('PUT', request_path, body, headers)
        except ApiError as e:
            save_json(directory / (str(rid)+'.error.json'), {
                'method': 'PUT', 'path': path, 'query': query, 'size_bytes': size_bytes,
                'sha256': actual_sha256, 'request_id': rid, 'task_id': task,
                'status': e.status, 'detail': e.detail,
            })
            raise
        except Exception as exc:
            save_json(unknown_file, {
                'outcome': 'unknown', 'request': str(reqfile), 'method': 'PUT', 'path': path,
                'query': query, 'size_bytes': size_bytes, 'sha256': actual_sha256,
                'request_id': rid, 'task_id': task, 'exception_type': type(exc).__name__,
                'instruction': 'Read back; do not generate a fresh identity. Replay only when endpoint contract permits.',
            })
            raise
        response_file = directory / (str(rid)+'.response')
        save(response_file, raw)
        save_json(directory / (str(rid)+'.receipt.json'), {
            'status': status, 'request': str(reqfile), 'method': 'PUT', 'path': path,
            'query': query, 'size_bytes': size_bytes, 'sha256': actual_sha256,
            'request_id': rid, 'task_id': task, 'content_type': hdr.get('content-type'),
            'response_file': str(response_file), 'response_sha256': hashlib.sha256(raw).hexdigest(),
        })
        try:
            return json.loads(raw)
        except ValueError:
            return {'status': status, 'response_file': str(response_file)}

    def raw_replay(self, request_file):
        plan = self._validate_raw_replay_record(request_file)
        return self._send_raw_upload(plan)

    def raw_upload(self, path, file_path, task='385-liyuhao', request_id=None, mime_type=None, filename=None, replay=False):
        plan = self._raw_upload_plan(path, file_path, task, request_id, mime_type, filename)
        reqfile = plan['request_file']
        unknown_file = plan['directory'] / (plan['request_id'] + '.unknown.json')
        if replay:
            plan = self._validate_raw_replay_record(reqfile, plan)
        else:
            if reqfile.exists():
                if json.loads(reqfile.read_text()) != plan['envelope']:
                    raise ValueError('Persisted raw request differs; use the exact original file, metadata, and request identity')
            else:
                save_json(reqfile, plan['envelope'])
            if unknown_file.exists():
                raise ValueError('This request previously ended with unknown outcome. Read back the server result; use replay only when idempotency permits.')
        return self._send_raw_upload(plan)


def load_json(name):
    return json.loads(sys.stdin.read() if name == '-' else Path(name).read_text())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    s.add_parser('login'); s.add_parser('doctor')
    c = s.add_parser('catalog'); c.add_argument('--filter', default='')
    c = s.add_parser('schema'); c.add_argument('method'); c.add_argument('path')
    c = s.add_parser('get'); c.add_argument('path'); c.add_argument('--out')
    c = s.add_parser('request'); c.add_argument('method', choices=['POST','PUT','PATCH','DELETE']); c.add_argument('path'); c.add_argument('--json'); c.add_argument('--task', default='385-liyuhao'); c.add_argument('--request-id'); c.add_argument('--allow-partial', action='store_true'); c.add_argument('--out')
    c = s.add_parser('replay'); c.add_argument('request_file'); c.add_argument('--allow-partial', action='store_true')
    c = s.add_parser('download'); c.add_argument('path'); c.add_argument('--out', required=True)
    c = s.add_parser('upload', help='multipart/form-data file upload'); c.add_argument('path'); c.add_argument('--file', required=True); c.add_argument('--file-field', default='file'); c.add_argument('--field', action='append', default=[]); c.add_argument('--task', default='385-liyuhao')
    c = s.add_parser('raw-upload', help='PUT exact dataset-file bytes with automatic query metadata'); c.add_argument('path'); c.add_argument('--file', required=True); c.add_argument('--task', default='385-liyuhao'); c.add_argument('--request-id'); c.add_argument('--out')
    a = p.parse_args()
    if a.command in ('catalog','schema'):
        spec = json.loads((ROOT/'docs/openapi.production.json').read_text())
        if a.command == 'catalog':
            result = [{'method': method.upper(), 'path': path, 'summary': op.get('summary')} for path, methods in spec['paths'].items() if a.filter in path for method,op in methods.items() if method in ['get','post','put','patch','delete']]
        else:
            match = next((path for path in spec['paths'] if re.fullmatch(re.sub(r'\{[^}]+\}', '[^/]+',path), a.path.split('?')[0])),None)
            if not match or a.method.lower() not in spec['paths'][match]:
                raise ValueError('Endpoint not present in production schema snapshot')
            operation = spec['paths'][match][a.method.lower()]
            all_schemas = spec.get('components',{}).get('schemas',{})
            needed = {}
            def collect(value):
                if isinstance(value, dict):
                    ref = value.get('$ref','')
                    if ref.startswith('#/components/schemas/'):
                        key = ref.rsplit('/',1)[-1]
                        if key not in needed and key in all_schemas:
                            needed[key] = all_schemas[key]
                            collect(needed[key])
                    for child in value.values(): collect(child)
                elif isinstance(value, list):
                    for child in value: collect(child)
            collect(operation)
            result = {'path':match,'operation':operation,'schemas':needed}
    else:
        client = Client()
        if a.command == 'login':result=client.login()
        elif a.command == 'doctor':
            me=client.get('/api/auth/me');result={'base_url':client.base,'authenticated':True,'identity':{k:v for k,v in me.items() if 'token' not in k.lower()},'permission_mode':'existing workbench account; all available API methods; server validation retained'}
        elif a.command == 'get':result=client.get(a.path)
        elif a.command == 'request':result=client.mutate(a.method,a.path,load_json(a.json) if a.json else None,a.task,a.request_id,a.allow_partial)
        elif a.command == 'replay':
            e=load_json(a.request_file)
            if not isinstance(e, dict):
                raise ValueError('Replay request matches neither the complete raw-upload nor complete JSON schema')
            keys = frozenset(e)
            if keys == RAW_UPLOAD_REPLAY_KEYS:
                result=client.raw_replay(a.request_file)
            elif keys == JSON_REPLAY_KEYS:
                result=client.json_replay(a.request_file,a.allow_partial)
            else:
                raise ValueError('Replay request matches neither the complete raw-upload nor complete JSON schema')
        elif a.command == 'download':
            status,hdr,raw=client.request('GET',a.path,safe=True);target=Path(a.out)
            if not target.is_absolute():target=ROOT/target
            target=target.resolve()
            if ROOT not in target.parents:raise ValueError('Save downloaded evidence within this project')
            save(target,raw)
            result={'path':str(target),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'status':status,'content_type':hdr.get('content-type'),'source_path':a.path,'source_sha256':hdr.get('x-source-sha256'),'reading_pdf_sha256':hdr.get('x-reading-pdf-sha256')}
            save_json(target.with_name(target.name+'.source.json'),result)
        elif a.command == 'upload':
            f=Path(a.file);boundary='litai-'+uuid.uuid4().hex;parts=[]
            def field(name,value):
                if any(x in name for x in ['\r','\n','"']):raise ValueError('Invalid field')
                parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n').encode())
            for item in a.field:
                k,v=item.split('=',1);field(k,v)
            if any(x in a.file_field+f.name for x in ['\r','\n','"']):raise ValueError('Invalid filename')
            parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+a.file_field+'"; filename="'+f.name+'"\r\nContent-Type: '+(mimetypes.guess_type(f.name)[0] or 'application/octet-stream')+'\r\n\r\n').encode()+f.read_bytes()+b'\r\n')
            body=b''.join(parts)+('--'+boundary+'--\r\n').encode();rid=str(uuid.uuid4());folder=ROOT/'outputs/uploads'/rid
            save(folder/'body.multipart',body);save_json(folder/'request.json',{'path':a.path,'file':str(f),'request_id':rid,'task_id':a.task,'content_type':'multipart/form-data; boundary='+boundary,'body_sha256':hashlib.sha256(body).hexdigest()})
            status,hdr,raw=client.request('POST',a.path,body,{'Content-Type':'multipart/form-data; boundary='+boundary,'X-Request-ID':rid,'X-Task-ID':a.task});save(folder/'response.json',raw);result=json.loads(raw)
        elif a.command == 'raw-upload':
            result=client.raw_upload(a.path,a.file,a.task,a.request_id)
    if getattr(a,'out',None) and a.command!='download':
        target=Path(a.out);save_json(target if target.is_absolute() else ROOT/target,result)
        print(json.dumps({'saved':str(target)},ensure_ascii=False))
    else:print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:main()
    except (ApiError,ValueError,OSError,KeyError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
