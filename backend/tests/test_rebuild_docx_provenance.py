"""Isolated real PostgreSQL + filesystem + route contract for DOCX reading pages."""
import hashlib
import json
import zipfile
from types import SimpleNamespace
from uuid import uuid4
from copy import deepcopy

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from app.api import rebuild
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.db.models import (Paper, RebuildPaperFile, RebuildDataValue, RebuildValueSource,
                           RebuildVisualAsset, RebuildDataRow, AuditLog)
from app.services.rebuild_file_preview import install_derived_pdf, reading_identity
from app.services.rebuild_source_correction import source_state
from app.services.analysis_readonly import unresolved_conflict


@pytest.fixture
def env(setup_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv('LITAI_STORAGE_ROOT',str(tmp_path));get_settings.cache_clear()
    settings=Settings(storage_root=tmp_path)
    factory=sessionmaker(bind=setup_test_db,expire_on_commit=False)
    with zipfile.ZipFile(tmp_path/'source.docx','w') as z:
        z.writestr('[Content_Types].xml','<Types/>')
        z.writestr('word/document.xml','<document><p>Synthetic fixture only</p></document>')
    pdf=fitz.open();pdf.new_page().insert_text((70,70),'Synthetic derived reading page one');pdf.new_page().insert_text((70,70),'Synthetic reading page two');pdf.save(tmp_path/'reading.pdf');pdf.close()
    with factory() as s:
        papers=[Paper(title='DOCX fixture',pdf_path=''),Paper(title='Other fixture',pdf_path='')];s.add_all(papers);s.flush()
        files=[]
        for p,name in [(papers[0],'source.docx'),(papers[1],'source.docx'),(papers[0],'reading.pdf')]:
            raw=(tmp_path/name).read_bytes()
            files.append(RebuildPaperFile(paper_id=p.id,role='si',storage_path=name,original_filename=name,sha256=hashlib.sha256(raw).hexdigest(),file_size=len(raw)))
        s.add_all(files);s.flush();install_derived_pdf(files[0],settings,tmp_path/'reading.pdf')
        cache,identity=reading_identity(files[0],settings);s.commit()
        ids=SimpleNamespace(paper=str(papers[0].id),other=str(papers[1].id),file=str(files[0].id),foreign=str(files[1].id),pdf=str(files[2].id),identity=identity,cache=cache)
    app=FastAPI();app.include_router(rebuild.router,prefix='/api/rebuild')
    def db():
        with factory() as s:yield s
    app.dependency_overrides[get_db_session]=db;app.dependency_overrides[get_settings]=lambda:settings
    app.dependency_overrides[rebuild._require_section_write_session]=lambda:SimpleNamespace(username='fixture-actor',csrf='fixture-csrf')
    app.dependency_overrides[rebuild._require_identity_correction_session]=lambda:SimpleNamespace(username='fixture-actor',csrf='fixture-csrf')
    app.dependency_overrides[rebuild._require_work_package_session]=lambda:SimpleNamespace(username='fixture-actor',csrf='fixture-csrf')
    with TestClient(app) as client:
        yield client,factory,settings,ids,tmp_path
    get_settings.cache_clear()


def source(ids,**changes):
    return dict(file_id=ids.file,asset_id=None,source_kind='text',page_number=1,
                label='Synthetic XML table',table_row=None,table_column=None,quote='fixture quote',estimate_basis=None,**ids.identity)|changes


def row_request(ids,src):
    return dict(paper_id=ids.paper,request_id=str(uuid4()),rows=[dict(row_key='synthetic-row',material='Synthetic-X',reaction='HER',data_type='dft',active_site_type='single_atom',values=[dict(field_name='adsorption_energy_ev',raw_value='-1.230',numeric_value=-1.23,unit='eV',precision_digits=3,sources=[src])])])


def counts(factory):
    with factory() as s:
        return [s.scalar(select(func.count()).select_from(m)) for m in (RebuildDataRow,RebuildDataValue,RebuildValueSource,RebuildVisualAsset,AuditLog)]


def read_source(client,factory):
    with factory() as s: sid=str(s.scalar(select(RebuildValueSource.id)))
    response=client.get('/api/rebuild/sources/'+sid);assert response.status_code==200,response.text
    return sid,response.json()


def correction(ids,state,src):
    return dict(paper_id=ids.paper,request_id=str(uuid4()),expected_revision=state['source_revision'],correction_reason='Synthetic locator verification',source=src)


def test_import_preflight_correct_serialize_link_crop_and_replay(env):
    client,factory,settings,ids,root=env
    payload=row_request(ids,source(ids));before=counts(factory);tree={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    pre=client.post('/api/rebuild/rows/import/preflight',json=payload);assert pre.status_code==200,pre.text
    assert counts(factory)==before
    assert tree=={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    imp=client.post('/api/rebuild/rows/import',json=payload);assert imp.status_code==200,imp.text;assert not imp.json()['errors'],imp.text
    after=counts(factory);assert client.post('/api/rebuild/rows/import',json=payload).status_code==200;assert counts(factory)==after
    sid,state=read_source(client,factory)
    assert all(state[k]==v for k,v in ids.identity.items());assert state['page_identity_status']=='verified'
    rows=client.get('/api/rebuild/rows',params={'paper_id':ids.paper});assert rows.status_code==200,rows.text
    assert rows.json()['items'][0]['values'][0]['sources'][0]['reading_pdf_sha256']==ids.identity['reading_pdf_sha256']
    req=correction(ids,state,source(ids,page_number=2,quote='Synthetic second page'))
    response=client.put('/api/rebuild/sources/'+sid,json=req);assert response.status_code==200,response.text
    updated=response.json();assert updated['page_number']==2 and updated['source_corrections'][-1]['after']['reading_pdf_sha256']==ids.identity['reading_pdf_sha256']
    assert updated['source_corrections'][-1]['before']['page_number']==1
    assert client.put('/api/rebuild/sources/'+sid,json=req).json()==updated
    changed=deepcopy(req);changed['source']['quote']='different';assert client.put('/api/rebuild/sources/'+sid,json=changed).status_code==409
    url='/api/rebuild/files/'+ids.file+'/preview'
    preview=client.get(url,params=ids.identity);assert preview.status_code==200,preview.text
    assert hashlib.sha256(preview.content).hexdigest()==ids.identity['reading_pdf_sha256']
    assert client.get(url,params={**ids.identity,'reading_pdf_sha256':'0'*64}).status_code==400
    download=client.get('/api/rebuild/files/'+ids.file+'/download');assert download.content==(root/'source.docx').read_bytes()
    crop=dict(file_id=ids.file,asset_key='synthetic-crop',asset_type='figure',request_id=str(uuid4()),regions=[dict(page_number=2,bbox=[0,0,1,0.2])])
    result=client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json=crop);assert result.status_code==200,result.text
    asset=result.json()['asset'];assert asset['provenance']['page_identity']==ids.identity;assert asset['page_numbers']==[2]
    png=client.get(asset['image_url']);assert png.status_code==200 and png.content.startswith(b'\x89PNG')
    assert client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json=crop).json()['asset']['id']==asset['id']
    with factory() as s:
        v=s.scalar(select(RebuildDataValue));assert v.raw_value=='-1.230' and not unresolved_conflict(v.conflict)
        s.expire_all();assert source_state(s,__import__('uuid').UUID(sid))['reading_pdf_sha256']==ids.identity['reading_pdf_sha256']


@pytest.mark.parametrize('change,error',[
    ({'page_basis':None},'source_derived_page_identity_required'),
    ({'source_sha256':None},'source_derived_page_identity_required'),
    ({'reading_pdf_sha256':None},'source_derived_page_identity_required'),
    ({'source_sha256':'0'*64},'source_sha256_mismatch'),
    ({'reading_pdf_sha256':'0'*64},'source_reading_pdf_sha256_mismatch'),
    ({'page_basis':'source_pdf'},'source_derived_page_identity_required'),
    ({'page_number':3},'source_page_out_of_range'),
    ({'page_number':None},'source_page_identity_requires_page'),
    ({'file_id':'foreign'},'source_file_belongs_to_other_paper'),
])
def test_import_and_correction_negatives_no_source_or_cache_side_effect(env,change,error):
    client,factory,settings,ids,root=env
    assert not client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids))).json()['errors']
    sid,state=read_source(client,factory);baseline=counts(factory);cache=ids.cache.read_bytes()
    if change.get('file_id')=='foreign':change={'file_id':ids.foreign}
    invalid=source(ids,**change)
    req=correction(ids,state,invalid)
    response=client.put('/api/rebuild/sources/'+sid,json=req);assert response.status_code==(409 if error=='source_sha256_mismatch' else 400),response.text;assert response.json()['detail']==error
    assert counts(factory)==baseline and read_source(client,factory)[1]==state
    result=client.post('/api/rebuild/rows/import/preflight',json=row_request(ids,invalid));assert result.status_code==200,result.text;assert error in json.dumps(result.json())
    assert counts(factory)==baseline and ids.cache.read_bytes()==cache


def test_missing_preview_and_crop_preflight_no_files(env):
    client,factory,settings,ids,root=env
    ids.cache.rename(root/'held-reading.pdf');before=counts(factory);paths=sorted(str(p) for p in root.rglob('*'))
    crop=dict(file_id=ids.file,asset_key='missing',asset_type='figure',request_id=str(uuid4()),regions=[dict(page_number=1,bbox=[0,0,1,1])])
    response=client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json=crop);assert response.status_code==409 and response.json()['detail']=='derived_pdf_preview_unavailable'
    result=client.post('/api/rebuild/rows/import/preflight',json=row_request(ids,source(ids)));assert 'derived_pdf_preview_unavailable' in result.text
    assert counts(factory)==before and paths==sorted(str(p) for p in root.rglob('*'))


def test_legacy_xml_pdf_and_retirement(env):
    client,factory,settings,ids,root=env
    xml=source(ids,page_number=None,page_basis=None,reading_pdf_sha256=None,source_sha256=None)
    assert not client.post('/api/rebuild/rows/import',json=row_request(ids,xml)).json()['errors']
    sid,state=read_source(client,factory);assert state['page_basis'] is None
    req=correction(ids,state,source(ids));assert client.put('/api/rebuild/sources/'+sid,json=req).status_code==200
    sid,state=read_source(client,factory)
    retire=dict(paper_id=ids.paper,expected_revision=state['source_revision'],request_id=str(uuid4()),reason='fixture retired',action='deactivate')
    assert client.post('/api/rebuild/sources/'+sid+'/lifecycle',json=retire).status_code==200
    rejected=client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids)));assert 'source_inactive' in rejected.text
    assert client.put('/api/rebuild/sources/'+sid,json=correction(ids,read_source(client,factory)[1],source(ids,page_number=2))).status_code==409
    # Old native PDF request needs no new fields; hashes are derived from checked original.
    pdfsource=source(ids,file_id=ids.pdf,page_basis=None,source_sha256=None,reading_pdf_sha256=None,quote='Native PDF fixture')
    result=client.post('/api/rebuild/rows/import',json=row_request(ids,pdfsource));assert not result.json()['errors'],result.text
    with factory() as s:
        rows=list(s.scalars(select(RebuildValueSource)));native=next(x for x in rows if str(x.file_id)==ids.pdf)
        assert source_state(s,native.id)['page_basis']=='source_pdf'
        # Simulate the exact pre-contract DOCX stored shape without inventing a reading identity.
        v=s.get(RebuildDataValue,rows[0].value_id);d=dict(v.conflict);d.pop('source_page_identities');v.conflict=d;s.commit()
        assert source_state(s,rows[0].id)['page_identity_status']=='legacy_unverified'


def test_crop_identity_negatives_keep_db_and_images(env):
    client,factory,settings,ids,root=env
    crop=dict(file_id=ids.file,asset_key='guarded',asset_type='figure',request_id=str(uuid4()),regions=[dict(page_number=1,bbox=[0,0,1,.2])])
    ok=client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json=crop);assert ok.status_code==200,ok.text
    before=counts(factory);stored=client.get(ok.json()['asset']['image_url']).content
    for change,error in [({'file_id':ids.foreign},'file_belongs_to_other_paper'),({'regions':[dict(page_number=3,bbox=[0,0,1,1])]},'crop_page_out_of_range'),({'provenance':{'page_identity':{**ids.identity,'reading_pdf_sha256':'0'*64}}},'source_page_identity_mismatch')]:
        bad={**crop,**change,'request_id':str(uuid4())};r=client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json=bad)
        assert r.status_code==400 and r.json()['detail']==error,r.text
        assert counts(factory)==before and client.get(ok.json()['asset']['image_url']).content==stored
    original=(root/'source.docx').read_bytes();(root/'source.docx').write_bytes(original+b'changed')
    r=client.post('/api/rebuild/papers/'+ids.paper+'/assets/crop',json={**crop,'request_id':str(uuid4())});assert r.status_code==409 and r.json()['detail']=='source_sha256_mismatch'
    assert counts(factory)==before


def test_legacy_retired_unbound_source_cannot_return_as_derived(env):
    client,factory,settings,ids,root=env
    client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids)))
    sid,state=read_source(client,factory)
    with factory() as s:
        v=s.scalar(select(RebuildDataValue));d=dict(v.conflict);d.pop('source_page_identities');v.conflict=d
        src=s.scalar(select(RebuildValueSource));from app.services.rebuild_workflow_service import _source_key,serialize_source
        src.source_key=_source_key(serialize_source(src));s.commit()
    sid,state=read_source(client,factory);assert state['page_identity_status']=='legacy_unverified'
    req=dict(paper_id=ids.paper,expected_revision=state['source_revision'],request_id=str(uuid4()),reason='legacy fixture retired',action='deactivate')
    assert client.post('/api/rebuild/sources/'+sid+'/lifecycle',json=req).status_code==200
    result=client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids)));assert 'source_inactive' in result.text


@pytest.mark.skipif(not __import__('os').environ.get('DOCX_BROWSER_BRIDGE'),reason='isolated host Chromium bridge enabled explicitly')
def test_real_browser_source_link_bridge(env):
    import os,time,threading,uvicorn
    from pathlib import Path
    client,factory,settings,ids,root=env
    result=client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids)));assert not result.json()['errors'],result.text
    sid,state=read_source(client,factory)
    output=Path(os.environ['DOCX_BROWSER_BRIDGE'])
    server=uvicorn.Server(uvicorn.Config(client.app,host='127.0.0.1',port=25443,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.1)
        assert server.started
        (output/'browser-fixture.json').write_text(json.dumps({'base':'http://127.0.0.1:25443','paper_id':ids.paper,'source':state,'file':{'id':ids.file,'paper_id':ids.paper,'role':'si','sha256':ids.identity['source_sha256']}}))
        deadline=time.monotonic()+55
        while not (output/'BROWSER_RESULTS.json').exists() and time.monotonic()<deadline:time.sleep(.25)
        assert (output/'BROWSER_RESULTS.json').exists(),'host Chromium result missing'
        assert json.loads((output/'BROWSER_RESULTS.json').read_text())['status']=='PASS'
    finally:
        server.should_exit=True;thread.join(10)


def test_reading_hash_drift_is_rejected_end_to_end(env):
    client,factory,settings,ids,root=env
    assert not client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids))).json()['errors']
    sid,state=read_source(client,factory);before=counts(factory)
    ids.cache.write_bytes(ids.cache.read_bytes()+b'\n% Synthetic changed renderer bytes\n')
    r=client.get('/api/rebuild/files/'+ids.file+'/preview',params=ids.identity)
    assert r.status_code==400 and r.json()['detail']=='source_reading_pdf_sha256_mismatch'
    r=client.put('/api/rebuild/sources/'+sid,json=correction(ids,state,source(ids,page_number=2)))
    assert r.status_code==400 and r.json()['detail']=='source_reading_pdf_sha256_mismatch'
    r=client.post('/api/rebuild/rows/import/preflight',json=row_request(ids,source(ids)))
    assert 'source_reading_pdf_sha256_mismatch' in r.text
    assert counts(factory)==before and read_source(client,factory)[1]==state


def test_identity_metadata_alone_is_not_scientific_conflict(env):
    client,factory,settings,ids,root=env
    assert not client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids))).json()['errors']
    with factory() as s:
        v=s.scalar(select(RebuildDataValue));src=s.scalar(select(RebuildValueSource))
        v.conflict={'source_page_identities':v.conflict['source_page_identities']};s.commit()
        assert not unresolved_conflict(v.conflict)
        from app.services.analysis_readonly import AnalysisReadonlyService
        loader=AnalysisReadonlyService(s);loader._load_rows()
        item=next(r for r in loader.records if r['field_name']=='adsorption_energy_ev')
        assert item['exploratory_eligible'] and item['exclusion_reason'] is None
        assert item['sources'][0]['reading_pdf_sha256']==ids.identity['reading_pdf_sha256']
        original_id=str(src.id)
    result=client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids)));assert not result.json()['errors'],result.text
    with factory() as s:
        v=s.scalar(select(RebuildDataValue))
        assert not unresolved_conflict(v.conflict)
        assert v.conflict['candidates'][0]['source_ids']==[original_id]


def test_value_correction_reuses_page_contract_and_legacy_fingerprint(env):
    from app.schemas.rebuild import RebuildValueCorrectionRequest
    from app.services.rebuild_workflow_service import value_revision,_canonical_json
    client,factory,settings,ids,root=env
    assert not client.post('/api/rebuild/rows/import',json=row_request(ids,source(ids))).json()['errors']
    with factory() as s:
        v=s.scalar(select(RebuildDataValue));vid=str(v.id);revision=value_revision(v)
    req=dict(raw_value='-1.230',numeric_value=-1.23,unit='eV',precision_digits=3,correction_reason='Synthetic source-only verification',expected_revision=revision,request_id=str(uuid4()),source=source(ids,page_number=2))
    bad={**req,'source':{**req['source'],'reading_pdf_sha256':'0'*64}}
    response=client.put('/api/rebuild/values/'+vid,json=bad);assert response.status_code==400,response.text
    response=client.put('/api/rebuild/values/'+vid,json=req);assert response.status_code==200,response.text
    assert response.json()['sources'][-1]['reading_pdf_sha256']==ids.identity['reading_pdf_sha256']
    assert client.put('/api/rebuild/values/'+vid,json=req).status_code==200
    with factory() as s:revision=value_revision(s.get(RebuildDataValue,__import__('uuid').UUID(vid)))
    native=source(ids,file_id=ids.pdf,page_basis=None,source_sha256=None,reading_pdf_sha256=None)
    legacy={**req,'request_id':str(uuid4()),'expected_revision':revision,'source':native}
    wire=RebuildValueCorrectionRequest(**legacy).model_dump(mode='json')
    for k in ('page_basis','source_sha256','reading_pdf_sha256'):wire['source'].pop(k)
    expected=hashlib.sha256(_canonical_json(wire).encode()).hexdigest()
    r=client.put('/api/rebuild/values/'+vid,json=legacy);assert r.status_code==200,r.text
    assert r.json()['conflict']['correction_requests'][-1]['request_fingerprint']==expected
    assert client.put('/api/rebuild/values/'+vid,json=legacy).status_code==200
