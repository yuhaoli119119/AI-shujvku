"""Exercise the real mapping loader and projection, without database writes."""
from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.analysis_readonly import AnalysisReadonlyService

pytestmark = pytest.mark.no_test_database

DEDICATED = [
    ('li2s_adsorption_energy', 'li2s_adsorption_energy', 'eV'),
    ('li2s_dissociation_barrier', 'li2s_dissociation_barrier', 'eV'),
    ('li2s_bader_charge_transfer', 'li2s_bader_charge_transfer', 'e'),
    ('li1_s_bond_length', 'li1_s_bond_length', 'Å'),
    ('li2_s_bond_length', 'li2_s_bond_length', 'Å'),
    ('adsorption_energy_li2s_ev', 'li2s_adsorption_energy', 'eV'),
]


@pytest.fixture
def loaded_service():
    def load(field, unit, adsorbate, location='properties'):
        pid, rid = uuid4(), uuid4()
        paper = dict(id=pid, library_name='synthetic', paper_code='synthetic',
                     title='Synthetic loader evidence', doi=None, journal=None, year=None)
        row = dict(id=rid, paper_id=pid, configuration='synthetic', properties={}, condition={},
                   reaction='SRR', material='synthetic', active_site_type='dual_atom',
                   active_site='synthetic', support='synthetic', material_family=None, data_type='dft')
        if location != 'cell' and adsorbate is not None:
            row[location]['adsorbate'] = adsorbate
        values, sources = [], []
        def add(name, number, raw, value_unit):
            vid = uuid4()
            values.append(dict(id=vid, row_id=rid, paper_id=pid, field_name=name,
                numeric_value=number, raw_value=raw, unit=value_unit, value_type='explicit',
                is_estimated=False, conflict=None, missing_reason=None))
            sources.append(dict(id=uuid4(), row_id=rid, value_id=vid, paper_id=pid,
                source_kind='table', page_number=3, quote='synthetic original '+str(raw)))
        add(field, .5, '0.500', unit)
        add('d_band_center_ev', -1., '-1.00', 'eV')
        if location == 'cell' and adsorbate is not None:
            add('adsorbate', None, adsorbate, None)
        before = deepcopy((paper, row, values, sources))
        session = MagicMock()
        # Current _papers and _read_rows use Core mappings, not scalars/ORM.
        results = []
        for records in ([paper], [row], values, sources):
            result = MagicMock(); result.mappings.return_value = records; results.append(result)
        session.execute.side_effect = results
        service = AnalysisReadonlyService(session)
        service._read_rows()
        assert session.execute.call_count == 4
        assert (paper, row, values, sources) == before
        session.commit.assert_not_called()
        return service, values[0], sources[0]
    return load


@pytest.mark.parametrize('field,projected,unit', DEDICATED)
@pytest.mark.parametrize('location', ['properties', 'condition', 'cell'])
def test_loader_keeps_contradictory_object_and_analysis_excludes_it(loaded_service, field, projected, unit, location):
    service, value, source = loaded_service(field, unit, 'Li2S4', location)
    original = next(r for r in service.records if r['field_name'] == field)
    assert original['adsorbate'] == 'Li2S4'
    result = service._rebuild_fields()[original['rebuild_row_id']][projected]
    assert not result['exploratory_eligible']
    assert result['exclusion_reason'] == 'field_adsorbate_conflict'
    assert result['raw_value'] == value['raw_value'] == '0.500'
    assert result['raw_evidence'][0]['adsorbate'] == 'Li2S4'
    assert result['raw_evidence'][0]['sources'][0]['id'] == source['id']
    correlation = service.correlation(x_field=projected, y_field='d_band_center')
    assert correlation['points'] == []
    assert correlation['excluded_reasons']['field_adsorbate_conflict'] == 1


@pytest.mark.parametrize('field,projected,unit', DEDICATED)
@pytest.mark.parametrize('adsorbate', ['*Li₂S', None, 'unknown'])
def test_loader_preserves_matching_missing_unknown_objects_without_field_inference(loaded_service, field, projected, unit, adsorbate):
    service, _, _ = loaded_service(field, unit, adsorbate)
    original = next(r for r in service.records if r['field_name'] == field)
    assert original['adsorbate'] == adsorbate
    result = service._rebuild_fields()[original['rebuild_row_id']][projected]
    assert result['adsorbate'] == adsorbate and result['exploratory_eligible']
    correlation = service.correlation(x_field=projected, y_field='d_band_center')
    assert len(correlation['points']) == 1
    assert correlation['points'][0]['semantic_context']['x_adsorbate'] == adsorbate
