"""Optional Chinese reading help. Reuses Paper fields and native extraction dispatch.

Never writes scientific rows, files, assets, sections or approval state.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import select
from app.db.models import AuditLog, Paper, RebuildVisualAsset, WorkflowJob
from app.services.rebuild_file_lifecycle import active_files
from app.services.rebuild_file_preview import _checked_source
from app.services.reading_abstract_source import invalid_translation

TYPE = 'paper_reading_assistance'
VERSION = 'beginner_zh_v1'
RUNNING = ('dispatching', 'running', 'queued')


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def reading_revision(paper):
    return digest({'guide': paper.reading_guide, 'abstract': paper.abstract,
                   'abstract_zh': (paper.comprehensive_analysis or {}).get('abstract_zh')})


def input_context(session, paper_id, settings):
    """Source text, with explicit bounded coverage; no model or mutation on GET."""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ValueError('paper_not_found')
    documents = []; remaining = 280000
    for file in active_files(session, paper_id):
        item = {'file_id': str(file.id), 'role': file.role, 'name': file.original_filename,
                'source_sha256': file.sha256, 'complete_text': True, 'pages': []}
        try:
            source, kind = _checked_source(file, settings)
            if kind == 'pdf':
                import fitz
                with fitz.open(source) as document:
                    item['physical_pages'] = document.page_count
                    for index, page in enumerate(document):
                        text = page.get_text()
                        if len(text) > remaining:
                            item['complete_text'] = False; break
                        page_item = {'page': index + 1, 'text': text}
                        if file.role == 'main' and index < 5:
                            from app.services.reading_abstract_source import publisher_notice, pdf_abstract_boxes
                            if not (paper.abstract or '').strip() or publisher_notice(paper.abstract):
                                try:
                                    boxes = pdf_abstract_boxes(page, text)
                                except (RuntimeError, ValueError):
                                    # Optional layout recovery must not hide readable text.
                                    boxes = []
                                if boxes:
                                    page_item['abstract_boxes'] = boxes
                                if index + 1 < min(document.page_count, 5):
                                    from app.services.reading_abstract_source import pdf_numbered_abstract
                                    try:
                                        sections = pdf_numbered_abstract(page, text, document[index + 1])
                                    except (RuntimeError, ValueError):
                                        sections = []
                                    if sections:
                                        page_item['numbered_abstract_sections'] = sections
                        item['pages'].append(page_item); remaining -= len(text)
                    if not any(p['text'].strip() for p in item['pages']):
                        item['complete_text'] = False; item['reason'] = '扫描页未提供可读文字'
            elif kind == 'docx':
                from app.services.ordinary_parsing import reading_text
                offset = 0; parts = []
                while True:
                    block = reading_text(session, paper_id, file.id, settings, offset, 100)
                    for part in block['items']:
                        size = len(json.dumps(part, ensure_ascii=False))
                        if size > remaining:
                            item['complete_text'] = False; break
                        parts.append(part); remaining -= size
                    if not item['complete_text'] or not block['has_more']: break
                    offset = block['next_offset']
                item.update(page_basis='ooxml', items=parts, image_limit='只读到文字与表格，未读取DOCX内嵌图片像素')
            else:
                item.update(complete_text=False, reason='当前格式无可读文字')
        except Exception as exc:
            item.update(complete_text=False, reason=f'文件文字暂不可读: {type(exc).__name__}')
        if not item['complete_text'] and not item.get('reason'): item['reason'] = '文字超出本次轻量读取范围'
        documents.append(item)
    assets = []
    for a in session.scalars(select(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == paper_id,
                               RebuildVisualAsset.asset_type != 'subfigure').order_by(RebuildVisualAsset.id)):
        assets.append({'asset_id': str(a.id), 'file_id': str(a.file_id) if a.file_id else None,
            'label': a.figure_label, 'pages': a.page_numbers, 'caption': (a.caption or '')[:2500],
            'explanation': (a.explanation or '')[:2500], 'has_image': bool(a.image_path)})
    result = {'paper_id': str(paper_id), 'title': paper.title, 'abstract_original': paper.abstract or '',
              'documents': documents, 'figures': assets[:100], 'reading_revision': reading_revision(paper),
              'instruction': '正文提供文字证据；未直接看图像，图表判断需说明依据是已有图注/解读。翻译原摘要，不扩写为全文总结。'}
    from app.services.reading_abstract_source import select_abstract
    result['abstract_for_translation'] = select_abstract(paper.abstract, documents)
    result['input_revision'] = digest(result)
    from app.schemas.reading_assistance import ReadingAssistanceSave, AbstractTranslationSave
    result['save_contract'] = ReadingAssistanceSave.model_json_schema()
    result['abstract_save_contract'] = AbstractTranslationSave.model_json_schema()
    return result


def latest(session, paper_id):
    return session.scalar(select(WorkflowJob).where(WorkflowJob.type == TYPE,
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id)).order_by(WorkflowJob.created_at.desc()))


def serialize(job):
    if not job: return {'status': 'not_started', 'job_id': None}
    progress = job.progress or {}
    return {'job_id': job.job_id, 'status': job.status, 'phase': progress.get('phase'), 'scope':(job.payload or {}).get('reading_scope','full'),
        'message': '阅读内容已保存' if progress.get('reading_saved') else
                   '生成未完成，可保留已有内容后重试' if job.status == 'failed' else
                   '正在准备中文阅读内容' if job.status in RUNNING else '阅读内容待补充',
        'saved': bool(progress.get('reading_saved')), 'error': job.error,
        'thread_id':progress.get('thread_id'), 'turn_id':progress.get('turn_id'),
        'chosen_model':(job.payload or {}).get('chosen_model'),'effective_model':progress.get('effective_model'),
        'fallback_attempts':len(progress.get('native_attempts') or []),
        'native_error':progress.get('native_error'),
        'source_job_id':(job.payload or {}).get('continuation',{}).get('source_job_id'),
        'can_retry': job.status == 'failed' and bool(progress.get('native_terminal_verified')),
        'created_at': job.created_at.isoformat(), 'updated_at': job.updated_at.isoformat()}


def prompt(paper_id, jid, scope='full'):
    if scope == 'abstract_only':
        return abstract_prompt(paper_id, jid)
    return f'''为科研新人生成中文摘要和本文导读，只处理paper_id={paper_id}。这是独立阅读辅助，不解析/修改科学数据，不部署、不派子任务、不另建任务、不更换模型。只用现有统一客户端scripts/ordinary_parse_client.py，--job {jid} --paper {paper_id}；先--help。产物仅outputs/stable-parsing-command-20261023/detail-reading/jobs/{jid}/，不输出凭据。
1) get /api/rebuild/papers/{paper_id}/reading-assistance/context，保存并完整读取返回原摘要、正文/SI文字与图注（分块看已保存JSON，不能只看终端截断）。缺失须标注，外部检索不是这次前置。
2) 一次组织中文内容，abstract_zh必须忠实翻译abstract_for_translation.text，仅翻译不可把全文结论扩写进去；元数据为空时仅用服务器核实的正文摘要回退，保存abstract_source_revision=abstract_for_translation.revision；text为空则abstract_zh空并填missing_reason。one_sentence、problem、main_finding让新人30秒读懂；guide六项background_gap/research_objective/study_design/innovation/conclusions/limitations给出本文背景空白、目标、方案与关键对照、创新、结论适用条件、局限；argument_flow用4-6条串联问题→方案/假设→具体实验/计算证据→结论。只写本文真实内容，不用通用套话凑齐。innovation区分作者声称和已有对照直接支持，不能自称世界首创、猜作者心理。证据不足留空并missing。
3) evidence为3-5个关键图表asset_id/question/finding（只引用输入已有asset_id；说明回答什么问题、支持哪个结论，文字/图注依据明确），terms为少量term/explanation。content_scope只能abstract/main_text/main_and_si/partial_text，按实际documents完整范围选；未读完整正文不能称全文完成。DOCX未读图像写入missing，不能把图注冒充图像验证。
4) 保存JSON：request_id稳定、job_id={jid}、input_revision来自context，其余字段abstract_zh/abstract_missing_reason/one_sentence/problem/main_finding/guide/argument_flow/evidence/terms/content_scope/missing。write /api/rebuild/papers/{paper_id}/reading-assistance JSON_FILE。未知结果原体原ID重放，客户端最多三次退避；拒绝则修正明确错误，不循环重建任务。最后get同路径读回中文摘要和导读。生成/保存失败只保留原因，不阻塞普通解析；可报告partial，非scientific accepted。全部保存读回后结束目标。'''


def abstract_prompt(paper_id, jid):
    return f'''只补充paper_id={paper_id}的中文摘要，不重生成或改写任何已有导读、科学数据、Paper.abstract，不部署、不派发其他任务。仅使用scripts/ordinary_parse_client.py --job {jid} --paper {paper_id}；先--help。产物仅outputs/stable-parsing-command-20261023/detail-reading/jobs/{jid}/，不输出凭据。
1) get /api/rebuild/papers/{paper_id}/reading-assistance/context，完整读取abstract_for_translation.text及source。它优先选择科研摘要元数据，明确的出版社版本声明被排除，否则为服务端核实的正文Abstract段；不得换用全文总结。source包含真实file_id/sha256/物理页与字符定位。不确定或text空就报告待补，不猜、不重建任务。
2) 一次忠实翻译该段为中文，保留原意、条件、数字与作者主张强度，不添加整篇结论。遵守abstract_save_contract，只写save_mode=abstract_only、request_id稳定、job_id={jid}、input_revision、abstract_source_revision=abstract_for_translation.revision、abstract_zh。服务端只补缺失译文或纠正已核实误译的出版社版本说明及来源，保留旧译文历史，保护所有已有导读。
3) write /api/rebuild/papers/{paper_id}/reading-assistance JSON_FILE；未知结果原体原ID有界重放。保存后get同路径核对译文及abstract_translation来源。不调用其他写接口，不改原摘要字段。确认保存读回后结束原生目标并结束本轮，不等待后台job completed（它需要本轮先结束），不另开任务。'''


async def create(session, paper_id, settings, request_id, retry=False, scope='full', *, source_job_id=None, actor=None, model=None):
    from functools import partial
    from app.services.paper_db_phase import observe_and_apply
    from app.services.ai_extract_service import _arm_dispatches
    dispatches = []
    result = await observe_and_apply(session, partial(_create_phase, paper_id=paper_id,
        settings=settings, request_id=request_id, retry=retry, scope=scope,
        source_job_id=source_job_id, actor=actor, model=model, dispatches=dispatches),
        after_commit=lambda _: _arm_dispatches(dispatches))
    return result


def _create_phase(session, paper_id, settings, request_id, retry=False, scope='full', *, source_job_id=None, actor=None, model=None, observation, dispatches):
    from app.services.rebuild_queue import paper_busy
    # Paper FOR UPDATE is the cross-route writer mutex. Do not serialize unrelated
    # papers on the queue-wide advisory lock.
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if not paper: raise ValueError('paper_not_found')
    previous = session.scalar(select(WorkflowJob).where(WorkflowJob.type == TYPE,
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id),
        WorkflowJob.payload.op('->>')('request_id') == request_id))
    if previous:
        if (previous.payload.get('reading_scope','full')!=scope or
                previous.payload.get('actor') is not None and previous.payload.get('actor') != actor or
                previous.payload.get('requested_source_job_id') != source_job_id or
                previous.payload.get('requested_model') != model):raise ValueError('reading_request_scope_reused')
        return serialize(previous)
    old = latest(session, paper_id)
    if old and old.status in RUNNING: return serialize(old)
    if old and old.status == 'failed' and not (old.progress or {}).get('native_terminal_verified'):
        return serialize(old)
    if paper_busy(session, paper_id): raise ValueError('这篇文献已有任务，完成后可补充导读')
    from app.services.paper_db_phase import require_source
    continuation = require_source(session, paper_id, settings, source_job_id, actor, observation)
    context = input_context(session, paper_id, settings)
    if scope == 'abstract_only':
        if (paper.comprehensive_analysis or {}).get('abstract_zh') and not invalid_translation(paper):
            raise ValueError('中文摘要已保存，无需重复补充')
        if not context['abstract_for_translation']['text']:
            raise ValueError(context['abstract_for_translation']['missing_reason'])
    jid = 'aiext-guide-' + uuid4().hex[:20]; token = str(uuid4())
    instruction = 'attempt_token=' + token + '\n' + prompt(paper_id, jid, scope)
    from app.services.paper_native_models import require_model
    requested_model=model
    model=require_model(model or (continuation or {}).get('source_model') or 'cn:deepseek-v4.1-flash')
    if continuation and model!=continuation['source_model']:
        raise ValueError('use_same_job_recovery_for_model_switch')
    job = WorkflowJob(job_id=jid, type=TYPE, status='dispatching', library_name=paper.library_name,
        payload={'paper_id':str(paper_id), 'request_id':request_id, 'attempt_token':token,
                 'actor':actor, 'requested_source_job_id':source_job_id, 'requested_model':requested_model,
                 **({'continuation':continuation} if continuation else {}),
                 'chosen_model':model, 'chosen_mode':'codex_web_target', 'parse_contract':'ordinary_parse_v1',
                 'reading_revision':context['reading_revision'], 'input_revision':context['input_revision'], 'reading_scope':scope,
                 'dispatch_prompt':instruction, 'dispatch_objective':instruction},
        progress={'phase':'dispatching', 'dispatch_mode':'codex_web_target', 'model':model,
                  'dispatch_started':False, 'events':[],
                  **({'thread_id':continuation['thread_id'], 'previous_turn_id':continuation['turn_id']} if continuation else {})})
    session.add(job);session.commit()
    dispatches.append(dict(job_id=jid, database_url=settings.database_url,
        prompt=instruction, objective=instruction, settings=settings, primary_model=model, attempt_token=token))
    return serialize(job)


def save(session, paper_id, body, settings, actor):
    from app.services.external_paper_work import guard_workbench_write, VERSION as MCP_VERSION
    guard_workbench_write(session, paper_id, actor, body.job_id)
    paper=session.scalar(select(Paper).where(Paper.id==paper_id).with_for_update())
    if not paper: raise ValueError('paper_not_found')
    job=session.scalar(select(WorkflowJob).where(WorkflowJob.job_id==body.job_id).with_for_update())
    external = bool(job and job.type == 'rebuild_external_extract' and
                    job.payload.get('protocol') == MCP_VERSION and job.payload.get('actor') == actor and
                    session.info.get(MCP_VERSION) == job.job_id)
    if not job or (job.type not in (TYPE,'ai_extract_rebuild') and not external) or job.payload.get('paper_id')!=str(paper_id):
        raise ValueError('reading_job_not_owned')
    data=body.model_dump(mode='json')
    abstract_only=body.save_mode=='abstract_only'
    if not abstract_only:
        # Preserve fingerprints of requests issued before the optional fields existed.
        data.pop('save_mode',None)
        if data.get('abstract_source_revision') is None:data.pop('abstract_source_revision',None)
    fingerprint=digest(data)
    # Request IDs may be 128 characters; legacy audit target_id is only 64.
    # Keep existing short identities and use the complete long identity's digest.
    audit_id=body.request_id if len(body.request_id)<=64 else hashlib.sha256(body.request_id.encode()).hexdigest()
    previous=session.scalar(select(AuditLog).where(AuditLog.action==VERSION,AuditLog.paper_id==paper_id,AuditLog.target_id==audit_id))
    if previous:
        if previous.payload['fingerprint']!=fingerprint or previous.payload['actor']!=actor: raise ValueError('reading_request_id_reused')
        return {**previous.payload['receipt'],'replayed':True}
    if job.status not in RUNNING: raise ValueError('reading_task_finished')
    if job.payload.get('reading_scope')=='abstract_only' and not abstract_only:
        raise ValueError('abstract_task_cannot_replace_guide')
    context=input_context(session,paper_id,settings)
    if context['input_revision']!=body.input_revision: raise ValueError('reading_inputs_changed_refresh_context')
    if abstract_only and job.type==TYPE and job.payload.get('input_revision')!=body.input_revision:
        raise ValueError('abstract_inputs_changed_since_dispatch')
    expected=job.payload.get('reading_revision')
    if expected and expected!=context['reading_revision']: raise ValueError('reading_content_changed_preserve_current')
    source=context['abstract_for_translation']
    if body.abstract_zh:
        if not source['text']:raise ValueError('verified_abstract_source_required')
        if body.abstract_source_revision is not None and body.abstract_source_revision!=source['revision']:
            raise ValueError('abstract_source_changed_or_unverified')
        if (abstract_only or source['source']['kind']=='main_text') and body.abstract_source_revision!=source['revision']:
            raise ValueError('verified_abstract_source_revision_required')
    if abstract_only and (paper.comprehensive_analysis or {}).get('abstract_zh') and not invalid_translation(paper):
        raise ValueError('existing_chinese_abstract_preserved')
    if not abstract_only and paper.abstract and not body.abstract_zh and not body.abstract_missing_reason:
        raise ValueError('abstract_translation_or_reason_required')
    if body.abstract_zh and not any('\u4e00' <= ch <= '\u9fff' for ch in body.abstract_zh):
        raise ValueError('chinese_abstract_required')
    old={'reading_guide':copy.deepcopy(paper.reading_guide),'abstract_zh':(paper.comprehensive_analysis or {}).get('abstract_zh')}
    if not abstract_only:
        docs=context['documents'];main=[d for d in docs if d['role']=='main'];si=[d for d in docs if d['role']=='si']
        if body.content_scope in ('main_text','main_and_si') and (not main or not all(d['complete_text'] for d in main)):
            raise ValueError('full_main_text_not_available')
        if body.content_scope=='main_and_si' and (not si or not all(d['complete_text'] for d in si)):
            raise ValueError('complete_si_text_not_available')
        assets={a['asset_id'] for a in context['figures']}
        if any(str(e.asset_id) not in assets for e in body.evidence):raise ValueError('evidence_asset_not_in_paper_context')
        missing=list(body.missing)
        if body.abstract_missing_reason: missing.append(body.abstract_missing_reason)
        for d in docs:
            if d.get('image_limit') and d['image_limit'] not in missing:missing.append(d['image_limit'])
            if d.get('reason') and d['reason'] not in missing:missing.append(d['reason'])
        abstract_source_sha256 = ((source.get('source') or {}).get('text_sha256') or
                                  hashlib.sha256((paper.abstract or '').encode()).hexdigest())
        guide={k:v for k,v in data.items() if k not in ('request_id','job_id','input_revision','abstract_zh','abstract_missing_reason','abstract_source_revision','save_mode')}
        guide.update(version=VERSION,updated_at=datetime.utcnow().isoformat(),missing=missing,
            source_files=[{k:d[k] for k in ('file_id','role','name','source_sha256','complete_text')} for d in docs],
            abstract_source_sha256=abstract_source_sha256,
            input_revision=body.input_revision,scientific_acceptance='not_implied')
        paper.reading_guide={**(paper.reading_guide or {}),'beginner_zh':guide}
    if body.abstract_zh:
        if invalid_translation(paper):
            history=list((paper.reading_guide or {}).get('abstract_translation_history') or [])
            history.append({'abstract_zh':old['abstract_zh'],'translation':copy.deepcopy((paper.reading_guide or {}).get('abstract_translation')),
                            'reason':'publisher_version_notice_not_research_abstract','superseded_at':datetime.utcnow().isoformat()})
            paper.reading_guide={**(paper.reading_guide or {}),'abstract_translation_history':history}
        paper.comprehensive_analysis={**(paper.comprehensive_analysis or {}),'abstract_zh':body.abstract_zh}
        paper.reading_guide={**(paper.reading_guide or {}),'abstract_translation':{
            'original_text':source['text'],'source':source['source'],'source_revision':source['revision'],
            'translation_sha256':hashlib.sha256(body.abstract_zh.encode()).hexdigest(),'supplement_only':abstract_only,
            'updated_at':datetime.utcnow().isoformat()}}
    receipt={'paper_id':str(paper_id),'job_id':job.job_id,'saved':True,'content_scope':(paper.reading_guide or {}).get('beginner_zh',{}).get('content_scope') if abstract_only else body.content_scope,
             'replayed':False,'reading_revision':reading_revision(paper)}
    session.add(AuditLog(action=VERSION,source=VERSION,paper_id=paper_id,target_type='paper_reading',target_id=audit_id,
        payload={'actor':actor,'fingerprint':fingerprint,'before':old,'after':{'reading_guide':paper.reading_guide,'abstract_zh':body.abstract_zh},'receipt':receipt}))
    job.progress={**(job.progress or {}),'reading_saved':True,'saved_receipt':receipt}
    session.flush();return receipt


async def reconcile(session, job, settings):
    from app.services.paper_db_phase import observe_job
    return await observe_job(session, job, settings, _apply_readback)


def _apply_readback(session, job, settings, status):
    from app.services.codex_web_dispatch import matches_owned_attempt
    from app.services.ordinary_parsing import recover_auto_started_turn
    tid=(job.progress or {}).get('thread_id')
    if not tid:return job
    if job.status not in RUNNING or status is None:return job
    progress=dict(job.progress or {})
    if not progress.get('turn_id') and (job.payload or {}).get('continuation'):
        from app.services.reading_thread_continuation import recover_turn
        recovered = recover_turn(job, status)
        if recovered: progress['turn_id'] = recovered
        else: return job
    if not progress.get('turn_id'):
        recovered=recover_auto_started_turn(job.payload,progress,status,settings)
        if recovered:progress.update(turn_id=recovered['turn_id'],identity_recovery=recovered)
    if not matches_owned_attempt(status,tid,progress.get('turn_id'),allow_goal_continuations=True):return job
    terminal=(status.goal_status in ('complete','completed','blocked','paused') or
              status.goal_observed and status.goal_status is None and status.thread_status in ('idle','notLoaded'))
    from app.services.paper_native_models import stage_fallback
    if (status.turn_id!=progress.get('turn_id') and status.goal_objective==(job.payload or {}).get('dispatch_objective')):
        progress.update(start_turn_id=progress.get('start_turn_id') or progress.get('turn_id'),turn_id=status.turn_id)
        job.progress=progress
    if stage_fallback(job,status):
        session.flush();return job
    if status.turn_status in ('completed','failed','interrupted') and terminal:
        job.status='completed' if progress.get('reading_saved') else 'failed'
        native_error = getattr(status, 'native_error', None)
        progress['native_error'] = native_error
        job.error = None if progress.get('reading_saved') else job.error if job.error and 'fallback_exhausted' in job.error else ('模型服务暂时限流，请稍后重试；已有资料保留' if native_error and '429' in native_error else '本次未保存中文阅读内容，已有资料保留，可单独重试'+('：'+native_error[:500] if native_error else ''))
        progress.update(native_terminal_verified=True,phase=job.status)
    else:job.status='running';progress['phase']='running'
    job.progress=progress;session.flush();return job


async def monitor_once(settings):
    from app.db.session import get_engine, session_scope
    from app.services.ai_extract_service import _background_dispatch
    from sqlalchemy.orm import Session
    with session_scope(settings.database_url) as s:
        ids=list(s.scalars(select(WorkflowJob.job_id).where(WorkflowJob.type==TYPE,WorkflowJob.status.in_(RUNNING)).order_by(WorkflowJob.updated_at).limit(5)))
    engine = get_engine(settings.database_url)
    for jid in ids:
        with session_scope(settings.database_url) as s:
            job=s.get(WorkflowJob,jid)
            if job is None:
                continue
            p=dict(job.payload or {})
            unsent=not (job.progress or {}).get('dispatch_started')
            s.expunge(job)
            # No database transaction remains open while the native thread is read.
        if not unsent:
            with Session(bind=engine, autoflush=False, expire_on_commit=False) as phase_session:
                await reconcile(phase_session, job, settings)
        if unsent:await _background_dispatch(job_id=jid,database_url=settings.database_url,prompt=p['dispatch_prompt'],objective=p['dispatch_objective'],settings=settings,primary_model=p['chosen_model'],attempt_token=p['attempt_token'])
