from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app.services.paper_processing_status import resolve_status, apply_file_status
pytestmark = pytest.mark.no_test_database
NOW=datetime(2026,10,11,12)

def job(status,minutes=0,progress=None):
    return SimpleNamespace(status=status,progress=progress or {},created_at=NOW-timedelta(minutes=minutes),updated_at=NOW-timedelta(minutes=minutes),job_id='test')

def test_metadata_is_not_parsing():
    assert resolve_status({'assets':0,'rows':0})['status']=='unparsed'

def test_any_writer_results_recognized_without_workflow_flag():
    assert resolve_status({'assets':2,'rows':3})['status']=='parsed'
    assert resolve_status({'assets':2,'rows':0})['status']=='partial'

def test_tasks_lifecycle_and_stale_running_are_distinct():
    c={'assets':2,'rows':3}
    for js,expected in [('queued','queued'),('running','parsing'),('failed','failed'),('completed','parsed')]:
        assert resolve_status(c,job=job(js),now=NOW)['status']==expected
    assert resolve_status(c,job=job('running',20),now=NOW)['status']=='needs_check'
    assert resolve_status(c,job=job('running',progress={'needs_check':True}),now=NOW)['status']=='needs_check'

def test_other_ai_results_after_failure_override_failed_attempt():
    assert resolve_status({'assets':2,'rows':3},latest_output=NOW,job=job('failed',60),now=NOW)['status']=='parsed'

def test_completed_job_without_results_cannot_fake_parsed():
    assert resolve_status({'assets':0,'rows':0},job=job('completed'),now=NOW)['status']=='needs_check'

def test_main_complete_then_new_si_pending():
    state=resolve_status({'assets':5,'rows':8})
    state['files']=[{'role':'main','name':'main.pdf','status':'parsed'},{'role':'si','name':'supplement.pdf','status':'unparsed'}]
    apply_file_status(state)
    assert state['status']=='partial' and state['parsed_files']==1 and state['total_files']==2
    assert 'SI supplement.pdf' in state['message']
    state['files'][1]['status']='parsed';state['status']='parsed';apply_file_status(state)
    assert state['status']=='parsed' and state['parsed_files']==2

def test_new_si_uploaded_after_job_completion_requires_more_work():
    assert resolve_status({'assets':5,'rows':8},job=job('completed',60),latest_file=NOW,now=NOW)['status']=='partial'
