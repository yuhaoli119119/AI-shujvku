"""Explicit same-job recovery after interruption, or adoption of its exact goal turn."""
from __future__ import annotations

from functools import partial

from sqlalchemy import select

from app.db.models import Paper, WorkflowJob
from app.services.paper_native_models import MODELS, fallback_reason, next_model, stage_followup, terminal


def _owned_job(session, paper_id, job_id, actor, expected_type):
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise ValueError('paper_not_found')
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
    if (not job or job.type not in ('paper_reading_assistance', 'ai_extract_rebuild') or
            expected_type is not None and job.type != expected_type or
            (job.payload or {}).get('paper_id') != str(paper_id) or
            job.payload.get('actor') is not None and job.payload['actor'] != actor):
        raise ValueError('same_job_recovery_not_owned')
    return job


def _known_receipt(job, request_id, model, prepared_hash):
    receipts = list((job.progress or {}).get('recovery_receipts') or [])
    prior = next((item for item in receipts if item['request_id'] == request_id), None)
    if prior and (prior['model'] != model or prior.get('prepared_hash') != prepared_hash):
        raise ValueError('recovery_request_reused')
    return prior


def _verify_native(job, native):
    progress = job.progress or {}
    thread_id = progress.get('thread_id')
    turn_id = progress.get('turn_id')
    if not thread_id or not turn_id:
        raise ValueError('same_job_recovery_identity_unknown')
    if (native is None or native.thread_id != thread_id or native.model_provider != 'workbuddy' or
            native.cwd not in ('/opt/literature-ai', '/opt/AI-shujvku/literature-ai') or
            turn_id not in (native.turn_ids or (native.turn_id,))):
        raise ValueError('same_job_recovery_native_unknown')
    current_model = progress.get('effective_model') or (job.payload or {}).get('chosen_model')
    if native.model not in MODELS or native.model not in (None, current_model):
        raise ValueError('same_job_recovery_model_unknown')
    return thread_id, turn_id, current_model


def _resume_phase(
    session,
    paper_id,
    job_id,
    settings,
    *,
    request_id,
    actor,
    model=None,
    prepared_content=None,
    expected_type=None,
    observation,
    dispatches,
):
    from app.services.paper_db_phase import NativeReadRequired, UNOBSERVED, job_pin
    from app.services.reading_assistance import digest
    from app.services.rebuild_queue import paper_busy

    job = _owned_job(session, paper_id, job_id, actor, expected_type)
    prepared_hash = digest(prepared_content.model_dump(mode='json')) if prepared_content else None
    if _known_receipt(job, request_id, model, prepared_hash):
        return {'job': job, 'reconcile': False, 'dispatches': dispatches}
    progress = dict(job.progress or {})
    if progress.get('reading_saved') and job.status == 'completed':
        return {'job': job, 'reconcile': False, 'dispatches': dispatches}
    if paper_busy(session, paper_id, excluding=job_id):
        raise ValueError('same_job_recovery_other_writer')

    thread_id = progress.get('thread_id')
    if not thread_id or not progress.get('turn_id'):
        raise ValueError('same_job_recovery_identity_unknown')
    if observation is UNOBSERVED:
        # This exception unwinds the worker-owned transaction before the native
        # read starts. No paper/job row lock or connection crosses the IPC wait.
        raise NativeReadRequired({
            'thread_id': thread_id,
            'settings': settings,
            'pin': {'job': job_pin(job)},
        })

    pin, native = observation
    if job_pin(job) != pin['job']:
        # A concurrent replay may have completed while this snapshot was being
        # read natively. Return the durable receipt instead of advancing stale state.
        if _known_receipt(job, request_id, model, prepared_hash):
            return {'job': job, 'reconcile': False, 'dispatches': dispatches}
        raise ValueError('same_job_changed_during_readback')
    from app.services.ordinary_openai_recovery import applies, stage_resume
    if applies(job):
        if prepared_content is not None:
            raise ValueError('ordinary_openai_prepared_reading_not_supported')
        stage_resume(job, native, request_id, model)
        job.progress = {**job.progress, 'recovery_receipts': [
            *list((job.progress or {}).get('recovery_receipts') or []),
            {'request_id': request_id, 'model': model, 'prepared_hash': prepared_hash}]}
        payload = job.payload
        dispatches.append(dict(job_id=job_id, database_url=settings.database_url,
            prompt=payload['dispatch_prompt'], objective=payload['dispatch_objective'], settings=settings,
            primary_model=payload['chosen_model'], attempt_token=payload['attempt_token']))
        return {'job': job, 'reconcile': False, 'dispatches': dispatches}

    thread_id, turn_id, current_model = _verify_native(job, native)

    reconcile = False
    if native.turn_id != turn_id:
        # The coordinator already resumed this exact objective. Adopt the owned
        # turn only; never issue another resume/start from this recovery path.
        if ((native.goal_objective != (job.payload or {}).get('dispatch_objective') and
             (job.payload or {}).get('dispatch_objective') not in (native.input_text or '')) or
                native.model not in (None, current_model)):
            raise ValueError('same_job_recovery_foreign_followup')
        if model not in (None, current_model):
            raise ValueError('same_job_recovery_active_model_change')
        job.progress = {**progress, 'turn_id': native.turn_id, 'native_terminal_verified': False,
                        'phase': 'running', 'recovery_adopted_from': turn_id}
        job.status = 'running'
        job.error = None
        if prepared_content is not None:
            if (job.type != 'paper_reading_assistance' or prepared_content.job_id != job_id or
                    not terminal(native) or 'reading_task_finished' not in (native.final_report or '')):
                raise ValueError('prepared_recovery_requires_owned_terminal_save_block')
            from app.services import reading_assistance
            reading_assistance.save(session, paper_id, prepared_content, settings, actor)
            reconcile = job.type == 'paper_reading_assistance'
    else:
        if not terminal(native):
            raise ValueError('same_job_recovery_not_terminal')
        if fallback_reason(native.native_error):
            target = model or next_model(current_model, [item['model'] for item in progress.get('native_attempts', [])])
            if not target:
                raise ValueError('paper_model_fallback_exhausted')
        else:
            if native.turn_status != 'interrupted' or native.goal_status != 'paused':
                raise ValueError('same_job_recovery_not_interrupted_or_quota')
            target = model or current_model
        stage_followup(job, native, target, reason='explicit_resume', recovery_id=request_id)

    job.progress = {
        **(job.progress or {}),
        'recovery_receipts': [
            *list((job.progress or {}).get('recovery_receipts') or []),
            {'request_id': request_id, 'model': model, 'prepared_hash': prepared_hash},
        ],
    }
    if job.status == 'dispatching' and not job.progress.get('dispatch_started'):
        payload = job.payload or {}
        dispatches.append(dict(
            job_id=job_id,
            database_url=settings.database_url,
            prompt=payload['dispatch_prompt'],
            objective=payload['dispatch_objective'],
            settings=settings,
            primary_model=payload['chosen_model'],
            attempt_token=payload['attempt_token'],
        ))
    return {'job': job, 'reconcile': reconcile, 'dispatches': dispatches}


async def resume_job(session, paper_id, job_id, settings, *, request_id, actor, model=None,
                     prepared_content=None, expected_type=None):
    from app.services.ai_extract_service import _arm_dispatches
    from app.services.paper_db_phase import observe_and_apply

    dispatches = []
    result = await observe_and_apply(
        session,
        partial(
            _resume_phase,
            paper_id=paper_id,
            job_id=job_id,
            settings=settings,
            request_id=request_id,
            actor=actor,
            model=model,
            prepared_content=prepared_content,
            expected_type=expected_type,
            dispatches=dispatches,
        ),
        after_commit=lambda _: _arm_dispatches(dispatches),
    )
    job = result['job']
    if result['reconcile']:
        from app.services import reading_assistance
        job = await reading_assistance.reconcile(session, job, settings)
    return job
