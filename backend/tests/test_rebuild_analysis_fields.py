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
