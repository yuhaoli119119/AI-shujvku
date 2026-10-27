"""Isolated fixture: same-job OpenAI lifecycle, idempotent intent and lost ACK."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from app.db.models import WorkflowJob
from app.services import ordinary_openai_recovery as recovery
from app.services import codex_web_dispatch as native_dispatch
from app.services.ai_extract_service import sync_job_status
from app.services.codex_web_dispatch import ThreadStatus, CodexWebDispatchError
from test_ordinary_parsing import env, start, cp
from test_rebuild_mutation_audit import audit_env
from test_reading_abstract_recovery import science


def native(**changes):
    evidence = ({'id': 'original', 'status': 'completed', 'input_text': 'original task',
                 'final_report': 'Original work stopped at TLS EOF', 'native_error': None},
                {'id': 'forward', 'status': 'completed', 'input_text': 'forwarded instruction',
                 'final_report': 'Wrong follow-up final', 'native_error': None})
    values = dict(turn_status='completed', goal_status=None, latest_progress=None,
        final_report='Wrong follow-up final', thread_id='owned', turn_id='forward',
        turn_ids=('original', 'forward'), turn_evidence=evidence,
        model='gpt-6-luna', model_provider='openai', cwd='/opt/literature-ai',
        thread_status='idle', goal_observed=True)
    return ThreadStatus(**{**values, **changes})


def bind(e):
    response = start(e, model='gpt-6-luna'); assert response.status_code == 200, response.text
    jid = response.json()['job_id']
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid); job.status = 'running'
        job.progress = {**job.progress, 'thread_id': 'owned', 'turn_id': 'original',
            'effective_provider': 'openai', 'effective_model': 'gpt-6-luna',
            'start_effective_provider': 'openai', 'dispatch_started': True}
        session.commit()
    return jid


def endpoint(e, jid):
    return f'/api/rebuild/papers/{e["paper"]}/ai-extract/jobs/{jid}/resume'


def test_exact_original_terminal_stops_pending_job_without_adopting_forward(env, monkeypatch):
    e = env; jid = bind(e)
    async def read(*a, **kwargs):
        assert kwargs.get('include_goal', True)
        return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    before = science(e)
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings']))
        session.commit()
        assert job.status == 'failed' and job.error == 'ordinary_native_terminal_with_pending_stages'
        assert job.progress['turn_id'] == 'original'
        assert job.progress['native_terminal']['turn_id'] == 'original'
        assert job.progress['native_terminal']['observed_thread_tail'] == 'forward'
        assert job.progress['native_terminal_verified']
        assert not job.progress.get('actual_inference_verified')
        assert all(part['status'] == 'pending' for f in job.progress['file_plan'].values() for part in f['stages'].values())
    assert science(e) == before


def test_explicit_resume_keeps_job_original_request_and_checkpoints_once(env, monkeypatch):
    e = env; jid = bind(e); assert cp(e, jid).status_code == 200
    calls = []
    monkeypatch.setattr('app.services.ai_extract_service._arm_dispatches', lambda entries: calls.extend(entries))
    async def read(*a, **kwargs): return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    before = science(e)
    with e['factory']() as session:
        previous = session.get(WorkflowJob, jid); plan = copy.deepcopy(previous.progress['file_plan'])
        old_payload = copy.deepcopy(previous.payload)
    response = e['client'].post(endpoint(e, jid), json={'request_id': 'resume-one'})
    assert response.status_code == 200, response.text
    assert response.json()['job_id'] == jid and len(calls) == 1
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'resume-one'}).status_code == 200
    assert len(calls) == 1
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'resume-one', 'model': 'cn:deepseek-v4.1-flash'}).status_code == 409
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid)
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 1
        assert job.payload['request_id'] == old_payload['request_id']
        assert job.payload['chosen_model'] == 'gpt-6-luna'
        assert job.progress['file_plan'] == plan and job.progress['checkpoint_revision'] == 1
        assert job.payload['continuation']['owned_turn_id'] == 'original'
        assert job.payload['continuation']['turn_id'] == 'forward'
        assert job.payload['continuation']['history_ids'] == ['original', 'forward']
        assert job.progress['native_attempts'][0]['dispatch_prompt'] == old_payload['dispatch_prompt']
        assert job.progress['native_attempts'][0]['turn_id'] == 'original'
        assert job.progress['turn_id'] is None and not job.progress['native_terminal_verified']
    assert science(e) == before


@pytest.mark.parametrize('changes', [
    {'model_provider': 'workbuddy'}, {'model': 'cn:deepseek-v4.1-flash'}, {'cwd': '/other'},
    {'goal_observed': False}, {'goal_status': 'active'}, {'thread_status': 'active'},
    {'thread_active_flags': True}, {'turn_ids': ('foreign',)},
    {'turn_evidence': ({'id': 'original', 'status': 'completed'}, {'id': 'forward', 'status': 'inProgress'})},
])
def test_unknown_identity_or_inflight_turn_retains_writer_without_dispatch(env, monkeypatch, changes):
    e = env; jid = bind(e); calls = []
    monkeypatch.setattr('app.services.ai_extract_service._arm_dispatches', lambda entries: calls.extend(entries))
    async def read(*a, **kwargs): return native(**changes)
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    response = e['client'].post(endpoint(e, jid), json={'request_id': 'unsafe-resume'})
    assert response.status_code == 409 and not calls
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid)
        assert job.status == 'running' and job.progress['turn_id'] == 'original'
        assert not job.progress.get('recovery_receipts')


def test_other_writer_actor_and_unknown_native_request_cannot_resume(env, monkeypatch):
    e = env; jid = bind(e); calls = []
    monkeypatch.setattr('app.services.ai_extract_service._arm_dispatches', lambda entries: calls.extend(entries))
    async def read(*a, **kwargs): return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid)
        job.payload = {**job.payload, 'actor': 'writer'}
        job.progress = {**job.progress, 'events': [{'method': 'turn/start', 'phase': 'request_prepared', 'rpc_id': 'unknown'}]}
        session.commit()
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'unknown'}).status_code == 409
    other = e['client_for']('other')
    try: assert other.post(endpoint(e, jid), json={'request_id': 'wrong-actor'}).status_code == 409
    finally: other.close()
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid); job.progress = {**job.progress, 'events': []}
        session.add(WorkflowJob(job_id='other-writer', type='rebuild_external_extract', status='running', payload={'paper_id': str(e['paper'])})); session.commit()
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'busy'}).status_code == 409
    assert not calls


def staged_job():
    job = SimpleNamespace(type='ai_extract_rebuild', job_id='same', status='running', error=None, result=None,
        payload={'paper_id': 'paper', 'parse_contract': 'ordinary_parse_v1', 'chosen_mode': 'codex_web_ordinary',
                 'chosen_model': 'gpt-6-luna', 'attempt_token': 'old-token', 'dispatch_prompt': 'old-token task'},
        progress={'thread_id': 'owned', 'turn_id': 'original', 'file_plan': {'f': {'stages': {'read': {'status': 'pending'}}}}})
    recovery.stage_resume(job, native(), 'recover-once')
    return job


@pytest.mark.no_test_database
def test_lost_ack_adopts_exact_single_native_turn_and_never_blindly_replays():
    job = staged_job(); prompt = job.payload['dispatch_prompt']
    job.progress.update(dispatch_started=True, events=[{'phase': 'request_prepared', 'method': 'turn/start',
        'rpc_id': 'sent-once', 'params': {'threadId': 'owned', 'input': [{'type': 'text', 'text': prompt}]}}])
    evidence = (*native().turn_evidence, {'id': 'new', 'status': 'inProgress', 'input_text': prompt})
    observed = native(turn_id='new', turn_ids=('original', 'forward', 'new'), turn_evidence=evidence,
        turn_status='inProgress', thread_status='active')
    proof = recovery.recover_turn(job, observed)
    assert proof['turn_id'] == 'new' and proof['no_request_replayed']
    assert recovery.recover_turn(job, replace(observed, turn_evidence=(*evidence[:-1], {**evidence[-1], 'input_text': 'foreign'}))) is None
    assert recovery.recover_turn(job, replace(observed, turn_ids=('foreign', 'forward', 'new'))) is None
    job.progress['events'] = []
    assert recovery.recover_turn(job, observed) is None


@pytest.mark.no_test_database
@pytest.mark.parametrize('failure', [None, 'provider', 'permissions', 'goal', 'intervening', 'lost_ack'])
def test_resume_native_transport_has_no_new_thread_goal_or_cross_provider(monkeypatch, failure):
    job = staged_job(); binding = job.payload['continuation']; calls = []; events = []; reads = 0
    def thread():
        return {'id': 'owned', 'cwd': '/opt/literature-ai', 'model': 'gpt-6-luna', 'modelProvider': 'openai',
                'status': {'type': 'idle'}, 'turns': [{'id': t, 'status': 'completed'} for t in binding['history_ids']]}
    async def rpc(ws, method, params, *args):
        nonlocal reads
        calls.append((method, params))
        if method == 'thread/read':
            reads += 1; value = thread()
            if reads == 2 and failure == 'intervening': value['turns'].append({'id': 'foreign', 'status': 'inProgress'})
            return {'thread': value}
        if method == 'thread/goal/get': return {'goal': {'threadId': 'owned', 'status': 'active'} if failure == 'goal' else None}
        if method == 'thread/resume':
            value = {'thread': thread(), 'cwd': '/opt/literature-ai', 'model': 'gpt-6-luna', 'modelProvider': 'openai',
                'approvalPolicy': 'never', 'sandbox': {'type': 'dangerFullAccess'}, 'reasoningEffort': 'high'}
            if failure == 'provider': value['modelProvider'] = 'workbuddy'
            if failure == 'permissions': value['approvalPolicy'] = 'on-request'
            return value
        if method == 'turn/start':
            if failure == 'lost_ack': raise CodexWebDispatchError('RPC timeout; outcome unknown')
            return {'threadId': 'owned', 'turn': {'id': 'new'}}
        raise AssertionError(method)
    monkeypatch.setattr(native_dispatch, '_call', rpc)
    if failure:
        with pytest.raises(CodexWebDispatchError): asyncio.run(recovery.dispatch(None, binding, job.payload['dispatch_prompt'], 1, events.append))
    else:
        result = asyncio.run(recovery.dispatch(None, binding, job.payload['dispatch_prompt'], 1, events.append))
        assert result.thread_id == 'owned' and result.turn_id == 'new' and result.start_effective_provider == 'openai'
        assert not result.actual_inference_verified
    assert all(method not in ('thread/start', 'thread/goal/set') for method, _ in calls)
    assert sum(method == 'turn/start' for method, _ in calls) == (1 if failure in (None, 'lost_ack') else 0)
    if any(method == 'thread/resume' for method, _ in calls):
        params = next(params for method, params in calls if method == 'thread/resume')
        assert params['modelProvider'] == 'openai' and params['model'] == 'gpt-6-luna'
        assert params['approvalPolicy'] == 'never' and params['sandbox'] == 'danger-full-access'


def test_processed_stage_terminal_uses_owned_report_and_preserves_science(env, monkeypatch):
    e = env; jid = bind(e)
    for revision, stage in enumerate(('read', 'assets', 'rows')):
        response = cp(e, jid, request_id='finish-' + stage, stage=stage, expected_revision=revision,
                      status='partial', reason='Specific source limitation')
        assert response.status_code == 200, response.text
    before = science(e)
    async def read(*a, **kwargs): return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings']))
        session.commit()
        assert job.status == 'completed' and job.progress['content_status'] == 'partial'
        assert job.result['final_report'] == 'Original work stopped at TLS EOF'
        assert job.result['file_ids'] == []
        assert job.result['scientific_acceptance'] == 'pending'
        assert job.progress['turn_id'] == 'original'
    assert science(e) == before


def test_unknown_native_ack_is_recovered_and_cannot_reissue_rpc(env, monkeypatch):
    e = env; jid = bind(e)
    async def read_old(*a, **kwargs): return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read_old)
    monkeypatch.setattr('app.services.ai_extract_service._arm_dispatches', lambda entries: None)
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'recover-lost'}).status_code == 200
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid); prompt = job.payload['dispatch_prompt']
        job.progress = {**job.progress, 'dispatch_started': True, 'events': [{
            'method': 'turn/start', 'phase': 'request_prepared', 'rpc_id': 'lost-ack',
            'params': {'threadId': 'owned', 'input': [{'type': 'text', 'text': prompt}]}}]}
        session.commit()
    # A second recovery ID cannot resend an outstanding intent.
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'duplicate'}).status_code == 409
    observed = native(turn_id='new', turn_ids=('original', 'forward', 'new'),
        turn_evidence=(*native().turn_evidence, {'id': 'new', 'status': 'inProgress', 'input_text': prompt}),
        thread_status='active', turn_status='inProgress')
    async def read_new(*a, **kwargs): return observed
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read_new)
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings']))
        session.commit()
        assert job.status == 'running' and job.progress['turn_id'] == 'new'
        assert job.progress['openai_native_request_resolutions'][0]['rpc_id'] == 'lost-ack'
        assert not job.progress['native_terminal_verified']
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'recover-lost'}).status_code == 200
    assert e['client'].post(endpoint(e, jid), json={'request_id': 'still-active'}).status_code == 409


@pytest.mark.no_test_database
def test_native_read_returns_per_turn_evidence_without_changing_legacy_latest(monkeypatch):
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(native_dispatch.websockets, 'connect', lambda *a, **kw: Connection())
    async def rpc(ws, method, *args):
        if method == 'thread/goal/get': return {'goal': None}
        return {'thread': {'id': 'owned', 'cwd': '/opt/literature-ai', 'model': 'gpt-6-luna',
            'modelProvider': 'openai', 'status': {'type': 'idle'}, 'turns': [
                {'id': 'original', 'status': 'completed', 'items': [
                    {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'original input'}]},
                    {'type': 'agentMessage', 'text': 'actual original final', 'phase': 'final_answer'}]},
                {'id': 'forward', 'status': 'completed', 'items': [
                    {'type': 'agentMessage', 'text': 'forwarded final', 'phase': 'final_answer'}]}]}}
    monkeypatch.setattr(native_dispatch, '_call', rpc)
    result = asyncio.run(native_dispatch.read_thread_status('owned', SimpleNamespace(
        codex_web_ipc_uri='ws://fixture', codex_web_dispatch_timeout_seconds=1)))
    assert result.turn_id == 'forward' and result.final_report == 'forwarded final'
    assert result.turn_evidence[0]['id'] == 'original'
    assert result.turn_evidence[0]['final_report'] == 'actual original final'
    assert result.turn_evidence[0]['input_text'] == 'original input'


def test_checkpoint_advance_during_native_read_wins_cas_and_no_dispatch(env, monkeypatch):
    e = env; jid = bind(e); calls = []
    monkeypatch.setattr('app.services.ai_extract_service._arm_dispatches', lambda entries: calls.extend(entries))
    async def read(*a, **kwargs):
        # This independent transaction would fail if the native read held the job lock.
        from sqlalchemy import text
        with e['factory']() as session:
            session.execute(text("SET LOCAL lock_timeout='500ms'"))
            job = session.get(WorkflowJob, jid, with_for_update=True)
            job.progress = {**job.progress, 'checkpoint_revision': 7}
            session.commit()
        return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    response = e['client'].post(endpoint(e, jid), json={'request_id': 'stale-read'})
    assert response.status_code == 409 and 'changed_during_readback' in response.text and not calls
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid)
        assert job.progress['checkpoint_revision'] == 7 and job.progress['turn_id'] == 'original'
        assert not job.progress.get('recovery_receipts')


def test_historical_completed_job_is_not_reconciled_or_downgraded(env, monkeypatch):
    e = env; jid = bind(e)
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid); job.status = 'completed'
        job.result = {'preserved': 'legacy outcome'}; previous = copy.deepcopy(job.progress); session.commit()
    async def forbidden(*a, **kwargs): raise AssertionError('completed history must not read native')
    monkeypatch.setattr(native_dispatch, 'read_thread_status', forbidden)
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings']))
        assert job.status == 'completed' and job.result == {'preserved': 'legacy outcome'} and job.progress == previous


def test_unresolved_native_request_never_releases_writer_and_retry_uses_same_job(env, monkeypatch):
    e = env; jid = bind(e)
    async def read(*a, **kwargs): return native()
    monkeypatch.setattr(native_dispatch, 'read_thread_status', read)
    with e['factory']() as session:
        job = session.get(WorkflowJob, jid)
        job.progress = {**job.progress, 'events': [{'method': 'turn/start', 'phase': 'request_prepared', 'rpc_id': 'unresolved'}]}
        session.commit()
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings'])); session.commit()
        assert job.status == 'running' and not job.progress.get('native_terminal_verified')
        assert job.progress['native_readback_error'] == 'ordinary_openai_native_request_unresolved'
        job.progress = {**job.progress, 'events': []}; session.commit()
    with e['factory']() as session:
        job = asyncio.run(sync_job_status(session, session.get(WorkflowJob, jid), e['settings'])); session.commit()
        assert job.status == 'failed'
        from app.services.ai_extract_service import serialize_job
        public = serialize_job(job, session)
        assert public['can_resume'] and not public['can_retry'] and public['resume_endpoint'].endswith(jid + '/resume')
    response = start(e, model='gpt-6-luna', request_id='no-new-job', retry=True)
    assert response.status_code == 409 and 'use_same_job_resume' in response.text
    with e['factory']() as session:
        assert session.scalar(select(func.count()).select_from(WorkflowJob)) == 1
