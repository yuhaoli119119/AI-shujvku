"""Synthetic isolated DB tests; retained per-test namespaces and source fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from app.config import get_settings
from app.db.models import Base, AuditLog, Paper, RebuildPaperFile, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.services.staged_extraction import EVENT
from test_rebuild_mutation_audit import audit_env, row_body
from test_extraction_coverage import snapshot, disk
from conftest import validated_test_root_database_url


@pytest.fixture(autouse=True)
def default_test_database_mode(monkeypatch):
    # Override suite cleanup here: retain these task-specific fixtures for review.
    url=validated_test_root_database_url(os.environ.get('LITAI_TEST_ROOT_DATABASE_URL'))
    admin=create_engine(url);schema='staged_'+uuid4().hex
    with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    parsed=make_url(url);url=parsed.set(query={**parsed.query,'options':f'-csearch_path={schema},public'}).render_as_string(hide_password=False)
    engine=create_engine(url);Base.metadata.create_all(engine)
    monkeypatch.setenv('LITAI_TEST_DATABASE_URL',url);monkeypatch.setenv('LITAI_DATABASE_URL',url);get_settings.cache_clear()
    evidence('retained-'+schema,{'schema':schema,'database':'literature_ai_test'})
    yield
    from app.db.session import _engines,_session_factories,_initialized_urls
    for item in _engines.values():item.dispose()
    _engines.clear();_session_factories.clear();_initialized_urls.clear();get_settings.cache_clear()
    engine.dispose();admin.dispose()


def evidence(name,data):
    if os.getenv('STAGED_EVIDENCE_ROOT'):
        root=Path(os.environ['STAGED_EVIDENCE_ROOT']);root.mkdir(parents=True,exist_ok=True)
        (root/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n')


def url(env):return f'/api/rebuild/papers/{env["paper"]}'
def read(env,stage='source_scope',suffix=''):
    response=env['client'].get(url(env)+f'/staged-work-package?stage={stage}'+suffix)
    assert response.status_code==200,response.text
    return response.json()
def request(env,stage='reconcile'):
    package=read(env)
    return {'request_id':str(uuid4()),'stage':stage,'expected_binding':package['binding'],'expected_check_revision':package['check_revision'],
            'source_scope':[{k:f[k] for k in ('file_id','source_sha256','reading_pdf_sha256')}|{'pages':list(range(1,f['physical_pages']+1)),'locator':'Synthetic full original scope reviewed'} for f in package['items']],
            'item_keys':env.get('keys',[]),'review_statement':'Synthetic scope declaration only, not independent scientific acceptance.'}
def save(env,body):
    response=env['client'].put(url(env)+'/extraction-checks',json=body)
    assert response.status_code==200,response.text
    return response.json()
def codes(package):return {f['code'] for f in package['assessment']['findings']}

@pytest.fixture
def ready_env(audit_env):
    env=audit_env
    with env['factory']() as session:
        session.get(RebuildPaperFile,env['file']).role='main';session.commit()
    imported=env['client'].post('/api/rebuild/rows/import',json=row_body(env));assert imported.status_code==200,imported.text
    row=imported.json()['results'][0]
    with env['factory']() as session:
        value=session.scalar(select(RebuildDataValue));value_id=str(value.id);source_id=str(session.scalar(select(RebuildValueSource.id)))
    file=read(env)['items'][0]
    plan={'request_id':'synthetic-plan','expected_plan_revision':None,'expected_output_revision':read(env)['binding']['output_revision'],
        'files':[{k:file[k] for k in ('file_id','source_sha256','reading_pdf_sha256')}|{'declared_pages':[1,2],'scope_note':'All synthetic physical pages reviewed'}],
        'items':[{'item_key':'energy','title':'Synthetic material energy','kind':'value','identity':{'material':'fixture'},'field_name':'adsorption_energy_ev',
            'source':{'file_id':str(env['file']),'pages':[2],'locator':'Synthetic original cell'},'state':'extracted',
            'link':{'row_id':row['row_id'],'value_id':value_id,'source_id':source_id}}]}
    response=env['client'].put(url(env)+'/coverage-plan',json=plan);assert response.status_code==200,response.text
    env.update(keys=['energy'],plan=plan,imported=imported.json());return env


def test_read_preview_metadata_only_replay_and_scope(ready_env):
    env=ready_env;body=request(env);before=snapshot(env);files=disk(env)
    initial=read(env,'handoff');assert 'current_scoped_check_missing' in codes(initial)
    response=env['client'].post(url(env)+'/extraction-checks/preview',json=body);assert response.status_code==200,response.text
    evidence('preview',response.json())
    assert response.json()['assessment']['program_status']=='scoped_handoff_candidate',response.json()
    assert snapshot(env)==before and disk(env)==files
    receipt=save(env,body);assert receipt['metadata_only'] and receipt['scientific_acceptance']=='not_implied'
    after=snapshot(env)
    for table in before:
        if table!='audit_logs':assert before[table]==after[table],table
    assert len(after['audit_logs'])==len(before['audit_logs'])+1 and disk(env)==files
    current=read(env,'handoff');assert current['assessment']['program_status']=='scoped_handoff_candidate',current
    assert current['latest_checks'][0]['freshness']=='current'
    assert save(env,body)['replayed'] and snapshot(env)==after
    evidence('handoff-current',current);evidence('check-receipt',receipt);evidence('source-package',read(env))
    changed=copy.deepcopy(body);changed['review_statement']='other'
    assert env['client'].put(url(env)+'/extraction-checks',json=changed).status_code==409
    with env['client_for']('other') as client:assert client.put(url(env)+'/extraction-checks',json=body).status_code==409
    stale=copy.deepcopy(body);stale['request_id']='cas'
    assert env['client'].put(url(env)+'/extraction-checks',json=stale).status_code==409
    detail=env['client'].get(url(env)+'/extraction-checks/'+receipt['record_id']);assert detail.json()['freshness']=='current'
    evidence('check-detail',detail.json())


@pytest.mark.parametrize('change',['output','physical','plan','receipt'])
def test_each_revision_invalidates_and_replay_is_original(ready_env,change):
    env=ready_env;body=request(env);receipt=save(env,body);pin=read(env,'coverage')['revision']
    if change=='output':
        with env['factory']() as session:session.scalar(select(RebuildDataRow)).material='changed fixture';session.commit()
    elif change=='physical':
        path=env['settings'].storage_root/'fixture.pdf';path.write_bytes(path.read_bytes()+b'\n% synthetic physical change\n')
    elif change=='plan':
        plan=copy.deepcopy(env['plan']);state=env['client'].get(url(env)+'/coverage-plan').json()
        plan.update(request_id='new-plan',expected_plan_revision=state['plan_revision'],expected_output_revision=state['output_revision'])
        plan['items'][0]['title']='Changed scope title';assert env['client'].put(url(env)+'/coverage-plan',json=plan).status_code==200
    else:
        # A rejected import adds a genuine receipt while scientific output stays the same.
        before=read(env)['binding']['output_revision'];bad=row_body(env);bad['rows'][0]['reaction']='INVALID'
        response=env['client'].post('/api/rebuild/rows/import',json=bad);assert response.status_code==200,response.text
        assert read(env)['binding']['output_revision']==before
    state=read(env,'handoff');assert state['latest_checks'][0]['freshness']=='stale'
    assert state['assessment']['program_status']=='open_items'
    if change=='output':assert 'current_output_receipt_unverified' in codes(state)
    replay=save(env,body);assert replay['replayed'] and replay['binding_as_recorded']==receipt['binding_as_recorded']
    assert env['client'].get(url(env)+f'/staged-work-package?stage=coverage&offset=1&expected_revision={pin}').status_code==409
    evidence('stale-'+change,state)


def test_unknown_cannot_disappear_by_omission_and_exact_receipt_resolves(ready_env):
    env=ready_env;original=row_body(env);original['rows'][0]['row_key']='later-row';original['rows'][0]['material']='later'
    fingerprint=hashlib.sha256(json.dumps({'body':original,'task_id':original['task_id']},sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    body=request(env);body['unknown_requests']=[{'operation':'rows.import','request_id':original['request_id'],'request_sha256':fingerprint,'reason':'Synthetic timeout, actual write outcome unknown'}]
    saved=save(env,body);assert saved['assessment_as_recorded']['receipt_scope']['unknown_total']==1
    assert read(env)['latest_checks'][0]['freshness']=='current' # metadata does not invalidate itself
    with env['client_for']('other') as other:
        check=other.get(url(env)+'/staged-work-package?stage=handoff').json()
        assert check['assessment']['receipt_scope']['unknown_total']==1
    omitted=save(env,request(env));assert omitted['assessment_as_recorded']['receipt_scope']['unknown_total']==1
    assert env['client'].post('/api/rebuild/rows/import',json=original).status_code==200
    result=read(env,'reconcile');assert result['assessment']['receipt_scope']['unknown_total']==0
    evidence('unknown-resolved-actual-receipt',result)


@pytest.mark.parametrize('fault',['file','hash','page','item','receipt','actor','blank','booleanpage'])
def test_rejects_false_references_or_unsafe_writes(ready_env,fault):
    env=ready_env;body=request(env);before=snapshot(env)
    if fault=='file':body['source_scope'][0]['file_id']=str(uuid4())
    elif fault=='hash':body['source_scope'][0]['source_sha256']='0'*64
    elif fault=='page':body['source_scope'][0]['pages']=[99]
    elif fault=='item':body['item_keys']=['foreign-key']
    elif fault=='receipt':body['receipt_ids']=[str(uuid4())]
    elif fault=='actor':body['actor']='forged'
    elif fault=='booleanpage':body['source_scope'][0]['pages']=[True]
    else:body['review_statement']='   '
    response=env['client'].put(url(env)+'/extraction-checks',json=body);assert response.status_code==422,response.text
    assert snapshot(env)==before


def test_auth_csrf_and_missing_scope_are_not_success(ready_env):
    env=ready_env;body=request(env);body['source_scope']=[];body['item_keys']=[]
    receipt=save(env,body);assert receipt['assessment_as_recorded']['program_status']=='open_items'
    before=snapshot(env)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:assert client.get(url(env)+'/staged-work-package').status_code==401
    body=request(env);env['client'].headers['X-CSRF-Token']='wrong'
    assert env['client'].put(url(env)+'/extraction-checks',json=body).status_code==403
    assert snapshot(env)==before


def test_legacy_empty_and_partial_stay_honest(audit_env):
    env=audit_env;legacy=read(env,'handoff');assert legacy['assessment']['program_status']=='open_items'
    assert {'coverage_plan_missing','no_current_scientific_output'}<=codes(legacy)
    body=row_body(env);body['rows'][0]['values'].append({'field_name':'bad','numeric_value':3.14,'sources':[]})
    response=env['client'].post('/api/rebuild/rows/import',json=body);assert response.status_code==200
    partial=read(env,'reconcile');assert 'import_partial_or_recovery_unresolved' in codes(partial)
    assert partial['assessment']['receipt_scope']['partial_unresolved_total']>0
    evidence('legacy-empty',legacy);evidence('partial',partial)


@pytest.mark.parametrize("extra_rows",[0,80])
def test_pagination_pin_and_cost(ready_env,monkeypatch,extra_rows):
    env=ready_env;metrics=[]
    if extra_rows:
        from test_extraction_quality_efficiency import seed_rows
        seed_rows(env,extra_rows)
        state=env['client'].get(url(env)+'/coverage-plan').json();plan=copy.deepcopy(env['plan'])
        plan.update(request_id='refresh-after-seed',expected_plan_revision=state['plan_revision'],expected_output_revision=state['output_revision'])
        assert env['client'].put(url(env)+'/coverage-plan',json=plan).status_code==200
        env['plan']=plan
    before=snapshot(env);files=disk(env)
    baseline=Path('/evidence/backups/backend/app/services')
    if baseline.exists():
        import importlib.util
        import app.services.rebuild_quality as quality_module
        import app.services.rebuild_work_package as package_module
        def load(name):
            spec=importlib.util.spec_from_file_location('baseline_'+name,baseline/(name+'.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
        with monkeypatch.context() as patch:
            patch.setattr(quality_module,'producer_contract',load('rebuild_quality').producer_contract)
            patch.setattr(package_module,'compact_package',load('rebuild_work_package').compact_package)
            queries=[]
            def query_count(*args):queries.append(args[2])
            event.listen(env['engine'],'before_cursor_execute',query_count)
            start=perf_counter();response=env['client'].get(url(env)+'/ai-work-package?view=compact');elapsed=perf_counter()-start
            event.remove(env['engine'],'before_cursor_execute',query_count)
            assert response.status_code==200,response.text
            metrics.append({'name':'baseline_compact','queries':len(queries),'bytes':len(response.content),'seconds':elapsed})
    for name,path in [('compact',url(env)+'/ai-work-package?view=compact'),('source_scope',url(env)+'/staged-work-package?stage=source_scope&limit=1'),
        ('extract',url(env)+'/staged-work-package?stage=extract&limit=1'),('receipts',url(env)+'/staged-work-package?stage=extract&view=receipts&limit=1'),
        ('handoff',url(env)+'/staged-work-package?stage=handoff&limit=1')]:
        queries=[]
        def count(*args):queries.append(args[2])
        event.listen(env['engine'],'before_cursor_execute',count)
        start=perf_counter();response=env['client'].get(path);elapsed=perf_counter()-start
        event.remove(env['engine'],'before_cursor_execute',count)
        assert response.status_code==200,response.text
        metrics.append({'name':name,'queries':len(queries),'bytes':len(response.content),'seconds':elapsed})
        evidence('package-'+name,response.json())
    assert snapshot(env)==before and disk(env)==files
    plan=copy.deepcopy(env['plan']);state=env['client'].get(url(env)+'/coverage-plan').json()
    plan.update(request_id='many',expected_plan_revision=state['plan_revision'])
    for i in range(7):plan['items'].append({'item_key':f'pending-{i}','title':f'Pending {i}','kind':'figure','source':{'file_id':str(env['file']),'pages':[1],'locator':f'Region {i}'},'state':'pending','reason':{'code':'not_processed','detail':'Synthetic missing object'}})
    assert env['client'].put(url(env)+'/coverage-plan',json=plan).status_code==200
    page=read(env,'extract','&limit=2');all_items=[]
    while True:
        all_items+=page['items'];assert len(page['items'])<=2
        if not page['next_page']:break
        response=env['client'].get(page['next_page']);assert response.status_code==200;page=response.json()
    assert len({i['item_key'] for i in all_items})==7
    assert env['client'].get(url(env)+'/staged-work-package?stage=extract&offset=1').status_code==409
    evidence('metrics-'+str(extra_rows+1)+'-rows',metrics);evidence('pending-package',read(env,'extract','&limit=2'))


def test_cross_paper_receipts_records_and_original_scope(ready_env):
    env=ready_env;body=request(env);first=save(env,body)
    with env['factory']() as session:
        foreign=Paper(title='Other synthetic paper',library_name='默认文献库',paper_code='T998',pdf_path='foreign.pdf');session.add(foreign);session.flush()
        audit=AuditLog(paper_id=foreign.id,action='rebuild_mutation_receipt',source='rebuild_mutation_v1',target_type='rows.import',target_id='foreign',payload={'actor':'writer','outcome':'succeeded','result':{'complete':True}})
        session.add(audit);session.commit();foreign_id=str(foreign.id);audit_id=str(audit.id)
    body=request(env);body['receipt_ids']=[audit_id];before=snapshot(env)
    assert env['client'].put(url(env)+'/extraction-checks',json=body).status_code==422
    assert env['client'].get(f'/api/rebuild/papers/{foreign_id}/extraction-checks/{first["record_id"]}').status_code==404
    assert snapshot(env)==before


def test_not_reported_is_scoped_declaration_not_success_by_empty_counts(ready_env):
    env=ready_env;plan=copy.deepcopy(env['plan']);state=env['client'].get(url(env)+'/coverage-plan').json()
    plan.update(request_id='not-reported',expected_plan_revision=state['plan_revision'])
    plan['items'].append({'item_key':'not-reported','title':'Original unreported property','kind':'figure','source':{'file_id':str(env['file']),'pages':[1],'locator':'Scoped synthetic region'},'state':'not_reported',
        'reason':{'code':'not_reported','detail':'Not reported within declared region','checked_pages':[1],'method':'Synthetic original region review'}})
    assert env['client'].put(url(env)+'/coverage-plan',json=plan).status_code==200
    env['keys'].append('not-reported');receipt=save(env,request(env))
    assert receipt['assessment_as_recorded']['program_status']=='scoped_handoff_candidate'
    assert receipt['assessment_as_recorded']['scientific_declaration']=='not_independently_verified'
    evidence('not-reported-scoped',receipt)


def test_prompt_keeps_bound_and_ds_guard(ready_env):
    env=ready_env
    from app.services.ai_extract_service import _build_prompt
    with env['factory']() as session:
        prompt,_=_build_prompt(session,env['paper'],env['settings'])
    assert len(prompt)<=4000,len(prompt)
    assert 'producer_contract.staged_checks' in prompt and 'stage=source_scope&limit=25' in prompt
    assert 'DS' in prompt and '非 DS 模型仅报告章节缺口交 DS' in prompt
    evidence('prompt-length',{'characters':len(prompt),'limit':4000,'ds_guard_retained':True})


@pytest.mark.parametrize('view,key',[('rows_contract','preflight_endpoint'),('table_contract','version'),('section_contract','version')])
def test_only_requested_contract_is_loaded(ready_env,view,key):
    env=ready_env;before=snapshot(env)
    result=read(env,'extract','&view='+view)
    assert result['view']==view and key in result['contract'] and result['items']==[]
    assert not {'producer_contract','table_write_contract','section_write_contract'}&result.keys()
    assert snapshot(env)==before


def test_browser_fixture_with_actual_versioned_responses(ready_env):
    env=ready_env;save(env,request(env))
    fixture={'source':read(env),'coverage':read(env,'coverage'),'handoff':read(env,'handoff'),
             'receipts':read(env,'extract','&view=receipts')}
    plan=copy.deepcopy(env['plan']);state=env['client'].get(url(env)+'/coverage-plan').json()
    plan.update(request_id='browser-pending',expected_plan_revision=state['plan_revision'])
    for i in range(3):plan['items'].append({'item_key':f'pending-{i}','title':f'实际合成原文待办 {i}','kind':'figure','source':{'file_id':str(env['file']),'pages':[1],'locator':f'Synthetic region {i}'},'state':'pending','reason':{'code':'not_processed','detail':'Awaiting original object extraction'}})
    assert env['client'].put(url(env)+'/coverage-plan',json=plan).status_code==200
    fixture['pending']=read(env,'extract','&limit=2')
    fixture['pending_next']=env['client'].get(fixture['pending']['next_page']).json()
    fixture['stale']=read(env,'handoff');fixture['empty_extract']=read(env,'extract')
    partial=row_body(env);partial['rows'][0]['values'].append({'field_name':'bad','numeric_value':3.14,'sources':[]})
    assert env['client'].post('/api/rebuild/rows/import',json=partial).status_code==200
    fixture['receipts']=read(env,'extract','&view=receipts')
    key=next(i['receipt_id'] for i in fixture['receipts']['items'] if i['request_id']==partial['request_id'])
    fixture['receipt_errors']=env['client'].get(url(env)+'/extraction-import-receipts/'+key+'?limit=1').json()
    evidence('browser-fixture',fixture)


def test_metadata_insert_failure_rolls_back_no_reserved_id(ready_env):
    env=ready_env;body=request(env);before=snapshot(env);files=disk(env)
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO audit_logs'):raise RuntimeError('synthetic metadata failure')
    event.listen(env['engine'],'before_cursor_execute',fail)
    try:assert env['client'].put(url(env)+'/extraction-checks',json=body).status_code==500
    finally:event.remove(env['engine'],'before_cursor_execute',fail)
    assert snapshot(env)==before and disk(env)==files
    assert not save(env,body)['replayed']


def test_partial_field_resolves_only_through_real_later_value_receipt(ready_env):
    env=ready_env;body=row_body(env)
    body['rows'][0]['values'].append({'field_name':'bad','numeric_value':3.14,'sources':[]})
    assert env['client'].post('/api/rebuild/rows/import',json=body).status_code==200
    before=read(env,'reconcile');assert before['assessment']['receipt_scope']['partial_unresolved_total']==1
    repaired=row_body(env);repaired['rows'][0]['values']=[{'field_name':'bad','raw_value':'3.1400','numeric_value':3.14,'unit':'eV','precision_digits':4,
        'sources':[{'source_kind':'table','file_id':str(env['file']),'page_number':2,'table_row':0,'table_column':2,'quote':'3.1400'}]}]
    result=env['client'].post('/api/rebuild/rows/import',json=repaired);assert result.status_code==200,result.text
    after=read(env,'reconcile');assert after['assessment']['receipt_scope']['partial_unresolved_total']==0,after
    evidence('partial-field-recovered',after)



def test_original_receipt_failures_paginate_without_reading_or_changing_current_science(ready_env):
    env=ready_env;body=row_body(env)
    for i in range(3):body['rows'][0]['values'].append({'field_name':f'bad-{i}','numeric_value':3.14,'sources':[]})
    assert env['client'].post('/api/rebuild/rows/import',json=body).status_code==200
    receipts=read(env,'extract','&view=receipts');header=next(i for i in receipts['items'] if i['request_id']==body['request_id'])
    assert header['errors_total']==3 and header['outcome']=='partial'
    before=snapshot(env);files=disk(env);path=url(env)+'/extraction-import-receipts/'+header['receipt_id']
    first=env['client'].get(path+'?limit=1');assert first.status_code==200,first.text
    first=first.json();errors=first['errors']
    assert first['errors_total']==3 and first['has_more'] and len(errors)==1
    for offset in (1,2):
        response=env['client'].get(path+f'?limit=1&offset={offset}&expected_revision='+first['revision'])
        assert response.status_code==200,response.text;errors+=response.json()['errors']
    assert len({item['field'] for item in errors})==3
    assert env['client'].get(path+'?offset=1').status_code==409
    assert env['client'].get(path+'?offset=1&expected_revision='+'0'*64).status_code==409
    assert env['client'].get(url(env)+'/extraction-import-receipts/'+str(uuid4())).status_code==404
    assert snapshot(env)==before and disk(env)==files
    evidence('actual-partial-receipt-first-page',first)


@pytest.mark.parametrize('fault',['file','hash','page','item','receipt','blank_statement','blank_unknown_reason'])
def test_preview_invalid_references_are_blockers_without_writes(ready_env,fault):
    env=ready_env;body=request(env)
    if fault=='file':body['source_scope'][0]['file_id']=str(uuid4())
    elif fault=='hash':body['source_scope'][0]['source_sha256']='0'*64
    elif fault=='page':body['source_scope'][0]['pages']=[99]
    elif fault=='item':body['item_keys']=['nonexistent-object']
    elif fault=='receipt':body['receipt_ids']=[str(uuid4())]
    elif fault=='blank_statement':body['review_statement']='   '
    else:
        # Match a real resolved receipt so only the invalid blank reason blocks.
        with env['factory']() as session:
            receipt=session.scalar(select(AuditLog).where(AuditLog.action=='rebuild_mutation_receipt'))
            body['unknown_requests']=[{'operation':receipt.target_type,'request_id':receipt.target_id,
                'request_sha256':receipt.payload['request_sha256'],'reason':'   '}]
    before=snapshot(env);files=disk(env)
    response=env['client'].post(url(env)+'/extraction-checks/preview',json=body)
    assert response.status_code==200,response.text
    assessment=response.json()['assessment']
    assert not assessment['references_valid'] and assessment['reference_errors']
    assert assessment['program_status']=='open_items'
    assert {'code':'check_references_invalid','count':len(assessment['reference_errors'])} in assessment['findings']
    assert assessment['next_stage']!='handoff'
    assert '未发现阻碍' not in assessment['advice_zh'] and '可携范围交接复核' not in assessment['advice_zh']
    assert env['client'].put(url(env)+'/extraction-checks',json=body).status_code==422
    assert snapshot(env)==before and disk(env)==files
    evidence('preview-invalid-'+fault,response.json())



def test_historical_section_event_cannot_replace_current_scientific_output(audit_env):
    env=audit_env
    with env['factory']() as session:
        session.get(RebuildPaperFile,env['file']).role='main'
        session.add(AuditLog(paper_id=env['paper'],action='section_imported',source='rebuild_sections_v1',target_type='paper_section',target_id=str(uuid4()),payload={}))
        session.commit()
    file=read(env)['items'][0]
    plan={'request_id':'history-only','expected_plan_revision':None,'expected_output_revision':read(env)['binding']['output_revision'],
        'files':[{k:file[k] for k in ('file_id','source_sha256','reading_pdf_sha256')}|{'declared_pages':[1,2],'scope_note':'Synthetic full original scope'}],
        'items':[{'item_key':'not-reported','title':'Synthetic unreported object','kind':'figure','source':{'file_id':str(env['file']),'pages':[1],'locator':'Original region'},'state':'not_reported',
            'reason':{'code':'not_reported','detail':'Unreported within reviewed scope','checked_pages':[1],'method':'Synthetic visual examination'}}]}
    response=env['client'].put(url(env)+'/coverage-plan',json=plan);assert response.status_code==200,response.text
    env['keys']=['not-reported'];before=snapshot(env);files=disk(env)
    response=env['client'].post(url(env)+'/extraction-checks/preview',json=request(env));assert response.status_code==200,response.text
    assessment=response.json()['assessment']
    assert assessment['references_valid'] and assessment['program_status']=='open_items'
    assert 'no_current_scientific_output' in {item['code'] for item in assessment['findings']}
    assert snapshot(env)==before and disk(env)==files
    evidence('historical-section-without-current-output',response.json())
