"""C fixtures: source-role priority and object-versioned independent review."""
import copy
import hashlib
from uuid import uuid4
import pytest
from sqlalchemy import select
from app.db.models import RebuildPaperFile, RebuildDataValue, RebuildDataRow, AuditLog
from test_rebuild_mutation_audit import audit_env, row_body, assets
from test_rebuild_acceptance import freeze_body, review_body, base
from test_mcp_v2 import mcp_env, begin_fixture


@pytest.mark.no_test_database
@pytest.mark.parametrize('ids', [['not-a-uuid'], [None], 42, ['valid-looking-but-invalid']])
def test_legacy_malformed_candidate_source_ids_remain_unknown(ids):
    from unittest.mock import MagicMock
    from app.services.rebuild_workflow_service import _trusted_candidate_role
    session=MagicMock()
    assert _trusted_candidate_role(session,uuid4(),{'source_ids':ids})=='unknown'
    session.scalars.assert_not_called()


def main_file(env):
    with env['factory']() as session:
        si=session.get(RebuildPaperFile,env['file'])
        path=env['settings'].storage_root/'main.pdf'
        path.write_bytes((env['settings'].storage_root/si.storage_path).read_bytes()+b'\nmain source')
        file=RebuildPaperFile(paper_id=env['paper'],role='main',storage_path=path.name,original_filename=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),file_size=path.stat().st_size)
        session.add(file);session.commit();return str(file.id)


def controlled_grant(env,frozen,monkeypatch,*,witness=None,request_id='c-controlled'):
    import asyncio,json
    from app.config import get_settings
    from app.services.codex_web_dispatch import ThreadStatus
    from app.services.rebuild_task_authorization import issue_reviewer_authorization
    from app.utils import project_paths
    key=env['settings'].storage_root.parent/'c-witness-authority.key'
    key.write_bytes(b'separate-fixture-authority-key-32bytes-minimum');key.chmod(0o600)
    monkeypatch.setenv('LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE',str(key));get_settings.cache_clear()
    project=env['settings'].storage_root.parent/'c-witness-project';evidence=project/'outputs/evidence';evidence.mkdir(parents=True,exist_ok=True)
    monkeypatch.setattr(project_paths,'PROJECT_ROOT',project)
    async def native(thread_id,**kwargs):
        return ThreadStatus('completed','active',None,None,turn_id=thread_id+'-turn',thread_id=thread_id,cwd='/opt/literature-ai')
    options={};artifact=None
    if witness is not None:
        artifact=evidence/(request_id+'.json')
        artifact.write_text(json.dumps({'format':'coordinator_correction_witness_v1','paper_id':str(env['paper']),
            'writer_thread_id':'writer-native','events':witness},sort_keys=True))
        options={'correction_event_ids':[r['event_id'] for r in witness], 'writer_ownership_evidence':artifact,
                 'writer_ownership_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}
    with env['factory']() as session:
        issued=asyncio.run(issue_reviewer_authorization(session,frozen['job_id'],request_id=request_id,owner='writer',
            reviewer_task_id='review-native',reviewer_thread_id='review-native',writer_thread_id='writer-native',
            evidence_root=evidence,issuer_key=key.read_bytes(),settings=get_settings(),native_read=native,**options));session.commit()
    return issued,artifact


def send_value(env,fid,raw,*,unit='eV',condition=None,label='Table 1'):
    body=row_body(env);row=body['rows'][0]
    if condition:
        row['condition']=condition
        row['row_key']='condition-'+str(condition['temperature_k'])
    cell=row['values'][0];cell.update(raw_value=raw,numeric_value=float(raw),unit=unit)
    cell['sources'][0].update(file_id=str(fid),quote=raw,label=label)
    response=env['client'].post('/api/rebuild/rows/import',json=body)
    assert response.status_code==200,response.text
    assert response.json()['results'][0]['values'][0]['persisted'],response.text
    assert response.json()['results'][0]['values'][0]['outcome'] in {'written','conflict','duplicate'},response.text
    return response.json()


@pytest.mark.parametrize('order',['si-first','main-first'])
def test_registered_main_priority_preserves_si_candidates_and_sources(audit_env,order):
    env=audit_env;main=main_file(env)
    data=[(str(env['file']),'1.0000'),(main,'2.0000')]
    if order=='main-first':data.reverse()
    for fid,raw in data:send_value(env,fid,raw)
    with env['factory']() as session:
        cell=session.scalar(select(RebuildDataValue))
        assert cell.raw_value=='2.0000'
        assert cell.conflict['status']=='value_conflict'
        assert {c['raw_value'] for c in cell.conflict['candidates']}=={'1.0000','2.0000'}
        assert cell.conflict['source_priority']['preferred_role']=='main'
        assert all(c['source_ids'] for c in cell.conflict['candidates'])


def test_different_conditions_and_units_never_silently_convert(audit_env):
    env=audit_env;main=main_file(env)
    send_value(env,env['file'],'1.0000',condition={'temperature_k':300})
    send_value(env,main,'2.0000',condition={'temperature_k':400})
    with env['factory']() as session:
        assert len(list(session.scalars(select(RebuildDataRow))))==2
    # Separate fixture identity with same conditions but incompatible units.
    send_value(env,env['file'],'3.0000',condition={'temperature_k':500})
    send_value(env,main,'4.0000',unit='kJ/mol',condition={'temperature_k':500})
    with env['factory']() as session:
        row=session.scalar(select(RebuildDataRow).where(RebuildDataRow.condition['temperature_k'].as_integer()==500))
        value=session.scalar(select(RebuildDataValue).where(RebuildDataValue.row_id==row.id))
        assert value.raw_value=='3.0000' and value.unit=='eV'
        assert value.conflict['status']=='unit_conflict'


def test_labels_do_not_promote_untrusted_si_to_main(audit_env):
    env=audit_env
    send_value(env,env['file'],'1.0000')
    send_value(env,env['file'],'2.0000',label='MAIN TEXT authoritative')
    with env['factory']() as session:
        value=session.scalar(select(RebuildDataValue))
        assert value.raw_value=='1.0000'
        assert 'source_priority' not in value.conflict


def test_failed_item_formal_fix_and_independent_resolution(audit_env):
    env=audit_env
    initial={'request_id':'asset-original','assets':[{'asset_key':'review-target','asset_type':'table','file_id':str(env['file']),
        'context_text':'Original stored text','caption':'Original caption'}]}
    response=env['client'].post(assets(env)+'/import',json=initial)
    assert response.status_code==200,response.text
    aid=response.json()['results'][0]['asset_id']
    frozen_response=env['client'].post(base(env)+'/freeze',json=freeze_body(env))
    assert frozen_response.status_code==200,frozen_response.text
    frozen=frozen_response.json()
    version=next(i['object_version'] for i in frozen['object_versions'] if i['object_id']==aid)
    failed={'issue_id':'caption-1','kind':'rebuild_visual_assets','object_id':aid,'object_version':version,
            'source_file_ids':[str(env['file'])],'required_change':'Correct the caption against the source.'}
    request=review_body(env,frozen,'needsfix');request['failed_items']=[failed]
    reviewer=env['client_for']('other')
    try:
        endpoint=base(env)+f'/{frozen["job_id"]}/reviews'
        assert env['client'].post(endpoint,json=request).status_code==409
        response=reviewer.post(endpoint,json=request);assert response.status_code==200,response.text
        # Formal source-preserving import update creates real before/after and receipt.
        fixed=copy.deepcopy(initial);fixed['request_id']='asset-fix';fixed['assets'][0]['caption']='Corrected caption'
        saved=env['client'].post(assets(env)+'/import',json=fixed);assert saved.status_code==200,saved.text
        event_id=saved.json()['audit']['event_ids'][0]
        newfreeze=freeze_body(env);newfreeze['request_id']='freeze-fixed'
        newer=env['client'].post(base(env)+'/freeze',json=newfreeze).json()
        current=next(i['object_version'] for i in newer['object_versions'] if i['object_id']==aid)
        resolved={'prior_freeze_id':frozen['job_id'],'issue_id':'caption-1','kind':'rebuild_visual_assets','object_id':aid,
            'prior_object_version':version,'current_object_version':current,'correction_event_id':event_id,'note':'Independently re-read corrected caption.'}
        review=review_body(env,newer,'partial');review['request_id']='review-fixed';review['resolved_items']=[resolved]
        path=base(env)+f'/{newer["job_id"]}/reviews'
        assert env['client'].post(path,json=review).status_code==409
        # A different actor freezing the same correction must not let the
        # correction's author masquerade as an independent reviewer.
        alternate=freeze_body(env);alternate['request_id']='other-writer-freeze'
        otherfreeze=reviewer.post(base(env)+'/freeze',json=alternate).json()
        self_review=copy.deepcopy(review);self_review['expected_revision']=otherfreeze['revision']
        denied=env['client'].post(base(env)+f'/{otherfreeze["job_id"]}/reviews',json=self_review)
        assert denied.status_code==400 and denied.json()['detail']=='correction_writer_cannot_close_own_issue',denied.text
        bad=copy.deepcopy(review);bad['resolved_items'][0]['prior_object_version']='0'*64
        assert reviewer.post(path,json=bad).status_code==400
        accepted=reviewer.post(path,json=review);assert accepted.status_code==200,accepted.text
        assert accepted.json()['review']['resolved_items'][0]['issue_id']=='caption-1'
        assert reviewer.post(path,json=review).json()['replayed']
        # Old finding cannot be attached to current version by guessing the new freeze.
        stale=review_body(env,newer,'needsfix');stale['request_id']='stale-issue';stale['failed_items']=[failed]
        assert reviewer.post(path,json=stale).status_code==400
        with env['factory']() as session:
            from app.db.models import WorkflowJob
            assert session.get(WorkflowJob,frozen['job_id']).result['failed_items'][0]['object_version']==version
    finally:reviewer.close()


@pytest.mark.parametrize('same_principal',[False,True])
def test_source_bound_section_replacement_uses_dedicated_correction_receipt(audit_env,monkeypatch,same_principal):
    env=audit_env
    with env['factory']() as session:sha=session.get(RebuildPaperFile,env['file']).sha256
    root=f'/api/rebuild/papers/{env["paper"]}'
    def section(key,text,conflicts=None):
        return {'request_id':key,'sections':[{'file_id':str(env['file']),'source_sha256':sha,
            'section_key':key,'page_basis':'source_pdf','section_title':'Synthetic heading','text':text,
            'explanation_zh':'合成测试章节','page_start':1,'page_end':1,'conflicts_with_section_ids':conflicts or [],
            'sources':[{'page_number':1,'quote':'Synthetic table','locator':'first paragraph'}]}]}
    response=env['client'].post(root+'/sections/import',json=section('old-section','Synthetic table'))
    assert response.status_code==200,response.text
    old=response.json()['items'][0]['section_id']
    frozen=env['client'].post(base(env)+'/freeze',json=freeze_body(env)).json()
    target=next(i for i in frozen['object_versions'] if i['object_id']==old)
    reviewer=env['client_for']('other')
    try:
        review=review_body(env,frozen,'needsfix');review['failed_items']=[{'issue_id':'section-1','kind':'paper_sections','object_id':old,
            'object_version':target['object_version'],'source_file_ids':[str(env['file'])],'required_change':'Replace incomplete extraction boundary.'}]
        response=reviewer.post(base(env)+f'/{frozen["job_id"]}/reviews',json=review);assert response.status_code==200,response.text
        response=env['client'].post(root+'/sections/import',json=section('new-section','Synthetic table: s 1.00s',[old]))
        assert response.status_code==200,response.text
        new=response.json()['items'][0]['section_id']
        sections={s['id']:s for s in env['client'].get(root+'/sections').json()['items']}
        correction={'request_id':'section-correction','corrections':[{'kind':'extraction_boundary','file_id':str(env['file']),
            'source_sha256':sha,'old_section':{'section_id':old,'content_sha256':sections[old]['provenance']['content_sha256']},
            'new_sections':[{'section_id':new,'content_sha256':sections[new]['provenance']['content_sha256']}],
            'reason':'Correct boundary and preserve original.', 'sources':[{'page_number':1,'quote':'Synthetic table','locator':'first paragraph'}]}]}
        saved=env['client'].post(root+'/sections/corrections',json=correction);assert saved.status_code==200,saved.text
        eid=saved.json()['items'][0]['audit_id']
        body=freeze_body(env);body['request_id']='section-refreeze'
        newer=env['client'].post(base(env)+'/freeze',json=body).json()
        current=next(i for i in newer['object_versions'] if i['object_id']==new)
        review=review_body(env,newer,'partial');review['request_id']='section-independent-recheck'
        review['resolved_items']=[{'prior_freeze_id':frozen['job_id'],'issue_id':'section-1','kind':'paper_sections',
            'object_id':old,'current_object_id':new,'prior_object_version':target['object_version'],
            'current_object_version':current['object_version'],'correction_event_id':eid,'note':'Independently checked replacement source boundary.'}]
        client=reviewer;headers={}
        if same_principal:
            from app.services.rebuild_correction_witness import event_hash
            from app.services.rebuild_workflow_service import RebuildWorkflowError
            from uuid import UUID
            grant,_=controlled_grant(env,newer,monkeypatch,request_id='chapter-no-witness')
            review['reviewer_task_id']='review-native';client=env['client']
            denied=client.post(base(env)+f'/{newer["job_id"]}/reviews',json=review,headers={'X-Reviewer-Authorization':grant['authorization']})
            assert denied.status_code==400 and denied.json()['detail']=='correction_writer_cannot_close_own_issue',denied.text
            with env['factory']() as session:
                event=session.get(AuditLog,UUID(eid));hashed=event_hash(event)
            witness=[{'event_id':eid,'event_sha256':hashed,'prior_freeze_id':frozen['job_id'],'issue_id':'section-1'}]
            bad=copy.deepcopy(witness);bad[0]['event_sha256']='0'*64
            with pytest.raises(RebuildWorkflowError,match='correction_witness_event_mismatch'):
                controlled_grant(env,newer,monkeypatch,witness=bad,request_id='chapter-bad-witness')
            grant,artifact=controlled_grant(env,newer,monkeypatch,witness=witness,request_id='chapter-bound-witness')
            original=artifact.read_bytes();artifact.write_bytes(original+b' ')
            rejected=client.post(base(env)+f'/{newer["job_id"]}/reviews',json=review,headers={'X-Reviewer-Authorization':grant['authorization']})
            assert rejected.status_code==400 and rejected.json()['detail']=='correction_witness_artifact_invalid',rejected.text
            artifact.write_bytes(original);headers={'X-Reviewer-Authorization':grant['authorization']}
        response=client.post(base(env)+f'/{newer["job_id"]}/reviews',json=review,headers=headers);assert response.status_code==200,response.text
        if same_principal:
            assert response.json()['review']['reviewer_identity']['correction_witness']['basis']=='current_coordinator_witness_not_historical_event_identity'
        assert env['client'].get(root+'/delivery-package').json()['independent_issue_summary']['open']==0
        assert old in {s['id'] for s in env['client'].get(root+'/sections').json()['items']}
    finally:reviewer.close()


def test_committed_partial_ack_loss_new_process_and_actor_isolation(mcp_env):
    import subprocess,sys,json
    from app.mcp.context import mcp_auth_context
    from app.mcp.v2_server import save_data_rows
    from app.mcp.v2_contracts import SaveDataRows
    from app.services import external_paper_work as work
    from app.services.rebuild_workflow_service import RebuildWorkflowError
    env=mcp_env;begun=begin_fixture(env)
    rows=row_body(env)['rows']
    rows[0]['values'].append({'field_name':'local-si-failure','numeric_value':42,'sources':[]})
    request=SaveDataRows(paper_id=env['paper'],work_id=begun['work_id'],request_id='lost-ack',expected_revision=begun['work_revision'],rows=rows)
    with mcp_auth_context('writer-fixture'):
        first=save_data_rows(request)
    assert first['rows_partial']==1 and first['results'][0]['values'][0]['persisted']
    # Discard caller's ACK and rebuild Python/SQLAlchemy process. Only read the
    # exact stored receipt: no task/model dispatch, replay or fixture mutation.
    code="""import json,os,sys
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.external_paper_work import receipt
with Session(create_engine(os.environ['LITAI_DATABASE_URL'])) as s:
 print(json.dumps(receipt(s,UUID(sys.argv[1]),sys.argv[2],'mcp:writer','lost-ack'),default=str))
"""
    result=subprocess.run([sys.executable,'-c',code,str(env['paper']),begun['work_id']],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    recovered=json.loads(result.stdout)
    assert recovered['replayed'] and recovered['rows_partial']==1
    with mcp_auth_context('writer-fixture'):
        replay=save_data_rows(request)
    assert replay['replayed']
    with env['factory']() as session:
        assert len(list(session.scalars(select(RebuildDataValue))))==1
        assert work.state(session,env['paper'],'mcp:writer',begun['work_id'])['status']=='running'
        with pytest.raises(RebuildWorkflowError,match='work_not_owned'):
            work.receipt(session,env['paper'],begun['work_id'],'mcp:other','lost-ack')
        session.rollback()
        with pytest.raises(RebuildWorkflowError,match='revision_conflict'):
            work.require_work(session,env['paper'],begun['work_id'],'mcp:writer',begun['work_revision'])


def test_new_freeze_cannot_hide_open_issue_and_reading_change_blocks_old_review(audit_env):
    from app.db.models import Paper,WorkflowJob
    from test_reading_assistance import body as reading_body
    env=audit_env
    saved=env['client'].post('/api/rebuild/rows/import',json=row_body(env));assert saved.status_code==200
    frozen=env['client'].post(base(env)+'/freeze',json=freeze_body(env)).json()
    target=next(i for i in frozen['object_versions'] if i['kind']=='rebuild_data_values')
    review=review_body(env,frozen,'needsfix')
    review['failed_items']=[{'issue_id':'field-1','kind':target['kind'],'object_id':target['object_id'],
        'object_version':target['object_version'],'source_file_ids':[str(env['file'])],'required_change':'Independently verify this value.'}]
    other=env['client_for']('other')
    try:
        response=other.post(base(env)+f'/{frozen["job_id"]}/reviews',json=review);assert response.status_code==200,response.text
        body=freeze_body(env);body['request_id']='next-freeze'
        newer=env['client'].post(base(env)+'/freeze',json=body).json()
        omitted=review_body(env,newer);omitted['request_id']='omitted-open-issue'
        response=other.post(base(env)+f'/{newer["job_id"]}/reviews',json=omitted)
        assert response.status_code==400 and response.json()['detail']=='open_independent_issues_require_resolution',response.text
        delivery=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/delivery-package').json()
        assert delivery['independent_issue_summary']['open']==1
        # A formal reading save does not alter scientific snapshot revision,
        # but must still invalidate a new freeze's object versions at review.
        with env['factory']() as session:
            paper=session.get(Paper,env['paper']);paper.abstract='A catalyst improves sulfur conversion under tested conditions.'
            session.add(WorkflowJob(job_id='reading-without-model',type='ai_extract_rebuild',status='running',payload={'paper_id':str(env['paper'])}))
            session.commit()
        payload=reading_body(env,'reading-without-model')
        response=env['client'].post(f'/api/rebuild/papers/{env["paper"]}/reading-assistance',json=payload)
        assert response.status_code==200,response.text
        response=other.post(base(env)+f'/{newer["job_id"]}/reviews',json=review_body(env,newer))
        assert response.status_code==409 and response.json()['detail']=='acceptance_frozen_revision_changed',response.text
    finally:other.close()


@pytest.mark.parametrize('same_principal',[False,True])
def test_reading_specific_receipt_can_resolve_guide_issue(audit_env,monkeypatch,same_principal):
    from app.db.models import Paper,WorkflowJob
    from test_reading_assistance import body as reading_body
    env=audit_env
    with env['factory']() as session:
        session.get(Paper,env['paper']).abstract='A catalyst improves sulfur conversion under tested conditions.'
        session.add(WorkflowJob(job_id='reading-formal-correction',type='ai_extract_rebuild',status='running',
            payload={'paper_id':str(env['paper']),'actor':'writer'},progress={'thread_id':'writer-native'}))
        session.commit()
    path=f'/api/rebuild/papers/{env["paper"]}/reading-assistance'
    payload=reading_body(env,'reading-formal-correction')
    assert env['client'].post(path,json=payload).status_code==200
    frozen=env['client'].post(base(env)+'/freeze',json=freeze_body(env)).json()
    target=next(i for i in frozen['object_versions'] if i['kind']=='reading_module' and i['object_id']=='guide')
    issue={'issue_id':'guide-1','kind':'reading_module','object_id':'guide','object_version':target['object_version'],
           'source_file_ids':[str(env['file'])],'required_change':'Correct guide conclusion.'}
    reviewer=env['client_for']('other')
    try:
        review=review_body(env,frozen,'needsfix');review['failed_items']=[issue]
        assert reviewer.post(base(env)+f'/{frozen["job_id"]}/reviews',json=review).status_code==200
        payload=reading_body(env,'reading-formal-correction');payload['request_id']='guide-fix';payload['main_finding']='Corrected guide finding'
        response=env['client'].post(path,json=payload);assert response.status_code==200,response.text
        with env['factory']() as session:
            event=session.scalar(select(AuditLog).where(AuditLog.action=='beginner_zh_v1',AuditLog.target_id=='guide-fix'))
            eid=str(event.id)
        body=freeze_body(env);body['request_id']='guide-fixed-freeze'
        newer=env['client'].post(base(env)+'/freeze',json=body).json()
        version=next(i['object_version'] for i in newer['object_versions'] if i['kind']=='reading_module' and i['object_id']=='guide')
        review=review_body(env,newer,'partial');review['request_id']='guide-recheck'
        review['resolved_items']=[{'prior_freeze_id':frozen['job_id'],'issue_id':'guide-1','kind':'reading_module','object_id':'guide',
            'prior_object_version':target['object_version'],'current_object_version':version,'correction_event_id':eid,'note':'Independent guide recheck.'}]
        headers={};final_reviewer=reviewer
        if same_principal:
            import asyncio
            from app.config import get_settings
            from app.services.codex_web_dispatch import ThreadStatus
            from app.services.rebuild_task_authorization import issue_reviewer_authorization
            from app.utils import project_paths
            key=env['settings'].storage_root.parent/'c-review-authority.key'
            key.write_bytes(b'separate-fixture-authority-key-32bytes-minimum');key.chmod(0o600)
            monkeypatch.setenv('LITAI_REBUILD_REVIEW_AUTHORITY_KEY_FILE',str(key));get_settings.cache_clear()
            project=env['settings'].storage_root.parent/'c-review-project';evidence=project/'outputs/evidence';evidence.mkdir(parents=True)
            monkeypatch.setattr(project_paths,'PROJECT_ROOT',project)
            async def native(thread_id,**kwargs):
                return ThreadStatus('completed','active',None,None,turn_id=thread_id+'-turn',thread_id=thread_id,cwd='/opt/literature-ai')
            with env['factory']() as session:
                grant=asyncio.run(issue_reviewer_authorization(session,newer['job_id'],request_id='c-reading-authority',owner='writer',
                    reviewer_task_id='review-native',reviewer_thread_id='review-native',writer_thread_id='writer-native',
                    evidence_root=evidence,issuer_key=key.read_bytes(),settings=get_settings(),native_read=native));session.commit()
            review['reviewer_task_id']='review-native';headers={'X-Reviewer-Authorization':grant['authorization']};final_reviewer=env['client']
        response=final_reviewer.post(base(env)+f'/{newer["job_id"]}/reviews',json=review,headers=headers)
        assert response.status_code==200,response.text
        assert env['client'].get(f'/api/rebuild/papers/{env["paper"]}/delivery-package').json()['independent_issue_summary']['open']==0
    finally:reviewer.close()
