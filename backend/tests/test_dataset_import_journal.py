"""Import journal refuses replay after an unknown write outcome."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from dataset_package_import import dataset_version, durable_request
pytestmark = pytest.mark.no_test_database

class Response:
    def raise_for_status(self): pass
    def json(self): return {'id': 'saved'}

def test_request_journal_resume_and_conflict(tmp_path):
    calls = []
    spec = {'method':'POST','body':{'x':1}}
    def send(): calls.append(1); return Response()
    assert durable_request(tmp_path,'example',spec,send)=={'id':'saved'}
    assert durable_request(tmp_path,'example',spec,send)=={'id':'saved'}
    assert len(calls)==1
    with pytest.raises(RuntimeError, match='journal request mismatch'):
        durable_request(tmp_path,'example',{'method':'POST','body':{'x':2}},send)

def test_unknown_outcome_is_not_replayed(tmp_path):
    calls=[]
    def fail(): calls.append(1); raise TimeoutError('lost response')
    with pytest.raises(TimeoutError): durable_request(tmp_path,'unknown',{'a':1},fail)
    with pytest.raises(RuntimeError, match='unknown request outcome'):
        durable_request(tmp_path,'unknown',{'a':1},fail)
    assert len(calls)==1

def test_no_file_availability_version():
    assert dataset_version('U139',{}, {})=='availability-2026-10-28'
