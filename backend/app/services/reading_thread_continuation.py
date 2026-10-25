"""Server-registered paper ownership; never accept a client-supplied thread ID."""
from __future__ import annotations

from sqlalchemy import select
from app.db.models import WorkflowJob
from app.services.codex_web_dispatch import CodexWebDispatchError, _call, _emit_event, _verify_goal, DispatchResult

BINDING = 'paper_native_thread_binding'
from app.services.paper_native_models import MODELS, DEFAULT as MODEL, require_model, fallback_reason
CWD = {'/opt/literature-ai', '/opt/AI-shujvku/literature-ai'}


def source_for(session, paper_id, source_job_id=None, actor=None):
    types = (BINDING, 'paper_reading_assistance', 'ai_extract_rebuild')
    if source_job_id:
        job = session.get(WorkflowJob, source_job_id)
        if not job or job.type not in types or (job.payload or {}).get('paper_id') != str(paper_id):
            raise ValueError('same_thread_ownership_missing')
    else:
        job = session.scalar(select(WorkflowJob).where(WorkflowJob.type.in_(types),
            WorkflowJob.payload.op('->>')('paper_id') == str(paper_id),
            WorkflowJob.progress.op('->>')('thread_id').isnot(None)).order_by(WorkflowJob.created_at.desc()))
    if not job:
        return None
    p, r = job.payload or {}, job.progress or {}
    if p.get('actor') and p['actor'] != actor:
        raise ValueError('same_thread_actor_mismatch')
    if ((r.get('effective_model') or p.get('chosen_model')) not in MODELS or not r.get('thread_id') or not r.get('turn_id') or
            job.status not in ('completed', 'failed') or not r.get('native_terminal_verified')):
        raise ValueError('same_thread_source_unverified_or_busy')
    return {'source_job_id': job.job_id, 'thread_id': r['thread_id'], 'turn_id': r['turn_id'], 'source_model':r.get('effective_model') or p['chosen_model']}


async def confirmed_source(session, paper_id, settings, source_job_id=None, actor=None):
    binding = source_for(session, paper_id, source_job_id, actor)
    if binding:
        from app.services.codex_web_dispatch import read_thread_status
        native = await read_thread_status(binding['thread_id'], settings)
        if (native is None or native.thread_id != binding['thread_id'] or
                native.turn_id != binding['turn_id'] or native.turn_status not in ('completed','failed','interrupted') or
                native.thread_status not in ('idle','notLoaded') or not native.goal_observed or
                native.goal_status not in (None,'complete','completed') or native.model_provider != 'workbuddy'):
            raise ValueError('same_thread_native_busy_or_unverified')
    return binding


def verify_thread(thread, binding):
    status = thread.get('status')
    kind = status.get('type') if isinstance(status, dict) else status
    turns = thread.get('turns')
    if (thread.get('id') != binding['thread_id'] or thread.get('cwd') not in CWD or
            thread.get('modelProvider') != 'workbuddy' or not isinstance(turns, list) or not turns or
            turns[-1].get('id') != binding['turn_id'] or
            turns[-1].get('status') not in ('completed', 'failed', 'interrupted') or
            kind not in ('idle', 'notLoaded') or isinstance(status, dict) and status.get('activeFlags')):
        raise CodexWebDispatchError('same_thread_identity_unknown_or_busy')
    return turns


async def prepare(ws, binding, timeout, on_event):
    tid = binding['thread_id']
    read = await _call(ws, 'thread/read', {'threadId': tid, 'includeTurns': True}, timeout, on_event)
    verify_thread(read.get('thread') or {}, binding)
    goal = await _call(ws, 'thread/goal/get', {'threadId': tid}, timeout, on_event)
    g = goal.get('goal')
    if ('goal' not in goal or g is not None and
            (not isinstance(g, dict) or g.get('threadId') != tid or g.get('status') not in (('complete','completed','paused','blocked') if binding.get('resume_paused') else ('complete','completed')))):
        raise CodexWebDispatchError('same_thread_goal_not_terminal')
    target = require_model(binding.get('target_model',binding.get('source_model',MODEL)))
    source = require_model(binding.get('source_model',MODEL))
    if target != source and binding.get('switch_reason') not in ('quota_exhausted','model_unavailable'):
        raise CodexWebDispatchError('unverified_model_switch')
    params = {'threadId':tid}
    if target != source:
        params.update(model=target,modelProvider='workbuddy',approvalPolicy='never',sandbox='danger-full-access',
            allowProviderModelFallback=False,config={'model_reasoning_effort':'high'})
    resumed = await _call(ws, 'thread/resume', params, timeout, on_event)
    verify_thread(resumed.get('thread') or {}, binding)
    if (resumed.get('model') != target or resumed.get('modelProvider') != 'workbuddy' or
            resumed.get('approvalPolicy') != 'never' or
            (resumed.get('sandbox') or {}).get('type') != 'dangerFullAccess' or resumed.get('cwd') not in CWD):
        raise CodexWebDispatchError('same_thread_model_or_permissions_mismatch')
    return read['thread']['turns']


async def dispatch(ws, binding, prompt, timeout, on_event):
    model = require_model(binding.get('target_model',binding.get('source_model',MODEL)))
    before = await prepare(ws, binding, timeout, on_event)
    await _emit_event(on_event, {'method':'thread/resume','phase':'effective_verified','effective_model':model,'effective_provider':'workbuddy','approval_policy':'never','sandbox_type':'dangerFullAccess'})
    tid = binding['thread_id']
    goal = await _call(ws, 'thread/goal/set', {'threadId': tid, 'objective': prompt, 'status': 'active'}, timeout, on_event)
    _verify_goal(goal, tid, active=True, objective=prompt)
    goal = await _call(ws, 'thread/goal/get', {'threadId': tid}, timeout, on_event)
    _verify_goal(goal, tid, active=True, objective=prompt)
    snapshot = await _call(ws, 'thread/read', {'threadId': tid, 'includeTurns': True}, timeout, on_event)
    t = snapshot.get('thread') or {}
    turns = t.get('turns') or []
    old_ids = [x['id'] for x in before]
    if t.get('id') != tid or [x.get('id') for x in turns[:len(before)]] != old_ids:
        raise CodexWebDispatchError('same_thread_history_changed_readback_required')
    new = turns[len(before):]
    if new:
        if len(new) != 1 or not new[0].get('id'):
            raise CodexWebDispatchError('same_thread_auto_start_unknown')
        await _emit_event(on_event, {'method': 'thread/read', 'phase': 'auto_start_adopted', 'thread_id': tid, 'turn_id': new[0]['id']})
        turn_id = new[0]['id']
    else:
        verify_thread(t, binding)
        result = await _call(ws, 'turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': prompt}],
            'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}, 'model': model, 'effort': 'high',
            'collaborationMode': {'mode': 'default', 'settings': {'model': model, 'reasoning_effort': 'high', 'developer_instructions': None}}}, timeout, on_event)
        turn_id = (result.get('turn') or {}).get('id')
        if not turn_id or result.get('threadId', tid) != tid or turn_id in old_ids:
            raise CodexWebDispatchError('same_thread_new_turn_unknown')
    return DispatchResult(thread_id=tid, turn_id=turn_id, model=model, goal_status='active',
        start_effective_model=model, start_effective_provider='workbuddy',
        start_collaboration_verification='verified_resume')


def recover_turn(job, status):
    """Only adopt one turn after the registered predecessor and our exact goal."""
    binding = job.payload['continuation']
    ids = list(status.turn_ids or ())
    if (status.thread_id != binding['thread_id'] or binding['turn_id'] not in ids or
            getattr(status, 'goal_objective', None) != job.payload['dispatch_objective']):
        return None
    new = ids[ids.index(binding['turn_id']) + 1:]
    return new[0] if len(new) == 1 and status.turn_id == new[0] else None


async def register(session, proof, settings):
    """Host-only CLI entry, deliberately not exposed through an HTTP endpoint.

    The host verifier supplies persisted coordinator receipts, not browser claims.
    """
    import hashlib
    import json
    import websockets
    from app.db.models import Paper, AuditLog
    from app.services.paper_db_phase import run_phase
    from app.services.rebuild_queue import paper_busy
    pid = proof['paper_id']
    tid = proof['thread_id']
    jid = 'paper-thread-' + hashlib.sha256(tid.encode()).hexdigest()[:32]

    def snapshot(worker):
        paper = worker.get(Paper, pid)
        if not paper or paper.paper_code != proof['code']:
            raise ValueError('same_thread_paper_identity_mismatch')
        if paper_busy(worker, pid):
            raise ValueError('same_thread_paper_busy')
        old = worker.get(WorkflowJob, jid)
        if old:
            if old.payload.get('paper_id') != pid or old.payload.get('actor') != proof['actor']:
                raise ValueError('same_thread_binding_identity_reused')
            return {'existing': True, 'job_id': jid, 'paper': paper}
        for other in worker.scalars(select(WorkflowJob).where(
                WorkflowJob.progress.op('->>')('thread_id') == tid)):
            if (other.payload or {}).get('paper_id') != pid:
                raise ValueError('same_thread_cross_paper_binding')
        return {'existing': False, 'job_id': jid, 'paper': paper}

    checked = await run_phase(session, snapshot)
    if checked['existing']:
        return jid
    timeout = settings.codex_web_dispatch_timeout_seconds
    events = []
    # No transaction, row lock, or scheduler advisory lock exists during IPC.
    async with websockets.connect(settings.codex_web_ipc_uri, open_timeout=timeout, max_size=32*1024*1024) as ws:
        if proof.get('archived'):
            # Host verifier proved the archived rollout and its terminal audit turn.
            # Restore that original thread only; never create a replacement.
            await _call(ws, 'thread/unarchive', {'threadId':tid}, timeout, events.append)
        await prepare(ws, {**proof,'source_model':proof.get('model',MODEL)}, timeout, events.append)
    def apply(worker):
        paper = worker.scalar(select(Paper).where(Paper.id == pid).with_for_update())
        if not paper or paper.paper_code != proof['code']:
            raise ValueError('same_thread_paper_identity_mismatch')
        old = worker.scalar(select(WorkflowJob).where(WorkflowJob.job_id == jid).with_for_update())
        if old:
            if old.payload.get('paper_id') != pid or old.payload.get('actor') != proof['actor']:
                raise ValueError('same_thread_binding_identity_reused')
            return jid
        if paper_busy(worker, pid):
            raise ValueError('same_thread_paper_busy')
        for other in worker.scalars(select(WorkflowJob).where(
                WorkflowJob.progress.op('->>')('thread_id') == tid)):
            if (other.payload or {}).get('paper_id') != pid:
                raise ValueError('same_thread_cross_paper_binding')
        worker.add(WorkflowJob(job_id=jid, type=BINDING, status='completed', library_name=paper.library_name,
            payload={'paper_id':pid, 'actor':proof['actor'], 'chosen_model':proof.get('model',MODEL), 'ownership_proof':proof},
            progress={'thread_id':tid, 'turn_id':proof['turn_id'], 'native_terminal_verified':True, 'events':events}))
        worker.add(AuditLog(action='paper_thread_registered', source='host_coordinator_receipt', paper_id=pid,
            target_type='workflow_job', target_id=jid, payload={'actor':proof['actor'], 'proof_sha256':hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}))
        worker.flush()
        return jid
    return await run_phase(session, apply)
