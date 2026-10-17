"""Historical indexing and declarations. No native polling or scheduling authority."""
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID, uuid4
from sqlalchemy import select
from app.db.models import AuditLog, WorkflowJob
from app.services.extraction_coverage import _digest
from app.services.history_evidence import catalog, load_entry
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.services.staged_extraction import context as stage_context

VERSION = 'history_reconciliation_v1'
EVENT = 'history_reconciliation_recorded'
JOB_TYPES = ('ai_extract_rebuild', 'rebuild_external_extract')
ACTIVE = {'queued', 'dispatching', 'running'}


def _mapping(value):return value if isinstance(value, dict) else {}


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):return None


def job_identity(job):
    payload, progress = _mapping(job.payload), _mapping(job.progress)
    return {'job_id':job.job_id, 'type':job.type, 'paper_id':payload.get('paper_id'),
            'run_id':payload.get('run_id'), 'attempt_token':payload.get('attempt_token'),
            'thread_id':progress.get('thread_id'), 'turn_id':progress.get('turn_id')}


def job_revision(job):
    progress = _mapping(job.progress)
    return _digest([job_identity(job), job.status, job.created_at.isoformat(), job.updated_at.isoformat(),
                    {key:progress.get(key) for key in ('phase','content_status','needs_check','needs_attention','native_stop','readback')}])


def execution_observation(job):
    progress = _mapping(job.progress)
    identity = job_identity(job)
    stop = _mapping(progress.get('native_stop'))
    mode = progress.get('dispatch_mode') or _mapping(job.payload).get('chosen_mode')
    goal_ended = mode == 'codex_web_ordinary' or (mode == 'codex_web_target' and
                 stop.get('observed_goal_status') in {'paused','complete','completed'})
    try:
        UUID(stop.get('request_id',''))
        receipt_identity = bool(_time(stop.get('requested_at')) and _time(stop.get('last_checked_at')) and
            _time(stop['requested_at']) <= _time(stop['last_checked_at']) and
            stop.get('automatic_retry') is False and isinstance(stop.get('events'),list))
    except (ValueError,TypeError,AttributeError):receipt_identity = False
    verified = (receipt_identity and stop.get('verified_stop') is True and stop.get('phase') == 'verified_stopped' and
                identity['thread_id'] and identity['turn_id'] and stop.get('thread_id') == identity['thread_id'] and
                stop.get('start_turn_id') == identity['turn_id'] and
                stop.get('observed_turn_status') in {'completed','failed','interrupted'} and
                goal_ended and _time(stop.get('last_checked_at')) is not None)
    state = 'stopped_as_observed' if verified else 'ended_as_recorded' if job.status in {'completed','failed','cancelled','canceled'} else 'unknown'
    labels = {'stopped_as_observed':'曾核实该尝试停止（本轮未回查）',
              'ended_as_recorded':'库内结束记录（不等于科学通过或线程已停）', 'unknown':'当前执行状态未知，不据旧状态推断'}
    return {'state':state, 'label':labels[state], 'raw_status':job.status,
            'native_current_status':'not_polled_unknown', 'observed_at':stop.get('last_checked_at') if verified else None,
            'evidence_ref':{'source':'stored_native_stop','request_id':stop.get('request_id'),'thread_id':stop.get('thread_id'),
                            'start_turn_id':stop.get('start_turn_id'),'last_checked_at':stop.get('last_checked_at')} if verified else None,
            'can_unlock':False, 'scientific_acceptance':'not_implied'}


def context(session, paper_id, settings, snapshot, actor):
    stage = stage_context(session, paper_id, snapshot, actor)
    jobs = list(session.scalars(select(WorkflowJob).where(WorkflowJob.type.in_(JOB_TYPES),
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id)).order_by(WorkflowJob.created_at, WorkflowJob.job_id)))
    records = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == EVENT,
        AuditLog.source == VERSION).order_by(AuditLog.created_at, AuditLog.id)))
    inventory = catalog(settings)
    reports = [load_entry(settings, entry, paper_id, snapshot) for entry in inventory['entries'] if str(entry.paper_id) == str(paper_id)]
    revision = _digest([stage['binding'], [(j.job_id,job_revision(j)) for j in jobs],
        [(str(e.id),_digest(e.payload)) for e in records], inventory['revision'],
        [(r['report_id'],r['evidence_revision']) for r in reports]])
    return {'binding':stage['binding'], 'revision':revision, 'jobs':jobs, 'records':records,
            'reports':reports, 'catalog_status':inventory['status'], 'snapshot':snapshot}


def _record_fresh(event, ctx):
    data = event.payload
    report = next((r for r in ctx['reports'] if r['report_id'] == data['request']['report_id']), None)
    jobs = {j.job_id:j for j in ctx['jobs']}
    return bool(data['binding'] == ctx['binding'] and report and report.get('usable_for_index') and
                report['evidence_revision'] == data['report']['evidence_revision'] and all(
                    key in jobs and job_revision(jobs[key]) == revision for key,revision in data['job_revisions'].items()))


def record_summary(event, ctx, full=False):
    data = event.payload
    result = {'record_id':str(event.id), 'kind':data['request']['kind'], 'actor':data['actor'],
              'created_at':event.created_at.isoformat(), 'report_id':data['request']['report_id'],
              'freshness':'current_binding_only' if _record_fresh(event,ctx) else 'stale',
              'historical_verdict_as_reported':data['report'].get('historical_verdict_as_reported'),
              'semantic_basis':data['report'].get('semantic_basis'),
              'operator_declared_verdict':data['report'].get('operator_declared_verdict'),
              'current_acceptance':'not_granted', 'occupancy_changed':False}
    if full:result.update(request=data['request'], binding_as_recorded=data['binding'], report_as_recorded=data['report'])
    return result


def preview(payload, ctx):
    body = payload.model_dump(mode='json')
    if body['expected_revision'] != ctx['revision']:raise RebuildWorkflowError('history_revision_changed')
    if body['expected_binding'] != ctx['binding']:raise RebuildWorkflowError('history_binding_changed')
    errors = []
    if not body['note'].strip():errors.append('history_note_blank')
    report = next((r for r in ctx['reports'] if r['report_id'] == body['report_id']), None)
    if report is None or not report.get('usable_for_index'):errors.append('history_report_not_verified_for_index')
    elif body['expected_report_sha256'] != report['report_sha256']:errors.append('history_report_hash_mismatch')
    job_revisions = {}
    if body['kind'] == 'attempt_supersession':
        jobs = {j.job_id:j for j in ctx['jobs']}
        old, new = jobs.get(body['old_job_id']), jobs.get(body['new_job_id'])
        if not old or not new or (new.created_at,new.job_id) <= (old.created_at,old.job_id):
            errors.append('history_attempt_relation_invalid')
        else:
            identities = [job_identity(old),job_identity(new)]
            expected = {'old_job_id':old.job_id,'new_job_id':new.job_id,
                        'old_run_id':identities[0]['run_id'],'new_run_id':identities[1]['run_id'],
                        'old_attempt_token':identities[0]['attempt_token'],'new_attempt_token':identities[1]['attempt_token']}
            if not report or expected not in report.get('attempt_links',[]):errors.append('history_attempt_relation_not_in_evidence')
            job_revisions = {j.job_id:job_revision(j) for j in (old,new)}
    return {'valid':not errors, 'errors':errors, 'report':report, 'job_revisions':job_revisions,
            'registration_status':'index_candidate_only' if not errors else 'invalid_candidate',
            'current_acceptance':'not_granted', 'occupancy_changed':False, 'can_unlock':False, 'read_only':True}


def save(session, paper_id, payload, ctx, actor):
    if not isinstance(actor,str) or not actor.strip():raise RebuildWorkflowError('history_actor_required')
    fingerprint = _digest(payload.model_dump(mode='json',exclude_unset=True))
    previous = next((e for e in ctx['records'] if e.target_id == payload.request_id),None)
    if previous:
        if previous.payload['actor'] != actor or previous.payload['request_sha256'] != fingerprint:
            raise RebuildWorkflowError('history_request_id_reused')
        return {**previous.payload['receipt'], 'replayed':True}, None
    result = preview(payload,ctx)
    if not result['valid']:return None,result
    event_id = uuid4()
    receipt = {'record_id':str(event_id),'paper_id':str(paper_id), 'metadata_only':True,'replayed':False,
               'registration_status':'historical_index_recorded_not_accepted', 'current_acceptance':'not_granted',
               'occupancy_changed':False,'can_unlock':False,'binding_as_recorded':ctx['binding'],
               'report_as_recorded':result['report'], 'notice':'Original index/declaration receipt, not current freshness or native-state proof.'}
    session.add(AuditLog(id=event_id,paper_id=paper_id,action=EVENT,source=VERSION,target_type=payload.kind,target_id=payload.request_id,
        payload={'actor':actor,'request_sha256':fingerprint,'request':payload.model_dump(mode='json'),
                 'binding':ctx['binding'],'report':result['report'],'job_revisions':result['job_revisions'],'receipt':receipt}))
    session.flush()
    return receipt,result


def package(paper_id, ctx, view='attempts', offset=0, limit=25, expected_revision=None):
    if offset and expected_revision is None:raise RebuildWorkflowError('history_page_revision_required')
    if expected_revision is not None and expected_revision != ctx['revision']:raise RebuildWorkflowError('history_revision_changed')
    records = [record_summary(e,ctx) for e in ctx['records']]
    if view == 'attempts':
        latest = ctx['jobs'][-1].job_id if ctx['jobs'] else None
        items = []
        for job in reversed(ctx['jobs']):
            annotations = [e for e in ctx['records'] if e.payload['request']['kind'] == 'attempt_supersession' and
                           e.payload['request']['old_job_id'] == job.job_id]
            note = annotations[-1] if annotations else None
            fresh = note and _record_fresh(note,ctx)
            items.append({**job_identity(job),'revision':job_revision(job),'raw_status':job.status,
                'created_at':job.created_at.isoformat(),'updated_at':job.updated_at.isoformat(),
                'record_role':'latest_recorded' if job.job_id == latest else 'older_record',
                'selection_status':'superseded_declaration_only' if fresh else 'stale_declaration' if note else 'not_declared',
                'superseded_by_job_id':note.payload['request']['new_job_id'] if note else None,
                'observation':execution_observation(job), 'occupancy_changed':False})
    else:
        items = sorted(ctx['reports'],key=lambda r:r['report_id'])
    next_offset = offset+limit if offset+limit<len(items) else None
    return {'paper_id':str(paper_id),'view':view,'revision':ctx['revision'],'binding':ctx['binding'],
        'catalog_status':ctx['catalog_status'], 'items':items[offset:offset+limit], 'total':len(items),
        'offset':offset,'limit':limit,'has_more':next_offset is not None,'next_offset':next_offset,
        'next_page':f'/api/rebuild/papers/{paper_id}/history-reconciliation?'+urlencode(
            {'view':view,'offset':next_offset,'limit':limit,'expected_revision':ctx['revision']}) if next_offset is not None else None,
        'raw_active_records':sum(j.status in ACTIVE for j in ctx['jobs']),
        'latest_recorded_job_id':ctx['jobs'][-1].job_id if ctx['jobs'] else None,
        'current_native_execution':'unknown_not_polled','can_unlock':False,'occupancy_changed':False,
        'recent_registrations':records[-5:], 'read_only':True,'current_acceptance':'not_granted',
        'notice_zh':'库内任务记录、历史报告与当前原生执行分别核对；替代声明不证明旧任务已停，也不解除占用。历史结论及其范围保留，缺新登记不否定既有科学结论。正式独立验收仍另行进行。'}
