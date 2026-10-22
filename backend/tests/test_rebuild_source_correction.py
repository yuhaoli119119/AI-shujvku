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
    ({'page_number':3}, 'source_page_out_of_range'),
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
        other=RebuildValueSource(value_id=ids.value,row_id=original.row_id,paper_id=ids.paper,source_key=_source_key(data),**{k:v for k,v in data.items() if k not in {"page_basis","source_sha256","reading_pdf_sha256","ooxml_locator","merged_cell"}})
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
        with pytest.raises(RebuildWorkflowError,match='source_derived_page_identity_required'):
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


def test_legacy_native_pdf_request_fingerprint_is_stable(fixture):
    factory,settings,ids=fixture
    with factory() as s:
        req=body(s,ids)
        # Exactly the pre-extension wire body: no optional page identity keys.
        expected=hashlib.sha256(json.dumps(req,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        result=correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='trusted-login');s.commit()
        assert result['source_corrections'][-1]['request_fingerprint']==expected
        assert correct_source(s,ids.source,RebuildSourceCorrectionRequest(**req),settings,actor='trusted-login')==result


def test_merged_cell_correction_persistence_cas_replay_and_null_science(fixture):
    """Synthetic blank cell; metadata survives session reload and is audit bound."""
    from app.schemas.rebuild import RebuildValueCorrectionRequest
    from app.services.rebuild_workflow_service import update_data_value, value_revision
    factory, settings, ids = fixture
    merged = dict(coordinate_basis='zero_based_data_rows', merged_group_id='synthetic-group',
                  role='continuation', anchor_row=0, anchor_column=1, row_span=2,
                  column_span=1, cell_content='blank')
    with factory() as s:
        value = s.get(RebuildDataValue, ids.value)
        value.raw_value = None; value.numeric_value = None
        value.value_type = 'missing'; value.missing_reason = 'Printed continuation blank'
        table = s.get(RebuildVisualAsset, ids.assets[0])
        table.provenance = {'table_data':{'columns':['shell','delta'], 'rows':[['A','4.774'],['B',None]]}}
        s.commit()
        before = source_state(s,ids.source)
        science = {k:getattr(value,k) for k in ('raw_value','numeric_value','unit','value_type','missing_reason','preferred_source_id')}
        req = RebuildSourceCorrectionRequest(**body(s,ids,table_row=1,merged_cell=merged))
        result = correct_source(s,ids.source,req,settings,actor='synthetic-reviewer')
        assert result['merged_cell']['role'] == 'continuation'
        assert result['source_revision'] != before['source_revision']
        assert {k:getattr(value,k) for k in science} == science
        assert result['source_corrections'][-1]['after']['merged_cell'] == result['merged_cell']
        s.commit()
    with factory() as s:
        result=source_state(s,ids.source)
        assert result['merged_cell']['row_span'] == 2
        frozen=snapshot(s)
        assert correct_source(s,ids.source,req,settings,actor='synthetic-reviewer') == result
        assert snapshot(s) == frozen
        omitted=RebuildSourceCorrectionRequest(**body(s,ids,table_row=1))
        with pytest.raises(RebuildWorkflowError,match='merged_cell_replacement_required'):
            correct_source(s,ids.source,omitted,settings,actor='synthetic-reviewer')
        assert snapshot(s) == frozen
        value=s.get(RebuildDataValue,ids.value)
        # Existing blank evidence must also reject numeric promotion through the value API.
        numeric=RebuildValueCorrectionRequest(expected_revision=value_revision(value),request_id=uuid4(),
            correction_reason='Forbidden inheritance',raw_value='4.774',numeric_value=4.774,
            value_type='explicit',source={**body(s,ids,table_row=1)['source'],'merged_cell':merged})
        with pytest.raises(RebuildWorkflowError,match='merged_blank_cell_cannot_supply_value'):
            update_data_value(s,value_id=value.id,payload=numeric)
        assert snapshot(s) == frozen
        replacement=RebuildSourceCorrectionRequest(**body(s,ids,table_row=1,merged_cell=merged))
        corrected=correct_source(s,ids.source,replacement,settings,actor='synthetic-reviewer')
        assert corrected['merged_cell'] == result['merged_cell']
        assert s.get(RebuildDataValue,ids.value).raw_value is None


@pytest.mark.parametrize('changes,error', [
    ({'row_span':3},'row_out_of_range'),
    ({'anchor_column':1,'column_span':2},'column_out_of_range'),
])
def test_merged_source_correction_invalid_bounds_no_mutation(fixture, changes, error):
    factory,settings,ids=fixture
    with factory() as s:
        value=s.get(RebuildDataValue,ids.value)
        value.raw_value=None; value.numeric_value=None
        value.value_type='missing'; value.missing_reason='Blank'
        s.get(RebuildVisualAsset,ids.assets[0]).provenance={
            'table_data':{'columns':['shell','delta'],'rows':[['A','4.774'],['B',None]]}}
        s.commit(); frozen=snapshot(s)
        merged=dict(coordinate_basis='zero_based_data_rows',merged_group_id='invalid-test',
                    role='continuation',anchor_row=0,anchor_column=1,row_span=2,column_span=1,cell_content='blank')
        request=RebuildSourceCorrectionRequest(**body(s,ids,table_row=1,merged_cell=merged | changes))
        with pytest.raises(RebuildWorkflowError,match=error):
            correct_source(s,ids.source,request,settings,actor='test')
        assert snapshot(s)==frozen


def test_merged_cell_import_and_value_update_keep_json_extension(fixture):
    from app.schemas.rebuild import RebuildValueCorrectionRequest
    from app.services.rebuild_workflow_service import update_data_value, value_revision
    factory,settings,ids=fixture
    merged=dict(coordinate_basis='zero_based_data_rows',merged_group_id='synthetic-import',
                role='anchor',anchor_row=0,anchor_column=1,row_span=2,column_span=1,cell_content='printed')
    with factory() as s:
        s.get(RebuildVisualAsset,ids.assets[0]).provenance={
            'table_data':{'columns':['shell','delta'],'rows':[['A','-1.230'],['B',None]]}}
        src={**body(s,ids)['source'],'merged_cell':merged}
        data=RebuildDataRowInput(material='synthetic-merged',reaction='HER',data_type='dft',
            active_site_type='single_atom',values=[dict(field_name='energy',raw_value='-1.230',
            numeric_value=-1.23,unit='eV',sources=[src])])
        result=import_data_row(s,ids.paper,data)
        value=s.scalar(select(RebuildDataValue).where(RebuildDataValue.field_name=='energy'))
        source=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==value.id))
        assert serialize_source(source)['merged_cell']['role']=='anchor'
        request=RebuildValueCorrectionRequest(expected_revision=value_revision(value),request_id=uuid4(),
            correction_reason='Explicit synthetic value update',raw_value='-1.230',numeric_value=-1.23,
            unit='eV',value_type='explicit',source=src)
        update_data_value(s,value_id=value.id,payload=request)
        assert serialize_source(source)['merged_cell']['merged_group_id']=='synthetic-import'
        s.commit()
        assert serialize_source(source)['merged_cell']['row_span']==2


def test_exact_six_b046_synthetic_targets_persist_without_inheritance(setup_test_db, tmp_path):
    from pathlib import Path
    from app.db.models import RebuildDataRow
    shape=json.loads((Path(__file__).parent/'fixtures/b046_merged_cells.json').read_text())
    pdf=fitz.open()
    for _ in range(26): pdf.new_page()
    pdf.save(tmp_path/'six-synthetic.pdf'); pdf.close()
    content=(tmp_path/'six-synthetic.pdf').read_bytes()
    settings=Settings(storage_root=tmp_path)
    factory=sessionmaker(bind=setup_test_db,expire_on_commit=False)
    pid,fid,aid=[UUID(shape[k]) for k in ('paper_id','file_id','asset_id')]
    with factory() as s:
        s.add(Paper(id=pid,title='Synthetic six-target merged cells',pdf_path=''));s.flush()
        s.add(RebuildPaperFile(id=fid,paper_id=pid,role='si',storage_path='six-synthetic.pdf',
            original_filename='six-synthetic.pdf',sha256=hashlib.sha256(content).hexdigest(),file_size=len(content)));s.flush()
        s.add(RebuildVisualAsset(id=aid,paper_id=pid,file_id=fid,asset_type='table',
            asset_key='synthetic-six',page_numbers=[26],provenance={'table_data':shape['table_data']}));s.flush()
        for target in shape['targets']:
            row=RebuildDataRow(paper_id=pid,row_key=target['value_id'],reaction='SRR',material='synthetic',data_type='dft',active_site_type='single_atom')
            s.add(row);s.flush()
            value=RebuildDataValue(id=UUID(target['value_id']),paper_id=pid,row_id=row.id,
                field_name='delta_e0_ev',raw_value=None,numeric_value=None,value_type='missing',
                missing_reason='Synthetic printed continuation blank')
            s.add(value);s.flush()
            src=RebuildValueSource(id=UUID(target['source_id']),paper_id=pid,row_id=row.id,value_id=value.id,
                file_id=fid,asset_id=aid,source_key='legacy-'+target['source_id'],source_kind='table',
                page_number=26,label='Synthetic S2',table_row=target['table_row'],table_column=target['table_column'])
            s.add(src);s.flush()
            state=source_state(s,src.id)
            fields={k:state[k] for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
            req=RebuildSourceCorrectionRequest(paper_id=pid,expected_revision=state['source_revision'],
                request_id=uuid4(),correction_reason='Synthetic-only span test',source=fields | {'merged_cell':target['merged_cell']})
            result=correct_source(s,src.id,req,settings,actor='synthetic-test')
            assert result['merged_cell']==target['merged_cell']
            assert value.raw_value is None and value.numeric_value is None
        s.commit()
    with factory() as s:
        assert len(list(s.scalars(select(RebuildValueSource))))==6
        assert len(list(s.scalars(select(RebuildDataValue))))==6
        for target in shape['targets']:
            state=source_state(s,UUID(target['source_id']))
            assert state['merged_cell']==target['merged_cell']
            assert s.get(RebuildDataValue,UUID(target['value_id'])).raw_value is None
