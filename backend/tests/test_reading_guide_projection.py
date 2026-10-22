"""Guide API/export against synthetic isolated PostgreSQL and file fixtures."""
import base64
import hashlib
from copy import deepcopy
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.api.papers.detail import router
from app.config import Settings, get_settings
from app.db.models import (AuditLog, Paper, PaperFigure, PaperSection,
                           RebuildPaperFile, RebuildVisualAsset)
from app.db.session import get_db_session
from app.services.figure_reading_service import FigureReadingService
from app.services.reading_guide_projection import ReadingGuideProjectionError
from app.services.rebuild_sections import EVENT, VERSION, _digest, list_sections


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9sAAAAASUVORK5CYII=')


@pytest.fixture
def guide_env(setup_test_db, tmp_path, monkeypatch):
    settings = Settings(storage_root=tmp_path)
    monkeypatch.setenv('LITAI_STORAGE_ROOT', str(tmp_path))
    get_settings.cache_clear()
    factory = sessionmaker(bind=setup_test_db, expire_on_commit=False)
    root = settings.storage_paths['figures']; root.mkdir(parents=True)
    (root / 'stored.png').write_bytes(PNG)
    with factory() as session:
        p = Paper(title='Synthetic guide <paper>', pdf_path='')
        other = Paper(title='Unrelated paper', pdf_path='')
        session.add_all([p, other]); session.flush()
        f = RebuildPaperFile(paper_id=p.id, role='si', storage_path='si.docx',
            original_filename='si.docx', sha256='a' * 64, file_size=123)
        session.add(f); session.flush()
        chapters = [PaperSection(id=UUID(int=i), paper_id=p.id,
            section_title=title, section_type='results', text=text, page_start=page,
            page_end=page, section_number=str(i), heading_path=['Results', title])
            for i, title, text, page in [(3, 'Last chapter', 'Original final text', None),
                (2, 'Same title', 'Same original text <script>bad()</script>', 1),
                (1, 'Same title', 'Same original text <script>bad()</script>', 1)]]
        legacy = PaperFigure(paper_id=p.id, figure_label='Legacy Figure', page=1,
            image_path=str(root / 'stored.png'), caption='Legacy caption')
        a = RebuildVisualAsset(paper_id=p.id, file_id=f.id, asset_key='si-s1',
            asset_type='figure', figure_label='Figure S1', caption='Original SI <caption>',
            page_numbers=[], image_path='figures/stored.png', status='reviewed',
            provenance={'page_identity': {'page_basis': 'ooxml', 'source_sha256': f.sha256,
                'page_number': None, 'reading_pdf_sha256': None,
                'ooxml_locator': 'word/document.xml#body-child[1]/w:p[1]'}})
        # Equal labels must never collapse distinct source assets or subfigures.
        b = RebuildVisualAsset(paper_id=p.id, asset_key='si-s1-panel', asset_type='subfigure',
            figure_label='Figure S1', subfigure_label='a', caption='Panel caption',
            page_numbers=[], image_path='figures/stored.png', status='draft')
        alien = RebuildVisualAsset(paper_id=other.id, asset_key='alien', asset_type='figure',
            figure_label='Alien', page_numbers=[], image_path='missing.png', status='reviewed')
        session.add_all([*chapters, legacy, a, b, alien]); session.commit()
        ids = {'paper': p.id, 'file': f.id, 'asset': a.id, 'panel': b.id,
               'legacy': legacy.id, 'alien': alien.id}
    app = FastAPI(); app.include_router(router, prefix='/api/papers')
    def db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    with TestClient(app) as client:
        yield factory, settings, ids, client
    get_settings.cache_clear()


def snapshot(session):
    return {model.__tablename__: [
        {c.key: deepcopy(getattr(row, c.key)) for c in model.__table__.columns}
        for row in session.scalars(select(model).order_by(model.id))]
        for model in (Paper, PaperSection, PaperFigure, RebuildVisualAsset, RebuildPaperFile, AuditLog)}


def test_complete_api_export_deterministic_and_readonly(guide_env):
    factory, settings, ids, client = guide_env
    with factory() as session:
        before = snapshot(session)
        service = FigureReadingService(session, settings)
        data = service.get_paper_reading_guide_data(ids['paper'])
        assert data == service.get_paper_reading_guide_data(ids['paper'])
        assert [s['id'] for s in data['sections']] == [str(UUID(int=i)) for i in (3, 1, 2)]
        assert [s['order'] for s in data['sections']] == [0, 1, 2]
        assert data['sections'] == [{**s, 'order': i} for i, s in enumerate(list_sections(session, ids['paper']))]
        assert [f['figure_id'] for f in data['figures']] == list(map(str, [ids['legacy'], ids['asset'], ids['panel']]))
        xml = data['figures'][1]
        assert xml['page'] is None and xml['page_basis'] == 'ooxml'
        assert xml['image_url'] == f'/api/rebuild/assets/{ids["asset"]}'
        assert xml['source_file']['id'] == str(ids['file'])
        assert xml['status'] == 'reviewed' and data['figures'][2]['status'] == 'draft'
        html = service.render_offline_html(ids['paper'])
        assert html.count('class="card section-block"') == 3
        assert html.count('class="figure-block"') == 3
        assert html.count('data:image/png;base64,') == 3
        assert 'Original final text' in html and '&lt;script&gt;bad()&lt;/script&gt;' in html
        assert '<script>bad()' not in html
        assert 'word/document.xml#body-child[1]/w:p[1]' in html
        assert 'OOXML 定位（无物理页）' in html and 'Page -' not in html
        assert f'/api/rebuild/files/{ids["file"]}/download' in html
        assert snapshot(session) == before and not session.dirty and not session.new
    response = client.get(f'/api/papers/{ids["paper"]}/reading-guide')
    assert response.status_code == 200 and response.json()['projection_complete']
    exported = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert exported.status_code == 200, exported.text
    assert hashlib.sha256(exported.content).hexdigest() == exported.headers['x-export-sha256']
    assert len(exported.content) == int(exported.headers['x-export-size'])
    assert 'Original final text' in exported.text
    assert f'href="http://testserver/api/rebuild/files/{ids["file"]}/download"' in exported.text
    with factory() as session:
        assert snapshot(session) == before


@pytest.mark.parametrize('change,code', [
    ({'image_path': None}, 'missing_or_malformed_image_pointer'),
    ({'image_path': 'figures/missing.png'}, 'image_not_found'),
    ({'image_path': '../stored.png'}, 'malformed_image_pointer'),
    ({'image_path': 'figures/wrong/stored.png'}, 'image_not_found'),
    ({'image_path': 'https://example.invalid/figure.png'}, 'malformed_image_pointer'),
    ({'page_numbers': 3}, 'malformed_page_numbers'),
    ({'provenance': {'page_identity': []}}, 'malformed_page_identity'),
    ({'provenance': {'media': 'invalid'}}, 'malformed_media_pointer'),
    ({'provenance': {'page_identity': {'page_basis': 'ooxml', 'ooxml_locator': 'bad'}}}, 'malformed_ooxml_pointer'),
    ({'provenance': {'source_sha256': 'b' * 64}}, 'source_sha256_mismatch'),
])
def test_broken_pointer_visible_api_and_export_fails_without_artifact(guide_env, change, code):
    factory, settings, ids, client = guide_env
    with factory() as session:
        a = session.get(RebuildVisualAsset, ids['asset'])
        for key, value in change.items(): setattr(a, key, value)
        session.commit()
    response = client.get(f'/api/papers/{ids["paper"]}/reading-guide')
    assert response.status_code == 200
    data = response.json()
    assert len(data['figures']) == 3 and not data['projection_complete']
    assert {'asset_id': str(ids['asset']), 'code': code} in data['projection_errors']
    exported = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert exported.status_code == 409
    assert exported.json()['detail']['code'] == 'reading_guide_projection_incomplete'
    assert {'asset_id': str(ids['asset']), 'code': code} in exported.json()['detail']['issues']
    assert not (settings.storage_root / 'exports').exists()


def test_legacy_docx_media_and_hash_failure_preserve_existing_export(guide_env):
    factory, settings, ids, client = guide_env
    with factory() as session:
        asset = session.get(RebuildVisualAsset, ids['asset'])
        asset.provenance = {'source_sha256': 'a' * 64,
            'media': {'sha256': hashlib.sha256(PNG).hexdigest(), 'zip_member': 'word/media/image1.png',
                      'relationship_target': 'media/image1.png'},
            'caption_xml_locator': '/w:document/w:body/w:p[39]',
            'media_occurrence': {'r_id': 'rId7'}}
        session.commit()
        service = FigureReadingService(session, settings)
        export = service.export_offline_html(ids['paper'])
        assert 'rId7' in open(export['runtime_path']).read()
        saved = open(export['runtime_path'], 'rb').read()
        asset.provenance = {**asset.provenance, 'media': {**asset.provenance['media'], 'sha256': 'b' * 64}}
        session.commit()
        with pytest.raises(ReadingGuideProjectionError, match='reading_guide_projection_incomplete'):
            service.export_offline_html(ids['paper'])
        assert open(export['runtime_path'], 'rb').read() == saved


def test_reads_do_not_autoflush_unrelated_pending_changes(guide_env):
    factory, settings, ids, _ = guide_env
    with factory() as session:
        paper = session.get(Paper, ids['paper']); paper.title = 'Uncommitted local edit'
        writes = []
        def capture(conn, cursor, sql, params, context, many):
            if sql.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')): writes.append(sql)
        event.listen(session.bind, 'before_cursor_execute', capture)
        try:
            FigureReadingService(session, settings).get_paper_reading_guide_data(ids['paper'])
            assert writes == []
        finally:
            event.remove(session.bind, 'before_cursor_execute', capture)


def test_symlink_escape_and_foreign_file_are_visible(guide_env):
    factory, settings, ids, _ = guide_env
    outside = settings.storage_root.parent / 'outside.png'; outside.write_bytes(PNG)
    (settings.storage_root / 'figures' / 'escape.png').symlink_to(outside)
    with factory() as session:
        asset = session.get(RebuildVisualAsset, ids['asset'])
        alien_paper = session.get(RebuildVisualAsset, ids['alien']).paper_id
        file = RebuildPaperFile(paper_id=alien_paper, role='si', storage_path='other.docx',
            original_filename='other.docx', sha256='a' * 64, file_size=123)
        session.add(file); session.flush()
        asset.file_id = file.id; asset.image_path = 'figures/escape.png'; session.commit()
        data = FigureReadingService(session, settings).get_paper_reading_guide_data(ids['paper'])
        codes = {i['code'] for i in data['projection_errors']}
        assert 'image_pointer_outside_storage' in codes
        assert 'source_file_not_found_or_wrong_paper' in codes
        assert len(data['figures']) == 3


def test_legacy_missing_image_and_empty_paper_remain_usable(guide_env):
    factory, settings, ids, _ = guide_env
    with factory() as session:
        legacy = session.get(PaperFigure, ids['legacy']); legacy.image_path = None
        empty = Paper(title='Empty legacy paper', pdf_path='')
        session.add(empty); session.commit()
        service = FigureReadingService(session, settings)
        assert '图片未内嵌或暂不可用' in service.render_offline_html(ids['paper'])
        data = service.get_paper_reading_guide_data(empty.id)
        assert data['sections'] == [] and data['figures'] == [] and data['projection_complete']
        assert '暂无解析章节' in service.render_offline_html(empty.id)


def test_curated_zh_reading_arrays_and_ooxml_evidence_render_without_mutation(guide_env):
    factory, settings, ids, _ = guide_env
    reading = {'summary_zh': 'Stored summary', 'detailed_explanation_zh': 'Stored detailed reading',
        'subfigures': [{'label': 'a', 'description': 'Stored panel text',
                       'methods': ['XRD', 'XPS'], 'findings': ['Original finding <value>']}],
        'evidence_locators': [{'page_basis': 'ooxml', 'page': None,
            'ooxml_locator': 'word/document.xml#body-child[1]/w:p[1]', 'quote': 'Stored source quote'}]}
    with factory() as session:
        asset = session.get(RebuildVisualAsset, ids['asset'])
        asset.provenance = {**asset.provenance, 'reading_explanation': reading}
        session.commit(); before = snapshot(session)
        service = FigureReadingService(session, settings)
        data = service.get_paper_reading_guide_data(ids['paper'])
        assert next(f for f in data['figures'] if f['figure_id'] == str(ids['asset']))['reading'] == reading
        page = service.render_offline_html(ids['paper'])
        assert 'XRD; XPS' in page and 'Original finding &lt;value&gt;' in page
        assert 'Stored detailed reading' in page and 'OOXML: word/document.xml' in page
        assert 'P.None' not in page
        assert snapshot(session) == before


def test_source_bound_section_metadata_not_lost(guide_env):
    factory, settings, ids, _ = guide_env
    with factory() as session:
        row = session.get(PaperSection, UUID(int=1))
        fields = ('section_title', 'section_type', 'text', 'page_start', 'page_end',
                  'section_level', 'section_number', 'parent_heading', 'heading_path')
        snapshot = {k: getattr(row, k) for k in fields}
        snapshot.update(file_id=str(ids['file']), source_sha256='a' * 64,
                        section_key='si:results', page_basis='ooxml', sources=[{'ooxml_locator': 'word/document.xml#body-child[1]/w:p[1]'}])
        session.add(AuditLog(paper_id=ids['paper'], action=EVENT, source=VERSION,
            target_type='paper_section', target_id=str(row.id),
            payload={'section': snapshot, 'content_sha256': _digest(snapshot)}))
        session.commit()
        data = FigureReadingService(session, settings).get_paper_reading_guide_data(ids['paper'])
        s = next(s for s in data['sections'] if s['id'] == str(row.id))
        assert s['provenance']['file_id'] == str(ids['file'])
        assert s['section_key'] == 'si:results' and s['provenance']['sources'] == snapshot['sources']
        assert s['reading_order']['basis'] == 'original_heading_number'
        html = FigureReadingService(session, settings).render_offline_html(ids['paper'])
        assert 'si:results' in html and 'original_heading_number' in html
