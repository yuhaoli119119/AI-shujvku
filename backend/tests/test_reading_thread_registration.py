import importlib.util
import json
from pathlib import Path
import pytest

pytestmark=pytest.mark.no_test_database


def verifier(tmp_path,monkeypatch,kind='create'):
    path=Path('/project-scripts/register_reading_thread.py')
    spec=importlib.util.spec_from_file_location('registration_verifier',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    root=tmp_path/'project';batch=root/'outputs/full-library-acceptance-20261024';sessions=tmp_path/'sessions'
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'BATCH',batch);monkeypatch.setattr(m,'SESSIONS',sessions)
    folder=batch/'papers'/'B002';(folder/'dispatch').mkdir(parents=True)
    row={'paper_id':'paper-id','code':'B002','phase':'guide_wait_same_thread','request_id':'req','thread_id':'owned',
         'unresolved_writer_ids':[], 'reuse_for_audit':True, 'parse_thread_id':'owned'}
    (batch/'STATE.json').write_text(json.dumps({'queue':[row]}))
    receipt={'threadId':'owned','kind':kind,'turnId':'audit'}
    if kind=='create':receipt['cwd']=str(root)
    (folder/'dispatch'/'owned.json').write_text(json.dumps(receipt));(folder/'PROMPT.md').write_text('read paper-id')
    (folder/'permissions.json').write_text(json.dumps({'turn_id':'audit'}))
    (folder/'terminal-native.json').write_text(json.dumps({'thread':{'id':'owned','turns':[{'id':'audit','status':'completed'}]}}))
    f=sessions/'2026/10/24/rollout-owned.jsonl';f.parent.mkdir(parents=True)
    items=[{'type':'response_item','payload':{'role':'user','content':[{'text':'paper-id audit'}]}},
           {'type':'turn_context','payload':{'turn_id':'audit','cwd':str(root),'model':'cn:deepseek-v4.1-flash','approval_policy':'never','sandbox_policy':{'type':'danger-full-access'}}}]
    f.write_text('\n'.join(json.dumps(x) for x in items))
    return m,batch,folder,f


@pytest.mark.parametrize('kind',['create','followup'])
def test_host_register_receipt_and_rollout_for_creation_or_parser_audit_followup(tmp_path,monkeypatch,kind):
    m,batch,folder,f=verifier(tmp_path,monkeypatch,kind)
    proof=m.verified_proof('paper-id','writer')
    assert proof['thread_id']=='owned' and proof['turn_id']=='audit' and not proof['archived']


@pytest.mark.parametrize('fault',['busy','foreign_paper','foreign_model','foreign_cwd','missing_receipt'])
def test_host_registration_rejects_unowned_receipt_or_busy_roster(tmp_path,monkeypatch,fault):
    m,batch,folder,f=verifier(tmp_path,monkeypatch)
    if fault=='busy':
        state=json.loads((batch/'STATE.json').read_text());state['queue'][0]['phase']='audit_running';(batch/'STATE.json').write_text(json.dumps(state))
    elif fault=='missing_receipt':(folder/'dispatch'/'owned.json').write_text('{}')
    else:
        items=[json.loads(x) for x in f.read_text().splitlines()]
        if fault=='foreign_paper':items[0]['payload']['content'][0]['text']='another paper'
        if fault=='foreign_model':items[1]['payload']['model']='gpt-6-luna'
        if fault=='foreign_cwd':items[1]['payload']['cwd']='/another/project'
        f.write_text('\n'.join(json.dumps(x) for x in items))
    with pytest.raises(ValueError):m.verified_proof('paper-id','writer')


@pytest.mark.parametrize('fault',[None,'missing_old_turn','forged_old_model','latest_restricted'])
def test_resumed_audit_attests_latest_turn_without_trusting_stale_permissions(tmp_path,monkeypatch,fault):
    m,batch,folder,f=verifier(tmp_path,monkeypatch)
    items=[json.loads(x) for x in f.read_text().splitlines()]
    latest={**items[-1]['payload'],'turn_id':'resumed','model':'cn:glm-5.2'}
    if fault=='latest_restricted':latest['sandbox_policy']={'type':'read-only'}
    items.append({'type':'turn_context','payload':latest})
    f.write_text('\n'.join(json.dumps(x) for x in items))
    (folder/'terminal-native.json').write_text(json.dumps({'thread':{'id':'owned','turns':[{'id':'resumed','status':'completed'}]}}))
    permission={'turn_id':'missing' if fault=='missing_old_turn' else 'audit','model':'gpt-6-luna' if fault=='forged_old_model' else 'cn:deepseek-v4.1-flash'}
    (folder/'permissions.json').write_text(json.dumps(permission))
    if fault:
        with pytest.raises(ValueError):m.verified_proof('paper-id','writer')
    else:
        proof=m.verified_proof('paper-id','writer')
        assert proof['turn_id']=='resumed' and proof['model']=='cn:glm-5.2'
