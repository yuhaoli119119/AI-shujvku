"""Isolated PostgreSQL HTTP acceptance of narrow, audited metadata correction."""
from __future__ import annotations

import copy
import hashlib
from uuid import uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select

from app.db.models import (AuditLog, Paper, PaperSection, RebuildPaperFile,
                           RebuildVisualAsset, RebuildDataRow, RebuildDataValue, RebuildValueSource)
from app.main import app
from app.services.rebuild_mutation_audit import safe_json, EVENT, RECEIPT
from test_rebuild_mutation_audit import audit_env


@pytest.fixture
def metadata_env(audit_env):
    env = audit_env
    with env['factory']() as s:
        file = s.get(RebuildPaperFile, env['file'])
        path = env['settings'].storage_root / file.storage_path
        with fitz.open(path) as document:
            for _ in range(4):
                document.new_page()
            raw = document.tobytes()
        path.write_bytes(raw)
        file.sha256 = hashlib.sha256(raw).hexdigest()
        file.file_size = len(raw)
        other_paper = Paper(title='Foreign synthetic paper', library_name='默认文献库',
                            paper_code='T998', pdf_path='foreign.pdf')
        asset = RebuildVisualAsset(paper_id=env['paper'], file_id=env['file'],
            asset_key='metadata-target', asset_type='figure', page_numbers=[5],
            y_axis_unit='Original unit', image_path='immutable.png', caption='Original caption', explanation='Original explanation',
            context_text='original context', bbox={'page': 5},
            provenance={'reading_explanation': {'text': 'preserve reading'}, 'identity': 'preserve'},
            unreadable_fields=['label'], material_mapping={'a': 'fixture'})
        section = PaperSection(paper_id=env['paper'], section_title='Legacy heading',
            section_type='legacy', text='Original text', page_start=5, page_end=6,
            section_level=2, section_number='S2', parent_heading='Methods', heading_path=['Methods'])
        untouched = PaperSection(paper_id=env['paper'], section_title='Untouched', text='Untouched text')
        s.add_all([other_paper, asset, section, untouched]); s.flush()
        row = RebuildDataRow(paper_id=env['paper'], row_key='linked', reaction='SRR',
                            material='fixture', active_site_type='dual_atom', data_type='dft')
        s.add(row); s.flush()
        value = RebuildDataValue(paper_id=env['paper'], row_id=row.id,
            field_name='adsorption_energy_ev', raw_value='1.000', numeric_value=1.0, precision_digits=3)
        s.add(value); s.flush()
        source = RebuildValueSource(paper_id=env['paper'], row_id=row.id, value_id=value.id,
            file_id=env['file'], asset_id=asset.id, source_key='linked-source', source_kind='figure',
            page_number=5, quote='1.000')
        s.add(source); s.flush(); value.preferred_source_id = source.id
        s.commit()
        env.update(asset=asset.id, section=section.id, untouched=untouched.id, foreign=other_paper.id)
    return env


def url(env, kind, target=None, paper=None):
    return f"/api/rebuild/papers/{paper or env['paper']}/{kind}/{target or env['asset' if kind == 'assets' else 'section']}/metadata"


def read(env, kind):
    response = env['client'].get(url(env, kind))
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result['expected_revision']) == 64
    return result


def request(env, kind, changes):
    return {'request_id': str(uuid4()), 'expected_revision': read(env, kind)['expected_revision'],
            'reason': 'Synthetic evidence verified', 'changes': changes}


def snapshots(env):
    with env['factory']() as s:
        return {model.__tablename__: {str(row.id): safe_json({col.key: getattr(row, col.key)
                    for col in inspect(model).column_attrs}) for row in s.scalars(select(model))}
                for model in (RebuildVisualAsset, PaperSection, RebuildDataRow, RebuildDataValue,
                              RebuildValueSource, RebuildPaperFile)}


@pytest.mark.parametrize('kind,changes', [('assets', {'y_axis_unit': 'Synthetic / a.u.'}), ('assets', {'y_axis_unit': None}), ('assets', {'page_numbers': [6]}),
                                          ('sections', {'section_title': 'Corrected heading'})])
def test_exact_target_omission_preservation_and_immutable_audit(metadata_env, kind, changes):
    env = metadata_env
    before = snapshots(env)
    body = request(env, kind, changes)
    response = env['client'].put(url(env, kind), json=body)
    assert response.status_code == 200, response.text
    assert response.json()['audit']['replayed'] is False
    after = snapshots(env)
    table = 'rebuild_visual_assets' if kind == 'assets' else 'paper_sections'
    target = str(env['asset' if kind == 'assets' else 'section'])
    original = before[table][target]
    updated = after[table][target]
    assert {k: updated[k] for k in changes} == changes
    # Runtime maintenance columns are separate from scientific content.
    allowed = set(changes) | ({'updated_at', 'version'} if kind == 'assets' else set())
    assert {k: v for k, v in original.items() if k not in allowed} == {
        k: v for k, v in updated.items() if k not in allowed}
    before[table][target] = after[table][target]
    assert before == after
    with env['factory']() as s:
        events = list(s.scalars(select(AuditLog).where(AuditLog.action == EVENT)))
        receipts = list(s.scalars(select(AuditLog).where(AuditLog.action == RECEIPT)))
        assert len(events) == len(receipts) == 1
        payload = copy.deepcopy(events[0].payload)
        assert payload['before'] == original and payload['after'] == updated
        assert payload['actor'] == 'writer' and payload['request_id'] == body['request_id']
    replay = env['client'].put(url(env, kind), json=body)
    assert replay.status_code == 200 and replay.json()['audit']['replayed'] is True
    assert snapshots(env) == after
    with env['factory']() as s:
        assert list(s.scalars(select(AuditLog).where(AuditLog.action == EVENT)))[0].payload == payload
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action.in_([EVENT, RECEIPT]))))) == 2


@pytest.mark.parametrize('kind,changes', [('assets', {'y_axis_unit': 'Synthetic / a.u.'}), ('assets', {'y_axis_unit': None}), ('assets', {'page_numbers': [6]}), ('sections', {'text': 'Corrected text'})])
def test_stale_reuse_actor_and_aba(metadata_env, kind, changes):
    env = metadata_env
    original = read(env, kind)
    body = request(env, kind, changes)
    assert env['client'].put(url(env, kind), json=body).status_code == 200
    baseline = snapshots(env)
    stale = {**body, 'request_id': str(uuid4())}
    assert env['client'].put(url(env, kind), json=stale).status_code == 409
    reused = copy.deepcopy(body); reused['reason'] = 'Changed body'
    assert env['client'].put(url(env, kind), json=reused).status_code == 409
    other = env['client_for']('other')
    try:
        assert other.put(url(env, kind), json=body).status_code == 409
    finally:
        other.close()
    assert snapshots(env) == baseline
    field = next(iter(changes))
    restore = request(env, kind, {field: original['snapshot'][field]})
    assert env['client'].put(url(env, kind), json=restore).status_code == 200
    # Even a content round-trip cannot make an earlier CAS token fresh again.
    assert read(env, kind)['expected_revision'] != original['expected_revision']
    assert env['client'].put(url(env, kind), json=stale).status_code == 409


@pytest.mark.parametrize('kind,changes', [
    *[('assets', {'y_axis_unit': v}) for v in (1, True, 1.5, [], {}, 'x' * 65)],
    ('assets', {}), ('assets', {'page_numbers': None}),
    ('assets', {'y_axis_unit': 'unit', 'page_numbers': [6]}),
    ('assets', {'y_axis_unit': 'unit', 'caption': None}),
    ('assets', {'page_numbers': [0]}), ('assets', {'page_numbers': [True]}),
    ('assets', {'page_numbers': [1.5]}), ('assets', {'page_numbers': [6, 6]}),
    ('assets', {'page_numbers': []}), ('assets', {'image_path': 'overwrite.png'}),
    ('assets', {'file_id': str(uuid4())}), ('assets', {'provenance': {}}),
    ('sections', {'text': ' '}), ('sections', {'section_title': ''}),
    ('sections', {'section_type': 'x' * 65}), ('sections', {'page_start': 7}),
    ('sections', {'heading_path': ['overwrite']}), ('sections', {}),
])
def test_field_validation_never_changes_any_target(metadata_env, kind, changes):
    env = metadata_env; before = snapshots(env)
    response = env['client'].put(url(env, kind), json=request(env, kind, changes))
    assert response.status_code == 422, response.text
    assert snapshots(env) == before


@pytest.mark.parametrize('kind', ['assets', 'sections'])
def test_cross_paper_ownership_and_missing_target(metadata_env, kind):
    env = metadata_env; before = snapshots(env)
    body = request(env, kind, {'page_numbers': [6]} if kind == 'assets' else {'text': 'new'})
    for path in (url(env, kind, paper=env['foreign']), url(env, kind, target=uuid4())):
        response = env['client'].put(path, json=body)
        assert response.status_code in (404, 422), response.text
    assert snapshots(env) == before


@pytest.mark.parametrize('kind,changes', [('assets', {'y_axis_unit': 'Synthetic / a.u.'}), ('assets', {'y_axis_unit': None}), ('assets', {'page_numbers': [6]}),
                                          ('sections', {'section_title': 'new', 'text': 'new text'})])
def test_audit_failure_rolls_back_entire_target(metadata_env, kind, changes):
    env = metadata_env; before = snapshots(env); body = request(env, kind, changes)
    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO audit_logs'):
            raise RuntimeError('synthetic metadata audit failure')
    event.listen(env['engine'], 'before_cursor_execute', fail_audit)
    try:
        response = env['client'].put(url(env, kind), json=body)
        assert response.status_code == 500, response.text
    finally:
        event.remove(env['engine'], 'before_cursor_execute', fail_audit)
    assert snapshots(env) == before
    with env['factory']() as s:
        assert not list(s.scalars(select(AuditLog).where(AuditLog.action.in_([EVENT, RECEIPT]))))
    assert env['client'].put(url(env, kind), json=body).status_code == 200


@pytest.mark.parametrize('kind', ['assets', 'sections'])
def test_auth_origin_and_csrf(metadata_env, kind):
    env = metadata_env; before = snapshots(env)
    body = request(env, kind, {'page_numbers': [6]} if kind == 'assets' else {'text': 'new'})
    with TestClient(app) as anonymous:
        assert anonymous.get(url(env, kind)).status_code == 401
        assert anonymous.put(url(env, kind), json=body).status_code == 401
    saved = dict(env['client'].headers)
    env['client'].headers.pop('X-CSRF-Token')
    assert env['client'].put(url(env, kind), json=body).status_code == 403
    env['client'].headers.update(saved)
    env['client'].headers['Origin'] = 'https://untrusted.invalid'
    assert env['client'].put(url(env, kind), json=body).status_code == 403
    env['client'].headers.update(saved)
    assert snapshots(env) == before


def test_source_bound_import_section_rejected(metadata_env):
    env = metadata_env
    body = request(env, 'sections', {'text': 'attempted replacement'})
    with env['factory']() as s:
        s.add(AuditLog(paper_id=env['paper'], action='section_imported', source='rebuild_sections_v1',
                       target_type='paper_section', target_id=str(env['section']), payload={'original': True}))
        s.commit()
    before = snapshots(env)
    response = env['client'].put(url(env, 'sections'), json=body)
    assert response.status_code == 422, response.text
    assert snapshots(env) == before


@pytest.mark.parametrize('kind,field', [('assets', 'page_numbers'), ('assets', 'y_axis_unit'), ('sections', 'text')])
def test_concurrent_same_revision_only_one_winner(metadata_env, kind, field):
    from concurrent.futures import ThreadPoolExecutor
    env = metadata_env
    changes = {'page_numbers': [6]} if kind == 'assets' else {'text': 'first correction'}
    if field == 'y_axis_unit':
        changes = {'y_axis_unit': 'first unit'}
    first = request(env, kind, changes)
    second = copy.deepcopy(first); second['request_id'] = str(uuid4())
    second['changes'] = {'page_numbers': [4]} if kind == 'assets' else {'text': 'second correction'}
    if field == 'y_axis_unit':
        second['changes'] = {'y_axis_unit': 'second unit'}
    def send(body):
        client = env['client_for']('writer')
        try:
            return client.put(url(env, kind), json=body)
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [first, second]))
    assert sorted(result.status_code for result in results) == [200, 409], [r.text for r in results]
    with env['factory']() as s:
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action == EVENT)))) == 1
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action == RECEIPT)))) == 1


def test_asset_existing_foreign_file_pointer_rejected(metadata_env):
    env = metadata_env
    body = request(env, 'assets', {'page_numbers': [6]})
    with env['factory']() as s:
        foreign_file = RebuildPaperFile(paper_id=env['foreign'], role='main',
            storage_path='foreign.pdf', original_filename='foreign.pdf', sha256='f' * 64, file_size=1)
        s.add(foreign_file); s.flush()
        s.get(RebuildVisualAsset, env['asset']).file_id = foreign_file.id
        s.commit()
    before = snapshots(env)
    response = env['client'].put(url(env, 'assets'), json=body)
    assert response.status_code == 422, response.text
    assert snapshots(env) == before


def test_full_raw_provenance_affects_revision_even_when_sensitive_key_redacted(metadata_env):
    env = metadata_env
    with env['factory']() as s:
        asset = s.get(RebuildVisualAsset, env['asset'])
        asset.provenance = {**asset.provenance, 'api_key': 'synthetic-before-never-real'}
        s.commit()
    body = request(env, 'assets', {'page_numbers': [6]})
    with env['factory']() as s:
        asset = s.get(RebuildVisualAsset, env['asset'])
        asset.provenance = {**asset.provenance, 'api_key': 'synthetic-after-never-real'}
        # Restore updated_at so the provenance itself must invalidate CAS.
        previous_time = asset.updated_at
        s.flush()
        asset.updated_at = previous_time
        s.commit()
    before = snapshots(env)
    assert env['client'].put(url(env, 'assets'), json=body).status_code == 409
    assert snapshots(env) == before
    assert read(env, 'assets')['expected_revision'] != body['expected_revision']


def test_sensitive_named_provenance_snapshot_and_receipt_are_lossless(metadata_env):
    env = metadata_env
    provenance = {'api_key': 'synthetic-marker-only', 'nested': {'authorization': 'scientific-label'}}
    with env['factory']() as s:
        s.get(RebuildVisualAsset, env['asset']).provenance = provenance
        s.commit()
    state = read(env, 'assets')
    assert state['snapshot']['provenance'] == provenance
    body = {'request_id': str(uuid4()), 'expected_revision': state['expected_revision'],
            'reason': 'Preserve exact metadata', 'changes': {'page_numbers': [6]}}
    result = env['client'].put(url(env, 'assets'), json=body)
    assert result.status_code == 200, result.text
    original = result.json()
    assert original['snapshot']['provenance'] == original['before']['provenance'] == provenance
    replay = env['client'].put(url(env, 'assets'), json=body)
    assert replay.status_code == 200
    expected = copy.deepcopy(original); expected['audit']['replayed'] = True
    assert replay.json() == expected
    with env['factory']() as s:
        asset = s.get(RebuildVisualAsset, env['asset'])
        assert asset.provenance == provenance
        audit = s.scalar(select(AuditLog).where(AuditLog.action == EVENT))
        assert audit.payload['before']['provenance'] == audit.payload['after']['provenance'] == provenance
        receipt = s.scalar(select(AuditLog).where(AuditLog.action == RECEIPT))
        assert receipt.payload['result'] == original


def test_section_all_allowed_fields_then_explicit_null_type(metadata_env):
    env = metadata_env
    with env['factory']() as s:
        s.get(PaperSection, env['section']).section_type = 'table'
        s.commit()
    before = snapshots(env)
    changes = {'section_title': 'Corrected plain text section', 'section_type': 'text',
               'text': 'Original evidence classified as text rather than a table'}
    body = request(env, 'sections', changes)
    response = env['client'].put(url(env, 'sections'), json=body)
    assert response.status_code == 200, response.text
    updated = response.json()['snapshot']
    assert {key: updated[key] for key in changes} == changes
    original = before['paper_sections'][str(env['section'])]
    assert {key: value for key, value in updated.items() if key not in changes} == {
        key: value for key, value in original.items() if key not in changes}
    null_body = request(env, 'sections', {'section_type': None})
    response = env['client'].put(url(env, 'sections'), json=null_body)
    assert response.status_code == 200, response.text
    final = response.json()['snapshot']
    assert final == {**updated, 'section_type': None}
    after = snapshots(env)
    before['paper_sections'][str(env['section'])] = after['paper_sections'][str(env['section'])]
    assert before == after
    with env['factory']() as s:
        events = list(s.scalars(select(AuditLog).where(AuditLog.action == EVENT).order_by(AuditLog.created_at)))
        assert len(events) == 2
        assert events[0].payload['after'] == events[1].payload['before'] == updated
        assert events[1].payload['after'] == final
