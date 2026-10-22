"""Guide API/export against synthetic isolated PostgreSQL and file fixtures."""
import base64
import hashlib
import io
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
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
from app.services.reading_guide_projection import ReadingGuideProjectionError, validate_guide_image_bytes
from app.services.rebuild_sections import EVENT, VERSION, _digest, list_sections


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC')


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


@pytest.mark.parametrize('binding', ['missing', 'foreign'])
def test_section_invalid_file_binding_retained_and_blocks_publication(guide_env, binding):
    factory, settings, ids, client = guide_env
    reference = str(UUID(int=999))
    with factory() as session:
        if binding == 'foreign':
            alien = session.get(RebuildVisualAsset, ids['alien'])
            file = RebuildPaperFile(paper_id=alien.paper_id, role='si', storage_path='alien.docx',
                original_filename='alien.docx', sha256='a' * 64, file_size=1)
            session.add(file); session.flush(); reference = str(file.id)
        row = session.get(PaperSection, UUID(int=1))
        fields = ('section_title', 'section_type', 'text', 'page_start', 'page_end',
                  'section_level', 'section_number', 'parent_heading', 'heading_path')
        source = {k: getattr(row, k) for k in fields}
        source.update(file_id=reference, section_key='si:results', source_sha256='a' * 64,
                      page_basis='ooxml', sources=[{'ooxml_locator': 'word/document.xml#body-child[1]/w:p[1]'}])
        session.add(AuditLog(paper_id=ids['paper'], action=EVENT, source=VERSION,
            target_type='paper_section', target_id=str(row.id),
            payload={'section': source, 'content_sha256': _digest(source)}))
        session.commit(); before = snapshot(session)
        data = FigureReadingService(session, settings).get_paper_reading_guide_data(ids['paper'])
        section = next(s for s in data['sections'] if s['id'] == str(row.id))
        assert section['provenance']['file_id'] == reference
        assert section['provenance']['sources'] == source['sources']
        assert section['provenance']['download_url'] is None
        assert next(s for s in data['sections'] if s['id'] == str(UUID(int=2)))['provenance'] is None
        assert not data['projection_complete']
        assert snapshot(session) == before
    issue = {'section_id': str(UUID(int=1)), 'code': 'source_file_not_found_or_wrong_paper'}
    assert issue in client.get(f'/api/papers/{ids["paper"]}/reading-guide').json()['projection_errors']
    response = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert response.status_code == 409 and issue in response.json()['detail']['issues']
    assert not (settings.storage_root / 'exports').exists()


@pytest.mark.parametrize('declared_hash', [False, True])
@pytest.mark.parametrize('raw', [b'not an image', PNG[:50]], ids=['not-image', 'truncated-png'])
def test_undecodable_curated_image_blocks_api_export_and_preserves_last_export(guide_env, declared_hash, raw):
    factory, settings, ids, client = guide_env
    with factory() as session:
        service = FigureReadingService(session, settings)
        export = service.export_offline_html(ids['paper'])
        path = Path(export['runtime_path'])
        saved = path.read_bytes()
        (settings.storage_root / 'figures' / 'invalid.png').write_bytes(raw)
        asset = session.get(RebuildVisualAsset, ids['asset'])
        asset.image_path = 'figures/invalid.png'
        asset.provenance = {'media': {'sha256': hashlib.sha256(raw).hexdigest(),
            'zip_member': 'word/media/image1.png'}} if declared_hash else {}
        session.commit(); before = snapshot(session)
        data = service.get_paper_reading_guide_data(ids['paper'])
        assert not data['projection_complete']
        assert next(a for a in data['figures'] if a.get('asset_id') == str(asset.id))['image_status'] == 'unavailable'
        with pytest.raises(ReadingGuideProjectionError):
            service.render_offline_html(ids['paper'])
        assert snapshot(session) == before
    issue = {'asset_id': str(ids['asset']), 'code': 'image_undecodable'}
    assert issue in client.get(f'/api/papers/{ids["paper"]}/reading-guide').json()['projection_errors']
    response = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert response.status_code == 409 and issue in response.json()['detail']['issues']
    assert path.read_bytes() == saved


def test_image_rechecked_at_embedding_when_bytes_change_after_projection(guide_env, monkeypatch):
    factory, settings, ids, _ = guide_env
    with factory() as session:
        service = FigureReadingService(session, settings)
        original = service.get_paper_reading_guide_data
        def change_after_projection(paper_id):
            data = original(paper_id)
            assert data['projection_complete']
            (settings.storage_root / 'figures' / 'stored.png').write_bytes(b'not an image')
            return data
        monkeypatch.setattr(service, 'get_paper_reading_guide_data', change_after_projection)
        with pytest.raises(ReadingGuideProjectionError) as caught:
            service.export_offline_html(ids['paper'])
        assert {'asset_id': str(ids['asset']), 'code': 'image_undecodable'} in caught.value.issues
        assert not (settings.storage_root / 'exports').exists()


@pytest.mark.no_test_database
@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'GIF'])
def test_image_decoder_accepts_valid_curated_formats(format):
    raw = io.BytesIO()
    with Image.open(io.BytesIO(PNG)) as image:
        image.convert('RGB').save(raw, format=format)
    validate_guide_image_bytes(raw.getvalue())


def animation_bytes(frames=3, size=(8, 8), format='GIF', **kwargs):
    images = [Image.new('RGB', size, (i % 256, i // 256, 0)) for i in range(frames)]
    raw = io.BytesIO()
    images[0].save(raw, format=format, save_all=True, append_images=images[1:],
                   duration=20, loop=0, **kwargs)
    return raw.getvalue()


@pytest.mark.no_test_database
@pytest.mark.parametrize('format', ['GIF', 'PNG'])
def test_small_animation_within_all_budgets(format):
    validate_guide_image_bytes(animation_bytes(format=format))


@pytest.mark.no_test_database
@pytest.mark.parametrize('budget,boundary,raw', [
    ('GUIDE_IMAGE_MAX_ENCODED_BYTES', len(PNG), PNG),
    ('GUIDE_IMAGE_MAX_DIMENSION', 8, animation_bytes(size=(8, 1))),
    ('GUIDE_IMAGE_MAX_FRAME_PIXELS', 64, animation_bytes()),
    ('GUIDE_IMAGE_MAX_FRAMES', 3, animation_bytes()),
    ('GUIDE_IMAGE_MAX_TOTAL_PIXELS', 192, animation_bytes()),
    ('GUIDE_IMAGE_MAX_FRAMES', 3, animation_bytes(format='PNG')),
    ('GUIDE_IMAGE_MAX_TOTAL_PIXELS', 192, animation_bytes(format='PNG')),
])
def test_image_budgets_inclusive_boundary_and_reject_before_decode(monkeypatch, budget, boundary, raw):
    from app.services import reading_guide_projection as projection
    monkeypatch.setattr(projection, budget, boundary)
    validate_guide_image_bytes(raw)
    monkeypatch.setattr(projection, budget, boundary - 1)
    # Rejection must precede even the first load/verify; using Pillow n_frames
    # then seek across GIF frames would inadvertently decode prior frames.
    def forbidden(*args, **kwargs):
        pytest.fail('over-budget image reached decoding/verification')
    from PIL import GifImagePlugin, PngImagePlugin
    for cls in (GifImagePlugin.GifImageFile, PngImagePlugin.PngImageFile):
        monkeypatch.setattr(cls, 'load', forbidden)
        monkeypatch.setattr(cls, 'verify', forbidden)
    with pytest.raises(ValueError, match='^image_resource_limit_exceeded$'):
        validate_guide_image_bytes(raw)


def test_600_frame_resource_probe_blocks_publication_preserves_export(guide_env):
    factory, settings, ids, client = guide_env
    raw = animation_bytes(600, (512, 512))
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (512, 512) and image.n_frames == 600
    with factory() as session:
        service = FigureReadingService(session, settings)
        exported = service.export_offline_html(ids['paper'])
        path = Path(exported['runtime_path']); saved = path.read_bytes()
        (settings.storage_root / 'figures' / 'over-budget.gif').write_bytes(raw)
        asset = session.get(RebuildVisualAsset, ids['asset'])
        asset.image_path = 'figures/over-budget.gif'; session.commit()
        before = snapshot(session)
        data = service.get_paper_reading_guide_data(ids['paper'])
        assert not data['projection_complete']
        assert snapshot(session) == before
    issue = {'asset_id': str(ids['asset']), 'code': 'image_resource_limit_exceeded'}
    assert issue in client.get(f'/api/papers/{ids["paper"]}/reading-guide').json()['projection_errors']
    response = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert response.status_code == 409 and issue in response.json()['detail']['issues']
    assert path.read_bytes() == saved


@pytest.mark.parametrize('changed_bytes', [animation_bytes(129), b'x' * (16 * 1024 * 1024 + 1)],
                         ids=['frame-count', 'encoded-bytes'])
def test_budget_rechecked_at_embedding_preserves_prior_export(guide_env, monkeypatch, changed_bytes):
    factory, settings, ids, _ = guide_env
    with factory() as session:
        service = FigureReadingService(session, settings)
        exported = service.export_offline_html(ids['paper'])
        path = Path(exported['runtime_path']); saved = path.read_bytes()
        original = service.get_paper_reading_guide_data
        def change_after_projection(paper_id):
            data = original(paper_id)
            assert data['projection_complete']
            (settings.storage_root / 'figures' / 'stored.png').write_bytes(changed_bytes)
            return data
        monkeypatch.setattr(service, 'get_paper_reading_guide_data', change_after_projection)
        with pytest.raises(ReadingGuideProjectionError) as caught:
            service.export_offline_html(ids['paper'])
        assert {'asset_id': str(ids['asset']), 'code': 'image_resource_limit_exceeded'} in caught.value.issues
        assert path.read_bytes() == saved


@pytest.mark.no_test_database
def test_apng_default_image_counts_toward_total_budget(monkeypatch):
    from app.services import reading_guide_projection as projection
    raw = animation_bytes(format='PNG', default_image=True)
    validate_guide_image_bytes(raw)
    monkeypatch.setattr(projection, 'GUIDE_IMAGE_MAX_TOTAL_PIXELS', 192)
    validate_guide_image_bytes(raw)
    monkeypatch.setattr(projection, 'GUIDE_IMAGE_MAX_TOTAL_PIXELS', 191)
    with pytest.raises(ValueError, match='^image_resource_limit_exceeded$'):
        validate_guide_image_bytes(raw)


@pytest.mark.no_test_database
def test_gif_descriptor_expansion_rejected_before_frame_decode(monkeypatch):
    from app.services import reading_guide_projection as projection
    from PIL import GifImagePlugin
    raw = bytearray(animation_bytes())
    # First descriptor is already read by Pillow on open. Change the SECOND
    # descriptor: its extent must be budgeted without seeking to its pixels.
    with Image.open(io.BytesIO(raw)) as image:
        image.seek(1)
        start = image.tile[0][2]
    # This synthetic palette contains only 0, 1 and 2, so the last separator
    # before Pillow's compressed-data offset identifies the second descriptor.
    descriptor = raw.rfind(b'\x2c', 0, start)
    assert descriptor > 0 and raw[descriptor + 5:descriptor + 7] == b'\x08\x00'
    raw[descriptor + 1:descriptor + 3] = (8).to_bytes(2, 'little')  # x offset
    monkeypatch.setattr(projection, 'GUIDE_IMAGE_MAX_DIMENSION', 8)
    def forbidden(*args, **kwargs):
        pytest.fail('expanded GIF reached decoding')
    monkeypatch.setattr(GifImagePlugin.GifImageFile, 'load', forbidden)
    with pytest.raises(ValueError, match='^image_resource_limit_exceeded$'):
        validate_guide_image_bytes(bytes(raw))


def test_small_animation_embeds_successfully(guide_env):
    factory, settings, ids, client = guide_env
    raw = animation_bytes()
    with factory() as session:
        asset = session.get(RebuildVisualAsset, ids['asset'])
        asset.image_path = 'figures/small.gif'
        (settings.storage_root / asset.image_path).write_bytes(raw)
        session.commit()
    data = client.get(f'/api/papers/{ids["paper"]}/reading-guide').json()
    assert data['projection_complete']
    response = client.post(f'/api/papers/{ids["paper"]}/reading-guide/export-html')
    assert response.status_code == 200
    assert 'data:image/gif;base64,' + base64.b64encode(raw).decode() in response.text
