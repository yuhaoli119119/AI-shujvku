from uuid import uuid4
import pytest
from app.db.models import Paper, PaperRelationship, RebuildPaperFile, WorkflowJob
from app.db.session import get_engine
from sqlalchemy.orm import sessionmaker
from app.schemas.rebuild import ProcessingStatusReport
from app.services.paper_processing_status import processing_statuses, report_processing
from sqlalchemy import select,func


def test_external_completion_new_si_and_idempotent_state(setup_test_db):
    from app.config import get_settings
    factory=sessionmaker(bind=get_engine(get_settings().database_url))
    with factory() as session:
        paper=Paper(title='status fixture',pdf_path='main.pdf',abstract='Metadata is not parsing')
        session.add(paper);session.flush()
        main=RebuildPaperFile(paper_id=paper.id,role='main',storage_path='main.pdf',original_filename='main.pdf',sha256='a'*64,file_size=1)
        session.add(main);session.flush()
        assert processing_statuses(session,[paper.id])[paper.id]['status']=='unparsed'
        report_processing(session,paper,ProcessingStatusReport(run_id='main-only',status='parsed',file_ids=[main.id],no_extractable_content_reason='PDF read; no applicable numerical data'))
        assert processing_statuses(session,[paper.id])[paper.id]['status']=='parsed'
        si=RebuildPaperFile(paper_id=paper.id,role='si',storage_path='si.pdf',original_filename='si.pdf',sha256='b'*64,file_size=1)
        session.add(si);session.flush()
        state=processing_statuses(session,[paper.id])[paper.id]
        assert state['status']=='partial' and state['parsed_files']==1 and state['total_files']==2
        report_processing(session,paper,ProcessingStatusReport(run_id='si-only',status='parsing',file_ids=[si.id]))
        assert processing_statuses(session,[paper.id])[paper.id]['status']=='parsing'
        done=ProcessingStatusReport(run_id='si-only',status='parsed',file_ids=[si.id],no_extractable_content_reason='SI reviewed; no applicable numerical data')
        report_processing(session,paper,done)
        assert processing_statuses(session,[paper.id])[paper.id]['status']=='parsed'
        report_processing(session,paper,done)
        assert session.scalar(select(func.count()).select_from(WorkflowJob))==2
        with pytest.raises(ValueError):report_processing(session,paper,ProcessingStatusReport(run_id='si-only',status='parsing'))
        other=Paper(title='other paper',pdf_path='other.pdf');session.add(other);session.flush()
        with pytest.raises(ValueError):report_processing(session,other,ProcessingStatusReport(run_id='cross-file',status='parsed',file_ids=[main.id],no_extractable_content_reason='test'))
        session.rollback()


def test_later_manually_linked_si_counts_until_its_own_completion(setup_test_db):
    from app.config import get_settings
    factory=sessionmaker(bind=get_engine(get_settings().database_url))
    with factory() as session:
        main=Paper(title='main',pdf_path='main.pdf')
        child=Paper(title='SI',pdf_path='later-si.pdf',paper_type='supplementary')
        session.add_all([main,child]);session.flush()
        source=RebuildPaperFile(paper_id=main.id,role='main',storage_path='main.pdf',original_filename='main.pdf',sha256='a'*64,file_size=1)
        session.add(source);session.flush()
        report_processing(session,main,ProcessingStatusReport(run_id='main',status='parsed',file_ids=[source.id],no_extractable_content_reason='read'))
        session.add(PaperRelationship(source_paper_id=main.id,target_paper_id=child.id,relationship_type='supplementary'))
        session.flush()
        state=processing_statuses(session,[main.id])[main.id]
        assert (state['status'],state['parsed_files'],state['total_files'])==('partial',1,2)
        assert state['files'][1]['paper_id']==str(child.id)
        report_processing(session,child,ProcessingStatusReport(run_id='SI',status='parsed',no_extractable_content_reason='SI read'))
        state=processing_statuses(session,[main.id])[main.id]
        assert (state['status'],state['parsed_files'],state['total_files'])==('parsed',2,2)
        session.rollback()
