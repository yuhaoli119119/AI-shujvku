"""The ordinary flow saves to the real database without requiring job gates."""
import copy
from uuid import uuid4

from sqlalchemy import select, func
from app.db.models import Paper, RebuildPaperFile, RebuildDataRow, WorkflowJob
from app.mcp.context import mcp_auth_context
from app.mcp.v2_contracts import SaveDataRows
from app.mcp.v2_server import save_data_rows
from test_mcp_v2 import mcp_env
from test_ordinary_parsing import env, start
from test_reading_assistance import reading_env, body, base
from test_rebuild_mutation_audit import audit_env, row_body


def test_main_only_starts_even_with_old_unknown_job(env):
    with env['factory']() as session:
        session.get(RebuildPaperFile, env['file']).role = 'main'
        session.add(WorkflowJob(job_id='old-unknown', type='rebuild_external_extract', status='running',
            payload={'paper_id': str(env['paper'])}))
        session.commit()
    response = start(env, request_id='new-direct-request')
    assert response.status_code == 200, response.text
    assert response.json()['file_progress']['total_files'] == 1


def test_reading_saves_despite_old_job_and_stale_revisions(reading_env):
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
    assert response.status_code == 200, response.text
    saved = e['client'].get(base(e)).json()
    assert saved['abstract_zh'] == payload['abstract_zh']
    assert saved['guide']['content_scope'] in ('main_text', 'partial_text')


def test_mcp_direct_write_is_not_blocked_by_old_job_or_revision(mcp_env):
    e = mcp_env
    with e['factory']() as session:
        session.add(WorkflowJob(job_id='old',type='rebuild_external_extract',status='running',
            payload={'paper_id':str(e['paper'])}))
        session.commit()
    rows = row_body(e)['rows']
    with mcp_auth_context('writer-fixture'):
        request = SaveDataRows(paper_id=e['paper'],request_id=str(uuid4()),expected_revision='0'*64,rows=rows)
        result = save_data_rows(request)
        assert result['rows_imported'] == 1
        assert save_data_rows(request)['replayed'] is True
    with e['factory']() as session:
        assert session.scalar(select(func.count()).select_from(RebuildDataRow)) == 1


def test_same_request_id_new_content_still_saves_one_paper(reading_env):
    e=reading_env
    first=body(e,None);first.pop('job_id')
    assert e['client'].post(base(e),json=first).status_code == 200
    second=copy.deepcopy(first);second['one_sentence']='更新后的文章概述。'
    response=e['client'].post(base(e),json=second)
    assert response.status_code == 200, response.text
    with e['factory']() as session:
        assert session.scalar(select(func.count()).select_from(Paper)) == 1
        assert session.get(Paper,e['paper']).reading_guide['beginner_zh']['one_sentence'] == second['one_sentence']


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
