"""Retained isolated fixtures; no production DB, native backend, grants or science writes."""
import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4, UUID

import pytest
from sqlalchemy import select, event
from app.db.models import AuditLog, WorkflowJob, Paper, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.services.rebuild_workflow_service import value_revision, serialize_source
from app.services.rebuild_source_correction import source_state
from test_rebuild_mutation_audit import audit_env, row_body
from test_staged_extraction import default_test_database_mode, ready_env, read as stage_read, request as check_body, evidence
from test_extraction_coverage import snapshot, disk


def endpoint(env):return f'/api/rebuild/papers/{env["paper"]}/history-reconciliation'
def get(env,view='reports',suffix=''):
    r=env['client'].get(endpoint(env)+f'?view={view}'+suffix);assert r.status_code==200,r.text;return r.json()
def put(env,body):
    r=env['client'].put(endpoint(env),json=body);assert r.status_code==200,r.text;return r.json()
def body(env,kind='report_index',**extra):
    state=get(env);return {'request_id':str(uuid4()),'expected_revision':state['revision'],'expected_binding':state['binding'],
        'report_id':'synthetic-report','expected_report_sha256':env['report_sha'],'kind':kind,'note':'Synthetic explicit scoped historical reference, no scientific acceptance.',**extra}


def catalog_fixture(env,known=True,scope=True,links=None):
    root=env['settings'].storage_root.parent/'history_evidence';root.mkdir(exist_ok=True)
    original={'paper_id':str(env['paper']),'observed_output_revision':stage_read(env)['binding']['output_revision'] if known else None,'verdict':'accepted','limitations':['Synthetic report, not a real scientific review.']}
    raw=json.dumps(original,ensure_ascii=False).encode();(root/'original.json').write_bytes(raw)
    files=stage_read(env)['items']
    envelope={'format':'history_evidence_v1','paper_id':str(env['paper']),
        'original':{'path':'original.json','sha256':hashlib.sha256(raw).hexdigest()},'historical_verdict':'accepted',
        'observed_output_revision':stage_read(env)['binding']['output_revision'] if known else None,
        'source_scope':[{k:f[k] for k in ('file_id','source_sha256','reading_pdf_sha256','role')}|
            {'pages':list(range(1,f['physical_pages']+1)),'locator':'Synthetic explicit original scope'} for f in files] if scope else [],
        'limitations':['Synthetic full scope declaration does not prove complete object inventory or independent correctness.'], 'attempt_links':links or []}
    def refresh():
        content=json.dumps(envelope,ensure_ascii=False).encode();(root/'evidence.json').write_bytes(content)
        env['report_sha']=hashlib.sha256(content).hexdigest()
        catalog={'format':'history_catalog_v1','entries':[{'report_id':'synthetic-report','paper_id':str(env['paper']),
            'evidence':{'path':'evidence.json','sha256':env['report_sha']}}]}
        (root/'catalog.json').write_text(json.dumps(catalog));return catalog
    env.update(evidence_root=root,envelope=envelope,refresh_catalog=refresh);env['catalog']=refresh();return env


def update_original(env, document, extension='json'):
    content=(json.dumps(document,ensure_ascii=False) if extension=='json' else document).encode()
    name='original.'+extension
    (env['evidence_root']/name).write_bytes(content)
    env['envelope']['original']={'path':name,'sha256':hashlib.sha256(content).hexdigest()}
    env['refresh_catalog']()


def jobs_fixture(env):
    with env['factory']() as session:
        now=datetime.utcnow();jobs=[]
        for i,status in enumerate(['running','running','failed','completed']):
            progress={'thread_id':f'synthetic-thread-{i}','turn_id':f'synthetic-turn-{i}','dispatch_mode':'codex_web_target'}
            if i==1:progress['native_stop']={'verified_stop':True,'phase':'verified_stopped','thread_id':progress['thread_id'],
                'request_id':str(uuid4()),'requested_at':(now-timedelta(seconds=1)).isoformat(),'automatic_retry':False,'events':[],
                'start_turn_id':progress['turn_id'],'observed_turn_status':'interrupted','observed_goal_status':'paused','last_checked_at':now.isoformat()}
            job=WorkflowJob(job_id=f'synthetic-job-{i}',type='ai_extract_rebuild',status=status,payload={'paper_id':str(env['paper']),
                'run_id':f'synthetic-run-{i}','attempt_token':f'synthetic-attempt-{i}'},progress=progress,
                created_at=now-timedelta(days=4-i),updated_at=now-timedelta(days=3-i))
            session.add(job);jobs.append(job)
        env['jobs']=[j.job_id for j in jobs]
        session.commit()
    return [{'old_job_id':env['jobs'][0],'new_job_id':env['jobs'][-1],'old_run_id':'synthetic-run-0','new_run_id':'synthetic-run-3',
             'old_attempt_token':'synthetic-attempt-0','new_attempt_token':'synthetic-attempt-3'}]


def test_attempt_records_never_poll_resume_or_unlock(audit_env):
    env=audit_env;jobs_fixture(env);before=snapshot(env);files=disk(env)
    first=get(env,'attempts','&limit=2');assert first['raw_active_records']==2 and not first['can_unlock']
    r=env['client'].get(first['next_page']);assert r.status_code==200;second=r.json()
    combined=first['items']+second['items'];by_id={j['job_id']:j for j in combined}
    assert by_id['synthetic-job-0']['observation']['state']=='unknown'
    assert by_id['synthetic-job-1']['observation']['state']=='stopped_as_observed'
    assert by_id['synthetic-job-3']['observation']['state']=='ended_as_recorded'
    assert by_id['synthetic-job-3']['record_role']=='latest_recorded'
    assert by_id['synthetic-job-0']['selection_status']=='not_declared' # later completed never implies safe supersession
    assert all(j['observation']['native_current_status']=='not_polled_unknown' for j in combined)
    assert snapshot(env)==before and disk(env)==files
    assert env['client'].get(endpoint(env)+'?view=attempts&offset=1').status_code==409
    evidence('attempts',first);evidence('attempts-page2',second)
    with env['factory']() as session:session.get(WorkflowJob,'synthetic-job-0').status='failed';session.commit()
    assert env['client'].get(first['next_page']).status_code==409


def test_index_preview_save_only_audit_idempotent_and_actor_bound(ready_env):
    env=catalog_fixture(ready_env);request=body(env);before=snapshot(env);files=disk(env)
    report=get(env);assert report['items'][0]['comparison']=='current_version_match_only'
    checked=env['client'].post(endpoint(env)+'/preview',json=request);assert checked.status_code==200,checked.text
    assert checked.json()['valid'] and checked.json()['current_acceptance']=='not_granted'
    assert snapshot(env)==before and disk(env)==files
    receipt=put(env,request);after=snapshot(env)
    for table in before:
        if table!='audit_logs':assert before[table]==after[table],table
    assert len(after['audit_logs'])==len(before['audit_logs'])+1 and disk(env)==files
    detail=env['client'].get(endpoint(env)+'/records/'+receipt['record_id']);assert detail.status_code==200
    assert detail.json()['freshness']=='current_binding_only'
    assert put(env,request)['replayed'] and snapshot(env)==after
    with env['client_for']('other') as other:assert other.put(endpoint(env),json=request).status_code==409
    modified=copy.deepcopy(request);modified['note']='different'
    assert env['client'].put(endpoint(env),json=modified).status_code==409
    modified=copy.deepcopy(request);modified['request_id']='stale-cas'
    assert env['client'].put(endpoint(env),json=modified).status_code==409
    evidence('reports-current',report);evidence('index-receipt',receipt);evidence('index-record',detail.json())


def test_explicit_supersession_is_only_a_declaration_not_stop_or_unlock(ready_env):
    env=ready_env;links=jobs_fixture(env);catalog_fixture(env,links=links)
    receipt=put(env,body(env,kind='attempt_supersession',old_job_id=env['jobs'][0],new_job_id=env['jobs'][-1]))
    state=get(env,'attempts');old=next(j for j in state['items'] if j['job_id']==env['jobs'][0])
    assert old['selection_status']=='superseded_declaration_only' and old['raw_status']=='running'
    assert old['observation']['state']=='unknown' and state['raw_active_records']==2 and not receipt['can_unlock']
    with env['factory']() as session:session.get(WorkflowJob,env['jobs'][0]).progress={'thread_id':'changed-handle'};session.commit()
    state=get(env,'attempts');old=next(j for j in state['items'] if j['job_id']==env['jobs'][0])
    assert old['selection_status']=='stale_declaration' and old['raw_status']=='running'
    evidence('supersession-stale',state)


@pytest.mark.parametrize('change',['output','source','plan','report','original'])
def test_registration_versions_go_stale_and_replay_is_historical(ready_env,change):
    env=catalog_fixture(ready_env);request=body(env);receipt=put(env,request)
    if change=='output':
        with env['factory']() as s:s.scalar(select(RebuildDataRow)).material='Changed synthetic identity';s.commit()
    elif change=='source':
        p=env['settings'].storage_root/'fixture.pdf';p.write_bytes(p.read_bytes()+b'\n% synthetic file change\n')
    elif change=='plan':
        path=f'/api/rebuild/papers/{env["paper"]}/coverage-plan';state=env['client'].get(path).json();plan=copy.deepcopy(env['plan'])
        plan.update(request_id='new-plan',expected_plan_revision=state['plan_revision'],expected_output_revision=state['output_revision']);plan['items'][0]['title']='new explicit title'
        assert env['client'].put(path,json=plan).status_code==200
    else:
        p=env['evidence_root']/('evidence.json' if change=='report' else 'original.json');p.write_bytes(p.read_bytes()+b' ')
    detail=env['client'].get(endpoint(env)+'/records/'+receipt['record_id']);assert detail.status_code==200
    assert detail.json()['freshness']=='stale'
    assert put(env,request)['replayed']


def test_unknown_versions_and_no_plan_remain_unverified_compatible(audit_env):
    env=catalog_fixture(audit_env,known=False,scope=False);state=get(env)
    item=state['items'][0];assert item['comparison']=='version_unknown' and item['scope_status']=='unknown'
    assert item['historical_verdict_as_reported']=='accepted' and item['current_acceptance']=='not_granted'
    receipt=put(env,body(env));assert receipt['registration_status']=='historical_index_recorded_not_accepted'
    assert stage_read(env,'handoff')['assessment']['program_status']=='open_items'
    with env['factory']() as session:assert not list(session.scalars(select(WorkflowJob)))
    evidence('reports-unknown-version',state)


@pytest.mark.parametrize('path',['../outside.json','/etc/passwd','./evidence.json','.hidden.json','nested/../evidence.json','nested//evidence.json','nested\\evidence.json','secret.pem'])
def test_no_arbitrary_paths_or_credentials_interface(ready_env,path):
    env=catalog_fixture(ready_env);env['catalog']['entries'][0]['evidence']['path']=path
    (env['evidence_root']/'catalog.json').write_text(json.dumps(env['catalog']))
    before=snapshot(env);state=get(env);assert not state['items'][0]['usable_for_index']
    response=env['client'].put(endpoint(env),json=body(env));assert response.status_code==422,response.text
    assert snapshot(env)==before


@pytest.mark.parametrize('which',['file','directory','root','catalog'])
def test_symlinks_are_not_followed(ready_env,which):
    env=catalog_fixture(ready_env);root=env['evidence_root']
    if which=='file':
        (root/'link.json').symlink_to(root/'evidence.json');env['catalog']['entries'][0]['evidence']['path']='link.json'
        (root/'catalog.json').write_text(json.dumps(env['catalog']))
    elif which=='directory':
        (root/'linked').symlink_to(root,target_is_directory=True);env['catalog']['entries'][0]['evidence']['path']='linked/evidence.json'
        (root/'catalog.json').write_text(json.dumps(env['catalog']))
    elif which=='catalog':
        (root/'catalog.json').rename(root/'catalog-original.json');(root/'catalog.json').symlink_to(root/'catalog-original.json')
    else:
        saved=root.with_name('retained-original-evidence');root.rename(saved);root.symlink_to(saved,target_is_directory=True)
    state=get(env)
    assert state['catalog_status']!='available' if which in {'root','catalog'} else not state['items'][0]['usable_for_index']
    assert env['client'].put(endpoint(env),json=body(env)).status_code==422


@pytest.mark.parametrize('fault',['body_paper','original_paper','scope_file','scope_page','hash','forged_state'])
def test_forged_or_cross_paper_evidence_does_not_grant_trust(ready_env,fault):
    env=catalog_fixture(ready_env)
    if fault=='body_paper':env['envelope']['paper_id']=str(uuid4());env['refresh_catalog']()
    elif fault=='original_paper':
        content=json.dumps({'paper_id':str(uuid4())}).encode();(env['evidence_root']/'original.json').write_bytes(content)
        env['envelope']['original']['sha256']=hashlib.sha256(content).hexdigest();env['refresh_catalog']()
    elif fault=='scope_file':env['envelope']['source_scope'][0]['file_id']=str(uuid4());env['refresh_catalog']()
    elif fault=='scope_page':env['envelope']['source_scope'][0]['pages']=[999];env['refresh_catalog']()
    elif fault=='hash':(env['evidence_root']/'evidence.json').write_text('{}')
    request=body(env)
    if fault=='forged_state':request['current_acceptance']='accepted'
    before=snapshot(env);r=env['client'].put(endpoint(env),json=request);assert r.status_code==422,r.text
    assert snapshot(env)==before


def test_auth_csrf_unknown_catalog_and_metadata_failure_rollback(ready_env):
    env=ready_env;assert get(env)['catalog_status']=='catalog_unavailable_or_invalid'
    catalog_fixture(env);request=body(env);before=snapshot(env)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:assert anonymous.get(endpoint(env)).status_code==401
    env['client'].headers['X-CSRF-Token']='bad';assert env['client'].put(endpoint(env),json=request).status_code==403
    env['client'].headers['X-CSRF-Token']=env['client'].cookies.get('litai_csrf')
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO audit_logs'):raise RuntimeError('synthetic metadata failure')
    event.listen(env['engine'],'before_cursor_execute',fail)
    try:assert env['client'].put(endpoint(env),json=request).status_code==500
    finally:event.remove(env['engine'],'before_cursor_execute',fail)
    assert snapshot(env)==before
    assert not put(env,request)['replayed']


def legacy_failure(env,alias=None):
    payload=row_body(env)
    if alias is not None:payload['rows'][0]['row_key']=alias
    payload['rows'][0]['values'][0].update(raw_value='not_numeric',numeric_value=1.0)
    response=env['client'].post('/api/rebuild/rows/import',json=payload);assert response.status_code==200,response.text
    with env['factory']() as session:
        receipt=session.scalar(select(AuditLog).where(AuditLog.action=='rebuild_mutation_receipt',AuditLog.target_id==payload['request_id']))
        data=copy.deepcopy(receipt.payload)
        for error in data['result']['errors']:
            error.pop('row_id',None);error['field_name']=error.pop('field')
        for item in data['result']['results']:item.pop('row_index',None)
        receipt.payload=data;session.commit();env['failed_receipt']=str(receipt.id)
    return payload


def value_correction(env):
    with env['factory']() as s:
        value=s.scalar(select(RebuildDataValue));sid=value.preferred_source_id;source=s.get(RebuildValueSource,sid)
        request={'request_id':str(uuid4()),'expected_revision':value_revision(value),'correction_reason':'Synthetic original cell rereview, preserving exact reported precision',
            'raw_value':'0.0200','numeric_value':0.02,'unit':'eV','value_type':'explicit','precision_digits':4,'missing_reason':None,
            'source':{k:v for k,v in serialize_source(source).items() if k not in {'id','source_key'}}}
        request['source']['quote']='0.0200';vid=str(value.id)
    r=env['client'].put('/api/rebuild/values/'+vid,json=request);assert r.status_code==200,r.text
    return vid


def source_correction(env):
    with env['factory']() as s:
        value=s.scalar(select(RebuildDataValue));source=s.get(RebuildValueSource,value.preferred_source_id);state=source_state(s,source.id)
        fields={k:state[k] for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
        fields['label']='Synthetic independently checked table cell'
        request={'paper_id':str(env['paper']),'request_id':str(uuid4()),'expected_revision':state['source_revision'],'correction_reason':'Synthetic provenance CAS rereview','source':fields};sid=str(source.id)
    r=env['client'].put('/api/rebuild/sources/'+sid,json=request);assert r.status_code==200,r.text
    return sid


@pytest.mark.parametrize('correction',['value','source'])
def test_real_cas_recovers_legacy_error_without_row_id_only_with_unique_evidence(ready_env,correction):
    env=ready_env;legacy_failure(env)
    before=stage_read(env,'reconcile');assert before['assessment']['receipt_scope']['partial_unresolved_total']==1
    value_correction(env) if correction=='value' else source_correction(env)
    immutable=snapshot(env);files=disk(env);after=stage_read(env,'reconcile');scope=after['assessment']['receipt_scope']
    evidence('cas-recovery-'+correction,after)
    assert scope['partial_unresolved_total']==0,after
    assert scope['recovered_total']==1 and scope['cas_proved_entities']>=1,after
    assert scope['recovered_sample'][0]['proof']['kind'] in {'value_cas','import_with_source_cas'}
    assert snapshot(env)==immutable and disk(env)==files
    assert after['assessment']['scientific_acceptance']=='not_implied'


@pytest.mark.parametrize('fault',['fingerprint','before','expected_revision','time','identity','duplicate_alias','numeric','source_owner','estimated_flag','correction_notes'])
def test_wrong_or_ambiguous_recovery_stays_unknown(ready_env,fault):
    env=ready_env;legacy_failure(env);value_correction(env)
    with env['factory']() as s:
        value=s.scalar(select(RebuildDataValue));row=s.get(RebuildDataRow,value.row_id)
        if fault in {'fingerprint','before','expected_revision','time'}:
            conflict=copy.deepcopy(value.conflict);entry=conflict['correction_requests'][-1]
            if fault=='fingerprint':entry['request_fingerprint']='0'*64
            elif fault=='before':entry['before']['raw_value']='999'
            elif fault=='expected_revision':entry['expected_revision']='0'*64
            else:entry['recorded_at']='2000-01-01T00:00:00+00:00'
            value.conflict=conflict
        elif fault=='identity':row.material='Other scientific identity'
        elif fault=='duplicate_alias':
            s.add(RebuildDataRow(paper_id=env['paper'],row_key='another-row',reaction='SRR',material='other',active_site_type='dual_atom',data_type='dft',
                properties={'_rebuild_import':{'aliases':[row.row_key]}}))
        elif fault=='numeric':value.numeric_value=99
        elif fault=='estimated_flag':value.is_estimated=True
        elif fault=='correction_notes':
            conflict=copy.deepcopy(value.conflict);conflict['notes']='unmatched journal notes';value.conflict=conflict
        else:s.get(RebuildValueSource,value.preferred_source_id).paper_id=uuid4() # insert foreign paper for FK below
        if fault=='source_owner':
            foreign=Paper(id=s.get(RebuildValueSource,value.preferred_source_id).paper_id,title='Foreign fixture',pdf_path='foreign.pdf');s.add(foreign)
        s.commit()
    after=stage_read(env,'reconcile');assert after['assessment']['receipt_scope']['partial_unresolved_total']==1,after
    assert after['assessment']['program_status']=='open_items'


def test_prompt_guard_and_old_api_status_codes_preserved(ready_env):
    from app.services.ai_extract_service import _build_prompt
    from app.services.paper_processing_status import resolve_status
    env=ready_env;jobs_fixture(env)
    with env['factory']() as s:
        prompt,_=_build_prompt(s,env['paper'],env['settings']);job=s.get(WorkflowJob,'synthetic-job-0')
        state=resolve_status({'rows':1,'assets':1},job=job,now=job.updated_at)
    assert len(prompt)<=4000 and '非 DS 模型仅报告章节缺口交 DS' in prompt
    assert state['status'] in {'parsing','needs_check'} and '执行待核实' in state['label']
    assert state['job_status_as_recorded']=='running' and not state['occupancy_changed']



def test_value_cas_then_source_cas_links_new_source_without_fabricated_import_event(ready_env):
    env=ready_env;legacy_failure(env);value_correction(env);source_correction(env)
    state=stage_read(env,'reconcile');scope=state['assessment']['receipt_scope']
    assert scope['partial_unresolved_total']==0 and scope['recovered_total']==1,state
    assert scope['recovered_sample'][0]['proof']['kind']=='value_cas_with_source_chain'
    assert scope['cas_proved_entities']>=2
    evidence('combined-value-source-cas',state)


@pytest.mark.parametrize('fault',['hash','before','parent','expected'])
def test_source_journal_mismatch_never_proves_recovery(ready_env,fault):
    env=ready_env;legacy_failure(env);source_correction(env)
    with env['factory']() as s:
        value=s.scalar(select(RebuildDataValue));conflict=copy.deepcopy(value.conflict);entry=conflict['source_corrections'][0]
        if fault=='hash':entry['request_fingerprint']='0'*64
        elif fault=='before':entry['before']['quote']='forged previous original'
        elif fault=='parent':entry['after']['row_id']=str(uuid4())
        else:entry['expected_revision']='0'*64
        value.conflict=conflict;s.commit()
    state=stage_read(env,'reconcile');assert state['assessment']['receipt_scope']['partial_unresolved_total']==1,state


@pytest.mark.parametrize('fault',['request_id','timestamp','thread','turn','goal','flag'])
def test_incomplete_or_mismatched_stop_observations_are_unknown(audit_env,fault):
    env=audit_env;jobs_fixture(env)
    with env['factory']() as s:
        job=s.get(WorkflowJob,'synthetic-job-1');progress=copy.deepcopy(job.progress);stop=progress['native_stop']
        if fault=='request_id':stop.pop('request_id')
        elif fault=='timestamp':stop['last_checked_at']='invalid'
        elif fault=='thread':stop['thread_id']='other-thread'
        elif fault=='turn':stop['start_turn_id']='other-turn'
        elif fault=='goal':stop['observed_goal_status']='active'
        else:stop['verified_stop']='true'
        job.progress=progress;s.commit()
    item=next(j for j in get(env,'attempts')['items'] if j['job_id']=='synthetic-job-1')
    assert item['observation']['state']=='unknown' and not item['observation']['can_unlock']


@pytest.mark.parametrize('fault',['unknown_job','wrong_link','reversed'])
def test_supersession_requires_exact_same_paper_evidence_and_order(ready_env,fault):
    env=ready_env;links=jobs_fixture(env);catalog_fixture(env,links=links)
    request=body(env,kind='attempt_supersession',old_job_id=env['jobs'][0],new_job_id=env['jobs'][-1])
    if fault=='unknown_job':request['new_job_id']='other-paper-job'
    elif fault=='reversed':request['old_job_id'],request['new_job_id']=request['new_job_id'],request['old_job_id']
    else:
        env['envelope']['attempt_links'][0]['old_run_id']='not-the-original-run';env['refresh_catalog']()
        request=body(env,kind='attempt_supersession',old_job_id=env['jobs'][0],new_job_id=env['jobs'][-1])
    before=snapshot(env);r=env['client'].put(endpoint(env),json=request);assert r.status_code==422,r.text
    assert snapshot(env)==before


@pytest.mark.parametrize('fault',['verdict','version'])
def test_explicit_original_report_fields_cannot_be_contradicted(ready_env,fault):
    env=catalog_fixture(ready_env)
    content={'paper_id':str(env['paper']),'verdict':'failed' if fault=='verdict' else 'accepted'}
    if fault=='version':content['observed_output_revision']='0'*64
    raw=json.dumps(content).encode();(env['evidence_root']/'original.json').write_bytes(raw)
    env['envelope']['original']['sha256']=hashlib.sha256(raw).hexdigest();env['refresh_catalog']()
    assert not get(env)['items'][0]['usable_for_index']
    assert env['client'].put(endpoint(env),json=body(env)).status_code==422


@pytest.mark.parametrize('fault',['string_error','invalid_index','invalid_row_id','property_error'])
def test_unsupported_old_error_shapes_remain_unverified_without_crashing(ready_env,fault):
    env=ready_env;legacy_failure(env);value_correction(env)
    with env['factory']() as s:
        receipt=s.get(AuditLog,UUID(env['failed_receipt']));data=copy.deepcopy(receipt.payload)
        if fault=='string_error':data['result']['errors']=['opaque old error']
        elif fault=='invalid_index':data['result']['errors'][0]['row_index']=True
        elif fault=='invalid_row_id':data['result']['errors'][0]['row_id']=['not-a-row-id']
        else:data['result']['errors'][0]['part']='properties'
        receipt.payload=data;s.commit()
    state=stage_read(env,'reconcile');assert state['assessment']['receipt_scope']['partial_unresolved_total']==1,state



def test_non_json_or_duplicate_claims_and_nul_text_cannot_be_registered(ready_env):
    env=catalog_fixture(ready_env);request=body(env);request['note']='invalid'+chr(0)+'note';before=snapshot(env)
    assert env['client'].put(endpoint(env),json=request).status_code==422
    assert snapshot(env)==before
    (env['evidence_root']/'catalog.json').write_text('{"format":"history_catalog_v1","entries":[],"entries":[]}')
    assert get(env)['catalog_status']=='catalog_unavailable_or_invalid'



def test_same_paper_browser_fixture(ready_env):
    env=ready_env;links=jobs_fixture(env);catalog_fixture(env,links=links)
    put(env,body(env,kind='attempt_supersession',old_job_id=env['jobs'][0],new_job_id=env['jobs'][-1]))
    first=get(env,'attempts','&limit=2');second=env['client'].get(first['next_page']);assert second.status_code==200
    fixture={'attempts':first,'attempts_next':second.json(),'reports_current':get(env)}
    original_scope=copy.deepcopy(env['envelope']['source_scope'])
    env['envelope']['observed_output_revision']=None;env['envelope']['source_scope']=[]
    update_original(env,{'paper_id':str(env['paper']),'verdict':'accepted','observed_output_revision':None})
    fixture['reports_unknown']=get(env)
    env['envelope']['observed_output_revision']='0'*64;env['envelope']['source_scope']=original_scope
    update_original(env,{'paper_id':str(env['paper']),'verdict':'accepted','revision':'0'*64})
    fixture['reports_stale']=get(env)
    env['envelope']['observed_output_revision']=fixture['reports_current']['binding']['output_revision']
    update_original(env,{'paper_id':str(env['paper']),'revision':env['envelope']['observed_output_revision'],'status':'completed',
        'checks':[{'scientific_verdict':'accepted'}]})
    fixture['reports_operator']=get(env)
    update_original(env,'Synthetic Markdown: accepted; declared version '+env['envelope']['observed_output_revision'],'md')
    fixture['reports_text']=get(env)
    env['envelope']['historical_verdict']='PASS_WITH_LIMITATIONS'
    update_original(env,{'paper_id':str(env['paper']),'revision':env['envelope']['observed_output_revision'],'extraction_verdict':'PASS_WITH_LIMITATIONS'})
    fixture['reports_limited']=get(env)
    (env['evidence_root']/'catalog.json').rename(env['evidence_root']/'catalog-retained.json')
    fixture['reports_missing']=get(env)
    evidence('browser-fixture',fixture)



def test_read_cost_is_explicit_and_no_native_or_scientific_writes(ready_env):
    from time import perf_counter
    env=ready_env;catalog_fixture(env,links=jobs_fixture(env));metrics=[]
    before=snapshot(env);files=disk(env)
    for view in ('attempts','reports'):
        queries=[]
        def count(*args):queries.append(args[2])
        event.listen(env['engine'],'before_cursor_execute',count)
        started=perf_counter();response=env['client'].get(endpoint(env)+'?view='+view+'&limit=2');elapsed=perf_counter()-started
        event.remove(env['engine'],'before_cursor_execute',count)
        assert response.status_code==200,response.text
        assert len(response.json()['items'])<=2
        metrics.append({'view':view,'sql_queries':len(queries),'bytes':len(response.content),'seconds':elapsed})
    assert snapshot(env)==before and disk(env)==files
    evidence('history-read-cost',metrics)


def test_foreign_registration_record_and_fake_actor_are_rejected(ready_env):
    env=catalog_fixture(ready_env);request=body(env);receipt=put(env,request)
    with env['factory']() as session:
        other=Paper(title='Foreign record fixture',pdf_path='foreign.pdf');session.add(other);session.commit();other_id=str(other.id)
    before=snapshot(env)
    assert env['client'].get(f'/api/rebuild/papers/{other_id}/history-reconciliation/records/{receipt["record_id"]}').status_code==404
    request=body(env);request['actor']='forged'
    assert env['client'].put(endpoint(env),json=request).status_code==422
    assert snapshot(env)==before


@pytest.mark.parametrize('version_key',['revision','output_revision','observed_output_revision'])
@pytest.mark.parametrize('identity_path',['paper_id','paper.paper_id','paper.id'])
def test_supported_report_version_aliases_and_known_nested_identity(ready_env,version_key,identity_path):
    env=catalog_fixture(ready_env);document={'scientific_verdict':'accepted',version_key:env['envelope']['observed_output_revision']}
    if identity_path=='paper_id':document['paper_id']=str(env['paper'])
    else:document['paper']={identity_path.split('.')[1]:str(env['paper']),'current_code':'SYNTHETIC'}
    update_original(env,document);item=get(env)['items'][0]
    assert item['usable_for_index'] and item['comparison']=='current_version_match_only',item
    assert item['semantic_basis']=='supported_original_report_fields'
    assert item['original_identity_fields']==[identity_path] and item['original_version_fields']==[version_key]


@pytest.mark.parametrize('verdict_key',['verdict','decision','scientific_verdict','extraction_verdict'])
def test_scientific_conclusion_aliases_keep_original_raw_with_limitations(ready_env,verdict_key):
    env=catalog_fixture(ready_env);env['envelope']['historical_verdict']='pass_with_limitations'
    update_original(env,{'paper_id':str(env['paper']),'revision':env['envelope']['observed_output_revision'],verdict_key:'PASS_WITH_LIMITATIONS'})
    item=get(env)['items'][0];assert item['usable_for_index'] and item['historical_verdict_as_reported']=='PASS_WITH_LIMITATIONS'
    assert item['operator_declared_verdict']=='pass_with_limitations' and item['current_acceptance']=='not_granted'


@pytest.mark.parametrize('schema_key,schema',[('format','scientific_review_v1'),('record_type','article_acceptance_v1'),('report_type','independent_scientific_review')])
def test_scientific_status_requires_explicit_supported_report_schema(ready_env,schema_key,schema):
    env=catalog_fixture(ready_env);env['envelope']['historical_verdict']='accepted_partial'
    update_original(env,{'paper_id':str(env['paper']),schema_key:schema,'output_revision':env['envelope']['observed_output_revision'],'status':'accepted_partial'})
    item=get(env)['items'][0];assert item['usable_for_index'] and item['historical_verdict_as_reported']=='accepted_partial'
    assert item['comparison']=='current_version_match_only' and item['original_verdict_fields']==['status']


@pytest.mark.parametrize('fault',['revision_alias','nested_identity','decision_conflict','partial_status','limited_promoted','missing_version','null_version_alias'])
def test_original_alias_conflicts_rejected_or_unknown_never_promoted(ready_env,fault):
    env=catalog_fixture(ready_env);revision=env['envelope']['observed_output_revision']
    document={'paper_id':str(env['paper']),'revision':revision,'verdict':'accepted'}
    if fault=='revision_alias':document['output_revision']='0'*64
    elif fault=='nested_identity':document['paper']={'id':str(uuid4())}
    elif fault=='decision_conflict':document['decision']='partial'
    elif fault=='partial_status':document={'paper_id':str(env['paper']),'revision':revision,'status':'partial'}
    elif fault=='limited_promoted':document['verdict']='PASS_WITH_LIMITATIONS'
    elif fault=='missing_version':document.pop('revision')
    else:document['observed_output_revision']=None
    update_original(env,document);item=get(env)['items'][0]
    if fault in {'missing_version','null_version_alias'}:
        assert item['usable_for_index'] and item['comparison']=='version_unknown' and item['historical_output_revision'] is None
    else:
        assert not item['usable_for_index'] and env['client'].put(endpoint(env),json=body(env)).status_code==422


@pytest.mark.parametrize('shape',['array','nested_subcheck','task_completed','unmarked_status_accepted','no_identity','opaque_verdict','md','txt'])
def test_unknown_or_text_semantics_stay_operator_declarations(ready_env,shape):
    env=catalog_fixture(ready_env);revision=env['envelope']['observed_output_revision']
    document={'paper_id':str(env['paper']),'revision':revision}
    extension='json'
    if shape=='array':document=[{'paper_id':str(env['paper']),'verdict':'accepted','revision':revision}]
    elif shape=='nested_subcheck':document.update(checks=[{'verdict':'accepted','revision':revision}],results={'status':'accepted'})
    elif shape=='task_completed':document['status']='completed'
    elif shape=='unmarked_status_accepted':document['status']='accepted'
    elif shape=='no_identity':document={'revision':revision,'verdict':'accepted'}
    elif shape=='opaque_verdict':document['verdict']='NONSTANDARD_REPORT_OUTCOME'
    else:document='Accepted synthetic report, version '+revision;extension=shape
    update_original(env,document,extension);item=get(env)['items'][0]
    assert item['usable_for_index'] and item['semantic_basis']=='operator_declaration_only',item
    assert item['comparison']=='version_unknown' and item['historical_verdict_as_reported'] is None
    assert item['historical_output_revision'] is None and item['operator_declared_verdict']=='accepted'
    assert item['current_acceptance']=='not_granted'
    evidence('operator-only-'+shape,item)


def test_real_report_structure_fixture_root_article_and_nested_review(ready_env):
    env=catalog_fixture(ready_env);revision=env['envelope']['observed_output_revision']
    env['envelope']['historical_verdict']='PASS_WITH_LIMITATIONS'
    root_article={'at':'2026-10-18T00:00:00Z','paper_id':str(env['paper']),'paper_code':'SYNTHETIC',
        'revision':revision,'extraction_verdict':'PASS_WITH_LIMITATIONS','root_checks':{'subitem':{'status':'accepted'}},
        'independent_reports':[],'limitations':['Synthetic fixture retains original structure only.'],'formal_production_accepted_or_freeze_written':False}
    update_original(env,root_article);root_item=get(env)['items'][0]
    assert root_item['comparison']=='current_version_match_only' and root_item['historical_verdict_as_reported']=='PASS_WITH_LIMITATIONS'
    env['envelope']['historical_verdict']='accepted';env['envelope']['observed_output_revision']=None
    nested_review={'record_type':'historical_review','review_stage':'independent-review','paper':{'paper_id':str(env['paper']),'current_code':'SYNTHETIC'},
        'verdict':'accepted','verdict_basis':['Synthetic exact report shape'],'fresh_db_readback':{'revision':revision}}
    update_original(env,nested_review);nested_item=get(env)['items'][0]
    assert nested_item['historical_verdict_as_reported']=='accepted' and nested_item['comparison']=='version_unknown'
    evidence('real-shape-root-article',root_item);evidence('real-shape-nested-review',nested_item)


def test_legitimate_original_alias_resolves_only_to_its_own_indexed_row(ready_env):
    env=ready_env;legacy_failure(env);value_correction(env)
    with env['factory']() as session:
        row=session.scalar(select(RebuildDataRow));aliases=(row.properties or {}).get('_rebuild_import',{}).get('aliases',[])
        assert 'synthetic-row' in {row.row_key,*aliases}
        receipt=session.get(AuditLog,UUID(env['failed_receipt']));data=copy.deepcopy(receipt.payload)
        data['result']['errors'][0]['row_key']='synthetic-row';receipt.payload=data;session.commit()
    scope=stage_read(env,'reconcile')['assessment']['receipt_scope']
    assert scope['partial_unresolved_total']==0 and scope['recovered_total']==1,scope
    evidence('legitimate-original-alias-recovery',scope)


@pytest.mark.parametrize('fault',['error_alias','error_index','error_uuid','result_alias','result_uuid','event_uuid','result_index','boolean_result_index'])
def test_conflicting_original_row_identifiers_never_recover(ready_env,fault):
    env=ready_env;legacy_failure(env);value_correction(env)
    with env['factory']() as session:
        other=RebuildDataRow(paper_id=env['paper'],row_key='unrelated-unique-key',reaction='SRR',material='unrelated',active_site_type='dual_atom',data_type='dft')
        session.add(other);session.flush()
        receipt=session.get(AuditLog,UUID(env['failed_receipt']));data=copy.deepcopy(receipt.payload);error=data['result']['errors'][0]
        if fault=='error_alias':error['row_key']=other.row_key
        elif fault=='error_index':error['row_index']=99
        elif fault=='error_uuid':error['row_id']=str(other.id)
        elif fault=='result_alias':data['result']['results'][0]['row_key']=other.row_key
        elif fault=='result_uuid':data['result']['results'][0]['row_id']=str(other.id)
        elif fault=='result_index':data['result']['results'][0]['row_index']=99
        elif fault=='boolean_result_index':data['result']['results'][0]['row_index']=False
        else:
            event_id=data['result']['audit']['event_ids'][0]
            event=session.get(AuditLog,UUID(event_id));changed=copy.deepcopy(event.payload)
            if event.target_type=='rebuild_data_rows':changed['after']['id']=str(other.id)
            else:changed['after']['row_id']=str(other.id)
            event.payload=changed
        receipt.payload=data;session.commit()
    before=snapshot(env);files=disk(env);scope=stage_read(env,'reconcile')['assessment']['receipt_scope']
    assert scope['partial_unresolved_total']==1 and scope['recovered_total']==0,scope
    assert snapshot(env)==before and disk(env)==files



def test_real_imported_noncanonical_historical_alias_is_preserved(ready_env):
    env=ready_env;alias='actual-old-input-alias'
    prime=row_body(env);prime['rows'][0]['row_key']=alias
    response=env['client'].post('/api/rebuild/rows/import',json=prime);assert response.status_code==200,response.text
    with env['factory']() as session:
        rows=list(session.scalars(select(RebuildDataRow)));assert len(rows)==1
        assert rows[0].row_key!=alias and alias in rows[0].properties['_rebuild_import']['aliases']
    legacy_failure(env,alias=alias);value_correction(env)
    with env['factory']() as session:
        receipt=session.get(AuditLog,UUID(env['failed_receipt']));payload=copy.deepcopy(receipt.payload)
        payload['result']['errors'][0]['row_key']=alias;receipt.payload=payload;session.commit()
    scope=stage_read(env,'reconcile')['assessment']['receipt_scope']
    assert scope['partial_unresolved_total']==0 and scope['recovered_total']==1,scope
    evidence('noncanonical-historical-alias-positive',scope)
