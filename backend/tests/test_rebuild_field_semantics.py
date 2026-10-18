from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.rebuild_field_semantics import field_semantics_contract
from app.services.rebuild_quality import producer_contract
from app.services.rebuild_workflow_service import reaction_templates

pytestmark = pytest.mark.no_test_database


def test_template_and_producer_share_additive_semantic_contract():
    contract = field_semantics_contract()
    templates = reaction_templates()
    assert templates['version'] == 'rebuild_v1'
    assert templates['field_semantics'] == producer_contract()['field_semantics'] == contract
    fields = {f['name']: f for f in templates['common_fields'] + templates['reactions']['SRR']['fields']}
    for field in contract['recommended_fields']:
        assert fields[field['name']] == field
        assert field['applies_when'] and field['not_for']
    assert {'migration_barrier_ev', 'adsorption_free_energy_ev'} <= fields.keys()
    assert '不能从最大 ΔG' in fields['delta_g_rds_ev']['not_for']
    assert 'missing_reason' in contract['uncertainty']
    assert 'reaction_step' in contract['identity_and_evidence']


def test_real_compact_work_package_transmits_contract_without_database():
    from app.services.rebuild_work_package import compact_package
    session = MagicMock()
    paper_id = uuid4()
    session.get.return_value = SimpleNamespace(paper_code='synthetic', title='synthetic')
    session.scalar.return_value = 0
    session.scalars.return_value = []
    package = compact_package(session, paper_id, {'revision': 'synthetic', 'output_manifest': []})
    assert package['producer_contract']['field_semantics'] == reaction_templates()['field_semantics']
    assert package['templates_endpoint'] == '/api/rebuild/templates'
    session.commit.assert_not_called()


def test_full_work_package_route_transmits_same_contract(monkeypatch):
    import asyncio
    from app.api import rebuild as route
    from app.services import rebuild_acceptance, rebuild_sections
    session = MagicMock()
    snapshot = {'revision': 'a' * 64, 'output_manifest': []}
    monkeypatch.setattr(rebuild_acceptance, 'current_snapshot', lambda *args: (snapshot, None))
    monkeypatch.setattr(rebuild_acceptance, 'acceptance_status', lambda *args, **kwargs: {})
    monkeypatch.setattr(rebuild_sections, 'list_sections', lambda *args: [])
    monkeypatch.setattr(rebuild_sections, 'section_coverage', lambda *args: {})
    monkeypatch.setattr(route, '_required_paper', lambda *args: None)
    monkeypatch.setattr(route, 'list_files', lambda *args: [])
    monkeypatch.setattr(route, 'list_assets', lambda *args: [])
    monkeypatch.setattr(route, 'get_paper', lambda *args: {})
    monkeypatch.setattr(route, 'list_rows', lambda *args, **kwargs: {'has_more': False})
    package = asyncio.run(route.rebuild_ai_work_package(uuid4(), rows_limit=50, rows_offset=0,
        session=session, _auth=None, view='full', expected_revision=None))
    assert package['producer_contract']['field_semantics'] == package['templates']['field_semantics']
    session.commit.assert_not_called()


@pytest.mark.parametrize('field', ['migration_barrier_ev', 'adsorption_free_energy_ev',
                                 'reaction_step_delta_g_ev', 'delta_g_rds_ev', 'legacy_custom_value'])
def test_legacy_and_custom_fields_remain_valid_without_semantic_required_keys(field):
    from app.schemas.rebuild import RebuildDataRowInput, RebuildDataValueInput
    value = RebuildDataValueInput(field_name=field, raw_value='0.50', numeric_value=.5, unit='eV')
    row = RebuildDataRowInput(reaction='SRR', material='synthetic', active_site_type='dual_atom',
                             data_type='dft', values=[value.model_dump()])
    assert row.values[0]['field_name'] == field


def test_migration_and_adsorption_free_energy_are_not_silently_reclassified():
    from app.services.rebuild_analysis_fields import project_rows
    records = [dict(rebuild_row_id='one', field_name=field, adsorbate='Li2S')
               for field in ('migration_barrier_ev', 'adsorption_free_energy_ev', 'reaction_step_delta_g_ev')]
    assert project_rows(records) == {'one': {}}
