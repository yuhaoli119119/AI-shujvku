from unittest.mock import MagicMock

import pytest

from app.services.analysis_readonly import AnalysisReadonlyService
from app.services.rebuild_analysis_fields import project_rows, source_ids

pytestmark = pytest.mark.no_test_database


def cell(field, value, *, row='one', adsorbate='Li2S', unit='eV', reason=None):
    return dict(record_id=row + ':' + field, rebuild_row_id=row, field_name=field,
                adsorbate=adsorbate, target=dict(normalized_value=value, normalized_unit=unit),
                exploratory_eligible=reason is None, exclusion_reason=reason,
                analysis_context={}, catalyst_name='Fe-N4', paper_id='paper',
                raw_value=value, raw_unit=unit)


def test_physical_aliases_translate_and_model_results_are_not_descriptors():
    svc = AnalysisReadonlyService(MagicMock())
    svc.records = [cell('adsorption_energy_li2s_ev', -2), cell('d_band_center_ev', -1),
                   cell('feat_importance_chi_pct', 20, unit='%'),
                   cell('ml_predicted_adsorption_energy_li2s_ev', -3)]
    svc._loaded = ([], {}, {'total_dft_rows':len(svc.records)})
    fields = {f['key']: f for f in svc.field_registry()}
    assert fields['li2s_adsorption_energy']['label_zh'] == 'Li₂S 吸附能'
    assert fields['d_band_center']['available_count'] == 1
    assert len(fields) == 6  # All four presets remain selectable, even if empty.
    assert not any('feat_importance' in f or 'predicted' in f for f in fields)
    svc.session.commit.assert_not_called()


def test_bader_atomic_charge_is_not_charge_transfer_and_species_are_separate():
    rows = project_rows([cell('Li1_bader_net_charge', .8, unit='e'),
                         cell('bader_charge_e', .8, unit='e'),
                         cell('bader_charge_transfer_e', -.4, adsorbate='*Li2S2', unit='e')])
    assert 'li2s_bader_charge_transfer' not in rows['one']
    rows = project_rows([cell('bader_charge_transfer_e', -.4, adsorbate='*Li2S', unit='e')])
    assert rows['one']['li2s_bader_charge_transfer']['target']['normalized_value'] == -.4


def test_maximum_preserves_both_sources_and_requires_same_row():
    a = cell('li1_s_bond_length_angstrom', 2.3, unit='Å')
    b = cell('li2_s_bond_length_angstrom', 2.6, unit='Å')
    maximum = project_rows([a, b])['one']['li_s_bond_max']
    assert maximum['target']['normalized_value'] == 2.6
    assert source_ids(maximum) == [a['record_id'], b['record_id']]
    assert 'same rebuild row' in maximum['derivation']
    b['rebuild_row_id'] = 'two'
    assert all('li_s_bond_max' not in row for row in project_rows([a, b]).values())
    assert 'li_s_bond_max' not in project_rows([cell('li_s_bond_length_angstrom', 2.5, unit='Å')])['one']


def test_divergent_aliases_are_excluded_instead_of_arbitrarily_chosen():
    rows = project_rows([cell('li2s_decomposition_barrier_ev', .5), cell('li2s_dissociation_barrier_ev', .8)])
    barrier = rows['one']['li2s_dissociation_barrier']
    assert not barrier['exploratory_eligible']
    assert barrier['exclusion_reason'] == 'conflicting_field_aliases'
    assert len(source_ids(barrier)) == 2


def test_empty_preset_returns_no_points_without_legacy_fallback():
    svc = AnalysisReadonlyService(MagicMock())
    svc.records = [cell('d_band_center_ev', -1)]
    svc._loaded = ([], {}, {'total_dft_rows':len(svc.records)})
    result = svc.correlation(x_field='li2s_adsorption_energy', y_field='d_band_center')
    assert result['points'] == []
    assert result['ready'] is False
    svc.session.scalars.assert_not_called()


def test_derived_maximum_does_not_rescue_excluded_bond():
    rows = project_rows([cell('li1_s_bond_length_angstrom', 2.3, unit='Å'),
                         cell('li2_s_bond_length_angstrom', 2.6, unit='Å', reason='estimated_excluded')])
    assert not rows['one']['li_s_bond_max']['exploratory_eligible']


@pytest.mark.parametrize('species,field', [('Li2S','li2s_adsorption_energy'),
    ('Li2S6','li2s6_adsorption_energy'), ('S8','s8_adsorption_energy')])
def test_known_adsorption_has_only_one_species_variable(species, field):
    original = cell('adsorption_energy_ev', -2, adsorbate=species)
    projected = project_rows([original])['one']
    assert set(projected) == {field}
    assert source_ids(projected[field]) == [original['record_id']]
    assert original['field_name'] == 'adsorption_energy_ev'


def test_known_bader_transfer_is_not_duplicated_as_generic():
    projected = project_rows([cell('bader_charge_transfer_e', -.4, unit='e')])['one']
    assert set(projected) == {'li2s_bader_charge_transfer'}


def test_unclassified_adsorption_preserved_but_not_a_matrix_axis():
    svc = AnalysisReadonlyService(MagicMock())
    svc.records = [cell('adsorption_energy_ev', -2, adsorbate=None),
                   cell('adsorption_energy_ev', -3, row='two'),
                   cell('d_band_center_ev', -1)]
    svc._loaded = ([], {}, {'total_dft_rows':len(svc.records)})
    assert 'adsorption_energy' in svc._rebuild_fields()['one']
    matrix = svc.correlation_matrix()
    assert 'adsorption_energy' in matrix['omitted_fields']
    assert 'adsorption_energy' not in {v['key'] for v in matrix['variables']}
    assert 'li2s_adsorption_energy' in {v['key'] for v in matrix['variables']}
    svc.session.commit.assert_not_called()


@pytest.mark.parametrize('raw,field,unit', [
    ('adsorption_energy_li2s_ev', 'li2s_adsorption_energy', 'eV'),
    ('li2s_decomposition_barrier_ev', 'li2s_dissociation_barrier', 'eV'),
    ('li2s_delithiation_barrier_ev', 'li2s_delithiation_barrier', 'eV'),
    ('li2s_bader_charge_transfer', 'li2s_bader_charge_transfer', 'e'),
    ('li1_s_bond_length_angstrom', 'li1_s_bond_length', 'Å'),
    ('li_s_bond_max', 'li_s_bond_max', 'Å'),
    ('adsorption_energy_s8_ev', 's8_adsorption_energy', 'eV'),
    ('adsorption_energy_li2s8_ev', 'li2s8_adsorption_energy', 'eV'),
    ('adsorption_energy_li2s6_ev', 'li2s6_adsorption_energy', 'eV'),
    ('adsorption_energy_li2s4_ev', 'li2s4_adsorption_energy', 'eV'),
    ('adsorption_energy_li2s2_ev', 'li2s2_adsorption_energy', 'eV'),
])
def test_dedicated_species_conflicts_excluded_with_original_evidence(raw, field, unit):
    from copy import deepcopy
    from app.services.rebuild_analysis_pairing import pair_rows
    original = cell(raw, .5, adsorbate='CO2', unit=unit, reason='existing_conflict')
    original.update(raw_value='0.500', sources=[{'quote': 'original', 'page_number': 3}])
    before = deepcopy(original)
    projected = project_rows([original])['one'][field]
    assert not projected['exploratory_eligible']
    assert projected['exclusion_reason'] == 'field_adsorbate_conflict'
    assert projected['raw_evidence'] == [before]
    assert projected['raw_field_names'] == [raw]
    assert source_ids(projected) == [original['record_id']]
    assert original == before
    pairs, excluded, _, _ = pair_rows({'one': {field: projected}}, field, 'd_band_center')
    assert pairs == [] and excluded[0][2] == 'field_adsorbate_conflict'


@pytest.mark.parametrize('species', ['Li2S', '*Li2S', 'Li₂S*', None, 'unknown', 'LiPS'])
def test_dedicated_field_consistent_or_unknown_does_not_invent_object(species):
    original = cell('li2s_decomposition_barrier_ev', .5, adsorbate=species)
    result = project_rows([original])['one']['li2s_dissociation_barrier']
    assert result['exploratory_eligible'] and result['exclusion_reason'] is None
    assert result['adsorbate'] == species


def test_conflicting_species_aliases_preserve_all_raw_candidates():
    a = cell('li2s_decomposition_barrier_ev', .5)
    b = cell('li2s_dissociation_barrier_ev', .8, adsorbate='Li2S4')
    result = project_rows([a, b])['one']['li2s_dissociation_barrier']
    assert result['exclusion_reason'] == 'field_adsorbate_conflict'
    assert result['raw_evidence'] == [a, b]
    assert len(source_ids(result)) == 2


def test_li2s_field_with_explicit_li2s4_is_excluded_and_generic_is_unchanged():
    original = cell('adsorption_energy_li2s_ev', -2, adsorbate='Li2S4')
    result = project_rows([original])['one']['li2s_adsorption_energy']
    assert result['exclusion_reason'] == 'field_adsorbate_conflict'
    assert result['raw_evidence'] == [original]
    generic = cell('adsorption_energy_ev', -2, adsorbate='unresolved original object')
    result = project_rows([generic])['one']['adsorption_energy']
    assert result['exploratory_eligible'] and result['adsorbate'] == generic['adsorbate']
