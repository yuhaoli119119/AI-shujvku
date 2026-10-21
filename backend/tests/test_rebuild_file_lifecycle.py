"""Isolated PostgreSQL HTTP contract: immutable file replacement, never extraction."""
from __future__ import annotations

import copy
import hashlib
import json
from uuid import UUID, uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select

from app.db.models import (AuditLog, Paper, RebuildPaperFile, RebuildDataRow,
                           RebuildDataValue, RebuildValueSource, WorkflowJob, RebuildVisualAsset)
from app.main import app
from app.services.rebuild_file_lifecycle import ACTION
from app.services.rebuild_mutation_audit import safe_json
from test_rebuild_mutation_audit import audit_env


def pdf(label):
    with fitz.open() as doc:
        doc.new_page().insert_text((30, 40), label)
        return doc.tobytes()


def url(env):
    return f'/api/rebuild/papers/{env["paper"]}/files'


def read(env):
    response = env['client'].get(url(env))
    assert response.status_code == 200, response.text
    return response.json()


def body(env, *, role='main', targets=()):
    return {'request_id': str(uuid4()), 'expected_revision': read(env)['file_set_revision'],
            'role': role, 'action': 'supersede' if targets else 'add',
            'supersede_file_ids': json.dumps([str(value) for value in targets]),
            'reason': 'Synthetic file source replacement only'}


def upload(env, data, raw, *, filename='synthetic.pdf', client=None):
    return (client or env['client']).post(url(env), data=data,
        files={'file': (filename, raw, 'application/pdf')})


def snapshots(env):
    with env['factory']() as session:
        return {model.__tablename__: {str(row.id): safe_json({column.key: getattr(row, column.key)
            for column in inspect(model).column_attrs}) for row in session.scalars(select(model))}
            for model in (RebuildPaperFile, RebuildDataRow, RebuildDataValue, RebuildValueSource, WorkflowJob, RebuildVisualAsset)}


def bytes_snapshot(env):
    root = env['settings'].storage_root
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def events(env):
    with env['factory']() as session:
        return [copy.deepcopy(record.payload) for record in session.scalars(select(AuditLog).where(AuditLog.action == ACTION))]


def add_main(env):
    raw = pdf('Original synthetic main')
    response = upload(env, body(env), raw)
    assert response.status_code == 200, response.text
    return response.json()['file']['id'], raw


def test_replace_keeps_old_identity_bytes_and_science_and_does_not_parse(audit_env):
    env = audit_env
    old_id, old_raw = add_main(env)
    with env['factory']() as session:
        row = RebuildDataRow(paper_id=env['paper'], row_key='immutable-row', reaction='SRR',
                            material='synthetic', active_site_type='dual_atom', data_type='dft')
        session.add(row); session.flush()
        value = RebuildDataValue(paper_id=env['paper'], row_id=row.id, field_name='adsorption_energy_ev',
                                 raw_value='1.000', numeric_value=1.0, unit='eV')
        session.add(value); session.flush()
        source = RebuildValueSource(paper_id=env['paper'], row_id=row.id, value_id=value.id,
            file_id=UUID(old_id), source_key='exact-old-main', source_kind='text', page_number=1, quote='original evidence')
        session.add(source); session.flush(); value.preferred_source_id=source.id
        asset = RebuildVisualAsset(paper_id=env['paper'], file_id=UUID(old_id),
            asset_key='retained-old-figure', asset_type='figure', page_numbers=[1],
            figure_label='Figure 1', caption='Original synthetic figure')
        session.add(asset); session.flush()
        digest = session.get(RebuildPaperFile, UUID(old_id)).sha256
        row_id, value_id, source_id, asset_id = map(str, (row.id, value.id, source.id, asset.id))
        session.commit()
    chapter = {'request_id': str(uuid4()), 'sections': [{'file_id': old_id,
        'source_sha256': digest, 'section_key': 'retained-old-chapter', 'page_basis': 'source_pdf',
        'section_title': 'Original heading', 'text': 'Original synthetic main', 'explanation_zh': '仅供隔离测试的原章节说明',
        'page_start': 1, 'page_end': 1, 'sources': [{'page_number': 1,
            'quote': 'Original synthetic main', 'locator': 'synthetic first paragraph'}]}]}
    chapter_response = env['client'].post(f'/api/rebuild/papers/{env["paper"]}/sections/import', json=chapter)
    assert chapter_response.status_code == 200, chapter_response.text
    with env['factory']() as session:
        original_chapter_event = copy.deepcopy(session.scalar(select(AuditLog).where(AuditLog.action == 'section_imported')).payload)
    before = snapshots(env); physical = bytes_snapshot(env); first_audit = events(env)
    before_state = read(env)
    new_raw = pdf('Replacement synthetic main')
    request = body(env, targets=[old_id])
    response = upload(env, request, new_raw, filename='new-original-name.pdf')
    assert response.status_code == 200, response.text
    receipt = response.json(); new_id = receipt['file']['id']
    assert new_id != old_id and receipt['created']
    assert receipt['automatic_parsing_started'] is False and receipt['scientific_bindings_remapped'] is False
    state = read(env); by_id = {f['id']: f for f in state['items']}
    assert state['preferred_main_id'] == new_id and new_id in state['active_file_ids']
    assert by_id[old_id]['lifecycle_status'] == 'superseded' and not by_id[old_id]['preferred']
    assert by_id[old_id]['scientific_review_required']
    assert state['scientific_review_required_file_ids'] == [old_id]
    after = snapshots(env)
    assert after['rebuild_paper_files'][old_id] == before['rebuild_paper_files'][old_id]
    for model in (RebuildDataRow, RebuildDataValue, RebuildValueSource, WorkflowJob, RebuildVisualAsset):
        assert after[model.__tablename__] == before[model.__tablename__]
    assert {key: bytes_snapshot(env)[key] for key in physical} == physical
    assert events(env)[0] == first_audit[0]
    audit = events(env)[-1]
    assert audit['actor'] == 'writer' and audit['request_id'] == request['request_id']
    assert audit['before'] == before_state['items']
    persisted = next(f for f in audit['after'] if f['id'] == new_id)
    assert persisted['original_filename'] == 'new-original-name.pdf'
    assert persisted['sha256'] == hashlib.sha256(new_raw).hexdigest()
    assert persisted['file_size'] == len(new_raw) and persisted['storage_path']
    historical = env['client'].get(f'/api/rebuild/files/{old_id}/download')
    assert historical.status_code == 200 and historical.content == old_raw
    preferred = env['client'].get(f'/api/rebuild/papers/{env["paper"]}/source-pdf/preview')
    assert preferred.status_code == 200 and preferred.content == new_raw
    generic = env['client'].get(f'/api/papers/{env["paper"]}/pdf')
    assert generic.status_code == 200 and generic.content == new_raw
    with env['factory']() as session:
        from app.services.rebuild_workflow_service import resolve_paper_source_pdf
        paper = session.get(Paper, env['paper'])
        assert resolve_paper_source_pdf(paper, env['settings']).read_bytes() == new_raw
        assert paper.pdf_path == 'fixture.pdf'  # Original legacy pointer is preserved.
    package_response = env['client'].get(f'/api/rebuild/papers/{env["paper"]}/ai-work-package?view=full')
    assert package_response.status_code == 200, package_response.text
    package = package_response.json()
    assert isinstance(package['rows'], dict) and isinstance(package['rows']['items'], list)
    assert isinstance(package['assets'], list) and isinstance(package['sections'], list)
    assert package['file_lifecycle']['preferred_main_id'] == new_id
    assert old_id not in {file['id'] for file in package['files']}
    assert old_id in {file['id'] for file in package['file_history']}
    row = next(item for item in package['rows']['items'] if item['id'] == row_id)
    value = next(item for item in row['values'] if item['id'] == value_id)
    source = next(item for item in value['sources'] if item['id'] == source_id)
    assert row['scientific_review_required'] and value['scientific_review_required']
    assert source['file_id'] == old_id and source['page_number'] == 1 and source['quote'] == 'original evidence'
    assert source['scientific_review_required'] and value['raw_value'] == '1.000'
    asset = next(item for item in package['assets'] if item['id'] == asset_id)
    assert asset['file_id'] == old_id and asset['scientific_review_required']
    assert asset['page_numbers'] == [1] and asset['caption'] == 'Original synthetic figure'
    assert len(package['sections']) == 1
    section = package['sections'][0]
    assert section['text'] == 'Original synthetic main' and section['scientific_review_required']
    assert section['provenance']['file_id'] == old_id
    with env['factory']() as session:
        stored_chapter_event = session.scalar(select(AuditLog).where(AuditLog.action == 'section_imported')).payload
        assert stored_chapter_event == original_chapter_event
        assert stored_chapter_event['section']['file_id'] == old_id
        assert 'scientific_review_required' not in stored_chapter_event['section']


def test_replay_stale_id_body_hash_filename_and_actor_conflicts(audit_env):
    env = audit_env; raw = pdf('Replay')
    request = body(env); first = upload(env, request, raw)
    assert first.status_code == 200, first.text
    baseline = snapshots(env); bytes_before = bytes_snapshot(env); audit_before = events(env)
    replay = upload(env, request, raw)
    assert replay.status_code == 200 and replay.json()['audit']['replayed']
    expected = copy.deepcopy(first.json()); expected['audit']['replayed'] = True
    assert replay.json() == expected
    stale = {**request, 'request_id':str(uuid4())}
    assert upload(env, stale, pdf('Stale')).status_code == 409
    changed = {**request, 'reason':'Different request body'}
    assert upload(env, changed, raw).status_code == 409
    assert upload(env, request, pdf('Changed bytes')).status_code == 409
    assert upload(env, request, raw, filename='changed.pdf').status_code == 409
    other = env['client_for']('other')
    try:
        assert upload(env, request, raw, client=other).status_code == 409
    finally:
        other.close()
    assert snapshots(env) == baseline and bytes_snapshot(env) == bytes_before and events(env) == audit_before


def test_duplicate_same_role_sha_and_self_supersede_are_safe(audit_env):
    env = audit_env; fid, raw = add_main(env)
    baseline=snapshots(env); physical=bytes_snapshot(env)
    duplicate=upload(env,body(env),raw)
    assert duplicate.status_code == 200 and duplicate.json()['created'] is False
    assert duplicate.json()['file']['id'] == fid
    assert snapshots(env) == baseline and bytes_snapshot(env) == physical
    invalid=upload(env,body(env,targets=[fid]),raw)
    assert invalid.status_code == 409 and invalid.json()['detail']=='file_cannot_supersede_itself'
    assert snapshots(env) == baseline and bytes_snapshot(env) == physical


def test_target_ownership_role_validation_before_storage(audit_env):
    env=audit_env; fid,raw=add_main(env)
    with env['factory']() as s:
        foreign=Paper(title='Foreign',library_name='默认文献库',paper_code='F999',pdf_path='foreign.pdf')
        s.add(foreign);s.flush()
        foreign_file=RebuildPaperFile(paper_id=foreign.id,role='main',storage_path='foreign.pdf',
            original_filename='foreign.pdf',sha256='a'*64,file_size=1)
        s.add(foreign_file);s.commit();foreign_id=foreign_file.id
    baseline=snapshots(env);physical=bytes_snapshot(env)
    foreign=upload(env,body(env,targets=[foreign_id]),pdf('Foreign rejection'))
    assert foreign.status_code == 422 and foreign.json()['detail']=='supersede_file_not_in_paper'
    mismatch=upload(env,body(env,role='main',targets=[env['file']]),pdf('Role rejection'))
    assert mismatch.status_code == 422 and mismatch.json()['detail']=='supersede_file_role_mismatch'
    assert snapshots(env) == baseline and bytes_snapshot(env) == physical
    # Existing candidate cannot belong to another paper, even with a fresh local revision.
    selected={'request_id':str(uuid4()),'expected_revision':read(env)['file_set_revision'],
              'role':'main','action':'supersede','reason':'cross paper rejected',
              'supersede_file_ids':[fid],'file_id':str(foreign_id)}
    response=env['client'].post(url(env)+'/lifecycle',json=selected)
    assert response.status_code == 422 and response.json()['detail']=='selected_file_not_in_paper_role'
    assert snapshots(env) == baseline


def test_auth_csrf_origin_and_missing_cas_cannot_write(audit_env):
    env=audit_env;request=body(env);raw=pdf('Unauthorised')
    baseline=snapshots(env);physical=bytes_snapshot(env)
    with TestClient(app) as anon:
        assert upload(env,request,raw,client=anon).status_code==401
        assert anon.get(url(env)).status_code==401
    headers=dict(env['client'].headers)
    env['client'].headers.pop('X-CSRF-Token')
    assert upload(env,request,raw).status_code==403
    env['client'].headers.update(headers)
    env['client'].headers['Origin']='https://attacker.invalid'
    assert upload(env,request,raw).status_code==403
    env['client'].headers.update(headers)
    for key in ('request_id','expected_revision','action','reason'):
        missing={k:v for k,v in request.items() if k!=key}
        assert upload(env,missing,raw).status_code==422
    assert snapshots(env)==baseline and bytes_snapshot(env)==physical


def test_audit_failure_rolls_back_files_and_science(audit_env):
    env=audit_env;fid,raw=add_main(env);baseline=snapshots(env);audit_before=events(env)
    def fail_audit(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO audit_logs'):
            raise RuntimeError('isolated audit failure')
    event.listen(env['engine'],'before_cursor_execute',fail_audit)
    try:
        response=upload(env,body(env,targets=[fid]),pdf('Audit rollback'))
        assert response.status_code==500
    finally:
        event.remove(env['engine'],'before_cursor_execute',fail_audit)
    assert snapshots(env)==baseline and events(env)==audit_before
    assert read(env)['preferred_main_id']==fid


def test_ambiguous_legacy_main_fails_closed_and_explicit_existing_choice(audit_env):
    env=audit_env;first,raw=add_main(env)
    with env['factory']() as s:
        raw2=pdf('Other legacy main');path=env['settings'].storage_root/'legacy-second.pdf';path.write_bytes(raw2)
        second=RebuildPaperFile(paper_id=env['paper'],role='main',storage_path='legacy-second.pdf',
            original_filename='legacy-second.pdf',sha256=hashlib.sha256(raw2).hexdigest(),file_size=len(raw2))
        s.add(second);s.commit();second_id=str(second.id)
    state=read(env)
    assert state['ambiguous_main'] and state['preferred_main_id'] is None
    assert first not in state['active_file_ids'] and second_id not in state['active_file_ids']
    response=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/source-pdf/preview')
    assert response.status_code==409 and response.json()['detail']=='preferred_main_source_ambiguous'
    baseline=snapshots(env)
    body_json={'request_id':str(uuid4()),'expected_revision':state['file_set_revision'],'role':'main',
        'action':'supersede','reason':'Select explicit original candidate','supersede_file_ids':[first],'file_id':second_id}
    selected=env['client'].post(url(env)+'/lifecycle',json=body_json)
    assert selected.status_code==200,selected.text
    assert read(env)['preferred_main_id']==second_id and not read(env)['ambiguous_main']
    assert snapshots(env)==baseline
    assert env['client'].post(url(env)+'/lifecycle',json=body_json).json()['audit']['replayed']
    stale={**body_json,'request_id':str(uuid4())}
    assert env['client'].post(url(env)+'/lifecycle',json=stale).status_code==409
    assert env['client'].get(f'/api/rebuild/files/{first}/download').content==raw


def test_si_replacement_keeps_other_active_si_and_cannot_reactivate_history(audit_env):
    env=audit_env;old=str(env['file'])
    raw=pdf('Replacement supplementary')
    response=upload(env,body(env,role='si',targets=[old]),raw)
    assert response.status_code==200,response.text
    replacement=response.json()['file']['id']
    second=upload(env,body(env,role='si'),pdf('Independent supplementary'))
    assert second.status_code==200,second.text
    other=second.json()['file']['id'];state=read(env)
    assert set(state['active_file_ids'])=={replacement,other}
    assert old not in state['active_file_ids']
    baseline=snapshots(env);physical=bytes_snapshot(env)
    history={'request_id':str(uuid4()),'expected_revision':state['file_set_revision'],
        'file_id':old,'role':'si','action':'supersede','reason':'Forbidden silent revival',
        'supersede_file_ids':[replacement]}
    denied=env['client'].post(url(env)+'/lifecycle',json=history)
    assert denied.status_code==409 and denied.json()['detail']=='superseded_file_cannot_be_reactivated'
    target_history=upload(env,body(env,role='si',targets=[old]),pdf('Invalid old target'))
    assert target_history.status_code==409 and target_history.json()['detail']=='file_already_superseded'
    assert snapshots(env)==baseline and bytes_snapshot(env)==physical


def test_implicit_main_add_rejected_before_saving_upload(audit_env):
    env=audit_env;fid,raw=add_main(env)
    baseline=snapshots(env);physical=bytes_snapshot(env);audit_before=events(env)
    denied=upload(env,body(env),pdf('Rejected unintended second main'))
    assert denied.status_code==409 and denied.json()['detail']=='main_source_requires_explicit_supersede'
    assert snapshots(env)==baseline and events(env)==audit_before
    assert bytes_snapshot(env)==physical


def test_simultaneous_file_set_cas_allows_only_one_winner(audit_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    env=audit_env;fid,raw=add_main(env)
    request1=body(env,targets=[fid]);request2={**request1,'request_id':str(uuid4())}
    clients=[env['client_for']('writer'),env['client_for']('writer')]
    barrier=Barrier(2)
    def invoke(index):
        barrier.wait(timeout=10)
        return upload(env,[request1,request2][index],pdf(f'Concurrent contender {index}'),client=clients[index])
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(invoke,[0,1]))
    finally:
        for client in clients:client.close()
    assert sorted(response.status_code for response in responses)==[200,409]
    winner=next(response.json() for response in responses if response.status_code==200)
    loser=next(response.json() for response in responses if response.status_code==409)
    assert loser['detail']=='file_set_revision_conflict'
    assert read(env)['preferred_main_id']==winner['file']['id']
    assert len([f for f in read(env)['items'] if f['role']=='main'])==2
    assert len(events(env))==2
