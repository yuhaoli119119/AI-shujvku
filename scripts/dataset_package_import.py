#!/usr/bin/env python3
"""Preview or register dataset packages through the workbench API.

The default is read-only. --execute registers datasets/files only: scientific
row import requires a separate, reviewed source-cell mapping and is not done here.
Never retries an unknown-outcome request.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import requests
from dataset_ingest_preview import PAPERS, inspect, digest

# Cross-checked against the 385 source PDFs and PBS registered main files.
MAIN_SHA256 = {
    'U137':'919e82b33bf57c4d9a263d25604737d4f7b18c5aca5554c8dd23a32bb7d779c0',
    'U138':'913d457e9f9e88513f825fd0b0be51d1efc46baf8fcbaa9fa41572d4bdb68cc9',
    'U139':'51f03c26767ce586baff8e7491d566d618d9e6d69c71c7594524f2b0a60efe62',
    'U140':'147d95cdfea1a971bcde427b5b629211ef475509f1213f5500417c47ac9c2963',
    'U141':'27001c1bba44cb39c6e9b83b45534721be5847c87f4d72b2b0e7538f2a312d43',
    'U142':'9f84103949d596d9e5f12b639699b2438a73933ba1496f33f0aca5afe455df7e',
    'U143':'9ea7d30b738602ae101d6631c62573a19867a6f8c7b9c9fb0f0eaa1391b09465',
    'U144':'eab144bbcffecfc1d256d0c1755635e90834b8a1ae8360ba10d76b5a45c40902',
}


def dataset_version(code, sha_map, item):
    if code == 'U137': return 'v1.1.0'
    if code == 'U138': return 'materialscloud-5a-vm'
    if code == 'U141': return item['commit']
    if code == 'U142': return 'si-' + sha_map['u142-si.docx']['sha256'][:16]
    return 'availability-2026-10-28'


def durable_request(directory, key, spec, send):
    """Persist exact intent before sending; never replay an unknown outcome."""
    token = hashlib.sha256(key.encode()).hexdigest()
    request_path, receipt_path = directory / (token + '.request.json'), directory / (token + '.receipt.json')
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    if request_path.exists():
        if request_path.read_text(encoding='utf-8') != encoded:
            raise RuntimeError(f'journal request mismatch: {key}')
        if not receipt_path.exists():
            raise RuntimeError(f'unknown request outcome; inspect original request and server receipt before resuming: {request_path}')
        return json.loads(receipt_path.read_text(encoding='utf-8'))
    def publish(path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    publish(request_path, encoded)
    response = send()
    response.raise_for_status()
    result = response.json()
    publish(receipt_path, json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source_dir', type=Path)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--credentials', type=Path)
    p.add_argument('--journal-dir', type=Path, help='private task directory under project outputs')
    p.add_argument('--include-rows', action='store_true', help='also preflight and import 259 source-bound scientific rows')
    args = p.parse_args()
    preview = inspect(args.source_dir)
    if not args.execute:
        print(json.dumps(preview, ensure_ascii=False, indent=2)); return
    if not args.credentials or not args.journal_dir:
        p.error('--credentials and --journal-dir are required with --execute')
    outputs = (Path(__file__).resolve().parents[1] / 'outputs').resolve()
    journal = args.journal_dir.resolve()
    if not journal.is_relative_to(outputs):
        p.error('--journal-dir must be within this project outputs directory')
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = args.source_dir.resolve(strict=True)
    manifest = json.loads((root / 'DATA_SOURCES_MANIFEST.json').read_text(encoding='utf-8'))
    credentials = json.loads(args.credentials.read_text(encoding='utf-8'))
    base = (credentials.get('base_url') or credentials.get('url') or credentials.get('origin') or '').rstrip('/')
    if not base.startswith(('https://', 'http://127.0.0.1:', 'http://localhost:')):
        raise ValueError('explicit HTTPS or loopback fixture origin required')
    session = requests.Session()
    session.headers['Origin'] = base
    login = session.post(base + '/api/auth/login', json={'username': credentials.get('username') or credentials.get('account'), 'password': credentials['password']}, timeout=30)
    login.raise_for_status()
    session.headers['X-CSRF-Token'] = login.json()['csrf_token']
    # Check every paper identity before the first write.
    for item in manifest['papers']:
        paper_id = PAPERS[item['paper']]
        response = session.get(base + f'/api/papers/{paper_id}', params={'mode': 'light'}, timeout=30)
        response.raise_for_status()
        paper = response.json()
        doi = str(paper.get('doi') or '').lower()
        if str(paper.get('id')) != paper_id or (doi and doi != item['doi'].lower()) or paper.get('library_name') != manifest['library']:
            raise ValueError(f'paper identity mismatch: {item["paper"]}')
        response = session.get(base + f'/api/rebuild/papers/{paper_id}/files', timeout=30)
        response.raise_for_status()
        file_set = response.json()
        active = set(file_set['active_file_ids'])
        matching = [f for f in file_set['items'] if f['id'] in active and f['role'] == 'main' and f['sha256'] == MAIN_SHA256[item['paper']]]
        if len(matching) != 1:
            raise ValueError(f'paper main file identity mismatch: {item["paper"]}')
        response = session.get(base + f'/api/papers/{paper_id}/pdf', params={'legacy_original':'true'}, stream=True, timeout=60)
        response.raise_for_status()
        pdf_hash = hashlib.sha256()
        for chunk in response.iter_content(1024 * 1024): pdf_hash.update(chunk)
        response.close()
        if pdf_hash.hexdigest() != MAIN_SHA256[item['paper']]:
            raise ValueError(f'paper PDF content mismatch: {item["paper"]}')
    registered = {}
    for item in manifest['papers']:
        code = item['paper']; paper_id = PAPERS[code]
        sha_map = item.get('file_hashes', {})
        version = dataset_version(code, sha_map, item)
        metadata = {k:v for k,v in item.items() if k not in ('files', 'file_hashes')}
        metadata['files'] = sha_map
        metadata['downloaded_at'] = None  # Original download time is not evidenced by the manifest.
        metadata['file_notes'] = {
            name: ('experimental_or_derived_source_data' if code == 'U137' and 'source-fig' in name else
                   'derived' if ('rows.csv' in name or name.endswith('rows.xlsx') or 'Tables_S1-S7' in name) else 'source')
            for name in sha_map
        }
        if code == 'U141':
            metadata['access_restriction'] = '许可证待确认；描述符数据集，部分可复现，缺少催化剂身份。'
        body = {'dataset_key': code.lower() + '-data-sources', 'version': version,
                'title': code + ' 数据源', 'metadata': metadata}
        url = base + f'/api/papers/{paper_id}/datasets'
        result = durable_request(journal, 'dataset:'+code, {'method':'POST','url':url,'body':body},
                                 lambda: session.post(url, json=body, timeout=30))
        dataset_id = result['id']
        listing = session.get(url, timeout=30)
        listing.raise_for_status()
        matching = [d for d in listing.json() if d['id'] == dataset_id]
        if len(matching) != 1 or matching[0]['dataset_key'] != body['dataset_key'] or matching[0]['version'] != version or matching[0]['title'] != body['title'] or matching[0]['metadata'] != metadata:
            raise RuntimeError(f'{code}: dataset metadata readback mismatch')
        for name, info in sha_map.items():
            path = root / code / name
            if path.stat().st_size != info['bytes'] or digest(path) != info['sha256']:
                raise ValueError(f'file changed after preview: {code}/{name}')
            params = {'filename': name, 'size_bytes': info['bytes'], 'mime_type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}
            file_url = url + '/' + dataset_id + '/files/' + info['sha256']
            def send_file():
                with path.open('rb') as data:
                    return session.put(file_url, params=params, data=data, timeout=120)
            result = durable_request(journal, 'file:'+code+':'+name,
                                     {'method':'PUT','url':file_url,'params':params,'source_sha256':info['sha256']}, send_file)
            if result['sha256'] != info['sha256'] or result['size_bytes'] != info['bytes']:
                raise ValueError(f'file receipt mismatch: {code}/{name}')
            registered[name] = result
            download = session.get(url + '/' + dataset_id + '/files/' + result['id'], stream=True, timeout=120)
            download.raise_for_status()
            check_hash = hashlib.sha256(); check_size = 0
            for chunk in download.iter_content(1024 * 1024):
                check_hash.update(chunk); check_size += len(chunk)
            download.close()
            if check_hash.hexdigest() != info['sha256'] or check_size != info['bytes']:
                raise ValueError(f'download readback mismatch: {code}/{name}')
        print(f'{code}: dataset {dataset_id}, {len(sha_map)} files registered')
    if args.include_rows:
        from dataset_row_payloads import build
        batches = build(root, registered)
        for code, rows in batches.items():
            expected_rows = {}
            for start in range(0, len(rows), 5):
                chunk = rows[start:start + 5]
                body = {'paper_id': PAPERS[code], 'rows': chunk}
                encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                body['request_id'] = 'dataset-' + hashlib.sha256(encoded).hexdigest()[:48]
                check = session.post(base + '/api/rebuild/rows/import/preflight', json=body, timeout=120)
                check.raise_for_status()
                if not check.json().get('complete'):
                    raise RuntimeError(f'{code} rows {start+1}-{start+len(chunk)} preflight incomplete; stop and inspect response')
                import_url = base + '/api/rebuild/rows/import'
                receipt = durable_request(journal, 'rows:'+code+':'+body['request_id'],
                                          {'method':'POST','url':import_url,'body':body},
                                          lambda: session.post(import_url, json=body, timeout=120))
                if not receipt.get('complete'):
                    raise RuntimeError(f'{code} rows {start+1}-{start+len(chunk)} partially saved; stop and inspect receipt {body["request_id"]}')
                for result in receipt['results']:
                    if not result.get('row_id'):
                        raise RuntimeError(f'{code}: missing row receipt identity')
                    expected_rows[result['row_id']] = chunk[result['row_index']]
                print(f'{code}: rows {start+1}-{start+len(chunk)} receipt {body["request_id"]}')
            pending = dict(expected_rows); offset = 0
            while pending:
                read = session.get(base + '/api/rebuild/rows', params={'paper_id':PAPERS[code], 'limit':100, 'offset':offset}, timeout=120)
                read.raise_for_status(); page = read.json()
                for row in page['items']:
                    expected = pending.pop(row['id'], None)
                    if expected is None: continue
                    cells = {c['field_name']:c for c in row['values']}
                    for wanted in expected['values']:
                        actual = cells.get(wanted['field_name'])
                        if not actual or actual['raw_value'] != wanted['raw_value'] or actual['unit'] != wanted['unit']:
                            raise RuntimeError(f'{code}: cell readback mismatch in row {row["id"]}')
                        src = wanted['sources'][0]
                        if not any(x['source_kind'] == 'dataset' and x['dataset_file_id'] == src['dataset_file_id'] and x['dataset_locator'] == src['dataset_locator'] for x in actual['sources']):
                            raise RuntimeError(f'{code}: source readback mismatch in row {row["id"]}')
                if not page['has_more']: break
                offset = page['next_offset']
            if pending: raise RuntimeError(f'{code}: {len(pending)} receipt rows missing from readback')
            print(f'{code}: {len(expected_rows)} rows and their source cells read back')
    else:
        print('Scientific rows were not imported; pass --include-rows only after isolated verification.')

if __name__ == '__main__':
    main()
