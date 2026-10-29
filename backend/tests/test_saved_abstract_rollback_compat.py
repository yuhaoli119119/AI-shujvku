"""Old-code rollback reads only intact, source-verified saved abstracts."""
import copy
import hashlib
from types import SimpleNamespace
import pytest
from app.services.reading_abstract_source import revision
from app.services.reading_assistance import _saved_abstract_fallback

pytestmark = pytest.mark.no_test_database


def fixture(method='unlabelled_front_abstract_v1'):
    raw = 'Author\n' + 'This is the complete, source-bound abstract. ' * 4 + '\nIntroduction'
    start = raw.index('This'); end = raw.index('\nIntroduction'); text = raw[start:end]
    source = {'kind':'main_text','file_id':'file-1','source_sha256':'a'*64,
              'physical_page':1,'start_char':start,'end_char':end,'selection_method':method,
              'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
    zh = '已经保存的中文摘要。'
    saved = {'original_text':text,'source':source,'source_status':'verified',
             'source_revision':revision({'text':text,'source':source}),
             'translation_sha256':hashlib.sha256(zh.encode()).hexdigest()}
    paper = SimpleNamespace(reading_guide={'abstract_translation':saved},
                            comprehensive_analysis={'abstract_zh':zh})
    docs = [{'role':'main','file_id':'file-1','source_sha256':'a'*64,'pages':[{'page':1,'text':raw}]}]
    selected = {'text':'','source':None,'revision':None,'missing_reason':'pending'}
    return paper,docs,selected


@pytest.mark.parametrize('method', ['unlabelled_front_abstract_v1','spaced_abstract_heading_v1'])
def test_saved_new_layout_is_exactly_reconstructed(method):
    paper, docs, missing = fixture(method); before = copy.deepcopy((paper.reading_guide,docs))
    got = _saved_abstract_fallback(paper,docs,missing)
    assert got['source'] == paper.reading_guide['abstract_translation']['source']
    assert got['revision'] == paper.reading_guide['abstract_translation']['source_revision']
    assert got['text'] == paper.reading_guide['abstract_translation']['original_text']
    assert (paper.reading_guide,docs) == before
    already = {'text':'priority metadata','source':{'kind':'metadata'}}
    assert _saved_abstract_fallback(paper,docs,already) == already


@pytest.mark.parametrize('change', ['status','translation','revision','source_hash','file_hash','file_id','span','missing_page','second_main','unrelated_method','text'])
def test_changed_or_unverified_source_stays_missing(change):
    paper,docs,missing=fixture();saved=paper.reading_guide['abstract_translation'];source=saved['source']
    if change=='status': saved['source_status']='changed_since_input'
    elif change=='translation': paper.comprehensive_analysis['abstract_zh']='已改变的译文'
    elif change=='revision': saved['source_revision']='0'*64
    elif change=='source_hash': source['text_sha256']='0'*64
    elif change=='file_hash': docs[0]['source_sha256']='0'*64
    elif change=='file_id': docs[0]['file_id']='another'
    elif change=='span': source['end_char']-=1; saved['source_revision']=revision({'text':saved['original_text'],'source':source})
    elif change=='missing_page': docs[0]['pages']=[]
    elif change=='second_main': docs.append(copy.deepcopy(docs[0]))
    elif change=='unrelated_method': source['selection_method']='some_other_method'; saved['source_revision']=revision({'text':saved['original_text'],'source':source})
    elif change=='text': docs[0]['pages'][0]['text']=docs[0]['pages'][0]['text'].replace('complete','incorrect')
    assert _saved_abstract_fallback(paper,docs,missing)==missing


def test_cross_page_heading_source_is_supported_only_with_verified_span():
    paper,docs,missing=fixture();saved=paper.reading_guide['abstract_translation'];source=saved['source']
    del source['selection_method'];docs[0]['pages'].append({'page': 2, 'text': 'Abstract\n'});source['heading_location']={'physical_page':2,'start_char':0,'end_char':8}
    saved['source_revision']=revision({'text':saved['original_text'],'source':source})
    assert _saved_abstract_fallback(paper,docs,missing)['text']==saved['original_text']


def test_cross_page_heading_must_match_actual_source():
    paper,docs,missing=fixture();saved=paper.reading_guide['abstract_translation'];source=saved['source']
    del source['selection_method'];source['heading_location']={'physical_page':1,'start_char':0,'end_char':6}
    saved['source_revision']=revision({'text':saved['original_text'],'source':source})
    assert _saved_abstract_fallback(paper,docs,missing)==missing
