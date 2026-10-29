"""Same-paper admission separates writer ownership from read availability."""
from concurrent.futures import ThreadPoolExecutor
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import RebuildDataRow, WorkflowJob
from app.main import app
from app.services.rebuild_file_lifecycle import file_set
from app.services.rebuild_queue import paper_busy, writer_task
from test_dataset_write_resume import dataset_row, existing_dataset
from test_mcp_v2 import mcp_env
from test_rebuild_mutation_audit import audit_env
from test_rebuild_sections import body as section_body, chapter_env
from test_sync_write_lock_contract import PaperLock


def running_parser(env, actor='writer'):
    with env['factory']() as session:
        session.add(WorkflowJob(job_id='owned-parser', type='ai_extract_rebuild', status='running',
                                payload={'paper_id': str(env['paper']), 'actor': actor}))
        session.commit()


def test_paper_busy_only_reports_unresolved_job_and_honors_same_work(audit_env):
    env = audit_env
    with env['factory']() as session:
        session.add_all([
            WorkflowJob(job_id='terminal', type='ai_extract_rebuild', status='completed',
                        payload={'paper_id': str(env['paper']), 'actor': 'writer'}),
            WorkflowJob(job_id='unresolved', type='ai_extract_rebuild', status='running',
                        payload={'paper_id': str(env['paper']), 'actor': 'writer'}),
        ])
        session.commit()
        busy = paper_busy(session, env['paper'])
        assert busy.job_id == 'unresolved'
        assert paper_busy(session, env['paper'], excluding='unresolved') is None
        session.get(WorkflowJob, 'unresolved').status = 'failed'
        session.commit()
        assert paper_busy(session, env['paper']).job_id == 'unresolved'
        session.get(WorkflowJob, 'unresolved').progress = {'native_terminal_verified': True}
        session.commit()
        assert paper_busy(session, env['paper']) is None


@pytest.mark.parametrize('status', ['queued', 'dispatching', 'running', 'unknown', 'needs_attention'])
@pytest.mark.parametrize('job_type', ['ai_extract_rebuild', 'rebuild_external_extract', 'paper_reading_assistance'])
def test_legacy_unresolved_status_matrix_retains_occupancy(audit_env, status, job_type):
    env = audit_env
    with env['factory']() as session:
        session.add(WorkflowJob(job_id='matrix', type=job_type, status=status,
                                payload={'paper_id': str(env['paper']), 'actor': 'writer'}))
        session.commit()
        assert paper_busy(session, env['paper']).job_id == 'matrix'


@pytest.mark.parametrize('status', [
    'pending', 'claimed', 'dispatch_intent', 'running', 'unknown', 'needs_attention', 'failed',
])
def test_queue_attempt_status_matrix_retains_occupancy(audit_env, status):
    env = audit_env
    with env['factory']() as session:
        session.add(WorkflowJob(
            job_id='queue-matrix', type='rebuild_pipeline_attempt', status=status,
            payload={'paper_id': str(env['paper']), 'actor': 'writer'},
        ))
        session.commit()
        assert paper_busy(session, env['paper']).job_id == 'queue-matrix'
        if status == 'pending':
            assert writer_task(session, env['paper'], 'writer', 'queue-matrix') is None
        else:
            assert writer_task(session, env['paper'], 'writer', 'queue-matrix') is not None
        session.get(WorkflowJob, 'queue-matrix').progress = {'native_terminal_verified': True}
        session.commit()
        if status == 'failed':
            assert paper_busy(session, env['paper']) is None
        else:
            assert paper_busy(session, env['paper']).job_id == 'queue-matrix'


def test_dual_authenticated_clients_get_409_for_real_preflight_and_import_lock(
        audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    payload = dataset_row(env, fixture, row_key='dual-client-row')
    writer = env['client_for']('writer')
    other = env['client_for']('other')
    try:
        with PaperLock(env['engine'], env['paper']):
            with ThreadPoolExecutor(max_workers=2) as pool:
                preflight = pool.submit(
                    writer.post, '/api/rebuild/rows/import/preflight', json=payload)
                imported = pool.submit(
                    other.post, '/api/rebuild/rows/import', json=payload)
                responses = [preflight.result(timeout=5), imported.result(timeout=5)]
        for response in responses:
            assert response.status_code == 409, response.text
            assert response.json()['detail'] == 'paper_lock_busy_retry_same_request'
        with env['factory']() as session:
            assert session.scalar(select(RebuildDataRow).where(
                RebuildDataRow.row_key == 'dual-client-row')) is None
    finally:
        writer.close()
        other.close()


def test_same_work_can_continue_and_replay_but_different_actor_cannot(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    running_parser(env)
    payload = dataset_row(env, fixture, row_key='owned-row-one')
    payload['task_id'] = 'owned-parser'
    writer = env['client']

    first = writer.post('/api/rebuild/rows/import', json=payload)
    assert first.status_code == 200 and first.json()['summary']['outcome'] == 'all_success'
    replay = writer.post('/api/rebuild/rows/import', json=payload)
    assert replay.status_code == 200

    continuation = copy.deepcopy(payload)
    continuation['request_id'] = 'owned-parser-second-request'
    continuation['rows'][0]['row_key'] = 'owned-row-two'
    continuation['rows'][0]['material'] = 'owned-row-two'
    second = writer.post('/api/rebuild/rows/import', json=continuation)
    assert second.status_code == 200 and second.json()['summary']['outcome'] == 'all_success'

    other = env['client_for']('other')
    try:
        rejected = other.post('/api/rebuild/rows/import', json=continuation)
        assert rejected.status_code == 409
        assert rejected.json()['detail'] == 'mutation_request_id_reused'
    finally:
        other.close()
    no_owner = copy.deepcopy(continuation)
    no_owner['request_id'] = 'missing-owner-token'
    no_owner.pop('task_id', None)
    no_owner['rows'][0]['row_key'] = 'must-not-write'
    rejected_without_token = writer.post('/api/rebuild/rows/import', json=no_owner)
    assert rejected_without_token.status_code == 409
    assert 'paper_has_other_unresolved_writer_or_reviewer' in rejected_without_token.text

    with env['factory']() as session:
        keys = set(session.scalars(select(RebuildDataRow.row_key).where(
            RebuildDataRow.paper_id == env['paper'])))
        assert {'owned-row-one', 'owned-row-two'} <= keys
        assert 'must-not-write' not in keys


def test_x_task_id_header_is_equivalent_to_body_work_token(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    running_parser(env)
    payload = dataset_row(env, fixture, row_key="header-owned-row")
    payload.pop("task_id", None)
    response = env["client"].post(
        "/api/rebuild/rows/import", json=payload,
        headers={"X-Task-ID": "owned-parser"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["audit"]["task_id"] == "owned-parser"
    replay = env["client"].post(
        "/api/rebuild/rows/import", json=payload,
        headers={"X-Task-ID": "owned-parser"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["audit"]["replayed"] is True


def test_conflicting_header_and_body_work_tokens_are_rejected_before_write(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    running_parser(env)
    payload = dataset_row(env, fixture, row_key="identity-conflict-row")
    payload["task_id"] = "different-task"
    response = env["client"].post(
        "/api/rebuild/rows/import", json=payload,
        headers={"X-Task-ID": "owned-parser"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "mutation_task_id_mismatch"
    with env["factory"]() as session:
        assert session.scalar(select(RebuildDataRow).where(
            RebuildDataRow.row_key == "identity-conflict-row")) is None


def test_header_owner_survives_secondary_service_guard(chapter_env):
    env = chapter_env
    with env["factory"]() as session:
        session.add(WorkflowJob(
            job_id="chapter-owner", type="ai_extract_rebuild", status="running",
            payload={"paper_id": str(env["paper_id"]), "actor": "chapter_tester"},
        ))
        session.commit()
    payload = section_body(env)
    response = env["client"].post(
        f'/api/rebuild/papers/{env["paper_id"]}/sections/import', json=payload,
        headers={"X-Task-ID": "chapter-owner"},
    )
    assert response.status_code == 200, response.text


def test_exact_historical_replay_precedes_new_writer_but_changed_body_conflicts(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    running_parser(env, actor="writer")
    payload = dataset_row(env, fixture, row_key="historical-replay-row")
    payload["task_id"] = "owned-parser"
    first = env["client"].post("/api/rebuild/rows/import", json=payload)
    assert first.status_code == 200, first.text
    with env["factory"]() as session:
        original = session.get(WorkflowJob, "owned-parser")
        original.status = "completed"
        original.progress = {"native_terminal_verified": True}
        session.add(WorkflowJob(
            job_id="new-active-writer", type="ai_extract_rebuild", status="running",
            payload={"paper_id": str(env["paper"]), "actor": "other"},
        ))
        session.commit()

    replay = env["client"].post("/api/rebuild/rows/import", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["audit"]["replayed"] is True
    assert replay.json()["audit"]["receipt_is_historical"] is True

    changed = copy.deepcopy(payload)
    changed["rows"][0]["material"] = "changed-after-receipt"
    conflict = env["client"].post("/api/rebuild/rows/import", json=changed)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "mutation_request_id_reused"

    other = env["client_for"]("other")
    try:
        stolen = other.post("/api/rebuild/rows/import", json=payload)
        assert stolen.status_code == 409, stolen.text
    finally:
        other.close()


def test_legacy_actorless_parser_can_continue_only_with_exact_work_token(audit_env):
    env = audit_env
    fixture = existing_dataset(env)
    running_parser(env, actor=None)
    payload = dataset_row(env, fixture, row_key='legacy-owned-row')
    payload['task_id'] = 'owned-parser'
    imported = env['client'].post('/api/rebuild/rows/import', json=payload)
    assert imported.status_code == 200, imported.text
    without_token = copy.deepcopy(payload)
    without_token.pop('task_id')
    without_token['request_id'] = 'legacy-without-token'
    without_token['rows'][0]['row_key'] = 'legacy-unowned-row'
    blocked = env['client'].post('/api/rebuild/rows/import', json=without_token)
    assert blocked.status_code == 409, blocked.text


def test_mcp_begin_concurrency_has_one_owner_and_loser_rolls_back(mcp_env):
    env = mcp_env
    with env['factory']() as session:
        revision = file_set(session, env['paper'])['file_set_revision']

    call = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': 'begin_paper_work',
            'arguments': {'request': {
                'paper_id': str(env['paper']),
                'request_id': 'concurrent-begin',
                'file_ids': [str(env['file'])],
                'file_set_revision': revision,
            }},
        },
    }

    def begin(key):
        with TestClient(app) as client:
            return client.post('/mcp/', headers={
                'Authorization': f'Bearer {key}',
                'Accept': 'application/json, text/event-stream',
            }, json=call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(begin, ('writer-fixture', 'other-fixture')))

    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    results = [response.json()['result'] for response in responses]
    assert sum(not result.get('isError', False) for result in results) == 1, results
    assert sum(bool(result.get('isError', False)) for result in results) == 1, results
    with env['factory']() as session:
        jobs = list(session.scalars(select(WorkflowJob).where(
            WorkflowJob.type == 'rebuild_external_extract',
            WorkflowJob.status == 'running')))
        assert len(jobs) == 1
    backups = list((env['settings'].storage_root / 'mcp-v2-backups' /
                    str(env['paper'])).glob('*.json'))
    assert len(backups) == 1
