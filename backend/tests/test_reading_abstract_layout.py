"""Source geometry and save contracts for unlabelled shaded abstracts."""
import copy
import hashlib
from uuid import uuid4

import fitz
import pytest
from app.db.models import Paper, RebuildPaperFile, WorkflowJob
from app.services.reading_abstract_source import pdf_abstract_boxes, select_abstract
from test_reading_abstract_recovery import ABSTRACT, NOTICE, patch, science, source_pdf, start_patch
from test_reading_assistance import reading_env, base
from test_rebuild_mutation_audit import audit_env

TEXT = (ABSTRACT + ' ' + ABSTRACT).replace('under the tested conditions', 'within the tested scope')


def draw_layout(page, *, shade=True, content=TEXT, wrong_column=False, title=True):
    if title:
        page.insert_text((50, 70), 'A study of coordination and sulfur conversion', fontsize=19)
        page.insert_text((50, 110), 'Research authors', fontsize=13)
    # Deliberately draw the right body column first, as in mixed-order PDFs.
    page.insert_textbox(fitz.Rect(395, 160, 565, 650), 'Continuation of the introduction. ' * 30, fontsize=9)
    rect = fitz.Rect(45, 165, 375, 292)
    if shade:
        page.draw_rect(rect, color=None, fill=(.82, .82, .82), overlay=False)
    page.insert_textbox(fitz.Rect(52, 172, 368, 290), content, fontsize=10, fontname='hebo')
    x = 395 if wrong_column else 50
    page.insert_text((x, 320), '1. Introduction', fontsize=11)
    page.insert_textbox(fitz.Rect(x, 335, min(x + 240, 590), 500),
                        'Introduction body and background. ' * 10, fontsize=9, fontname='tiro')


def documents(pdf):
    pages = []
    for index, page in enumerate(pdf):
        text = page.get_text()
        pages.append({'page': index + 1, 'text': text, 'abstract_boxes': pdf_abstract_boxes(page, text)})
    return [{'role': 'main', 'file_id': str(uuid4()), 'source_sha256': 'a'*64, 'pages': pages}]


@pytest.mark.no_test_database
def test_box_in_mixed_text_order_has_exact_spans_pixels_and_revision():
    with fitz.open() as pdf:
        draw_layout(pdf.new_page(width=600, height=800))
        docs = documents(pdf)
        result = select_abstract(None, docs)
    source = result['source']
    assert source and source['selection_method'] == 'shaded_abstract_box_v1'
    assert source['kind'] == 'main_text' and source['physical_page'] == 1
    assert source['layout_evidence']['title_bbox'][1] < 100
    assert len(source['layout_evidence']['background_samples_rgb']) == 6
    assert docs[0]['pages'][0]['text'][source['start_char']:source['end_char']] == result['text']
    assert hashlib.sha256(result['text'].encode()).hexdigest() == source['text_sha256']
    assert len(result['revision']) == 64 and result == select_abstract(None, docs)
    assert 'Continuation of the introduction' not in result['text']


@pytest.mark.no_test_database
@pytest.mark.parametrize('options', [
    {'shade': False}, {'wrong_column': True}, {'title': False},
    {'content': NOTICE * 3}, {'content': 'Publisher disclaimer. ' * 30},
    {'content': '1. Introduction\n' + TEXT}, {'content': 'Figure 1. ' + TEXT},
])
def test_plain_prose_notices_and_wrong_geometry_are_not_inferred(options):
    with fitz.open() as pdf:
        draw_layout(pdf.new_page(width=600, height=800), **options)
        assert not select_abstract(None, documents(pdf))['text']


@pytest.mark.no_test_database
def test_competing_boxes_labelled_ambiguity_and_multiple_mains_are_rejected():
    with fitz.open() as pdf:
        draw_layout(pdf.new_page(width=600, height=800))
        draw_layout(pdf.new_page(width=600, height=800))
        docs = documents(pdf)
    assert not select_abstract(None, docs)['text']
    docs[0]['pages'] = docs[0]['pages'][:1]
    assert select_abstract(None, docs)['text']
    assert not select_abstract(None, docs + copy.deepcopy(docs))['text']
    docs[0]['pages'][0]['text'] += '\nAbstract:\nunclear competing source'
    assert not select_abstract(None, docs)['text']
    assert select_abstract(ABSTRACT, docs)['source']['kind'] == 'metadata'


def register_layout(e):
    with e['factory']() as session:
        record = session.get(RebuildPaperFile, e['file'])
        path = e['settings'].storage_root / record.storage_path
        with fitz.open() as pdf:
            draw_layout(pdf.new_page(width=600, height=800))
            pdf.save(path)
        record.role = 'main'; record.preferred = True
        record.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        record.file_size = path.stat().st_size
        paper = session.get(Paper, e['paper']); paper.abstract = None
        paper.reading_guide = {'beginner_zh': {'one_sentence': '已有导读'}, 'legacy': 'retain'}
        session.commit()


def test_context_and_existing_abstract_only_save_revalidate_source_and_preserve_data(reading_env):
    e = reading_env; register_layout(e)
    before = science(e)
    with e['factory']() as session:
        old_guide = copy.deepcopy(session.get(Paper, e['paper']).reading_guide)
    ctx = e['client'].get(base(e) + '/context').json()
    source = ctx['abstract_for_translation']['source']
    assert source['selection_method'] == 'shaded_abstract_box_v1'
    assert source['file_id'] == str(e['file'])
    assert len(source['source_sha256']) == len(ctx['abstract_for_translation']['revision']) == 64
    job = start_patch(e)
    payload = patch(e, job['job_id'])
    forged = {**payload, 'abstract_source_revision': '0'*64}
    assert e['client'].post(base(e), json=forged).status_code == 409
    assert e['client'].post(base(e), json=payload).status_code == 200
    read = e['client'].get(base(e)).json()
    assert read['abstract_zh'] == payload['abstract_zh']
    assert read['abstract_translation']['source'] == source
    with e['factory']() as session:
        paper = session.get(Paper, e['paper'])
        assert paper.abstract is None
        assert {k:v for k,v in paper.reading_guide.items() if k != 'abstract_translation'} == old_guide
    assert science(e) == before
    with e['factory']() as session:
        record = session.get(RebuildPaperFile, e['file'])
        path = e['settings'].storage_root / record.storage_path
    path.write_bytes(path.read_bytes() + b'\nsource changed')
    assert not e['client'].get(base(e) + '/context').json()['abstract_for_translation']['text']


@pytest.mark.no_test_database
def test_later_introduction_callout_and_rotated_or_oversized_pages_are_rejected():
    with fitz.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        draw_layout(page)
        docs = documents(pdf)
        assert select_abstract(None, docs)['text']
        docs[0]['pages'].append({'page': 2, 'text': '1. Introduction\nAnother introduction'})
        assert not select_abstract(None, docs)['text']
        page.set_rotation(90)
        assert not pdf_abstract_boxes(page, page.get_text())
    with fitz.open() as pdf:
        page = pdf.new_page(width=2000, height=3000)
        assert not pdf_abstract_boxes(page, '')


def test_optional_layout_failure_preserves_labelled_abstract_and_readable_text(reading_env, monkeypatch):
    e = reading_env; source_pdf(e)
    def unavailable(*args):
        raise ValueError('unsupported optional layout')
    monkeypatch.setattr('app.services.reading_abstract_source.pdf_abstract_boxes', unavailable)
    response = e['client'].get(base(e) + '/context')
    assert response.status_code == 200
    context = response.json()
    assert context['documents'][0]['complete_text']
    assert context['abstract_for_translation']['text']
    assert context['abstract_for_translation']['source']['start_marker'].startswith('Abstract')
