"""Focused regression coverage for spaced front-page ABSTRACT sources."""
import hashlib

import fitz
import pytest

from app.db.models import Paper, RebuildPaperFile
from app.services.reading_abstract_source import (pdf_spaced_front_abstract,
                                                  select_abstract)
from test_reading_assistance import base, reading_env
from test_rebuild_mutation_audit import audit_env

ABSTRACT = (
    'Spaced labels occur in real journal layouts and must remain source bounded. '
    'This study compares transition-metal sites under controlled electrochemical conditions. '
    'The measured and calculated evidence supports the reported trend, while the limitations '
    'of the tested material family remain explicit. No result is inferred beyond the supplied text. '
    'All observations remain within that stated scope.'
)


def spaced_pdf(boundary=None, following=None):
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 100), 'A B S T R A C T', fontsize=12)
    page.insert_textbox(fitz.Rect(72, 120, 523, 360), ABSTRACT, fontsize=9)
    body = next(block for block in page.get_text('dict')['blocks']
                if block['type'] == 0 and block['bbox'][0] == 72 and block['bbox'][1] > 110 and block['bbox'][1] < 200)
    next_y = body['bbox'][3] + 3
    if boundary:
        page.insert_textbox(fitz.Rect(72, next_y, 523, next_y + 22), boundary, fontsize=9)
        next_y += 24
    if following:
        page.insert_textbox(fitz.Rect(72, next_y, 523, next_y + 44), following, fontsize=9)
    raw = page.get_text()
    return document, page, raw


def documents(page, raw, sha='a' * 64, front=None):
    item = {'page': 1, 'text': raw}
    if front is not None:
        item['spaced_front_abstracts'] = front
    return [{'role': 'main', 'file_id': 'main-file', 'source_sha256': sha, 'pages': [item]}]


@pytest.mark.no_test_database
def test_spaced_heading_uses_exact_page_one_span_and_preserves_revision():
    document, page, raw = spaced_pdf()
    try:
        front = pdf_spaced_front_abstract(page, raw)
        assert len(front) == 1
        selected = select_abstract(None, documents(page, raw, front=front))
        source = selected['source']
        assert ' '.join(selected['text'].split()) == ABSTRACT
        assert selected['text'] == raw[source['start_char']:source['end_char']]
        assert source['kind'] == 'main_text' and source['physical_page'] == 1
        assert source['selection_method'] == 'spaced_abstract_heading_v1'
        assert source['text_sha256'] == hashlib.sha256(selected['text'].encode()).hexdigest()
    finally:
        document.close()


@pytest.mark.no_test_database
def test_spaced_heading_keeps_metadata_priority_and_rejects_competing_geometry():
    document, page, raw = spaced_pdf()
    try:
        front = pdf_spaced_front_abstract(page, raw)
        assert select_abstract('Metadata remains authoritative.', documents(page, raw, front=front))['text'] == 'Metadata remains authoritative.'
        ambiguous = documents(page, raw, front=front)
        ambiguous[0]['pages'].append({'page': 2, 'text': 'A B S T R A C T\nAnother label.\n'})
        assert not select_abstract(None, ambiguous)['text']
        ambiguous = documents(page, raw, front=front + front)
        assert not select_abstract(None, ambiguous)['text']
    finally:
        document.close()


@pytest.mark.no_test_database
def test_spaced_heading_rejects_rotated_page_and_stale_span():
    document, page, raw = spaced_pdf()
    try:
        front = pdf_spaced_front_abstract(page, raw)
        stale = [{**front[0], 'start_char': len(raw) + 1, 'end_char': len(raw) + 2}]
        assert not select_abstract(None, documents(page, raw, front=stale))['text']
        page.set_rotation(90)
        assert not pdf_spaced_front_abstract(page, raw)
    finally:
        document.close()


@pytest.mark.no_test_database
@pytest.mark.parametrize('boundary,following', [
    ('© 2024 Elsevier B.V. All rights reserved.', None),
    ('©2024 Elsevier B.V. All rights reserved.', None),
    ('1. Introduction', 'This section begins immediately after the abstract heading.'),
    ('1 Introduction', 'This section begins immediately after the abstract heading.'),
    ('1.Introduction', 'This section begins immediately after the abstract heading.'),
])
def test_spaced_heading_stops_at_immediate_copyright_or_numbered_introduction(boundary, following):
    document, page, raw = spaced_pdf(boundary=boundary, following=following)
    try:
        boundary_y = next(block['bbox'][3] for block in page.get_text('dict')['blocks']
                          if block['type'] == 0 and boundary in
                          ''.join(span['text'] for line in block.get('lines', []) for span in line['spans']))
        body_y = max(block['bbox'][3] for block in page.get_text('dict')['blocks']
                     if block['type'] == 0 and block['bbox'][3] < boundary_y)
        assert boundary_y - body_y <= 16
        front = pdf_spaced_front_abstract(page, raw)
        assert len(front) == 1
        selected = select_abstract(None, documents(page, raw, front=front))
        assert ' '.join(selected['text'].split()) == ABSTRACT
        assert boundary not in selected['text']
        if following:
            assert following not in selected['text']
        assert raw.index(boundary) >= front[0]['end_char']
    finally:
        document.close()


def test_context_and_formal_save_accept_only_server_verified_spaced_source(reading_env):
    env = reading_env
    with env['factory']() as session:
        file = session.get(RebuildPaperFile, env['file'])
        path = env['settings'].storage_root / file.storage_path
        document, page, _ = spaced_pdf()
        try:
            document.save(path)
        finally:
            document.close()
        file.role = 'main'
        file.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        file.file_size = path.stat().st_size
        paper = session.get(Paper, env['paper'])
        paper.abstract = None
        paper.reading_guide = {'research_background': 'retain me'}
        paper.comprehensive_analysis = {'retained_science': {'x': 42}}
        session.commit()

    issued = env['client'].post(base(env) + '/jobs', json={'request_id': 'spaced-abstract', 'scope': 'abstract_only'})
    assert issued.status_code == 200, issued.text
    job = issued.json()
    context = env['client'].get(base(env) + '/context').json()
    candidate = context['abstract_for_translation']
    assert ' '.join(candidate['text'].split()) == ABSTRACT
    assert candidate['source']['physical_page'] == 1
    assert 'spaced_front_abstracts' not in context['documents'][0]['pages'][0]

    payload = {
        'save_mode': 'abstract_only',
        'request_id': 'spaced-abstract-save',
        'job_id': job['job_id'],
        'input_revision': context['input_revision'],
        'abstract_source_revision': candidate['revision'],
        'abstract_zh': '该研究在受控电化学条件下比较过渡金属位点，并保留证据与适用限制。',
    }
    invalid = {**payload, 'abstract_source_revision': '0' * 64}
    unverified = env['client'].post(base(env), json=invalid)
    assert unverified.status_code == 200
    unverified_read = env['client'].get(base(env)).json()['abstract_translation']
    assert unverified_read['source_status'] == 'changed_since_input'
    assert unverified_read['source'] is None
    payload['request_id'] = 'spaced-abstract-save-verified'
    saved = env['client'].post(base(env), json=payload)
    assert saved.status_code == 200, saved.text
    readback = env['client'].get(base(env)).json()
    assert readback['abstract_translation']['source'] == candidate['source']
    with env['factory']() as session:
        paper = session.get(Paper, env['paper'])
        assert paper.abstract is None
        assert paper.reading_guide['research_background'] == 'retain me'
        assert paper.comprehensive_analysis['retained_science'] == {'x': 42}
