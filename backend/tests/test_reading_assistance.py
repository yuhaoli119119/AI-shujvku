import asyncio
import copy
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from app.db.models import Paper, WorkflowJob, RebuildDataValue, RebuildVisualAsset, AuditLog
from app.schemas.reading_assistance import ReadingAssistanceSave
from app.services import reading_assistance as service
from app.services.codex_web_dispatch import ThreadStatus
from test_rebuild_mutation_audit import audit_env


@pytest.fixture
def reading_env(audit_env,monkeypatch):
    async def noop(**kw):pass
    monkeypatch.setattr('app.services.ai_extract_service._background_dispatch',noop)
    with audit_env['factory']() as s:
        p=s.get(Paper,audit_env['paper']);p.abstract='A catalyst improves sulfur conversion under tested conditions.'
        p.reading_guide={'research_background':'old content'};p.comprehensive_analysis={'retained_science':{'x':42}}
        s.commit()
    return audit_env


def base(e):return f'/api/rebuild/papers/{e["paper"]}/reading-assistance'

def create(e,rid='generate-1'):
    r=e['client'].post(base(e)+'/jobs',json={'request_id':rid});assert r.status_code==200,r.text;return r.json()


def body(e,jid):
    context=e['client'].get(base(e)+'/context').json()
    return {'request_id':'save-1','job_id':jid,'input_revision':context['input_revision'],
        'abstract_zh':'在所测试条件下，一种催化剂改善了硫转化。','one_sentence':'研究比较了催化剂对硫转化的作用。',
        'problem':'硫转化缓慢','main_finding':'测试条件下转化改善',
        'guide':{'research_objective':'比较反应动力学','limitations':'没有跨工况验证'},
        'argument_flow':['问题：转化慢','证据：比较催化剂','结论：仅适用于实验条件'],
        'content_scope':'partial_text','missing':['正文尚未完整读取']}


def test_real_api_save_readback_preserves_original_and_namespaces_idempotent(reading_env):
    e=reading_env;j=create(e);p=body(e,j['job_id'])
    r=e['client'].post(base(e),json=p);assert r.status_code==200,r.text
    assert e['client'].post(base(e),json=p).json()['replayed']
    altered=copy.deepcopy(p);altered['abstract_zh']='不同译文'
    assert e['client'].post(base(e),json=altered).status_code==409
    read=e['client'].get(base(e)).json();assert read['abstract_original'].startswith('A catalyst') and read['abstract_zh']==p['abstract_zh']
    with e['factory']() as s:
        paper=s.get(Paper,e['paper']);assert paper.reading_guide['research_background']=='old content'
        assert paper.comprehensive_analysis['retained_science']=={'x':42}
        assert paper.reading_guide['beginner_zh']['scientific_acceptance']=='not_implied'
        assert s.scalar(select(func.count()).select_from(RebuildDataValue))==0


def test_refresh_context_and_saved_reads_never_create_generation_and_retries_same_id(reading_env):
    e=reading_env
    for _ in range(3):
        assert e['client'].get(base(e)).status_code==200
        assert e['client'].get(base(e)+'/context').status_code==200
    with e['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==0
    j=create(e);assert create(e)['job_id']==j['job_id'] and create(e,'new-click-while-running')['job_id']==j['job_id']
    assert len(service.prompt(e['paper'],j['job_id']))<4000


def test_partial_abstract_missing_and_full_text_scope_guard(reading_env):
    e=reading_env;j=create(e);p=body(e,j['job_id']);p['content_scope']='main_and_si'
    assert e['client'].post(base(e),json=p).status_code==409
    p['content_scope']='partial_text';p['abstract_zh']='';p['abstract_missing_reason']='摘要译文暂未生成'
    assert e['client'].post(base(e),json=p).status_code==200
    read=e['client'].get(base(e)).json();assert '摘要译文暂未生成' in read['guide']['missing']
    assert read['abstract_original'].startswith('A catalyst')


def test_bound_job_source_revision_ownership_and_csrf(reading_env):
    e=reading_env;j=create(e);p=body(e,j['job_id'])
    wrong=copy.deepcopy(p);wrong['job_id']='other-job';assert e['client'].post(base(e),json=wrong).status_code==409
    wrong=copy.deepcopy(p);wrong['evidence']=[{'asset_id':str(uuid4()),'question':'what','finding':'finding'}];assert e['client'].post(base(e),json=wrong).status_code==409
    wrong=copy.deepcopy(p);wrong['abstract_zh']='English only';assert e['client'].post(base(e),json=wrong).status_code==409
    with e['factory']() as s:
        paper=s.get(Paper,e['paper']);paper.abstract='Changed original';s.commit()
    assert e['client'].post(base(e),json=p).status_code==409
    c=e['client_for']('other');c.headers['X-CSRF-Token']='bad';assert c.post(base(e),json=p).status_code==403


def test_failed_generation_does_not_touch_scientific_or_stop_next_parse_and_retry(reading_env,monkeypatch):
    e=reading_env;j=create(e)
    async def done(*a,**k):return ThreadStatus('completed','complete',None,'could not translate',thread_id='owned',turn_id='turn',native_error='exceeded retry limit, last status: 429 Too Many Requests')
    monkeypatch.setattr('app.services.codex_web_dispatch.read_thread_status',done)
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);job.progress={**job.progress,'thread_id':'owned','turn_id':'turn'};s.commit()
    with e['factory']() as s:
        job=asyncio.run(service.reconcile(s,s.get(WorkflowJob,j['job_id']),e['settings']));s.commit()
        assert job.status=='failed' and job.progress['native_terminal_verified']
        assert '429' in job.progress['native_error'] and '限流' in job.error
        paper=s.get(Paper,e['paper']);assert paper.reading_guide=={'research_background':'old content'}
    assert create(e,'retry-2')['job_id']!=j['job_id']
    assert e['client'].get(base(e)).json()['abstract_original']


def test_same_paper_writer_blocks_guide_without_restarting_unknown(reading_env):
    e=reading_env
    with e['factory']() as s:
        s.add(WorkflowJob(job_id='unknown-writer',type='rebuild_external_extract',status='running',payload={'paper_id':str(e['paper'])}));s.commit()
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'one'}).status_code==409


def test_ordinary_parse_can_save_optional_reading_without_new_task(reading_env):
    e=reading_env
    with e['factory']() as s:
        s.add(WorkflowJob(job_id='aiext-ordinary',type='ai_extract_rebuild',status='running',payload={'paper_id':str(e['paper'])}));s.commit()
    p=body(e,'aiext-ordinary');assert e['client'].post(base(e),json=p).status_code==200
    with e['factory']() as s:
        assert s.get(WorkflowJob,'aiext-ordinary').status=='running'
        assert s.scalar(select(func.count()).select_from(WorkflowJob))==1


def test_legacy_guide_update_preserves_new_saved_namespace(reading_env):
    from app.services.figure_reading_service import FigureReadingService
    e=reading_env;j=create(e);p=body(e,j['job_id']);assert e['client'].post(base(e),json=p).status_code==200
    with e['factory']() as s:
        result=FigureReadingService(s).save_paper_reading_guide(e['paper'],{'research_background':'legacy edit'})
        assert result['guide']['beginner_zh']['one_sentence']==p['one_sentence']
        assert result['guide']['research_background']=='legacy edit'


@pytest.mark.parametrize('length',[64,65,128])
def test_full_request_identity_saves_and_replays_with_legacy_audit_limit(reading_env,length):
    e=reading_env;j=create(e);p=body(e,j['job_id']);p['request_id']='r'*length
    result=e['client'].post(base(e),json=p);assert result.status_code==200,result.text
    # A lost ACK can be replayed even after native finalization, without a new audit/write.
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);job.status='completed';s.commit()
    result=e['client'].post(base(e),json=p);assert result.status_code==200,result.text
    assert result.json()['replayed']
    changed=copy.deepcopy(p);changed['one_sentence']='不同正文'
    assert e['client'].post(base(e),json=changed).status_code==409
    with e['factory']() as s:
        audits=list(s.scalars(select(AuditLog).where(AuditLog.action==service.VERSION)))
        assert len(audits)==1 and len(audits[0].target_id)<=64
        assert s.get(Paper,e['paper']).abstract.startswith('A catalyst')
        assert s.get(Paper,e['paper']).reading_guide['research_background']=='old content'


def test_long_request_ids_with_same_prefix_are_distinct_not_truncated(reading_env):
    e=reading_env;j=create(e);p=body(e,j['job_id']);p['request_id']='x'*64+'first'
    assert e['client'].post(base(e),json=p).status_code==200
    next_body=body(e,j['job_id']);next_body['request_id']='x'*64+'second'
    # Simulate the same ordinary task's next valid source revision, without creating a second task.
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);job.payload={**job.payload,'reading_revision':service.reading_revision(s.get(Paper,e['paper']))};s.commit()
    result=e['client'].post(base(e),json=next_body);assert result.status_code==200,result.text
    with e['factory']() as s:
        audits=list(s.scalars(select(AuditLog).where(AuditLog.action==service.VERSION)))
        assert len(audits)==2 and len({a.target_id for a in audits})==2
