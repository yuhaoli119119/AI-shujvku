"""Offline, explicitly reviewed shard orchestration; no scientific inference.

Approvals are caller-supplied review receipts, not signatures or a grant of
scientific eligibility. All shards use the same immutable snapshot. Overlap is
retained in shard artifacts and deduplicated only in the aggregate UUID index.
"""
from collections import defaultdict
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from app.services.rebuild_ml_dataset import (
    MODELS, SHA, _authorized, build_dataset, digest, encoded, paper_revision,
    PARTIAL_ACCEPTANCE_VERSION,
)

PLAN_VERSION = 'rebuild_ml_shard_plan_v1'
INDEX_VERSION = 'rebuild_ml_shard_index_v1'
FILES = frozenset({'observations.jsonl', 'formal_numeric.jsonl', 'formal_text.jsonl',
    'exploratory.jsonl', 'acceptance.json', 'snapshot.json', 'schema.json',
    'build_manifest.json', 'SHA256SUMS'})
SAFE_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$')


def load_plan(path):
    """JSON duplicate keys must not silently replace a shard or approval."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate plan key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def _hashes(payload):
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}


def _checksums(payload):
    return ''.join(sha + '  ' + name + '\n' for name, sha in _hashes(payload).items()).encode()


def _records(payload, name):
    return [json.loads(line) for line in payload[name].splitlines()]


def _formal(payload):
    return _records(payload, 'formal_numeric.jsonl') + _records(payload, 'formal_text.jsonl')


def _membership(records):
    ids = [r['value_id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate output value UUID')
    return sorted(ids)


def _verify_shard(payload):
    if set(payload) != FILES:
        raise ValueError('shard must contain exactly the nine-file contract')
    if payload['SHA256SUMS'] != _checksums({k: v for k, v in payload.items() if k != 'SHA256SUMS'}):
        raise ValueError('shard output hash mismatch')
    info = json.loads(payload['build_manifest.json'])
    if info['files'] != _hashes({k: v for k, v in payload.items()
                               if k not in {'SHA256SUMS', 'build_manifest.json'}}):
        raise ValueError('shard build manifest hash mismatch')
    if info['split_status'] != 'preview/not final':
        raise ValueError('shard split must remain preview')


def _union(shards, entries, overlap_policy):
    owners = defaultdict(list)
    semantics = {}
    rows = set(); papers = set()
    for sid in sorted(shards):
        policy = entries[sid]['policy_sha256']
        for record in _formal(shards[sid]):
            vid = record['value_id']
            # Conservative equality includes policy, all stored/provenance fields,
            # review dispositions and masks, not merely the numerical projection.
            identity = digest({'policy_sha256': policy, 'observation': record})
            if vid in semantics:
                if semantics[vid] != identity:
                    raise ValueError('overlapping UUID has conflicting semantics: ' + vid)
                if overlap_policy != 'deduplicate_identical':
                    raise ValueError('overlapping UUID requires explicit deduplication: ' + vid)
            semantics[vid] = identity
            owners[vid].append(sid)
            rows.add(record['row_id']); papers.add(record['paper_id'])
    memberships = {vid: {'shard_ids': ids, 'canonical_shard_id': ids[0],
                        'semantics_sha256': semantics[vid]} for vid, ids in sorted(owners.items())}
    overlaps = {vid: ids for vid, ids in sorted(owners.items()) if len(ids) > 1}
    return {'memberships': memberships, 'union_value_ids': sorted(owners),
            'union_membership_sha256': digest(sorted(owners)),
            'union_value_count': len(owners), 'union_row_count': len(rows),
            'union_paper_count': len(papers), 'overlaps': overlaps,
            'overlap_value_count': len(overlaps),
            'total_shard_memberships': sum(len(ids) for ids in owners.values()),
            'deduplicated_memberships': sum(len(ids) - 1 for ids in owners.values())}


def validate_frozen_plan(snapshot, plan):
    """Shared fail-closed input contract for building and accepting readback."""
    if plan.get('schema_version') != PLAN_VERSION:
        raise ValueError('unsupported shard plan schema')
    if (plan.get('snapshot_sha256') != snapshot.get('snapshot_sha256') or
            snapshot.get('snapshot_sha256') != digest(snapshot['graph']) or
            not SHA.fullmatch(snapshot.get('query_bundle_sha256', ''))):
        raise ValueError('missing or stale snapshot/provenance')
    for kind in MODELS:
        ids = [x['id'] for x in snapshot['graph'][kind]]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate snapshot UUID: ' + kind)
    shards = plan.get('shards')
    approvals = plan.get('approved_policies')
    if not isinstance(shards, dict) or not shards or not isinstance(approvals, dict) or not approvals:
        raise ValueError('explicit shards and approved policies required')
    if plan.get('overlap_policy') not in {'reject', 'deduplicate_identical'}:
        raise ValueError('explicit overlap policy required')
    for pid, approved in approvals.items():
        policy = approved.get('policy', {})
        if (not SAFE_ID.fullmatch(pid) or policy.get('data_type') not in {'dft', 'experimental'} or
                approved.get('policy_sha256') != digest(policy) or
                not _authorized({'schema_version': PARTIAL_ACCEPTANCE_VERSION}, approved)):
            raise ValueError('unapproved data_type/policy: ' + pid)
    for sid, shard in sorted(shards.items()):
        if not SAFE_ID.fullmatch(sid):
            raise ValueError('invalid shard_id')
        approved = approvals.get(shard.get('policy_id'))
        if approved is None:
            raise ValueError('unknown policy')
        manifest = shard['manifest']
        if (manifest.get('policy') != approved['policy'] or
                shard.get('data_type') != approved['policy']['data_type']):
            raise ValueError('mixed or undeclared shard policy/data_type')
        if (shard.get('manifest_sha256') != digest(manifest) or
                shard.get('snapshot_sha256') != snapshot['snapshot_sha256']):
            raise ValueError('stale shard input manifest/snapshot hash')
        members = shard.get('eligible_value_ids')
        if (not isinstance(members, list) or not members or
                members != sorted({str(UUID(x)) for x in members}) or
                shard.get('membership_sha256') != digest(members)):
            raise ValueError('explicit sorted unique eligible UUID set and hash required')
        known = {x['id'] for x in snapshot['graph']['values']}
        if not set(members) <= known:
            raise ValueError('member outside snapshot')
        # The legacy builder represents stale reviews as exclusions. The shard
        # contract rejects stale paper revisions explicitly, even before output.
        for pid, decision in manifest.get('papers', {}).items():
            if decision.get('graph_revision') != paper_revision(snapshot, pid):
                raise ValueError('stale paper graph revision')
    return shards


def build_sharded_dataset(snapshot, plan, *, seed=0, code_revision):
    """Return a flat relative-path/bytes map; never reads DB or writes artifacts."""
    shards = validate_frozen_plan(snapshot, plan)
    outputs = {}; entries = {}; payload = {'shard_plan.json': encoded(plan)}
    for sid, shard in sorted(shards.items()):
        manifest = shard['manifest']
        approved = plan['approved_policies'][shard['policy_id']]
        members = shard['eligible_value_ids']
        out = build_dataset(snapshot, manifest, seed=seed, code_revision=code_revision)
        _verify_shard(out)
        formal = _formal(out)
        if _membership(formal) != members:
            raise ValueError('shard output membership mismatch (eligibility/provenance/scope)')
        hashes = _hashes(out)
        if 'expected_files_sha256' in shard and shard['expected_files_sha256'] != hashes:
            raise ValueError('shard output hash mismatch against plan')
        outputs[sid] = out
        entries[sid] = {'policy_id': shard['policy_id'], 'policy_sha256': digest(approved['policy']),
            'data_type': shard['data_type'], 'input_manifest_sha256': digest(manifest),
            'snapshot_sha256': snapshot['snapshot_sha256'], 'value_ids': members,
            'output_membership_sha256': digest(members), 'value_count': len(members),
            'row_count': len({r['row_id'] for r in formal}),
            'statistics': json.loads(out['build_manifest.json'])['statistics'],
            'files': {f'shards/{sid}/{name}': sha for name, sha in hashes.items()}}
        payload.update({f'shards/{sid}/{name}': data for name, data in out.items()})
    payload['shard_index.json'] = encoded({'schema_version': INDEX_VERSION,
        'split_status': 'preview/not final', 'shards': entries})
    hashes = _hashes(payload)
    payload['aggregate_index.json'] = encoded({'schema_version': INDEX_VERSION,
        'code_revision': code_revision, 'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'seed': seed, 'split_status': 'preview/not final', 'production_writes': 0,
        'plan_sha256': digest(plan), 'overlap_policy': plan['overlap_policy'],
        **_union(outputs, entries, plan['overlap_policy']),
        'files': hashes, 'aggregate_content_sha256': digest(hashes)})
    payload['SHA256SUMS'] = _checksums(payload)
    verify_sharded_dataset(payload)
    return payload


def verify_sharded_dataset(payload):
    """Recompute file and membership hashes before accepting a build readback."""
    aggregate = json.loads(payload['aggregate_index.json'])
    hashes = _hashes({k: v for k, v in payload.items() if k not in {'aggregate_index.json', 'SHA256SUMS'}})
    if aggregate['files'] != hashes or aggregate['aggregate_content_sha256'] != digest(hashes):
        raise ValueError('aggregate output hash mismatch')
    if payload['SHA256SUMS'] != _checksums({k: v for k, v in payload.items() if k != 'SHA256SUMS'}):
        raise ValueError('aggregate checksums mismatch')
    index = json.loads(payload['shard_index.json'])
    entries = index['shards']
    if (index.get('schema_version') != INDEX_VERSION or aggregate.get('schema_version') != INDEX_VERSION or
            index.get('split_status') != 'preview/not final' or
            aggregate.get('split_status') != 'preview/not final' or
            aggregate.get('production_writes') != 0):
        raise ValueError('invalid aggregate/index version or preview status')
    outputs = {}
    expected_paths = {'shard_plan.json', 'shard_index.json', 'aggregate_index.json', 'SHA256SUMS'}
    plan = json.loads(payload['shard_plan.json'])
    # Validate every embedded snapshot against the exact same frozen contract.
    # Outer checksums are integrity checks, never authorization.
    for sid in entries:
        validate_frozen_plan(json.loads(payload[f'shards/{sid}/snapshot.json']), plan)
    if not entries:
        raise ValueError('explicit shards required')
    if (set(entries) != set(plan['shards']) or aggregate['plan_sha256'] != digest(plan) or
            aggregate['overlap_policy'] != plan['overlap_policy']):
        raise ValueError('shard index/plan mismatch')
    for sid, entry in entries.items():
        if not SAFE_ID.fullmatch(sid):
            raise ValueError('invalid shard_id')
        prefix = f'shards/{sid}/'
        out = {name: payload[prefix + name] for name in FILES}
        _verify_shard(out)
        outputs[sid] = out
        members = _membership(_formal(out))
        shard = plan['shards'][sid]
        manifest = json.loads(out['acceptance.json'])
        approved = plan['approved_policies'][shard['policy_id']]
        snapshot = json.loads(out['snapshot.json'])
        info = json.loads(out['build_manifest.json'])
        if (manifest != shard['manifest'] or
                entry['input_manifest_sha256'] != digest(manifest) or
                entry['policy_id'] != shard['policy_id'] or
                entry['policy_sha256'] != digest(approved['policy']) or
                manifest['policy'] != approved['policy'] or
                entry['data_type'] != manifest['policy']['data_type'] or
                entry['snapshot_sha256'] != snapshot['snapshot_sha256'] or
                snapshot['snapshot_sha256'] != digest(snapshot['graph']) or
                snapshot['snapshot_sha256'] != plan['snapshot_sha256'] or
                info['acceptance_sha256'] != digest(manifest) or
                entry['statistics'] != info['statistics'] or
                entry['row_count'] != len({r['row_id'] for r in _formal(out)}) or
                members != shard['eligible_value_ids'] or
                digest(members) != shard['membership_sha256']):
            raise ValueError('shard policy/input membership mismatch')
        if 'expected_files_sha256' in shard and shard['expected_files_sha256'] != _hashes(out):
            raise ValueError('shard output hash mismatch against plan')
        if (entry['value_ids'] != members or entry['value_count'] != len(members) or
                entry['output_membership_sha256'] != digest(members) or
                entry['files'] != {prefix + k: v for k, v in _hashes(out).items()}):
            raise ValueError('shard index output hash/membership mismatch')
        expected_paths.update(prefix + name for name in FILES)
    if set(payload) != expected_paths:
        raise ValueError('unexpected aggregate artifacts')
    union = _union(outputs, entries, aggregate['overlap_policy'])
    if any(aggregate.get(k) != v for k, v in union.items()):
        raise ValueError('aggregate union mismatch')
    return aggregate


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextmanager
def _directory_at(parent_fd, name):
    """Open one component only; never resolve symlinks."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _output_parent(output):
    # Walk even pre-existing ancestors without following links. Do not resolve()
    # first: that would silently accept a caller's symlink destination.
    parts = Path(os.path.abspath(output)).parts
    fd = os.open(parts[0], _DIR_FLAGS)
    try:
        for part in parts[1:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _publish_no_clobber(source_fd, staging, parent_fd, destination):
    """Linux renameat2(RENAME_NOREPLACE): atomic even against an empty dir."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2  # Fail closed on unsupported platforms; no rename fallback.
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(staging), parent_fd, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _write_staged_file(fd, name, data):
    leaf = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o600, dir_fd=fd)
    with os.fdopen(leaf, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def write_sharded_dataset(payload, output):
    """Verify private staging before atomic publication; retain failed evidence.

    Directory descriptors and O_NOFOLLOW protect every component. Staging is
    private (0700); this is not a security boundary against root or a hostile
    process with the same credentials able to mutate already-open files.
    """
    verify_sharded_dataset(payload)
    with _output_parent(output) as (parent_fd, destination):
        # lexists via lstat rejects even a dangling destination symlink.
        try:
            os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(destination)
        staging = '.' + destination + '.staging-' + uuid4().hex
        os.mkdir(staging, mode=0o700, dir_fd=parent_fd)
        # Keep the rename source inside a private wrapper, not in the shared
        # destination parent. Publication uses its open descriptor even if the
        # wrapper's external name is moved or replaced concurrently.
        with _directory_at(parent_fd, staging) as workspace_fd:
            workspace = os.fstat(workspace_fd)
            if workspace.st_uid != os.geteuid() or stat.S_IMODE(workspace.st_mode) != 0o700:
                raise ValueError('unsafe staging permissions/owner')
            os.mkdir('payload', mode=0o700, dir_fd=workspace_fd)
            with _directory_at(workspace_fd, 'payload') as root_fd:
                identities = {'': _identity(root_fd)}

                @contextmanager
                def directory(parts, create=False):
                    fd = os.dup(root_fd)
                    try:
                        for i, part in enumerate(parts):
                            key = '/'.join(parts[:i + 1])
                            if create and key not in identities:
                                # No exist_ok: inserted directories or links fail closed.
                                os.mkdir(part, mode=0o700, dir_fd=fd)
                            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                            os.close(fd)
                            fd = child
                            if key in identities and identities[key] != _identity(fd):
                                raise ValueError('staging directory replaced')
                            identities[key] = _identity(fd)
                        yield fd
                    finally:
                        os.close(fd)

                for name, data in sorted(payload.items()):
                    parts = name.split('/')
                    with directory(parts[:-1], create=True) as fd:
                        _write_staged_file(fd, parts[-1], data)
                        os.fsync(fd)
                readback = {}
                for name in sorted(payload):
                    parts = name.split('/')
                    with directory(parts[:-1]) as fd:
                        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                       dir_fd=fd)
                        with os.fdopen(leaf, 'rb') as handle:
                            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                                raise ValueError('non-regular staging file')
                            readback[name] = handle.read()
                # Check complete directory contents as well as the expected bytes.
                for key in identities:
                    parts = key.split('/') if key else []
                    with directory(parts) as fd:
                        expected = {n[len(key) + 1:].split('/')[0] if key else n.split('/')[0]
                                    for n in payload if not key or n.startswith(key + '/')}
                        if set(os.listdir(fd)) != expected:
                            raise ValueError('unexpected staging entries')
                if readback != payload:
                    raise ValueError('staging readback bytes mismatch')
                verify_sharded_dataset(readback)
                with _directory_at(workspace_fd, 'payload') as current_fd:
                    if _identity(current_fd) != identities['']:
                        raise ValueError('staging root replaced')
                os.fsync(root_fd)
                _publish_no_clobber(workspace_fd, 'payload', parent_fd, destination)
            os.fsync(workspace_fd)
        os.fsync(parent_fd)
