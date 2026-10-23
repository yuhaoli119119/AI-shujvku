#!/usr/bin/env python3
"""Register a completed batch audit for same-paper reading; never dispatch.

Run on master from the single project checkout, after taking a paper snapshot.
Only the fixed coordinator roster and dispatch receipts are trusted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/opt/AI-shujvku/literature-ai')
BATCH = ROOT / 'outputs/full-library-acceptance-20261024'
SESSIONS = Path('/home/2401liyuhao/apps/codex/web-state/sessions')


def verified_proof(paper_id, actor):
    state = json.loads((BATCH / 'STATE.json').read_text())
    rows = [x for x in state['queue'] if x['paper_id'] == paper_id]
    if len(rows) != 1:
        raise ValueError('paper_not_in_coordinator_roster')
    row = rows[0]
    if row['phase'] != 'guide_wait_same_thread' or row.get('unresolved_writer_ids'):
        raise ValueError('paper_not_waiting_or_has_unknown_writer')
    folder = (BATCH / 'papers' / row['code']).resolve()
    if folder.parent != (BATCH / 'papers').resolve():
        raise ValueError('invalid_paper_folder')
    tid = row['thread_id']
    receipt_path = folder / 'dispatch' / (tid + '.json')
    receipt = json.loads(receipt_path.read_text())
    prompt = (folder / 'PROMPT.md').read_text()
    cwd_owned = receipt.get('cwd') in (str(ROOT), '/opt/literature-ai')
    followup = receipt.get('kind') == 'followup' and row.get('reuse_for_audit') and row.get('parse_thread_id') == tid
    if receipt.get('threadId') != tid or paper_id not in prompt or not (cwd_owned or followup):
        raise ValueError('coordinator_receipt_mismatch')
    permission = json.loads((folder / 'permissions.json').read_text())
    # Re-read actual native rollout, rather than trusting copied model claims.
    files = list(SESSIONS.glob('*/*/*/rollout-*' + tid + '.jsonl')) + list((SESSIONS.parent / 'archived_sessions').glob('rollout-*' + tid + '.jsonl'))
    if len(files) != 1:
        raise ValueError('native_rollout_missing_or_ambiguous')
    context = None; same_paper_input = False
    for line in files[0].open():
        item = json.loads(line)
        if item.get('type') == 'turn_context': context = item['payload']
        if item.get('type') == 'response_item' and item.get('payload', {}).get('role') == 'user':
            same_paper_input |= any(paper_id in part.get('text', '') for part in item['payload'].get('content', []) if isinstance(part, dict))
        if item.get('type') == 'event_msg' and item.get('payload', {}).get('type') == 'user_message':
            same_paper_input |= paper_id in item['payload'].get('message', '')
    if (not context or context.get('cwd') not in (str(ROOT), '/opt/literature-ai') or
            not same_paper_input or context.get('turn_id') != permission.get('turn_id') or
            context.get('model') != 'cn:deepseek-v4.1-flash' or context.get('approval_policy') != 'never' or
            (context.get('sandbox_policy') or {}).get('type') != 'danger-full-access'):
        raise ValueError('native_paper_model_permissions_unverified')
    # Coordinator's terminal receipt also must cover exactly the last audit turn.
    terminal = json.loads((folder / 'terminal-native.json').read_text())
    turns = (terminal.get('thread') or {}).get('turns') or []
    if not turns or turns[-1].get('id') != context['turn_id'] or turns[-1].get('status') not in ('completed', 'failed', 'interrupted'):
        raise ValueError('audit_terminal_receipt_missing')
    return {'paper_id':paper_id, 'code':row['code'], 'actor':actor, 'thread_id':tid,
        'turn_id':context['turn_id'], 'receipt_sha256':hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        'roster_request_id':row['request_id'], 'archived':files[0].parent.name=='archived_sessions', 'rollout_file':str(files[0]), 'receipt_file':str(receipt_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--check', action='store_true', help='Verify local evidence only; no registration')
    args = parser.parse_args()
    proof = verified_proof(args.paper, args.actor)
    if args.check:
        print(json.dumps(proof, ensure_ascii=False, indent=2)); return
    # No HTTP route can call register or supply proof. This requires host access.
    code = '''import asyncio,json,sys
from app.config import get_settings
from app.db.session import session_scope
from app.services.reading_thread_continuation import register
p=json.load(sys.stdin);s=get_settings()
with session_scope(s.database_url) as db:
 print(json.dumps({'source_job_id':asyncio.run(register(db,p,s))}))
'''
    result = subprocess.run(['docker','compose','exec','-T','backend','python','-c',code],
        cwd=ROOT,input=json.dumps(proof),text=True,capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:])
    print(result.stdout.strip())


if __name__ == '__main__': main()
