"""Dataset write pause preserves all reads and rejects only affected writes."""
import copy
import hashlib

from sqlalchemy import func, select

from app.db.models import (DatasetFile, DatasetSource, DatasetValueSource,
                           RebuildDataRow, RebuildDataValue, RebuildValueSource)
from test_rebuild_mutation_audit import audit_env, row_body


def existing_dataset(env):
    content = b'column,target\nsite-a,1.25\n'
    digest = hashlib.sha256(content).hexdigest()
    with env['factory']() as session:
        dataset = DatasetSource(paper_id=env['paper'], dataset_key='pause-readback',
                                version='fixture-v1', title='Read-only dataset', metadata_={})
        session.add(dataset)
        session.flush()
        object_key = f"{env['paper']}/{digest}"
        path = env['settings'].storage_root / 'datasets' / object_key
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        file = DatasetFile(dataset_id=dataset.id, paper_id=env['paper'], filename='data.csv',
                           mime_type='text/csv', size_bytes=len(content), sha256=digest,
                           object_key=object_key, metadata_={})
        session.add(file)
        session.flush()
        row = RebuildDataRow(paper_id=env['paper'], row_key='dataset-read-row', reaction='SRR',
                             material='catalyst', active_site_type='dual_atom', data_type='dft')
        session.add(row)
        session.flush()
        value = RebuildDataValue(row_id=row.id, paper_id=env['paper'], field_name='target',
                                 raw_value='1.25', numeric_value=1.25, unit='eV', value_type='explicit')
        session.add(value)
        session.flush()
        locator = {'sha256': digest, 'sheet': 'data.csv', 'row': 2, 'column': 'target'}
        source = RebuildValueSource(value_id=value.id, row_id=row.id, paper_id=env['paper'],
                                    source_key='dataset-pause-source', source_kind='dataset')
        session.add(source)
        session.flush()
        session.add(DatasetValueSource(source_id=source.id, paper_id=env['paper'],
                                       dataset_file_id=file.id, dataset_locator=locator))
        session.commit()
        return {'dataset_id': str(dataset.id), 'file_id': str(file.id),
                'digest': digest, 'content': content, 'locator': locator}


def counts(env):
    with env['factory']() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (DatasetSource, DatasetFile, DatasetValueSource,
                                   RebuildDataRow, RebuildDataValue, RebuildValueSource))


def test_dataset_write_pause_keeps_lists_downloads_and_exports_readable(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    before = counts(env)
    base = f"/api/papers/{env['paper']}/datasets"

    register = env['client'].post(base, json={'dataset_key': 'blocked', 'version': 'v1',
                                               'title': 'Blocked', 'metadata': {}})
    assert register.status_code == 503
    assert register.json()['detail'] == 'dataset_writes_temporarily_disabled'

    new_content = b'column,target\nsite-b,2.5\n'
    new_digest = hashlib.sha256(new_content).hexdigest()
    upload = env['client'].put(
        f"{base}/{fixture['dataset_id']}/files/{new_digest}", content=new_content,
        params={'filename': 'new.csv', 'size_bytes': len(new_content),
                'mime_type': 'text/csv'})
    assert upload.status_code == 503
    assert upload.json()['detail'] == 'dataset_writes_temporarily_disabled'
    assert counts(env) == before

    listing = env['client'].get(base)
    assert listing.status_code == 200, listing.text
    assert [item['id'] for item in listing.json()] == [fixture['dataset_id']]
    download = env['client'].get(
        f"{base}/{fixture['dataset_id']}/files/{fixture['file_id']}")
    assert download.status_code == 200 and download.content == fixture['content']
    exported = env['client'].get(f"{base}/{fixture['dataset_id']}/rows.csv")
    assert exported.status_code == 200, exported.text
    assert exported.text.startswith('row_key,material,field_name,raw_value,unit')
    assert 'dataset-read-row' in exported.text and fixture['digest'] in exported.text
    assert counts(env) == before


def test_dataset_write_pause_preflight_and_import_reject_only_affected_row(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    payload = row_body(env)
    normal = copy.deepcopy(payload['rows'][0])
    normal['row_key'] = 'normal-row'
    normal['material'] = 'normal-material'
    blocked = copy.deepcopy(normal)
    blocked['row_key'] = 'blocked-dataset-row'
    blocked['material'] = 'blocked-material'
    blocked['values'][0]['sources'] = [{
        'source_kind': 'dataset', 'dataset_file_id': fixture['file_id'],
        'dataset_locator': fixture['locator']}]
    payload['rows'] = [normal, blocked]
    before = counts(env)

    preflight = env['client'].post('/api/rebuild/rows/import/preflight', json=payload)
    assert preflight.status_code == 200, preflight.text
    preflight_errors = preflight.json()['errors']
    assert any(item.get('row_key') == 'blocked-dataset-row' and
               item.get('error') == 'dataset_writes_temporarily_disabled'
               for item in preflight_errors)
    assert not any(item.get('row_key') == 'normal-row' and item.get('outcome') == 'rejected'
                   for item in preflight_errors)
    assert counts(env) == before

    imported = env['client'].post('/api/rebuild/rows/import', json=payload)
    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert result['summary']['outcome'] == 'partial_success'
    assert result['summary']['rows']['persisted'] == 1
    assert result['summary']['rows']['not_persisted'] == 1
    assert any(item.get('row_key') == 'blocked-dataset-row' and
               item.get('error') == 'dataset_writes_temporarily_disabled'
               for item in result['errors'])
    with env['factory']() as session:
        assert session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.row_key == 'blocked-dataset-row')) is None
        assert session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.row_key == 'normal-row')) is not None
