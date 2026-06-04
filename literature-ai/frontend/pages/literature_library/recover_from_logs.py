import json
import re

log_path = r'C:\Users\28128\.gemini\antigravity\brain\4d7788d3-3018-4a0b-add6-9eafd261fec9\.system_generated\logs\transcript.jsonl'

files_to_recover = {
    'index.html': None,
    'page.css': None,
    'page.js': None
}

with open(log_path, 'r', encoding='utf-8') as f:
    for line in f:
        try:
            entry = json.loads(line)
        except:
            continue
            
        if entry.get('type') == 'PLANNER_RESPONSE':
            # Not what we need directly, we need the TOOL_RESPONSE for view_file
            pass
        elif entry.get('type') == 'TOOL_RESPONSE' and entry.get('source') == 'SYSTEM':
            # Need to see if it's view_file output.
            # Unfortunately, transcript TOOL_RESPONSE doesn't always have tool name directly, 
            # but we can look at the output content.
            # The output of view_file looks like:
            # "File Path: `.../index.html`\nTotal Lines: ...\nShowing lines 1 to ...\nThe following code has been modified..."
            content = entry.get('content', '')
            if 'File Path: ' in content and 'Total Lines:' in content:
                # Find which file it is
                for filename in files_to_recover.keys():
                    if f'/{filename}`' in content or f'\\{filename}`' in content:
                        # Extract the actual code
                        # The code starts after "The following code has been modified to include a line number before every line... \n"
                        # Or after "The following code has been modified..."
                        
                        # Find the start of the code
                        start_idx = content.find('remove the line number, colon, and leading space.\n')
                        if start_idx != -1:
                            start_idx += len('remove the line number, colon, and leading space.\n')
                            end_idx1 = content.rfind('\nThe above content shows the entire, complete file contents')
                            end_idx2 = content.rfind('\nUse the StartLine and EndLine arguments')
                            
                            end_idx = end_idx1 if end_idx1 != -1 else end_idx2
                            if end_idx == -1: end_idx = len(content)
                            
                            raw_code = content[start_idx:end_idx]
                            
                            # Remove line numbers: `1: ` or `123: `
                            # regex ^\d+:\s
                            clean_code = re.sub(r'^\d+: ', '', raw_code, flags=re.MULTILINE)
                            
                            # Save it! We only keep the FIRST occurrence (which is the earliest view_file before any changes)
                            if files_to_recover[filename] is None:
                                files_to_recover[filename] = clean_code
                                print(f"Recovered {filename} from logs!")

# Write recovered files
base_dir = r'D:\Desktop\03_代码与开发\AI-shujvku\literature-ai\frontend\pages\literature_library'
import os

for filename, content in files_to_recover.items():
    if content:
        path = os.path.join(base_dir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Wrote recovered {filename} to disk.")
    else:
        print(f"Could not find {filename} in logs.")
