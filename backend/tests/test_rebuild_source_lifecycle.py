"""Synthetic-only lifecycle and ordering regression; uses isolated PostgreSQL."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from test_rebuild_source_correction import fixture, snapshot, body
from test_rebuild_sections import chapter_env, body as section_body, post
from app.db.models import RebuildDataValue, RebuildValueSource, PaperSection, AuditLog
from app.schemas.rebuild import RebuildSourceLifecycleRequest, RebuildSourceCorrectionRequest
from app.services.rebuild_source_correction import deactivate_source, source_state, correct_source
from app.services.rebuild_workflow_service import serialize_value, _upsert_source, RebuildWorkflowError
from app.services.rebuild_source_lifecycle import current_conflict
from app.services.rebuild_sections import list_sections, ORDER_EVENT


def retire(s, ids, **changes):
    return RebuildSourceLifecycleRequest(**dict(paper_id=ids.paper,
        expected_revision=source_state(s, ids.source)['source_revision'], request_id=uuid4(),
        action='deactivate', reason='Wrong material evidence; retain original quote', **changes))


def test_retirement_preserves_correct_value_preferred_and_all_source_bytes(fixture):
    factory, settings, ids = fixture
    with factory() as s:
        original = s.get(RebuildValueSource, ids.source)
        value = s.get(RebuildDataValue, ids.value)
        data = {k: getattr(original,k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
        data['quote'] = 'correct table cell'
        from app.services.rebuild_workflow_service import _source_key
        good = RebuildValueSource(value_id=value.id, row_id=value.row_id, paper_id=value.paper_id, source_key=_source_key(data), **data)
        s.add(good);s.flush();value.preferred_source_id=good.id
        value.conflict={**value.conflict,'candidates':[{'raw_value':'canonical','source_ids':[str(original.id),str(good.id)]},{'raw_value':'bad','source_ids':[str(original.id)]}]}
        s.commit()
        source_bytes=[{c.name: getattr(obj,c.name) for c in RebuildValueSource.__table__.columns} for obj in (original,good)]
        science = {k:getattr(value,k) for k in ('raw_value','numeric_value','unit','value_type','missing_reason','preferred_source_id')}
        before=deepcopy(value.conflict)
        request=retire(s,ids)
        result=deactivate_source(s,ids.source,request,actor='trusted');s.commit()
        assert result['lifecycle_status']=='inactive'
        assert result['source_lifecycle'][0]['before']==result['source_lifecycle'][0]['after']
        assert result['source_lifecycle'][0]['actor']=='trusted'
        assert {k:getattr(value,k) for k in science}==science
        assert all(value.conflict[k]==v for k,v in before.items())
        assert source_bytes==[{c.name:getattr(obj,c.name) for c in RebuildValueSource.__table__.columns} for obj in (original,good)]
        projection=serialize_value(value,[original,good])
        assert [x['id'] for x in projection['sources']]==[str(good.id)]
        assert [x['id'] for x in projection['historical_sources']]==[str(original.id)]
        assert current_conflict(value)['candidates']==[{'raw_value':'canonical','source_ids':[str(good.id)]}]
        stored=snapshot(s)
        assert deactivate_source(s,ids.source,request,actor='trusted')==result
        s.commit();assert snapshot(s)==stored
        for update, actor, error in [({'reason':'changed'},'trusted','source_request_id_reused'),({},'attacker','source_request_id_reused'),({'request_id':uuid4()},'trusted','source_revision_conflict')]:
            with pytest.raises(RebuildWorkflowError,match=error):
                deactivate_source(s,ids.source,request.model_copy(update=update),actor=actor)
            s.rollback();assert snapshot(s)==stored
        req=RebuildSourceCorrectionRequest(**body(s,ids))
        with pytest.raises(RebuildWorkflowError,match='source_inactive'):
            correct_source(s,ids.source,req,settings,actor='trusted')


def test_last_source_is_unsourced_not_missing_and_cannot_reimport(fixture):
    factory,settings,ids=fixture
    with factory() as s:
        source=s.get(RebuildValueSource,ids.source);value=s.get(RebuildDataValue,ids.value)
        req=retire(s,ids);deactivate_source(s,ids.source,req,actor='trusted');s.commit()
        result=serialize_value(value,[source])
        assert result['sources']==[] and result['preferred_source_id'] is None
        assert result['evidence_status']=='no_current_sources'
        assert value.raw_value=='-1.230' and value.numeric_value==-1.23 and value.value_type=='explicit'
        data={k:getattr(source,k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
        with pytest.raises(RebuildWorkflowError,match='source_inactive'):
            _upsert_source(s,value=value,row=SimpleNamespace(id=value.row_id,paper_id=value.paper_id),data=data)
        assert source_state(s,source.id)['source_lifecycle'][0]['preferred_source_id_before']==str(source.id)


def test_lifecycle_cross_paper_actor_and_http_boundary(fixture,monkeypatch):
    from app.api import rebuild
    from app.db.session import get_db_session
    factory,settings,ids=fixture
    with factory() as s:
        req=retire(s,ids);stored=snapshot(s)
        for payload,actor,error in [(req.model_copy(update={'paper_id':ids.other}),'trusted','source_paper_mismatch'),(req,' ','source_actor_required')]:
            with pytest.raises(RebuildWorkflowError,match=error):deactivate_source(s,ids.source,payload,actor=actor)
            s.commit();assert snapshot(s)==stored
    app=FastAPI();app.include_router(rebuild.router,prefix='/api/rebuild')
    def db():
        with factory() as s:yield s
    app.dependency_overrides[get_db_session]=db
    path='/api/rebuild/sources/'+str(ids.source)+'/lifecycle'
    with TestClient(app) as client:
        data=req.model_dump(mode='json')
        assert client.post(path,json=data).status_code==401
        async def logged(*a,**kw):return SimpleNamespace(username='trusted-login',csrf='fixture-csrf')
        monkeypatch.setattr(rebuild,'require_session',logged)
        assert client.post(path,json=data).status_code==403
        client.cookies.set('litai_csrf','fixture-csrf');client.headers['X-CSRF-Token']='fixture-csrf'
        assert client.post(path,json=data,headers={'Origin':'https://evil.invalid'}).status_code==403
        assert client.post(path,json={**data,'action':'delete'}).status_code==422
        response=client.post(path,json=data);assert response.status_code==200,response.text
        assert response.json()['source_lifecycle'][0]['actor']=='trusted-login'
        assert client.post(path,json=data).status_code==200
        assert client.post(path,json={**data,'reason':'changed'}).status_code==409
        assert client.get('/api/rebuild/values/'+str(ids.value)).json()['sources']==[]
        package=client.get('/api/rebuild/papers/'+str(ids.paper)+'/ai-work-package').json()
        assert package['rows']['items'][0]['values'][0]['sources']==[]


def test_section_same_page_natural_order_and_evidence_order_cas(chapter_env):
    env=chapter_env
    for number in ['3.9','3.8','3.5','3.4']:
        b=section_body(env,key=number,request='section-'+number)
        b['sections'][0].update(section_title=number+'. Original heading',section_number=number)
        assert post(env,b).status_code==200
    with env['factory']() as s:
        items=list_sections(s,env['paper_id'])
        numbered=[x for x in items if x.get('provenance')]
        assert [x['section_number'] for x in numbered]==['3.4','3.5','3.8','3.9']
        original_rows=[(r.id,r.text,r.section_title,r.page_start,r.page_end) for r in s.scalars(select(PaperSection))]
    # Legacy remains readable and unproven; source-bound group can still be ordered.
    request={'request_id':str(uuid4()),'expected_revision':items[0]['section_order_revision'],
        'reason':'Synthetic heading locators checked', 'sections':[
            {'section_id':x['id'],'content_sha256':x['provenance']['content_sha256'],
             'sources':[{'page_number':1,'locator':'Exact heading position','quote':x['section_title']}]} for x in numbered]}
    path=f'/api/rebuild/papers/{env["paper_id"]}/sections/order'
    r=env['client'].post(path,json=request);assert r.status_code==200,r.text
    assert env['client'].post(path,json=request).json()['replayed'] is True
    assert env['client'].post(path,json={**request,'request_id':str(uuid4())}).status_code==409
    assert env['client'].post(path,json={**request,'reason':'changed'}).status_code==409
    with env['factory']() as s:
        after=list_sections(s,env['paper_id'])
        assert [x['id'] for x in after if x.get('provenance')]==[x['id'] for x in numbered]
        assert all(x['reading_order']['basis']=='source_bound_declaration' for x in after if x.get('provenance'))
        assert after[0]['section_order_revision']==r.json()['revision']
        assert original_rows==[(r.id,r.text,r.section_title,r.page_start,r.page_end) for r in s.scalars(select(PaperSection))]
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action==ORDER_EVENT))))==1
    request['request_id']=str(uuid4());request['expected_revision']=after[0]['section_order_revision']
    assert env['client'].post(path,json={**request,'sections':request['sections'][:-1]}).status_code==400
    alien=deepcopy(request);alien['sections'][0]['section_id']=str(uuid4())
    assert env['client'].post(path,json=alien).status_code==400
    wrong=deepcopy(request);wrong['sections'][0]['content_sha256']='0'*64
    assert env['client'].post(path,json=wrong).status_code==400
    wrong=deepcopy(request);wrong['sections'][0]['sources'][0]['page_number']=2
    assert env['client'].post(path,json=wrong).status_code==400


@pytest.mark.no_test_database
def test_verified_heading_typography_and_file_groups(tmp_path, monkeypatch):
    import hashlib
    import fitz
    from app.config import get_settings
    from app.services.rebuild_sections import _ordered_sections, SECTION_FIELDS
    monkeypatch.setenv('LITAI_STORAGE_ROOT', str(tmp_path));get_settings.cache_clear()
    path = tmp_path/'reading.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40,40), 'A B S T R A C T')
        page.insert_text((40,80), '1. Introduction')
        pdf.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    file = SimpleNamespace(storage_path=path.name, sha256=digest, role='main', original_filename='reading.pdf')
    def item(id, title, fid='main'):
        return {**{f:None for f in SECTION_FIELDS}, 'id':id, 'section_title':title, 'text':title,
                'page_start':1,'page_end':1,'display_status':'current',
                'provenance': {'file_id':fid, 'page_basis':'source_pdf','reading_pdf_sha256':digest}}
    abstract = item('z','Abstract');intro = item('a','1. Introduction')
    session = SimpleNamespace(scalars=lambda *a: [])
    result = _ordered_sections(session, uuid4(), [intro,abstract], {'main':file})
    assert [s['id'] for s in result] == ['z','a']  # Opposite UUID/identifier order.
    assert all(s['reading_order']['basis']=='verified_reading_heading_offset' for s in result)
    # Unnumbered/letter/Roman headings stay literal and unknown without evidence.
    a=item('z','II. Unproven',fid='unknown');b=item('a','A. Methods',fid='unknown')
    result = _ordered_sections(session,uuid4(),[a,b],{})
    assert all(s['reading_order']['basis']=='page_then_content_order_unverified' for s in result)
    unbound = [item('x','2. Legacy'), item('y','1. Legacy')]
    for chapter in unbound: chapter['provenance'] = None
    result = _ordered_sections(session, uuid4(), unbound, {})
    assert all(s['reading_order']['basis']=='page_then_content_order_unverified' for s in result)
    get_settings.cache_clear()


def test_analysis_and_quality_use_only_current_evidence(fixture):
    from app.services.analysis_readonly import AnalysisReadonlyService
    factory,settings,ids=fixture
    with factory() as s:
        before=AnalysisReadonlyService(s);before._load_rows()
        assert before.records and before.records[0]['sources']
        deactivate_source(s,ids.source,retire(s,ids),actor='trusted');s.commit()
        after=AnalysisReadonlyService(s);after._load_rows()
        assert after.records and after.records[0]['sources']==[]
        assert after.records[0]['evidence_text']==''


def test_lifecycle_concurrent_cas_and_rollback(fixture,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    factory,settings,ids=fixture
    with factory() as s: request=retire(s,ids)
    barrier=Barrier(2)
    def write(actor):
        with factory() as s:
            barrier.wait()
            try:
                deactivate_source(s,ids.source,request.model_copy(update={'request_id':uuid4()}),actor=actor)
                s.commit();return 'ok'
            except RebuildWorkflowError as exc:s.rollback();return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(write,['first','second']))
    assert sorted(results)==['ok','source_revision_conflict']
    with factory() as s:
        assert len(source_state(s,ids.source)['source_lifecycle'])==1


def test_lifecycle_failure_after_flush_rolls_back(fixture,monkeypatch):
    factory,settings,ids=fixture
    with factory() as s:
        request=retire(s,ids);before=snapshot(s);flush=s.flush
        def fail(*a,**kw):flush(*a,**kw);raise RuntimeError('injected after SQL')
        monkeypatch.setattr(s,'flush',fail)
        with pytest.raises(RuntimeError,match='injected after SQL'):
            deactivate_source(s,ids.source,request,actor='trusted')
        s.rollback();monkeypatch.setattr(s,'flush',flush)
        assert snapshot(s)==before


def test_retired_sources_do_not_satisfy_quality_or_coverage(fixture):
    from app.services.rebuild_quality import integrity_report
    from app.services.extraction_coverage import _graph, _item_problems
    factory,settings,ids=fixture
    with factory() as s:
        value=s.get(RebuildDataValue,ids.value);source=s.get(RebuildValueSource,ids.source)
        deactivate_source(s,ids.source,retire(s,ids),actor='trusted');s.commit()
        report=integrity_report(s,ids.paper,settings,limit=None)
        assert any(i['code']=='value_source_missing' and i['id']==str(value.id) for i in report['issues'])
        # Re-evaluate existing linkage without changing any coverage-plan record.
        item={'state':'extracted','kind':'value','origin':'reported','identity':{},'condition':{},
              'source':{'file_id':str(source.file_id),'pages':[source.page_number]},
              'link':{'row_id':str(value.row_id),'value_id':str(value.id),'source_id':str(source.id)},
              'field_name':value.field_name}
        problems=_item_problems(item,_graph(s,ids.paper),{}, {})
        assert 'value_source_file_page_or_cell_mismatch' in problems
        assert 'value_evidence_pages_incomplete' in problems


def test_dedup_does_not_move_retired_evidence(fixture):
    from app.services.rebuild_value_dedup import _preview, _state
    from app.schemas.rebuild import RebuildDedupPreviewRequest
    factory,settings,ids=fixture
    with factory() as s:
        value=s.get(RebuildDataValue,ids.value)
        deactivate_source(s,ids.source,retire(s,ids),actor='trusted');s.commit()
        state=_state(s,ids.paper)
        other=SimpleNamespace(id=uuid4(),row_id=value.row_id,conflict={})
        state[RebuildDataValue].append(other)
        request=RebuildDedupPreviewRequest(pairs=[dict(keep_value_id=value.id,duplicate_value_id=other.id,
            same_fact=True,reason='synthetic test',identity_assertion='synthetic same identity')])
        with pytest.raises(RebuildWorkflowError,match='source_lifecycle_requires_explicit_review_before_dedup'):
            _preview(s,ids.paper,request,state,[])


def test_order_auth_and_actor_replay(chapter_env):
    from app.main import app
    from app.services.rebuild_sections import declare_section_order
    from app.schemas.rebuild import RebuildSectionOrderRequest
    env=chapter_env
    assert post(env,section_body(env)).status_code==200
    with env['factory']() as s:
        items=list_sections(s,env['paper_id']);bound=[x for x in items if x.get('provenance')]
    request={'request_id':str(uuid4()),'expected_revision':items[0]['section_order_revision'],
             'reason':'synthetic order', 'sections':[{'section_id':x['id'],'content_sha256':x['provenance']['content_sha256'],
             'sources':[{'page_number':1,'locator':'heading','quote':'Original heading'}]} for x in bound]}
    path=f'/api/rebuild/papers/{env["paper_id"]}/sections/order'
    with TestClient(app) as anonymous:assert anonymous.post(path,json=request).status_code==401
    csrf=env['client'].headers.pop('X-CSRF-Token')
    assert env['client'].post(path,json=request).status_code==403
    env['client'].headers['X-CSRF-Token']=csrf
    assert env['client'].post(path,json=request,headers={'Origin':'https://evil.invalid'}).status_code==403
    assert env['client'].post(path,json=request).status_code==200
    with env['factory']() as s:
        with pytest.raises(RebuildWorkflowError,match='section_order_request_id_reused'):
            declare_section_order(s,env['paper_id'],RebuildSectionOrderRequest(**request),actor='other-authorized-user')


def test_legacy_analysis_excludes_last_retired_source(fixture):
    from app.schemas.rebuild import RebuildAnalysisRequest
    from app.services.rebuild_workflow_service import analyze
    factory,settings,ids=fixture
    request=RebuildAnalysisRequest(paper_id=ids.paper,x_field='adsorption_energy_ev',y_field='adsorption_energy_ev')
    with factory() as s:
        assert len(analyze(s,request)['points'])==1
        deactivate_source(s,ids.source,retire(s,ids),actor='trusted');s.commit()
        assert analyze(s,request)['points']==[]


@pytest.mark.parametrize('original_conflict,eligible',[
    (None,True), ({},True),
    ({'status':'value_conflict','candidates':[{'raw_value':'-9.000','source_ids':[]}]},False),
    ({'unknown_legacy_disagreement':{'raw_value':'-9.000'}},False),
    ({'status':'evidence_history'},True),
    ({'status':'corrected_previous_retained'},True),
    ({'status':'unit_conflict'},False),
    ({'status':'evidence_history','legacy_unresolved':'value_conflict'},False),
])
def test_r2_lifecycle_audit_does_not_create_scientific_conflict(fixture,original_conflict,eligible):
    from app.services.analysis_readonly import AnalysisReadonlyService
    from app.api import analysis_readonly as api
    from app.db.session import get_db_session
    from app.services.rebuild_workflow_service import _source_key
    factory,settings,ids=fixture
    with factory() as s:
        value=s.get(RebuildDataValue,ids.value);correct=s.get(RebuildValueSource,ids.source)
        value.conflict=deepcopy(original_conflict)
        data={k:getattr(correct,k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
        data['quote']='Unrelated synthetic material, -9.000'
        wrong=RebuildValueSource(value_id=value.id,row_id=value.row_id,paper_id=value.paper_id,source_key=_source_key(data),**data)
        s.add(wrong);s.commit();wrong_id=wrong.id
        science={k:getattr(value,k) for k in ('raw_value','numeric_value','unit','value_type','missing_reason','preferred_source_id')}
        before=AnalysisReadonlyService(s);before._load_rows()
        old=next(r for r in before.records if r['record_id']==str(value.id))
        assert old['exploratory_eligible'] is eligible
    app=FastAPI();app.include_router(api.router,prefix='/api')
    def db():
        with factory() as s:yield s
    app.dependency_overrides[get_db_session]=db
    path='/api/papers/compare?paper_id='+str(ids.paper)
    with TestClient(app) as client:
        initial=client.get(path);assert initial.status_code==200,initial.text
        assert initial.json()['items'][0]['exploratory_eligible'] is eligible
        with factory() as s:
            req=RebuildSourceLifecycleRequest(paper_id=ids.paper,request_id=uuid4(),action='deactivate',
                expected_revision=source_state(s,wrong_id)['source_revision'],reason='Retire unrelated synthetic source')
            deactivate_source(s,wrong_id,req,actor='trusted');s.commit()
        response=client.get(path);assert response.status_code==200,response.text
        item=response.json()['items'][0]
        assert item['exploratory_eligible'] is eligible
        assert item['exclusion_reason']==(None if eligible else 'unresolved_conflict')
        assert [str(x['id']) for x in item['sources']]==[str(ids.source)]
        summary=client.get('/api/papers/analysis-summary?paper_id='+str(ids.paper))
        assert summary.status_code==200,summary.text
        assert summary.json()['exploratory_numeric_records']==int(eligible)
        dataset=client.get('/api/papers/export/dft-dataset?paper_id='+str(ids.paper))
        assert dataset.status_code==200,dataset.text
        assert len(dataset.json()['records'])==int(eligible)
        with factory() as s:
            value=s.get(RebuildDataValue,ids.value)
            after=AnalysisReadonlyService(s);after._load_rows()
            current=next(r for r in after.records if r['record_id']==str(value.id))
            assert current['exploratory_eligible'] is eligible
            assert {k:getattr(value,k) for k in science}==science
            assert {k:v for k,v in value.conflict.items() if k!='source_lifecycle'}==(original_conflict or {})
            assert 'status' not in value.conflict if not original_conflict else value.conflict.get('status')==original_conflict.get('status')
            # Last evidence remains a local exclusion, never a fabricated missing value.
            deactivate_source(s,ids.source,retire(s,ids),actor='trusted');s.commit()
        last=client.get(path).json()['items'][0]
        assert not last['exploratory_eligible']
        assert last['exclusion_reason']==('missing_source' if eligible else 'unresolved_conflict')
        assert last['raw_value']==science['raw_value'] and last['value']==science['numeric_value']


@pytest.mark.parametrize('original_conflict,expected_adsorbate',[(None,'Li2S4'),({},'Li2S4'),({'status':'value_conflict'},None)])
def test_r2_adsorbate_cell_selection_ignores_only_lifecycle_audit(fixture,original_conflict,expected_adsorbate):
    from app.services.analysis_readonly import AnalysisReadonlyService
    from app.services.rebuild_workflow_service import _source_key
    factory,settings,ids=fixture
    with factory() as s:
        numeric=s.get(RebuildDataValue,ids.value);numeric.conflict=None
        source=s.get(RebuildValueSource,ids.source)
        adsorbate=RebuildDataValue(paper_id=numeric.paper_id,row_id=numeric.row_id,field_name='adsorbate',raw_value='Li2S4',
            value_type='explicit',numeric_value=None,is_estimated=False,conflict=deepcopy(original_conflict))
        s.add(adsorbate);s.flush()
        data={k:getattr(source,k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
        data['quote']='Synthetic adsorbate Li2S4'
        correct=RebuildValueSource(value_id=adsorbate.id,row_id=adsorbate.row_id,paper_id=adsorbate.paper_id,source_key=_source_key(data),**data)
        data={**data,'quote':'Unrelated synthetic adsorbate Li2S6'}
        wrong=RebuildValueSource(value_id=adsorbate.id,row_id=adsorbate.row_id,paper_id=adsorbate.paper_id,source_key=_source_key(data),**data)
        s.add_all([correct,wrong]);s.flush();adsorbate.preferred_source_id=correct.id;s.commit()
        before=AnalysisReadonlyService(s);before._load_rows()
        assert next(r for r in before.records if r['record_id']==str(numeric.id))['adsorbate']==expected_adsorbate
        req=RebuildSourceLifecycleRequest(paper_id=ids.paper,request_id=uuid4(),action='deactivate',
            expected_revision=source_state(s,wrong.id)['source_revision'],reason='Retire wrong adsorbate locator')
        deactivate_source(s,wrong.id,req,actor='trusted');s.commit()
        after=AnalysisReadonlyService(s);after._load_rows()
        assert next(r for r in after.records if r['record_id']==str(numeric.id))['adsorbate']==expected_adsorbate
        assert {k:v for k,v in adsorbate.conflict.items() if k!='source_lifecycle'}==(original_conflict or {})
        assert adsorbate.preferred_source_id==correct.id
