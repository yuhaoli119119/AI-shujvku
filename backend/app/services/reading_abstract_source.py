"""Conservative, source-bound abstract selection; never changes paper metadata."""
from __future__ import annotations

import hashlib
import json
import re


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def select_abstract(metadata, documents):
    if (metadata or '').strip():
        text = metadata
        source = {'kind': 'metadata', 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        return {'text': text, 'source': source, 'revision': revision({'text': text, 'source': source}), 'missing_reason': ''}
    candidates = []
    mains = [doc for doc in documents if doc['role'] == 'main']
    # Multiple active originals cannot supply an unambiguous fallback.
    if len(mains) == 1:
        doc = mains[0]
        for page in doc.get('pages', [])[:5]:
            raw = page['text']
            starts = list(re.finditer(r'(?im)^[ \t]*abstract(?:[ \t]*[:：][ \t]*|[ \t]*\n)', raw))
            if len(starts) != 1:
                continue
            for start in starts:
                tail = raw[start.end():]
                end = re.search(r'(?im)^[ \t]*(?:key[ -]?words\b[ \t]*[:：]?|(?:1[.\s]+)?introduction\b)', tail)
                if not end:
                    continue
                first, last = start.end(), start.end() + end.start()
                while first < last and raw[first].isspace(): first += 1
                while last > first and raw[last - 1].isspace(): last -= 1
                text = raw[first:last]
                # A heading alone, long unrelated section, nested abstract, or non-text page is not enough.
                if not 100 <= len(text) <= 15000 or re.search(r'(?im)^\s*abstract\s*[:：]?', text):
                    continue
                source = {'kind': 'main_text', 'file_id': doc['file_id'], 'source_sha256': doc['source_sha256'],
                          'physical_page': page['page'], 'start_char': first, 'end_char': last,
                          'start_marker': start.group().strip(), 'end_marker': end.group().strip(),
                          'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
                candidates.append({'text': text, 'source': source, 'revision': revision({'text': text, 'source': source}), 'missing_reason': ''})
    if len(candidates) == 1:
        return candidates[0]
    return {'text': '', 'source': None, 'revision': None,
            'missing_reason': '原摘要元数据为空，正文中未找到边界唯一且可核实的摘要段；中文摘要待补。'}
