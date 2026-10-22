"""Synthetic-only B046 geometry. No production files, sessions or data writes."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.merged_cell import MergedCell, validate_cell_location
from app.schemas.rebuild import RebuildSourceIdentity, RebuildValueSourceInput
from app.services.rebuild_merged_cell import validate_source_cell
from app.services.rebuild_workflow_service import _source_key, _validate_value, serialize_source
from app.services.rebuild_table_content import validate_table_provenance, table_content, table_write_contract
from app.services.rebuild_source_correction import source_correction_contract

pytestmark = pytest.mark.no_test_database
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/b046_merged_cells.json').read_text())


def source(target):
    return dict(file_id=FIXTURE['file_id'], asset_id=FIXTURE['asset_id'], source_kind='table',
                page_number=26, label='Synthetic Table S2', table_row=target['table_row'],
                table_column=target['table_column'], quote=None, estimate_basis=None,
                merged_cell=deepcopy(target['merged_cell']))


def asset():
    return SimpleNamespace(asset_type='table', file_id=FIXTURE['file_id'], page_numbers=[26],
                           context_text=None, provenance=dict(table_data=deepcopy(FIXTURE['table_data'])))


def test_exact_six_sources_values_blank_preserved_in_table_and_contracts():
    assert len({t['source_id'] for t in FIXTURE['targets']}) == 6
    assert len({t['value_id'] for t in FIXTURE['targets']}) == 6
    record = asset()
    record.provenance['table_sources'] = []
    for target in FIXTURE['targets']:
        data = source(target)
        assert RebuildSourceIdentity(**data).merged_cell.role == 'continuation'
        assert RebuildValueSourceInput(**data).merged_cell.cell_bbox is None
        validate_source_cell(data, record)
        _validate_value(dict(raw_value=None, numeric_value=None, value_type='missing',
                             missing_reason='Printed merged continuation is blank', sources=[data]))
        record.provenance['table_sources'].append({k:v for k,v in data.items() if k in {
            'file_id', 'page_number', 'table_row', 'table_column', 'quote', 'merged_cell'}} | {'locator':'Synthetic S2'})
    before = deepcopy(record.provenance['table_data'])
    validate_table_provenance(record.provenance, file_id=record.file_id, page_numbers=[26])
    content = table_content(record)
    assert content['data']['rows'] == before['rows']
    assert all(content['data']['rows'][t['table_row']][5] is None for t in FIXTURE['targets'])
    assert content['sources'] == record.provenance['table_sources']
    assert 'merged_cell' in table_write_contract()
    assert 'merged_cell' in source_correction_contract()


@pytest.mark.parametrize('changes', [
    {'row_span':0}, {'row_span':True}, {'row_span':'2'}, {'column_span':0},
    {'column_span':201}, {'anchor_row':-1}, {'coordinate_basis':'one_based'},
    {'cell_content':'printed'}, {'cell_bbox':[0,0,1,1]},
    {'bbox_basis':'source_page_normalized'},
    *[{'cell_bbox':box,'bbox_basis':'source_page_normalized'} for box in
      ([0,0,0,1], [0,1,1,0], [-1,0,1,1], [0,0,2,1], [0,0,float('nan'),1],
       [0,0,float('inf'),1], [0,0,1], [0,0,1,1,1])],
    {'role':'inherited'}, {'unknown':1},
])
def test_invalid_geometry_fails_closed(changes):
    with pytest.raises(ValidationError):
        MergedCell(**(source(FIXTURE['targets'][0])['merged_cell'] | changes))


def test_anchor_continuation_cross_column_and_offset_are_explicit():
    cell = dict(coordinate_basis='zero_based_data_rows', merged_group_id='wide',role='anchor',
                anchor_row=0,anchor_column=1,row_span=2,column_span=2,cell_content='printed',
                cell_bbox=[0.1,0.2,0.8,0.9], bbox_basis='source_page_normalized')
    validate_cell_location(cell,0,1,rows=2,columns=3)
    validate_cell_location(cell | {'role':'continuation','cell_content':'blank'},1,2,rows=2,columns=3)
    for row,column in [(0,2),(1,1),(1,2)]:
        with pytest.raises(ValueError,match='role_coordinate'):
            validate_cell_location(cell,row,column)
    with pytest.raises(ValueError,match='role_coordinate'):
        validate_cell_location(cell | {'role':'continuation','cell_content':'blank'},0,1)
    for row,column in [(2,2),(1,3),(-1,1)]:
        with pytest.raises(ValueError,match='outside_span'):
            validate_cell_location(cell,row,column)
    for bounds in [dict(rows=1,columns=3),dict(rows=2,columns=2)]:
        with pytest.raises(ValueError,match='out_of_range'):
            validate_cell_location(cell,0,1,**bounds)


def test_blank_cannot_be_inherited_or_written_as_scientific_number():
    data = source(FIXTURE['targets'][0])
    for kind in ['explicit','derived','estimated','missing']:
        with pytest.raises(ValueError,match='merged_blank_cell_cannot_supply_value'):
            _validate_value(dict(raw_value='4.774',numeric_value=4.774,value_type=kind,sources=[data]))
    record = asset()
    record.provenance['table_data']['rows'][data['table_row']][5] = '4.774'
    with pytest.raises(ValueError,match='blank_cell_contains_value'):
        validate_source_cell(data,record)
    with pytest.raises(ValueError,match='structured_table_bounds'):
        validate_source_cell(data,SimpleNamespace(asset_type='table',provenance={}))
    for changes in [dict(table_row=17),dict(table_row=data['merged_cell']['anchor_row']),dict(source_kind='text')]:
        with pytest.raises(ValueError):
            RebuildValueSourceInput(**(data | changes))
    with pytest.raises(ValueError,match='row_out_of_range'):
        validate_source_cell(data | {'merged_cell':data['merged_cell'] | {'row_span':100}},asset())


def test_missing_legacy_metadata_is_not_inferred_and_keys_remain_compatible(monkeypatch):
    data = source(FIXTURE['targets'][0]); data.pop('merged_cell')
    legacy = RebuildSourceIdentity(**data).model_dump(mode='json')
    assert legacy['merged_cell'] is None
    assert _source_key(data) == _source_key(data | {'merged_cell':None})
    assert _source_key(source(FIXTURE['targets'][0])) != _source_key(data)
    obj = SimpleNamespace(**data,id=uuid4(),source_key=_source_key(data),value_id=uuid4())
    monkeypatch.setattr('app.services.rebuild_file_preview.stored_source_identity',lambda _: {})
    monkeypatch.setattr('app.services.rebuild_merged_cell.stored_merged_cell',lambda _: None)
    result=serialize_source(obj)
    assert 'merged_cell' not in result
    assert result['table_row'] == data['table_row']
    monkeypatch.setattr('app.services.rebuild_merged_cell.stored_merged_cell',lambda _: FIXTURE['targets'][0]['merged_cell'])
    assert serialize_source(obj)['merged_cell'] == FIXTURE['targets'][0]['merged_cell']


def test_table_group_geometry_disagreement_rejected():
    record=asset(); target=FIXTURE['targets'][0]; data=source(target)
    base={k:v for k,v in data.items() if k in {'file_id','page_number','table_row','table_column','merged_cell'}} | {'locator':'synthetic'}
    record.provenance['table_sources']=[base,deepcopy(base)]
    record.provenance['table_sources'][1]['merged_cell']['row_span']=3
    with pytest.raises(ValueError,match='group_geometry_mismatch'):
        validate_table_provenance(record.provenance,file_id=record.file_id,page_numbers=[26])


@pytest.mark.parametrize('row,column', [('10',5),(10,'5'),(True,5),(10,False)])
def test_merged_scalar_coordinates_reject_coercion(row,column):
    with pytest.raises(ValidationError):
        RebuildValueSourceInput(**(source(FIXTURE['targets'][0]) | {'table_row':row,'table_column':column}))


def test_one_row_offset_is_rejected_without_normalizing_to_anchor():
    data=source(FIXTURE['targets'][0])
    assert data['table_row']==10 and data['merged_cell']['anchor_row']==9
    with pytest.raises(ValidationError,match='outside_span'):
        RebuildSourceIdentity(**(data | {'table_row':11}))
    with pytest.raises(ValidationError):
        MergedCell(**(data['merged_cell'] | {'cell_bbox':[False,0,1,1],'bbox_basis':'source_page_normalized'}))
