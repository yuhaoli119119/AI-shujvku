from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4
import hashlib
import json

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.models import Paper, RebuildDataValue, RebuildValueSource, RebuildPaperFile, RebuildVisualAsset
from app.schemas.rebuild import RebuildDataRowInput, RebuildSourceCorrectionRequest
from app.services.rebuild_workflow_service import import_data_row, RebuildWorkflowError, _source_key, serialize_source
from app.services.rebuild_source_correction import correct_source, source_state


@pytest.fixture
def fixture(setup_test_db, tmp_path, monkeypatch):
    # Import and source correction now share actual registered-PDF checks.
    # Keep both services on the same synthetic storage root.
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    factory = sessionmaker(bind=setup_test_db, expire_on_commit=False)
    doc = fitz.open(); doc.new_page(); doc.new_page(); doc.save(tmp_path / 'original.pdf'); doc.close()
    content = (tmp_path / 'original.pdf').read_bytes()
    settings = Settings(storage_root=tmp_path)
    with factory() as s:
        papers = [Paper(title='Source fixture', pdf_path=''), Paper(title='Other paper', pdf_path='')]
        s.add_all(papers); s.flush()
        files = [RebuildPaperFile(paper_id=p.id, role='main', storage_path='original.pdf',
                  original_filename='original.pdf', sha256=hashlib.sha256(content).hexdigest(), file_size=len(content)) for p in papers]
        files.append(RebuildPaperFile(paper_id=papers[0].id, role='si', storage_path='original.pdf',
                  original_filename='si.pdf', sha256=hashlib.sha256(content).hexdigest(), file_size=len(content)))
        s.add_all(files); s.flush()
        assets = [RebuildVisualAsset(paper_id=f.paper_id, file_id=f.id, asset_key='table-'+str(f.id), asset_type='table', page_numbers=[2]) for f in files]
        s.add_all(assets); s.flush()
        import_data_row(s, papers[0].id, RebuildDataRowInput(material='Fe-N4', reaction='HER', data_type='dft',
            active_site_type='single_atom', values=[dict(field_name='adsorption_energy_ev', raw_value='-1.230',
            numeric_value=-1.23, unit='eV', precision_digits=3, sources=[dict(file_id=files[0].id,
            source_kind='table', page_number=2, label='Table S2', quote='-1.230')])]))
        s.flush(); value = s.scalar(select(RebuildDataValue)); source = s.scalar(select(RebuildValueSource))
        value.conflict = {'history': [{'legacy': 'keep'}], 'candidates': [{'raw_value': 'old'}], 'notes': 'keep'}
        s.commit()
        ids = SimpleNamespace(paper=papers[0].id, other=papers[1].id, source=source.id, value=value.id,
                              files=[f.id for f in files], assets=[a.id for a in assets])
    return factory, settings, ids


def body(s, ids, **changes):
    state = source_state(s, ids.source)
    source = {k: state[k] for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
    source.update(asset_id=str(ids.assets[0]), table_row=0, table_column=1)
    source.update(changes)
    return dict(paper_id=str(ids.paper), expected_revision=state['source_revision'], request_id=str(uuid4()),
                correction_reason='Verified original table locator', source=source)


def snapshot(s):
    return [[{c.name: json.dumps(getattr(row,c.name),sort_keys=True,default=str) for c in model.__table__.columns} for row in s.scalars(select(model).order_by(model.id))]
            for model in (RebuildDataValue, RebuildValueSource)]


def test_success_history_uuid_preferred_science_replay_and_aba(fixture):
    factory, settings, ids = fixture
    with factory() as s:
        v = s.get(RebuildDataValue, ids.value)
        science = {k: getattr(v,k) for k in ('id','row_id','paper_id','field_name','raw_value','numeric_value','unit','value_type','precision_digits','is_estimated','missing_reason','preferred_source_id')}
        old_conflict = deepcopy(v.conflict)
        before = source_state(s,ids.source)
        req = RebuildSourceCorrectionRequest(**body(s,ids))
        result = correct_source(s,ids.source,req,settings,actor='trusted-login'); s.commit()
        assert {k:getattr(v,k) for k in science} == science
        assert result['id'] == before['id'] and result['value_id'] == before['value_id']
        assert result['source_key'] == _source_key(result) != before['source_key']
        assert result['source_revision'] != before['source_revision']
        assert {k:v.conflict[k] for k in old_conflict} == old_conflict
        audit = result['source_corrections'][0]
        assert audit['before'] == {k:before[k] for k in audit['before']}
        assert audit['after'] == {k:result[k] for k in audit['after']}
        assert audit['actor'] == 'trusted-login' and audit['reason'] == req.correction_reason and audit['recorded_at']
        stored = snapshot(s)
        assert correct_source(s,ids.source,req,settings,actor='trusted-login') == result
        s.commit(); assert snapshot(s) == stored
        for actor, update, error in [('other',{},'source_request_id_reused'), ('trusted-login',{'correction_reason':'changed'},'source_request_id_reused'), ('trusted-login',{'request_id':uuid4()},'source_revision_conflict')]:
            with pytest.raises(RebuildWorkflowError,match=error):
                correct_source(s,ids.source,req.model_copy(update=update),settings,actor=actor)
            s.rollback(); assert snapshot(s)==stored
        # Returning to an earlier identity must not return its old revision.
        undo=body(s,ids,asset_id=None,table_row=0,table_column=0)
        correct_source(s,ids.source,RebuildSourceCorrectionRequest(**undo),settings,actor='trusted-login');s.commit()
        assert len(source_state(s,ids.source)['source_corrections'])==2
        again=body(s,ids)
        third=correct_source(s,ids.source,RebuildSourceCorrectionRequest(**again),settings,actor='trusted-login');s.commit()
        assert third['source_key']==result['source_key']
        assert third['source_revision']!=result['source_revision']
        assert len(third['source_corrections'])==3


@pytest.mark.parametrize('change,error',[
    ({'file_id':'other_file'}, 'source_file_belongs_to_other_paper'),
    ({'asset_id':'other_asset'}, 'source_asset_belongs_to_other_paper'),
    ({'asset_id':'si_asset'}, 'source_asset_file_mismatch'),
    ({'file_id':'missing'}, 'file_not_found'),
    ({'asset_id':'missing'}, 'asset_not_found'),
    ({'table_row':None}, 'source_table_coordinates_required'),
    ({'source_kind':'text'}, 'source_coordinates_require_table_kind'),
    ({'page_number':3}, 'source_physical_page_out_of_range'),
    ({'source_kind':'estimated','table_row':None,'table_column':None}, 'source_estimate_basis_required'),
    ({'source_kind':'estimated','table_row':None,'table_column':None,'estimate_basis':'interpolation'}, 'explicit_value_requires_explicit_evidence'),
    ({'quote':'bad\x00quote'}, 'database_constraint_rejected'),
    ({'asset_id':None,'page_number':None,'label':' ','quote':' '}, 'source_requires_page_label_quote_or_asset'),
])
def test_invalid_source_zero_mutation(fixture,change,error):
    factory,settings,ids=fixture
    replacements={'other_file':ids.files[1],'other_asset':ids.assets[1],'si_asset':ids.assets[2],'missing':uuid4()}
    with factory() as s:
        stored=snapshot(s)
        req=body(s,ids,**{k:str(replacements[v]) if isinstance(v,str) and v in replacements else v for k,v in change.items()})
        with pytest.raises(RebuildWorkflowError,match=error):
            correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='tester')
        # Even a caller that commits after validation failure cannot persist a partial edit.
        s.commit(); assert snapshot(s)==stored


def test_key_collision_paper_mismatch_missing_and_rollback(fixture,monkeypatch):
    factory,settings,ids=fixture
    with factory() as s:
        req=body(s,ids)
        original=s.get(RebuildValueSource,ids.source)
        data=RebuildSourceCorrectionRequest(**req).source.model_dump()
        other=RebuildValueSource(value_id=ids.value,row_id=original.row_id,paper_id=ids.paper,source_key=_source_key(data),**data)
        s.add(other);s.commit();stored=snapshot(s)
        for source_id,changes,error in [(ids.source,{},'source_key_conflict'),(ids.source,{'paper_id':ids.other},'source_paper_mismatch'),(uuid4(),{},'source_not_found'),(ids.source,{'correction_reason':'  '},'correction_reason_required')]:
            with pytest.raises(RebuildWorkflowError,match=error):
                correct_source(s,source_id,RebuildSourceCorrectionRequest(**{**req,**changes}),settings,actor='tester')
            s.commit();assert snapshot(s)==stored
        req=RebuildSourceCorrectionRequest(**body(s,ids,quote='distinct'))
        original_flush=s.flush
        def fail(*a,**kw):
            original_flush(*a,**kw)
            raise RuntimeError('injected failure after SQL')
        monkeypatch.setattr(s,'flush',fail)
        with pytest.raises(RuntimeError,match='injected'):
            correct_source(s,ids.source,req,settings,actor='tester')
        s.rollback();monkeypatch.setattr(s,'flush',original_flush)
        assert snapshot(s)==stored


def test_original_docx_cannot_claim_derived_pdf_page(fixture):
    factory,settings,ids=fixture
    with factory() as s:
        # A real DOCX source header, never a generated reading PDF.
        import zipfile
        path=settings.storage_root/'original.docx'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('[Content_Types].xml','');z.writestr('word/document.xml','<document/>')
        f=s.get(RebuildPaperFile,ids.files[0]);f.storage_path=path.name
        f.sha256=hashlib.sha256(path.read_bytes()).hexdigest();s.commit()
        req=body(s,ids)
        stored=snapshot(s)
        with pytest.raises(RebuildWorkflowError,match='source_physical_page_requires_original_pdf'):
            correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='tester')
        s.commit();assert snapshot(s)==stored
        req['source']['page_number']=None
        assert correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='tester')['page_number'] is None
        s.commit()


def test_http_contract_auth_csrf_validation_and_actor(fixture,monkeypatch):
    from app.api import rebuild
    from app.db.session import get_db_session
    from app.config import get_settings
    factory,settings,ids=fixture
    app=FastAPI();app.include_router(rebuild.router,prefix='/api/rebuild')
    def db():
        with factory() as s:yield s
    app.dependency_overrides[get_db_session]=db
    app.dependency_overrides[get_settings]=lambda:settings
    with factory() as s:req=body(s,ids);stored=snapshot(s)
    path='/api/rebuild/sources/'+str(ids.source)
    with TestClient(app) as client:
        assert client.get(path).status_code==401
        assert client.put(path,json=req).status_code==401
        client.cookies.set('litai_session','tampered.invalid')
        assert client.get(path).status_code==401
        assert client.put(path,json=req).status_code==401
        client.cookies.clear()
        # Stub only the session verifier, retaining the route's CSRF/origin checks.
        async def logged(*args,**kwargs):return SimpleNamespace(username='verified-login',csrf='test-token')
        monkeypatch.setattr(rebuild,'require_session',logged)
        assert client.get(path).status_code==200
        assert client.put(path,json=req).status_code==403
        client.cookies.set('litai_csrf','test-token');client.headers['X-CSRF-Token']='test-token'
        assert client.put(path,json=req,headers={'Origin':'https://evil.invalid'}).status_code==403
        for change in ({'table_row':-1},{'table_column':True},{'page_number':1.5},{'source_kind':'bogus'},{'numeric_value':10}):
            assert client.put(path,json={**req,'source':{**req['source'],**change}}).status_code==422
        with factory() as s:assert snapshot(s)==stored
        response=client.put(path,json=req);assert response.status_code==200,response.text
        assert response.json()['source_corrections'][0]['actor']=='verified-login'
        assert client.put(path,json={**req,'request_id':str(uuid4())}).status_code==409
        assert client.put(path,json=req).status_code==200
        package=client.get('/api/rebuild/papers/'+str(ids.paper)+'/ai-work-package')
        assert package.status_code==200,package.text
        assert package.json()['write_endpoints']['source_correction']== '/api/rebuild/sources/{source_id}'
        assert package.json()['source_correction_contract']['read'].startswith('GET')


def test_concurrent_cas_only_one_writer_wins(fixture):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    factory,settings,ids=fixture
    with factory() as s:
        req=body(s,ids)
    gate=Barrier(2)
    def run(label):
        with factory() as s:
            payload=RebuildSourceCorrectionRequest(**{**req,'request_id':str(uuid4()),'source':{**req['source'],'quote':label}})
            gate.wait()
            try:
                correct_source(s,ids.source,payload,settings,actor=label);s.commit();return 'ok'
            except RebuildWorkflowError as exc:
                s.rollback();return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,['first','second']))
    assert sorted(results)==['ok','source_revision_conflict']
    with factory() as s:
        assert len(source_state(s,ids.source)['source_corrections'])==1


def test_file_kind_quote_and_physical_identity_all_recomputed(fixture):
    factory,settings,ids=fixture
    with factory() as s:
        before=source_state(s,ids.source)
        req=body(s,ids,file_id=str(ids.files[2]),asset_id=str(ids.assets[2]),page_number=1,
                 source_kind='figure',table_row=None,table_column=None,label='Fig. S1',quote='original caption',estimate_basis=None)
        result=correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='tester');s.commit()
        assert result['source_key']==_source_key(req['source']) != before['source_key']
        assert result['file_id']==str(ids.files[2]) and result['asset_id']==str(ids.assets[2])
        assert result['source_kind']=='figure' and result['page_number']==1
        assert result['source_corrections'][0]['before']['source_key']==before['source_key']
        assert result['id']==before['id'] and result['preferred_source_id']==before['preferred_source_id']
