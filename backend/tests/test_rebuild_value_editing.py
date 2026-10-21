from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func
from app.db.models import Paper, RebuildValueSource, RebuildDataValue
from app.schemas.rebuild import RebuildDataRowInput, RebuildValueCorrectionRequest
from app.services.rebuild_workflow_service import import_data_row, list_rows, update_data_value, RebuildWorkflowError, value_revision


def test_value_cas_replay_and_precision(setup_test_db):
    factory = sessionmaker(bind=setup_test_db, expire_on_commit=False)
    with factory() as session:
        paper = Paper(title='Editing fixture', pdf_path='', library_name='Editing')
        session.add(paper); session.flush()
        import_data_row(session, paper.id, RebuildDataRowInput(
            material='Fe-N4', reaction='HER', data_type='experimental', active_site_type='single_atom',
            values=[dict(field_name='overpotential_mv_10', raw_value='120.00', numeric_value=120,
                         unit='mV', precision_digits=2,
                         sources=[dict(source_kind='table',page_number=2,label='Table 1',quote='120.00 mV')])]))
        session.commit()
        cell = list_rows(session, paper_id=paper.id)['items'][0]['values'][0]
        source = {k:v for k,v in cell['sources'][0].items() if k not in {'id','source_key','page_identity_status'}}
        payload = RebuildValueCorrectionRequest(raw_value='120.00', numeric_value=120, unit='mV',
            precision_digits=2, source=source, correction_reason='UI acceptance with unchanged scientific value',
            expected_revision=cell['value_revision'], request_id=uuid4(), notes='verified')
        corrected=update_data_value(session,value_id=UUID(cell['id']),payload=payload)
        session.commit()
        revision=value_revision(corrected)
        assert corrected.raw_value=='120.00'
        assert session.scalar(select(func.count()).select_from(RebuildValueSource))==1
        update_data_value(session,value_id=corrected.id,payload=payload);session.commit()
        assert value_revision(corrected)==revision
        assert len(corrected.conflict['correction_requests'])==1
        with pytest.raises(RebuildWorkflowError, match='value_request_id_reused'):
            update_data_value(session,value_id=corrected.id,payload=payload.model_copy(update={'notes':'different'}))
        session.rollback()
        with pytest.raises(RebuildWorkflowError, match='value_revision_conflict'):
            update_data_value(session,value_id=corrected.id,payload=payload.model_copy(update={'request_id':uuid4()}))
        session.rollback()
        missing=payload.model_copy(update={'request_id':uuid4(),'expected_revision':value_revision(corrected),
            'raw_value':None,'numeric_value':None,'source':None,'value_type':'missing','missing_reason':'not reported'})
        update_data_value(session,value_id=corrected.id,payload=missing);session.commit()
        assert corrected.numeric_value is None and corrected.raw_value is None
        assert corrected.missing_reason=='not reported'
        assert session.scalar(select(func.count()).select_from(RebuildValueSource))==1


def test_value_readback_and_edit_http_contract(setup_test_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.rebuild import router, _require_value_write_session
    from app.db.session import get_db_session
    from types import SimpleNamespace
    factory=sessionmaker(bind=setup_test_db,expire_on_commit=False)
    with factory() as session:
        paper=Paper(title='API editing fixture',pdf_path='',library_name='API Editing')
        session.add(paper);session.flush()
        import_data_row(session,paper.id,RebuildDataRowInput(material='Fe-N4',reaction='HER',
            data_type='dft',active_site_type='single_atom',values=[dict(field_name='adsorption_energy_ev',
            raw_value='-1.230',numeric_value=-1.23,unit='eV',precision_digits=3,
            sources=[dict(source_kind='table',page_number=2,label='Table 1',quote='-1.230 eV')])]))
        session.commit();cell=list_rows(session,paper_id=paper.id)['items'][0]['values'][0]
    app=FastAPI();app.include_router(router,prefix='/api/rebuild')
    def db():
        with factory() as session:yield session
    app.dependency_overrides[get_db_session]=db
    app.dependency_overrides[_require_value_write_session]=lambda:SimpleNamespace(username='tester')
    with TestClient(app) as client:
        path='/api/rebuild/values/'+cell['id']
        read=client.get(path);assert read.status_code==200
        assert read.json()['value_revision']==cell['value_revision']
        source={k:v for k,v in cell['sources'][0].items() if k not in {'id','page_identity_status'}}
        body=dict(expected_revision=cell['value_revision'],request_id=str(uuid4()),correction_reason='API acceptance',
            raw_value='-1.230',numeric_value=-1.23,unit='eV',precision_digits=3,source=source)
        response=client.put(path,json=body);assert response.status_code==200,response.text
        current=client.get(path).json();assert current['raw_value']=='-1.230'
        assert len(current['conflict']['correction_requests'])==1
        assert client.put(path,json=body).status_code==200
        stale={**body,'request_id':str(uuid4())}
        assert client.put(path,json=stale).status_code==409
