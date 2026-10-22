"""Regression for real B064 goal auto-start plus >1MB native transcripts."""
import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.db.models import WorkflowJob
from app.services.codex_web_dispatch import ThreadStatus, dispatch_agent, read_thread_status
from app.services.ordinary_parsing import recover_auto_started_turn
from test_ordinary_parsing import env, start
from test_rebuild_mutation_audit import audit_env


def identity():
    payload={'parse_contract':'ordinary_parse_v1','chosen_mode':'codex_web_target','attempt_token':'token','dispatch_prompt':'own task token'}
    def event(method,result):return {'method':method,'phase':'response_received','response':{'result':result}}
    goal={'threadId':'owned','status':'active','objective':'own task token'}
    events=[event('thread/start',{'thread':{'id':'owned','turns':[]},'modelProvider':'workbuddy'}),
            {'method':'thread/goal/set','phase':'request_prepared','params':{'threadId':'owned','objective':'own task token'}},
            event('thread/goal/set',{'goal':goal}),event('thread/goal/get',{'goal':goal}),
            event('thread/read',{'thread':{'id':'owned','modelProvider':'workbuddy','turns':[{'id':'first','status':'inProgress'}]}})]
    progress={'thread_id':'owned','events':events}
    status=ThreadStatus('completed',None,None,'done',thread_id='owned',turn_id='later',turn_ids=('first','later'),
                        cwd='/fixture',model_provider='workbuddy',thread_status='idle',goal_observed=True)
    return payload,progress,status,SimpleNamespace(codex_web_cwd='/fixture')


@pytest.mark.no_test_database
def test_owned_goal_double_ack_and_live_turn_history_recover_same_identity():
    payload,progress,status,settings=identity()
    assert recover_auto_started_turn(payload,progress,status,settings)['turn_id']=='first'
    for corrupt in ('goal','start','missing_turn','turn_request','provider','cwd'):
        p=copy.deepcopy(progress);st=status
        if corrupt=='goal':p['events'][2]['response']['result']['goal']['objective']='other task'
        if corrupt=='start':p['events'][0]['response']['result']['thread']['turns']=[{'id':'foreign'}]
        if corrupt=='missing_turn':st=ThreadStatus('completed',None,None,None,thread_id='owned',turn_id='later',turn_ids=('later',),cwd='/fixture',model_provider='workbuddy')
        if corrupt=='turn_request':p['events'].append({'method':'turn/start','phase':'request_prepared'})
        if corrupt=='provider':st=ThreadStatus('completed',None,None,None,thread_id='owned',turn_id='first',turn_ids=('first',),cwd='/fixture',model_provider='other')
        if corrupt=='cwd':st=ThreadStatus('completed',None,None,None,thread_id='owned',turn_id='first',turn_ids=('first',),cwd='/other',model_provider='workbuddy')
        assert recover_auto_started_turn(payload,p,st,settings) is None,corrupt


@pytest.mark.no_test_database
def test_dispatch_adopts_goal_auto_started_turn_without_second_turn_start(monkeypatch):
    settings=SimpleNamespace(codex_web_default_model='cn:deepseek-v4.1-flash',codex_web_cwd='/fixture',codex_web_ipc_uri='ws://fixture',codex_web_dispatch_timeout_seconds=1)
    calls=[];events=[]
    @asynccontextmanager
    async def connect(*a,**kw):yield object()
    async def call(ws,method,params,*a):
        calls.append(method)
        if method=='thread/start':return {'thread':{'id':'owned','turns':[]},'model':'cn:deepseek-v4.1-flash','modelProvider':'workbuddy'}
        if method in ('thread/goal/set','thread/goal/get'):return {'goal':{'threadId':'owned','status':'active','objective':'task'}}
        if method=='thread/read':return {'thread':{'id':'owned','modelProvider':'workbuddy','status':{'type':'active'},'turns':[{'id':'auto','status':'inProgress'}]}}
        raise AssertionError('must not send '+method)
    monkeypatch.setattr('app.services.codex_web_dispatch.websockets.connect',connect)
    monkeypatch.setattr('app.services.codex_web_dispatch._call',call)
    result=asyncio.run(dispatch_agent(prompt='task',objective='task',settings=settings,on_event=events.append))
    assert result.turn_id=='auto' and 'turn/start' not in calls
    assert events[-1]['phase']=='auto_start_adopted'


@pytest.mark.no_test_database
def test_large_transcript_and_confirmed_absent_goal_select_final_answer(monkeypatch):
    settings=SimpleNamespace(codex_web_ipc_uri='ws://fixture',codex_web_dispatch_timeout_seconds=1)
    limits=[]
    @asynccontextmanager
    async def connect(*a,**kw):limits.append(kw['max_size']);yield object()
    async def call(ws,method,params,*a):
        if method=='thread/goal/get':return {'goal':None}
        return {'thread':{'id':'owned','cwd':'/fixture','modelProvider':'workbuddy','status':{'type':'idle'},'turns':[{'id':'first','status':'completed','items':[{'type':'agentMessage','phase':'commentary','text':'x'*(2*1024*1024)},{'type':'agentMessage','phase':'final_answer','text':'actual final'}]}]}}
    monkeypatch.setattr('app.services.codex_web_dispatch.websockets.connect',connect)
    monkeypatch.setattr('app.services.codex_web_dispatch._call',call)
    result=asyncio.run(read_thread_status('owned',settings))
    assert limits==[32*1024*1024] and result.final_report=='actual final'
    assert result.goal_observed and result.goal_status is None and result.thread_status=='idle'


@pytest.mark.parametrize('goal_observed,stages_processed,expected',[(True,True,'completed'),(False,True,'running'),(True,False,'running')])
def test_reconcile_existing_autostart_job_only_when_terminal_is_proven(env,monkeypatch,goal_observed,stages_processed,expected):
    from dataclasses import replace
    from app.services.ai_extract_service import sync_job_status
    jid=start(env).json()['job_id']
    payload,progress,status,_=identity();status=replace(status,cwd=env['settings'].codex_web_cwd,goal_observed=goal_observed)
    async def read(*a,**kw):return status
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',read)
    with env['factory']() as session:
        job=session.get(WorkflowJob,jid)
        job.payload={**job.payload,**payload}
        old=job.progress
        if stages_processed:
            for file in old['file_plan'].values():
                for part in file['stages'].values():part['status']='completed'
        job.progress={**old,**progress};session.commit()
    with env['factory']() as session:
        job=asyncio.run(sync_job_status(session,session.get(WorkflowJob,jid),env['settings']));session.commit()
        assert job.status==expected and job.progress['turn_id']=='first'
        assert job.progress['identity_recovery']['thread_id']=='owned'
        if expected=='completed':assert job.progress['native_terminal_verified'] and job.result['file_ids']==[str(env['file'])]
