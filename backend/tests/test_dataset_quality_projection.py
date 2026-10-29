"""Dataset-source quality projection must verify the existing source identity."""
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.rebuild_quality import (_dataset_file_inventory,
                                         _dataset_source_identity)

pytestmark = pytest.mark.no_test_database


def dataset_fixture(tmp_path, content=b'id,target\n1,2\n'):
    paper_id, file_id = uuid4(), uuid4()
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / 'datasets' / str(paper_id) / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    file = SimpleNamespace(id=file_id, paper_id=paper_id, filename='data.csv',
                           sha256=digest, size_bytes=len(content),
                           object_key=f'{paper_id}/{digest}')
    binding = SimpleNamespace(dataset_file_id=file_id,
                              dataset_locator={'sha256': digest, 'sheet': 'data.csv',
                                               'row': 2, 'column': 'target'})
    source = SimpleNamespace(id=uuid4(), paper_id=paper_id, source_kind='dataset',
                             file_id=None, asset_id=None, page_number=None,
                             dataset_binding=binding)
    settings = SimpleNamespace(storage_root=tmp_path)
    return source, binding, file, settings


def test_verified_dataset_source_uses_dataset_file_identity(tmp_path):
    source, _, file, settings = dataset_fixture(tmp_path)
    physical = _dataset_file_inventory([file], settings)
    assert physical[file.id]['status'] == 'verified'
    selected, error = _dataset_source_identity(source, {file.id: file}, physical)
    assert selected is file and error == (None, None)


@pytest.mark.parametrize('change,code', [
    ('missing_binding', 'source_dataset_binding_missing'),
    ('missing_file', 'source_dataset_file_identity_mismatch'),
    ('hash', 'source_dataset_hash_mismatch'),
    ('column', 'source_dataset_locator_invalid'),
    ('pdf_identity', 'source_dataset_pdf_identity_conflict'),
])
def test_invalid_dataset_source_is_not_reported_as_generic_missing_file(tmp_path, change, code):
    source, binding, file, settings = dataset_fixture(tmp_path)
    files = {file.id: file}
    physical = _dataset_file_inventory([file], settings)
    if change == 'missing_binding':
        source.dataset_binding = None
    elif change == 'missing_file':
        files = {}
    elif change == 'hash':
        binding.dataset_locator = {**binding.dataset_locator, 'sha256': '0' * 64}
    elif change == 'column':
        binding.dataset_locator = {**binding.dataset_locator, 'column': '   '}
    else:
        source.page_number = 1
    _, error = _dataset_source_identity(source, files, physical)
    assert error == ('error', code)


@pytest.mark.parametrize('locator_change', [
    {'row': True, 'frame': 0},
    {'row': 0, 'frame': True},
    {'row': -1, 'frame': 0},
    {'row': 0, 'frame': -1},
    {'row': '2', 'frame': 0},
    {'row': 0, 'frame': 2.0},
    {'row': None, 'frame': None},
    {'column': 'x' * 1001},
    {'sheet': 7},
    {'sheet': 's' * 1001},
])
def test_quality_rejects_every_invalid_locator_field_even_with_another_valid_coordinate(tmp_path, locator_change):
    source, binding, file, settings = dataset_fixture(tmp_path)
    binding.dataset_locator = {**binding.dataset_locator, **locator_change}
    _, error = _dataset_source_identity(
        source, {file.id: file}, _dataset_file_inventory([file], settings))
    assert error == ('error', 'source_dataset_locator_invalid')


def test_quality_rejects_oversize_persisted_locator(tmp_path):
    source, binding, file, settings = dataset_fixture(tmp_path)
    binding.dataset_locator = {**binding.dataset_locator, 'padding': 'é' * 40_000}
    _, error = _dataset_source_identity(
        source, {file.id: file}, _dataset_file_inventory([file], settings))
    assert error == ('error', 'source_dataset_locator_invalid')


def test_dataset_bytes_are_checked_and_changed_object_fails_closed(tmp_path):
    _, _, file, settings = dataset_fixture(tmp_path)
    path = settings.storage_root / 'datasets' / file.object_key
    path.write_bytes(b'id,target\n9,9\n')
    physical = _dataset_file_inventory([file], settings)
    assert physical[file.id]['status'] == 'unknown'
    assert physical[file.id]['reason'] == 'dataset_file_sha256_mismatch'


def test_non_dataset_source_cannot_carry_dataset_binding(tmp_path):
    source, _, file, settings = dataset_fixture(tmp_path)
    source.source_kind = 'text'
    _, error = _dataset_source_identity(source, {file.id: file},
                                        _dataset_file_inventory([file], settings))
    assert error == ('error', 'source_dataset_binding_conflict')
