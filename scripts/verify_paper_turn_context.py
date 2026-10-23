#!/usr/bin/env python3
"""Host-only attestation of actual same-paper native turn context. No dispatch."""
import argparse,json,hashlib,subprocess
from pathlib import Path

ROOT=Path('/opt/AI-shujvku/literature-ai')
MODELS=('cn:deepseek-v4.1-flash','cn:glm-5.3-flash','cn:glm-5.2')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--job',required=True);parser.add_argument('--paper',required=True)
    args=parser.parse_args()
    # Read only this job's identity; no secrets are returned.
    lookup="""import json,sys
from app.config import get_settings
from app.db.session import session_scope
from app.db.models import WorkflowJob
q=json.load(sys.stdin)
with session_scope(get_settings().database_url) as s:
 j=s.get(WorkflowJob,q['job']);assert j and j.payload['paper_id']==q['paper']
 print(json.dumps({'thread_id':j.progress.get('thread_id'),'model':j.payload['chosen_model']}))
"""
    r=subprocess.run(['docker','compose','exec','-T','backend','python','-c',lookup],cwd=ROOT,input=json.dumps(vars(args)),capture_output=True,text=True,check=True)
    identity=json.loads(r.stdout);tid=identity['thread_id'];assert tid
    sessions=Path('/home/2401liyuhao/apps/codex/web-state/sessions')
    files=list(sessions.glob('*/*/*/rollout-*'+tid+'.jsonl'))+list((sessions.parent/'archived_sessions').glob('rollout-*'+tid+'.jsonl'))
    assert len(files)==1,'native_rollout_missing_or_ambiguous'
    ctx=None
    for line in files[0].open():
        d=json.loads(line)
        if d.get('type')=='turn_context':ctx=d['payload']
    assert ctx and ctx.get('model') in MODELS and ctx['model']==identity['model'],'actual_model_mismatch'
    assert ctx.get('approval_policy')=='never' and ctx.get('sandbox_policy',{}).get('type')=='danger-full-access','actual_permissions_mismatch'
    assert ctx.get('cwd') in (str(ROOT),'/opt/literature-ai'),'actual_cwd_mismatch'
    proof={k:ctx.get(k) for k in ('turn_id','model','approval_policy','sandbox_policy','permission_profile','cwd')}
    proof.update(thread_id=tid,rollout_file=str(files[0]),context_sha256=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest())
    update="""import asyncio,json,sys
from app.config import get_settings
from app.db.session import session_scope
from app.db.models import WorkflowJob
from app.services.codex_web_dispatch import read_thread_status
q=json.load(sys.stdin);settings=get_settings()
with session_scope(settings.database_url) as s:
 j=s.get(WorkflowJob,q['job'],with_for_update=True);p=q['proof']
 assert j.payload['paper_id']==q['paper'] and j.progress.get('thread_id')==p['thread_id'] and j.payload['chosen_model']==p['model']
 status=asyncio.run(read_thread_status(p['thread_id'],settings))
 assert status and status.turn_id==p['turn_id'] and j.progress.get('turn_id') in status.turn_ids and status.model==p['model']
 j.progress={**j.progress,'turn_context_proof':p,'actual_inference_verified':True,'effective_model':p['model'],'effective_provider':'workbuddy'}
 print(json.dumps(p))
"""
    r=subprocess.run(['docker','compose','exec','-T','backend','python','-c',update],cwd=ROOT,input=json.dumps({'job':args.job,'paper':args.paper,'proof':proof}),capture_output=True,text=True,check=True)
    print(r.stdout.strip())

if __name__=='__main__':main()
