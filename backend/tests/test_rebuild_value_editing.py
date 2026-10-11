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
        source = {k:v for k,v in cell['sources'][0].items() if k not in {'id','source_key'}}
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
