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


def pdf_abstract_boxes(page, raw):
    """Read-only evidence for a narrow unlabelled, shaded abstract layout.

    Text order alone is unsafe in two columns. Require an isolated bold
    paragraph on a neutral grey background, wider than the body column,
    below a larger title and immediately above the first Introduction.
    The caller supplies a hash-checked main PDF, never client-provided boxes.
    """
    import fitz

    if page.rotation or page.rect.width > 1600 or page.rect.height > 2400:
        return []
    blocks = [b for b in page.get_text('dict', flags=fitz.TEXTFLAGS_TEXT)['blocks']
              if b['type'] == 0]

    def block_text(block):
        return '\n'.join(''.join(span['text'] for span in line['spans'])
                         for line in block['lines'])

    def max_size(block):
        return max((s['size'] for line in block['lines'] for s in line['spans']), default=0)

    intros = [b for b in blocks if re.fullmatch(
        r'(?:1[.\s]+)?introduction', block_text(b).strip(), re.I)]
    if len(intros) != 1:
        return []
    intro = intros[0]; ib = fitz.Rect(intro['bbox']); width = page.rect.width
    bodies = [b for b in blocks if 0 <= b['bbox'][1] - ib.y1 <= 40
              and abs(b['bbox'][0] - ib.x0) <= 5
              and .25 * width <= b['bbox'][2] - b['bbox'][0] <= .55 * width
              and len(block_text(b)) >= 80]
    if not bodies:
        return []
    body = min(bodies, key=lambda b: b['bbox'][1]); bb = fitz.Rect(body['bbox'])
    candidates = []; pixmap = None
    for block in blocks:
        rect = fitz.Rect(block['bbox']); text = block_text(block).strip()
        size = max_size(block)
        if not (100 <= len(text) <= 15000 and len(text.split()) >= 50
                and 6 <= size <= 14 and rect.height >= size * 3
                and abs(rect.x0 - ib.x0) <= 5
                and 0 < ib.y0 - rect.y1 <= 60
                and rect.width >= 1.15 * bb.width and rect.width <= .85 * width):
            continue
        # Notices/captions/headings are not scientific abstracts, even in boxes.
        if (publisher_notice(text) or not re.search(r'[.!?]$', text)
                or re.search(r'(?im)^\s*(?:publisher|copyright|accepted manuscript|'
                             r'journal pre-proof|disclaimer|key[ -]?words|'
                             r'\d*[.\s]*introduction|fig(?:ure)?[.\s]|table\s)', text)):
            continue
        spans = [s for line in block['lines'] for s in line['spans']]
        count = sum(len(s['text']) for s in spans)
        bold = sum(len(s['text']) for s in spans if s['flags'] & 16)
        if not count or bold / count < .8:
            continue
        titles = [b for b in blocks if b['bbox'][3] < rect.y0
                  and b['bbox'][2] - b['bbox'][0] >= .6 * width
                  and max_size(b) >= size * 1.4]
        if not titles:
            continue
        start = raw.find(text)
        if start < 0 or raw.find(text, start + 1) >= 0:
            continue
        # Render source pixels: actual fills may be raster images, not PDF paths.
        if pixmap is None:
            pixmap = page.get_pixmap(colorspace=fitz.csRGB, alpha=False)
        pixels = []
        for x in (rect.x0 - 3, rect.x1 + 3):
            for y in (rect.y0, (rect.y0 + rect.y1) / 2, rect.y1):
                px, py = int(x) - pixmap.x, int(y) - pixmap.y
                if not (0 <= px < pixmap.width and 0 <= py < pixmap.height):
                    pixels = []; break
                pixels.append(pixmap.pixel(px, py)[:3])
        if len(pixels) != 6 or any(max(rgb) - min(rgb) > 20
                or not 165 <= sum(rgb) / 3 <= 238 for rgb in pixels):
            continue
        candidates.append({'start_char': start, 'end_char': start + len(text),
            'bbox': [round(x, 3) for x in rect],
            'selection_method': 'shaded_abstract_box_v1',
            'layout_evidence': {'introduction_bbox': [round(x, 3) for x in ib],
                'body_column_bbox': [round(x, 3) for x in bb],
                'title_bbox': [round(x, 3) for x in max(titles, key=max_size)['bbox']],
                'background_samples_rgb': pixels},
            'start_marker': 'layout:unique_shaded_bold_paragraph',
            'end_marker': block_text(intro).strip()})
    return candidates


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
    # Never use layout to break a labelled Abstract ambiguity or failed boundary.
    if len(mains) == 1 and not starts:
        # A later page must not turn an Introduction callout into an abstract.
        introduction_count = sum(bool(re.fullmatch(r'(?:1[. \t]+)?introduction', line.strip(), re.I))
                                 for page in pages for line in page['text'].splitlines())
        doc = mains[0]
        boxes = [(page, box) for page in pages
                 for box in page.get('abstract_boxes', [])]
        if len(boxes) == 1 and introduction_count == 1:
            page, box = boxes[0]
            text = page['text'][box['start_char']:box['end_char']]
            if 100 <= len(text) <= 15000 and not publisher_notice(text):
                source = {'kind': 'main_text', 'file_id': doc['file_id'],
                    'source_sha256': doc['source_sha256'], 'physical_page': page['page'],
                    **box, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                candidates.append({'text': text, 'source': source,
                    'revision': revision({'text': text, 'source': source}), 'missing_reason': ''})
    if len(candidates)==1:return candidates[0]
    return {'text':'','source':None,'revision':None,'missing_reason':
            ('元数据是出版社版本说明，不能作为科研摘要；' if rejected else '原摘要元数据为空；')+
            '正文中未找到边界唯一且可核实的摘要段，中文摘要待补。'}
