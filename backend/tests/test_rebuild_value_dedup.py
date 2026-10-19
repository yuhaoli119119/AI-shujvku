from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import Paper, RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildAnalysisRun
from app.schemas.rebuild import RebuildDedupPreviewRequest, RebuildDedupApplyRequest
from app.services.rebuild_value_dedup import preview_dedup, apply_dedup
from app.services.rebuild_workflow_service import RebuildWorkflowError, _source_key, serialize_source


@pytest.fixture
def data(setup_test_db):
    factory = sessionmaker(bind=setup_test_db, expire_on_commit=False)
    with factory() as s:
        p = Paper(title='Isolated dedup', pdf_path=''); other = Paper(title='Other', pdf_path='')
        s.add_all([p, other]); s.flush()
        pairs=[]
        # Exact B075 identifiers, isolated synthetic science; never connected to production.
        identifiers = {
            'CrN3': [('c9acfd69-70ec-44dd-89f4-a33a1c25b091','5f6d5bd1-02ca-443e-8174-010a0dbd4bcc','ade0aaf2-17ff-4def-bb61-3fea058eede7'),('e19e8f79-087b-4746-a71f-a73ce200a527','619d9f3a-f8fd-42bc-b521-b190359889ae','97b5162f-cc13-4975-bad5-0887f022934f')],
            'CrN4': [('4df4e6e4-9ed3-407f-87a2-f932ef6227f2','588b96f7-9a68-499c-9d8d-15d5343430c8','caa005ad-c78c-4700-851a-1ee231729647'),('34c8548d-52c3-49ae-a3f1-f13073d5634d','e3cbb7ba-a74a-45c9-9801-2e50abb84d55','c04d93b5-5cb3-4024-9289-bfe14222e7fb')]}
        for material, raw in [('CrN3','0.4'),('CrN4','0.59')]:
            vv=[]
            for n in range(2):
                r=RebuildDataRow(id=UUID(identifiers[material][n][1]),paper_id=p.id,row_key=str(uuid4()),material=material,reaction='SRR',active_site_type='single_atom',data_type='dft',condition={'step':str(n)},properties={})
                s.add(r);s.flush()
                v=RebuildDataValue(id=UUID(identifiers[material][n][0]),paper_id=p.id,row_id=r.id,field_name='rds_barrier_ev' if n==0 else 'gibbs_energy_change_ev',raw_value=raw+'0' if n==1 and material=='CrN3' else raw,numeric_value=float(raw),unit='eV',value_type='explicit',precision_digits=len(raw.split('.')[1])+n,conflict={'history':[{'old':'preserve'}]},created_at=datetime(2020,1,1)+timedelta(days=n))
                s.add(v);s.flush()
                src=RebuildValueSource(id=UUID(identifiers[material][n][2]),paper_id=p.id,row_id=r.id,value_id=v.id,source_kind='text',quote=material+' source '+str(n),source_key='pending')
                src.source_key=_source_key(serialize_source(src));s.add(src);s.flush()
                v.preferred_source_id=src.id
                vv.append(v.id)
            pairs.append(dict(keep_value_id=str(vv[0]),duplicate_value_id=str(vv[1]),same_fact=True,reason='Independent science review',identity_assertion='Same elementary step and barrier; field and condition labels differ as explicitly reviewed'))
        s.commit()
        return factory,p.id,other.id,RebuildDedupPreviewRequest(pairs=pairs)


def snapshot(s):
    return {m.__tablename__:[{c.name:str(getattr(o,c.name)) for c in m.__table__.columns} for o in s.scalars(select(m).order_by(m.id))] for m in [RebuildDataRow,RebuildDataValue,RebuildValueSource]}


def request(s,pid,body):
    p=preview_dedup(s,pid,body)
    return RebuildDedupApplyRequest(**body.model_dump(),expected_plan_revision=p['plan_revision'],confirm_delete_value_ids=p['delete_value_ids'],confirm_delete_row_ids=p['delete_row_ids'],expected_move_source_ids=p['move_source_ids'],request_id=uuid4(),reason='User approved exact four-record deletion list')


def test_preview_pure_and_success_restore_all_history_and_replay(data):
    factory,pid,other,body=data
    with factory() as s:
        before=snapshot(s)
        p=preview_dedup(s,pid,body);s.commit()
        assert snapshot(s)==before
        assert p==preview_dedup(s,pid,body)
        assert p['counts']==dict(delete_values=2,delete_rows=2,move_sources=2,delete_sources=0)
        req=request(s,pid,body)
        result=apply_dedup(s,pid,req,actor='trusted');s.commit()
        after=snapshot(s)
        assert len(after['rebuild_data_rows'])==2 and len(after['rebuild_data_values'])==2 and len(after['rebuild_value_sources'])==4
        for pair in body.pairs:
            v=s.get(RebuildDataValue,pair.keep_value_id)
            old=next(o for o in before['rebuild_data_values'] if o['id']==str(v.id))
            for key in ('raw_value','precision_digits','unit','value_type','preferred_source_id','id'):
                assert str(getattr(v,key))==old[key]
            assert v.conflict['history']==[{'old':'preserve'}]
            event=v.conflict['merge_history'][0]
            assert event['actor']=='trusted' and event['recorded_at'] and event['reason']==req.reason
            assert event['preview']==p
            assert any(x.get('duplicate_value',{}).get('raw_value')=='0.40' for x in event['preview']['pairs'])
        assert apply_dedup(s,pid,req,actor='trusted')==result;s.commit();assert snapshot(s)==after
        for actor,update in [('different',{}),('trusted',{'reason':'different'})]:
            with pytest.raises(RebuildWorkflowError,match='request_id_reused'):
                apply_dedup(s,pid,req.model_copy(update=update),actor=actor)
            s.commit();assert snapshot(s)==after
        # Restore every retired record with its original UUID and exact raw/history.
        originals=[x for x in p['pairs'] if 'duplicate_value' in x]
        for item in originals:
            for model,key in [(RebuildDataRow,'duplicate_row'),(RebuildDataValue,'duplicate_value')]:
                image=deepcopy(item[key])
                from uuid import UUID
                for c in model.__table__.columns:
                    if image[c.name] is not None and c.name in ('id','paper_id','row_id','preferred_source_id'): image[c.name]=UUID(image[c.name])
                    if image[c.name] is not None and c.name in ('created_at','updated_at'): image[c.name]=datetime.fromisoformat(image[c.name])
                s.add(model(**image));s.flush()
        for item in p['pairs']:
            if 'source_before' in item:
                from uuid import UUID
                image=item['source_before'];src=s.get(RebuildValueSource,UUID(image['id']))
                src.row_id=UUID(image['row_id']);src.value_id=UUID(image['value_id'])
        s.flush()
        for pair in body.pairs:
            s.get(RebuildDataValue,pair.keep_value_id).conflict={'history':[{'old':'preserve'}]}
        s.flush()
        restored=snapshot(s)
        # canonical updated_at changes on audit append; all original scientific data/history restores exactly.
        for row in restored['rebuild_data_values']:
            old=next(o for o in before['rebuild_data_values'] if o['id']==row['id'])
            assert {k:v for k,v in row.items() if k!='updated_at'}=={k:v for k,v in old.items() if k!='updated_at'}
        assert restored['rebuild_value_sources']==before['rebuild_value_sources']
        assert restored['rebuild_data_rows']==before['rebuild_data_rows']
        s.rollback()


@pytest.mark.parametrize('mutation,error',[
    ('stale','revision_conflict'),('extra_confirm','confirmation_mismatch'),('missing_confirm','confirmation_mismatch'),
    ('material','identity_mismatch'),('active_site','identity_mismatch'),('data_type','identity_mismatch'),('reaction','identity_mismatch'),
    ('cross_paper','source_owner_inconsistent'),('key','source_key_conflict'),('external_json','external_reference_conflict'),
    ('external_fk','external_reference_conflict'),('preferred','preferred_source_invalid'),('number','value_mismatch'),('no_reason','actor_reason_required'),
    ('no_pair_reason','pair_reason_identity_required'),('not_oldest','canonical_not_oldest'),
])
def test_reject_zero_mutation(data,mutation,error):
    factory,pid,other,body=data
    with factory() as s:
        req=request(s,pid,body)
        keep=s.get(RebuildDataValue,body.pairs[0].keep_value_id);dup=s.get(RebuildDataValue,body.pairs[0].duplicate_value_id)
        row=s.get(RebuildDataRow,dup.row_id)
        if mutation=='stale':keep.conflict={'fresh':'change'}
        elif mutation=='extra_confirm':req=req.model_copy(update={'confirm_delete_value_ids':[*req.confirm_delete_value_ids,uuid4()]})
        elif mutation=='missing_confirm':req=req.model_copy(update={'confirm_delete_row_ids':[]})
        elif mutation in ('material','active_site','data_type','reaction'):setattr(row,mutation,'different')
        elif mutation=='cross_paper':dup.paper_id=other
        elif mutation=='number':dup.numeric_value=99.0
        elif mutation=='not_oldest':keep.created_at=datetime(2030,1,1)
        elif mutation=='no_reason':req=req.model_copy(update={'reason':' '})
        elif mutation=='no_pair_reason':req.pairs[0].reason=' '
        elif mutation=='key':
            a=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==keep.id));b=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==dup.id))
            b.quote=a.quote;b.source_key=a.source_key
        elif mutation=='external_json':s.add(RebuildAnalysisRun(spec_hash=uuid4().hex,spec={},result={'nested':[{'value_id':str(dup.id)}]}))
        elif mutation=='external_fk':
            s.execute(text('CREATE TABLE extra_reference (id uuid PRIMARY KEY, value_id uuid REFERENCES rebuild_data_values(id) ON DELETE CASCADE)'))
            s.execute(text('INSERT INTO extra_reference VALUES (:id,:v)'),{'id':uuid4(),'v':dup.id})
        elif mutation=='preferred':keep.preferred_source_id=dup.id
        s.commit();before=snapshot(s)
        with pytest.raises(RebuildWorkflowError,match=error): apply_dedup(s,pid,req,actor='trusted')
        s.commit();assert snapshot(s)==before


def test_atomic_rollback_after_first_delete_even_if_caller_commits(data):
    from sqlalchemy import event
    factory,pid,other,body=data
    with factory() as s:
        req=request(s,pid,body);before=snapshot(s);executed=[]
        def fail_after_sql(conn,cursor,statement,parameters,context,executemany):
            executed.append(statement)
            if statement.lstrip().startswith('DELETE FROM rebuild_data_values'):
                raise RuntimeError('injected after value DELETE executed')
        engine=s.get_bind();event.listen(engine,'after_cursor_execute',fail_after_sql)
        try:
            with pytest.raises(RuntimeError,match='after value DELETE executed'):
                apply_dedup(s,pid,req,actor='trusted')
        finally:
            event.remove(engine,'after_cursor_execute',fail_after_sql)
        assert any(sql.lstrip().startswith('UPDATE rebuild_value_sources') for sql in executed)
        assert any(sql.lstrip().startswith('DELETE FROM rebuild_data_values') for sql in executed)
        s.commit();assert snapshot(s)==before


def test_concurrent_cas_single_winner(data):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    factory,pid,other,body=data
    with factory() as s:req=request(s,pid,body)
    gate=Barrier(2)
    def run(n):
        with factory() as s:
            gate.wait()
            try:
                result=apply_dedup(s,pid,req.model_copy(update={'request_id':uuid4()}),actor='trusted');s.commit();return 'ok'
            except RebuildWorkflowError as e:s.rollback();return str(e)
    with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(run,range(2)))
    assert out.count('ok')==1 and out.count('dedup_value_missing_or_cross_paper')==1


def test_http_auth_origin_csrf_contract(data,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import rebuild
    from app.db.session import get_db_session
    factory,pid,other,body=data
    app=FastAPI();app.include_router(rebuild.router,prefix='/api/rebuild')
    def db():
        with factory() as s:yield s
    app.dependency_overrides[get_db_session]=db
    path=f'/api/rebuild/papers/{pid}/value-dedup/'
    with factory() as s:req=request(s,pid,body).model_dump(mode='json');before=snapshot(s)
    with TestClient(app) as c:
        for op,payload in [('preview',body.model_dump(mode='json')),('apply',req)]:
            assert c.post(path+op,json=payload).status_code==401
        async def logged(*a,**kw):return SimpleNamespace(username='trusted-http',csrf='token')
        monkeypatch.setattr(rebuild,'require_session',logged)
        assert c.post(path+'apply',json=req).status_code==403
        c.cookies.set('litai_csrf','token');c.headers['X-CSRF-Token']='token'
        for op in ('apply','preview'):
            assert c.post(path+op,json=req if op=='apply' else body.model_dump(mode='json'),headers={'Origin':'https://evil.invalid'}).status_code==403
        assert c.post(path+'apply',json={k:v for k,v in req.items() if k!='confirm_delete_row_ids'}).status_code==422
        p=c.post(path+'preview',json=body.model_dump(mode='json'));assert p.status_code==200,p.text
        with factory() as s:assert snapshot(s)==before
        res=c.post(path+'apply',json=req);assert res.status_code==200,res.text
        with factory() as s:
            assert s.get(RebuildDataValue,body.pairs[0].keep_value_id).conflict['merge_history'][0]['actor']=='trusted-http'



def test_nonempty_duplicate_row_survives_and_unlisted_records_unchanged(data):
    factory,pid,other,body=data
    with factory() as s:
        dup=s.get(RebuildDataValue,body.pairs[0].duplicate_value_id)
        extra=RebuildDataValue(row_id=dup.row_id,paper_id=pid,field_name='unrelated',raw_value='7.000',numeric_value=7,unit='eV')
        s.add(extra);s.commit();row_id=dup.row_id
        req=request(s,pid,body)
        assert row_id not in req.confirm_delete_row_ids
        result=apply_dedup(s,pid,req,actor='trusted');s.commit()
        assert result['counts']['delete_rows']==1
        assert s.get(RebuildDataRow,row_id) is not None
        assert s.get(RebuildDataValue,extra.id).raw_value=='7.000'


def test_cross_paper_target_and_overlapping_pair_rejected(data):
    factory,pid,other,body=data
    with factory() as s:
        with pytest.raises(RebuildWorkflowError):preview_dedup(s,other,body)
        with pytest.raises(RebuildWorkflowError,match='overlapping_pairs'):
            preview_dedup(s,pid,RebuildDedupPreviewRequest(pairs=[body.pairs[0],body.pairs[0]]))


def test_external_preferred_conflict_and_row_json_references(data):
    factory,pid,other,body=data
    with factory() as s:
        target=body.pairs[0].duplicate_value_id
        for field in ('preferred','conflict','row_json'):
            r=RebuildDataRow(paper_id=other,row_key=str(uuid4()),material='Other',reaction='SRR',active_site_type='single_atom',data_type='dft',properties={'refs':[str(target)]} if field=='row_json' else {})
            s.add(r);s.flush()
            v=RebuildDataValue(paper_id=other,row_id=r.id,field_name='x',preferred_source_id=target if field=='preferred' else None,conflict={'refs':[str(target)]} if field=='conflict' else {})
            s.add(v);s.flush()
            with pytest.raises(RebuildWorkflowError,match='external_reference_conflict'):preview_dedup(s,pid,body)
            s.rollback()


def test_moved_source_text_cannot_hide_external_retired_reference(data):
    factory,pid,other,body=data
    with factory() as s:
        dup=body.pairs[0].duplicate_value_id
        source=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==dup))
        source.quote='active pointer '+str(dup)
        source.source_key=_source_key(serialize_source(source));s.commit()
        with pytest.raises(RebuildWorkflowError,match='external_reference_conflict'):
            preview_dedup(s,pid,body)



def test_external_preferred_moved_source_rejected(data):
    factory,pid,other,body=data
    with factory() as s:
        source=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==body.pairs[0].duplicate_value_id))
        r=RebuildDataRow(paper_id=other,row_key='outside',material='Other',reaction='SRR',active_site_type='single_atom',data_type='dft')
        s.add(r);s.flush()
        s.add(RebuildDataValue(paper_id=other,row_id=r.id,field_name='outside',preferred_source_id=source.id));s.commit()
        with pytest.raises(RebuildWorkflowError,match='external_preferred_source_conflict'):
            preview_dedup(s,pid,body)



def test_request_id_reuse_across_papers_is_global_conflict(data):
    factory,pid,other,body=data
    with factory() as s:
        original=request(s,pid,body)
        apply_dedup(s,pid,original,actor='trusted');s.commit()
        vals=[]
        for n in range(2):
            row=RebuildDataRow(paper_id=other,row_key='pair-'+str(n),material='Other',reaction='SRR',active_site_type='single_atom',data_type='dft')
            s.add(row);s.flush()
            v=RebuildDataValue(paper_id=other,row_id=row.id,field_name='x',raw_value='1.0',numeric_value=1.0,unit='eV',created_at=datetime(2020,1,1)+timedelta(days=n))
            s.add(v);s.flush();vals.append(v.id)
        s.commit()
        other_body=RebuildDedupPreviewRequest(pairs=[dict(keep_value_id=vals[0],duplicate_value_id=vals[1],same_fact=True,reason='confirmed',identity_assertion='same fact')])
        req=request(s,other,other_body).model_copy(update={'request_id':original.request_id})
        before=snapshot(s)
        with pytest.raises(RebuildWorkflowError,match='request_id_reused'):
            apply_dedup(s,other,req,actor='trusted')
        s.commit();assert snapshot(s)==before



def test_actual_delete_count_mismatch_rolls_back_preceding_moves_and_audit(data):
    factory,pid,other,body=data
    with factory() as s:
        req=request(s,pid,body);before=snapshot(s)
        # Transaction-local fixture DDL; rolled back below, never production.
        s.execute(text('CREATE FUNCTION skip_dedup_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$'))
        s.execute(text('CREATE TRIGGER skip_dedup_delete BEFORE DELETE ON rebuild_data_values FOR EACH ROW EXECUTE FUNCTION skip_dedup_delete()'))
        with pytest.raises(RebuildWorkflowError,match='delete_count_conflict'):
            apply_dedup(s,pid,req,actor='trusted')
        # Check before rolling back the outer fixture DDL: service savepoint
        # must already have undone every source move and canonical audit.
        assert snapshot(s)==before
        s.rollback()


def test_docx_page_identity_survives_explicit_source_move(data):
    factory,pid,other,body=data
    with factory() as s:
        identities={}
        for pair in body.pairs:
            dup=s.get(RebuildDataValue,pair.duplicate_value_id)
            src=s.scalar(select(RebuildValueSource).where(RebuildValueSource.value_id==dup.id))
            identity={'page_basis':'derived_pdf','source_sha256':'a'*64,'reading_pdf_sha256':'b'*64}
            identities[str(src.id)]=identity
            dup.conflict={**dup.conflict,'source_page_identities':{str(src.id):identity}}
            src.page_number=1
            src.source_key=_source_key(serialize_source(src))
        s.commit()
        req=request(s,pid,body);apply_dedup(s,pid,req,actor='fixture');s.commit();s.expire_all()
        for sid,identity in identities.items():
            src=s.get(RebuildValueSource,UUID(sid))
            assert all(serialize_source(src)[k]==v for k,v in identity.items())
