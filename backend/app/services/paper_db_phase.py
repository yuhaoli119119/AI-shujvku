"""Short synchronous paper transactions, isolated from the HTTP event loop.

Only plain snapshots cross native awaits. Each worker phase owns its Session;
no request Session or connection is shared with a worker thread.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
from functools import partial
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

LOCK_TIMEOUT = '1000ms'
BUSY = 'paper_lock_busy_retry_same_request'
UNOBSERVED = object()


class LockConflictError(ValueError):
    """A bounded row/advisory-lock wait expired; the transaction rolled back."""


class NativeReadRequired(Exception):
    def __init__(self, binding):
        self.binding = copy.deepcopy(binding)


def bound_lock_wait(session):
    """Transaction-local guard; never changes PostgreSQL/global configuration."""
    session.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def is_lock_conflict(exc):
    return getattr(getattr(exc, 'orig', None), 'sqlstate', None) in ('55P03', '40P01')


async def run_phase(request_session, operation, *, after_commit=None):
    bind = request_session.get_bind()
    info = copy.copy(request_session.info)

    def run():
        with Session(bind=bind, autoflush=False, expire_on_commit=False, info=info) as session:
            # Every transaction in this one phase, including one opened after an
            # existing explicit commit, has the same bounded lock policy.
            @event.listens_for(session, 'after_begin')
            def limit_wait(session, transaction, connection):
                connection.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            try:
                result = operation(session)
                session.commit()
                return result
            except Exception as exc:
                session.rollback()
                if isinstance(exc, OperationalError) and is_lock_conflict(exc):
                    raise LockConflictError(BUSY) from exc
                raise

    async def execute():
        result = await asyncio.to_thread(run)
        # A durable commit may need to arm one exactly-once dispatch before the
        # HTTP task observes cancellation. This callback runs inside the shielded
        # task, so a disconnect cannot strand a committed dispatching intent.
        if after_commit is not None:
            callback_result = after_commit(result)
            if inspect.isawaitable(callback_result):
                await callback_result
        return result

    task = asyncio.create_task(execute())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The worker Session is thread-owned and the phase is lock-bounded. Wait
        # for it to commit/rollback and close, then preserve the caller's cancel.
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


async def observe_and_apply(request_session, operation, *, after_commit=None):
    from app.services.codex_web_dispatch import read_thread_status
    try:
        return await run_phase(request_session, partial(operation, observation=UNOBSERVED),
                               after_commit=after_commit)
    except NativeReadRequired as need:
        # The exception unwinds/rolls back the first phase before any IPC await.
        try:
            native = await asyncio.wait_for(read_thread_status(need.binding['thread_id'],
                                                              need.binding['settings']), timeout=10)
        except asyncio.TimeoutError as exc:
            raise ValueError('native_read_timeout') from exc
        return await run_phase(request_session, partial(operation,
            observation=(need.binding['pin'], native)), after_commit=after_commit)


def require_source(session, paper_id, settings, source_job_id, actor, observation):
    from app.services.reading_thread_continuation import source_for
    binding = source_for(session, paper_id, source_job_id, actor)
    if observation is UNOBSERVED:
        if binding:
            raise NativeReadRequired({'thread_id': binding['thread_id'], 'settings': settings, 'pin': binding})
        return None
    pin, native = observation
    if binding != pin:
        raise ValueError('same_thread_source_changed_refresh')
    if (native is None or native.thread_id != binding['thread_id']
            or native.turn_id != binding['turn_id']
            or native.turn_status not in ('completed', 'failed', 'interrupted')
            or native.thread_status not in ('idle', 'notLoaded') or not native.goal_observed
            or native.goal_status not in (None, 'complete', 'completed')
            or native.model_provider != 'workbuddy'):
        raise ValueError('same_thread_native_busy_or_unverified')
    return binding


def job_pin(job):
    return {'status': job.status, 'payload': copy.deepcopy(job.payload),
            'progress': copy.deepcopy(job.progress), 'result': copy.deepcopy(job.result),
            'error': job.error}


async def observe_job(request_session, job, settings, apply, *, include_goal=True):
    """Read status without a transaction, then CAS under a bounded row lock."""
    from app.db.models import WorkflowJob
    from app.services.codex_web_dispatch import read_thread_status
    if request_session.new or request_session.dirty or request_session.deleted:
        raise ValueError('native_readback_requires_clean_transaction')
    jid = job.job_id
    def snapshot(session):
        current = session.get(WorkflowJob, jid)
        return job_pin(current) if current else None

    pin = await run_phase(request_session, snapshot)
    if pin is None:
        raise ValueError('reading_job_not_owned')
    tid = (pin['progress'] or {}).get('thread_id')
    native = None
    ordinary = (pin['payload'] or {}).get('parse_contract') == 'ordinary_parse_v1'
    stopped = bool((pin['progress'] or {}).get('needs_attention')) and not ordinary
    if tid and pin['status'] in ('queued', 'dispatching', 'running') and not stopped:
        native = await asyncio.wait_for(read_thread_status(tid, settings=settings,
                                                           include_goal=include_goal), timeout=10)

    def update(session):
        current = session.get(WorkflowJob, jid, with_for_update=True)
        if current is None:
            raise ValueError('reading_job_not_owned')
        if job_pin(current) != pin:
            return current  # A real writer advanced state: preserve it, never overwrite.
        return apply(session, current, settings, native)

    result = await run_phase(request_session, update)
    # Result is clean/detached, loaded in its owning worker Session. No SQL and
    # no connection/thread sharing; retain callers' identity-map references.
    return request_session.merge(result, load=False)
