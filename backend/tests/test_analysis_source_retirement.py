from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analysis_readonly as api
from app.db.session import get_db_session
from app.services.analysis_readonly import AnalysisReadonlyService, dataset_profile_matches, property_filter_matches

pytestmark = pytest.mark.no_test_database


def client():
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    return TestClient(app)


@pytest.mark.parametrize('path', [
    '/papers/compare', '/papers/analysis-summary', '/papers/export/dft-dataset',
    '/papers/export/csv', '/dft/catalyst-dataset', '/dft/catalyst-dataset.csv',
    '/visuals/analysis-fields', '/visuals/data-revision', '/visuals/catalyst-correlation',
    '/visuals/catalyst-correlation/render.svg',
    '/visuals/correlation-pairs', '/visuals/overview',
])
def test_retired_source_cannot_reach_any_public_analysis_or_export(path, monkeypatch):
    def forbidden(*_):
        raise AssertionError('retired-source request must never load observations')
    monkeypatch.setattr(api, 'service', forbidden)
    response = client().get('/api' + path, params=dict(source='legacy', x_field='x', y_field='y', target_property='y', descriptor='x'))
    assert response.status_code == 410
    assert response.json()['detail']['code'] == 'legacy_extraction_retired'


def test_default_and_explicit_new_source_select_dft_without_legacy_fallback(monkeypatch):
    captured = []
    class Projection:
        def compare(self, **kwargs):
            return {'source': 'rebuild', 'items': []}
    def fake_service(args, session):
        captured.append(args)
        return Projection()
    monkeypatch.setattr(api, 'service', fake_service)
    c = client()
    for params in [{}, {'source': 'rebuild'}, {'data_type': 'experimental'}]:
        assert c.get('/api/papers/compare', params=params).status_code == 200
    assert [args['source'] for args in captured] == ['rebuild'] * 3
    assert [args['filters']['data_type'] for args in captured] == ['dft', 'dft', 'experimental']


def test_experimental_rows_do_not_enter_default_dft_projection():
    svc = AnalysisReadonlyService(MagicMock(), filters={'data_type': 'dft'})
    assert svc.source == 'rebuild'
    assert svc._matches({'data_type': 'dft'})
    assert not svc._matches({'data_type': 'experimental'})


def test_binding_energy_filter_includes_species_specific_fields():
    svc = AnalysisReadonlyService(MagicMock(), filters={'property_type': 'binding_energy'})
    assert svc._matches({
        'property_type': 'li2s2_binding_energy_ev',
        'canonical_property_type': 'li2s2_binding_energy_ev',
    })
    assert svc._matches({
        'property_type': 'li2s8_binding_energy_ev',
        'canonical_property_type': 'li2s8_binding_energy_ev',
    })
    assert svc._matches({
        'property_type': 'binding_energy_li2s8_ev',
        'canonical_property_type': 'binding_energy_li2s8_ev',
    })
    assert svc._matches({
        'property_type': 'binding_energy_eV',
        'canonical_property_type': 'binding_energy_eV',
        'adsorbate': 'Li2S6*',
    })
    assert not svc._matches({
        'property_type': 'adsorption_energy_ev',
        'canonical_property_type': 'adsorption_energy_ev',
    })
    assert property_filter_matches({'property_type': 'metal_support_binding_energy_eb'}, 'binding_energy') is False
    assert property_filter_matches({'property_type': 'metal_support_binding_energy_ev'}, 'binding_energy') is False
    assert property_filter_matches({'property_type': 'relative_binding_energy_ev'}, 'binding_energy') is False
    assert property_filter_matches({'property_type': 'binding_energy'}, 'binding_energy') is False
    assert property_filter_matches({'property_type': 'binding_energy_eV'}, 'binding_energy') is False


@pytest.mark.parametrize('profile,catalyst_type,library,reaction,included', [
    ('dac_lis_ml', 'dual_atom', '锂硫双原子', 'SRR', True),
    ('dac_lis_ml', 'single_atom', '锂硫双原子', 'SRR', False),
    ('dac_lis_ml', 'DAC', '锂硫双原子', 'srr', True),
    ('dac_lis_ml', 'dual_atom_catalyst', '锂硫双原子', 'SRR', True),
    ('dac_lis_ml', 'double_atom', '锂硫双原子', 'SRR', True),
    ('bimetallic_lis_ml', 'bimetallic', '锂硫双原子', 'SRR', True),
    ('dac_lis_ml', 'dual_atom', '硫中毒', 'SRR', False),
    ('dac_lis_ml', 'dual_atom', '锂硫双原子', 'HER', False),
    ('sac_lis_ml', 'single_atom', '锂硫双原子', 'SRR', True),
    ('sac_lis_ml', 'SAC', '锂硫双原子', 'srr', True),
    ('sac_lis_ml', 'single_atom_catalyst', '锂硫双原子', 'SRR', True),
])
def test_lis_dataset_profile_is_strict_to_library_reaction_and_catalyst_type(profile, catalyst_type, library, reaction, included):
    record = {'catalyst_type': catalyst_type, 'library_name': library, 'reaction': reaction}
    assert dataset_profile_matches(record, profile) is included
    svc = AnalysisReadonlyService(MagicMock())
    svc.records = [{
        'record_id': 'record-1',
        'paper_id': 'paper-1',
        'sources': [],
        'library_name': library,
        'catalyst_type': catalyst_type,
        'reaction': reaction,
        'exploratory_eligible': True,
    }]
    svc._loaded = ([], {}, {})
    payload = svc.observation_dataset(dataset_profile=profile)
    assert payload['metadata']['profile_included_count_before_limit'] == int(included)
    assert len(payload['records']) == int(included)


def correlation_fixture(contexts):
    points = [{'x': {'value': float(i)}, 'y': {'value': float(i) * 2},
               'semantic_context': {
                   'data_type': 'dft', 'reaction_type': 'SRR',
                   'x_method': {}, 'y_method': {},
                   'x_condition': {'pressure': context}, 'y_condition': {'pressure': context},
                   'x_unit': 'eV', 'y_unit': 'eV',
               }}
              for i, context in enumerate(contexts)]
    stats = dict(pearson=1., pearson_r=1., spearman=1., spearman_rho=1., r_squared=1., r2=1., slope=2., intercept=0., ready=True)
    return {'points': points, 'warnings': [], 'statistics': dict(stats), **stats}


def test_mixed_conditions_suppress_global_fit_even_with_enough_points(monkeypatch):
    svc = AnalysisReadonlyService(MagicMock())
    payload = correlation_fixture(['a'] * 3 + ['b'] * 3)
    monkeypatch.setattr(svc, '_rebuild_correlation', lambda *_: payload)
    result = svc.correlation(x_field='x', y_field='y', min_n=3)
    assert len(result['points']) == 6
    assert not result['ready']
    assert result['slope'] is None
    assert result['insufficient_reason'].startswith('mixed_calculation_or_reaction_contexts')
    assert [g['n'] for g in result['comparability_groups']] == [3, 3]
    assert all(g['statistics']['slope'] == 2 for g in result['comparability_groups'])
    svc.session.commit.assert_not_called()


def test_single_context_preserves_fit(monkeypatch):
    svc = AnalysisReadonlyService(MagicMock())
    monkeypatch.setattr(svc, '_rebuild_correlation', lambda *_: correlation_fixture(['a'] * 3))
    result = svc.correlation(x_field='x', y_field='y', min_n=3)
    assert result['ready']
    assert result['slope'] == 2
    svc.session.commit.assert_not_called()


def test_retired_history_transfer_never_writes():
    import asyncio
    from uuid import uuid4
    from fastapi import HTTPException
    from app.api.rebuild import transfer_history
    session = MagicMock()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(transfer_history(uuid4(), None, session=session, auth=None))
    assert exc.value.status_code == 410
    session.commit.assert_not_called()
    session.add.assert_not_called()


def test_constant_x_never_claims_fit_ready(monkeypatch):
    svc = AnalysisReadonlyService(MagicMock())
    payload = correlation_fixture(['a'] * 3)
    payload['slope'] = None
    payload['statistics']['slope'] = None
    monkeypatch.setattr(svc, '_rebuild_correlation', lambda *_: payload)
    result = svc.correlation(x_field='x', y_field='y', min_n=3)
    assert not result['ready']
    assert not result['statistics']['ready']
    assert 'X 字段' in result['insufficient_reason']


@pytest.mark.parametrize('status,eligible', [
    ('evidence_history', True), ('corrected_previous_retained', True),
    ('value_conflict', False), ('unit_conflict', False),
])
def test_new_projection_keeps_evidence_history_but_excludes_actual_conflicts(status, eligible):
    from uuid import uuid4
    pid, rid, vid = uuid4(), uuid4(), uuid4()
    paper = {'id': pid, 'library_name': 'test', 'paper_code': 'T1',
             'title': 'Evidence-backed DFT', 'doi': None, 'journal': None, 'year': None}
    row = {'id': rid, 'paper_id': pid, 'configuration': 'synthetic', 'properties': {},
           'condition': {}, 'reaction': 'SRR', 'material': 'Fe-N4',
           'active_site_type': 'single_atom', 'active_site': 'Fe', 'support': 'NC',
           'material_family': None, 'data_type': 'dft'}
    value = {'id': vid, 'row_id': rid, 'paper_id': pid, 'field_name': 'adsorption_energy_ev',
             'numeric_value': -1., 'raw_value': '-1.0', 'unit': 'eV', 'value_type': 'explicit',
             'is_estimated': False,
             'conflict': {'status': status, 'candidates': [{'numeric_value': -1.}]},
             'missing_reason': None}
    source = {'id': uuid4(), 'row_id': rid, 'value_id': vid, 'paper_id': pid,
              'source_kind': 'table', 'page_number': 2, 'quote': 'Fe-N4 -1.0 eV'}
    session = MagicMock()
    results = []
    for records in ([paper], [row], [value], [source]):
        result = MagicMock()
        result.mappings.return_value = records
        results.append(result)
    session.execute.side_effect = results
    svc = AnalysisReadonlyService(session, filters={'data_type': 'dft'})
    payload = svc.compare(offset=0, limit=25, sort='catalyst_group')
    assert payload['total'] == 1
    assert payload['items'][0]['exploratory_eligible'] is eligible
    assert svc.summary()['exploratory_numeric_records'] == int(eligible)
    assert any(f['available_count'] for f in svc.field_registry()) is eligible
    assert payload['items'][0]['sources'][0]['page_number'] == 2
    assert session.execute.call_count == 4  # No old catalyst or DFT-setting reads.
    session.commit.assert_not_called()
