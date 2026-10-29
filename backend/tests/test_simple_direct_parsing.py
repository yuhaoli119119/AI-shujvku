"""Direct saves remain available with same-paper writer admission."""
import copy
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from app.db.models import Paper, RebuildPaperFile, RebuildDataRow, WorkflowJob
from app.mcp.context import mcp_auth_context
from app.mcp.v2_contracts import SaveDataRows
from app.mcp.v2_server import save_data_rows
from test_mcp_v2 import mcp_env
from test_ordinary_parsing import env, start
from test_reading_assistance import reading_env, body, base
from test_rebuild_mutation_audit import audit_env, row_body


def test_main_only_start_is_blocked_by_unverified_old_job(env):
    with env['factory']() as session:
        session.get(RebuildPaperFile, env['file']).role = 'main'
        session.add(WorkflowJob(job_id='old-unknown', type='rebuild_external_extract', status='running',
            payload={'paper_id': str(env['paper'])}))
        session.commit()
    response = start(env, request_id='new-direct-request')
    assert response.status_code == 409, response.text
    assert 'paper_has_other_unresolved_writer_or_reviewer' in response.text
    with env['factory']() as session:
        assert session.scalar(select(func.count()).select_from(WorkflowJob).where(
            WorkflowJob.type == 'ai_extract_rebuild')) == 0
        assert session.get(WorkflowJob, 'old-unknown').status == 'running'


def test_reading_save_is_blocked_by_unverified_old_job(reading_env):
    e = reading_env
    with e['factory']() as session:
        session.get(RebuildPaperFile, e['file']).role = 'main'
        session.add(WorkflowJob(job_id='unknown',type='rebuild_external_extract',status='running',
            payload={'paper_id':str(e['paper'])}))
        session.commit()
    payload = body(e, None)
    payload.pop('job_id')
    payload['input_revision'] = '0' * 64
    payload['expected_reading_revision'] = '1' * 64
    payload['content_scope'] = 'main_and_si'
    response = e['client'].post(base(e), json=payload)
    assert response.status_code == 409, response.text
    assert 'paper_has_other_unresolved_writer_or_reviewer' in response.text
    saved = e['client'].get(base(e)).json()
    assert saved['abstract_zh'] is None
    with e['factory']() as session:
        assert session.get(Paper, e['paper']).reading_guide['research_background'] == 'old content'


def test_mcp_direct_write_requires_old_writer_to_reach_verified_terminal(mcp_env):
    e = mcp_env
    with e['factory']() as session:
        session.add(WorkflowJob(job_id='old',type='rebuild_external_extract',status='running',
            payload={'paper_id':str(e['paper'])}))
        session.commit()
    rows = row_body(e)['rows']
    with mcp_auth_context('writer-fixture'):
        request = SaveDataRows(paper_id=e['paper'],request_id=str(uuid4()),expected_revision='0'*64,rows=rows)
        from app.services.rebuild_workflow_service import RebuildWorkflowError
        with pytest.raises(RebuildWorkflowError, match='paper_has_other_unresolved'):
            save_data_rows(request)
    with e['factory']() as session:
        job=session.get(WorkflowJob,'old')
        job.status='failed'
        job.progress={'native_terminal_verified':True}
        session.commit()
    with mcp_auth_context('writer-fixture'):
        result = save_data_rows(request)
        assert result['rows_imported'] == 1
        assert save_data_rows(request)['replayed'] is True
    with e['factory']() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 1


def test_same_request_id_new_content_conflicts_without_overwriting(reading_env):
    e=reading_env
    first=body(e,None);first.pop('job_id')
    assert e['client'].post(base(e),json=first).status_code == 200
    second=copy.deepcopy(first);second['one_sentence']='更新后的文章概述。'
    response=e['client'].post(base(e),json=second)
    assert response.status_code == 409, response.text
    assert response.json()['detail']=='mutation_request_id_reused'
    with e['factory']() as session:
        assert session.scalar(select(func.count()).select_from(Paper)) == 1
        assert session.get(Paper,e['paper']).reading_guide['beginner_zh']['one_sentence'] == first['one_sentence']


def test_client_writes_directly_without_preflight_or_staging(tmp_path):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/ordinary_parse_client.py'
    spec=importlib.util.spec_from_file_location('direct_client',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Client:
        calls=[]
        def request(self,path,body,**kw):
            self.calls.append(path)
            return {'saved':True}
    client=Client();directory=tmp_path/'unused'
    assert module.durable_write(client,directory,'/api/rebuild/rows/import',{'rows':[]})['saved']
    assert client.calls == ['/api/rebuild/rows/import']
    assert not directory.exists()
