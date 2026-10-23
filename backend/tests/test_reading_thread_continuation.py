import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from app.db.models import WorkflowJob
from app.services import reading_thread_continuation as c
from app.services.codex_web_dispatch import ThreadStatus, CodexWebDispatchError, dispatch_agent
from test_reading_assistance import reading_env, base, body
from test_rebuild_mutation_audit import audit_env


def status(**kw):
    values = dict(thread_id='owned', turn_id='audit', turn_ids=('parse','audit'), turn_status='completed',
        thread_status='idle', goal_status='complete', goal_observed=True, model_provider='workbuddy', latest_progress=None, final_report=None)
    return ThreadStatus(**{**values, **kw})


def source(e, **kw):
    values = dict(paper_id=str(e['paper']), actor='writer', chosen_model=c.MODEL)
    with e['factory']() as s:
        s.add(WorkflowJob(job_id='registered-audit', type=c.BINDING, status='completed',
            payload={**values, **kw}, progress={'thread_id':'owned','turn_id':'audit','native_terminal_verified':True}));s.commit()


@pytest.fixture
def ready(reading_env, monkeypatch):
    source(reading_env)
    async def read(*a,**kw):return status()
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',read)
    return reading_env


def test_registered_same_paper_formal_job_replay_save_and_terminal(ready,monkeypatch):
    e=ready
    request={'request_id':'same-1','source_job_id':'registered-audit'}
    result=e['client'].post(base(e)+'/jobs',json=request);assert result.status_code==200,result.text
    j=result.json();assert j['thread_id']=='owned' and j['source_job_id']=='registered-audit'
    assert e['client'].post(base(e)+'/jobs',json=request).json()['job_id']==j['job_id']
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);assert job.payload['continuation']['turn_id']=='audit'
        assert s.scalar(select(func.count()).select_from(WorkflowJob))==2
        job.progress={**job.progress,'dispatch_started':True,'turn_id':'guide'};s.commit()
    p=body(e,j['job_id']);assert e['client'].post(base(e),json=p).status_code==200
    # Saving alone cannot complete the job, and the old audit turn cannot finish it.
    from app.services import reading_assistance as r
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);asyncio.run(r.reconcile(s,job,e['settings']));assert job.status=='dispatching'
    async def done(*a,**kw):return status(turn_id='guide',turn_ids=('parse','audit','guide'))
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',done)
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);asyncio.run(r.reconcile(s,job,e['settings']));s.commit()
        assert job.status=='completed' and job.progress['native_terminal_verified']
    # Default page also uses the registered lineage, without taking a raw thread ID.
    request={'request_id':'followup-2'}
    result=e['client'].post(base(e)+'/jobs',json=request);assert result.status_code==200,result.text
    assert result.json()['thread_id']=='owned'


@pytest.mark.parametrize('change',[{'paper_id':str(uuid4())},{'actor':'other'},{'chosen_model':'gpt-6-luna'}])
def test_cross_paper_actor_non_ds_rejected(reading_env,change):
    e=reading_env;source(e,**change)
    response=e['client'].post(base(e)+'/jobs',json={'request_id':'no','source_job_id':'registered-audit'})
    assert response.status_code==409
    with e['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==1


def test_missing_binding_and_client_claim_rejected(reading_env):
    e=reading_env
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'no','source_job_id':'missing'}).status_code==409
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'no','thread_id':'borrowed'}).status_code==422


@pytest.mark.parametrize('native',[None,status(turn_status='inProgress'),status(goal_status='active'),status(turn_id='foreign')])
def test_busy_unknown_native_reject_without_job(ready,monkeypatch,native):
    async def read(*a,**kw):return native
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',read)
    result=ready['client'].post(base(ready)+'/jobs',json={'request_id':'no'})
    assert result.status_code==409
    with ready['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==1


def thread(turns=None):
    return {'id':'owned','cwd':'/opt/AI-shujvku/literature-ai','modelProvider':'workbuddy','status':{'type':'idle'},
        'turns':turns or [{'id':'audit','status':'completed'}]}


@pytest.mark.no_test_database
@pytest.mark.parametrize('auto',[False, True])
def test_dispatch_same_id_new_turn_no_thread_start(monkeypatch, auto):
    calls=[]; reads=0
    async def rpc(ws,method,params,*args):
        nonlocal reads
        calls.append(method)
        if method=='thread/read':
            reads+=1
            return {'thread':thread([{'id':'audit','status':'completed'},{'id':'guide','status':'inProgress'}] if auto and reads==2 else None)}
        if method=='thread/resume':
            return {'thread':thread(),'cwd':'/opt/literature-ai','model':c.MODEL,'modelProvider':'workbuddy',
                'approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
        if method=='thread/goal/get' and calls.count('thread/goal/set')==0:return {'goal':{'threadId':'owned','status':'complete'}}
        if method in ('thread/goal/set','thread/goal/get'):return {'goal':{'threadId':'owned','status':'active','objective':'new goal'}}
        if method=='turn/start':return {'turn':{'id':'guide'}}
        raise AssertionError(method)
    monkeypatch.setattr(c,'_call',rpc)
    result=asyncio.run(c.dispatch(object(),{'thread_id':'owned','turn_id':'audit'},'new goal',1,lambda e:None))
    assert result.thread_id=='owned' and result.turn_id=='guide'
    assert calls.count('thread/start')==0 and calls.count('turn/start')==int(not auto)


@pytest.mark.no_test_database
@pytest.mark.parametrize('where',['thread/resume','thread/goal/set','turn/start'])
def test_unknown_dispatch_never_recreates_or_retries(monkeypatch,where):
    calls=[]
    async def rpc(ws,method,params,*args):
        calls.append(method)
        if method==where:raise CodexWebDispatchError('outcome unknown')
        if method=='thread/read':return {'thread':thread()}
        if method=='thread/resume':return {'thread':thread(),'cwd':'/opt/literature-ai','model':c.MODEL,'modelProvider':'workbuddy','approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
        if method=='thread/goal/get' and 'thread/goal/set' not in calls:return {'goal':None}
        return {'goal':{'threadId':'owned','status':'active','objective':'goal'}}
    monkeypatch.setattr(c,'_call',rpc)
    with pytest.raises(CodexWebDispatchError):asyncio.run(c.dispatch(object(),{'thread_id':'owned','turn_id':'audit'},'goal',1,lambda e:None))
    assert 'thread/start' not in calls and calls.count(where)==1


@pytest.mark.no_test_database
def test_recovery_requires_new_turn_and_exact_objective():
    job=SimpleNamespace(payload={'continuation':{'thread_id':'owned','turn_id':'audit'},'dispatch_objective':'token-goal'})
    assert c.recover_turn(job,status(goal_objective='token-goal')) is None
    new=status(turn_id='guide',turn_ids=('parse','audit','guide'),goal_objective='token-goal')
    assert c.recover_turn(job,new)=='guide'
    assert c.recover_turn(job,replace(new,goal_objective='foreign')) is None
    assert c.recover_turn(job,replace(new,turn_ids=('audit','other','guide'))) is None


@pytest.mark.no_test_database
@pytest.mark.parametrize('field,value',[('model','gpt-6-luna'),('approvalPolicy','on-request'),('sandbox',{'type':'workspaceWrite'})])
def test_resume_effective_model_permissions_cannot_be_overridden(monkeypatch,field,value):
    calls=[]
    async def rpc(ws,method,params,*args):
        calls.append(method)
        if method=='thread/read':return {'thread':thread()}
        if method=='thread/goal/get':return {'goal':None}
        if method=='thread/resume':
            result={'thread':thread(),'cwd':'/opt/literature-ai','model':c.MODEL,'modelProvider':'workbuddy','approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
            result[field]=value;return result
        raise AssertionError(method)
    monkeypatch.setattr(c,'_call',rpc)
    with pytest.raises(CodexWebDispatchError):asyncio.run(c.dispatch(object(),{'thread_id':'owned','turn_id':'audit'},'new goal',1,lambda e:None))
    assert 'thread/goal/set' not in calls and 'turn/start' not in calls and 'thread/start' not in calls


def test_local_parse_supplement_reuses_completed_guide_thread_and_replays(ready):
    e=ready;endpoint=f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs'
    request={'model':c.MODEL,'request_id':'local-supplement','retry':True,'source_job_id':'registered-audit'}
    r=e['client'].post(endpoint,json=request);assert r.status_code==200,r.text
    jid=r.json()['job_id']
    assert e['client'].post(endpoint,json=request).json()['job_id']==jid
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid)
        assert j.payload['continuation']['thread_id']=='owned' and j.progress['previous_turn_id']=='audit'
        assert j.progress['thread_id']=='owned' and not j.progress.get('turn_id')
        assert s.scalar(select(func.count()).select_from(WorkflowJob))==2


def test_parse_supplement_refuses_active_reading_and_model_switch(ready):
    e=ready;endpoint=f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs'
    r=e['client'].post(endpoint,json={'model':'gpt-6-luna','request_id':'wrong-model','retry':True})
    assert r.status_code==409 and 'same_thread_requires_deepseek' in r.text
    with e['factory']() as s:
        s.add(WorkflowJob(job_id='active-reading',type='paper_reading_assistance',status='running',payload={'paper_id':str(e['paper'])}));s.commit()
    assert e['client'].post(endpoint,json={'model':c.MODEL,'request_id':'busy','retry':True}).status_code==409


def test_parse_unknown_followup_does_not_adopt_old_audit_as_its_own(ready,monkeypatch):
    from app.services.ai_extract_service import sync_job_status
    e=ready;endpoint=f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs'
    r=e['client'].post(endpoint,json={'model':c.MODEL,'request_id':'unknown-supplement','retry':True});assert r.status_code==200,r.text
    with e['factory']() as s:
        j=s.get(WorkflowJob,r.json()['job_id']);j.progress={**j.progress,'dispatch_started':True};s.commit()
    with e['factory']() as s:
        j=s.get(WorkflowJob,r.json()['job_id']);asyncio.run(sync_job_status(s,j,e['settings']));s.commit()
        assert j.status=='dispatching' and not j.progress.get('turn_id') and j.progress['needs_check']
