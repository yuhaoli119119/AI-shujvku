"""Static regression gate for the documented synchronous-lock call family."""
import ast
import hashlib
from pathlib import Path

import pytest
from app.services.sync_lock_audit import audit_sources, audit_tree, body_nodes

pytestmark=pytest.mark.no_test_database


@pytest.mark.parametrize('lock', [
    'session.scalar(select(Paper).with_for_update())',
    'session.get(Paper, key, with_for_update=True)',
    'session.refresh(paper, with_for_update=True)',
    'session.execute(text("SELECT pg_advisory_xact_lock(:key)"))',
    'session.execute(text("SELECT pg_advisory_lock(:key)"))',
])
def test_gate_recognizes_lock_syntax_and_transitive_aliases(lock):
    sources={'app/locks.py':f'def acquire(session):\n    {lock}\n',
             'app/helper.py':'from app.locks import acquire as alias\ndef outer(session):\n    alias(session)\n',
             'app/api.py':'from app.helper import outer as renamed\nasync def endpoint(session):\n    renamed(session)\n'}
    report=audit_sources(sources)
    assert any(p['async_function']=='app.api.endpoint' and len(p['chain'])==3
               for p in report['async_lock_paths'])


def test_worker_barrier_is_safe_but_new_lock_before_barrier_is_rejected():
    safe='''def locked(session):
    session.get(Job, key, with_for_update=True)
async def phase(operation):
    return await run_phase(session, operation)
async def endpoint(session):
    return await phase(lambda worker: locked(worker))
'''
    assert not audit_sources({'app/a.py':safe})['async_lock_paths']
    unsafe=safe.replace('return await run_phase(session, operation)',
                        'locked(session)\n    return await run_phase(session, operation)')
    assert audit_sources({'app/a.py':unsafe})['async_lock_paths']


def test_callback_and_helper_advisory_calls_cannot_hide_from_gate():
    source='''def lock_scheduler(session):
    session.execute(text("SELECT pg_advisory_xact_lock(1)"))
async def endpoint(session):
    def journal(event):
        lock_scheduler(session)
    await native(on_event=journal)
'''
    report=audit_sources({'app/a.py':source})
    assert any(p['chain'][-1]=='app.a.lock_scheduler' for p in report['async_lock_paths'])


def test_app_lock_gate_only_has_hash_pinned_dormant_dispatch():
    app_root=Path(__file__).resolve().parents[1]/'app'
    report=audit_tree(app_root)
    dormant='app.services.rebuild_queue_native.dispatch_once'
    assert {p['async_function'] for p in report['async_lock_paths']}=={dormant}, report['async_lock_paths']
    source=(app_root/'services/rebuild_queue_native.py').read_text()
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='dispatch_once')
    # This is a reviewed, uncalled residual, not an exemption for new callers or changes.
    assert hashlib.sha256(ast.get_source_segment(source,node).encode()).hexdigest()==(
        'd158dd6ec13f0976ab74477ce01fba06db97cd65f5ec886ac14fa7ca8925d7d1')


def test_upload_async_boundaries_do_not_touch_request_session():
    path=Path(__file__).resolve().parents[1]/'app/api/papers/ingestion.py'
    tree=ast.parse(path.read_text())
    covered={'queue_ingest_upload','queue_upload_supplementary_pdf',
             'queue_attach_pdf_to_existing_paper','_upload_snapshot','_persist_staged_upload'}
    seen=set()
    for node in tree.body:
        if isinstance(node,ast.AsyncFunctionDef) and node.name in covered:
            seen.add(node.name)
            for call in body_nodes(node):
                if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute):
                    assert ast.unparse(call.func) not in {
                        'session.get','session.refresh','session.execute','session.scalar',
                        'session.scalars','session.flush','session.commit','session.begin'}, (node.name,call.lineno)
    assert seen==covered
