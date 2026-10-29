"""Formal quality projection recognizes verified dataset source identities."""
import hashlib
from types import SimpleNamespace
from sqlalchemy.orm import sessionmaker

from app.db.models import (DatasetFile, DatasetSource, DatasetValueSource,
                           Paper, RebuildDataRow, RebuildDataValue,
                           RebuildValueSource)
from app.services.rebuild_quality import integrity_report
from test_rebuild_mutation_audit import audit_env


def test_quality_projection_uses_dataset_file_without_pdf_page_unknown(setup_test_db, tmp_path):
    content = b'column,target\nsite-a,1.25\n'
    digest = hashlib.sha256(content).hexdigest()
    session_factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False)
    with session_factory() as session:
        paper = Paper(title='Dataset quality fixture', library_name='默认文献库', paper_code='T-Q1', pdf_path='fixture.pdf')
        session.add(paper)
        session.flush()
        dataset = DatasetSource(paper_id=paper.id, dataset_key='U141', version='fixture-v1',
                                title='Source-bound descriptors', metadata_={})
        session.add(dataset)
        session.flush()
        object_key = f'{paper.id}/{digest}'
        path = tmp_path / 'datasets' / object_key
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        file = DatasetFile(dataset_id=dataset.id, paper_id=paper.id, filename='data.csv',
                           mime_type='text/csv', size_bytes=len(content), sha256=digest,
                           object_key=object_key, metadata_={})
        session.add(file)
        session.flush()
        row = RebuildDataRow(paper_id=paper.id, row_key='U141-row-1', reaction='SRR',
                             material='催化剂身份未报告', active_site_type='dual_atom', data_type='dft')
        session.add(row)
        session.flush()
        value = RebuildDataValue(row_id=row.id, paper_id=paper.id, field_name='target',
                                 raw_value='1.25', numeric_value=1.25, unit='eV', value_type='explicit')
        session.add(value)
        session.flush()
        source = RebuildValueSource(value_id=value.id, row_id=row.id, paper_id=paper.id,
                                    source_key='dataset-source-1', source_kind='dataset')
        session.add(source)
        session.flush()
        session.add(DatasetValueSource(source_id=source.id, paper_id=paper.id,
                                       dataset_file_id=file.id,
                                       dataset_locator={'sha256': digest, 'sheet': 'data.csv',
                                                        'row': 2, 'column': 'target'}))
        session.commit()
        report = integrity_report(session, paper.id, SimpleNamespace(storage_root=tmp_path),
                                  file_states={})

    codes = {item['code'] for item in report['issues']}
    assert 'source_file_unknown' not in codes
    assert 'source_physical_page_unknown' not in codes
    assert report['status'] == 'stored_checks_passed'
    assert report['dataset_files'] == [{
        'file_id': str(file.id), 'paper_id': str(paper.id), 'filename': 'data.csv',
        'source_sha256': digest, 'size_bytes': len(content), 'status': 'verified'}]


def test_http_quality_endpoint_does_not_project_dataset_as_unknown(audit_env):
    env = audit_env
    content = b'column,target\nsite-a,1.25\n'
    digest = hashlib.sha256(content).hexdigest()
    with env['factory']() as session:
        dataset = DatasetSource(paper_id=env['paper'], dataset_key='U141-http',
                                version='fixture-v1', title='Source-bound descriptors',
                                metadata_={})
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
        row = RebuildDataRow(paper_id=env['paper'], row_key='U141-http-row', reaction='SRR',
                             material='催化剂身份未报告', active_site_type='dual_atom', data_type='dft')
        session.add(row)
        session.flush()
        value = RebuildDataValue(row_id=row.id, paper_id=env['paper'], field_name='target',
                                 raw_value='1.25', numeric_value=1.25, unit='eV', value_type='explicit')
        session.add(value)
        session.flush()
        source = RebuildValueSource(value_id=value.id, row_id=row.id, paper_id=env['paper'],
                                    source_key='dataset-http-source', source_kind='dataset')
        session.add(source)
        session.flush()
        session.add(DatasetValueSource(source_id=source.id, paper_id=env['paper'],
                                       dataset_file_id=file.id,
                                       dataset_locator={'sha256': digest, 'sheet': 'data.csv',
                                                        'row': 2, 'column': 'target'}))
        bad_value = RebuildDataValue(row_id=row.id, paper_id=env['paper'], field_name='target_bad',
                                     raw_value='2.50', numeric_value=2.5, unit='eV', value_type='explicit')
        session.add(bad_value)
        session.flush()
        bad_source = RebuildValueSource(value_id=bad_value.id, row_id=row.id, paper_id=env['paper'],
                                        source_key='dataset-http-bad-source', source_kind='dataset')
        session.add(bad_source)
        session.flush()
        session.add(DatasetValueSource(source_id=bad_source.id, paper_id=env['paper'],
                                       dataset_file_id=file.id,
                                       dataset_locator={'sha256': digest, 'sheet': 'data.csv',
                                                        'row': -1, 'frame': 0, 'column': 'target'}))
        session.commit()
        source_id = str(source.id)
        bad_source_id = str(bad_source.id)

    response = env['client'].get(
        f"/api/rebuild/papers/{env['paper']}/quality?limit=200")
    assert response.status_code == 200, response.text
    issues = [item for item in response.json()['issues'] if item['id'] == source_id]
    assert not any(item['code'] in {'source_file_unknown', 'source_physical_page_unknown'}
                   for item in issues)
    bad_issues = [item for item in response.json()['issues'] if item['id'] == bad_source_id]
    assert any(item['code'] == 'source_dataset_locator_invalid' for item in bad_issues)
    assert not any(item['code'] in {'source_file_unknown', 'source_physical_page_unknown'}
                   for item in bad_issues)
