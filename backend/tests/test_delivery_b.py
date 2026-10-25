"""B cache and source-preserving transport contracts, synthetic artifacts only."""
from types import SimpleNamespace
import hashlib
import pytest
from app.services.rebuild_file_preview import install_derived_pdf, resolve_reading_pdf, preview_provenance, resolve_versioned_preview
from app.services.rebuild_workflow_service import RebuildWorkflowError
from test_rebuild_file_preview import _docx, _pdf, _record
from test_rebuild_mutation_audit import audit_env

@pytest.mark.no_test_database
def test_converter_versions_leave_old_reading_and_original_untouched(tmp_path):
    root=tmp_path/'storage';root.mkdir();settings=SimpleNamespace(storage_root=root)
    record=_record(root,'si.docx',_docx(tmp_path/'si.docx'))
    pdf=tmp_path/'old.pdf';_pdf(pdf)
    old=install_derived_pdf(record,settings,pdf);old_bytes=old.read_bytes()
    assert preview_provenance(record,settings)['status']=='legacy_converter_unknown'
    v1=install_derived_pdf(record,settings,pdf,converter={'name':'fixture-converter','version':'1'})
    assert install_derived_pdf(record,settings,pdf,converter={'name':'fixture-converter','version':'1'})==v1
    v2=install_derived_pdf(record,settings,pdf,converter={'name':'fixture-converter','version':'2'})
    assert v1!=v2 and v1.read_bytes()==v2.read_bytes()==old_bytes
    assert resolve_versioned_preview(record,settings,{'name':'fixture-converter','version':'1'})==v1
    assert resolve_reading_pdf(record,settings)==old
    v1.write_bytes(b'corrupted')
    with pytest.raises(RebuildWorkflowError,match='versioned_preview_unavailable_or_corrupt'):
        resolve_versioned_preview(record,settings,{'name':'fixture-converter','version':'1'})
    with pytest.raises(RebuildWorkflowError,match='immutable_cache_content_mismatch'):
        install_derived_pdf(record,settings,pdf,converter={'name':'fixture-converter','version':'1'})
    assert resolve_reading_pdf(record,settings).read_bytes()==old_bytes
    assert v2.read_bytes()==old_bytes


def test_bounded_checkpoint_page_does_not_fetch_unrelated_asset_bodies(audit_env):
    from sqlalchemy import event
    from app.db.models import RebuildVisualAsset
    env=audit_env
    with env['factory']() as session:
        session.add_all([RebuildVisualAsset(paper_id=env['paper'],file_id=env['file'],asset_key=f'large-{i:04d}',
            asset_type='figure',explanation='preserved scientific explanation '*2000) for i in range(300)])
        session.commit()
    queries=[]
    def capture(conn,cursor,statement,params,*args):
        queries.append((statement,params))
    event.listen(env['engine'],'before_cursor_execute',capture)
    try:
        response=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/delivery-package?view=checkpoints&limit=10')
    finally:
        event.remove(env['engine'],'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    data=response.json()
    assert len(data['items'])==10 and data['total']==302 and data['has_more']
    assert len(response.content)<50000
    # Snapshot hashes rows inside PostgreSQL; ORM bodies must use page ID filters.
    bodies=[s for s,p in queries if 'rebuild_visual_assets.explanation' in s]
    assert bodies and all('rebuild_visual_assets.id IN' in s or
                          ('rebuild_visual_assets.asset_type =' in s and p.get('asset_type_1')=='table')
                          for s,p in queries if 'rebuild_visual_assets.explanation' in s),bodies
    assert len(queries)<65,len(queries)


def test_new_si_adds_uncovered_scope_without_staling_saved_guide(audit_env,monkeypatch):
    import copy
    from app.db.models import Paper,RebuildPaperFile
    from test_reading_assistance import body
    env=audit_env
    async def no_dispatch(**kwargs):pass
    monkeypatch.setattr('app.services.ai_extract_service._background_dispatch',no_dispatch)
    with env['factory']() as session:
        session.get(Paper,env['paper']).abstract='Synthetic research abstract.';session.commit()
    base=f'/api/rebuild/papers/{env["paper"]}/reading-assistance'
    job=env['client'].post(base+'/jobs',json={'request_id':'b-si-reading','model':'cn:deepseek-v4.1-flash'}).json()
    response=env['client'].post(base,json=body(env,job['job_id']))
    assert response.status_code==200,response.text
    endpoint=f'/api/rebuild/papers/{env["paper"]}/delivery-package'
    before=env['client'].get(endpoint).json()
    with env['factory']() as session:
        original=session.get(RebuildPaperFile,env['file'])
        path=env['settings'].storage_root/'new-si.pdf';path.write_bytes((env['settings'].storage_root/original.storage_path).read_bytes()+b'\nnew SI fixture')
        file=RebuildPaperFile(paper_id=env['paper'],role='si',storage_path=path.name,original_filename=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),file_size=path.stat().st_size)
        session.add(file);session.commit();fid=str(file.id)
    after=env['client'].get(endpoint).json()
    assert after['reading']['guide_status']=='source_identity_match_only'
    assert after['reading']['guide_uncovered_file_ids']==[fid]
    assert after['reading']['guide']==before['reading']['guide']
    assert after['status_layers']['execution']['jobs_as_recorded']==before['status_layers']['execution']['jobs_as_recorded']
    assert after['status_layers']['independent_review']['status']=='pending'


def test_real_http_and_streamable_mcp_16_cross_library_delivery_reads(audit_env, monkeypatch):
    import asyncio,json,socket,threading,time
    import httpx,uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from app.main import app
    from app.config import get_settings
    from app.db.models import Paper,RebuildPaperFile,WorkflowJob
    from test_backend_concurrency import _result_data
    env=audit_env
    monkeypatch.setenv('LITAI_MCP_API_KEYS','delivery|Delivery reader|delivery-reader|read_papers')
    get_settings.cache_clear()
    papers=[]
    with env['factory']() as session:
        original=session.get(RebuildPaperFile,env['file'])
        for i in range(16):
            paper=Paper(title=f'Delivery synthetic {i}',library_name=f'Delivery library {i%2}',paper_code=f'D{i:03d}',pdf_path=original.storage_path)
            session.add(paper);session.flush()
            session.add(RebuildPaperFile(paper_id=paper.id,role='main',storage_path=original.storage_path,
                original_filename='fixture.pdf',sha256=original.sha256,file_size=original.file_size))
            session.add(WorkflowJob(job_id=f'delivery-unknown-{i}',type='rebuild_external_extract',status='running',payload={'paper_id':str(paper.id)}))
            papers.append(str(paper.id))
        session.commit()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    base=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        async def run():
            timings=[]
            async with httpx.AsyncClient(base_url=base,cookies=dict(env['client'].cookies),timeout=30) as http:
                async def one(pid):
                    async with streamablehttp_client(base+'/mcp/',headers={'Authorization':'Bearer delivery-reader'},timeout=30) as (reader,writer,_):
                        async with ClientSession(reader,writer) as mcp:
                            await mcp.initialize()
                            for view in ('summary','checkpoints','gaps','jobs','receipts'):
                                started=time.monotonic()
                                response=await http.get(f'/api/rebuild/papers/{pid}/delivery-package',params={'view':view,'limit':25})
                                assert response.status_code==200,response.text
                                body=response.json()
                                result=await mcp.call_tool('get_paper_workspace',{'request':{'paper_id':pid,'view':'delivery' if view=='summary' else 'delivery_'+view,'limit':25,'expected_revision':body['revision']}})
                                assert not result.isError,result
                                observed=_result_data(result)
                                assert observed==body
                                assert observed['status_layers']['execution']['can_unlock'] is False
                                timings.append(time.monotonic()-started)
                async def health():
                    for _ in range(12):
                        started=time.monotonic();response=await http.get('/api/health')
                        assert response.status_code==200
                        assert time.monotonic()-started<5
                        await asyncio.sleep(.05)
                await asyncio.gather(*(one(pid) for pid in papers),health())
            assert len(timings)==80
            assert max(timings)<30
        asyncio.run(run())
    finally:
        server.should_exit=True;thread.join(timeout=10)
