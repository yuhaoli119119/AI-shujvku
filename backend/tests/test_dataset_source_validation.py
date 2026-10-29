"""Dataset provenance must be scoped and retain stable legacy identities."""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.schemas.rebuild import RebuildValueSourceInput
from app.services.rebuild_workflow_service import (_source_key, _validate_source_for_paper,
                                                   RebuildWorkflowError, validate_dataset_locator)

pytestmark = pytest.mark.no_test_database


def test_legacy_source_key_is_unchanged():
    data = {'source_kind': 'table', 'file_id': None, 'asset_id': None, 'page_number': 3,
            'label': 'Table S1', 'table_row': 2, 'table_column': 1, 'quote': None, 'estimate_basis': None}
    identity = {**data, 'file_id': '', 'asset_id': ''}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert _source_key(data) == 'source_' + hashlib.sha256(('source|' + encoded).encode()).hexdigest()[:32]


def test_dataset_provenance_validation():
    paper, other, file_id = uuid4(), uuid4(), uuid4()
    record = SimpleNamespace(id=file_id, paper_id=paper, sha256='a' * 64)
    session = SimpleNamespace(get=lambda cls, ident: record if ident == file_id else None)
    source = RebuildValueSourceInput(source_kind='dataset', dataset_file_id=file_id,
        dataset_locator={'sha256':'a'*64, 'sheet':'data.csv', 'row':2, 'column':'target'}).model_dump()
    _validate_source_for_paper(session, paper, source)
    with pytest.raises(RebuildWorkflowError, match='other_paper'):
        _validate_source_for_paper(session, other, source)
    with pytest.raises(RebuildWorkflowError, match='invalid_dataset_source'):
        _validate_source_for_paper(session, paper, {
            **source, 'dataset_locator': {**source['dataset_locator'], 'sha256': 'b' * 64}})


@pytest.mark.parametrize('locator', [
    {'sha256': 'a' * 64, 'column': 'target', 'row': 0},
    {'sha256': 'a' * 64, 'column': 'target', 'frame': 0},
    {'sha256': 'a' * 64, 'column': 'target', 'row': 0, 'frame': 0},
    {'sha256': 'a' * 64, 'column': 'x' * 1000, 'frame': 3, 'sheet': 's' * 1000},
])
def test_valid_locator_shapes_share_the_write_contract(locator):
    validate_dataset_locator(locator)


@pytest.mark.parametrize('locator_change,code', [
    ({'row': True, 'frame': 0}, 'invalid_dataset_coordinate'),
    ({'row': 0, 'frame': True}, 'invalid_dataset_coordinate'),
    ({'row': -1, 'frame': 0}, 'invalid_dataset_coordinate'),
    ({'row': 0, 'frame': -1}, 'invalid_dataset_coordinate'),
    ({'row': '2', 'frame': 0}, 'invalid_dataset_coordinate'),
    ({'row': 0, 'frame': 2.0}, 'invalid_dataset_coordinate'),
    ({'row': None, 'frame': None}, 'invalid_dataset_coordinate'),
    ({'column': '   '}, 'invalid_dataset_column'),
    ({'column': 'x' * 1001}, 'invalid_dataset_column'),
    ({'sheet': 7}, 'invalid_dataset_sheet'),
    ({'sheet': 's' * 1001}, 'invalid_dataset_sheet'),
])
def test_every_invalid_locator_field_is_rejected(locator_change, code):
    locator = {'sha256': 'a' * 64, 'column': 'target', 'row': 0, **locator_change}
    with pytest.raises(RebuildWorkflowError, match=code):
        validate_dataset_locator(locator)


def test_locator_canonical_utf8_size_boundary():
    locator = {'sha256': 'a' * 64, 'column': 'target', 'frame': 0,
               'sheet': 'data.csv', 'padding': ''}
    base_size = len(json.dumps(locator, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')).encode('utf-8'))
    remaining = 65536 - base_size
    locator['padding'] = 'é' * (remaining // 2) + 'x' * (remaining % 2)
    assert len(json.dumps(locator, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')) == 65536
    validate_dataset_locator(locator)
    locator['padding'] += 'x'
    with pytest.raises(RebuildWorkflowError, match='dataset_locator_too_large'):
        validate_dataset_locator(locator)
