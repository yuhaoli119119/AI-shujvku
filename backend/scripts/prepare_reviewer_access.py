"""One explicit review-access request; never dispatch or judge paper content.

Run through scripts/reviewer_access.py. This module uses the *running* service's
formal freeze/grant services, not a second implementation of their contracts.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from sqlalchemy import or_, select

from app.db.models import Paper, WorkflowJob
from app.schemas.rebuild import ExtractionFreezeRequest
from app.services.rebuild_acceptance import current_snapshot, freeze
from app.services.rebuild_task_authorization import (
    ALLOWED_CWDS, _key, issue_reviewer_authorization,
    verify_reviewer_authorization,
)
from app.services.rebuild_workflow_service import RebuildWorkflowError

WRITING_TYPES = {
    'ai_extract_rebuild', 'paper_reading_assistance',
    'rebuild_external_extract', 'rebuild_pipeline_attempt',
}
TERMINAL = {'completed', 'failed', 'cancelled', 'canceled', 'interrupted', 'accepted', 'partial'}


async def read_native_for_review(thread_id, *, settings, include_goal=False):
    """Bounded native read only; retain initial scope omitted by status helper."""
    import websockets
    from app.services.codex_web_dispatch import _call, ThreadStatus
    async with websockets.connect(settings.codex_web_ipc_uri, open_timeout=10,
                                  max_size=32 * 1024 * 1024) as ws:
        result = await _call(ws, 'thread/read', {'threadId': thread_id, 'includeTurns': True}, 15)
    thread = result.get('thread') or {}
    turns = thread.get('turns') or []
    last = turns[-1] if turns else {}
    if thread.get('id') != thread_id:
        raise RebuildWorkflowError('review_native_identity_unverified')
    return ThreadStatus(
        last.get('status'), None, None, None,
        thread_id=thread_id, turn_id=last.get('id'), cwd=thread.get('cwd'),
        thread_status=(thread.get('status') or {}).get('type'),
        input_text=thread.get('preview') or '',
    )


def snapshot(session, request):
    paper_id = UUID(request['paper_id'])
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise RebuildWorkflowError('paper_not_found')
    jobs = session.scalars(select(WorkflowJob).where(or_(
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id),
        WorkflowJob.payload.op('->')('claims').op('->>')('paper_id') == str(paper_id),
    ))).all()
    occupied = [j.job_id for j in jobs if j.type in WRITING_TYPES and j.status not in TERMINAL]
    if occupied:
        raise RebuildWorkflowError('review_prepare_writer_not_terminal:' + ','.join(occupied))
    other_reviews = [j.job_id for j in jobs if j.type == 'rebuild_task_authorization'
                     and j.status == 'active'
                     and (j.payload.get('claims') or {}).get('reviewer_thread_id')
                     != request['reviewer_thread_id']]
    if other_reviews:
        # Even an expired token does not prove its native writer has stopped.
        raise RebuildWorkflowError('review_prepare_other_reviewer_occupied:' + ','.join(other_reviews))
    state, _ = current_snapshot(session, paper_id, force_physical=True)
    return {'paper_id': str(paper_id), 'snapshot': state,
            'existing_control_jobs': [
                {'job_id': j.job_id, 'type': j.type, 'status': j.status,
                 'payload': j.payload, 'progress': j.progress, 'result': j.result}
                for j in jobs if j.type in {'rebuild_acceptance', 'rebuild_task_authorization'}],
            'source_jobs': [{'job_id': j.job_id, 'type': j.type, 'status': j.status}
                            for j in jobs if j.type in WRITING_TYPES]}


def coverage_for(session, request):
    from app.services.rebuild_acceptance import _scope
    files, _ = _scope(session, UUID(request['paper_id']))
    by_id = {str(f.id): f for f in files}
    supplied = request.get('coverage')
    if supplied is not None:
        # Formal freeze validates source/physical hashes, page bounds and scope.
        return supplied
    if not by_id:
        raise RebuildWorkflowError('review_prepare_no_registered_files')
    # A scope registration explicitly claims ZERO checked pages. The reviewer
    # can read/correct now, then submit real coverage under a new request ID.
    from app.services.rebuild_file_preview import resolve_reading_pdf, _sha256
    return [{'file_id': str(f.id), 'source_sha256': f.sha256,
             'reading_pdf_sha256': _sha256(resolve_reading_pdf(f, request['_settings'])),
             'covered_pages': [],
             'remaining_reason': 'Scope registered for independent review; page coverage has not yet been verified.'}
            for f in files]


async def prepare(session, request, settings, *, native_read=None):
    from app.services.paper_db_phase import run_phase
    read = native_read or read_native_for_review
    coordinator = request['coordinator_thread_id']
    reviewer = request['reviewer_thread_id']
    if coordinator == reviewer:
        raise RebuildWorkflowError('independent_reviewer_required')
    # These are actual current task handles, not invented historical writers.
    # Coordinator is the CURRENT snapshot registrar, never a claim of authorship.
    states = {}
    for tid in (coordinator, reviewer):
        observed = await asyncio.wait_for(read(tid, settings=settings, include_goal=False), 20)
        if (observed is None or observed.thread_id != tid or observed.cwd not in ALLOWED_CWDS
                or not observed.turn_id):
            raise RebuildWorkflowError('review_native_identity_unverified')
        states[tid] = observed
    if request['paper_id'] not in (states[reviewer].input_text or ''):
        raise RebuildWorkflowError('review_native_paper_scope_unverified')

    def register(worker):
        from app.services.rebuild_acceptance import _locked_paper
        _locked_paper(worker, UUID(request['paper_id']))
        before = snapshot(worker, request)
        if before['snapshot']['revision'] != request['expected_revision']:
            raise RebuildWorkflowError('review_prepare_revision_changed')
        body = ExtractionFreezeRequest(
            request_id=request['request_id'], task_id=coordinator,
            coverage=coverage_for(worker, {**request, '_settings': settings}))
        frozen = freeze(worker, UUID(request['paper_id']), body, settings, actor=request['owner'])
        if frozen['revision'] != request['expected_revision']:
            raise RebuildWorkflowError('review_prepare_revision_changed')
        return frozen

    frozen = await run_phase(session, register)
    async def checked(tid, **kwargs):
        # Re-read for signing; do not treat the initial preflight as a live grant.
        return await asyncio.wait_for(read(tid, **kwargs), 20)
    issued = await issue_reviewer_authorization(
        session, frozen['job_id'], request_id=request['request_id'], owner=request['owner'],
        reviewer_task_id=reviewer, reviewer_thread_id=reviewer,
        writer_thread_id=coordinator, evidence_root=request['evidence_root'],
        issuer_key=_key(settings), settings=settings, native_read=checked,
        ttl_seconds=request.get('ttl_seconds', 86400))
    # Non-mutating validation against the actual runtime verifier; never submit
    # a scientific verdict on behalf of the reviewer as an authorization test.
    def verify(worker):
        job = worker.get(WorkflowJob, frozen['job_id'])
        claims = verify_reviewer_authorization(
            worker, authorization=issued['authorization'], frozen=job,
            actor=request['owner'], reviewer_task_id=reviewer, settings=settings)
        return {'grant_id': claims['grant_id'], 'expires_at': claims['expires_at']}
    verified = await run_phase(session, verify)
    return {'authorization': issued['authorization'], 'freeze': frozen,
            'verified': verified, 'replayed': issued['replayed'],
            'snapshot_registrar_thread_id': coordinator,
            'registrar_is_not_historical_author': True,
            'scientific_review_submitted': False}


def main():
    import sys
    from app.config import get_settings
    from app.db.session import get_engine
    from sqlalchemy.orm import Session
    operation, raw = sys.argv[1:]
    request = json.loads(raw)
    settings = get_settings()
    with Session(get_engine(settings.database_url), expire_on_commit=False) as session:
        if operation == 'snapshot':
            result = snapshot(session, request)
        elif operation == 'prepare':
            result = asyncio.run(prepare(session, request, settings))
        else:
            raise ValueError('unsupported_operation')
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
