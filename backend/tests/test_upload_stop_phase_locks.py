"""Real fixture HTTP contention at upload/stop phase boundaries, never real stop."""
import asyncio
import copy
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import fitz
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text, func
from sqlalchemy.orm import Session

from app.api.papers import ingestion
from app.config import get_settings
from app.db.models import Paper, PaperRelationship, RebuildPaperFile, WorkflowJob
from app.main import app
from app.services.codex_web_dispatch import ThreadStatus, _emit_event
from app.services.rebuild_native_stop import protective_stop
from app.services.rebuild_workflow_service import RebuildWorkflowError
from test_dispatch_phase_locks import JobLock, server_for, record_evidence
from test_sync_write_lock_contract import PaperLock, _seed_paper


def pdf_bytes():
    with fitz.open() as doc:
        doc.new_page().insert_text((30,40),'isolated upload fixture')
        return doc.tobytes()


class AdvisoryLock(PaperLock):
    def _hold(self):
        try:
            with Session(bind=self.engine) as session:
                session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':self.paper_id})
                self.ready.set();self.release.wait(15);session.rollback()
        except BaseException as exc:self.failure=exc;self.ready.set()


def upload_key(scope, digest):
    return int.from_bytes(hashlib.sha256((scope+':'+digest).encode()).digest()[:8],'big',signed=True)


@pytest.mark.parametrize('kind',['main','si','attach'])
def test_upload_lock_wait_keeps_health_live_and_rolls_back(setup_test_db,monkeypatch,kind):
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor',no_monitor)
    pid=_seed_paper(setup_test_db);data=pdf_bytes();digest=hashlib.sha256(data).hexdigest()
    if kind=='main':
        holder=AdvisoryLock(setup_test_db,upload_key('main:acceptance',digest))
        path='/api/papers/ingest/upload/jobs';body={'library_name':'acceptance'}
    else:
        holder=PaperLock(setup_test_db,pid);body={}
        path=f'/api/papers/{pid}/'+('supplementary/upload/jobs' if kind=='si' else 'attach-pdf/jobs')
    with server_for(app) as base,holder:
        async def scenario():
            async with httpx.AsyncClient(base_url=base,timeout=5) as client:
                start=time.perf_counter();task=asyncio.create_task(client.post(path,data=body,
                    files={'file':('fixture.pdf',data,'application/pdf')}))
                await asyncio.sleep(.15);assert not task.done()
                tick=time.perf_counter();health=await client.get('/api/health');latency=time.perf_counter()-tick
                assert health.status_code==200 and latency<.45,latency
                assert not task.done(),'health must precede blocked upload completion'
                response=await task
                return response,latency,time.perf_counter()-start
        response,latency,elapsed=asyncio.run(scenario())
        assert response.status_code==409,response.text
        assert 'paper_lock_busy_retry_same_request' in response.text
        assert .8<=elapsed<1.8,elapsed
    with Session(bind=setup_test_db) as session:
        session.execute(text("SET LOCAL lock_timeout='250ms'"))
        assert session.get(Paper,pid,with_for_update=True)
        for model in (WorkflowJob,RebuildPaperFile,PaperRelationship):
            assert session.scalar(select(func.count()).select_from(model))==0
        session.rollback()
    assert not list(get_settings().storage_root.rglob('*.pdf'))
    record_evidence({'phase':'upload_'+kind,'health_seconds':latency,'conflict_seconds':elapsed,'status':409})


@pytest.mark.parametrize('phase_name',['intent','journal','final'])
def test_stop_lock_wait_keeps_health_live_and_never_sends_native(setup_test_db,monkeypatch,phase_name):
    jid='stop-'+uuid4().hex
    with Session(bind=setup_test_db) as session:
        session.add(WorkflowJob(job_id=jid,type='ai_extract_rebuild',status='running',
            payload={'attempt_token':'fixture-token'},progress={'thread_id':'owned','turn_id':'turn',
                'dispatch_mode':'codex_web_target'}));session.commit()
    entered=threading.Event();proceed=threading.Event();sent=[];done=threading.Event()
    async def read(tid,**kwargs):
        if phase_name=='final':
            entered.set()
            while not proceed.is_set():await asyncio.sleep(.005)
        return ThreadStatus('interrupted' if phase_name=='final' else 'inProgress',
            'paused' if phase_name=='final' else 'active',None,None,
            turn_id='turn',thread_id=tid,turn_ids=('turn',))
    async def command(*args,**kwargs):
        entered.set()
        while not proceed.is_set():await asyncio.sleep(.005)
        await _emit_event(kwargs['on_event'],{'method':'thread/goal/set','phase':'request_prepared'})
        sent.append('must not send after blocked journal')
    probe=FastAPI()
    @probe.get('/api/health')
    async def health():return {'ok':True}
    @probe.post('/stop')
    async def stop():
        try:
            return await protective_stop(setup_test_db.url.render_as_string(hide_password=False),jid,
                settings=SimpleNamespace(rebuild_native_stop_enabled=True),native_read=read,native_command=command)
        except RebuildWorkflowError as exc:raise HTTPException(409,str(exc))
        finally:done.set()
    def snapshot():
        with Session(bind=setup_test_db) as session:
            job=session.get(WorkflowJob,jid);return copy.deepcopy((job.status,job.payload,job.progress))
    with server_for(probe) as base:
        async def scenario():
            async with httpx.AsyncClient(base_url=base,timeout=5) as client:
                holder=JobLock(setup_test_db,jid)
                if phase_name=='intent':holder.__enter__()
                try:
                    task=asyncio.create_task(client.post('/stop'))
                    if phase_name!='intent':
                        assert await asyncio.to_thread(entered.wait,3)
                        await asyncio.to_thread(holder.__enter__)  # no Job lock spans native read/command await
                    before=snapshot();proceed.set();start=time.perf_counter()
                    await asyncio.sleep(.15);assert not done.is_set()
                    tick=time.perf_counter();health=await client.get('/api/health');latency=time.perf_counter()-tick
                    assert health.status_code==200 and latency<.45,latency
                    assert not done.is_set(),'health must finish before stop phase exits'
                    response=await task;elapsed=time.perf_counter()-start
                    assert response.status_code==409,response.text
                    assert 'paper_lock_busy_retry_same_request' in response.text
                    assert .8<=elapsed<2.8,elapsed
                    assert snapshot()==before
                finally:holder.__exit__(None,None,None)
                return latency,elapsed
        latency,elapsed=asyncio.run(scenario())
    assert not sent
    with Session(bind=setup_test_db) as session:
        session.execute(text("SET LOCAL lock_timeout='250ms'"));assert session.get(WorkflowJob,jid,with_for_update=True)
        session.rollback()
    record_evidence({'phase':'stop_'+phase_name,'health_seconds':latency,'conflict_seconds':elapsed,'native_sends':0})


def test_upload_staging_has_no_transaction_and_concurrent_duplicate_is_unique(setup_test_db,monkeypatch):
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor',no_monitor)
    original=ingestion._stage_uploaded_pdf;entered=threading.Event();release=threading.Event()
    data=pdf_bytes();digest=hashlib.sha256(data).hexdigest()
    async def stage(file,settings):
        entered.set()
        while not release.is_set():await asyncio.sleep(.005)
        return await original(file,settings)
    monkeypatch.setattr(ingestion,'_stage_uploaded_pdf',stage)
    with server_for(app) as base:
        async def scenario():
            async with httpx.AsyncClient(base_url=base,timeout=6) as client:
                def upload():return client.post('/api/papers/ingest/upload/jobs',
                    data={'library_name':'race'},files={'file':('fixture.pdf',data,'application/pdf')})
                first=asyncio.create_task(upload());assert await asyncio.to_thread(entered.wait,3)
                with Session(bind=setup_test_db) as session:
                    session.execute(text("SET LOCAL lock_timeout='250ms'"))
                    session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':upload_key('main:race',digest)})
                    assert session.scalar(select(func.count()).select_from(RebuildPaperFile))==0
                    session.rollback()
                second=asyncio.create_task(upload());await asyncio.sleep(.1);release.set()
                replies=await asyncio.gather(first,second)
                assert all(r.status_code==200 for r in replies),[r.text for r in replies]
                assert replies[0].json()['paper_id']==replies[1].json()['paper_id']
        asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(Paper))==1
        assert session.scalar(select(func.count()).select_from(RebuildPaperFile))==1
        assert session.scalar(select(func.count()).select_from(WorkflowJob))==1
    assert len(list(get_settings().storage_root.rglob('*.pdf')))==1


@pytest.mark.parametrize('kind',['si','attach'])
def test_upload_replay_preserves_file_and_association(setup_test_db,monkeypatch,kind):
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor',no_monitor)
    pid=_seed_paper(setup_test_db);data=pdf_bytes();digest=hashlib.sha256(data).hexdigest()
    path=f'/api/papers/{pid}/'+('supplementary/upload/jobs' if kind=='si' else 'attach-pdf/jobs')
    with server_for(app) as base:
        async def scenario():
            async with httpx.AsyncClient(base_url=base,timeout=5) as client:
                replies=[await client.post(path,files={'file':('fixture.pdf',data,'application/pdf')}) for _ in range(2)]
                assert all(r.status_code==200 for r in replies),[r.text for r in replies]
                assert replies[1].json()['duplicate'] is True
                assert all(r.json()['automatic_parsing_started'] is False for r in replies)
        asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        records=session.scalars(select(RebuildPaperFile)).all();assert len(records)==1
        assert str(records[0].paper_id)==pid and records[0].sha256==digest
        assert records[0].role==('si' if kind=='si' else 'main')
        assert session.scalar(select(func.count()).select_from(WorkflowJob))==1
        assert session.scalar(select(func.count()).select_from(PaperRelationship))==(1 if kind=='si' else 0)
    files=list(get_settings().storage_root.rglob('*.pdf'));assert len(files)==1
    assert hashlib.sha256(files[0].read_bytes()).hexdigest()==digest


def test_upload_final_identity_cas_rejects_changes_during_staging(setup_test_db,monkeypatch):
    async def no_monitor(settings):await asyncio.Event().wait()
    monkeypatch.setattr('app.services.ordinary_parsing.monitor',no_monitor)
    pid=_seed_paper(setup_test_db);original=ingestion._stage_uploaded_pdf
    async def changed(file,settings):
        with Session(bind=setup_test_db) as session:
            session.get(Paper,pid).pdf_path='changed-source.pdf';session.commit()
        return await original(file,settings)
    monkeypatch.setattr(ingestion,'_stage_uploaded_pdf',changed)
    with server_for(app) as base:
        response=httpx.post(base+f'/api/papers/{pid}/supplementary/upload/jobs',
            files={'file':('fixture.pdf',pdf_bytes(),'application/pdf')})
        assert response.status_code==409 and 'upload_target_changed_refresh' in response.text
    with Session(bind=setup_test_db) as session:
        assert session.get(Paper,pid).pdf_path=='changed-source.pdf'
        assert session.scalar(select(func.count()).select_from(RebuildPaperFile))==0
        assert session.scalar(select(func.count()).select_from(WorkflowJob))==0
    assert not list(get_settings().storage_root.rglob('*.pdf'))


def test_cancel_staging_removes_only_owned_incomplete_file(setup_test_db):
    from io import BytesIO
    from fastapi import UploadFile
    class SlowUpload(UploadFile):
        async def read(self,size=-1):
            if self.file.tell():
                entered.set();await asyncio.Event().wait()
            return self.file.read(size)
    async def scenario():
        nonlocal entered
        entered=asyncio.Event();file=SlowUpload(filename='fixture.pdf',file=BytesIO(pdf_bytes()))
        task=asyncio.create_task(ingestion._stage_uploaded_pdf(file,get_settings()))
        await asyncio.wait_for(entered.wait(),3);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    entered=None;asyncio.run(scenario())
    assert not list(get_settings().storage_root.rglob('*.pdf'))


def test_cancel_after_upload_commit_keeps_committed_file(setup_test_db,monkeypatch):
    from io import BytesIO
    from fastapi import UploadFile
    from app.services import paper_db_phase
    data=pdf_bytes();digest=hashlib.sha256(data).hexdigest()
    async def scenario():
        committed=asyncio.Event();real=paper_db_phase.run_phase;paused=False
        async def phase(*args,**kwargs):
            nonlocal paused
            result=await real(*args,**kwargs)
            if result is not None and not paused:
                paused=True;committed.set();await asyncio.Event().wait()
            return result
        monkeypatch.setattr(paper_db_phase,'run_phase',phase)
        file=UploadFile(filename='fixture.pdf',file=BytesIO(data))
        def persist(worker,staged):
            return ingestion._main_upload_phase(worker,target_library='cancel fixture',sha256=digest,
                file_size=len(data),staged_pdf=staged,filename='fixture.pdf',settings=get_settings())
        with Session(bind=setup_test_db) as descriptor:
            task=asyncio.create_task(ingestion._persist_staged_upload(descriptor,file,get_settings(),persist))
            await asyncio.wait_for(committed.wait(),3);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(RebuildPaperFile))==1
        assert session.scalar(select(func.count()).select_from(WorkflowJob))==1
    files=list(get_settings().storage_root.rglob('*.pdf'));assert len(files)==1
    assert hashlib.sha256(files[0].read_bytes()).hexdigest()==digest


def test_lost_upload_commit_ack_preserves_possible_committed_source(setup_test_db,monkeypatch):
    from io import BytesIO
    from fastapi import UploadFile
    from app.services import paper_db_phase
    data=pdf_bytes();digest=hashlib.sha256(data).hexdigest();real=paper_db_phase.run_phase
    async def lost_ack(*args,**kwargs):
        if kwargs.get('after_commit') is not None:
            kwargs.pop('after_commit')
            await real(*args,**kwargs)  # commit happened, caller never saw its ACK
            raise ConnectionError('fixture commit ACK lost')
        return await real(*args,**kwargs)
    monkeypatch.setattr(paper_db_phase,'run_phase',lost_ack)
    async def scenario():
        def persist(worker,staged):
            return ingestion._main_upload_phase(worker,target_library='lost ACK fixture',sha256=digest,
                file_size=len(data),staged_pdf=staged,filename='fixture.pdf',settings=get_settings())
        with Session(bind=setup_test_db) as descriptor:
            with pytest.raises(ConnectionError,match='ACK lost'):
                await ingestion._persist_staged_upload(descriptor,
                    UploadFile(filename='fixture.pdf',file=BytesIO(data)),get_settings(),persist)
    asyncio.run(scenario())
    with Session(bind=setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(RebuildPaperFile))==1
    assert len(list(get_settings().storage_root.rglob('*.pdf')))==1
