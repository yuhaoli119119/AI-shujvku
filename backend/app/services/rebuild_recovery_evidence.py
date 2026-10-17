"""Read-only recovery proof from real import receipts and the existing CAS journals.

Never infer a row from its present name alone or reconstruct missing audit state.
"""
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID
from pydantic import ValidationError
from app.db.models import RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.schemas.rebuild import RebuildDataValueInput, RebuildValueCorrectionRequest, RebuildSourceCorrectionRequest
from app.services.rebuild_mutation_audit import EVENT, VERSION
from app.services.rebuild_workflow_service import (IDENTITY_FIELDS, RebuildWorkflowError, _canonical_json,
    _source_key, _stored_identity, _validate_value_for_paper, serialize_source)
from app.services.extraction_coverage import _fact_problems

CORE = ('raw_value','numeric_value','unit','value_type','precision_digits','missing_reason')
HEX = re.compile(r'^[0-9a-f]{64}$')


def _at(value):
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError,ValueError):return None


def _hash(value):return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _core(value):
    return {key:value.get(key) for key in CORE}


def _value_revision(state):
    body = {key:state.get(key) for key in (*CORE,'conflict','preferred_source_id','updated_at')}
    return _hash(body)


def trusted_events(audits, receipt_keys):
    result = []
    for event in audits:
        if event.action != EVENT or event.source != VERSION or not isinstance(event.payload,dict):continue
        data = event.payload
        receipt = receipt_keys.get((data.get('operation'),data.get('request_id'),data.get('actor')))
        if receipt and str(event.id) in receipt.payload.get('result',{}).get('audit',{}).get('event_ids',[]):
            result.append(event)
    return result


def _journal_valid(entry):
    return bool(isinstance(entry,dict) and isinstance(entry.get('actor'),str) and entry['actor'].strip() and
                isinstance(entry.get('reason'),str) and entry['reason'].strip() and
                HEX.fullmatch(str(entry.get('expected_revision',''))) and
                HEX.fullmatch(str(entry.get('request_fingerprint',''))) and _at(entry.get('recorded_at')))


def _source_chains(paper_id, current, values, sources, events):
    chains = {}; proofs = {}
    by_id = {str(s.id):s for group in sources.values() for s in group}
    for value in values.values():
        history = (value.conflict or {}).get('source_corrections') or []
        if not isinstance(history,list):continue
        grouped = defaultdict(list)
        for entry in history:
            if isinstance(entry,dict):grouped[entry.get('source_id')].append(entry)
        for source_id, entries in grouped.items():
            source = by_id.get(source_id)
            if source is None or not _journal_valid(entries[0]):continue
            state = current.get((RebuildValueSource.__tablename__,source_id))
            anchor = next((e for e in events if e.target_type == RebuildValueSource.__tablename__ and e.target_id == source_id and
                           e.payload.get('after') == entries[0].get('before') and _at(e.created_at) < _at(entries[0].get('recorded_at'))),None)
            initial = anchor.payload['after'] if anchor else entries[0].get('before')
            if not isinstance(initial,dict) or not _at(initial.get('created_at')) or _at(initial['created_at']) >= _at(entries[0]['recorded_at']):continue
            cursor = initial; prefix = []; seen = set(); valid = True
            for entry in entries:
                try:
                    if not _journal_valid(entry) or entry['request_id'] in seen:raise ValueError()
                    seen.add(entry['request_id'])
                    if any(entry.get(k) != v for k,v in {'paper_id':str(paper_id),'value_id':str(value.id),'source_id':source_id}.items()):raise ValueError()
                    if cursor != entry['before'] or entry['expected_revision'] != _hash({'source':cursor,'history':prefix}):raise ValueError()
                    if prefix and _at(entry['recorded_at']) <= _at(prefix[-1]['recorded_at']):raise ValueError()
                    after = entry['after']
                    if any(after.get(k) != cursor.get(k) for k in ('id','paper_id','row_id','value_id','created_at')):raise ValueError()
                    if after.get('paper_id') != str(paper_id) or after.get('row_id') != str(value.row_id) or after.get('value_id') != str(value.id):raise ValueError()
                    fields = {k:after.get(k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')}
                    request = RebuildSourceCorrectionRequest(paper_id=paper_id,request_id=entry['request_id'],
                        expected_revision=entry['expected_revision'],correction_reason=entry['reason'],source=fields)
                    if _hash(request.model_dump(mode='json')) != entry['request_fingerprint'] or _source_key(after) != after.get('source_key'):raise ValueError()
                    cursor = after; prefix.append(entry)
                except (KeyError,ValueError,TypeError,ValidationError):valid = False; break
            if valid and cursor == state:
                chains[source_id] = {'entries':entries,'initial':initial,'trusted_anchor':bool(anchor)}
                if anchor:
                    proofs[(RebuildValueSource.__tablename__,source_id)] = {'kind':'source_cas_chain','anchor_event_id':str(anchor.id),
                        'request_id':entries[-1]['request_id'],'recorded_at':entries[-1]['recorded_at']}
    return chains,proofs


def _source_at(source, state, chain, when):
    if _at(source.created_at) > when:return None
    if chain:
        cursor = chain['initial']
        for entry in chain['entries']:
            if _at(entry['recorded_at']) > when:break
            cursor = entry['after']
        return cursor
    return state


def _legal(session, paper_id, row, value, sources):
    try:
        if row.paper_id != paper_id or value.paper_id != paper_id or value.row_id != row.id:return False
        if (row.properties or {}).get('_rebuild_import',{}).get('pending_identity_fields'):return False
        if value.is_estimated != (value.value_type=='estimated'):return False
        _stored_identity(session,row)
        if _fact_problems(value,sources):return False
        if any((s.paper_id,s.row_id,s.value_id) != (paper_id,row.id,value.id) for s in sources):return False
        payload = RebuildDataValueInput(field_name=value.field_name,**{k:getattr(value,k) for k in CORE},
            sources=[{k:v for k,v in serialize_source(s).items() if k not in {'id'}} for s in sources]).model_dump()
        _validate_value_for_paper(session,paper_id,payload)
        return True
    except (RebuildWorkflowError,ValidationError,ValueError,TypeError):return False


def augment_proofs(session, paper_id, current, rows, values, sources, audits, receipt_keys):
    events = trusted_events(audits,receipt_keys)
    chains, proofs = _source_chains(paper_id,current,values,sources,events)
    for value in values.values():
        state = current[(RebuildDataValue.__tablename__,str(value.id))]
        row = rows.get(str(value.row_id)); attached = sources[str(value.id)]
        history = (value.conflict or {}).get('correction_requests') or []
        if history:
            # Existing value journal does not persist all intermediate timestamp/
            # conflict state. Require an exact real import anchor for this CAS.
            entry = history[-1] if isinstance(history,list) else None
            if not _journal_valid(entry):continue
            when = _at(entry['recorded_at'])
            anchors = [e for e in events if e.target_type == RebuildDataValue.__tablename__ and e.target_id == str(value.id) and
                       isinstance(e.payload.get('after'),dict) and _core(e.payload['after']) == entry.get('before') and
                       _value_revision(e.payload['after']) == entry['expected_revision'] and _at(e.created_at) < when]
            if not anchors or _core(state) != entry.get('after') or not row or not _legal(session,paper_id,row,value,attached):continue
            matches = []
            candidates = [None] if value.value_type == 'missing' else attached
            for source in candidates:
                source_state = _source_at(source,current[(RebuildValueSource.__tablename__,str(source.id))],chains.get(str(source.id)),when) if source else None
                if source and source_state is None:continue
                for key in ([None,source_state.get('source_key')] if source_state else [None]):
                    try:
                        body = ({k:source_state.get(k) for k in ('file_id','asset_id','source_kind','page_number','label','table_row','table_column','quote','estimate_basis')} | {'source_key':key}) if source_state else None
                        if body and key is None and _source_key(body) != source_state['source_key']:continue
                        request = RebuildValueCorrectionRequest(**entry['after'],expected_revision=entry['expected_revision'],
                            request_id=entry['request_id'],correction_reason=entry['reason'],notes=entry.get('notes'),source=body)
                        if _hash(request.model_dump(mode='json')) == entry['request_fingerprint']:
                            matches.append(str(source.id) if source else None)
                    except (KeyError,ValueError,TypeError,ValidationError):continue
            if len(set(matches)) != 1 or not matches or (str(value.preferred_source_id) if value.preferred_source_id else None) != matches[0]:continue
            # All previous journals must be the anchor prefix. No unaudited tail.
            anchor = anchors[-1]; before = anchor.payload['after']
            if any(before.get(k) != state.get(k) for k in ('id','paper_id','row_id','field_name','created_at')):continue
            conflict=value.conflict or {}
            if conflict.get('status')!='corrected_previous_retained' or conflict.get('previous')!=entry['before'] or conflict.get('correction_reason')!=entry['reason'] or conflict.get('notes')!=entry.get('notes'):continue
            prior = (before.get('conflict') or {}).get('correction_requests') or []
            if prior != history[:-1]:continue
            if _at(value.updated_at) < when:continue
            source_history = (value.conflict or {}).get('source_corrections') or []
            if not isinstance(source_history,list) or any(not isinstance(e,dict) or not _journal_valid(e) or e.get('source_id') not in chains or
                not chains[e['source_id']]['trusted_anchor'] and (e['source_id'] != matches[0] or
                _at(chains[e['source_id']]['entries'][0]['recorded_at']) <= when) for e in source_history):continue
            latest = max([entry,*source_history],key=lambda e:_at(e['recorded_at']))
            if _at(value.updated_at) < _at(latest['recorded_at']):continue
            proofs[(RebuildDataValue.__tablename__,str(value.id))] = {'kind':'value_cas_with_source_chain' if source_history else 'value_cas','anchor_event_id':str(anchor.id),
                'request_id':entry['request_id'],'recorded_at':latest['recorded_at'],
                'source_correction_request_ids':[e['request_id'] for e in source_history]}
            if matches[0] is not None:
                sid = matches[0]
                if sid in chains and not chains[sid]['trusted_anchor']:
                    proofs[(RebuildValueSource.__tablename__,sid)] = {'kind':'value_cas_source_chain','anchor_event_id':str(anchor.id),
                        'request_id':chains[sid]['entries'][-1]['request_id'],'recorded_at':chains[sid]['entries'][-1]['recorded_at']}
                elif sid not in chains:
                    proofs[(RebuildValueSource.__tablename__,sid)] = {'kind':'value_cas_source','request_id':entry['request_id'],
                        'anchor_event_id':str(anchor.id),'recorded_at':entry['recorded_at']}
        elif (value.conflict or {}).get('source_corrections'):
            # Source-only CAS changes value.conflict but preserves the scientific
            # cell. Compare every other stored field against a real import event.
            source_history = value.conflict['source_corrections']
            if not isinstance(source_history,list) or any(not isinstance(e,dict) or not _journal_valid(e) or e.get('source_id') not in chains or not chains[e['source_id']]['trusted_anchor'] for e in source_history):continue
            for anchor in reversed(events):
                if anchor.target_type != RebuildDataValue.__tablename__ or anchor.target_id != str(value.id):continue
                before = anchor.payload.get('after') or {}; expected = dict(before)
                expected['conflict'] = {**(before.get('conflict') or {}),'source_corrections':source_history}
                if {k:v for k,v in expected.items() if k != 'updated_at'} != {k:v for k,v in state.items() if k != 'updated_at'}:continue
                last = max(source_history,key=lambda e:_at(e['recorded_at']))
                if _at(anchor.created_at) >= _at(source_history[0]['recorded_at']) or _at(value.updated_at) < _at(last['recorded_at']):continue
                if row and _legal(session,paper_id,row,value,attached):
                    proofs[(RebuildDataValue.__tablename__,str(value.id))] = {'kind':'import_with_source_cas','anchor_event_id':str(anchor.id),
                        'request_id':last['request_id'],'recorded_at':last['recorded_at']}
                break
    return proofs,events


def _same_identity(row, state):
    return (isinstance(state,dict) and state.get('id') == str(row.id) and state.get('paper_id') == str(row.paper_id) and
            all(state.get(k) == getattr(row,k) for k in IDENTITY_FIELDS) and state.get('condition',{}) == (row.condition or {}))


def resolve_failure(session, paper_id, receipt, error, rows, values, sources, proved, extra, events):
    result = {'recovered':False,'reason':'no_unique_original_row_or_field_evidence'}
    field = error.get('field') or error.get('field_name')
    if (not isinstance(field,str) or not (error.get('part') is None or isinstance(error.get('part'),str)) or
            error.get('part') not in {None,'values','value'} or
            error.get('field') and error.get('field_name') and error['field'] != error['field_name']):return result
    original_events = [e for e in events if e.payload.get('request_id') == receipt.target_id and
                       e.payload.get('actor') == receipt.payload.get('actor') and e.payload.get('operation') == receipt.target_type]
    index = error.get('row_index'); alias = error.get('row_key')
    if index is not None and (type(index) is not int or index < 0):return result
    if error.get('row_id') is not None and not isinstance(error['row_id'],str):return result
    if alias is not None and not isinstance(alias,str):return result
    def aliases(record):
        if isinstance(record,dict):
            key=record.get('row_key');properties=record.get('properties') or {}
        else:key=record.row_key;properties=record.properties or {}
        meta=properties.get('_rebuild_import',{}) if isinstance(properties,dict) else {}
        raw=meta.get('aliases',[]) if isinstance(meta,dict) else []
        return {key,*[a for a in raw if isinstance(a,str)]} if isinstance(raw,list) else {key}
    original_result=receipt.payload.get('result',{})
    raw_results=original_result.get('results',[])
    if not isinstance(raw_results,list):return result
    # Explicit item indexes take precedence. Positional fallback is supported
    # only by the original one-result-per-submitted-row contract, never by the
    # current alias or a guessed ordering.
    positional=(type(original_result.get('rows_submitted')) is int and
                original_result['rows_submitted']==len(raw_results))
    if positional and any(not isinstance(r,dict) or r.get('row_index') is not None and
            (type(r['row_index']) is not int or r['row_index']!=position) for position,r in enumerate(raw_results)):return result
    if index is not None:
        selected=[r for position,r in enumerate(raw_results) if isinstance(r,dict) and
                  (type(r.get('row_index')) is int and r['row_index']==index if r.get('row_index') is not None else positional and position==index)]
        original_events=[e for e in original_events if type(e.payload.get('item_index')) is int and e.payload['item_index']==index]
    else:
        def relevant(r):
            if error.get('row_id'):return r.get('row_id')==error['row_id']
            return bool(alias and r.get('row_key')==alias)
        selected=[r for r in raw_results if isinstance(r,dict) and relevant(r)]
        selected_ids={r.get('row_id') for r in selected if isinstance(r.get('row_id'),str)}
        selected_ids.update(e.target_id for e in original_events if e.target_type==RebuildDataRow.__tablename__ and
                            isinstance(e.payload.get('after'),dict) and alias in aliases(e.payload['after']))
        if error.get('row_id'):selected_ids.add(error['row_id'])
        original_events=[e for e in original_events if (e.target_id if e.target_type==RebuildDataRow.__tablename__ else
                         (e.payload.get('after') or {}).get('row_id')) in selected_ids]
    candidates=set();anchors=defaultdict(list);original_keys=[]
    for selected_row in selected:
        if selected_row.get('row_id') is not None:
            if not isinstance(selected_row['row_id'],str):return result
            candidates.add(selected_row['row_id'])
        if selected_row.get('row_key') is not None:
            if not isinstance(selected_row['row_key'],str):return result
            original_keys.append(selected_row['row_key'])
    for event in original_events:
        after=event.payload.get('after')
        if not isinstance(after,dict) or after.get('paper_id')!=str(paper_id):return result
        if after.get('id')!=event.target_id:return result
        original_row_id=event.target_id if event.target_type==RebuildDataRow.__tablename__ else after.get('row_id')
        if not isinstance(original_row_id,str):return result
        candidates.add(original_row_id)
        if event.target_type==RebuildDataRow.__tablename__:
            anchors[original_row_id].append(after)
    # A provided row UUID must agree with every original result/event, not
    # discard the contradictory candidates by intersecting them away.
    if len(candidates)!=1:return result
    rid=next(iter(candidates))
    if error.get('row_id') is not None and error['row_id']!=rid:return result
    row=rows.get(rid)
    if row is None:return result
    original_anchors=anchors.get(rid,[])
    if not original_anchors:
        original_anchors=[e.payload['after'] for e in reversed(events) if e.target_type==RebuildDataRow.__tablename__ and
                          e.target_id==rid and isinstance(e.payload.get('after'),dict) and _at(e.created_at)<=_at(receipt.created_at)][:1]
    if not original_anchors or any(not _same_identity(row,a) for a in original_anchors):return result
    historical_aliases=set.intersection(*(aliases(a) for a in original_anchors))
    provided_keys=[*original_keys,*([alias] if alias is not None else [])]
    if any(key not in aliases(row) or key not in historical_aliases for key in provided_keys):return result
    if any(sum(key in aliases(record) for record in rows.values())!=1 for key in provided_keys):return result
    value = values.get((rid,field))
    if value is None or not _legal(session,paper_id,row,value,sources[str(value.id)]):return result
    key = (RebuildDataValue.__tablename__,str(value.id))
    proof = extra.get(key)
    old = proved.get(key)
    if old and old.id != receipt.id and old.payload.get('outcome') == 'succeeded':
        proof = {'kind':'import','receipt_id':str(old.id),'recorded_at':old.created_at.isoformat()}
    if not proof or _at(proof['recorded_at']) <= _at(receipt.created_at):return result
    return {'recovered':True,'row_id':rid,'value_id':str(value.id),'field':field,'proof':proof,
            'reason':'unique_original_receipt_identity_and_current_legal_output_with_later_write_evidence'}
