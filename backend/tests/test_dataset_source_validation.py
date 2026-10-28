"""Dataset provenance must be scoped and retain stable legacy identities."""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.schemas.rebuild import RebuildValueSourceInput
from app.services.rebuild_workflow_service import _source_key, _validate_source_for_paper, RebuildWorkflowError

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
        _validate_source_for_paper(session, paper, {**source, 'dataset_locator':{**source['dataset_locator'], 'sha256':'b'*64}})
    for bad in (-1, True, '2'):
        with pytest.raises(RebuildWorkflowError, match='invalid_dataset_coordinate'):
            _validate_source_for_paper(session, paper, {**source, 'dataset_locator':{**source['dataset_locator'], 'row':bad}})
    with pytest.raises(RebuildWorkflowError, match='invalid_dataset_column'):
        _validate_source_for_paper(session, paper, {**source, 'dataset_locator':{**source['dataset_locator'], 'column':'   '}})
