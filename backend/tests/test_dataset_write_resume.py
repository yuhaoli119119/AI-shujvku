"""Dataset writes resume without weakening per-paper scientific writer locks."""
import asyncio
import copy
import hashlib

import pytest
from sqlalchemy import func, select

from app.db.models import (DatasetFile, DatasetSource, DatasetValueSource,
                           RebuildDataRow, RebuildDataValue, RebuildValueSource,
                           WorkflowJob)
from app.schemas.rebuild import RebuildDataRowInput
from app.services.paper_db_phase import LockConflictError, run_phase
from app.services.rebuild_workflow_service import import_data_row
from test_rebuild_mutation_audit import audit_env, row_body
from test_sync_write_lock_contract import PaperLock


def existing_dataset(env):
    content = b'column,target\nsite-a,1.25\n'
    digest = hashlib.sha256(content).hexdigest()
    with env['factory']() as session:
        dataset = DatasetSource(paper_id=env['paper'], dataset_key='resume-readback',
                                version='fixture-v1', title='Readback dataset', metadata_={})
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
                                    source_key='dataset-resume-source', source_kind='dataset')
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


def dataset_row(env, fixture, row_key='dataset-write-row'):
    payload = row_body(env)
    row = payload['rows'][0]
    row['row_key'] = row_key
    row['material'] = row_key
    row['values'][0]['sources'] = [{
        'source_kind': 'dataset', 'dataset_file_id': fixture['file_id'],
        'dataset_locator': fixture['locator']}]
    payload['rows'] = [row]
    return payload


def test_dataset_registration_upload_and_existing_reads_are_available(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    base = f"/api/papers/{env['paper']}/datasets"
    before = counts(env)

    registered = env['client'].post(base, json={
        'dataset_key': 'resumed', 'version': 'v1',
        'title': 'Resumed dataset', 'metadata': {'license': 'fixture'}})
    assert registered.status_code == 201, registered.text
    new_dataset_id = registered.json()['id']
    new_content = b'column,target\nsite-b,2.5\n'
    new_digest = hashlib.sha256(new_content).hexdigest()
    uploaded = env['client'].put(
        f"{base}/{new_dataset_id}/files/{new_digest}", content=new_content,
        params={'filename': 'new.csv', 'size_bytes': len(new_content),
                'mime_type': 'text/csv'})
    assert uploaded.status_code == 200, uploaded.text
    new_file_id = uploaded.json()['id']
    assert counts(env)[:2] == (before[0] + 1, before[1] + 1)

    listing = env['client'].get(base)
    assert listing.status_code == 200, listing.text
    assert {item['id'] for item in listing.json()} == {fixture['dataset_id'], new_dataset_id}
    old_download = env['client'].get(f"{base}/{fixture['dataset_id']}/files/{fixture['file_id']}")
    new_download = env['client'].get(f"{base}/{new_dataset_id}/files/{new_file_id}")
    assert old_download.status_code == 200 and old_download.content == fixture['content']
    assert new_download.status_code == 200 and new_download.content == new_content
    exported = env['client'].get(f"{base}/{fixture['dataset_id']}/rows.csv")
    assert exported.status_code == 200, exported.text
    assert 'dataset-read-row' in exported.text and fixture['digest'] in exported.text


def test_preflight_and_row_import_persist_dataset_source_during_active_parse(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    with env['factory']() as session:
        session.add(WorkflowJob(job_id='active-parser', type='ai_extract_rebuild',
                                status='running', payload={'paper_id': str(env['paper'])}))
        session.commit()
    payload = row_body(env)
    normal = copy.deepcopy(payload['rows'][0])
    normal['row_key'] = 'normal-row'
    normal['material'] = 'normal-material'
    dataset = dataset_row(env, fixture)['rows'][0]
    payload['rows'] = [normal, dataset]
    before = counts(env)

    preflight = env['client'].post('/api/rebuild/rows/import/preflight', json=payload)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()['summary']['rows']['would_persist'] == 2
    assert not preflight.json()['errors']
    assert counts(env) == before

    imported = env['client'].post('/api/rebuild/rows/import', json=payload)
    assert imported.status_code == 200, imported.text
    result = imported.json()
    assert result['summary']['outcome'] == 'all_success'
    assert result['summary']['rows']['persisted'] == 2
    with env['factory']() as session:
        row = session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.paper_id == env['paper'],
            RebuildDataRow.row_key == 'dataset-write-row'))
        assert row is not None
        value = session.scalar(select(RebuildDataValue).where(
            RebuildDataValue.row_id == row.id,
            RebuildDataValue.field_name == 'adsorption_energy_ev'))
        source = session.scalar(select(RebuildValueSource).where(
            RebuildValueSource.value_id == value.id, RebuildValueSource.source_kind == 'dataset'))
        binding = session.get(DatasetValueSource, source.id)
        assert binding is not None and str(binding.dataset_file_id) == fixture['file_id']
        assert session.get(WorkflowJob, 'active-parser').status == 'running'


def test_same_paper_writer_lock_rejects_concurrent_dataset_import_without_partial_write(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    payload = dataset_row(env, fixture, row_key='locked-dataset-row')
    before = counts(env)

    base = f"/api/papers/{env['paper']}/datasets"
    body = RebuildDataRowInput.model_validate(payload['rows'][0])
    with PaperLock(env['engine'], env['paper']), env['factory']() as session:
        assert env['client'].get(base).status_code == 200
        download = env['client'].get(
            f"{base}/{fixture['dataset_id']}/files/{fixture['file_id']}")
        assert download.status_code == 200 and download.content == fixture['content']
        assert env['client'].get(
            f"{base}/{fixture['dataset_id']}/rows.csv").status_code == 200
        with pytest.raises(LockConflictError, match='paper_lock_busy_retry_same_request'):
            asyncio.run(run_phase(
                session, lambda worker: import_data_row(worker, env['paper'], body)))
    assert counts(env) == before
    with env['factory']() as session:
        assert session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.row_key == 'locked-dataset-row')) is None
