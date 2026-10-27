"""Real HTTP saves on an isolated fixture preserve old scientific artifacts."""
import copy
import hashlib

import pytest
from app.db.models import Paper, RebuildPaperFile
from test_reading_assistance import reading_env, base
from test_rebuild_mutation_audit import audit_env
from test_reading_abstract_recovery import patch, start_patch, science
from test_reading_abstract_numbered import make_pdf, side_pdf


@pytest.mark.parametrize('layout,method', [('numbered', 'numbered_abstract_page_v1'), ('side', 'shaded_abstract_box_v1')])
def test_new_source_saves_with_original_job_and_preserves_legacy(reading_env, layout, method):
    e = reading_env
    with e['factory']() as session:
        record = session.get(RebuildPaperFile, e['file'])
        path = e['settings'].storage_root / record.storage_path
        with (make_pdf() if layout == 'numbered' else side_pdf()) as pdf:
            pdf.save(path)
        record.role = 'main'; record.preferred = True
        record.sha256 = hashlib.sha256(path.read_bytes()).hexdigest(); record.file_size = path.stat().st_size
        paper = session.get(Paper, e['paper']); paper.abstract = None
        paper.reading_guide = {'legacy': {'unchanged': True}, 'beginner_zh': {'one_sentence': '已有导读'}}
        old = copy.deepcopy(paper.reading_guide); session.commit()
    before = science(e)
    context = e['client'].get(base(e) + '/context').json()
    source = context['abstract_for_translation']['source']
    assert source['selection_method'] == method
    job = start_patch(e)
    payload = patch(e, job['job_id'])
    wrong = dict(payload, abstract_source_revision='0' * 64)
    assert e['client'].post(base(e), json=wrong).status_code == 409
    assert e['client'].post(base(e), json=payload).status_code == 200
    assert e['client'].post(base(e), json=payload).json()['replayed']
    saved = e['client'].get(base(e)).json()
    assert saved['abstract_translation']['source'] == source
    assert saved['abstract_translation']['original_text'] == context['abstract_for_translation']['text']
    with e['factory']() as session:
        guide = session.get(Paper, e['paper']).reading_guide
        assert {k:v for k,v in guide.items() if k != 'abstract_translation'} == old
    assert science(e) == before
