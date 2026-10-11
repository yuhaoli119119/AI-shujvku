from uuid import uuid4
import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func
from app.db.models import Paper, DFTResult, RebuildPaperFile, RebuildDataRow
from app.services.legacy_data_transfer import LegacyTransferRequest, transfer_legacy_value
from app.services.rebuild_workflow_service import RebuildWorkflowError


def test_legacy_transfer_explicit_sourced_and_replay_preserves_original(setup_test_db):
    factory=sessionmaker(bind=setup_test_db,expire_on_commit=False)
    with factory() as s:
        p=Paper(title='legacy',library_name='Transfer',pdf_path='');s.add(p);s.flush()
        f=RebuildPaperFile(paper_id=p.id,role='main',storage_path='pdf/a.pdf',original_filename='a.pdf',sha256='a'*64,file_size=1)
        old=DFTResult(paper_id=p.id,property_type='adsorption_energy_ev',value=-1.25,unit='eV',evidence_text='Table 1 -1.25')
        s.add_all([f,old]);s.commit()
        payload=LegacyTransferRequest(legacy_kind='dft',legacy_id=old.id,field_name='adsorption_energy_ev',reaction='SRR',material='FeN4',active_site_type='single_atom',source={'source_kind':'table','file_id':f.id,'page_number':2,'label':'Table 1'},reason='verified provenance',request_id=uuid4())
        result=transfer_legacy_value(s,p.id,payload,'tester');s.commit()
        assert result['status']=='transferred'
        assert result['row']['values'][0]['numeric_value']==-1.25
        assert old.value==-1.25 and old.evidence_text=='Table 1 -1.25'
        assert transfer_legacy_value(s,p.id,payload,'tester')['status']=='replayed'
        assert s.scalar(select(func.count()).select_from(RebuildDataRow))==1
        with pytest.raises(RebuildWorkflowError,match='legacy_transfer_already_exists'):
            transfer_legacy_value(s,p.id,payload.model_copy(update={'request_id':uuid4()}),'tester')
        s.rollback()


def test_missing_and_interval_transfer_preserve_original_boundaries(setup_test_db):
    factory=sessionmaker(bind=setup_test_db,expire_on_commit=False)
    with factory() as s:
        p=Paper(title='legacy ranges',library_name='Range Transfer',pdf_path='');s.add(p);s.flush()
        f=RebuildPaperFile(paper_id=p.id,role='main',storage_path='pdf/a.pdf',original_filename='a.pdf',sha256='b'*64,file_size=1)
        records=[DFTResult(paper_id=p.id,property_type='adsorption_energy_ev',value=None,unit='eV'),
                 DFTResult(paper_id=p.id,property_type='adsorption_energy_ev',value=-2.0,value_upper=-1.0,unit='eV')]
        s.add_all([f,*records]);s.commit()
        for record in records:
            payload=LegacyTransferRequest(legacy_kind='dft',legacy_id=record.id,field_name='adsorption_energy_ev',reaction='SRR',material='FeN4',active_site_type='single_atom',source={'source_kind':'table','file_id':f.id,'page_number':2,'label':'Table 1'},reason='retain original interval or missing value',request_id=uuid4())
            result=transfer_legacy_value(s,p.id,payload,'tester');s.commit()
            value=result['row']['values'][0]
            assert value['numeric_value'] is None
            if record.value is None:
                assert value['value_type']=='missing' and value['raw_value'] is None
            else:
                assert value['raw_value']=='-2.0 ～ -1.0'
                assert record.value==-2 and record.value_upper==-1
