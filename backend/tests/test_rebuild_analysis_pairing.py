import json
from unittest.mock import MagicMock

import pytest

from app.services.analysis_readonly import AnalysisReadonlyService
from app.services.rebuild_analysis_pairing import pair_rows

pytestmark = pytest.mark.no_test_database


def cell(field, value, row, material='Fe-N4', paper='paper', method='PBE', reason=None):
    return dict(field_name=field, record_id=row + ':' + field, rebuild_row_id=row,
                paper_id=paper, paper_code='B001', catalyst_name=material,
                active_site='N4', configuration=material, support='graphene',
                reaction='SRR', data_type='dft', adsorbate='Li2S',
                target={'normalized_value':value,'normalized_unit':'eV'},
                exploratory_eligible=reason is None, exclusion_reason=reason,
                raw_value=value, raw_unit='eV', sources=[{'page_number':2}],
                analysis_context={'condition_key':json.dumps({'functional':method})})


def test_split_rows_pair_same_material_and_keep_both_source_ids():
    x=cell('li2s_adsorption_energy',-2,'adsorption')
    y=cell('d_band_center',-1,'electronic')
    svc=AnalysisReadonlyService(MagicMock())
    svc.records=[x,y]
    svc._loaded=([],{}, {'total_dft_rows':2})
    result=svc.correlation(x_field=x['field_name'], y_field=y['field_name'])
    assert result['n_catalysts']==1
    point=result['points'][0]
    assert point['pairing_rule'].startswith('same_paper_material')
    assert point['x_source_record_ids']==[x['record_id']]
    assert point['y_source_record_ids']==[y['record_id']]
    assert set(result['record_sources'])=={x['record_id'],y['record_id']}
    svc.session.commit.assert_not_called()


@pytest.mark.parametrize('changed', [dict(paper_id='other'),dict(catalyst_name='Co-N4'),
                                     dict(active_site='N3'),dict(configuration='isomer 2'),
                                     dict(analysis_context={'condition_key':'{"functional":"HSE"}'})])
def test_split_rows_cannot_cross_known_paper_identity_or_method(changed):
    x=cell('x',-2,'x');y=cell('y',-1,'y');y.update(changed)
    pairs,_,_,_=pair_rows({'x':{'x':x},'y':{'y':y}},'x','y')
    assert pairs==[]


def test_conflicting_alternative_measurements_are_not_chosen_arbitrarily():
    x=cell('x',-2,'x');a=cell('y',-1,'a');b=cell('y',-.5,'b')
    pairs,excluded,_,_=pair_rows({'x':{'x':x},'a':{'y':a},'b':{'y':b}},'x','y')
    assert pairs==[]
    assert excluded[0][2]=='ambiguous_same_material_measurements'


def test_group_statistics_remain_available_when_global_fit_is_suppressed():
    svc=AnalysisReadonlyService(MagicMock())
    records=[]
    for method in ['PBE','HSE']:
        for i in range(3):
            material=f'{method}-{i}'
            records.extend([cell('li2s_adsorption_energy',-float(i+1),f'{material}-x',material=material,method=method),
                            cell('d_band_center',float(i+1)*2,f'{material}-y',material=material,method=method)])
    svc.records=records;svc._loaded=([],{}, {'total_dft_rows':len(records)})
    result=svc.correlation(x_field='li2s_adsorption_energy', y_field='d_band_center')
    assert len(result['points'])==6
    assert not result['ready']
    assert result['slope'] is None
    assert result['group_fit_count']==2
    assert all(g['statistics']['slope']==-2 for g in result['comparability_groups'])
    matrix=svc.correlation_matrix(fields=['li2s_adsorption_energy','d_band_center'])
    cell_result=next(c for c in matrix['cells'] if c['x_property']!=c['y_property'])
    assert cell_result['status']=='grouped'
    assert cell_result['n']==3 and cell_result['total_paired_n']==6
    assert cell_result['group_label']
    assert cell_result['pearson_r']==-1


def test_unpaired_observations_are_returned_as_real_distributions():
    svc=AnalysisReadonlyService(MagicMock())
    svc.records=[cell('li2s_adsorption_energy',-2,'x'),cell('d_band_center',-.5,'y',material='Co-N4')]
    svc._loaded=([],{}, {'total_dft_rows':2})
    result=svc.correlation(x_field='li2s_adsorption_energy',y_field='d_band_center')
    assert not result['points']
    assert result['distribution']['x'][0]['value']==-2
    assert result['distribution']['y'][0]['value']==-.5


def test_new_property_in_new_row_increases_catalyst_pairs_and_revision():
    x=cell('li2s_adsorption_energy',-2,'x')
    y=cell('d_band_center',-.5,'new-y')
    def service(records):
        svc=AnalysisReadonlyService(MagicMock())
        svc.records=records;svc._loaded=([],{}, {'total_dft_rows':len(records)})
        return svc
    before=service([x]);after=service([x,y])
    assert before.correlation(x_field=x['field_name'],y_field=y['field_name'])['n_catalysts']==0
    assert after.correlation(x_field=x['field_name'],y_field=y['field_name'])['n_catalysts']==1
    assert before.data_revision()['revision']!=after.data_revision()['revision']
    corrected=dict(y,raw_value=-.6,target={'normalized_value':-.6,'normalized_unit':'eV'})
    assert after.data_revision()['revision']!=service([x,corrected]).data_revision()['revision']


def test_same_source_alias_comparison_is_excluded_with_reason():
    x=cell('x',-2,'one');y=cell('y',-2,'one')
    x['source_record_ids']=['original'];y['source_record_ids']=['original']
    pairs,excluded,_,_=pair_rows({'one':{'x':x,'y':y}},'x','y')
    assert pairs==[]
    assert excluded[0][2]=='same_source_self_comparison'


def test_equal_values_from_distinct_sources_remain_pairable():
    x=cell('x',-2,'one');y=cell('y',-2,'one')
    pairs,excluded,_,_=pair_rows({'one':{'x':x,'y':y}},'x','y')
    assert len(pairs)==1 and excluded==[]
