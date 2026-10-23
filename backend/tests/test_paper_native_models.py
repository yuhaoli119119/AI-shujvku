import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from app.db.models import WorkflowJob
from app.services import paper_native_models as m, reading_thread_continuation as c
from app.services.codex_web_dispatch import ThreadStatus,CodexWebDispatchError
from test_reading_thread_continuation import reading_env, audit_env, ready, status, source, thread
from test_reading_assistance import base,body


@pytest.mark.no_test_database
@pytest.mark.parametrize('message,expected',[
 ('429 Too Many Requests',None),('RPC timeout; outcome unknown',None),('network error',None),
 ('usageLimitExceeded','quota_exhausted'),('insufficient credits','quota_exhausted'),('额度已耗尽','quota_exhausted'),
 ('quota exceeded','quota_exhausted'),('model_not_found','model_unavailable'),('model is unavailable','model_unavailable'),
 ('per minute quota exceeded',None),('rate limit exceeded',None),('500 internal server error',None)])
def test_only_quota_or_model_unavailable(message,expected):assert m.fallback_reason(message)==expected


@pytest.mark.no_test_database
def test_bounded_same_job_same_thread_fallback_does_not_restart_or_lose_checkpoints():
    j=SimpleNamespace(job_id='same-job',payload={'chosen_model':m.DEFAULT,'attempt_token':'token','dispatch_prompt':'token instruction','dispatch_objective':'token instruction'},
        progress={'thread_id':'owned','turn_id':'audit','file_plan':{'a':'completed'}},status='running',error=None)
    native=status(native_error='quota exceeded')
    assert m.stage_fallback(j,native) and j.payload['chosen_model']==m.MODELS[1]
    assert j.payload['continuation']['thread_id']=='owned' and j.payload['continuation']['turn_id']=='audit'
    assert j.progress['file_plan']=={'a':'completed'} and j.progress['turn_id'] is None
    # Re-observing the old terminal while new turn outcome is unknown cannot advance again.
    assert not m.stage_fallback(j,native)
    j.progress={**j.progress,'turn_id':'second','effective_model':m.MODELS[1]}
    native=replace(native,turn_id='second',turn_ids=('audit','second'))
    assert m.stage_fallback(j,native) and j.payload['chosen_model']==m.MODELS[2]
    j.progress={**j.progress,'turn_id':'third','effective_model':m.MODELS[2]}
    assert not m.stage_fallback(j,replace(native,turn_id='third')) and 'exhausted' in j.error
    assert len(j.progress['native_attempts'])==2


@pytest.mark.no_test_database
@pytest.mark.parametrize('change',[{'goal_status':'active'},{'turn_status':'inProgress'},{'thread_status':'active'},{'turn_id':'foreign'},{'native_error':'network error'}])
def test_fallback_waits_for_real_owned_terminal(change):
    j=SimpleNamespace(job_id='j',payload={'chosen_model':m.DEFAULT},progress={'turn_id':'audit'},error=None)
    assert not m.stage_fallback(j,status(native_error='quota exceeded',**change) if 'native_error' not in change else status(**change))


@pytest.mark.no_test_database
def test_switch_precedes_goal_auto_wake_and_effective_permissions_verified(monkeypatch):
    calls=[]
    async def rpc(ws,method,params,*args):
        calls.append((method,params))
        if method=='thread/read':return {'thread':thread()}
        if method=='thread/resume':
            assert params['model']==m.MODELS[1] and params['approvalPolicy']=='never'
            return {'thread':thread(),'cwd':'/opt/literature-ai','model':m.MODELS[1],'modelProvider':'workbuddy','approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
        if method=='thread/goal/get' and not any(x[0]=='thread/goal/set' for x in calls):return {'goal':None}
        if method in ('thread/goal/set','thread/goal/get'):return {'goal':{'threadId':'owned','status':'active','objective':'prompt'}}
        if method=='turn/start':return {'turn':{'id':'new'}}
        raise AssertionError(method)
    monkeypatch.setattr(c,'_call',rpc)
    events=[]
    result=asyncio.run(c.dispatch(None,{'thread_id':'owned','turn_id':'audit','source_model':m.DEFAULT,'target_model':m.MODELS[1],'switch_reason':'quota_exhausted'},'prompt',1,events.append))
    methods=[x[0] for x in calls]
    assert methods.index('thread/resume')<methods.index('thread/goal/set') and 'thread/start' not in methods
    assert result.thread_id=='owned' and result.model==m.MODELS[1] and result.turn_id=='new'
    assert next(e for e in events if e['phase']=='effective_verified')['approval_policy']=='never'


@pytest.mark.parametrize('model',m.MODELS[1:])
def test_formal_reading_and_parse_allow_verified_vision_models_first_task(reading_env,model):
    e=reading_env
    r=e['client'].post(base(e)+'/jobs',json={'request_id':'glm','model':model});assert r.status_code==200,r.text
    with e['factory']() as s:
        j=s.get(WorkflowJob,r.json()['job_id']);assert j.payload['chosen_model']==model
        j.status='completed';j.progress={'native_terminal_verified':True};s.commit()
    r=e['client'].post(f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs',json={'request_id':'parse-glm','model':model});assert r.status_code==200,r.text


def test_recover_existing_failed_reading_job_adopts_already_resumed_exact_goal_no_new_job(ready,monkeypatch):
    e=ready;r=e['client'].post(base(e)+'/jobs',json={'request_id':'original'});assert r.status_code==200
    jid=r.json()['job_id']
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);j.status='failed';j.progress={**j.progress,'turn_id':'interrupted','native_terminal_verified':True};objective=j.payload['dispatch_objective'];s.commit()
    async def active(*a,**kw):return status(turn_id='resumed',turn_ids=('audit','interrupted','resumed'),turn_status='inProgress',thread_status='active',goal_status='active',goal_objective=objective,cwd='/opt/literature-ai',model=m.DEFAULT)
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',active)
    request={'request_id':'resume-original'}
    result=e['client'].post(base(e)+f'/jobs/{jid}/resume',json=request);assert result.status_code==200,result.text
    assert result.json()['job_id']==jid and result.json()['turn_id']=='resumed'
    assert e['client'].post(base(e)+f'/jobs/{jid}/resume',json=request).json()['job_id']==jid
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);assert j.status=='running' and j.payload['request_id']=='original'
        assert s.scalar(select(func.count()).select_from(WorkflowJob))==2


def test_recovery_does_not_switch_model_on_manual_pause(ready,monkeypatch):
    e=ready;r=e['client'].post(base(e)+'/jobs',json={'request_id':'paused'});jid=r.json()['job_id']
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);j.status='failed';j.progress={**j.progress,'turn_id':'audit','native_terminal_verified':True};s.commit()
    async def paused(*a,**kw):return status(turn_status='interrupted',goal_status='paused',cwd='/opt/literature-ai',model=m.DEFAULT)
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',paused)
    assert e['client'].post(base(e)+f'/jobs/{jid}/resume',json={'request_id':'wrong','model':m.MODELS[1]}).status_code==409
    good=e['client'].post(base(e)+f'/jobs/{jid}/resume',json={'request_id':'right'});assert good.status_code==200,good.text
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);assert j.payload['continuation']['thread_id']=='owned' and j.payload['continuation']['resume_paused']
        assert j.payload['chosen_model']==m.DEFAULT and len(j.progress['native_attempts'])==1


def test_terminal_save_block_reuses_prepared_content_atomically_in_same_job(ready,monkeypatch):
    e=ready;r=e['client'].post(base(e)+'/jobs',json={'request_id':'original-prepared'});jid=r.json()['job_id']
    content=body(e,jid)
    original_sentence=content['one_sentence']
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);j.status='failed';j.progress={**j.progress,'turn_id':'interrupted','native_terminal_verified':True};objective=j.payload['dispatch_objective'];s.commit()
    async def ended(*a,**kw):return status(turn_id='last',turn_ids=('audit','interrupted','auto1','last'),goal_status='blocked',goal_objective=objective,cwd='/opt/literature-ai',model=m.DEFAULT,final_report='409 reading_task_finished')
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',ended)
    req={'request_id':'prepared-recovery','prepared_content':content}
    invalid=copy.deepcopy(req);invalid['prepared_content']['input_revision']='0'*64
    assert e['client'].post(base(e)+f'/jobs/{jid}/resume',json=invalid).status_code==409
    with e['factory']() as s:
        previous=s.get(WorkflowJob,jid);assert previous.status=='failed' and previous.progress['turn_id']=='interrupted'
        assert not previous.progress.get('recovery_receipts')
    result=e['client'].post(base(e)+f'/jobs/{jid}/resume',json=req);assert result.status_code==200,result.text
    assert result.json()['job_id']==jid and result.json()['status']=='completed' and result.json()['saved']
    assert e['client'].post(base(e)+f'/jobs/{jid}/resume',json=req).status_code==200
    req['prepared_content']['one_sentence']='different'
    assert e['client'].post(base(e)+f'/jobs/{jid}/resume',json=req).status_code==409
    read=e['client'].get(base(e)).json();assert read['guide']['one_sentence']==original_sentence
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);assert j.payload['request_id']=='original-prepared' and j.progress['native_terminal_verified']
        assert len(j.progress.get('events') or [])==0 and s.scalar(select(func.count()).select_from(WorkflowJob))==2


def test_parse_quota_resume_keeps_original_job_request_and_scientific_progress(reading_env,monkeypatch):
    e=reading_env;endpoint=f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs'
    r=e['client'].post(endpoint,json={'model':m.DEFAULT,'request_id':'original-parse'});assert r.status_code==200,r.text
    jid=r.json()['job_id']
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);j.status='failed';j.progress={**j.progress,'thread_id':'owned','turn_id':'audit','native_terminal_verified':True};plan=j.progress['file_plan'];s.commit()
    async def quota(*a,**kw):return status(turn_status='failed',native_error='insufficient credits',cwd='/opt/literature-ai',model=m.DEFAULT)
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',quota)
    result=e['client'].post(endpoint+f'/{jid}/resume',json={'request_id':'parse-resume','model':m.MODELS[1]})
    assert result.status_code==200,result.text
    with e['factory']() as s:
        j=s.get(WorkflowJob,jid);assert j.payload['request_id']=='original-parse' and j.payload['chosen_model']==m.MODELS[1]
        assert j.progress['file_plan']==plan and j.progress['thread_id']=='owned' and s.scalar(select(func.count()).select_from(WorkflowJob))==1


def test_reading_recovery_cannot_mutate_parse_job_via_wrong_route(reading_env):
    e=reading_env;r=e['client'].post(f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs',json={'model':m.DEFAULT,'request_id':'parse-only'})
    jid=r.json()['job_id'];result=e['client'].post(base(e)+f'/jobs/{jid}/resume',json={'request_id':'wrong-route'})
    assert result.status_code==409 and 'not_owned' in result.text
    with e['factory']() as s:assert not s.get(WorkflowJob,jid).progress.get('recovery_receipts')


@pytest.mark.no_test_database
def test_parse_can_continue_missing_stages_without_recreating_saved_reading():
    j=SimpleNamespace(type='ai_extract_rebuild',job_id='original',payload={'chosen_model':m.DEFAULT,'attempt_token':'t','dispatch_prompt':'t work','dispatch_objective':'t work'},progress={'reading_saved':True,'file_plan':{'read':'completed','rows':'partial'},'thread_id':'owned','turn_id':'audit'},error=None,status='running')
    assert m.stage_fallback(j,status(native_error='insufficient credits'))
    assert j.job_id=='original' and j.progress['reading_saved'] and j.progress['file_plan']['read']=='completed'


@pytest.mark.no_test_database
def test_native_usage_limit_code_is_verified_quota_even_when_message_is_429(monkeypatch):
    from app.services import codex_web_dispatch as d
    class Connection:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(d.websockets,'connect',lambda *a,**kw:Connection())
    async def rpc(ws,method,*a):
        if method=='thread/read':return {'thread':{'id':'owned','status':{'type':'idle'},'model':m.DEFAULT,'modelProvider':'workbuddy','turns':[{'id':'t','status':'failed','items':[],'error':{'message':'429 Too Many Requests','codexErrorInfo':'usageLimitExceeded'}}]}}
        return {'goal':{'threadId':'owned','status':'blocked'}}
    monkeypatch.setattr(d,'_call',rpc)
    r=asyncio.run(d.read_thread_status('owned',SimpleNamespace(codex_web_ipc_uri='ws://fixture',codex_web_dispatch_timeout_seconds=1)))
    assert m.fallback_reason(r.native_error)=='quota_exhausted' and r.turn_id=='t'


def test_default_parse_supplement_keeps_previously_effective_glm_model(ready,monkeypatch):
    e=ready
    with e['factory']() as s:
        j=s.get(WorkflowJob,'registered-audit');j.payload={**j.payload,'chosen_model':m.MODELS[1]};j.progress={**j.progress,'effective_model':m.MODELS[1]};s.commit()
    async def read(*a,**kw):return status(model=m.MODELS[1])
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',read)
    r=e['client'].post(f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs',json={'request_id':'default-glm-supplement','retry':True})
    assert r.status_code==200,r.text
    with e['factory']() as s:
        j=s.get(WorkflowJob,r.json()['job_id']);assert j.payload['chosen_model']==m.MODELS[1] and j.progress['thread_id']=='owned'
