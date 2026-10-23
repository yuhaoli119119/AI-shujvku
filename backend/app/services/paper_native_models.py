"""Bounded WorkBuddy vision model policy. No network/unknown-outcome fallback.

Local native model metadata inputModalities and catalog input_modalities were
verified on 2026-10-24. Both GLMs also have isolated real image probes.
"""
import re
from uuid import uuid4

MODELS = ('cn:deepseek-v4.1-flash', 'cn:glm-5.3-flash', 'cn:glm-5.2')
DEFAULT = MODELS[0]


def require_model(model):
    if model not in MODELS:
        raise ValueError('paper_vision_model_not_allowed')
    return model


def fallback_reason(error):
    message = str(error or '').lower()
    if re.search(r'usage.?limit.?exceeded',message):
        return 'quota_exhausted'
    if re.search(r'insufficient.{0,20}(credit|balance)|额度.{0,12}(耗尽|不足|用完)|余额不足|积分不足', message):
        return 'quota_exhausted'
    if re.search(r'too many requests|per minute|requests per|rate.?limit', message):
        return None
    # A bare 429 is a transient rate limit, not proof of exhausted credit.
    if re.search(r'quota.{0,40}(exhaust|exceed)|insufficient.{0,20}(credit|balance|quota)|usage.?limit.?exceeded|额度.{0,12}(耗尽|不足|用完)|余额不足|积分不足', message):
        return 'quota_exhausted'
    if re.search(r'model[_ ]not[_ ]found|model.{0,40}(unavailable|not available|not supported)|模型.{0,12}(不可用|不存在|下线)', message):
        return 'model_unavailable'
    return None


def next_model(current, tried):
    require_model(current)
    # Monotonic traversal; no cycles even if old history is incomplete.
    return next((m for m in MODELS[MODELS.index(current)+1:] if m not in tried), None)


def terminal(status):
    return bool(status and status.turn_status in ('completed','failed','interrupted') and
        status.thread_status in ('idle','notLoaded') and status.goal_observed and
        status.goal_status in (None,'complete','completed','blocked','paused'))


def stage_followup(job, status, model, *, reason, recovery_id=None):
    """Caller owns the job row lock. Intent commits before any native request."""
    require_model(model)
    p, r = dict(job.payload or {}), dict(job.progress or {})
    current = r.get('effective_model') or p['chosen_model']
    require_model(current)
    if (not terminal(status) or not r.get('turn_id') or status.turn_id != r['turn_id'] or
            status.thread_id!=r.get('thread_id')):
        raise ValueError('same_thread_recovery_requires_owned_terminal')
    history = list(r.get('native_attempts') or [])
    tried = [x['model'] for x in history] + [current]
    if model != current and (not fallback_reason(status.native_error) or model != next_model(current,tried)):
        raise ValueError('model_switch_requires_verified_quota_or_unavailable')
    if len(history) >= len(MODELS) + 1:
        raise ValueError('same_thread_recovery_budget_exhausted')
    history.append({'thread_id':status.thread_id,'turn_id':status.turn_id,'model':current,
        'native_error':status.native_error,'turn_status':status.turn_status,'goal_status':status.goal_status,
        'reason':reason,'recovery_request_id':recovery_id,'turn_context_proof':r.get('turn_context_proof')})
    token = str(uuid4())
    instruction = p['dispatch_prompt'].replace(p['attempt_token'],token)
    p.update(initial_chosen_model=p.get('initial_chosen_model',p['chosen_model']), chosen_model=model,
        attempt_token=token, dispatch_prompt=instruction, dispatch_objective=instruction,
        continuation={'source_job_id':job.job_id,'thread_id':status.thread_id,'turn_id':status.turn_id,
            'source_model':current,'target_model':model,'switch_reason':fallback_reason(status.native_error),
            'resume_paused':status.goal_status in ('paused','blocked')})
    job.payload = p
    job.progress = {**r,'native_attempts':history,'thread_id':status.thread_id,'previous_turn_id':status.turn_id,
        'turn_id':None,'native_terminal_verified':False,'dispatch_started':False,'phase':'dispatching',
        'needs_check':False,'actual_inference_verified':False,'turn_context_proof':None,'model':model,'effective_model':None,'reading_saved':r.get('reading_saved',False)}
    job.status='dispatching';job.error=None


def stage_fallback(job,status):
    p,r=job.payload or {},job.progress or {}
    if (r.get('reading_saved') and getattr(job,'type','paper_reading_assistance')=='paper_reading_assistance') or not terminal(status) or status.turn_id!=r.get('turn_id') or status.thread_id!=r.get('thread_id'):
        return False
    reason=fallback_reason(status.native_error)
    if not reason:return False
    current=r.get('effective_model') or p.get('chosen_model')
    if current not in MODELS:return False
    target=next_model(current,[x['model'] for x in r.get('native_attempts',[])])
    if not target:
        job.error='paper_model_fallback_exhausted: '+str(status.native_error or '')[:1000]
        return False
    stage_followup(job,status,target,reason=reason)
    return True
