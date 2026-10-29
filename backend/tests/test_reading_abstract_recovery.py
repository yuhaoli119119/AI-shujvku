import copy
import hashlib
import json
from uuid import uuid4

import fitz
import pytest
from sqlalchemy import select, func, text
from app.db.models import Paper, RebuildPaperFile, WorkflowJob, AuditLog
from app.services import reading_assistance as service
from app.services.reading_abstract_source import select_abstract
from app.schemas.reading_assistance import ReadingAssistanceSave
from test_reading_assistance import reading_env, base, body, create
from test_rebuild_mutation_audit import row_body, audit_env

ABSTRACT='Shuttle effects limit lithium sulfur batteries. We compare Fe and nitrogen coordination using density functional theory. The models suggest stronger binding under the tested conditions. No new experimental capacity is claimed.'


def source_pdf(e, abstract=True, end=True):
    with e['factory']() as s:
        f=s.get(RebuildPaperFile,e['file']);path=e['settings'].storage_root/f.storage_path
        with fitz.open() as doc:
            doc.new_page().insert_text((30,40),'Article cover sheet')
            page=doc.new_page()
            page.insert_textbox(fitz.Rect(30,30,550,650),'Paper title\nAbstract: '+ABSTRACT+'\n'+('Keywords: batteries\n1. Introduction\nBody text' if end else '') if abstract else 'This paper has no clear abstract boundaries.\n'+ABSTRACT,fontsize=10)
            doc.save(path)
        f.role='main';f.preferred=True;f.sha256=hashlib.sha256(path.read_bytes()).hexdigest();f.file_size=path.stat().st_size
        p=s.get(Paper,e['paper']);p.abstract=None
        p.reading_guide={'legacy':'retain','beginner_zh':{'version':'beginner_zh_v1','one_sentence':'原有导读','guide':{'innovation':'旧结论'},'content_scope':'main_text','missing':['旧摘要缺项','未裁决科学冲突']}}
        s.commit()


def patch(e,jid):
    ctx=e['client'].get(base(e)+'/context').json()
    return {'save_mode':'abstract_only','request_id':'abstract-save-'+('x'*80),'job_id':jid,'input_revision':ctx['input_revision'],
            'abstract_source_revision':ctx['abstract_for_translation']['revision'],'abstract_zh':'穿梭效应限制锂硫电池；本文用密度泛函理论比较配位，模型表明在所测条件下结合增强，未声称新的实验容量。'}


def start_patch(e,rid='abstract-generate'):
    r=e['client'].post(base(e)+'/jobs',json={'request_id':rid,'scope':'abstract_only'});assert r.status_code==200,r.text;return r.json()


def science(e):
    with e['factory']() as s:
        return {table:s.execute(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]') FROM {table} t WHERE paper_id=:pid"),{'pid':e['paper']}).scalar_one() for table in ['rebuild_data_rows','rebuild_data_values','rebuild_value_sources','rebuild_visual_assets','paper_sections','rebuild_paper_files']}


def test_fallback_translation_saved_replayed_preserves_existing_guide_english_science(reading_env):
    e=reading_env;source_pdf(e)
    r=e['client'].post('/api/rebuild/rows/import',json=row_body(e));assert r.status_code==200,r.text
    before=science(e)
    with e['factory']() as s:old=copy.deepcopy(s.get(Paper,e['paper']).reading_guide)
    ctx=e['client'].get(base(e)+'/context').json();candidate=ctx['abstract_for_translation'];source=candidate['source']
    assert ctx['abstract_original']=='' and source['kind']=='main_text' and source['physical_page']==2
    raw=ctx['documents'][0]['pages'][1]['text'];assert raw[source['start_char']:source['end_char']]==candidate['text']
    assert hashlib.sha256(candidate['text'].encode()).hexdigest()==source['text_sha256']
    j=start_patch(e);assert j['scope']=='abstract_only'
    with e['factory']() as s:
        prompt=s.get(WorkflowJob,j['job_id']).payload['dispatch_prompt'];assert '不重生成' in prompt and 'abstract_save_contract' in prompt
    invalid=body(e,j['job_id']);assert e['client'].post(base(e),json=invalid).status_code==409
    p=patch(e,j['job_id']);r=e['client'].post(base(e),json=p);assert r.status_code==200,r.text
    with e['factory']() as s:
        paper=s.get(Paper,e['paper']);assert paper.abstract is None
        assert {k:v for k,v in paper.reading_guide.items() if k!='abstract_translation'}==old
        assert paper.reading_guide['abstract_translation']['source']==source
        assert paper.comprehensive_analysis['retained_science']=={'x':42}
        job=s.get(WorkflowJob,j['job_id']);job.status='completed';s.commit()
    read=e['client'].get(base(e)).json();assert read['abstract_original'] is None and read['abstract_zh']==p['abstract_zh']
    assert read['abstract_translation']['original_text']==candidate['text']
    replay=e['client'].post(base(e),json=p);assert replay.status_code==200 and replay.json()['replayed']
    changed=copy.deepcopy(p);changed['abstract_zh']='修改译文';assert e['client'].post(base(e),json=changed).status_code==409
    assert science(e)==before
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'new','scope':'abstract_only'}).status_code==409
    with e['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==1


@pytest.mark.parametrize('has_abstract,has_end',[(False,True),(True,False)])
def test_unclear_abstract_is_not_inferred_and_falls_back_to_full_scope(reading_env,has_abstract,has_end):
    e=reading_env;source_pdf(e,has_abstract,has_end)
    candidate=e['client'].get(base(e)+'/context').json()['abstract_for_translation'];assert not candidate['text'] and candidate['missing_reason']
    r=e['client'].post(base(e)+'/jobs',json={'request_id':'missing','scope':'abstract_only'});assert r.status_code==200,r.text
    assert r.json()['scope']=='full'
    with e['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==1


def test_unverified_fallback_or_changed_registered_source_is_rejected(reading_env):
    e=reading_env;source_pdf(e);j=start_patch(e);p=patch(e,j['job_id'])
    bad=copy.deepcopy(p);bad['abstract_source_revision']='0'*64
    assert e['client'].post(base(e),json=bad).status_code==409
    bad=copy.deepcopy(p);bad['abstract_source']={'physical_page':1};assert e['client'].post(base(e),json=bad).status_code==422
    # Change real bytes without updating the registered SHA: the source guard must reject it.
    with e['factory']() as s:
        f=s.get(RebuildPaperFile,e['file']);path=e['settings'].storage_root/f.storage_path
    path.write_bytes(path.read_bytes()+b'\nchanged')
    assert not e['client'].get(base(e)+'/context').json()['abstract_for_translation']['text']
    assert e['client'].post(base(e),json=p).status_code==409
    with e['factory']() as s:assert not (s.get(Paper,e['paper']).comprehensive_analysis or {}).get('abstract_zh')


def test_full_save_fallback_requires_explicit_source_revision(reading_env):
    e=reading_env;source_pdf(e);j=create(e);p=body(e,j['job_id']);ctx=e['client'].get(base(e)+'/context').json()
    source=ctx['abstract_for_translation']['source']
    assert e['client'].post(base(e),json=p).status_code==409
    p['abstract_source_revision']=ctx['abstract_for_translation']['revision']
    r=e['client'].post(base(e),json=p);assert r.status_code==200,r.text
    with e['factory']() as s:
        paper=s.get(Paper,e['paper'])
        assert paper.abstract is None
        # B109: an empty metadata abstract must not replace the verified PDF
        # abstract hash with SHA256("").
        assert source['kind']=='main_text' and source['text_sha256']!=hashlib.sha256(b'').hexdigest()
        assert paper.reading_guide['beginner_zh']['abstract_source_sha256']==source['text_sha256']
        assert paper.reading_guide['abstract_translation']['original_text']==ctx['abstract_for_translation']['text']
        assert paper.reading_guide['abstract_translation']['source']==source


def test_metadata_priority_and_existing_chinese_is_preserved(reading_env):
    e=reading_env;source_pdf(e)
    with e['factory']() as s:
        paper=s.get(Paper,e['paper']);paper.abstract='Metadata abstract remains primary, even if the PDF has another abstract.';s.commit()
    ctx=e['client'].get(base(e)+'/context').json();assert ctx['abstract_for_translation']['source']['kind']=='metadata'
    assert ctx['abstract_for_translation']['text']==ctx['abstract_original']
    j=start_patch(e);p=patch(e,j['job_id']);assert e['client'].post(base(e),json=p).status_code==200
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);job.status='completed';s.commit()
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'different','scope':'abstract_only'}).status_code==409
    assert e['client'].get(base(e)).json()['abstract_zh']==p['abstract_zh']


def test_replay_of_pre_upgrade_full_request_retains_old_fingerprint(reading_env):
    e=reading_env;j=create(e);p=body(e,j['job_id']);old=ReadingAssistanceSave(**p).model_dump(mode='json');old.pop('save_mode');old.pop('abstract_source_revision')
    with e['factory']() as s:
        job=s.get(WorkflowJob,j['job_id']);job.status='completed'
        s.add(AuditLog(paper_id=e['paper'],action=service.VERSION,source=service.VERSION,target_type='paper_reading',target_id=p['request_id'],payload={'actor':'writer','fingerprint':service.digest(old),'receipt':{'saved':True,'replayed':False}}));s.commit()
    r=e['client'].post(base(e),json=p);assert r.status_code==200,r.text;assert r.json()['replayed']


def test_ambiguous_or_cross_page_abstract_is_not_guessed():
    raw='Abstract: '+ABSTRACT+'\nKeywords: battery'
    docs=[{'role':'main','file_id':str(uuid4()),'source_sha256':'a'*64,'pages':[{'page':1,'text':raw},{'page':2,'text':raw}]}]
    assert not select_abstract(None,docs)['text']
    docs[0]['pages']=[{'page':1,'text':'Abstract: '+ABSTRACT},{'page':2,'text':'Keywords: battery'}]
    assert not select_abstract(None,docs)['text']


def test_source_version_changed_since_abstract_dispatch_requires_new_intent(reading_env):
    e=reading_env;source_pdf(e);j=start_patch(e)
    with e['factory']() as s:
        f=s.get(RebuildPaperFile,e['file']);path=e['settings'].storage_root/f.storage_path
        # Registered source is legitimately replaced, so a fresh context is readable,
        # but the old dispatched intent must not silently translate a new version.
        path.write_bytes(path.read_bytes()+b'\nnew-version')
        f.sha256=hashlib.sha256(path.read_bytes()).hexdigest();s.commit()
    p=patch(e,j['job_id']);r=e['client'].post(base(e),json=p)
    assert r.status_code==409 and 'changed_since_dispatch' in r.text


def test_same_generation_request_cannot_be_reused_with_another_scope(reading_env):
    e=reading_env;create(e,'same-intent')
    r=e['client'].post(base(e)+'/jobs',json={'request_id':'same-intent','scope':'abstract_only'})
    assert r.status_code==409 and 'scope_reused' in r.text
    with e['factory']() as s:assert s.scalar(select(func.count()).select_from(WorkflowJob))==1

NOTICE='This is a PDF file of an article that is not yet the definitive version of record. This version will undergo additional copyediting, typesetting and review.'


def test_notice_is_rejected_and_adjacent_pages_have_exact_spans():
    from app.services.reading_abstract_source import publisher_notice
    assert publisher_notice(NOTICE)
    assert not publisher_notice(ABSTRACT)
    first='Title\nAbstract\n'+ABSTRACT[:110]+' but the\n  '
    second=' 2 \n\nunderlying mechanism remains unknown. '+ABSTRACT+'\nKey words: cells\n1. Introduction\nBody'
    docs=[{'role':'main','file_id':str(uuid4()),'source_sha256':'a'*64,'pages':[{'page':2,'text':first},{'page':3,'text':second}]}]
    selected=select_abstract(NOTICE,docs);assert selected['source']['physical_page_end']==3
    assert '\n'.join(docs[0]['pages'][i]['text'][span['start_char']:span['end_char']] for i,span in enumerate(selected['source']['spans']))==selected['text']
    docs[0]['pages'][1]['text']='Unknown Journal Header\n'+second
    assert not select_abstract(NOTICE,docs)['text']
    assert not select_abstract(NOTICE,[])['text']
    assert select_abstract(ABSTRACT,docs)['text']==ABSTRACT
    docs[0]['pages']=[{'page':1,'text':'ABSTRACT: '+ABSTRACT+'\n■INTRODUCTION\nBody'}]
    assert select_abstract(None,docs)['text']==ABSTRACT


def test_notice_correction_preserves_history_guide_english_science_and_cas(reading_env):
    e=reading_env;source_pdf(e)
    from app.services.reading_abstract_source import revision
    with e['factory']() as s:
        p=s.get(Paper,e['paper']);p.abstract=NOTICE
        src={'kind':'metadata','text_sha256':hashlib.sha256(NOTICE.encode()).hexdigest()}
        p.comprehensive_analysis={**p.comprehensive_analysis,'abstract_zh':'出版社版本声明旧译文'}
        p.reading_guide={**p.reading_guide,'abstract_translation':{'original_text':NOTICE,'source':src,'source_revision':revision({'text':NOTICE,'source':src}),'translation_sha256':hashlib.sha256('出版社版本声明旧译文'.encode()).hexdigest()}}
        s.commit();old=copy.deepcopy(p.reading_guide)
    before=science(e);assert e['client'].get(base(e)).json()['abstract_invalid_notice']
    j=start_patch(e);data=patch(e,j['job_id'])
    bad=copy.deepcopy(data);bad['abstract_source_revision']='f'*64
    assert e['client'].post(base(e),json=bad).status_code==409
    r=e['client'].post(base(e),json=data);assert r.status_code==200,r.text
    assert e['client'].post(base(e),json=data).json()['replayed']
    with e['factory']() as s:
        p=s.get(Paper,e['paper']);assert p.abstract==NOTICE
        assert p.reading_guide['beginner_zh']==old['beginner_zh']
        history=p.reading_guide['abstract_translation_history'];assert len(history)==1
        assert history[0]['abstract_zh']=='出版社版本声明旧译文' and history[0]['translation']==old['abstract_translation']
        assert p.reading_guide['abstract_translation']['source']['kind']=='main_text'
        job=s.get(WorkflowJob,j['job_id']);job.status='completed';s.commit()
    assert not e['client'].get(base(e)).json()['abstract_invalid_notice']
    assert e['client'].post(base(e)+'/jobs',json={'request_id':'no-second-correction','scope':'abstract_only'}).status_code==409
    assert science(e)==before
