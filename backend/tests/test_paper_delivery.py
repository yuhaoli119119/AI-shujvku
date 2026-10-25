"""Delivery reads over isolated PostgreSQL: no model calls or production data."""
import copy
import hashlib

from sqlalchemy import inspect, select

from app.db.models import AuditLog, Paper, RebuildDataRow, WorkflowJob
from test_rebuild_mutation_audit import audit_env, row_body
from test_rebuild_sections import chapter_env
from test_reading_assistance import reading_env


def url(env, suffix=''):
    return f'/api/rebuild/papers/{env["paper"]}/delivery-package' + suffix


def fingerprint(env):
    with env['factory']() as session:
        return {model.__tablename__: [repr({c.key: getattr(row, c.key) for c in inspect(model).column_attrs})
                for row in session.scalars(select(model).order_by(model.id if hasattr(model, 'id') else model.job_id))]
                for model in (Paper, AuditLog, RebuildDataRow, WorkflowJob)}


def test_legacy_no_plan_retained_and_reads_do_not_write(audit_env):
    env = audit_env
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        paper.comprehensive_analysis = {'abstract_zh': '旧中文摘要'}
        paper.reading_guide = {'beginner_zh': {'overview': '旧导读'}}
        session.add(WorkflowJob(job_id='unknown-writer', type='rebuild_external_extract', status='running',
                                payload={'paper_id': str(env['paper'])}))
        session.commit()
    before = fingerprint(env)
    response = env['client'].get(url(env))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['coverage_declaration']['present'] is False
    assert data['reading']['abstract_zh'] == '旧中文摘要'
    assert data['reading']['guide_status'] == 'unknown_legacy_provenance'
    assert data['execution']['can_unlock'] is False
    assert data['acceptance']['status'] == 'pending'
    jobs = env['client'].get(url(env, '?view=jobs')).json()['items']
    assert jobs[0]['status_as_recorded'] == 'running'
    assert fingerprint(env) == before


def test_object_receipt_readback_then_drift_and_revision_pin(audit_env):
    env = audit_env
    saved = env['client'].post('/api/rebuild/rows/import', json=row_body(env))
    assert saved.status_code == 200, saved.text
    response = env['client'].get(url(env, '?view=checkpoints&limit=1'))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['items'][0]['readback_status'] == 'matches_saved_image'
    assert data['items'][0]['receipt_id']
    assert env['client'].get(data['next_page']).status_code == 200
    assert env['client'].get(url(env, '?view=checkpoints&offset=1')).status_code == 409
    with env['factory']() as session:
        row = session.scalar(select(RebuildDataRow).where(RebuildDataRow.paper_id == env['paper']))
        row.material = 'changed after receipt'
        session.commit()
    assert env['client'].get(data['next_page']).status_code == 409
    items = env['client'].get(url(env, '?view=checkpoints')).json()['items']
    assert next(i for i in items if i['kind'] == 'rebuild_data_rows')['readback_status'] == 'changed_since_save'


def test_main_text_abstract_without_metadata_is_preserved(audit_env):
    env = audit_env
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        paper.abstract = None
        paper.comprehensive_analysis = {'abstract_zh': '来自真实正文的译文'}
        from app.db.models import RebuildPaperFile
        file = session.get(RebuildPaperFile, env['file'])
        paper.reading_guide = {'abstract_translation': {
            'original_text': 'Research abstract', 'translation_sha256': hashlib.sha256('来自真实正文的译文'.encode()).hexdigest(),
            'source': {'kind': 'main_text', 'file_id': str(file.id), 'source_sha256': file.sha256}}}
        session.commit()
    data = env['client'].get(url(env)).json()
    assert data['reading']['abstract_status'] == 'source_identity_match_only'
    assert data['reading']['abstract_zh'] == '来自真实正文的译文'


def test_reading_change_invalidates_delivery_but_not_scientific_revision(audit_env):
    env = audit_env
    first = env['client'].get(url(env)).json()
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        paper.comprehensive_analysis = {'abstract_zh': '补存摘要'}
        session.commit()
    assert env['client'].get(first['pages']['jobs']).status_code == 409
    second = env['client'].get(url(env)).json()
    assert second['output_revision'] == first['output_revision']
    assert second['revision'] != first['revision']


def test_gaps_preserve_unknown_scope_and_missing_modules(audit_env):
    env = audit_env
    response = env['client'].get(url(env, '?view=gaps'))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['scientific_acceptance'] == 'not_implied'
    assert any(i['code'] == 'coverage_plan_missing' for i in data['items'])
    assert {i['id'] for i in data['items'] if i['kind'] == 'reading_module'} == {'abstract', 'guide'}


def test_receipt_replay_does_not_create_checkpoint_duplicates(audit_env):
    env = audit_env
    body = row_body(env)
    assert env['client'].post('/api/rebuild/rows/import', json=body).status_code == 200
    first = env['client'].get(url(env, '?view=checkpoints')).json()
    assert env['client'].post('/api/rebuild/rows/import', json=copy.deepcopy(body)).status_code == 200
    second = env['client'].get(url(env, '?view=checkpoints')).json()
    assert second == first


def test_legacy_asset_without_file_binding_stays_visible(audit_env):
    env = audit_env
    from app.db.models import RebuildVisualAsset
    with env['factory']() as session:
        asset = RebuildVisualAsset(paper_id=env['paper'], file_id=None, asset_key='legacy-figure',
                                   asset_type='figure', caption='Original caption', explanation='Original explanation')
        session.add(asset)
        session.commit()
        aid = str(asset.id)
    before = fingerprint(env)
    response = env['client'].get(url(env, '?view=checkpoints'))
    assert response.status_code == 200, response.text
    item = next(i for i in response.json()['items'] if i['id'] == aid)
    assert item['save_status'] == 'stored_legacy_receipt_unknown'
    old = env['client'].get(f'/api/rebuild/papers/{env["paper"]}/ai-work-package?view=assets').json()
    assert next(a for a in old['items'] if a['id'] == aid)['caption'] == 'Original caption'
    assert fingerprint(env) == before


def test_reading_modules_keep_separate_receipts_and_current_readback(audit_env):
    env = audit_env
    from datetime import datetime, timedelta
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        guide = {'beginner_zh': {'overview': '已保存导读'}}
        paper.reading_guide = guide
        paper.comprehensive_analysis = {'abstract_zh': '后来单补摘要'}
        session.add(AuditLog(paper_id=paper.id, action='beginner_zh_v1', source='beginner_zh_v1',
            target_type='paper_reading', target_id='full-reading', created_at=datetime.utcnow() - timedelta(seconds=1),
            payload={'before': {'reading_guide': None, 'abstract_zh': None},
                     'after': {'reading_guide': guide, 'abstract_zh': '原摘要'}, 'receipt': {'saved': True}}))
        session.add(AuditLog(paper_id=paper.id, action='beginner_zh_v1', source='beginner_zh_v1',
            target_type='paper_reading', target_id='abstract-only',
            payload={'before': {'reading_guide': guide, 'abstract_zh': '原摘要'},
                     'after': {'reading_guide': guide, 'abstract_zh': '后来单补摘要'}, 'receipt': {'saved': True}}))
        session.commit()
    items = env['client'].get(url(env, '?view=checkpoints')).json()['items']
    modules = {i['id']: i for i in items if i['kind'] == 'reading_module'}
    assert modules['guide']['request_id'] == 'full-reading'
    assert modules['abstract']['request_id'] == 'abstract-only'
    assert {i['readback_status'] for i in modules.values()} == {'matches_saved_image'}


def test_delivery_requires_session(audit_env):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        assert client.get(url(audit_env)).status_code == 401


def test_formal_reading_save_then_metadata_change_marks_both_stale(audit_env, monkeypatch):
    env = audit_env
    from test_reading_assistance import body
    async def no_dispatch(**kwargs):
        pass
    monkeypatch.setattr('app.services.ai_extract_service._background_dispatch', no_dispatch)
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        paper.abstract = 'A catalyst improves sulfur conversion under tested conditions.'
        session.commit()
    base = f'/api/rebuild/papers/{env["paper"]}/reading-assistance'
    created = env['client'].post(base + '/jobs', json={'request_id': 'delivery-reading', 'model': 'cn:deepseek-v4.1-flash'})
    assert created.status_code == 200, created.text
    saved = env['client'].post(base, json=body(env, created.json()['job_id']))
    assert saved.status_code == 200, saved.text
    first = env['client'].get(url(env)).json()
    assert first['reading']['abstract_status'] == 'source_identity_match_only'
    assert first['reading']['guide_status'] == 'source_identity_match_only'
    modules = {i['id']: i for i in env['client'].get(url(env, '?view=checkpoints')).json()['items'] if i['kind'] == 'reading_module'}
    assert all(item['readback_status'] == 'matches_saved_image' for item in modules.values())
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        paper.abstract = 'New metadata abstract changes the stated result.'
        session.commit()
    after = env['client'].get(url(env)).json()
    assert after['reading']['abstract_status'] == 'stale'
    assert after['reading']['guide_status'] == 'stale'
    assert after['reading']['abstract_zh'] == first['reading']['abstract_zh']
    assert after['reading']['guide'] == first['reading']['guide']


def test_formal_section_save_readback_and_changed_text(chapter_env):
    env = chapter_env
    from test_rebuild_sections import body, post
    from app.db.models import PaperSection
    saved = post(env, body(env))
    assert saved.status_code == 200, saved.text
    sid = saved.json()['items'][0]['section_id']
    endpoint = f'/api/rebuild/papers/{env["paper_id"]}/delivery-package?view=checkpoints'
    first = env['client'].get(endpoint)
    assert first.status_code == 200, first.text
    item = next(i for i in first.json()['items'] if i['id'] == sid)
    assert item['readback_status'] == 'matches_saved_image'
    from uuid import UUID
    with env['factory']() as session:
        session.get(PaperSection, UUID(sid)).text = 'Changed after saved section'
        session.commit()
    item = next(i for i in env['client'].get(endpoint).json()['items'] if i['id'] == sid)
    assert item['readback_status'] == 'changed_since_save'


def test_formal_pdf_abstract_save_with_empty_metadata_and_physical_drift(reading_env):
    env = reading_env
    from test_reading_abstract_recovery import source_pdf, start_patch, patch, base
    from app.db.models import RebuildPaperFile
    source_pdf(env)
    job = start_patch(env)
    saved = env['client'].post(base(env), json=patch(env, job['job_id']))
    assert saved.status_code == 200, saved.text
    first = env['client'].get(url(env)).json()
    assert first['reading']['abstract_original'] is None
    assert first['reading']['abstract_status'] == 'source_identity_match_only'
    assert first['reading']['guide_status'] == 'unknown_legacy_provenance'
    with env['factory']() as session:
        file = session.get(RebuildPaperFile, env['file'])
        path = env['settings'].storage_root / file.storage_path
    path.write_bytes(path.read_bytes() + b'\nsource changed')
    after = env['client'].get(url(env)).json()
    assert after['reading']['abstract_status'] == 'source_unverified'
    assert after['reading']['abstract_zh'] == first['reading']['abstract_zh']


def test_source_drift_marks_only_related_objects_without_changing_saved_images(audit_env):
    env = audit_env
    from app.db.models import RebuildPaperFile
    from uuid import UUID
    with env['factory']() as session:
        first = session.get(RebuildPaperFile, env['file'])
        path = env['settings'].storage_root / 'second.pdf'
        path.write_bytes((env['settings'].storage_root / first.storage_path).read_bytes() + b'\nindependent second source fixture')
        second = RebuildPaperFile(paper_id=env['paper'], role='si', storage_path='second.pdf', original_filename='second.pdf',
                                  sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
        session.add(second)
        session.commit()
        second_id = str(second.id)
    for fid, material in [(str(env['file']), 'fixture'), (second_id, 'unrelated')]:
        payload = row_body(env)
        payload['rows'][0]['row_key'] = material
        payload['rows'][0]['material'] = material
        payload['rows'][0]['values'][0]['sources'][0]['file_id'] = fid
        saved = env['client'].post('/api/rebuild/rows/import', json=payload)
        assert saved.status_code == 200, saved.text
    endpoint = url(env, '?view=checkpoints&limit=100')
    before = env['client'].get(endpoint).json()['items']
    values = [i for i in before if i['kind'] == 'rebuild_data_values']
    assert all(i['source_bindings'][0]['source_readback_status'] == 'matches_saved_source_identity' for i in values)
    original_pin = next(i for i in values if i['source_bindings'][0]['file_id'] == str(env['file']))['source_bindings'][0]['saved_source_pin']
    with env['factory']() as session:
        file = session.get(RebuildPaperFile, env['file'])
        path = env['settings'].storage_root / file.storage_path
        path.write_bytes(path.read_bytes() + b'\nnew source edition')
        file.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        file.file_size = path.stat().st_size
        session.commit()
    after = env['client'].get(endpoint).json()['items']
    values = [i for i in after if i['kind'] == 'rebuild_data_values']
    assert all(i['readback_status'] == 'matches_saved_image' for i in values)
    bindings = {i['source_bindings'][0]['file_id']: i['source_bindings'][0] for i in values}
    assert bindings[str(env['file'])]['source_readback_status'] == 'source_changed_since_save'
    assert bindings[second_id]['source_readback_status'] == 'matches_saved_source_identity'
    assert bindings[str(env['file'])]['saved_source_pin'] == original_pin
    assert all(i['field_name'] == 'adsorption_energy_ev' and i['row_id'] and i['object_read_endpoint'] for i in values)


def test_legacy_current_pin_never_becomes_fabricated_historical_pin(audit_env):
    env = audit_env
    from app.db.models import RebuildVisualAsset, RebuildPaperFile
    with env['factory']() as session:
        file = session.get(RebuildPaperFile, env['file'])
        session.add(RebuildVisualAsset(paper_id=env['paper'], file_id=file.id, asset_key='legacy-bound', asset_type='figure',
            provenance={'page_identity': {'source_sha256': file.sha256}}))
        session.commit()
    item = next(i for i in env['client'].get(url(env, '?view=checkpoints')).json()['items'] if i['kind'] == 'rebuild_visual_assets')
    binding = item['source_bindings'][0]
    assert binding['current_stored_pin']['source_sha256']
    assert binding['current_registered_source']['source_sha256']
    assert binding['saved_source_pin'] is None
    assert binding['source_readback_status'] == 'unknown_saved_source_pin'
