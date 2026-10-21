"""Isolated PostgreSQL reader integration: replacement never borrows old science."""
import hashlib
from uuid import uuid4

import fitz
from sqlalchemy import select

from app.db.models import Paper, PaperRelationship, RebuildPaperFile, RebuildValueSource
from app.services.rebuild_file_lifecycle import apply_change, file_set
from app.services.rebuild_work_package import compact_package, mark_scientific_file_bindings
from app.services.rebuild_quality import integrity_report
from app.services.rebuild_table_reading import table_representations
from app.services.rebuild_acceptance import current_snapshot
from test_rebuild_mutation_audit import audit_env, assets, row_body


def replace_fixture_si(env):
    path = env['settings'].storage_root / 'replacement.pdf'
    with fitz.open() as document:
        document.new_page().insert_text((30, 40), 'Distinct replacement PDF; science not reviewed')
        document.save(path)
    with env['factory']() as session:
        before = file_set(session, env['paper'])
        record = RebuildPaperFile(paper_id=env['paper'], role='si', storage_path=path.name,
            original_filename=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
        session.add(record); session.flush()
        body = {'request_id': str(uuid4()), 'expected_revision': before['file_set_revision'], 'role': 'si',
            'action': 'supersede', 'reason': 'Synthetic reader integration only', 'supersede_file_ids': [str(env['file'])]}
        apply_change(session, env['paper'], body, 'writer', 'synthetic-fingerprint', record, before, created=True)
        session.commit()
        return record.id


def test_active_file_scope_preserves_old_science_and_flags_review(audit_env):
    env = audit_env
    response = env['client'].post('/api/rebuild/rows/import', json=row_body(env))
    assert response.status_code == 200, response.text
    with env['factory']() as session:
        original = session.scalar(select(RebuildValueSource))
        old_identity = (original.id, original.file_id, original.page_number, original.quote)
        initial, _ = current_snapshot(session, env['paper'])
        assert 'file_lifecycle' not in initial  # Existing unambiguous snapshots stay compatible.
    new_id = replace_fixture_si(env)
    with env['factory']() as session:
        snapshot, files = current_snapshot(session, env['paper'])
        assert [file.id for file in files] == [new_id]
        assert snapshot['revision'] != initial['revision']
        assert snapshot['file_lifecycle']['scientific_review_required_file_ids'] == [str(env['file'])]
        package = compact_package(session, env['paper'], snapshot)
        assert [item['id'] for item in package['files']] == [str(new_id)]
        assert [item['id'] for item in package['file_history']] == [str(env['file'])]
        quality = integrity_report(session, env['paper'], env['settings'])
        assert [item['file_id'] for item in quality['files']] == [str(new_id)]
        assert any(item['code'] == 'source_file_superseded_requires_scientific_review'
                   and item['file_id'] == str(env['file']) for item in quality['issues'])
        original = session.scalar(select(RebuildValueSource))
        assert (original.id, original.file_id, original.page_number, original.quote) == old_identity


def test_old_bound_formal_table_cannot_complete_new_source(audit_env):
    env = audit_env
    response = env['client'].post(assets(env), json={'asset_key': 'old-table', 'asset_type': 'table',
        'file_id': str(env['file']), 'page_numbers': [1], 'figure_label': 'S1',
        'provenance': {'table_markdown': '| A | B |\n| --- | --- |\n| original | 1.00 |',
            'table_sources': [{'file_id': str(env['file']), 'page_number': 1, 'role': 'body', 'locator': 'synthetic table body'}]}})
    assert response.status_code == 200, response.text
    replace_fixture_si(env)
    with env['factory']() as session:
        readings = table_representations(session, env['paper'], env['settings'])
        old_table = next(item for item in readings['tables'] if item['asset_id'] == response.json()['asset']['id'])
        assert not old_table['complete']
        assert old_table['reason'] == 'source_file_superseded_requires_scientific_review'
        assert old_table['binding']['file_id'] == str(env['file'])


def test_explicit_markers_keep_source_uuid_page_and_original_value(audit_env):
    old_id = str(audit_env['file'])
    source = {'id': str(uuid4()), 'file_id': old_id, 'page_number': 2, 'quote': '0.0100'}
    row = {'values': [{'raw_value': '0.0100', 'sources': [source]}]}
    assert mark_scientific_file_bindings(row, {old_id}) == {old_id}
    assert row['scientific_review_required'] and row['values'][0]['scientific_review_required']
    assert source['file_id'] == old_id and source['page_number'] == 2 and source['quote'] == '0.0100'
    assert row['values'][0]['raw_value'] == '0.0100'


def test_superseded_linked_si_does_not_reappear_as_unregistered_or_parsed(audit_env):
    from app.services.catalog_attachments import supplementary_attachments
    from app.services.paper_processing_status import processing_statuses
    env = audit_env
    response = env['client'].post('/api/rebuild/rows/import', json=row_body(env))
    assert response.status_code == 200, response.text
    with env['factory']() as session:
        child = Paper(title='Synthetic original SI', library_name='默认文献库', pdf_path='fixture.pdf')
        session.add(child); session.flush()
        session.add(PaperRelationship(source_paper_id=env['paper'], target_paper_id=child.id, relationship_type='supplementary'))
        session.commit()
        assert supplementary_attachments(session, [env['paper']])[env['paper']][0]['file_id'] == str(env['file'])
    new_id = replace_fixture_si(env)
    with env['factory']() as session:
        attachments = supplementary_attachments(session, [env['paper']])[env['paper']]
        assert len(attachments) == 1 and attachments[0]['file_id'] == str(new_id)
        status = processing_statuses(session, [env['paper']])[env['paper']]
        assert len(status['files']) == 1 and status['files'][0]['file_id'] == str(new_id)
        assert status['files'][0]['status'] == 'unparsed'
        assert status['file_history'][0]['id'] == str(env['file'])
        assert status['scientific_review_required']


def test_superseded_chapter_provenance_remains_original_and_review_is_explicit(audit_env):
    import copy
    from app.services.rebuild_work_package import bounded_view
    env = audit_env
    with env['factory']() as session:
        digest = session.get(RebuildPaperFile, env['file']).sha256
    payload = {'request_id': str(uuid4()), 'sections': [{'file_id': str(env['file']),
        'source_sha256': digest, 'section_key': 'synthetic-first', 'page_basis': 'source_pdf',
        'section_title': 'Synthetic heading', 'text': 'Synthetic table: s 1.00s',
        'explanation_zh': '合成测试章节，验证来源身份保留。',
        'page_start': 1, 'page_end': 1,
        'sources': [{'page_number': 1, 'quote': 'Synthetic table: s 1.00s', 'locator': 'synthetic first paragraph'}]}]}
    response = env['client'].post(f'/api/rebuild/papers/{env["paper"]}/sections/import', json=payload)
    assert response.status_code == 200, response.text
    replace_fixture_si(env)
    with env['factory']() as session:
        quality = integrity_report(session, env['paper'], env['settings'])
        assert any(item['code'] == 'source_file_superseded_requires_scientific_review'
                   and item['kind'] == 'section' for item in quality['issues'])
        page = bounded_view(session, env['paper'], view='sections', snapshot={'revision': 'synthetic'}, limit=25, offset=0)
        section = page['items'][0]
        assert section['scientific_review_required']
        raw_event = copy.deepcopy(section['source_import_event'])
        assert raw_event['section']['file_id'] == str(env['file'])
        assert raw_event['section']['page_start'] == 1
        assert 'scientific_review_required' not in raw_event
        assert 'scientific_review_required' not in raw_event['section']
        assert section['text'] == payload['sections'][0]['text']
