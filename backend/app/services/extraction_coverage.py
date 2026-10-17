"""Source declarations and exact output correspondence. No scientific writes.

Audit-only metadata is deliberately excluded from the scientific output hash.
No read creates a plan, task, acceptance receipt, cache or scientific output.
"""
from collections import Counter, defaultdict
from uuid import UUID, uuid4
from urllib.parse import urlencode
from sqlalchemy import Integer, cast, select, text
from sqlalchemy.orm import load_only
from app.db.models import (AuditLog, Paper, PaperSection, RebuildDataRow, RebuildDataValue,
                           RebuildValueSource, RebuildVisualAsset)
import hashlib
import json
from app.services.rebuild_workflow_service import RebuildWorkflowError, IDENTITY_FIELDS, _stored_identity

def _digest(value):
    # Exact finite JSON fingerprint. Do not redact keys before hashing; an
    # arbitrary condition name must not make two different requests collide.
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',', ':'),allow_nan=False).encode()).hexdigest()

VERSION = 'extraction_coverage_v1'
EVENT = 'extraction_coverage_plan_saved'


def contract(paper_id=None):
    base = f'/api/rebuild/papers/{paper_id or "{paper_id}"}/coverage-plan'
    return {'version': VERSION, 'read_endpoint': base, 'preview_endpoint': base + '/preview', 'save_endpoint': base, 'output_endpoint':base+'/items/{item_key}/output?expected_revision={revision}',
            'kinds': ['row', 'value', 'table', 'figure', 'subfigure', 'section'],
            'states': ['pending', 'extracted', 'not_reported', 'unknown', 'not_applicable'],
            'rule': 'Read original main/SI PDFs and declare only evidenced objects/material-condition combinations; never infer all objects from counts, keywords, cache inventories or existing outputs. Tables and scientific fact rows are separate expected items.',
            'write': 'Full versioned replacement with expected_plan_revision (null initially), expected_output_revision, request_id, file hashes/pages/scope_note and stable item_key/source/identity/condition. Preview first. Only metadata event is saved; no science/task/gate writes.',
            'links': 'Removed item keys stay reserved against all saved history. row means at least one legal, conflict-free matching fact on every declared source page, not completion of all row properties. extracted requires exact same-paper/file/pages output IDs; value requires row_id/value_id/source_id, field_name and declared material/condition. Tables require formal representation; figures/subfigures require image and interpretation. A link is not scientific acceptance.',
            'absence': 'Every non-extracted state needs structured reason. not_reported needs checked_pages+method, not an impossible quote saying absence. Missing links never infer not_reported. Unknown is not zero.',
            'revisions': 'Plan version and output revision are separate. Changed output/source makes old checks stale. Follow revision-pinned pages through has_more=false; aggregate summary always covers the full plan.',
            'scientific_acceptance': 'not_implied'}


def latest(session, paper_id):
    return session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == EVENT,
        AuditLog.source == VERSION).order_by(cast(AuditLog.payload['generation'].astext, Integer).desc(), AuditLog.id.desc()).limit(1))



def _identity_signature(item):
    return _digest({**{k:item.get(k) for k in ('kind','identity','condition','field_name','label','subfigure_label')},
                    'file_id':item['source']['file_id']})


def historical_identities(session, paper_id):
    # One compact JSON projection for the full history, not a query per item or
    # a transfer of historical quotes/table bodies. Removed keys remain reserved.
    result=defaultdict(set)
    records=session.execute(text("""
        SELECT item->>'item_key' AS item_key,
               jsonb_build_object('kind',item->'kind','identity',item->'identity',
                 'condition',item->'condition','field_name',item->'field_name',
                 'label',item->'label','subfigure_label',item->'subfigure_label',
                 'file_id',item->'source'->'file_id') AS identity
        FROM audit_logs a CROSS JOIN LATERAL jsonb_array_elements(a.payload->'plan'->'items') AS x(item)
        WHERE a.paper_id=:paper_id AND a.action=:action AND a.source=:source
    """),{'paper_id':paper_id,'action':EVENT,'source':VERSION})
    for record in records:result[record.item_key].add(_digest(record.identity))
    return result


def _fact_problems(value, evidence):
    from app.services.rebuild_workflow_service import _validate_value, serialize_source
    problems=[]
    try:
        _validate_value({**{k:getattr(value,k) for k in ('raw_value','numeric_value','unit','value_type','precision_digits','missing_reason')},
                         'sources':[serialize_source(s) for s in evidence]})
    except RebuildWorkflowError:problems.append('stored_value_contract_invalid')
    conflict=value.conflict if isinstance(value.conflict,dict) else {}
    if conflict.get('status') in {'value_conflict','unit_conflict','pending','unresolved'} or conflict.get('legacy_unresolved') in {'value_conflict','unit_conflict'}:
        problems.append('value_conflict_unresolved')
    return problems


def _graph(session, paper_id):
    def records(model, options=()):
        statement = select(model).where(model.paper_id == paper_id)
        if options: statement = statement.options(*options)
        return {str(row.id): row for row in session.scalars(statement)}
    rows = records(RebuildDataRow); values = records(RebuildDataValue); sources = records(RebuildValueSource)
    assets = records(RebuildVisualAsset)
    sections = records(PaperSection, (load_only(PaperSection.id, PaperSection.paper_id, *[
        getattr(PaperSection, field) for field in ('section_title','section_type','text','page_start','page_end',
        'section_level','section_number','parent_heading','heading_path')]),))
    from app.services.rebuild_sections import EVENT as SECTION_EVENT, VERSION as SECTION_VERSION
    section_events = {event.target_id: event for event in session.scalars(select(AuditLog).where(
        AuditLog.paper_id == paper_id, AuditLog.action == SECTION_EVENT, AuditLog.source == SECTION_VERSION)
        .order_by(AuditLog.created_at, AuditLog.id))}
    by_row, by_value = defaultdict(list), defaultdict(list)
    for value in values.values(): by_row[str(value.row_id)].append(value)
    for source in sources.values(): by_value[str(source.value_id)].append(source)
    return rows, values, sources, assets, sections, section_events, by_row, by_value


def _item_problems(item, graph, snapshot, identity_cache):
    rows, values, sources, assets, sections, section_events, by_row, by_value = graph
    link, source = item.get('link'), item['source']
    if item['state'] != 'extracted': return []
    errors = []
    def require(condition, code):
        if not condition: errors.append(code)
    require(item['origin'] != 'image_pending', 'image_processing_still_pending')
    allowed = {'row':{'row_id','row_key'}, 'value':{'row_id','row_key','value_id','source_id'},
               'table':{'asset_id','asset_key'}, 'figure':{'asset_id','asset_key'},
               'subfigure':{'asset_id','asset_key'}, 'section':{'section_id'}}[item['kind']]
    require(not any(value is not None and key not in allowed for key,value in link.items()), 'link_kind_mismatch')
    def source_matches(record,value):
        if record.value_id != value.id or record.row_id != value.row_id or record.paper_id != value.paper_id:
            return False
        asset = assets.get(str(record.asset_id)) if record.asset_id else None
        file_id = record.file_id or (asset.file_id if asset else None)
        if record.asset_id and (asset is None or record.file_id and str(asset.file_id) != str(record.file_id)):
            return False
        label = record.label or (asset.figure_label if asset else None)
        if item.get('label') is not None and item['label'] != label:
            return False
        if item.get('subfigure_label') is not None and (asset is None or item['subfigure_label'] != asset.subfigure_label):
            return False
        return (str(file_id) == source['file_id'] and record.page_number in source['pages'] and
                (source.get('table_row') is None or source['table_row'] == record.table_row) and
                (source.get('table_column') is None or source['table_column'] == record.table_column))
    if item['kind'] not in {'row','value'}:
        require(item['origin']=='reported', 'nonfact_origin_unverifiable')
    if item['kind'] in {'row', 'value'}:
        row = rows.get(link.get('row_id'))
        if row is None: return ['row_not_in_this_paper']
        require(not link.get('row_key') or link['row_key'] == row.row_key, 'canonical_row_key_mismatch')
        try:
            if str(row.id) not in identity_cache:
                identity_cache[str(row.id)] = _stored_identity(None, row, values=by_row[str(row.id)])
            identity = identity_cache[str(row.id)]
            require(all(identity['identity'].get(k) == v for k,v in item['identity'].items()), 'material_identity_mismatch')
            require(all(k in identity['condition'] and _digest(identity['condition'][k]) == _digest(v) for k,v in item['condition'].items()), 'condition_mismatch')
        except RebuildWorkflowError: errors.append('stored_identity_unresolved')
        require(not (row.properties or {}).get('_rebuild_import', {}).get('pending_identity_fields'), 'row_identity_pending')
        if item['kind'] == 'value':
            value = values.get(link.get('value_id')); evidence = sources.get(link.get('source_id'))
            if value is None or str(value.row_id) != str(row.id): return errors + ['value_row_mismatch']
            require(value.field_name == item['field_name'], 'value_field_mismatch')
            require(value.value_type == {'reported':'explicit','estimated':'estimated','derived':'derived'}.get(item['origin']), 'value_origin_mismatch')
            require(evidence is not None and str(evidence.value_id) == str(value.id) and str(evidence.row_id) == str(row.id)
                    and source_matches(evidence,value), 'value_source_file_page_or_cell_mismatch')
            require(set(source['pages']) <= {s.page_number for s in by_value[str(value.id)] if source_matches(s,value)}, 'value_evidence_pages_incomplete')
            errors.extend(_fact_problems(value,by_value[str(value.id)]))
        else:
            # A row item means existence of at least one legal matching fact,
            # never completion of every property. All declared source pages
            # must be supported by legal facts; bad siblings cannot prove it.
            legal_pages=set()
            for value in by_row[str(row.id)]:
                evidence=by_value[str(value.id)]
                if value.value_type != {'reported':'explicit','estimated':'estimated','derived':'derived'}.get(item['origin']):continue
                if _fact_problems(value,evidence):continue
                legal_pages.update(s.page_number for s in evidence if source_matches(s,value))
            require(set(source['pages']) <= legal_pages, 'row_has_no_legal_matching_sourced_fact')
    elif item['kind'] in {'table', 'figure', 'subfigure'}:
        asset = assets.get(link.get('asset_id'))
        if asset is None: return ['asset_not_in_this_paper']
        require(asset.asset_type == item['kind'], 'asset_type_mismatch')
        require(str(asset.file_id) == source['file_id'], 'asset_file_mismatch')
        require(set(source['pages']) <= set(asset.page_numbers or []), 'asset_page_mismatch')
        require(not link.get('asset_key') or link['asset_key'] == asset.asset_key, 'asset_key_mismatch')
        require(not item.get('label') or item['label'] == asset.figure_label, 'asset_label_mismatch')
        require(not item.get('subfigure_label') or item['subfigure_label'] == asset.subfigure_label, 'subfigure_label_mismatch')
        # Material/condition-specific figures must declare an exact mapping;
        # free caption matching never borrows another sample's image.
        mapping = asset.material_mapping if isinstance(asset.material_mapping,dict) else {}
        require(all(k in mapping and mapping[k] == v for k,v in item['identity'].items()), 'asset_material_identity_mismatch')
        mapped_condition = mapping.get('condition') if isinstance(mapping.get('condition'),dict) else {}
        require(all(k in mapped_condition and _digest(mapped_condition[k]) == _digest(v) for k,v in item['condition'].items()), 'asset_condition_mismatch')
        if item['kind'] == 'table':
            table = snapshot['coverage_table_map'].get(str(asset.id))
            require(table is not None and table['complete'] and table.get('file_id') == source['file_id'] and
                    set(source['pages']) <= set(table.get('physical_pages') or []), 'formal_table_representation_not_ready')
            if item.get('expected_columns') is not None:
                from app.services.rebuild_table_content import table_content
                content = table_content(asset)
                require(content is not None and content['format'] == 'structured' and
                        content['data']['columns'] == item['expected_columns'], 'table_columns_mismatch_or_unverified')
        else:
            from app.services.rebuild_workflow_service import resolve_asset_image_path
            from app.services.rebuild_acceptance import hash_bound_file
            from app.config import get_settings
            try: hash_bound_file(resolve_asset_image_path(asset, get_settings()))
            except (RebuildWorkflowError, OSError, TypeError): errors.append('image_artifact_unavailable')
            provenance = asset.provenance if isinstance(asset.provenance,dict) else {}
            reading = provenance.get('reading_explanation') or provenance.get('scientific_reading') or {}
            reading = reading if isinstance(reading,dict) else {}
            require(bool((asset.explanation or '').strip() or reading.get('detailed_explanation_zh')), 'image_interpretation_missing')
    else:
        from app.services.rebuild_sections import _checked_snapshot
        row = sections.get(link.get('section_id')); event = section_events.get(link.get('section_id'))
        try:
            declared = _checked_snapshot(row, event)
            require(declared.get('file_id') == source['file_id'], 'section_file_mismatch')
            actual = snapshot.get('file_reading_identity', {}).get(source['file_id'], {})
            require(declared.get('source_sha256') == actual.get('source_sha256') and
                    event.payload.get('reading_pdf_sha256') == actual.get('reading_pdf_sha256'), 'section_source_hash_mismatch')
            require(set(source['pages']) <= {s.get('page_number') for s in declared.get('sources', [])}, 'section_page_mismatch')
            require(not item['identity'] and not item['condition'], 'section_cannot_prove_material_condition_fact')
        except RebuildWorkflowError: errors.append('section_source_unverified')
    return errors


def evaluate(session, paper_id, plan, snapshot, *, checked_revision=None, previous=None):
    """One batched graph; no item-driven SQL or PDF text search."""
    physical = snapshot.get('file_reading_identity', {})
    errors, scopes, file_map = [], [], {f['file_id']: f for f in plan['files']}
    def error(code, item_key=None, file_id=None):
        errors.append({'code':code, 'item_key':item_key, 'file_id':file_id})
    if len(file_map) != len(plan['files']): error('duplicate_file_declaration')
    for file_id, declaration in file_map.items():
        actual = physical.get(file_id)
        if not actual or actual['status'] != 'verified' or any(declaration[k] != actual.get(k) for k in ('source_sha256','reading_pdf_sha256')):
            error('file_identity_or_hash_unverified', file_id=file_id)
        pages = declaration['declared_pages']; total = actual.get('physical_pages') if actual else None
        if len(set(pages)) != len(pages) or any(type(p) is not int or p < 1 or total is None or p > total for p in pages):
            error('declared_physical_pages_invalid', file_id=file_id)
        if not declaration['scope_note'].strip(): error('scope_note_blank', file_id=file_id)
    invalid_files = {e['file_id'] for e in errors if e.get('file_id')}
    for file_id, actual in physical.items():
        declared = file_map.get(file_id, {})
        pages = sorted(set(declared.get('declared_pages', []))); page_set = set(pages)
        total = actual.get('physical_pages')
        remaining = [p for p in range(1, (total or 0)+1) if p not in page_set]
        scopes.append({'file_id':file_id, 'original_filename':actual['original_filename'], 'role':actual['role'],
                       'physical_pages':total, 'declared_pages':pages, 'undeclared_pages':remaining,
                       'status':'unknown' if not declared or remaining or file_id in invalid_files or actual['status'] != 'verified' else 'declared_not_independently_verified'})
    graph = _graph(session, paper_id) if any(i['state']=='extracted' for i in plan['items']) else None
    snapshot = {**snapshot, 'coverage_table_map':{t.get('asset_id'):t for t in snapshot.get('table_completeness', [])}}
    items, keys, used, identity_cache = [], set(), {}, {}
    identities=historical_identities(session,paper_id)
    stale_output = checked_revision is not None and checked_revision != snapshot['revision']
    for item in sorted(plan['items'], key=lambda x:x['item_key']):
        key = item['item_key']; source = item['source']; problems = []
        if key in keys: problems.append('duplicate_item_key')
        keys.add(key)
        if source['file_id'] not in file_map: problems.append('item_file_not_declared')
        if any(type(p) is not int or p < 1 for p in source['pages']) or len(set(source['pages'])) != len(source['pages']) or not set(source['pages']) <= set(file_map.get(source['file_id'],{}).get('declared_pages', [])):
            problems.append('item_pages_outside_declared_scope')
        if source['file_id'] in invalid_files: problems.append('item_source_stale_or_unknown')
        if not source['locator'].strip() or not item['title'].strip(): problems.append('blank_evidence_locator_or_title')
        if set(item['identity']) - set(IDENTITY_FIELDS): problems.append('unsupported_identity_fields')
        if item['kind'] not in {'row','value','table','figure','subfigure'} and (item['identity'] or item['condition']): problems.append('section_identity_not_a_fact')
        reason = item.get('reason')
        if reason:
            if not reason['code'].strip() or not reason['detail'].strip(): problems.append('reason_blank')
            if not set(reason['checked_pages']) <= set(file_map.get(source['file_id'],{}).get('declared_pages', [])):
                problems.append('reason_pages_outside_declared_scope')
            if item['state']=='not_reported' and not set(source['pages']) <= set(reason['checked_pages']): problems.append('not_reported_checked_scope_incomplete')
        if key in identities and identities[key] != {_identity_signature(item)}:
            problems.append('stable_item_identity_changed_use_new_key')
        if graph and item['state']=='extracted':
            problems.extend(_item_problems(item, graph, snapshot, identity_cache))
            link = item['link']
            token = (item['kind'], link.get('value_id') or link.get('asset_id') or link.get('section_id') or link.get('row_id'))
            if token in used: problems.append('output_reused_for_distinct_plan_items')
            used[token] = key
        for code in problems: error(code, key, source['file_id'])
        state = 'unknown' if problems else item['state']
        freshness = 'stale' if source['file_id'] in invalid_files or stale_output and item['state'] in {'extracted','not_reported','not_applicable'} else 'current'
        if freshness=='stale': state='unknown'
        items.append({**item, 'effective_state':state, 'check_status':'incomplete' if problems else 'stale' if freshness=='stale' else 'linked_not_scientifically_accepted' if item['state']=='extracted' else 'declaration_only',
                      'problems':problems, 'freshness':freshness})
    counts = dict(Counter(i['effective_state'] for i in items))
    unknown_scope = not physical or bool(snapshot.get('unregistered_files')) or any(s['status']=='unknown' for s in scopes)
    status = 'unknown' if not items or unknown_scope or counts.get('unknown') or stale_output else 'partial' if counts.get('pending') or errors else 'declared_items_accounted_for'
    return {'paper_id':str(paper_id), 'valid':not errors, 'errors':errors, 'items':items, 'summary':{'status':status, 'total':len(items), 'counts':counts,
        'stale_items':sum(i['freshness']=='stale' for i in items), 'scope_unknown':unknown_scope,
        'unregistered_files':snapshot.get('unregistered_files', []), 'files':scopes,
        'rule':'Only declared objects checked. A declared full page scope is not proof of a complete source inventory or scientific correctness.'},
        'scientific_acceptance':'not_implied', 'read_only':True}


def save_plan(session, paper_id, payload, snapshot, *, actor):
    request = payload.model_dump(mode='json'); submitted = payload.model_dump(mode='json',exclude_unset=True); fingerprint = _digest(submitted)
    replay = session.scalar(select(AuditLog).where(AuditLog.paper_id==paper_id, AuditLog.action==EVENT,
        AuditLog.source==VERSION, AuditLog.target_id==payload.request_id))
    if replay:
        if replay.payload['request_sha256'] != fingerprint or replay.payload['actor'] != actor:
            raise RebuildWorkflowError('coverage_request_id_reused')
        return {**replay.payload['receipt'], 'replayed':True}, None
    event = latest(session, paper_id); previous = event.payload if event else None
    if payload.expected_plan_revision != (previous['plan_revision'] if previous else None):
        raise RebuildWorkflowError('coverage_plan_revision_changed')
    if payload.expected_output_revision != snapshot['revision']:
        raise RebuildWorkflowError('coverage_output_revision_changed')
    plan = {'files':request['files'], 'items':request['items']}
    report = evaluate(session, paper_id, plan, snapshot, previous=previous['plan'] if previous else None)
    if not report['valid']: return None, report
    revision = _digest(plan)
    generation = previous['generation']+1 if previous else 1
    event_id = uuid4()
    receipt = {'metadata_event_id':str(event_id), 'paper_id':str(paper_id), 'plan_revision':revision, 'generation':generation,
               'checked_output_revision':snapshot['revision'], 'request_id':payload.request_id, 'replayed':False,
               'metadata_only':True, 'scientific_acceptance':'not_implied'}
    session.add(AuditLog(id=event_id, paper_id=paper_id, action=EVENT, source=VERSION, target_type='paper_coverage_plan',
        target_id=payload.request_id, payload={'generation':generation, 'plan_revision':revision,
        'checked_output_revision':snapshot['revision'], 'actor':actor, 'request_sha256':fingerprint,
        'request':submitted, 'plan':plan, 'previous_plan_revision':previous['plan_revision'] if previous else None, 'receipt':receipt}))
    session.flush()
    return receipt, report


def report(session, paper_id, snapshot, *, offset=0, limit=50, expected_revision=None, event=None):
    event = latest(session, paper_id) if event is None else event
    stored = event.payload if event else None
    revision = _digest([stored['generation'] if stored else None, stored['plan_revision'] if stored else None, snapshot['revision']])
    if offset and expected_revision is None:
        raise RebuildWorkflowError('coverage_read_revision_required')
    if expected_revision is not None and expected_revision != revision:
        raise RebuildWorkflowError('coverage_read_revision_changed')
    plan = stored['plan'] if stored else {'files':[], 'items':[]}
    result = evaluate(session, paper_id, plan, snapshot, checked_revision=stored['checked_output_revision'] if stored else None)
    items = result['items']; next_offset = offset+limit if limit is not None and offset+limit < len(items) else None
    result.update(files=plan['files'], items=items if limit is None else items[offset:offset+limit], plan_revision=stored['plan_revision'] if stored else None,
                  generation=stored['generation'] if stored else None, checked_output_revision=stored['checked_output_revision'] if stored else None,
                  output_revision=snapshot['revision'], revision=revision, legacy_without_plan=stored is None,
                  offset=offset, limit=limit, has_more=next_offset is not None, next_offset=next_offset,
                  next_page=f'/api/rebuild/papers/{paper_id}/coverage-plan?'+urlencode({'offset':next_offset,'limit':limit,'expected_revision':revision}) if next_offset is not None else None,
                  declaration={'event_id':str(event.id), 'actor':stored['actor'], 'request_id':event.target_id,
                               'created_at':event.created_at.isoformat(), 'previous_plan_revision':stored.get('previous_plan_revision')} if event else None,
                  contract=contract(paper_id))
    # Error lists can also be large; per-page item details carry each issue.
    result['errors_total'] = len(result['errors'])
    result['errors'] = [e for e in result['errors'] if e.get('item_key') in {i['item_key'] for i in result['items']} or e.get('item_key') is None][:200]
    return result


def output_detail(session, paper_id, snapshot, item_key, expected_revision):
    """Exact linked object read. No row pagination or whole-paper body scan.

    The shared scientific snapshot still computes the established version hash;
    object bodies below are selected only by the plan's exact row/asset/section.
    """
    from app.services.rebuild_workflow_service import serialize_row, serialize_value, serialize_source, serialize_asset
    from app.services.rebuild_sections import SECTION_FIELDS, EVENT as SECTION_EVENT, VERSION as SECTION_VERSION
    stored_event = latest(session, paper_id)
    stored = stored_event.payload if stored_event else None
    revision = _digest([stored['generation'] if stored else None, stored['plan_revision'] if stored else None, snapshot['revision']])
    if expected_revision != revision:
        raise RebuildWorkflowError('coverage_read_revision_changed')
    item = next((i for i in stored['plan']['items'] if i['item_key']==item_key), None) if stored else None
    if item is None or not item.get('link'):
        raise RebuildWorkflowError('coverage_linked_output_not_found')
    link=item['link']; rows={}; values={}; sources={}; assets={}; sections={}; section_events={}
    def exact(model, key, options=()):
        if not key:return None
        query=select(model).where(model.paper_id==paper_id,model.id==UUID(key))
        if options:query=query.options(*options)
        return session.scalar(query)
    output={}
    if item['kind'] in {'row','value'}:
        row=exact(RebuildDataRow,link.get('row_id'))
        if row is None:raise RebuildWorkflowError('coverage_linked_output_not_found')
        rows[str(row.id)]=row
        values={str(v.id):v for v in session.scalars(select(RebuildDataValue).where(
            RebuildDataValue.paper_id==paper_id,RebuildDataValue.row_id==row.id).order_by(RebuildDataValue.field_name,RebuildDataValue.id))}
        sources={str(s.id):s for s in session.scalars(select(RebuildValueSource).where(
            RebuildValueSource.paper_id==paper_id,RebuildValueSource.row_id==row.id).order_by(RebuildValueSource.id))}
        asset_ids={s.asset_id for s in sources.values() if s.asset_id}
        if asset_ids:assets={str(a.id):a for a in session.scalars(select(RebuildVisualAsset).where(
            RebuildVisualAsset.paper_id==paper_id,RebuildVisualAsset.id.in_(asset_ids)))}
        output['row']=serialize_row(session,row,values=list(values.values()),sources=list(sources.values()))
        if item['kind']=='value':
            value=values.get(link.get('value_id'));source=sources.get(link.get('source_id'))
            if value is None or source is None or source.value_id!=value.id:
                raise RebuildWorkflowError('coverage_linked_output_not_found')
            output['value']=serialize_value(value,[s for s in sources.values() if s.value_id==value.id])
            output['source']=serialize_source(source)
    elif item['kind'] in {'table','figure','subfigure'}:
        asset=exact(RebuildVisualAsset,link.get('asset_id'))
        if asset is None:raise RebuildWorkflowError('coverage_linked_output_not_found')
        assets[str(asset.id)]=asset
        output['asset']=serialize_asset(asset)
        output['image_endpoint']=f'/api/rebuild/assets/{asset.id}' if asset.image_path else None
    else:
        section=exact(PaperSection,link.get('section_id'),(load_only(PaperSection.id,PaperSection.paper_id,*[getattr(PaperSection,f) for f in SECTION_FIELDS]),))
        if section is None:raise RebuildWorkflowError('coverage_linked_output_not_found')
        sections[str(section.id)]=section
        source_event=session.scalar(select(AuditLog).where(AuditLog.paper_id==paper_id,AuditLog.action==SECTION_EVENT,
            AuditLog.source==SECTION_VERSION,AuditLog.target_id==str(section.id)).order_by(AuditLog.created_at.desc(),AuditLog.id.desc()).limit(1))
        if source_event:section_events[str(section.id)]=source_event
        output['section']={'id':str(section.id),**{f:getattr(section,f) for f in SECTION_FIELDS}}
        output['section_source']=source_event.payload if source_event else None
    by_row,by_value=defaultdict(list),defaultdict(list)
    for value in values.values():by_row[str(value.row_id)].append(value)
    for source in sources.values():by_value[str(source.value_id)].append(source)
    graph=(rows,values,sources,assets,sections,section_events,by_row,by_value)
    checked_snapshot={**snapshot,'coverage_table_map':{t.get('asset_id'):t for t in snapshot.get('table_completeness',[])}}
    problems=_item_problems(item,graph,checked_snapshot,{})
    declared_file=next((f for f in stored['plan']['files'] if f['file_id']==item['source']['file_id']),None)
    actual_file=snapshot.get('file_reading_identity',{}).get(item['source']['file_id'])
    source_current=bool(declared_file and actual_file and actual_file['status']=='verified' and all(
        declared_file[k]==actual_file.get(k) for k in ('source_sha256','reading_pdf_sha256')))
    stale=stored['checked_output_revision']!=snapshot['revision'] or not source_current
    return {'paper_id':str(paper_id),'item_key':item_key,'title':item['title'],'kind':item['kind'],
        'source':item['source'],'file':actual_file,'link':link,'plan_revision':stored['plan_revision'],'revision':revision,
        'output_revision':snapshot['revision'],'freshness':'stale' if stale else 'current','problems':problems,
        'output':output,'read_only':True,'scientific_acceptance':'not_implied'}
