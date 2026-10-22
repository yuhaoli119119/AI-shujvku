"""One status projection for the current extraction path, regardless of writer.

Counts are stored work, not scientific approval or proof of full PDF coverage.
No old summary/abstract or retired workflow flag can mark a new paper parsed.
"""
from datetime import datetime, timedelta
import hashlib
from sqlalchemy import select, func
from app.db import models

LABELS = {'unparsed':'未解析','queued':'排队中','parsing':'解析中','parsed':'已解析',
          'partial':'部分解析','failed':'解析失败','needs_check':'待核实'}
JOB_TYPES = ('ai_extract_rebuild', 'rebuild_external_extract')
COUNTS = {'assets':models.RebuildVisualAsset,'rows':models.RebuildDataRow,
          'values':models.RebuildDataValue,'sources':models.RebuildValueSource}


def resolve_status(counts, latest_output=None, job=None, latest_file=None, now=None):
    now = now or datetime.utcnow()
    has_output = bool(counts.get('assets') or counts.get('rows') or counts.get('values'))
    status = 'parsed' if counts.get('assets') and counts.get('rows') else 'partial' if has_output else 'unparsed'
    message = '已存储新 AI 提取结果；不代表全文覆盖完整或科学结论已审核。' if has_output else '当前新提取流程尚无图表或数据表结果；摘要和旧解析标记不作为依据。'
    if job:
        js = job.status
        progress = job.progress or {}
        completed_at = job.updated_at or job.created_at
        # A failed old attempt cannot hide results written later by another AI.
        superseded = js == 'failed' and latest_output and completed_at and latest_output > completed_at
        if not superseded:
            if js in ('queued','dispatching','running'):
                checked = progress.get('status_checked_at')
                try: checked = datetime.fromisoformat(checked) if checked else job.updated_at or job.created_at
                except (ValueError,TypeError): checked = job.updated_at or job.created_at
                uncertain = progress.get('needs_check') or not checked or now - checked > timedelta(minutes=15)
                status = 'needs_check' if uncertain else 'queued' if js=='queued' else 'parsing'
                message = '任务状态需要回查，不能确认仍在解析。' if uncertain else '当前提取任务尚未结束；已写入的部分结果保留。'
            elif js == 'completed':
                status = 'partial' if progress.get('content_status')=='partial' else 'parsed' if has_output or progress.get('no_extractable_content_reason') else 'needs_check'
                message = progress.get('message') or ('提取任务已结束，当前结果可查看；科学验收另行判断。' if status=='parsed' else '任务结束但结果为空或未完成，请检查任务说明。')
                if latest_file and completed_at and latest_file > completed_at:
                    status, message = 'partial','提取完成后新增了正文或补充材料，需要继续解析新增文件。'
            elif js == 'failed':
                status, message = 'failed', '最新提取任务失败；已有结果保留，请检查任务记录。'
        if status in ('parsing','queued','needs_check') and progress.get('message'):
            message += ' ' + str(progress['message'])[:500]
    label = LABELS[status]
    if job and job.status in ('queued','dispatching','running'):
        label = '排队记录（执行待核实）' if job.status=='queued' else '提取记录（执行待核实）'
        message = '库内原始任务状态为 '+job.status+'；当前原生执行未在本接口独立证实，不能据旧记录、超时或已有结果判断正在执行或安全停止。'
    return {'status':status,'label':label,'message':message,'counts':counts,
            'job_status_as_recorded':job.status if job else None,'native_current_execution':'unknown_not_proven_here',
            'occupancy_changed':False,
            'job_id':job.job_id if job else None,'job_status':job.status if job else None,
            'has_results':has_output,'scientific_acceptance':'not_implied'}


def processing_statuses(session, paper_ids, include_attachments=True):
    if not paper_ids: return {}
    counts = {pid:{key:0 for key in COUNTS} for pid in paper_ids}
    latest = {pid:None for pid in paper_ids}
    with session.no_autoflush:
        for key, model in COUNTS.items():
            date = getattr(model,'updated_at',model.created_at)
            for pid,count,stamp in session.execute(select(model.paper_id,func.count(model.id),func.max(date)).where(model.paper_id.in_(paper_ids)).group_by(model.paper_id)):
                counts[pid][key] = int(count)
                if stamp and (latest[pid] is None or stamp > latest[pid]):latest[pid]=stamp
        from app.services.rebuild_file_lifecycle import file_set
        lifecycles = {pid: file_set(session, pid) for pid in paper_ids}
        active_ids = {fid for lifecycle in lifecycles.values() for fid in lifecycle['active_file_ids']}
        file_rows = [file for file in session.scalars(select(models.RebuildPaperFile).where(
            models.RebuildPaperFile.paper_id.in_(paper_ids))) if str(file.id) in active_ids]
        files = {}
        for file in file_rows:
            if file.created_at and (file.paper_id not in files or file.created_at > files[file.paper_id]):
                files[file.paper_id] = file.created_at
        jobs = list(session.scalars(select(models.WorkflowJob).where(models.WorkflowJob.type.in_(JOB_TYPES),models.WorkflowJob.payload.op('->>')('paper_id').in_([str(pid) for pid in paper_ids])).order_by(models.WorkflowJob.created_at.desc(),models.WorkflowJob.job_id.desc())))
        newest={}
        for job in jobs:newest.setdefault((job.payload or {})['paper_id'],job)
        statuses = {pid:resolve_status(counts[pid],latest[pid],newest.get(str(pid)),files.get(pid)) for pid in paper_ids}
        file_work = {}
        for model, key in ((models.RebuildVisualAsset,'assets'),(models.RebuildValueSource,'sources')):
            for file_id,count in session.execute(select(model.file_id,func.count(model.id)).where(model.paper_id.in_(paper_ids),model.file_id.is_not(None)).group_by(model.file_id)):
                file_work.setdefault(str(file_id),{})[key]=int(count)
        from app.services.rebuild_sections import EVENT as SECTION_EVENT, VERSION as SECTION_VERSION
        for event in session.scalars(select(models.AuditLog).where(models.AuditLog.paper_id.in_(paper_ids),
                models.AuditLog.action == SECTION_EVENT, models.AuditLog.source == SECTION_VERSION)):
            file_id = (event.payload or {}).get('section', {}).get('file_id')
            if file_id:
                file_work.setdefault(file_id, {})['sections'] = file_work.get(file_id, {}).get('sections', 0) + 1
        completed_files = set()
        for job in jobs:
            if job.status=='completed' and (job.progress or {}).get('content_status')!='partial':
                completed_files.update((job.result or {}).get('file_ids',[]) if isinstance(job.result,dict) else [])
        attachments = {}
        child_statuses = {}
        if include_attachments:
            from app.services.catalog_attachments import supplementary_attachments
            attachments = supplementary_attachments(session, paper_ids)
            child_ids = {item['paper_id'] for items in attachments.values() for item in items if not item['file_id'] and item['paper_id']}
            if child_ids:
                from uuid import UUID
                child_statuses = {str(pid): value for pid,value in processing_statuses(session,[UUID(pid) for pid in child_ids],include_attachments=False).items()}
        for pid,status in statuses.items():
            lifecycle = lifecycles[pid]
            status['file_lifecycle'] = lifecycle
            status['file_history'] = [item for item in lifecycle['items'] if item['id'] not in active_ids]
            review_ids = [fid for fid in lifecycle['scientific_review_required_file_ids'] if file_work.get(fid)]
            status['scientific_review_required'] = bool(review_ids)
            status['scientific_review_required_file_ids'] = review_ids
            status['files'] = []
            for file in file_rows:
                if file.paper_id != pid:continue
                work = file_work.get(str(file.id),{})
                parsed = str(file.id) in completed_files or bool(work.get('assets') or work.get('sources'))
                status['files'].append({'file_id':str(file.id),'role':file.role,'name':file.original_filename,
                    'status':'parsed' if parsed else 'unparsed','label':'已解析' if parsed else '未解析','counts':work})
            for attachment in attachments.get(pid,[]):
                if attachment['file_id']:continue
                child = child_statuses.get(attachment['paper_id'],{})
                child_state = child.get('status','unparsed')
                status['files'].append({'file_id':None,'paper_id':attachment['paper_id'],'role':'si',
                    'name':attachment['original_filename'],'status':child_state,'label':LABELS[child_state],
                    'counts':child.get('counts',{}),'registration_required':True})
            latest_job = newest.get(str(pid))
            active_scope = (latest_job.payload or {}).get('file_ids',[]) if latest_job else []
            if status['status'] in ('parsing','queued','needs_check','failed') and active_scope:
                for file in status['files']:
                    if file['file_id'] in active_scope:
                        file.update(status=status['status'], label=LABELS[status['status']])
            if latest_job and (latest_job.payload or {}).get('parse_contract') == 'ordinary_parse_v1':
                from app.services.ordinary_parsing import progress_summary
                plan = (latest_job.progress or {}).get('file_plan', {})
                status['file_progress'] = progress_summary(plan)
                for file in status['files']:
                    item = plan.get(file['file_id'])
                    if not item:
                        continue
                    states = [part['status'] for part in item['stages'].values()]
                    state = ('parsed' if all(s == 'completed' for s in states) else
                             'parsing' if 'running' in states and latest_job.status == 'running' else
                             'partial' if any(s in ('completed', 'partial') for s in states) else
                             'failed' if 'failed' in states else 'unparsed')
                    file.update(status=state, label=LABELS[state], stages=item['stages'],
                                reason=item.get('error') or '; '.join(s.get('reason') or '' for s in item['stages'].values() if s.get('reason')))
            apply_file_status(status)
            if lifecycle['ambiguous_main']:
                status.update(status='needs_check', label=LABELS['needs_check'],
                    message='存在多个当前正文且未明确首选来源；请经文件生命周期接口确认，保留历史文件和科学来源。')
        return statuses


def apply_file_status(status):
    missing = [file for file in status['files'] if file['status']!='parsed']
    if missing and status['status']=='parsed':
        status.update(status='partial',label=LABELS['partial'],message='已有解析结果，但还有未解析文件：'+ '、'.join(('正文' if file['role']=='main' else 'SI')+' '+file['name'] for file in missing))
    status['parsed_files'] = sum(file['status']=='parsed' for file in status['files'])
    status['total_files'] = len(status['files'])
    if status['status']=='unparsed' and status['parsed_files']:
        status.update(status='partial',label=LABELS['partial'],message='补充材料已有解析结果，正文仍需解析。')
    return status


def report_processing(session, paper, report):
    """Idempotent external-agent lifecycle in the existing job table.

    run_id is stable for one attempt; no old job or scientific value is rewritten.
    Completion reflects committed results, not a claim of scientific acceptance.
    """
    job_id = 'extparse-' + hashlib.sha256((str(paper.id)+'|'+report.run_id).encode()).hexdigest()[:32]
    from app.services.rebuild_queue import lock_scheduler, paper_busy, TYPE as PIPELINE_TYPE
    lock_scheduler(session)
    # Serialize same-paper reports so concurrent creation is idempotent.
    session.scalar(select(models.Paper).where(models.Paper.id==paper.id).with_for_update())
    job = session.get(models.WorkflowJob,job_id)
    terminal = report.status in ('parsed','partial','failed')
    mapped = {'queued':'queued','parsing':'running','parsed':'completed','partial':'completed','failed':'failed'}[report.status]
    if job and job.status in ('completed','failed'):
        expected = 'partial' if (job.progress or {}).get('content_status')=='partial' else 'parsed' if job.status=='completed' else 'failed'
        if report.status != expected: raise ValueError('terminal_run_is_immutable_use_new_run_id')
        return processing_statuses(session,[paper.id])[paper.id]
    occupied = paper_busy(session, paper.id, excluding=job_id)
    if occupied:
        same_writer = (occupied.type == PIPELINE_TYPE and occupied.payload.get('stage') == 'writer'
                       and report.run_id == occupied.job_id) or (
                       occupied.type == 'ai_extract_rebuild' and report.run_id in
                       {occupied.job_id, (occupied.payload or {}).get('attempt_token')})
        if not same_writer:
            raise ValueError('paper_has_other_unresolved_writer_or_reviewer')
    status = processing_statuses(session,[paper.id])[paper.id]
    if report.status=='parsed' and not status['has_results'] and not report.no_extractable_content_reason:
        raise ValueError('completion_requires_results_or_no_extractable_content_reason')
    if not job:
        job=models.WorkflowJob(job_id=job_id,type='rebuild_external_extract',library_name=paper.library_name,payload={'paper_id':str(paper.id),'run_id':report.run_id,'agent':report.agent})
        session.add(job)
    job.payload={**(job.payload or {}),'file_ids':[str(file_id) for file_id in report.file_ids]}
    job.status=mapped
    job.progress={'phase':report.status,'content_status':report.status,'message':report.message,
                  'status_checked_at':datetime.utcnow().isoformat(),
                  'no_extractable_content_reason':report.no_extractable_content_reason}
    selected_files = report.file_ids or []
    if selected_files:
        by_id={file['file_id']:file for file in status['files']}
        if report.status=='parsed' and not report.no_extractable_content_reason and any(not (by_id.get(str(fid),{}).get('counts',{}).get('assets') or by_id.get(str(fid),{}).get('counts',{}).get('sources')) for fid in selected_files):
            raise ValueError('file_completion_requires_file_results_or_reason')
        from app.services.rebuild_file_lifecycle import file_set
        owned = set(file_set(session, paper.id)['active_file_ids'])
        if any(str(file_id) not in owned for file_id in selected_files): raise ValueError('file_belongs_to_other_paper')
    job.result={'counts':status['counts'],'file_ids':[str(file_id) for file_id in selected_files], 'scientific_acceptance':'not_implied'} if terminal else None
    job.error=report.message if report.status=='failed' else None
    session.flush()
    return processing_statuses(session,[paper.id])[paper.id]
