"""Offline synthetic approvals only; production and DB connections forbidden."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy.engine import Engine

from app.services.rebuild_ml_dataset import digest, encoded
from app.services.rebuild_ml_shards import (FILES, build_sharded_dataset, load_plan,
    verify_sharded_dataset, write_sharded_dataset)

pytestmark = pytest.mark.no_test_database
FIXTURE = Path(__file__).parent / 'fixtures/ml_shards'


@pytest.fixture
def inputs(monkeypatch):
    def blocked(*args, **kwargs): raise AssertionError('DB connections forbidden')
    monkeypatch.setattr(Engine, 'connect', blocked)
    return json.loads((FIXTURE/'snapshot.json').read_text()), load_plan(FIXTURE/'plan.json')


def build(s, p): return build_sharded_dataset(s, p, seed=31, code_revision='synthetic-code')


def refresh(p):
    for shard in p['shards'].values():
        shard['manifest_sha256'] = digest(shard['manifest'])
        shard['membership_sha256'] = digest(shard['eligible_value_ids'])
    for approved in p['approved_policies'].values():
        approved['policy_sha256'] = digest(approved['policy'])


def test_independent_policies_exact_union_and_byte_identical_reruns(inputs, tmp_path):
    s, p = inputs; before = deepcopy((s, p))
    one = build(s, p); two = build(s, p)
    assert one == two and (s, p) == before
    assert len(one) == 22 and 'formal_numeric.jsonl' not in one
    aggregate = verify_sharded_dataset(one)
    assert aggregate['union_value_count'] == 2
    assert aggregate['union_row_count'] == aggregate['union_paper_count'] == 2
    assert aggregate['overlap_value_count'] == aggregate['deduplicated_memberships'] == 0
    assert aggregate['split_status'] == 'preview/not final'
    for sid in p['shards']:
        prefix = f'shards/{sid}/'
        assert set(n[len(prefix):] for n in one if n.startswith(prefix)) == FILES
        records = [json.loads(x) for x in one[prefix+'formal_numeric.jsonl'].splitlines()]
        assert len(records) == 1 and records[0]['identity']['data_type'] == sid
        r = records[0]
        assert r['raw_value'] == '−2.40' and r['precision_digits'] == 2 and r['unit'] == 'eV'
        assert r['row_partial'] is True and r['missing_cells'] == {'unreported_energy':'not_reported'}
        assert r['sources'][0]['page_number'] == 1 and r['sources'][0]['file']['sha256']
        observations = [json.loads(x) for x in one[prefix+'observations.jsonl'].splitlines()]
        missing = [r for r in observations if r['missing_mask']['is_missing']]
        assert missing and all(r['raw_value'] is None and r['numeric_value'] is None for r in missing)
        for line in one[prefix+'SHA256SUMS'].decode().splitlines():
            sha, name = line.split('  ')
            assert hashlib.sha256(one[prefix+name]).hexdigest() == sha
    for line in one['SHA256SUMS'].decode().splitlines():
        sha, name = line.split('  ')
        assert hashlib.sha256(one[name]).hexdigest() == sha
    write_sharded_dataset(one, tmp_path/'build')
    assert {str(f.relative_to(tmp_path/'build')):f.read_bytes() for f in (tmp_path/'build').rglob('*') if f.is_file()} == one
    with pytest.raises(FileExistsError): write_sharded_dataset(one, tmp_path/'build')


def test_dedup_exact_union_does_not_assume_disjointness(inputs):
    s, p = inputs
    p['shards']['experimental-copy'] = deepcopy(p['shards']['experimental'])
    with pytest.raises(ValueError, match='deduplication'): build(s, p)
    p['overlap_policy'] = 'deduplicate_identical'
    out = build(s, p); a = verify_sharded_dataset(out)
    assert a['total_shard_memberships'] == 3 and a['union_value_count'] == 2
    assert a['overlap_value_count'] == a['deduplicated_memberships'] == 1
    assert list(a['overlaps'].values()) == [['experimental', 'experimental-copy']]
    assert a['union_membership_sha256'] == digest(a['union_value_ids'])
    assert sum(len(x['shard_ids']) for x in a['memberships'].values()) == 3


def test_conflicting_overlap_even_when_dedup_declared(inputs):
    s, p = inputs
    p['overlap_policy'] = 'deduplicate_identical'
    p['approved_policies']['different-role'] = deepcopy(p['approved_policies']['experimental'])
    other = p['approved_policies']['different-role']['policy']
    other['fields']['energy']['role'] = 'feature'
    p['shards']['copy'] = deepcopy(p['shards']['experimental'])
    p['shards']['copy'].update(policy_id='different-role')
    p['shards']['copy']['manifest']['policy'] = deepcopy(other)
    refresh(p)
    with pytest.raises(ValueError, match='conflicting semantics'): build(s, p)


@pytest.mark.parametrize('case', ['unknown_policy','unapproved','bad_policy_hash','unknown_type',
    'mixed_policy','undeclared_type','stale_snapshot','stale_shard','stale_manifest','stale_revision',
    'missing_provenance','stale_disposition','membership_extra','membership_missing','bad_member_hash',
    'duplicate_member','outside_member','duplicate_snapshot','unsafe_shard','no_overlap_policy',
    'missing_source','missing_page_identity','bad_output_hash'])
def test_fail_closed(inputs, case):
    s, p = inputs; shard = p['shards']['experimental']; m = shard['manifest']
    vid = shard['eligible_value_ids'][0]
    if case == 'unknown_policy': shard['policy_id'] = 'unknown'
    elif case == 'unapproved': p['approved_policies']['experimental']['reviewer_authorization']['authorized'] = False
    elif case == 'bad_policy_hash': p['approved_policies']['experimental']['policy_sha256'] = '0'*64
    elif case == 'unknown_type':
        p['approved_policies']['experimental']['policy']['data_type'] = 'other'
        p['approved_policies']['experimental']['policy_sha256'] = digest(p['approved_policies']['experimental']['policy'])
    elif case == 'mixed_policy': m['policy']['data_type'] = 'dft'; refresh(p)
    elif case == 'undeclared_type': shard.pop('data_type')
    elif case == 'stale_snapshot': p['snapshot_sha256'] = '0'*64
    elif case == 'stale_shard': shard['snapshot_sha256'] = '0'*64
    elif case == 'stale_manifest': shard['manifest_sha256'] = '0'*64
    elif case == 'stale_revision':
        next(iter(m['papers'].values()))['graph_revision'] = '0'*64; refresh(p)
    elif case == 'missing_provenance': s.pop('query_bundle_sha256')
    elif case == 'stale_disposition': m['values'][vid]['sha256'] = '0'*64; refresh(p)
    elif case == 'membership_extra': shard['eligible_value_ids'] = sorted(v['id'] for v in s['graph']['values']); refresh(p)
    elif case == 'membership_missing': shard['eligible_value_ids'] = []; refresh(p)
    elif case == 'bad_member_hash': shard['membership_sha256'] = '0'*64
    elif case == 'duplicate_member': shard['eligible_value_ids'] *= 2; refresh(p)
    elif case == 'outside_member': shard['eligible_value_ids'] = ['00000000-0000-0000-0000-000000000999']; refresh(p)
    elif case == 'duplicate_snapshot':
        s['graph']['values'].append(deepcopy(s['graph']['values'][0])); s['snapshot_sha256'] = digest(s['graph']); p['snapshot_sha256'] = s['snapshot_sha256']
    elif case == 'unsafe_shard': p['shards']['../../escape'] = p['shards'].pop('experimental')
    elif case == 'no_overlap_policy': p.pop('overlap_policy')
    elif case == 'missing_source':
        source = next(x for x in s['graph']['sources'] if x['value_id']==vid)
        m['sources'].pop(source['id']); refresh(p)
    elif case == 'missing_page_identity':
        # A stale graph digest itself is sufficient to block a changed source.
        next(x for x in s['graph']['values'] if x['id']==vid)['conflict'] = {}
    elif case == 'bad_output_hash': shard['expected_files_sha256'] = {'formal_numeric.jsonl':'0'*64}
    with pytest.raises(ValueError): build(s, p)


@pytest.mark.parametrize('name', ['shards/dft/formal_numeric.jsonl','shards/experimental/SHA256SUMS',
    'shard_index.json','aggregate_index.json','SHA256SUMS'])
def test_readback_rejects_tampering(inputs, name, tmp_path):
    out = build(*inputs); out[name] += b' '
    with pytest.raises(ValueError): write_sharded_dataset(out, tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_plan_duplicate_shard_key_is_rejected(tmp_path):
    path = tmp_path/'plan.json'
    path.write_text('{"shards":{"same":{},"same":{}}}')
    with pytest.raises(ValueError, match='duplicate plan key'): load_plan(path)


def test_cli_independent_processes(inputs, tmp_path):
    script = Path(__file__).parents[1]/'scripts/export_rebuild_ml.py'
    for suffix in ['a','b']:
        subprocess.run([sys.executable,str(script),'build-shards','--snapshot',str(FIXTURE/'snapshot.json'),
            '--plan',str(FIXTURE/'plan.json'),'--output',str(tmp_path/suffix),'--seed','31',
            '--code-revision','synthetic-code'],check=True)
    files = lambda root: {str(f.relative_to(root)):f.read_bytes() for f in root.rglob('*') if f.is_file()}
    assert files(tmp_path/'a') == files(tmp_path/'b') == build(*inputs)


def test_expected_output_hashes_can_be_pinned(inputs):
    s, p = inputs; first = build(s, p)
    for sid, shard in p['shards'].items():
        shard['expected_files_sha256'] = {name:hashlib.sha256(first[f'shards/{sid}/{name}']).hexdigest() for name in FILES}
    verify_sharded_dataset(build(s, p))


@pytest.mark.parametrize('case', ['nested_checksum','row_count','policy_identity','union','preview'])
def test_rehashed_outer_index_cannot_hide_inner_inconsistency(inputs, case):
    out = build(*inputs)
    index = json.loads(out['shard_index.json'])
    if case == 'nested_checksum':
        out['shards/dft/formal_numeric.jsonl'] += b' '
    elif case == 'row_count': index['shards']['dft']['row_count'] = 99
    elif case == 'policy_identity': index['shards']['dft']['policy_sha256'] = '0'*64
    out['shard_index.json'] = encoded(index)
    a = json.loads(out['aggregate_index.json'])
    a['files'] = {k:hashlib.sha256(v).hexdigest() for k,v in sorted(out.items()) if k not in {'aggregate_index.json','SHA256SUMS'}}
    a['aggregate_content_sha256'] = digest(a['files'])
    if case == 'union': a['union_value_count'] = 99
    if case == 'preview': a['split_status'] = 'final'
    out['aggregate_index.json'] = encoded(a)
    out['SHA256SUMS'] = ''.join(hashlib.sha256(v).hexdigest()+'  '+k+'\n' for k,v in sorted(out.items()) if k!='SHA256SUMS').encode()
    with pytest.raises(ValueError): verify_sharded_dataset(out)


def rehash_root(out):
    """Reviewer probe: deliberately refresh every outer integrity hash."""
    aggregate = json.loads(out['aggregate_index.json'])
    aggregate['plan_sha256'] = digest(json.loads(out['shard_plan.json']))
    aggregate['files'] = {k: hashlib.sha256(v).hexdigest() for k, v in sorted(out.items())
                          if k not in {'aggregate_index.json', 'SHA256SUMS'}}
    aggregate['aggregate_content_sha256'] = digest(aggregate['files'])
    out['aggregate_index.json'] = encoded(aggregate)
    out['SHA256SUMS'] = ''.join(hashlib.sha256(v).hexdigest() + '  ' + k + '\n'
        for k, v in sorted(out.items()) if k != 'SHA256SUMS').encode()


@pytest.mark.parametrize('case', ['authorization_false', 'stale_manifest_hash',
    'stale_shard_snapshot', 'unknown_plan_schema', 'undeclared_shard_type',
    'no_receipt', 'bad_policy_digest', 'empty_shards', 'empty_policies', 'unknown_overlap'])
def test_reviewer_frozen_plan_bypasses_rejected_by_all_gates(inputs, tmp_path, case):
    s, p = inputs
    out = build(s, p)
    shard = p['shards']['experimental']
    approved = p['approved_policies']['experimental']
    if case == 'authorization_false': approved['reviewer_authorization']['authorized'] = False
    elif case == 'stale_manifest_hash': shard['manifest_sha256'] = '0' * 64
    elif case == 'stale_shard_snapshot': shard['snapshot_sha256'] = '0' * 64
    elif case == 'unknown_plan_schema': p['schema_version'] = 'invalid'
    elif case == 'undeclared_shard_type': shard['data_type'] = 'unknown'
    elif case == 'no_receipt': approved['reviewer_authorization']['receipt'] = ''
    elif case == 'bad_policy_digest': approved['policy_sha256'] = '0' * 64
    elif case == 'empty_shards': p['shards'] = {}
    elif case == 'empty_policies': p['approved_policies'] = {}
    elif case == 'unknown_overlap': p['overlap_policy'] = 'unknown'
    out['shard_plan.json'] = encoded(p)
    rehash_root(out)
    with pytest.raises(ValueError): build(s, p)
    with pytest.raises(ValueError): verify_sharded_dataset(out)
    with pytest.raises(ValueError): write_sharded_dataset(out, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert not list(tmp_path.glob('.*.staging-*'))


@pytest.mark.parametrize('fail_at', range(1, 23))
def test_staged_io_failure_keeps_final_absent_and_same_path_retry(inputs, tmp_path, monkeypatch, fail_at):
    from app.services import rebuild_ml_shards as module
    out = build(*inputs)
    original = module._write_staged_file
    calls = []
    def failing(fd, name, data):
        calls.append(name)
        if len(calls) == fail_at:
            raise OSError('injected disk/write failure')
        return original(fd, name, data)
    with monkeypatch.context() as m:
        m.setattr(module, '_write_staged_file', failing)
        with pytest.raises(OSError, match='injected'):
            write_sharded_dataset(out, tmp_path / 'build')
    assert not (tmp_path / 'build').exists()
    staging, = tmp_path.glob('.build.staging-*')
    assert len([p for p in staging.rglob('*') if p.is_file()]) == fail_at - 1
    write_sharded_dataset(out, tmp_path / 'build')
    assert staging.is_dir()  # Failed evidence is retained, not cleaned up.
    assert {str(p.relative_to(tmp_path / 'build')): p.read_bytes()
            for p in (tmp_path / 'build').rglob('*') if p.is_file()} == out


@pytest.mark.parametrize('component', ['shards', 'dft', 'SHA256SUMS'])
def test_reviewer_symlink_insertion_at_creation_cannot_escape(inputs, tmp_path, monkeypatch, component):
    import os
    from app.services import rebuild_ml_shards as module
    out = build(*inputs)
    outside = tmp_path / 'outside'; outside.mkdir()
    original = os.mkdir
    injected = []
    def inserting(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        fd = kwargs.get('dir_fd')
        if fd is not None and not injected:
            if name == 'payload' and component in {'shards', 'SHA256SUMS'}:
                with module._directory_at(fd, name) as stage:
                    os.symlink(str(outside), component, dir_fd=stage)
                injected.append(component)
            elif name == 'shards' and component == 'dft':
                with module._directory_at(fd, name) as shards:
                    os.symlink(str(outside), component, dir_fd=shards)
                injected.append(component)
        return result
    monkeypatch.setattr(os, 'mkdir', inserting)
    with pytest.raises(OSError): write_sharded_dataset(out, tmp_path / 'build')
    assert injected and list(outside.iterdir()) == []
    assert not (tmp_path / 'build').exists()


def test_output_ancestor_symlink_rejected(inputs, tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    (tmp_path / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError): write_sharded_dataset(build(*inputs), tmp_path / 'link' / 'build')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('kind', ['empty_directory', 'symlink'])
def test_atomic_publication_does_not_clobber_concurrent_destination(inputs, tmp_path, monkeypatch, kind):
    from app.services import rebuild_ml_shards as module
    original = module._publish_no_clobber
    destination = tmp_path / 'build'
    outside = tmp_path / 'outside'; outside.mkdir()
    def occupied(source_fd, staging, fd, name):
        if kind == 'empty_directory': destination.mkdir()
        else: destination.symlink_to(outside, target_is_directory=True)
        original(source_fd, staging, fd, name)
    monkeypatch.setattr(module, '_publish_no_clobber', occupied)
    with pytest.raises(FileExistsError): write_sharded_dataset(build(*inputs), destination)
    assert list(destination.iterdir()) == []
    assert destination.is_symlink() == (kind == 'symlink')
    assert len(list(tmp_path.glob('.build.staging-*'))) == 1


def test_staging_corruption_rejected_before_publication(inputs, tmp_path, monkeypatch):
    from app.services import rebuild_ml_shards as module
    original = module._write_staged_file
    def corrupt(fd, name, data):
        original(fd, name, data + b' ')
    monkeypatch.setattr(module, '_write_staged_file', corrupt)
    with pytest.raises(ValueError, match='readback'): write_sharded_dataset(build(*inputs), tmp_path / 'build')
    assert not (tmp_path / 'build').exists()


@pytest.mark.parametrize('component', ['wrapper', 'root', 'shards', 'dft'])
def test_directory_replaced_by_symlink_between_mkdir_and_open(inputs, tmp_path, monkeypatch, component):
    import os
    out = build(*inputs)
    outside = tmp_path / 'outside'; outside.mkdir()
    original = os.mkdir
    inserted = []
    def replace(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        fd = kwargs.get('dir_fd')
        matches = (str(name).startswith('.build.staging-') if component == 'wrapper'
                   else name == ('payload' if component == 'root' else component))
        if fd is not None and matches and not inserted:
            os.rename(name, str(name) + '.retained', src_dir_fd=fd, dst_dir_fd=fd)
            os.symlink(str(outside), name, dir_fd=fd)
            inserted.append(name)
        return result
    monkeypatch.setattr(os, 'mkdir', replace)
    with pytest.raises(OSError): write_sharded_dataset(out, tmp_path / 'build')
    assert inserted and list(outside.iterdir()) == []
    assert not (tmp_path / 'build').exists()


def test_reviewer_fourth_exclusive_open_failure_is_retryable(inputs, tmp_path, monkeypatch):
    import os
    out = build(*inputs)
    original = os.open
    writes = []
    def fourth(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            writes.append(name)
            if len(writes) == 4: raise OSError('fourth exclusive open failure')
        return original(name, flags, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(os, 'open', fourth)
        with pytest.raises(OSError, match='fourth'): write_sharded_dataset(out, tmp_path / 'build')
    assert len(writes) == 4 and not (tmp_path / 'build').exists()
    stage, = tmp_path.glob('.build.staging-*')
    assert len([p for p in stage.rglob('*') if p.is_file()]) == 3
    write_sharded_dataset(out, tmp_path / 'build')
    assert len([p for p in (tmp_path / 'build').rglob('*') if p.is_file()]) == 22


def test_publication_source_stays_anchored_when_wrapper_name_replaced(inputs, tmp_path, monkeypatch):
    from app.services import rebuild_ml_shards as module
    original = module._publish_no_clobber
    outside = tmp_path / 'outside'; outside.mkdir()
    def swap(source_fd, staging, parent_fd, destination):
        wrapper, = tmp_path.glob('.build.staging-*')
        wrapper.rename(tmp_path / 'retained-wrapper')
        wrapper.symlink_to(outside, target_is_directory=True)
        original(source_fd, staging, parent_fd, destination)
    monkeypatch.setattr(module, '_publish_no_clobber', swap)
    out = build(*inputs)
    write_sharded_dataset(out, tmp_path / 'build')
    assert list(outside.iterdir()) == []
    assert {str(p.relative_to(tmp_path / 'build')): p.read_bytes()
            for p in (tmp_path / 'build').rglob('*') if p.is_file()} == out


@pytest.mark.parametrize('case', ['snapshot_schema', 'snapshot_transaction', 'snapshot_scope',
    'manifest_schema', 'row_authorization', 'value_authorization', 'source_authorization', 'bad_row_mask'])
def test_extended_frozen_contract_rejected_by_all_gates(inputs, tmp_path, case):
    s, p = inputs
    out = build(s, p)
    shard = p['shards']['experimental']; manifest = shard['manifest']
    if case == 'snapshot_schema': s['schema_version'] = 'invalid'
    elif case == 'snapshot_transaction': s['transaction']['read_only'] = 'off'
    elif case == 'snapshot_scope': s['scope_paper_ids'] = []
    elif case == 'manifest_schema': manifest['schema_version'] = 'invalid'
    elif case == 'bad_row_mask': next(iter(manifest['rows'].values()))['eligible_value_mask'] = {}
    else:
        kind = {'row_authorization': 'rows', 'value_authorization': 'values',
                'source_authorization': 'sources'}[case]
        next(iter(manifest[kind].values()))['reviewer_authorization']['authorized'] = False
    refresh(p)
    # Embed the changed inputs and refresh all nested/outer integrity hashes,
    # while retaining the previously eligible output records.
    index = json.loads(out['shard_index.json'])
    for sid, sh in p['shards'].items():
        prefix = f'shards/{sid}/'
        out[prefix + 'snapshot.json'] = encoded(s)
        out[prefix + 'acceptance.json'] = encoded(sh['manifest'])
        info = json.loads(out[prefix + 'build_manifest.json'])
        info['acceptance_sha256'] = digest(sh['manifest'])
        info['files'] = {k[len(prefix):]: hashlib.sha256(v).hexdigest() for k, v in out.items()
                        if k.startswith(prefix) and k[len(prefix):] not in {'build_manifest.json', 'SHA256SUMS'}}
        out[prefix + 'build_manifest.json'] = encoded(info)
        out[prefix + 'SHA256SUMS'] = ''.join(hashlib.sha256(out[prefix + k]).hexdigest() + '  ' + k + '\n'
            for k in sorted(FILES - {'SHA256SUMS'})).encode()
        index['shards'][sid]['input_manifest_sha256'] = digest(sh['manifest'])
        index['shards'][sid]['files'] = {prefix + k: hashlib.sha256(out[prefix + k]).hexdigest() for k in FILES}
    out['shard_index.json'] = encoded(index)
    out['shard_plan.json'] = encoded(p)
    rehash_root(out)
    with pytest.raises(ValueError): build(s, p)
    with pytest.raises(ValueError): verify_sharded_dataset(out)
    with pytest.raises(ValueError): write_sharded_dataset(out, tmp_path / 'rejected')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('post_rename_sync', [1, 2])
def test_post_rename_sync_failure_explicitly_reports_complete_publication(inputs, tmp_path, monkeypatch, post_rename_sync):
    from app.services import rebuild_ml_shards as module
    out = build(*inputs)
    original_publish = module._publish_no_clobber
    original_sync = module.os.fsync
    published = []; syncs = []
    def publish(*args):
        original_publish(*args)
        published.append(True)
    def sync(fd):
        if published:
            syncs.append(fd)
            if len(syncs) == post_rename_sync:
                raise OSError('injected post-rename sync')
        original_sync(fd)
    destination = tmp_path / 'build'
    with monkeypatch.context() as m:
        m.setattr(module, '_publish_no_clobber', publish)
        m.setattr(module.os, 'fsync', sync)
        with pytest.raises(module.PublishedDurabilityError) as caught:
            write_sharded_dataset(out, destination)
    error = caught.value
    assert error.published is True and error.durability_confirmed is False
    assert error.output == str(destination) and isinstance(error.__cause__, OSError)
    readback = {str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
    assert readback == out
    verify_sharded_dataset(readback)
    with pytest.raises(FileExistsError): write_sharded_dataset(out, destination)
