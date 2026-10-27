"""Explicit same-job OpenAI ordinary recovery. No provider fallback or new thread."""
from __future__ import annotations

import copy
from dataclasses import replace
from uuid import uuid4

MODEL = 'gpt-6-luna'
KIND = 'openai_ordinary_recovery_v1'
CWDS = {'/opt/literature-ai', '/opt/AI-shujvku/literature-ai'}
TERMINAL = {'completed', 'failed', 'interrupted'}


def applies(job):
    payload = job.payload or {}
    return (job.type == 'ai_extract_rebuild' and payload.get('parse_contract') == 'ordinary_parse_v1'
            and payload.get('chosen_mode') == 'codex_web_ordinary' and payload.get('chosen_model') == MODEL)


def _identity(job, native):
    progress = job.progress or {}
    if (native is None or native.thread_id != progress.get('thread_id') or native.cwd not in CWDS
            or native.model_provider != 'openai' or native.model != MODEL
            or progress.get('effective_provider', 'openai') not in (None, 'openai')
            or progress.get('effective_model', MODEL) not in (None, MODEL)):
        raise ValueError('ordinary_openai_native_identity_unverified')
    ids = list(native.turn_ids or ())
    evidence = list(native.turn_evidence or ())
    if (not ids or len(set(ids)) != len(ids) or native.turn_id != ids[-1]
            or [turn.get('id') for turn in evidence] != ids):
        raise ValueError('ordinary_openai_turn_history_unverified')
    if not native.goal_observed or native.goal_status is not None:
        raise ValueError('ordinary_openai_goal_not_confirmed_absent')
    return ids, evidence


def owned_status(job, native, *, require_idle=False):
    """Project only the recorded business turn, never a later manual follow-up."""
    ids, evidence = _identity(job, native)
    tid = (job.progress or {}).get('turn_id')
    if tid not in ids:
        raise ValueError('ordinary_openai_owned_turn_missing')
    original = evidence[ids.index(tid)]
    if require_idle or original.get('status') in TERMINAL:
        if (native.thread_status not in ('idle', 'notLoaded') or native.thread_active_flags
                or any(turn.get('status') not in TERMINAL for turn in evidence)):
            raise ValueError('ordinary_openai_thread_not_terminal')
    return replace(native, turn_id=tid, turn_status=original.get('status'),
        final_report=original.get('final_report'), native_error=original.get('native_error'),
        input_text=original.get('input_text'), latest_progress=original.get('latest_progress'))


def _unresolved_requests(progress):
    events = progress.get('events') or []
    answered = {event.get('response', {}).get('id') for event in events
                if event.get('phase') == 'response_received'}
    resolved = {item['rpc_id'] for item in progress.get('openai_native_request_resolutions', [])}
    return [event for event in events if event.get('phase') == 'request_prepared'
            and event.get('method') in ('thread/start', 'thread/resume', 'turn/start', 'thread/goal/set')
            and event.get('rpc_id') not in answered | resolved]


def stage_resume(job, native, request_id, model=None):
    """Called only by the explicit authorized resume route under its CAS/locks."""
    if model not in (None, MODEL):
        raise ValueError('ordinary_openai_model_switch_forbidden')
    original = owned_status(job, native, require_idle=True)
    if original.turn_status not in TERMINAL:
        raise ValueError('ordinary_openai_owned_turn_not_terminal')
    progress, payload = copy.deepcopy(job.progress or {}), copy.deepcopy(job.payload or {})
    if _unresolved_requests(progress):
        raise ValueError('ordinary_openai_native_request_unresolved')
    plan = progress.get('file_plan') or {}
    if not plan or all(part.get('status') == 'completed'
                       for file in plan.values() for part in file.get('stages', {}).values()):
        raise ValueError('ordinary_openai_no_remaining_stages')
    history = list(progress.get('native_attempts') or [])
    if len(history) >= 4:
        raise ValueError('same_thread_recovery_budget_exhausted')
    old_token = payload.get('attempt_token')
    if not old_token or old_token not in payload.get('dispatch_prompt', ''):
        raise ValueError('ordinary_openai_attempt_identity_missing')
    history.append({'thread_id': original.thread_id, 'turn_id': original.turn_id,
        'model': MODEL, 'provider': 'openai', 'turn_status': original.turn_status,
        'native_error': original.native_error, 'final_report': original.final_report,
        'reason': 'explicit_resume', 'recovery_request_id': request_id,
        'observed_thread_tail': native.turn_id, 'attempt_token': old_token,
        'dispatch_prompt': payload['dispatch_prompt'], 'dispatch_objective': payload.get('dispatch_objective'),
        'turn_context_proof': progress.get('turn_context_proof'), 'result': copy.deepcopy(job.result)})
    token = str(uuid4())
    instruction = payload['dispatch_prompt'].replace(old_token, token)
    # The full original instructions/checkpoints remain intact. This is a direct
    # execution instruction, with no model identity or dispatch request in it.
    instruction += ('\n继续本篇当前任务，直接执行未完成对象；不要把本指令转发给其它对话。'
                    '先读回原job检查点和回执，保留已完成阶段与所有有效成果；不另建job或线程。')
    binding = {'kind': KIND, 'source_job_id': job.job_id, 'thread_id': native.thread_id,
        'turn_id': native.turn_id, 'owned_turn_id': original.turn_id,
        'history_ids': list(native.turn_ids), 'source_model': MODEL, 'target_model': MODEL,
        'recovery_request_id': request_id}
    payload.update(attempt_token=token, dispatch_prompt=instruction,
                   dispatch_objective=instruction, continuation=binding)
    job.payload = payload
    job.progress = {**progress, 'native_attempts': history, 'previous_turn_id': original.turn_id,
        'turn_id': None, 'native_terminal_verified': False, 'dispatch_started': False,
        'phase': 'dispatching', 'needs_check': False, 'actual_inference_verified': False,
        'turn_context_proof': None, 'effective_model': None, 'effective_provider': None}
    job.status = 'dispatching'; job.error = None


def recover_turn(job, native):
    """Lost ACK is resolved by exact history+request+input, without another send."""
    binding = (job.payload or {}).get('continuation') or {}
    if binding.get('kind') != KIND or not (job.progress or {}).get('dispatch_started'):
        return None
    try:
        ids, evidence = _identity(job, native)
    except ValueError:
        return None
    before = binding.get('history_ids') or []
    if ids[:len(before)] != before or len(ids) != len(before) + 1:
        return None
    prompt = job.payload['dispatch_prompt']
    if evidence[-1].get('input_text') != prompt:
        return None
    requests = [event for event in (job.progress or {}).get('events', [])
        if event.get('phase') == 'request_prepared' and event.get('method') == 'turn/start'
        and event.get('params', {}).get('threadId') == binding['thread_id']
        and event.get('params', {}).get('input') == [{'type': 'text', 'text': prompt}]
        and event.get('rpc_id')]
    if len(requests) != 1:
        return None
    return {'rpc_id': requests[0]['rpc_id'], 'turn_id': ids[-1], 'thread_id': native.thread_id,
            'method': 'exact_native_history_and_input', 'no_request_replayed': True}


def apply_status(session, job, settings, native):
    """Execution terminal evidence and content progress remain separate."""
    progress = copy.deepcopy(job.progress or {})
    if not progress.get('turn_id'):
        recovered = recover_turn(job, native)
        if recovered:
            progress['turn_id'] = recovered['turn_id']
            progress['openai_native_request_resolutions'] = [
                *progress.get('openai_native_request_resolutions', []), recovered]
            job.progress = progress
    try:
        owned = owned_status(job, native)
        if owned.turn_status in TERMINAL and _unresolved_requests(job.progress or {}):
            raise ValueError('ordinary_openai_native_request_unresolved')
    except ValueError as exc:
        job.progress = {**progress, 'phase': 'needs_check', 'needs_check': True,
                        'automatic_retry': False, 'native_readback_error': str(exc)}
        session.flush(); return job
    observed = {'thread_id': owned.thread_id, 'turn_id': owned.turn_id,
        'turn_status': owned.turn_status, 'thread_status': native.thread_status,
        'observed_thread_tail': native.turn_id, 'provider': 'openai', 'model': MODEL,
        'goal_observed': True, 'goal_status': None}
    progress.update(readback=observed, native_readback_error=None)
    if owned.turn_status in TERMINAL:
        from app.services.ordinary_parsing import progress_summary
        summary = progress_summary(progress.get('file_plan') or {})
        processed = bool(summary['total_stages']) and summary['finished_stages'] == summary['total_stages']
        success = owned.turn_status == 'completed' and processed
        job.status = 'completed' if success else 'failed'
        job.error = None if success else ('ordinary_native_terminal_with_pending_stages' if
            owned.turn_status == 'completed' else 'ordinary_native_' + str(owned.turn_status) + ': ' + str(owned.native_error or '')[:1000])
        progress.update(native_terminal_verified=True, native_terminal=observed,
            phase=job.status, content_status=summary['content_status'], needs_check=not success,
            automatic_retry=False)
        if success:
            from app.services.ai_extract_service import _result_summary
            from uuid import UUID
            job.result = {**_result_summary(session, UUID(job.payload['paper_id'])),
                'file_progress': summary, 'final_report': (owned.final_report or '')[:2000],
                'file_ids': [file['file_id'] for file in summary['files']
                    if all(stage['status'] == 'completed' for stage in file['stages'].values())]}
    else:
        job.status = 'running'; job.error = None
        progress.update(phase='running', needs_check=False, native_terminal_verified=False)
    job.progress = progress
    session.flush(); return job


async def dispatch(ws, binding, prompt, timeout, on_event):
    from app.services.codex_web_dispatch import _call, _emit_event, DispatchResult, CodexWebDispatchError

    def verify(thread):
        state = thread.get('status') or {}
        kind = state.get('type') if isinstance(state, dict) else state
        turns = thread.get('turns')
        if (thread.get('id') != binding.get('thread_id') or thread.get('cwd') not in CWDS
                or thread.get('modelProvider') != 'openai' or thread.get('model') != MODEL
                or not isinstance(turns, list) or not turns
                or [turn.get('id') for turn in turns] != binding.get('history_ids')
                or turns[-1].get('id') != binding.get('turn_id')
                or any(turn.get('status') not in TERMINAL for turn in turns)
                or kind not in ('idle', 'notLoaded') or isinstance(state, dict) and state.get('activeFlags')):
            raise CodexWebDispatchError('ordinary_openai_resume_history_or_route_changed')

    async def no_goal():
        response = await _call(ws, 'thread/goal/get', {'threadId': binding['thread_id']}, timeout, on_event)
        if 'goal' not in response or response['goal'] is not None:
            raise CodexWebDispatchError('ordinary_openai_resume_goal_not_absent')

    if (binding.get('kind') != KIND or binding.get('source_model') != MODEL
            or binding.get('target_model') != MODEL):
        raise CodexWebDispatchError('ordinary_openai_resume_binding_invalid')
    tid = binding['thread_id']
    before = await _call(ws, 'thread/read', {'threadId': tid, 'includeTurns': True}, timeout, on_event)
    verify(before.get('thread') or {})
    await no_goal()
    resumed = await _call(ws, 'thread/resume', {'threadId': tid, 'model': MODEL, 'modelProvider': 'openai',
        'approvalPolicy': 'never', 'sandbox': 'danger-full-access', 'allowProviderModelFallback': False,
        'config': {'model_reasoning_effort': 'high'}}, timeout, on_event)
    verify(resumed.get('thread') or {})
    if (resumed.get('model') != MODEL or resumed.get('modelProvider') != 'openai'
            or resumed.get('approvalPolicy') != 'never' or resumed.get('cwd') not in CWDS
            or (resumed.get('sandbox') or {}).get('type') != 'dangerFullAccess'
            or resumed.get('reasoningEffort') != 'high'):
        raise CodexWebDispatchError('ordinary_openai_resume_permissions_unverified')
    await _emit_event(on_event, {'method': 'thread/resume', 'phase': 'effective_verified',
        'effective_model': MODEL, 'effective_provider': 'openai',
        'approval_policy': 'never', 'sandbox_type': 'dangerFullAccess', 'reasoning_effort': 'high'})
    await no_goal()
    # A final identity/history read rejects any intervening externally started turn.
    fresh = await _call(ws, 'thread/read', {'threadId': tid, 'includeTurns': True}, timeout, on_event)
    verify(fresh.get('thread') or {})
    result = await _call(ws, 'turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': prompt}],
        'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}, 'model': MODEL, 'effort': 'high',
        'collaborationMode': {'mode': 'default', 'settings': {'model': MODEL, 'reasoning_effort': 'high',
                                                            'developer_instructions': None}}}, timeout, on_event)
    turn_id = (result.get('turn') or {}).get('id')
    if not turn_id or turn_id in binding['history_ids'] or result.get('threadId', tid) != tid:
        raise CodexWebDispatchError('ordinary_openai_resume_turn_unknown')
    return DispatchResult(thread_id=tid, turn_id=turn_id, model=MODEL, goal_status=None,
        dispatch_mode='codex_web_ordinary', start_effective_model=MODEL,
        start_effective_provider='openai', start_collaboration_verification='verified_resume')
