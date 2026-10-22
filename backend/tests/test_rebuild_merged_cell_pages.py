"""Cross-page rejection on real isolated PostgreSQL; no production connection."""
import hashlib
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import RebuildDataValue, RebuildPaperFile, RebuildValueSource, RebuildVisualAsset
from app.schemas.rebuild import (RebuildDataRowInput, RebuildSourceAssetBindingRequest,
                                RebuildSourceCorrectionRequest, RebuildValueCorrectionRequest)
from app.services.rebuild_source_correction import bind_source_asset, correct_source, source_state
from app.services.rebuild_workflow_service import (RebuildWorkflowError, import_data_row,
                                                 update_data_value, value_revision)
from test_rebuild_source_correction import fixture, body, snapshot

CELL = dict(coordinate_basis='zero_based_data_rows', merged_group_id='page-test',
            role='anchor', anchor_row=0, anchor_column=1, row_span=2, column_span=1,
            cell_content='printed')


@pytest.fixture
def prepared(fixture):
    factory, settings, ids = fixture
    with factory() as s:
        s.get(RebuildVisualAsset, ids.assets[0]).provenance = {
            'table_data': {'columns': ['shell', 'energy'], 'rows': [['A', '4.774'], ['B', None]]}}
        s.commit()
    return factory, settings, ids


@pytest.fixture
def http(prepared, monkeypatch):
    from app.api import rebuild
    from app.db.session import get_db_session
    factory, settings, ids = prepared
    app = FastAPI()
    app.include_router(rebuild.router, prefix='/api/rebuild')
    def db():
        with factory() as s:
            yield s
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_settings] = lambda: settings
    async def logged(*args, **kwargs):
        return SimpleNamespace(username='synthetic-page-test', csrf='synthetic-csrf')
    monkeypatch.setattr(rebuild, 'require_session', logged)
    with TestClient(app) as client:
        client.cookies.set('litai_csrf', 'synthetic-csrf')
        client.headers['X-CSRF-Token'] = 'synthetic-csrf'
        yield client, factory, settings, ids


@pytest.mark.parametrize('page', [1, None])
def test_put_cross_page_has_no_source_value_or_history_write(http, page):
    client, factory, _, ids = http
    with factory() as s:
        req = body(s, ids, merged_cell=CELL, page_number=page)
        before = snapshot(s)
    response = client.put('/api/rebuild/sources/' + str(ids.source), json=req)
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == 'merged_source_page_outside_asset'
    with factory() as s:
        assert snapshot(s) == before


@pytest.mark.parametrize('operation', ['import', 'value_update', 'binding', 'correction'])
@pytest.mark.parametrize('page', [1, None])
def test_service_cross_page_has_no_source_value_or_history_write(prepared, operation, page):
    factory, settings, ids = prepared
    with factory() as s:
        if operation == 'binding':
            src = s.get(RebuildValueSource, ids.source)
            src.page_number, src.table_row, src.table_column = page, 0, 1
            value = s.get(RebuildDataValue, ids.value)
            value.conflict = {**value.conflict, 'source_merged_cells': {str(src.id): CELL}}
            s.commit()
        before = snapshot(s)
        source = body(s, ids, merged_cell=CELL, page_number=page)['source']
        if operation == 'import':
            result = import_data_row(s, ids.paper, RebuildDataRowInput(
                material='Synthetic cross page', reaction='HER', data_type='dft', active_site_type='single_atom',
                values=[dict(field_name='energy', raw_value='4.774', numeric_value=4.774, unit='eV', sources=[source])]))
            assert result['complete'] is False
            assert result['values'][0]['persisted'] is False
            assert result['values'][0]['error'] == 'merged_source_page_outside_asset'
        else:
            with pytest.raises(RebuildWorkflowError, match='^merged_source_page_outside_asset$'):
                if operation == 'value_update':
                    value = s.get(RebuildDataValue, ids.value)
                    update_data_value(s, value_id=value.id, payload=RebuildValueCorrectionRequest(
                        expected_revision=value_revision(value), request_id=uuid4(),
                        correction_reason='Synthetic cross-page rejection', raw_value='4.774',
                        numeric_value=4.774, unit='eV', value_type='explicit', source=source))
                elif operation == 'binding':
                    bind_source_asset(s, ids.source, RebuildSourceAssetBindingRequest(
                        paper_id=ids.paper, expected_revision=source_state(s, ids.source)['source_revision'],
                        request_id=uuid4(), target_asset_id=ids.assets[0], reason='Synthetic page mismatch'), actor='tester')
                else:
                    correct_source(s, ids.source, RebuildSourceCorrectionRequest(
                        **body(s, ids, merged_cell=CELL, page_number=page)), settings, actor='tester')
        # Commit even after service rejection: rollback must not hide mutations.
        assert snapshot(s) == before
        s.commit()
    with factory() as s:
        assert snapshot(s) == before


@pytest.mark.parametrize('pages', [[2], [1, 2]])
def test_member_page_correction_persists_and_replays(prepared, pages):
    factory, settings, ids = prepared
    with factory() as s:
        s.get(RebuildVisualAsset, ids.assets[0]).page_numbers = pages
        s.commit()
        req = RebuildSourceCorrectionRequest(**body(s, ids, merged_cell=CELL, page_number=2))
        state = correct_source(s, ids.source, req, settings, actor='tester')
        s.commit()
    with factory() as s:
        assert source_state(s, ids.source) == state
        assert state['page_number'] == 2 and state['merged_cell']['role'] == 'anchor'
        assert correct_source(s, ids.source, req, settings, actor='tester') == state


@pytest.mark.parametrize('operation', ['correction', 'import', 'value_update', 'binding'])
def test_registered_ooxml_null_page_merged_source_is_preserved(prepared, operation):
    factory, settings, ids = prepared
    path = settings.storage_root / 'merged.docx'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc/></w:tr></w:tbl></w:body></w:document>')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    locator = 'word/document.xml#body-child[1]/w:tbl[1]'
    with factory() as s:
        file = s.get(RebuildPaperFile, ids.files[0])
        file.storage_path, file.original_filename = path.name, path.name
        file.sha256, file.file_size = sha, path.stat().st_size
        asset = s.get(RebuildVisualAsset, ids.assets[0])
        asset.page_numbers = []
        asset.provenance = {**asset.provenance, 'page_identity': {
            'page_basis': 'ooxml', 'source_sha256': sha, 'ooxml_locator': locator}}
        s.commit()
        req = RebuildSourceCorrectionRequest(**body(s, ids, merged_cell=CELL, page_number=None,
            page_basis='ooxml', source_sha256=sha, reading_pdf_sha256=None, ooxml_locator=locator))
        if operation == 'correction':
            result = correct_source(s, ids.source, req, settings, actor='tester')
        elif operation == 'import':
            imported = import_data_row(s, ids.paper, RebuildDataRowInput(
                material='Synthetic OOXML', reaction='HER', data_type='dft', active_site_type='single_atom',
                values=[dict(field_name='energy', raw_value='4.774', numeric_value=4.774, unit='eV',
                             sources=[req.source.model_dump()])]))
            assert imported['complete'] and imported['values'][0]['persisted']
            s.flush()
            from sqlalchemy import select
            ids.source = s.scalar(select(RebuildValueSource.id).where(RebuildValueSource.asset_id == asset.id))
            result = source_state(s, ids.source)
        elif operation == 'value_update':
            value = s.get(RebuildDataValue, ids.value)
            update_data_value(s, value_id=value.id, payload=RebuildValueCorrectionRequest(
                expected_revision=value_revision(value), request_id=uuid4(), correction_reason='Synthetic OOXML',
                raw_value='4.774', numeric_value=4.774, unit='eV', value_type='explicit', source=req.source.model_dump()))
            ids.source = value.preferred_source_id
            result = source_state(s, ids.source)
        else:
            correct_source(s, ids.source, req, settings, actor='tester')
            s.commit()
            result = bind_source_asset(s, ids.source, RebuildSourceAssetBindingRequest(
                paper_id=ids.paper, expected_revision=source_state(s, ids.source)['source_revision'],
                request_id=uuid4(), target_asset_id=asset.id, reason='Synthetic OOXML binding'), actor='tester')
        s.commit()
    with factory() as s:
        stored = source_state(s, ids.source)
        assert stored == result and stored['page_number'] is None
        assert stored['page_basis'] == 'ooxml' and stored['ooxml_locator'] == locator
        if operation == 'correction':
            assert correct_source(s, ids.source, req, settings, actor='tester') == stored


@pytest.mark.no_test_database
@pytest.mark.parametrize('pages,page,source_basis,asset_basis,error', [
    ([2], 2, 'ooxml', None, 'ooxml_has_no_physical_page'),
    ([2], 2, 'source_pdf', 'ooxml', 'ooxml_has_no_physical_page'),
    ([], None, None, None, 'merged_source_requires_asset_page_or_ooxml_identity'),
    ([], None, 'ooxml', None, 'merged_source_requires_asset_page_or_ooxml_identity'),
    ([], 1, 'ooxml', 'ooxml', 'merged_source_requires_asset_page_or_ooxml_identity'),
])
def test_page_identity_cannot_bypass_membership(pages, page, source_basis, asset_basis, error):
    from app.services.rebuild_merged_cell import validate_source_cell
    asset = SimpleNamespace(asset_type='table', page_numbers=pages,
                            provenance={'page_identity': {'page_basis': asset_basis},
                                        'table_data': {'columns': ['energy'], 'rows': [['4.774'], [None]]}})
    with pytest.raises(ValueError, match='^' + error + '$'):
        validate_source_cell(dict(source_kind='table', merged_cell=CELL,
                                  page_number=page, page_basis=source_basis), asset)
