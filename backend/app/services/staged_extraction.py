"""Bounded stage indexes and append-only, version-bound check declarations.

Nothing dispatches, retries, accepts, or writes scientific entities here.
"""
from collections import defaultdict
from uuid import uuid4
from urllib.parse import urlencode
from sqlalchemy import select,text,cast,Integer,inspect
from app.db.models import AuditLog,RebuildDataRow,RebuildDataValue,RebuildValueSource,RebuildVisualAsset
from app.services.extraction_coverage import _digest,latest as latest_plan
from app.services.rebuild_mutation_audit import EVENT as WRITE_EVENT,RECEIPT as WRITE_RECEIPT,VERSION as WRITE_VERSION,safe_json
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.config import get_settings

VERSION='staged_extraction_checks_v1'
EVENT='staged_extraction_check_recorded'
STAGES=('source_scope','coverage','extract','reconcile','handoff')
LABELS={'source_scope':'来源范围','coverage':'原文清单','extract':'提取与补项','reconcile':'结果核对','handoff':'交接准备'}
SCIENCE_ACTIONS=(WRITE_EVENT,WRITE_RECEIPT,'section_imported','section_import_request','section_boundary_corrected','section_correction_request')


def contract(paper_id=None):
    base=f'/api/rebuild/papers/{paper_id or "{paper_id}"}'
    return {'version':VERSION,'package_endpoint':base+'/staged-work-package?stage=source_scope&limit=25',
        'checks_endpoint':base+'/extraction-checks','preview_endpoint':base+'/extraction-checks/preview',
        'stages':list(STAGES),'rule':'Stages are read indexes/advice, not an execution engine. Read only the current contract refs and linked bounded evidence. No missing scientific measurement is invented.',
        'check':'Save an explicit source_scope/item_keys and review_statement with expected_binding, expected_check_revision and request_id. Program checks and scoped scientific declarations remain separate; no independent scientific verification or accepted state is implied.',
        'unknown':'Preserve the original import body/ID. unknown_requests requires the original run_audited request fingerprint; Known unknowns cannot disappear by omission or actor switching. Only a matching real receipt establishes its outcome; partial failures still need readback/recovery.',
        'replay':'Same body/ID/actor returns the original checkpoint receipt, never proof of current freshness. GET recomputes stale; source/output/plan/receipt changes invalidate old conclusions.',
        'scientific_acceptance':'not_implied'}


def _claim_key(item):return (item['operation'],item['request_id'],item['request_sha256'])

def context(session,paper_id,snapshot,actor):
    plan=latest_plan(session,paper_id)
    records=list(session.scalars(select(AuditLog).where(AuditLog.paper_id==paper_id,AuditLog.action==EVENT,AuditLog.source==VERSION)
        .distinct(AuditLog.target_type).order_by(AuditLog.target_type,cast(AuditLog.payload['generation'].astext,Integer).desc(),AuditLog.id.desc())))
    newest=max(records,key=lambda e:e.payload['generation']) if records else None
    claims=defaultdict(dict)
    for row in session.execute(select(AuditLog.payload['actor'].astext,AuditLog.payload['request']['unknown_requests']).where(
            AuditLog.paper_id==paper_id,AuditLog.action==EVENT,AuditLog.source==VERSION)):
        for claim in row[1] or []:claims[row[0]][_claim_key(claim)]=claim
    receipts=list(session.execute(text("""
      SELECT id, encode(sha256(convert_to((to_jsonb(a))::text,'UTF8')),'hex') AS digest
      FROM audit_logs a WHERE paper_id=:paper_id AND action = ANY(:actions) ORDER BY id
    """),{'paper_id':paper_id,'actions':list(SCIENCE_ACTIONS)}))
    binding={'output_revision':snapshot['revision'],
        'source_revision':_digest({k:snapshot.get(k) for k in ('files','file_reading_identity','unregistered_files')}),
        'receipt_revision':_digest([(str(r.id),r.digest) for r in receipts]),
        'plan_revision':plan.payload['plan_revision'] if plan else None,'plan_generation':plan.payload['generation'] if plan else None,
        'known_unknown_revision':_digest(sorted((user,*key) for user,items in claims.items() for key in items))}
    return {'snapshot':snapshot,'plan':plan,'latest':newest,'records':records,'claims':claims,'binding':binding,
            'check_revision':newest.payload['record_revision'] if newest else None,'actor':actor}


def _fresh(event,ctx):
    return event.payload['binding']==ctx['binding']


def record_summary(event,ctx,*,full=False):
    data=event.payload
    result={'id':str(event.id),'stage':data['stage'],'stage_label':LABELS[data['stage']], 'actor':data['actor'],
        'created_at':event.created_at.isoformat(),'record_revision':data['record_revision'],
        'freshness':'current' if _fresh(event,ctx) else 'stale','assessment_as_recorded':{k:data['assessment'][k] for k in ('program_status','advice_zh','scientific_acceptance')},
        'review_scope':{'files':len(data['request']['source_scope']),'items':len(data['request']['item_keys'])},
        'scientific_declaration':'not_independently_verified','scientific_acceptance':'not_implied'}
    if full:result.update(binding=data['binding'],request=data['request'],assessment_as_recorded=data['assessment'])
    return result


def _audits(session,paper_id):
    records=list(session.scalars(select(AuditLog).where(AuditLog.paper_id==paper_id,AuditLog.action.in_(SCIENCE_ACTIONS))))
    receipts={str(r.id):r for r in records if r.action==WRITE_RECEIPT and r.source==WRITE_VERSION}
    receipt_keys={(r.target_type,r.target_id,r.payload.get('actor')):r for r in receipts.values()}
    return records,receipts,receipt_keys


def assess(session,paper_id,ctx,*,source_scope=(),item_keys=(),receipt_ids=(),unknown_requests=(),statement=None):
    from app.services.extraction_coverage import report as coverage_report,_fact_problems
    from app.services.rebuild_quality import quality_report
    snapshot=ctx['snapshot'];physical=snapshot.get('file_reading_identity',{})
    coverage=coverage_report(session,paper_id,snapshot,limit=None)
    quality=quality_report(session,paper_id,get_settings(),snapshot,limit=100000,coverage_plan_report=coverage)
    references=[];scope_pages=defaultdict(set)
    for scope in source_scope:
        actual=physical.get(scope['file_id'])
        if not actual or actual['status']!='verified' or any(scope[k]!=actual.get(k) for k in ('source_sha256','reading_pdf_sha256')):
            references.append({'code':'check_source_identity_mismatch','file_id':scope['file_id']});continue
        if not scope['locator'].strip() or any(type(p) is not int or p<1 or p>actual['physical_pages'] for p in scope['pages']):
            references.append({'code':'check_source_scope_invalid','file_id':scope['file_id']});continue
        scope_pages[scope['file_id']].update(scope['pages'])
    plan_keys={i['item_key'] for i in coverage['items']}
    if len(set(item_keys))!=len(item_keys) or set(item_keys)-plan_keys:references.append({'code':'check_item_scope_not_in_plan'})
    audits,receipts,receipt_keys=_audits(session,paper_id)
    if len(set(receipt_ids))!=len(receipt_ids) or any(key not in receipts for key in receipt_ids):references.append({'code':'check_receipt_not_in_paper'})
    claim_map=dict(ctx['claims'].get(ctx['actor'],{}))
    for claim in unknown_requests:
        if not claim['reason'].strip():references.append({'code':'check_unknown_reason_blank'})
        claim_map[_claim_key(claim)]=claim
    unresolved=[]
    all_claims={**ctx['claims'],ctx['actor']:claim_map}
    for owner,claims in all_claims.items():
        for key,claim in claims.items():
            receipt=receipt_keys.get((claim['operation'],claim['request_id'],owner))
            if receipt is None or receipt.payload.get('request_sha256')!=claim['request_sha256']:
                unresolved.append({'actor':owner,'operation':claim['operation'],'request_id':claim['request_id'],'reason':'exact_original_receipt_not_confirmed'})
    current={};values={};rows={};sources_by_value=defaultdict(list)
    for model in (RebuildDataRow,RebuildDataValue,RebuildValueSource,RebuildVisualAsset):
        for row in session.scalars(select(model).where(model.paper_id==paper_id)):
            current[(model.__tablename__,str(row.id))]=safe_json({c.key:getattr(row,c.key) for c in inspect(model).column_attrs})
            if model is RebuildDataRow:rows[str(row.id)]=row
            elif model is RebuildDataValue:values[(str(row.row_id),row.field_name)]=row
            elif model is RebuildValueSource:sources_by_value[str(row.value_id)].append(row)
    proved={}
    for event in audits:
        if event.action!=WRITE_EVENT or event.source!=WRITE_VERSION:continue
        data=event.payload;receipt=receipt_keys.get((data.get('operation'),data.get('request_id'),data.get('actor')))
        if receipt is None or str(event.id) not in receipt.payload.get('result',{}).get('audit',{}).get('event_ids',[]):continue
        key=(event.target_type,event.target_id)
        if key in current and data.get('after')==current[key]:proved[key]=receipt
    partial=[]
    for receipt in receipts.values():
        result=receipt.payload.get('result',{})
        for error in result.get('errors',[]):
            row=rows.get(error.get('row_id'));value=values.get((error.get('row_id'),error.get('field')))
            proof=proved.get((RebuildDataValue.__tablename__,str(value.id))) if value else None
            repaired=bool(row and value and proof and proof.created_at>receipt.created_at and
                proof.id!=receipt.id and proof.payload.get('outcome')=='succeeded' and
                not (row.properties or {}).get('_rebuild_import',{}).get('pending_identity_fields') and
                not _fact_problems(value,sources_by_value[str(value.id)]))
            if not repaired:partial.append({'receipt_id':str(receipt.id),'request_id':receipt.target_id,'row_index':error.get('row_index'),
                                          'row_key':error.get('row_key'),'field':error.get('field'),'reason':error.get('error') or error.get('outcome')})
        if (result.get('complete') is False or receipt.payload.get('outcome')!='succeeded') and not result.get('errors'):partial.append({'receipt_id':str(receipt.id),'reason':'incomplete_receipt'})
    unproved=[{'kind':kind,'id':key} for kind,key in current if (kind,key) not in proved]
    # Chapters use their existing immutable source/receipt protocol. The quality
    # checker validates the stored text, page identity and physical source hash.
    from app.services.rebuild_sections import _checked_snapshot,EVENT as SECTION_EVENT,VERSION as SECTION_VERSION
    section_events={e.target_id:e for e in audits if e.action==SECTION_EVENT and e.source==SECTION_VERSION and e.target_type=='paper_section'}
    section_receipts={(r.target_id,r.payload.get('actor'),r.payload.get('request_sha256')):r for r in audits
                      if r.action=='section_import_request' and r.source==SECTION_VERSION}
    from app.db.models import PaperSection
    sections=list(session.scalars(select(PaperSection).where(PaperSection.paper_id==paper_id)))
    for section in sections:
        event=section_events.get(str(section.id))
        try:
            _checked_snapshot(section,event)
            receipt=section_receipts.get((event.payload.get('request_id'),event.payload.get('actor'),event.payload.get('request_sha256')))
            if receipt is None or str(section.id) not in {i['section_id'] for i in receipt.payload.get('result',{}).get('items',[])}:
                raise RebuildWorkflowError('section_receipt_unverified')
        except RebuildWorkflowError:unproved.append({'kind':'paper_sections','id':str(section.id)})
    findings=[]
    def finding(code,count=1):
        if count:findings.append({'code':code,'count':int(count)})
    finding('source_files_missing',not physical)
    finding('main_source_not_registered',bool(physical) and not any(f['role']=='main' for f in physical.values()))
    finding('source_material_unverified',sum(f['status']!='verified' for f in physical.values())+len(snapshot.get('unregistered_files',[])))
    finding('coverage_plan_missing',coverage['legacy_without_plan'])
    finding('coverage_scope_unknown',coverage['summary']['scope_unknown'])
    finding('coverage_items_missing',coverage['summary']['counts'].get('pending',0)+coverage['summary']['counts'].get('unknown',0))
    finding('coverage_items_empty',not coverage['summary']['total'])
    finding('coverage_check_stale',coverage['summary']['stale_items'])
    relevant=[i for i in quality['issues'] if i['code']!='file_extraction_coverage_unknown' and i['severity'] in {'error','unknown'} and not i['code'].startswith('coverage_plan_')]
    finding('stored_integrity_needs_review',len(relevant))
    finding('quality_check_truncated',quality['issues_total']>len(quality['issues']))
    finding('current_output_receipt_unverified',len(unproved))
    finding('no_current_scientific_output',not current and not sections)
    finding('import_partial_or_recovery_unresolved',len(partial));finding('write_result_unknown',len(unresolved))
    if statement is not None:
        finding('review_scope_incomplete',sum(not scope_pages.get(fid) or len(scope_pages[fid])!=f.get('physical_pages') for fid,f in physical.items()))
        finding('review_items_not_all_declared',set(item_keys)!=plan_keys or not plan_keys)
        if not statement.strip():references.append({'code':'check_review_statement_blank'})
    if statement is None:
        checked=any(_fresh(e,ctx) and e.payload['actor']==ctx['actor'] and e.payload['stage'] in {'reconcile','handoff'}
            and e.payload['assessment']['program_status']=='scoped_handoff_candidate' for e in ctx['records'])
        finding('current_scoped_check_missing',not checked)
    finding('check_references_invalid',len(references))
    ready=not findings
    next_stage=('source_scope' if any(f['code'].startswith(('source_','main_')) for f in findings) else
                'coverage' if coverage['legacy_without_plan'] or coverage['summary']['scope_unknown'] or not coverage['summary']['total'] else
                'extract' if coverage['summary']['counts'].get('pending') or coverage['summary']['counts'].get('unknown') or partial or unresolved else
                'reconcile' if findings else 'handoff')
    result={'program_status':'scoped_handoff_candidate' if ready else 'open_items', 'findings':findings,
        'next_stage':next_stage,'next_label':LABELS[next_stage],
        'advice_zh':'当前声明范围的程序关联检查未发现阻碍，可携范围交接复核；不代表全篇提取成功或科学验收通过。' if ready else
                    f'建议继续{LABELS[next_stage]}，先处理明确缺项或未核实回执；既有文献的科学结论不因此被否定。',
        'coverage':{k:coverage['summary'][k] for k in ('status','total','counts','stale_items','scope_unknown')},'receipt_scope':{'current_entities':len(current),'proved_entities':len(proved),
             'unverified_total':len(unproved),'unverified_sample':unproved[:20],'partial_unresolved_total':len(partial),
             'partial_sample':partial[:20],'unknown_total':len(unresolved),'unknown_sample':unresolved[:20]},
        'scientific_declaration':'not_independently_verified','scientific_acceptance':'not_implied',
        'formal_acceptance_scope':'separate; not activated or changed',
        'references_valid':not references,'reference_errors':references}
    binding={**ctx['binding'],'known_unknown_revision':_digest(sorted((user,*key) for user,items in all_claims.items() for key in items))}
    return result,binding


def save_check(session,paper_id,payload,ctx,actor):
    submitted=payload.model_dump(mode='json',exclude_unset=True);body=payload.model_dump(mode='json');fingerprint=_digest(submitted)
    previous=session.scalar(select(AuditLog).where(AuditLog.paper_id==paper_id,AuditLog.action==EVENT,AuditLog.source==VERSION,AuditLog.target_id==payload.request_id))
    if previous:
        if previous.payload['request_sha256']!=fingerprint or previous.payload['actor']!=actor:raise RebuildWorkflowError('stage_request_id_reused')
        return {**previous.payload['receipt'],'replayed':True},None
    if body['expected_check_revision']!=ctx['check_revision']:raise RebuildWorkflowError('stage_check_revision_changed')
    if body['expected_binding']!=ctx['binding']:raise RebuildWorkflowError('stage_binding_changed')
    assessment,binding=assess(session,paper_id,ctx,source_scope=body['source_scope'],item_keys=body['item_keys'],receipt_ids=body['receipt_ids'],unknown_requests=body['unknown_requests'],statement=body['review_statement'])
    if not assessment['references_valid']:return None,assessment
    generation=ctx['latest'].payload['generation']+1 if ctx['latest'] else 1
    revision=_digest([generation,actor,fingerprint]);event_id=uuid4()
    receipt={'paper_id':str(paper_id),'record_id':str(event_id),'record_revision':revision,'binding_as_recorded':binding,
             'assessment_as_recorded':assessment,'replayed':False,'metadata_only':True,
             'notice':'This is the original scoped declaration/check receipt, not a claim about current state. Read freshness before resuming.',
             'scientific_acceptance':'not_implied'}
    session.add(AuditLog(id=event_id,paper_id=paper_id,action=EVENT,source=VERSION,target_type=body['stage'],target_id=body['request_id'],
        payload={'generation':generation,'record_revision':revision,'actor':actor,'request_sha256':fingerprint,'request':body,
                 'stage':body['stage'],'binding':binding,'assessment':assessment,'receipt':receipt}))
    session.flush()
    return receipt,assessment


def package(session,paper_id,ctx,stage,view='items',offset=0,limit=25,expected_revision=None):
    snapshot=ctx['snapshot'];base=f'/api/rebuild/papers/{paper_id}'
    if view!='items' and stage!='extract':raise RebuildWorkflowError('stage_view_invalid')
    revision=_digest([ctx['binding'],ctx['check_revision'],stage,view])
    if offset and expected_revision is None:raise RebuildWorkflowError('stage_page_revision_required')
    if expected_revision is not None and expected_revision!=revision:raise RebuildWorkflowError('stage_page_revision_changed')
    result={'paper_id':str(paper_id),'stage':stage,'view':view,'stage_label':LABELS[stage],'revision':revision,'binding':ctx['binding'],
            'check_revision':ctx['check_revision'],'read_only':True,
            'stage_index':[{'stage':key,'label':LABELS[key],'endpoint':base+'/staged-work-package?stage='+key+'&limit=25'} for key in STAGES],'scientific_acceptance':'not_implied',
            'latest_checks':[record_summary(r,ctx) for r in ctx['records']],
            'evidence_rule':'Index and locators only, never a truncated substitute for original text. Use linked revision-bound source/output endpoints.'}
    refs={'source_scope':['physical file SHA/reading SHA/physical page identity'],
          'coverage':['producer_contract.coverage_plan'],
          'extract':['producer_contract.row_import','table_write_contract','section_write_contract (DS only)'],
          'reconcile':['quality_endpoint','producer_contract.row_import.recovery'],
          'handoff':['scoped declarations and actual receipt evidence; independent science review remains separate']}
    result['contract_refs']=refs[stage]
    result['resources']={'checks':base+'/extraction-checks','coverage':base+'/coverage-plan?limit=25',
        'quality':base+'/quality?expected_revision='+snapshot['revision'],
        'text_pages':'/api/rebuild/files/{file_id}/text-pages?offset=0&limit=5','page_image':'/api/rebuild/files/{file_id}/render-page?page=1&dpi=120'}
    if view.endswith('_contract'):
        from app.services.rebuild_quality import producer_contract
        from app.services.rebuild_table_content import table_write_contract
        from app.services.rebuild_sections import section_write_contract
        result['contract']=(producer_contract()['row_import'] if view=='rows_contract' else
                            table_write_contract() if view=='table_contract' else section_write_contract(paper_id))
        items=[]
        result['advice_zh']='只读取本次写入所需合同；章节提取继续遵守DS限定，不调度任务。'
    elif stage=='source_scope':
        items=sorted(snapshot.get('file_reading_identity',{}).values(),key=lambda f:f['file_id'])
        result['advice_zh']='先核对正文与SI的实际文件/物理页范围；文本层为空不代表没有数据，不启动重解析。'
    elif stage in {'coverage','extract'} and view=='items':
        from app.services.extraction_coverage import report
        plan=report(session,paper_id,snapshot,limit=None)
        selected=plan['items'] if stage=='coverage' else [i for i in plan['items'] if i['effective_state'] in {'pending','unknown'}]
        items=[{k:i[k] for k in ('item_key','title','kind','origin','state','effective_state','freshness','reason')} |
               {'source':{k:i['source'].get(k) for k in ('file_id','pages','locator')},
                'output_endpoint':base+f'/coverage-plan/items/{i["item_key"]}/output?expected_revision={plan["revision"]}' if i.get('link') else None} for i in selected]
        if stage=='coverage':
            from app.services.extraction_coverage import contract as coverage_contract
            result['contract']=coverage_contract()
        result['coverage_summary']=plan['summary'];result['legacy_without_plan']=plan['legacy_without_plan']
        result['advice_zh']='先依据原文声明对象和材料条件，未知不补猜值。' if plan['legacy_without_plan'] else '只补明确未处理/待核实项；原文未报告与不适用分别保留理由，图像不能被章节文字抵消。'
        result['resources']['contracts']={kind:base+'/staged-work-package?stage=extract&view='+kind+'_contract' for kind in ('rows','table','section')}
        result['resources']['import_receipts']=base+'/staged-work-package?stage=extract&view=receipts&limit=25'
    elif stage=='extract' and view=='receipts':
        # Metadata headers only. No scientific graph/body is copied to this page.
        receipt_total=session.scalar(text('SELECT count(*) FROM audit_logs WHERE paper_id=:paper_id AND action=:action AND source=:source'),
            {'paper_id':paper_id,'action':WRITE_RECEIPT,'source':WRITE_VERSION})
        items=[dict(r) for r in session.execute(text("""
            SELECT id::text AS receipt_id,target_type AS operation,target_id AS request_id,
                   payload->>'actor' AS actor,payload->>'outcome' AS outcome,
                   payload->>'request_sha256' AS request_sha256, payload->'result'->'complete' AS complete,
                   jsonb_array_length(coalesce(payload->'result'->'errors','[]'::jsonb)) AS errors_total
            FROM audit_logs WHERE paper_id=:paper_id AND action=:action AND source=:source ORDER BY created_at,id LIMIT :limit OFFSET :offset
        """),{'paper_id':paper_id,'action':WRITE_RECEIPT,'source':WRITE_VERSION,'limit':limit,'offset':offset}).mappings()]
        for item in items:item['errors_endpoint']=base+'/extraction-import-receipts/'+item['receipt_id']+'?limit=25'
        result['advice_zh']='核对真实回执及complete/errors；历史回执不能证明新输出，结果未知只能原体原ID查回，明确失败后新ID补交。'
    else:
        assessment,_=assess(session,paper_id,ctx)
        if stage=='reconcile':
            from app.services.rebuild_quality import producer_contract
            result['recovery_contract']=producer_contract()['row_import']['recovery']
        else:result['check_contract']=contract(paper_id)
        result['assessment']=assessment;result['advice_zh']=assessment['advice_zh']
        items=assessment['findings']
    receipt_page=stage=='extract' and view=='receipts'
    total=receipt_total if receipt_page else len(items);next_offset=offset+limit if offset+limit<total else None
    result.update(items=items if receipt_page else items[offset:offset+limit],total=total,offset=offset,limit=limit,has_more=next_offset is not None,next_offset=next_offset,
        next_page=base+'/staged-work-package?'+urlencode({'stage':stage,'view':view,'offset':next_offset,'limit':limit,'expected_revision':revision}) if next_offset is not None else None)
    return result



def receipt_errors(session,paper_id,receipt_id,*,offset=0,limit=25,expected_revision=None):
    record=session.scalar(select(AuditLog).where(AuditLog.id==receipt_id,AuditLog.paper_id==paper_id,
        AuditLog.action==WRITE_RECEIPT,AuditLog.source==WRITE_VERSION))
    if record is None:raise RebuildWorkflowError('stage_import_receipt_not_found')
    revision=_digest([str(record.id),record.payload]);data=record.payload;result=data.get('result',{})
    if offset and expected_revision is None:raise RebuildWorkflowError('stage_page_revision_required')
    if expected_revision is not None and expected_revision!=revision:raise RebuildWorkflowError('stage_page_revision_changed')
    errors=result.get('errors',[]);next_offset=offset+limit if offset+limit<len(errors) else None
    return {'paper_id':str(paper_id),'receipt_id':str(record.id),'request_id':record.target_id,'operation':record.target_type,
        'actor':data['actor'],'outcome_as_recorded':data['outcome'],'complete_as_recorded':result.get('complete'),
        'request_sha256':data['request_sha256'],'revision':revision,'errors':errors[offset:offset+limit],
        'errors_total':len(errors),'offset':offset,'limit':limit,'has_more':next_offset is not None,'next_offset':next_offset,
        'notice':'Original receipt errors only, never current recovery/completeness. Reconcile reads back current entities and later receipts.',
        'read_only':True,'scientific_acceptance':'not_implied'}
