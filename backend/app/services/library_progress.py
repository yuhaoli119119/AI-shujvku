"""Live read-only progress report, never a manually maintained ledger."""
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select
from app.db.models import Paper, RebuildDataValue
from app.services.paper_list_readonly import readonly_library_clause
from app.services.paper_processing_status import processing_statuses
from app.services.paper_codes import main_paper_clause


def library_progress(session, library_name):
    papers = list(session.scalars(select(Paper).where(readonly_library_clause(library_name), main_paper_clause()).order_by(Paper.paper_code, Paper.id)))
    ids = [p.id for p in papers]
    statuses = processing_statuses(session, ids)
    issues = {pid: [] for pid in ids}
    if ids:
        for value in session.scalars(select(RebuildDataValue).where(RebuildDataValue.paper_id.in_(ids))):
            if value.value_type == 'missing' or value.missing_reason:
                issues[value.paper_id].append({'kind':'missing_value','label':'缺失字段','field':value.field_name,'value_id':str(value.id),'reason':value.missing_reason or '尚无可用值'})
            conflict = value.conflict or {}
            if conflict.get('status') in ('value_conflict','unit_conflict') or conflict.get('legacy_unresolved') in ('value_conflict','unit_conflict'):
                issues[value.paper_id].append({'kind':'value_conflict','label':'数值或单位冲突','field':value.field_name,'value_id':str(value.id),'reason':'存在未裁决候选，请查看字段来源与冲突记录'})
    items=[]
    for paper in papers:
        status=statuses[paper.id]
        problems=issues[paper.id]
        if not paper.pdf_path:
            problems.append({'kind':'missing_pdf','label':'缺少正文 PDF','reason':'当前未关联正文 PDF'})
        for file in status['files']:
            if file['status']!='parsed':
                problems.append({'kind':'unfinished_file','label':'文件未完成','file_id':file['file_id'],'role':file['role'],'reason':file['name']+'：'+file['label']})
        if status['status'] in ('failed','needs_check','partial'):
            problems.append({'kind':'processing_issue','label':status['label'],'reason':status['message']})
        items.append({'paper_id':str(paper.id),'paper_code':paper.paper_code,'title':paper.title,
            'processing_status':status,'issues':problems,'issue_count':len(problems),
            'next_action':'核查问题与来源' if problems else '开始提取' if status['status']=='unparsed' else '等待任务并回查' if status['status'] in ('queued','parsing') else '查看已有结果',
            'work_package_url':f'/api/rebuild/papers/{paper.id}/ai-work-package'})
    return {'library_name':library_name,'generated_at':datetime.now(timezone.utc).isoformat(),
        'summary':{'total':len(items),'statuses':dict(Counter(x['processing_status']['status'] for x in items)),
                   'papers_with_issues':sum(bool(x['issues']) for x in items)},
        'read_contract':'Live database projection. Re-read before work; this report is not a task claim or scientific acceptance. Report external AI lifecycle through each work package processing_status_contract.',
        'items':items}
