import json
import re
import os

log_path = r'C:\Users\28128\.gemini\antigravity\brain\4d7788d3-3018-4a0b-add6-9eafd261fec9\.system_generated\logs\transcript.jsonl'
files_to_recover = {
    'index.html': None,
    'page.css': None,
    'page.js': None,
    'render-list.js': None
}

with open(log_path, 'r', encoding='utf-8') as f:
    for line in f:
        try:
            entry = json.loads(line)
        except Exception:
            continue
            
        if entry.get('type') == 'TOOL_RESPONSE' and entry.get('source') == 'SYSTEM':
            content = entry.get('content', '')
            if 'Total Lines:' in content:
                for filename in files_to_recover.keys():
                    if f'/{filename}`' in content or f'\\{filename}`' in content:
                        start_idx = content.find('remove the line number, colon, and leading space.\n')
                        if start_idx != -1:
                            start_idx += len('remove the line number, colon, and leading space.\n')
                            end_idx1 = content.rfind('\nThe above content shows the entire, complete file contents')
                            end_idx2 = content.rfind('\nUse the StartLine and EndLine arguments')
                            
                            end_idx = end_idx1 if end_idx1 != -1 else end_idx2
                            if end_idx == -1: end_idx = len(content)
                            
                            raw_code = content[start_idx:end_idx]
                            clean_code = re.sub(r'^\d+: ', '', raw_code, flags=re.MULTILINE)
                            
                            if files_to_recover[filename] is None:
                                files_to_recover[filename] = clean_code
                                print(f"Recovered {filename} from logs!")

base_dir = r'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\frontend\pages\literature_library'

for filename, content in files_to_recover.items():
    if content:
        path = os.path.join(base_dir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Wrote recovered {filename} to disk.")
    else:
        print(f"Could not find {filename} in logs.")
