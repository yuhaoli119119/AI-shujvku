"""Resumable ordinary extraction. Checkpoints describe work, never scientific approval."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db.models import RebuildPaperFile, WorkflowJob
from app.services.rebuild_file_lifecycle import active_files
from app.services.rebuild_file_preview import _checked_source, resolve_reading_pdf
from app.services.rebuild_workflow_service import RebuildWorkflowError

VERSION = 'ordinary_parse_v1'
STAGES = ('read', 'assets', 'rows')
STATES = ('pending', 'running', 'completed', 'partial', 'failed')


def file_plan(session, paper_id, settings, previous=None, selected=None):
    previous = previous or {}
    files = active_files(session, paper_id)
    if selected and set(selected) - {str(f.id) for f in files}:
        raise ValueError('selected_file_not_active_in_paper')
    plan = {}
    for file in files:
        fid = str(file.id)
        if selected and fid not in selected:
            continue
        old = previous.get(fid, {})
        item = {'file_id': fid, 'name': file.original_filename, 'role': file.role,
                'source_sha256': file.sha256, 'stages': {s: {'status': 'pending'} for s in STAGES}}
        if old.get('source_sha256') == file.sha256:
            item['stages'] = copy.deepcopy(old.get('stages', item['stages']))
        try:
            source, kind = _checked_source(file, settings)
            if kind == 'docx':
                # The deployed candidate already validates OOXML locators with page=null.
                item['reading_mode'] = 'ooxml'
            elif kind == 'pdf':
                import fitz
                with fitz.open(resolve_reading_pdf(file, settings)) as pdf:
                    if pdf.needs_pass or pdf.page_count < 1:
                        raise ValueError('pdf_encrypted_or_empty')
                    item.update(reading_mode='pdf', pages=pdf.page_count)
            else:
                raise ValueError('unsupported_file_format')
        except (OSError, ValueError, RebuildWorkflowError, RuntimeError) as exc:
            item['error'] = str(exc)[:500]
            item['stages'] = {s: {'status': 'failed', 'reason': str(exc)[:500]} for s in STAGES}
        plan[fid] = item
    if not plan:
        raise ValueError('no_active_files_select_or_register_main_and_si')
    return plan


def progress_summary(plan):
    files = list(plan.values())
    done = sum(all(f['stages'][s]['status'] == 'completed' for s in STAGES) for f in files)
    finished = sum(f['stages'][s]['status'] in ('completed', 'partial', 'failed') for f in files for s in STAGES)
    return {'files': files, 'completed_files': done, 'total_files': len(files),
            'finished_stages': finished, 'total_stages': len(files) * len(STAGES),
            'content_status': 'parsed' if done == len(files) else 'partial',
            'scientific_acceptance': 'not_implied'}


def checkpoint(session, paper_id, job_id, body, settings):
    job = session.scalar(select(WorkflowJob).where(WorkflowJob.job_id == job_id).with_for_update())
    if not job or job.type != 'ai_extract_rebuild' or (job.payload or {}).get('paper_id') != str(paper_id):
        raise ValueError('parse_job_not_found')
    if (job.payload or {}).get('parse_contract') != VERSION:
        raise ValueError('ordinary_parse_job_required')
    body = copy.deepcopy(body)
    rid = body.pop('request_id')
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    progress = copy.deepcopy(job.progress or {})
    receipts = progress.get('checkpoint_receipts', {})
    if rid in receipts:
        if receipts[rid]['sha256'] != digest:
            raise ValueError('checkpoint_request_id_reused')
        return {**receipts[rid]['receipt'], 'replayed': True}
    if job.status not in ('dispatching', 'running'):
        raise ValueError('parse_job_terminal')
    plan = progress.get('file_plan', {})
    fid, stage = body['file_id'], body['stage']
    if fid not in plan or stage not in STAGES or body['status'] not in STATES:
        raise ValueError('checkpoint_scope_invalid')
    record = session.get(RebuildPaperFile, UUID(fid))
    if not record or record.paper_id != paper_id:
        raise ValueError('checkpoint_source_changed')
    if body['status'] not in ('failed', 'partial'):
        if record.sha256 != plan[fid]['source_sha256']:
            raise ValueError('checkpoint_source_changed')
        _checked_source(record, settings)
    revision = progress.get('checkpoint_revision', 0)
    if body['expected_revision'] != revision:
        raise ValueError('checkpoint_revision_changed')
    prior = plan[fid]['stages'][stage]
    if prior['status'] == 'completed' and body['status'] != 'completed':
        raise ValueError('completed_stage_is_preserved')
    if body['status'] in ('partial', 'failed') and not body.get('reason'):
        raise ValueError('partial_or_failed_reason_required')
    plan[fid]['stages'][stage] = {'status': body['status'], 'reason': body.get('reason'),
                                 'updated_at': datetime.utcnow().isoformat()}
    receipt = {'job_id': job_id, 'file_id': fid, 'stage': stage, 'status': body['status'],
               'checkpoint_revision': revision + 1, 'replayed': False}
    receipts[rid] = {'sha256': digest, 'receipt': receipt}
    progress.update(file_plan=plan, checkpoint_revision=revision + 1, checkpoint_receipts=receipts,
                    content_status=progress_summary(plan)['content_status'], last_checkpoint_at=datetime.utcnow().isoformat())
    job.progress = progress
    session.flush()
    return receipt


def build_prompt(paper_id, plan, job_id, origin):
    scope = [{'file_id': f['file_id'], 'role': f['role'], 'sha256': f['source_sha256'],
              'read': f.get('reading_mode'), 'skip': [s for s in STAGES if f['stages'][s]['status'] == 'completed'],
              'error': f.get('error')} for f in plan.values()]
    return f'''普通文献解析，paper_id={paper_id}，job_id={job_id}。仅此篇，按 file_id 串行处理正文与 SI；不派子任务、不部署、不重启、不删除、不换模型。
本轮用户最新授权：简单、稳定、可用优先；允许估读、缺失、待核实并保留来源，不猜值。普通解析无需逐格审批、freeze 或独立科学验收，不宣称 scientific accepted。覆盖旧文档中普通解析前置独审要求。
文件范围：{json.dumps(scope, ensure_ascii=False, separators=(',', ':'))}
使用已有统一客户端 scripts/ordinary_parse_client.py（先 --help），会从本机工作台凭据文件登录，不输出凭据。所有本次请求/回执/临时文件仅存 outputs/stable-parsing-command-20261023/implementation/jobs/{job_id}/。CLI --job {job_id} --paper {paper_id}；get PATH、write PATH JSON_FILE、checkpoint FILE_ID STAGE STATUS [--reason 原因]。客户端固定请求字节和幂等ID、先预检数据行、最多3次退避，失败保留日志。
GET {origin}/api/rebuild/papers/{paper_id}/staged-work-package?stage=source_scope&limit=25；按分页读取所需合同，勿将首包当全量。先读已存图表/行，保留成果，跳过已完成阶段；不用每篇另写导入脚本。若请求结果未知，重放同一文件/请求ID，禁止改ID重复写。
按每个文件 read→assets→rows 执行；各阶段开始/结束用checkpoint持久回报 running/completed/partial/failed（partial/failed须原因）。PDF用文件 text-pages/render-page 读原文，DOCX用 GET /api/rebuild/papers/{paper_id}/files/FILE_ID/reading-text 取得OOXML段落/表格定位，page=null、page_basis=ooxml，不能伪造物理页。图表可保存有来源的文字与表格；不能裁图时保留文字结果并注明局限。章节完整解读不是本次正常解析门槛。
写入口 assets/import、rows/import/preflight→rows/import，均读取工作包当前合同；表格按table_write_contract保留原文结构和精度；不同条件分行，DFT/实验分开。每值须自身 file_id 与原句/图表定位。明确未报告留空，有根据估读标 estimated；局部字段不满足合同则保留失败回执并继续其他字段/文件，禁止编造填满。
普通解析最后以一次轻量中文阅读辅助收尾（不是完成门槛）：get /api/rebuild/papers/{paper_id}/reading-assistance/context，复用已读全文/图表，遵循其字段合同生成并write同路径（job_id={job_id}），中文摘要忠实翻译context.abstract_for_translation.text并保存abstract_source_revision=其revision；该来源元数据优先、为空时是服务端核实的正文摘要；text空则留缺，不概括全文冒充。导读包括问题/目标/关键对照/创新/结论条件/局限/论证脉络及少量真实图表证据；不猜首创或心理。失败保存原因即可结束普通解析，不新建任务或反复精修。
单个文件或阶段失败不能阻塞其他文件；先保留可用成果，再标注具体原因。不做多轮全篇科学精修，不反复重读已成功范围。结束报告各文件可用结果/未完成原因。GET本job核对断点；所有阶段均已处理（含失败/partial）即可结束普通解析目标，明确partial不是科学accepted。'''


def reading_text(session, paper_id, file_id, settings, offset=0, limit=25):
    """Bounded original OOXML text/table reading, with the existing locator contract."""
    import zipfile
    from xml.etree import ElementTree as ET
    record = session.get(RebuildPaperFile, file_id)
    if record is None or record.paper_id != paper_id:
        raise ValueError('file_not_in_paper')
    source, kind = _checked_source(record, settings)
    if kind != 'docx':
        raise ValueError('reading_text_requires_docx_use_pdf_text_pages')
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo('word/document.xml')
        if info.file_size > 32 * 1024 * 1024:
            raise ValueError('docx_document_too_large')
        xml = archive.read(info)
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('docx_xml_declarations_forbidden')
        root = ET.fromstring(xml)
    body = root.find('w:body', ns)
    items = []
    for index, child in enumerate(list(body) if body is not None else [], start=1):
        tag = child.tag.rsplit('}', 1)[-1]
        if tag not in ('p', 'tbl'):
            continue
        # Existing provenance uses document.xml/body/<physical child index>.
        locator = f'word/document.xml#body-child[{index}]' + ('/w:p[1]' if tag == 'p' else '/w:tr[1]/w:tc[1]')
        if tag == 'tbl':
            rows = [[''.join(t.text or '' for t in cell.findall('.//w:t', ns)) for cell in row.findall('w:tc', ns)] for row in child.findall('w:tr', ns)]
            items.append({'kind': 'table', 'rows': rows, 'cell_locators': [[f'word/document.xml#body-child[{index}]/w:tr[{ri + 1}]/w:tc[{ci + 1}]' for ci in range(len(row))] for ri, row in enumerate(rows)], 'ooxml_locator': locator})
        else:
            text = ''.join(t.text or '' for t in child.findall('.//w:t', ns))
            items.append({'kind': 'paragraph', 'text': text, 'ooxml_locator': locator})
    return {'file_id': str(file_id), 'source_sha256': record.sha256, 'page_basis': 'ooxml',
            'page_number': None, 'items': items[offset:offset + limit], 'total': len(items),
            'has_more': offset + limit < len(items), 'next_offset': offset + limit if offset + limit < len(items) else None}


async def reconcile_once(settings):
    """Recover only durable ordinary intents; never infer a stop from elapsed time."""
    import asyncio
    from app.db.session import session_scope
    from app.services.ai_extract_service import _background_dispatch, sync_job_status
    with session_scope(settings.database_url) as session:
        ids = list(session.scalars(select(WorkflowJob.job_id).where(
            WorkflowJob.type == 'ai_extract_rebuild',
            WorkflowJob.status.in_(['dispatching', 'running']),
            WorkflowJob.payload.op('->>')('parse_contract') == VERSION,
        ).order_by(WorkflowJob.updated_at).limit(4)))
    for jid in ids:
        with session_scope(settings.database_url) as session:
            job = session.get(WorkflowJob, jid)
            if job is None or job.status not in ('dispatching', 'running'):
                continue
            intent, progress = dict(job.payload or {}), dict(job.progress or {})
            unsent = job.status == 'dispatching' and not progress.get('dispatch_started')
            if not unsent:
                try:
                    await asyncio.wait_for(sync_job_status(session, job, settings), timeout=10)
                except Exception:
                    # This observation is unknown, preserving the same native identity.
                    session.rollback()
                    job = session.get(WorkflowJob, jid)
                    if job and job.status in ('dispatching', 'running'):
                        job.progress = {**(job.progress or {}), 'phase': 'needs_check', 'needs_check': True,
                                        'status_checked_at': datetime.utcnow().isoformat()}
        if unsent and intent.get('dispatch_prompt'):
            await _background_dispatch(job_id=jid, database_url=settings.database_url,
                prompt=intent['dispatch_prompt'], objective=intent['dispatch_objective'],
                settings=settings, primary_model=intent['chosen_model'], attempt_token=intent['attempt_token'])


async def monitor(settings):
    import asyncio
    import logging
    while True:
        await asyncio.sleep(30)
        try:
            await reconcile_once(settings)
            from app.services.reading_assistance import monitor_once
            await monitor_once(settings)
        except Exception:
            logging.getLogger(__name__).exception('Ordinary parsing readback will retry on next bounded observation')


def saved_file_plan(session, paper_id):
    """Merge file/hash checkpoints across subset retries, preserving prior successes."""
    merged = {}
    jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == 'ai_extract_rebuild',
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id),
        WorkflowJob.payload.op('->>')('parse_contract') == VERSION).order_by(WorkflowJob.created_at.desc(), WorkflowJob.job_id.desc()))
    for job in jobs:
        for fid, item in (job.progress or {}).get('file_plan', {}).items():
            if fid not in merged:
                merged[fid] = copy.deepcopy(item)
            elif merged[fid]['source_sha256'] == item['source_sha256']:
                for stage, part in item['stages'].items():
                    if part['status'] == 'completed':
                        merged[fid]['stages'][stage] = copy.deepcopy(part)
    return merged


def recover_auto_started_turn(payload, progress, status, settings):
    """Adopt only the first turn already journaled after our exact fresh goal ACK.

    Does not send a native request or infer identity from a file checkpoint.
    """
    if (payload.get('parse_contract') != VERSION or payload.get('chosen_mode') != 'codex_web_target'
            or progress.get('turn_id') or status is None or status.thread_id != progress.get('thread_id')
            or status.model_provider != 'workbuddy'):
        return None
    tid = progress['thread_id']
    events = progress.get('events', [])
    if any(e.get('method') == 'turn/start' and e.get('phase') == 'request_prepared' for e in events):
        return None
    fresh = False; goal_sent = False; acked = False; checked = False
    project_paths = {'/opt/literature-ai', '/opt/AI-shujvku/literature-ai'}
    cwd_matches = status.cwd == settings.codex_web_cwd or {status.cwd, settings.codex_web_cwd} == project_paths
    if not cwd_matches:
        return None
    for event in events:
        result = (event.get('response') or {}).get('result') or {}
        method, phase = event.get('method'), event.get('phase')
        if method == 'thread/start' and phase == 'response_received':
            thread = result.get('thread') or {}
            fresh = thread.get('id') == tid and thread.get('turns') == [] and result.get('modelProvider') == 'workbuddy'
        if method == 'thread/goal/set' and phase == 'request_prepared':
            params = event.get('params') or {}
            goal_sent = fresh and params.get('threadId') == tid and params.get('objective') == payload.get('dispatch_prompt') and bool(payload.get('attempt_token')) and payload['attempt_token'] in params.get('objective', '')
        if method in ('thread/goal/set', 'thread/goal/get') and phase == 'response_received':
            goal = result.get('goal') or {}
            good = goal.get('threadId') == tid and goal.get('objective') == payload.get('dispatch_prompt') and goal.get('status') == 'active'
            if method == 'thread/goal/set': acked = goal_sent and good
            else: checked = acked and good
        if method == 'thread/read' and phase == 'response_received' and checked:
            thread = result.get('thread') or {}
            turns = thread.get('turns') or []
            if thread.get('id') == tid and thread.get('modelProvider') == 'workbuddy' and turns:
                first = turns[0].get('id')
                if first and first in (status.turn_ids or ()):
                    return {'turn_id': first, 'reason': 'fresh_owned_goal_auto_start_journal_and_live_readback',
                            'thread_id': tid, 'checked_at': datetime.utcnow().isoformat()}
    return None
