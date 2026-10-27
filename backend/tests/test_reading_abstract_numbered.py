"""Reject ambiguous source geometry; recover only numbered standalone abstracts."""
import copy
import hashlib
import textwrap
from uuid import uuid4

import fitz
import pytest

from app.services.reading_abstract_source import pdf_numbered_abstract, select_abstract

pytestmark = pytest.mark.no_test_database
TEXT = ('Lithium sulfur batteries face slow conversion. We compare coordinated catalysts '
        'under controlled conditions and report improved conversion within the tested scope. '
        'The results provide evidence of a specific catalytic mechanism and its limits. '
        'These conclusions apply only to the measured conditions and require further investigation in other substantially different electrochemical and operating systems.')


def make_pdf(*, broken_number=False, intro=True, extra=False):
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((90, 85), 'Abstract', fontsize=12, fontname='hebo')
    page.insert_text((60, 85), '24', fontsize=10)
    chunks = textwrap.wrap(TEXT, 70)
    for i, chunk in enumerate(chunks):
        y = 115 + i * 30
        page.insert_text((90, y), chunk, fontsize=10)
        page.insert_text((60, y), str(25 + i + (1 if broken_number and i == 2 else 0)), fontsize=10)
    page.insert_text((50, 20), 'Journal header', fontsize=8)
    page.insert_text((180, 780), 'Publisher copyright notice', fontsize=8)
    page.insert_text((570, 680), 'Accepted Manuscript', fontsize=30, rotate=90)
    if extra:
        page.insert_text((90, 350), 'Unexpected section', fontsize=12)
    following = pdf.new_page(width=600, height=800)
    following.insert_text((90, 85), '1. Introduction' if intro else 'Methods', fontsize=12)
    following.insert_text((90, 115), 'Introduction body text.', fontsize=10)
    return pdf


def context(pdf):
    page = pdf[0]; raw = page.get_text()
    return [{'role': 'main', 'file_id': str(uuid4()), 'source_sha256': 'a' * 64,
        'pages': [{'page': 1, 'text': raw,
                   'numbered_abstract_sections': pdf_numbered_abstract(page, raw, pdf[1])},
                  {'page': 2, 'text': pdf[1].get_text()}]}]


def test_numbered_page_keeps_exact_spans_and_excludes_furniture():
    with make_pdf() as pdf:
        docs = context(pdf)
    selected = select_abstract(None, docs)
    source = selected['source']
    assert source['selection_method'] == 'numbered_abstract_page_v1'
    assert source['layout_evidence']['line_numbers'] == list(range(24, 25 + len(source['spans'])))
    assert len(source['spans']) >= 5
    assert selected['text'] == '\n'.join(docs[0]['pages'][0]['text'][s['start_char']:s['end_char']] for s in source['spans'])
    assert hashlib.sha256(selected['text'].encode()).hexdigest() == source['text_sha256']
    assert all(s['physical_page'] == 1 for s in source['spans'])
    assert 'Accepted' not in selected['text'] and 'copyright' not in selected['text']
    assert not any(line.strip().isdigit() for line in selected['text'].splitlines())
    assert select_abstract('Existing metadata', docs)['text'] == 'Existing metadata'
    docs[0]['pages'].append({'page': 3, 'text': 'Abstract\ncompeting paragraph'})
    assert not select_abstract(None, docs)['text']


@pytest.mark.parametrize('options', [{'broken_number': True}, {'intro': False}, {'extra': True}])
def test_unknown_boundary_or_numbering_refuses_recovery(options):
    with make_pdf(**options) as pdf:
        assert not pdf_numbered_abstract(pdf[0], pdf[0].get_text(), pdf[1])


def test_rotation_and_multiple_mains_refuse_recovery():
    with make_pdf() as pdf:
        docs = context(pdf)
        assert not select_abstract(None, docs + copy.deepcopy(docs))['text']
        pdf[0].set_rotation(90)
        assert not pdf_numbered_abstract(pdf[0], pdf[0].get_text(), pdf[1])


def side_pdf(shade=True, aligned=True):
    pdf = fitz.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((50, 70), 'A study of coordination and sulfur conversion', fontsize=19)
    paragraph = ' '.join([TEXT, TEXT])
    if shade:
        page.draw_rect(fitz.Rect(45, 165, 375, 405), color=None, fill=(.82, .82, .82), overlay=False)
    page.insert_textbox(fitz.Rect(52, 172, 368, 400), paragraph, fontsize=10, fontname='hebo')
    page.insert_text((395, 180 if aligned else 140), '1. Introduction', fontsize=11)
    page.insert_textbox(fitz.Rect(395, 195, 565, 410), 'This introduction describes detailed mechanisms and background evidence. ' * 7, fontsize=9)
    return pdf


@pytest.mark.parametrize('shade,aligned', [(True, True), (False, True), (True, False)])
def test_adjacent_shaded_box_requires_pixels_and_aligned_columns(shade, aligned):
    from app.services.reading_abstract_source import pdf_abstract_boxes
    with side_pdf(shade, aligned) as pdf:
        raw = pdf[0].get_text(); boxes = pdf_abstract_boxes(pdf[0], raw)
        assert bool(boxes) == (shade and aligned)
        if boxes:
            assert raw[boxes[0]['start_char']:boxes[0]['end_char']].startswith('Lithium sulfur')
