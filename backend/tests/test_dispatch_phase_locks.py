"""Paper association and every background dispatch DB phase leave health live.

Only isolated fixture jobs and fake native RPCs are used. The 450 ms health
bound is the original independent acceptance threshold, not a load target.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperRelationship, RebuildPaperFile, WorkflowJob
from app.main import app
from app.services import ai_extract_service as service
from app.services.codex_web_dispatch import CodexWebDispatchError, _emit_event
from test_sync_write_lock_contract import PaperLock


class JobLock(PaperLock):
    def _hold(self):
        try:
            with Session(bind=self.engine) as session:
                session.execute(select(WorkflowJob).where(
                    WorkflowJob.job_id == self.paper_id).with_for_update()).scalar_one()
                self.ready.set()
                self.release.wait(15)
                session.rollback()
        except BaseException as exc:
            self.failure = exc
            self.ready.set()


@contextmanager
def server_for(application):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port,
                                         log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start(); base = f'http://127.0.0.1:{port}'
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(base + '/api/health', timeout=.25).status_code == 200:break
            except httpx.HTTPError:time.sleep(.05)
        else:raise AssertionError('fixture Uvicorn did not start')
        yield base
    finally:
        server.should_exit = True; thread.join(10)
        assert not thread.is_alive()


def record_evidence(value):
    print(json.dumps(value))
    if destination := os.getenv('LITAI_DISPATCH_PROBE_EVIDENCE'):
        with Path(destination).open('a') as handle:handle.write(json.dumps(value) + '\n')


def test_async_relationship_paper_lock_does_not_block_health(setup_test_db, monkeypatch):
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor', no_monitor)
    with Session(bind=setup_test_db, expire_on_commit=False) as session:
        papers = [Paper(title=f'relationship fixture {i}', library_name='acceptance',
                        pdf_path=f'{uuid4()}.pdf') for i in range(2)]
        session.add_all(papers); session.commit()
        source, target = [str(p.id) for p in papers]
        before = [(str(p.id), p.paper_type, p.paper_code) for p in papers]
    with server_for(app) as base, PaperLock(setup_test_db, source):
        async def scenario():
            async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                started = time.perf_counter()
                task = asyncio.create_task(client.post(f'/api/papers/{source}/relationships',
                    json={'target_paper_id':target, 'relationship_type':'supplementary'}))
                await asyncio.sleep(.15); assert not task.done()
                tick = time.perf_counter(); health = await client.get('/api/health')
                latency = time.perf_counter() - tick
                assert health.status_code == 200
                assert latency < .45, latency
                assert not task.done(), 'health must finish before the blocked writer'
                write = await task
                return write, latency, time.perf_counter() - started
        write, latency, elapsed = asyncio.run(scenario())
        assert write.status_code == 409, write.text
        assert 'paper_lock_busy_retry_same_request' in write.text
        assert .8 <= elapsed < 1.8, elapsed
    with Session(bind=setup_test_db) as session:
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        actual = session.scalars(select(Paper).where(Paper.id.in_([source,target]))
                                 .order_by(Paper.id).with_for_update()).all()
        assert sorted((str(p.id),p.paper_type,p.paper_code) for p in actual) == sorted(before)
        assert session.scalar(select(PaperRelationship)) is None
        assert session.scalar(select(RebuildPaperFile)) is None
        session.rollback()
    record_evidence({'phase':'relationship', 'health_seconds':latency,
                     'conflict_seconds':elapsed, 'write_status':write.status_code})


@pytest.mark.parametrize('blocked_phase', ['claim', 'event', 'finish', 'failure'])
def test_background_dispatch_job_lock_does_not_block_health(setup_test_db, monkeypatch, blocked_phase):
    job_id = 'dispatch-probe-' + uuid4().hex
    token = 'correct-token'; model = 'fixture-model'
    with Session(bind=setup_test_db) as session:
        session.add(WorkflowJob(job_id=job_id, type=service.JOB_TYPE, status='dispatching',
            library_name='acceptance', payload={'attempt_token':token,'chosen_model':model},
            progress={'dispatch_started':False,'events':[]})); session.commit()
    entered = threading.Event(); proceed = threading.Event(); done = threading.Event()
    dispatched = []; native_sent = []; tasks = []; failures = []
    async def native(**kwargs):
        dispatched.append(kwargs)
        # This external await occurs after a durable claim, with no transaction.
        entered.set()
        while not proceed.is_set():await asyncio.sleep(.005)
        if blocked_phase == 'event':
            await _emit_event(kwargs['on_event'], {'method':'thread/start','phase':'request_prepared'})
            native_sent.append('must not send after failed journal')
        elif blocked_phase == 'failure':
            raise CodexWebDispatchError('fixture RPC unknown outcome')
        return SimpleNamespace(dispatch_mode='fixture',thread_id='fixture-thread',turn_id='fixture-turn',
            model=model,goal_status=None,start_effective_model=model,
            start_effective_provider='fixture',start_collaboration_verification='fixture')
    monkeypatch.setattr(service, 'dispatch_agent', native)
    probe = FastAPI()
    @probe.get('/api/health')
    async def health():return {'status':'ok'}
    async def execute():
        try:
            await service._background_dispatch(job_id=job_id,
                database_url=setup_test_db.url.render_as_string(hide_password=False),
                prompt='fixture',objective='fixture',settings=object(),primary_model=model,
                attempt_token='mismatch' if blocked_phase=='claim' else token)
        except BaseException as exc:failures.append(exc)
        finally:done.set()
    @probe.post('/start')
    async def start():
        tasks.append(asyncio.create_task(execute())); return {'started':True}
    def snapshot():
        with Session(bind=setup_test_db) as session:
            job=session.get(WorkflowJob,job_id)
            return copy.deepcopy((job.status,job.payload,job.progress,job.error))
    with server_for(probe) as base:
        async def scenario():
            async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                holder = JobLock(setup_test_db, job_id)
                if blocked_phase=='claim':holder.__enter__()
                try:
                    await client.post('/start')
                    if blocked_phase!='claim':
                        assert await asyncio.to_thread(entered.wait,3)
                        # Acquiring the Job lock here proves no lock spans native await.
                        await asyncio.to_thread(holder.__enter__)
                    before=snapshot()
                    proceed.set(); started=time.perf_counter()
                    await asyncio.sleep(.15); assert not done.is_set()
                    tick=time.perf_counter(); health=await client.get('/api/health')
                    latency=time.perf_counter()-tick
                    assert health.status_code==200
                    assert latency < .45, latency
                    assert not done.is_set(), 'health must precede background completion'
                    assert await asyncio.to_thread(done.wait,4)
                    elapsed=time.perf_counter()-started
                    assert .8 <= elapsed < 2.8, elapsed
                    assert snapshot()==before, 'failed phases must not partially update the job'
                finally:holder.__exit__(None,None,None)
                return latency,elapsed
        latency,elapsed=asyncio.run(scenario())
    assert not failures,failures
    assert len(dispatched)==(0 if blocked_phase=='claim' else 1)
    assert not native_sent
    with Session(bind=setup_test_db) as session:
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        assert session.scalar(select(WorkflowJob).where(WorkflowJob.job_id==job_id).with_for_update())
        session.rollback()
    record_evidence({'phase':blocked_phase,'health_seconds':latency,'conflict_seconds':elapsed,
                     'native_sends':len(native_sent)})


def test_background_dispatch_durable_journal_unique_claim_and_cancel(setup_test_db, monkeypatch):
    """Each callback commits before IPC continues; cancellation never replays."""
    ids = ['unique-'+uuid4().hex, 'cancel-'+uuid4().hex]
    with Session(bind=setup_test_db) as session:
        for jid in ids:
            session.add(WorkflowJob(job_id=jid,type=service.JOB_TYPE,status='dispatching',
                library_name='acceptance',payload={'attempt_token':jid,'chosen_model':'fixture'},
                progress={'dispatch_started':False,'events':[]}))
        session.commit()
    def read(jid):
        with Session(bind=setup_test_db) as session:
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            job=session.scalar(select(WorkflowJob).where(WorkflowJob.job_id==jid).with_for_update())
            return copy.deepcopy((job.status,job.progress,job.error))
    async def scenario(jid,cancel):
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def native(**kwargs):
            calls.append(kwargs)
            await _emit_event(kwargs['on_event'],{'method':'thread/start','phase':'response_received',
                'response':{'result':{'thread':{'id':'preserved-thread'}}}})
            # Short independent lock/readback proves journal durable and no IPC lock.
            saved=await asyncio.to_thread(read,jid)
            assert saved[1]['thread_id']=='preserved-thread'
            assert len(saved[1]['events'])==1
            entered.set();await release.wait()
            return SimpleNamespace(dispatch_mode='fixture',thread_id='preserved-thread',turn_id='fixture-turn',
                model='fixture',goal_status=None,start_effective_model='fixture',
                start_effective_provider='fixture',start_collaboration_verification='fixture')
        monkeypatch.setattr(service,'dispatch_agent',native)
        kwargs=dict(job_id=jid,database_url=setup_test_db.url.render_as_string(hide_password=False),
            prompt='fixture',objective='fixture',settings=object(),primary_model='fixture',attempt_token=jid)
        first=asyncio.create_task(service._background_dispatch(**kwargs))
        await asyncio.wait_for(entered.wait(),3)
        await service._background_dispatch(**kwargs)  # already claimed: do not dispatch twice
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):await first
        else:
            release.set();await first
        await service._background_dispatch(**kwargs)
        assert len(calls)==1
    asyncio.run(scenario(ids[0],False))
    normal=read(ids[0]);assert normal[0]=='running' and normal[1]['turn_id']=='fixture-turn'
    asyncio.run(scenario(ids[1],True))
    cancelled=read(ids[1]);assert cancelled[0]=='dispatching'
    assert cancelled[1]['needs_check'] and cancelled[1]['automatic_retry'] is False
    assert cancelled[1]['thread_id']=='preserved-thread' and len(cancelled[1]['events'])==1
    assert 'CancelledError' in cancelled[2]


@pytest.mark.no_test_database
def test_async_journal_is_awaited_before_rpc_send_and_reply_return():
    from app.services.codex_web_dispatch import _call, CHANNEL_FOR_VIEW
    writes=[]
    class Wire:
        async def send(self,body):
            assert writes==['request_prepared']
            outer=json.loads(body);self.outer=outer
        async def recv(self):
            rpc=self.outer['args'][0]['request']
            return json.dumps({'type':'ipc-main-event','channel':CHANNEL_FOR_VIEW,
                'args':[{'type':'mcp-response','message':{'id':rpc['id'],'result':{'ok':True}}}]})
    async def callback(event):
        await asyncio.sleep(.01)
        writes.append(event['phase'])
    result=asyncio.run(_call(Wire(),'fixture/read',{},on_event=callback))
    assert result=={'ok':True} and writes==['request_prepared','response_received']


def test_relationship_si_registration_replay_and_reverse_order(setup_test_db, monkeypatch):
    import fitz
    from app.config import get_settings
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor', no_monitor)
    storage=get_settings().storage_root
    storage.mkdir(parents=True,exist_ok=True)
    ids=[];hashes={}
    import hashlib
    with Session(bind=setup_test_db,expire_on_commit=False) as session:
        for i in range(2):
            filename=f'pair-{i}.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((30,40),f'Isolated pair source {i}')
                doc.save(storage/filename)
            hashes['main' if i==0 else 'si']=hashlib.sha256((storage/filename).read_bytes()).hexdigest()
            paper=Paper(title=f'Valid pair {i}',library_name='pair fixture',pdf_path=filename)
            session.add(paper);session.flush();ids.append(str(paper.id))
        session.commit()
    with server_for(app) as base:
        async def scenario():
            async with httpx.AsyncClient(base_url=base,timeout=5) as client:
                source,target=ids
                body={'target_paper_id':target,'relationship_type':'supplementary'}
                first=await client.post(f'/api/papers/{source}/relationships',json=body)
                assert first.status_code==200,first.text
                replay=await client.post(f'/api/papers/{source}/relationships',json=body)
                assert replay.status_code==200,replay.text
                assert first.json()['id']==replay.json()['id'] and replay.json()['status']=='existing'
                # Reverse requests encounter the same stable pair order. They may
                # be rejected by SI ownership, but cannot deadlock or remap SI.
                responses=await asyncio.gather(
                    client.post(f'/api/papers/{source}/relationships',json=body),
                    client.post(f'/api/papers/{target}/relationships',json={
                        'target_paper_id':source,'relationship_type':'supplementary'}))
                assert [r.status_code for r in responses]==[200,400],[r.text for r in responses]
        asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        relations=session.scalars(select(PaperRelationship)).all()
        assert len(relations)==1 and str(relations[0].source_paper_id)==ids[0]
        files=session.scalars(select(RebuildPaperFile)).all()
        assert len(files)==2
        assert {f.role:f.sha256 for f in files}==hashes
        assert all(str(f.paper_id)==ids[0] for f in files)


def test_cancel_after_final_commit_preserves_known_running_result(setup_test_db, monkeypatch):
    from app.services import paper_db_phase
    jid='final-cancel-'+uuid4().hex
    with Session(bind=setup_test_db) as session:
        session.add(WorkflowJob(job_id=jid,type=service.JOB_TYPE,status='dispatching',
            library_name='acceptance',payload={'attempt_token':jid,'chosen_model':'fixture'},
            progress={'dispatch_started':False,'events':[]}));session.commit()
    async def scenario():
        committed=asyncio.Event();paused=False;real_phase=paper_db_phase.run_phase
        async def phase(*args,**kwargs):
            nonlocal paused
            result=await real_phase(*args,**kwargs)
            with Session(bind=setup_test_db) as session:
                running=session.get(WorkflowJob,jid).status=='running'
            if running and not paused:
                paused=True;committed.set();await asyncio.Event().wait()
            return result
        async def native(**kwargs):
            return SimpleNamespace(dispatch_mode='fixture',thread_id='known-thread',turn_id='known-turn',
                model='fixture',goal_status=None,start_effective_model='fixture',
                start_effective_provider='fixture',start_collaboration_verification='fixture')
        monkeypatch.setattr(paper_db_phase,'run_phase',phase)
        monkeypatch.setattr(service,'dispatch_agent',native)
        task=asyncio.create_task(service._background_dispatch(job_id=jid,
            database_url=setup_test_db.url.render_as_string(hide_password=False),
            prompt='fixture',objective='fixture',settings=object(),primary_model='fixture',attempt_token=jid))
        await asyncio.wait_for(committed.wait(),3);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        job=session.get(WorkflowJob,jid)
        assert job.status=='running' and job.error is None
        assert job.progress['thread_id']=='known-thread' and job.progress['turn_id']=='known-turn'
        assert job.progress['needs_check'] is False
