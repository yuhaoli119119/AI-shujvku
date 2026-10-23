"""Conservative, source-bound abstract selection; never changes paper metadata."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def publisher_notice(text):
    """Recognize an explicit publisher pre-proof notice, not arbitrary short abstracts."""
    normalized = ' '.join(unicodedata.normalize('NFKC', text or '').lower().split())
    return (normalized.startswith('this is a pdf file of an article') and
            'definitive version of record' in normalized and
            'copyediting' in normalized and 'typesetting' in normalized)


def invalid_translation(paper):
    """Correction requires the saved translation to be bound to the rejected source."""
    saved = (paper.reading_guide or {}).get('abstract_translation') or {}
    original = saved.get('original_text') or ''
    chinese = (paper.comprehensive_analysis or {}).get('abstract_zh') or ''
    return bool(chinese and publisher_notice(original) and saved.get('source', {}).get('kind') == 'metadata'
                and saved.get('source', {}).get('text_sha256') == hashlib.sha256(original.encode()).hexdigest()
                and saved.get('translation_sha256') == hashlib.sha256(chinese.encode()).hexdigest()
                and saved.get('source_revision') == revision({'text': original, 'source': saved['source']}))


def _bounds(raw, first, last):
    while first < last and raw[first].isspace(): first += 1
    while last > first and raw[last - 1].isspace(): last -= 1
    return first, last


def select_abstract(metadata, documents):
    rejected = publisher_notice(metadata)
    if (metadata or '').strip() and not rejected:
        text = metadata
        source = {'kind': 'metadata', 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        return {'text': text, 'source': source, 'revision': revision({'text': text, 'source': source}), 'missing_reason': ''}
    candidates = []
    mains = [doc for doc in documents if doc['role'] == 'main']
    start_pattern = r'(?im)^[ \t]*abstract(?:[ \t]*[:：][ \t]*|[ \t]*\n)'
    end_pattern = r'(?im)^[ \t]*(?:key[ -]?words\b[ \t]*[:：]?|(?:[■][ \t]*)?(?:1[.\s]+)?introduction\b)'
    if len(mains) == 1:
        doc = mains[0]; pages = doc.get('pages', [])[:5]
        # A unique start is required even when one of the competing starts has no end.
        starts = [(i, m) for i,p in enumerate(pages) for m in re.finditer(start_pattern,p['text'])]
        if len(starts) == 1:
            i, start = starts[0]; page = pages[i]; raw = page['text']
            end = re.search(end_pattern, raw[start.end():])
            spans = []
            if end:
                first,last = _bounds(raw,start.end(),start.end()+end.start())
                spans = [{'physical_page':page['page'],'start_char':first,'end_char':last}]
                text = raw[first:last]
            elif i+1 < len(pages) and pages[i+1]['page'] == page['page']+1:
                following = pages[i+1]['text']; end = re.search(end_pattern,following)
                if end:
                    first,last = _bounds(raw,start.end(),len(raw))
                    # Only standalone numeric page furniture is removed. Unknown headers remain ambiguous.
                    lead = re.match(r'\s*\d+[ \t]*\n',following)
                    second,finish = _bounds(following,lead.end() if lead else 0,end.start())
                    continuation = following[second:finish]
                    prefix = raw[first:last]
                    if (len(continuation)>=50 and re.match(r'[a-z]',continuation) and
                        prefix and not re.search(r'[.!?]\s*$',prefix) and
                        not re.search(r'(?m)^\s*\d+\s*$',prefix) and
                        not re.search(r'(?m)^\s*(?:\d+[. ]+)?[A-Z][A-Za-z ]{0,60}\s*$',continuation)):
                        text=prefix+'\n'+continuation
                        spans=[{'physical_page':page['page'],'start_char':first,'end_char':last},
                               {'physical_page':pages[i+1]['page'],'start_char':second,'end_char':finish}]
            if spans and 100<=len(text)<=15000 and not publisher_notice(text):
                source={'kind':'main_text','file_id':doc['file_id'],'source_sha256':doc['source_sha256'],
                        **spans[0],'start_marker':start.group().strip(),'end_marker':end.group().strip(),
                        'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
                if len(spans)>1: source.update(physical_page_end=spans[-1]['physical_page'],spans=spans)
                candidates.append({'text':text,'source':source,'revision':revision({'text':text,'source':source}),'missing_reason':''})
    if len(candidates)==1:return candidates[0]
    return {'text':'','source':None,'revision':None,'missing_reason':
            ('元数据是出版社版本说明，不能作为科研摘要；' if rejected else '原摘要元数据为空；')+
            '正文中未找到边界唯一且可核实的摘要段，中文摘要待补。'}
