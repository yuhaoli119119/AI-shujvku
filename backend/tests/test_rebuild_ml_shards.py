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
