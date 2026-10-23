"""Explicit same-job recovery after interruption, or adoption of its exact goal turn."""
import asyncio
from sqlalchemy import select
from app.db.models import WorkflowJob
from app.services.paper_native_models import MODELS, terminal, stage_followup, fallback_reason, next_model


async def resume_job(session,paper_id,job_id,settings,*,request_id,actor,model=None,prepared_content=None,expected_type=None):
    from app.services.rebuild_queue import lock_scheduler,paper_busy
    from app.services.codex_web_dispatch import read_thread_status
    from app.services.ai_extract_service import _background_dispatch
    lock_scheduler(session)
    job=session.scalar(select(WorkflowJob).where(WorkflowJob.job_id==job_id).with_for_update())
    if (not job or job.type not in ('paper_reading_assistance','ai_extract_rebuild') or
            expected_type is not None and job.type!=expected_type or
            (job.payload or {}).get('paper_id')!=str(paper_id) or
            job.payload.get('actor') is not None and job.payload['actor']!=actor):
        raise ValueError('same_job_recovery_not_owned')
    p,r=job.payload or {},dict(job.progress or {})
    from app.services.reading_assistance import digest
    prepared_hash=digest(prepared_content.model_dump(mode='json')) if prepared_content else None
    receipts=list(r.get('recovery_receipts') or [])
    prior=next((x for x in receipts if x['request_id']==request_id),None)
    if prior:
        if prior['model']!=model or prior.get('prepared_hash')!=prepared_hash:raise ValueError('recovery_request_reused')
        return job
    if r.get('reading_saved') and job.status=='completed':return job
    occupied=paper_busy(session,paper_id,excluding=job_id)
    if occupied:raise ValueError('same_job_recovery_other_writer')
    tid=r.get('thread_id')
    if not tid or not r.get('turn_id'):raise ValueError('same_job_recovery_identity_unknown')
    native=await read_thread_status(tid,settings)
    if (native is None or native.thread_id!=tid or native.model_provider!='workbuddy' or
            native.cwd not in ('/opt/literature-ai','/opt/AI-shujvku/literature-ai') or
            r['turn_id'] not in (native.turn_ids or (native.turn_id,))):
        raise ValueError('same_job_recovery_native_unknown')
    current=r.get('effective_model') or p['chosen_model']
    if native.model not in MODELS:raise ValueError('same_job_recovery_model_unknown')
    if native.turn_id!=r['turn_id']:
        # Coordinator resumed this exact persisted objective already. Adopt only;
        # no model mutation, goal/set, turn/start or new job is allowed here.
        if (native.goal_objective!=p['dispatch_objective'] and p['dispatch_objective'] not in (native.input_text or '') or
                native.model not in (None,current)):
            raise ValueError('same_job_recovery_foreign_followup')
        if model not in (None,current):raise ValueError('same_job_recovery_active_model_change')
        job.progress={**r,'turn_id':native.turn_id,'native_terminal_verified':False,
            'phase':'running','recovery_adopted_from':r['turn_id']}
        job.status='running';job.error=None
        session.flush()
        from app.services import reading_assistance
        if prepared_content is not None:
            if (job.type!='paper_reading_assistance' or prepared_content.job_id!=job_id or not terminal(native) or
                    'reading_task_finished' not in (native.final_report or '')):
                raise ValueError('prepared_recovery_requires_owned_terminal_save_block')
            reading_assistance.save(session,paper_id,prepared_content,settings,actor)
            session.flush()
        if job.type=='paper_reading_assistance':await reading_assistance.reconcile(session,job,settings)
    else:
        if not terminal(native):raise ValueError('same_job_recovery_not_terminal')
        if fallback_reason(native.native_error):
            target=model or next_model(current,[x['model'] for x in r.get('native_attempts',[])])
            if not target:raise ValueError('paper_model_fallback_exhausted')
        else:
            if native.turn_status!='interrupted' or native.goal_status!='paused':
                raise ValueError('same_job_recovery_not_interrupted_or_quota')
            target=model or current
        stage_followup(job,native,target,reason='explicit_resume',recovery_id=request_id)
    job.progress={**job.progress,'recovery_receipts':[ *receipts,{'request_id':request_id,'model':model,'prepared_hash':prepared_hash} ]}
    session.commit()
    if job.status=='dispatching' and not job.progress.get('dispatch_started'):
        p=job.payload
        asyncio.create_task(_background_dispatch(job_id=job_id,database_url=settings.database_url,
            prompt=p['dispatch_prompt'],objective=p['dispatch_objective'],settings=settings,
            primary_model=p['chosen_model'],attempt_token=p['attempt_token']))
    return job
