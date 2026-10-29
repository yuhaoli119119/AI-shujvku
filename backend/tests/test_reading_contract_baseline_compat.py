"""Prove current baseline contracts and the real pre-upgrade fingerprint format."""
import copy
import hashlib
from app.db.models import AuditLog, Paper, WorkflowJob
from app.schemas.reading_assistance import ReadingAssistanceSave
from app.services import reading_assistance as service
from test_reading_assistance import reading_env, base, body, create
from test_rebuild_mutation_audit import audit_env
from test_reading_abstract_recovery import source_pdf, start_patch, patch


def test_actual_pre_upgrade_fingerprint_replays_without_new_write(reading_env):
    e=reading_env;job=create(e);payload=body(e,job['job_id'])
    old=ReadingAssistanceSave(**payload).model_dump(mode='json')
    old.pop('save_mode');old.pop('abstract_source_revision');old.pop('expected_reading_revision')
    with e['factory']() as s:
        j=s.get(WorkflowJob,job['job_id']);j.status='completed'
        s.add(AuditLog(paper_id=e['paper'],action=service.VERSION,source=service.VERSION,
            target_type='paper_reading',target_id=payload['request_id'],
            payload={'actor':'writer','fingerprint':service.digest(old),'receipt':{'saved':True,'replayed':False}}))
        before=copy.deepcopy(s.get(Paper,e['paper']).reading_guide);s.commit()
    r=e['client'].post(base(e),json=payload)
    assert r.status_code==200 and r.json()['replayed'] is True
    with e['factory']() as s:assert s.get(Paper,e['paper']).reading_guide==before


def test_mismatched_source_is_rejected_without_partial_attribution(reading_env):
    e=reading_env;source_pdf(e);job=start_patch(e);payload=patch(e,job['job_id'])
    payload['abstract_source_revision']='0'*64
    r=e['client'].post(base(e),json=payload)
    assert r.status_code==409 and r.json()['detail']=='abstract_source_revision_conflict'
    with e['factory']() as session:
        paper=session.get(Paper,e['paper'])
        assert paper.abstract is None
        assert 'abstract_translation' not in paper.reading_guide


def test_missing_source_uses_full_scope_without_claiming_an_abstract(reading_env):
    e=reading_env;source_pdf(e,abstract=False)
    r=e['client'].post(base(e)+'/jobs',json={'request_id':'missing-source-current-contract','scope':'abstract_only'})
    assert r.status_code==200 and r.json()['scope']=='full'
    context=e['client'].get(base(e)+'/context').json()['abstract_for_translation']
    assert not context['text'] and context['source'] is None
