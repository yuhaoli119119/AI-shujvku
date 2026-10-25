"""Current delivery index over existing records, never a second saved truth."""
from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from sqlalchemy import inspect, select, text

from app.db.models import (AuditLog, Paper, PaperSection, WorkflowJob, RebuildDataRow,
                           RebuildDataValue, RebuildValueSource, RebuildVisualAsset)
from app.services.rebuild_acceptance import _digest, acceptance_status
from app.services.rebuild_mutation_audit import EVENT, RECEIPT, VERSION, safe_json
from app.services.rebuild_workflow_service import RebuildWorkflowError


def reading_state(paper, files, physical=None):
    """Missing provenance stays unknown; retained legacy content stays readable."""
    from app.services.reading_abstract_source import publisher_notice, invalid_translation
    stored = paper.reading_guide or {}
    guide = stored.get('beginner_zh') or {}
    translation = stored.get('abstract_translation') or {}
    abstract_zh = (paper.comprehensive_analysis or {}).get('abstract_zh')
    active = {item['file_id']: item['source_sha256'] for item in files}
    pins = {item['file_id']: item.get('source_sha256') for item in guide.get('source_files', [])}
    guide_status = 'missing' if not guide else 'unknown_legacy_provenance'
    if guide and pins:
        guide_status = 'source_identity_match_only' if all(active.get(fid) == digest for fid, digest in pins.items()) else 'stale'
    guide_uncovered = sorted(set(active) - set(pins)) if guide else sorted(active)
    translation_status = 'missing' if not abstract_zh else 'unknown_legacy_provenance'
    source = translation.get('source') or {}
    metadata_hash = (hashlib.sha256(paper.abstract.encode()).hexdigest()
                     if (paper.abstract or '').strip() and not publisher_notice(paper.abstract) else None)
    if abstract_zh and translation:
        if translation.get('translation_sha256') != hashlib.sha256(abstract_zh.encode()).hexdigest():
            translation_status = 'stale'
        elif source.get('file_id'):
            translation_status = ('source_identity_match_only' if
                                  active.get(source['file_id']) == source.get('source_sha256')
                                  and source.get('source_sha256') else 'stale')
            if metadata_hash and metadata_hash != source.get('text_sha256'):
                translation_status = 'stale'
        elif translation.get('original_text') == paper.abstract and paper.abstract:
            translation_status = 'source_identity_match_only'
        elif source.get('kind') == 'metadata':
            translation_status = 'stale'
        if invalid_translation(paper):
            translation_status = 'invalid_source_notice'
    if guide and guide.get('abstract_source_sha256'):
        current = metadata_hash or (source.get('text_sha256') if source.get('kind') == 'main_text' else None)
        if current and guide['abstract_source_sha256'] != current:
            guide_status = 'stale'
        elif not current and guide_status != 'stale':
            guide_status = 'unknown_legacy_provenance'
    if physical is not None:
        if guide_status == 'source_identity_match_only' and any(physical.get(fid, {}).get('status') != 'verified' for fid in pins):
            guide_status = 'source_unverified'
        if translation_status == 'source_identity_match_only' and source.get('file_id') and physical.get(source['file_id'], {}).get('status') != 'verified':
            translation_status = 'source_unverified'
    return {'abstract_original': paper.abstract, 'abstract_zh': abstract_zh,
            'abstract_translation': translation or None, 'guide': guide or None,
            'abstract_status': translation_status, 'guide_status': guide_status,
            'guide_uncovered_file_ids': guide_uncovered,
            'guide_coverage_status': 'additional_files_not_in_saved_scope' if guide_uncovered and guide else 'saved_scope_only' if guide else 'unknown',
            'rule': 'Identity match is not semantic verification. Unknown legacy provenance preserves stored content.'}


def _jobs(session, paper_id):
    records = session.scalars(select(WorkflowJob).where(
        WorkflowJob.payload.op('->>')('paper_id') == str(paper_id)).order_by(WorkflowJob.created_at, WorkflowJob.job_id)).all()
    return [{'job_id': job.job_id, 'type': job.type, 'status_as_recorded': job.status,
             'updated_at': safe_json(job.updated_at),
             'checkpoint_revision': (job.progress or {}).get('checkpoint_revision'),
             'file_plan': (job.progress or {}).get('file_plan'),
             'checkpoint_receipts': (job.progress or {}).get('checkpoint_receipts', {}),
             'native_terminal_verified_as_recorded': (job.progress or {}).get('native_terminal_verified'),
             'current_native_execution': 'unknown_not_proven_here',
             'can_unlock': False} for job in records]


def _source_binding(file_id, saved_pin, current_pin, snapshot, **location):
    """Never fill a historical pin from today's file registry."""
    fid = str(file_id) if file_id else None
    registered = next((f for f in snapshot['files'] if f['file_id'] == fid), None)
    physical = snapshot.get('file_reading_identity', {}).get(fid)
    status = 'unknown_saved_source_pin'
    if saved_pin and saved_pin.get('source_sha256'):
        if saved_pin.get('file_id') and saved_pin['file_id'] != fid:
            status = 'source_binding_changed_since_save'
        elif registered is None:
            status = 'source_not_in_current_active_scope'
        elif saved_pin['source_sha256'] != registered['source_sha256']:
            status = 'source_changed_since_save'
        elif not physical or physical.get('status') != 'verified':
            status = 'current_physical_source_unverified'
        elif saved_pin.get('reading_pdf_sha256') and saved_pin['reading_pdf_sha256'] != physical.get('reading_pdf_sha256'):
            status = 'reading_pdf_changed_since_save'
        else:
            status = 'matches_saved_source_identity'
    return {'file_id': fid, 'saved_source_pin': saved_pin or None,
            'saved_pin_basis': 'original_save_event' if saved_pin else 'unknown_not_backfilled',
            'current_stored_pin': current_pin or None, 'current_registered_source': registered,
            'current_physical_identity': physical, 'source_readback_status': status, **location}


def _checkpoints(session, paper_id, snapshot, events, receipts, entries=None):
    """Only a matching current entity image AND original receipt prove a save."""
    models = {model.__tablename__: model for model in
              (RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildVisualAsset)}
    latest = {}
    saved_images = {}
    for event in events:
        if event.action == EVENT and event.source == VERSION:
            latest[(event.target_type, event.target_id)] = event
            saved_images[(event.payload.get('operation'), event.payload.get('request_id'), event.payload.get('actor'),
                          event.target_type, event.target_id)] = event.payload.get('after') or {}
    receipt_keys = {(r.target_type, r.target_id, r.payload.get('actor')): r for r in receipts}
    from uuid import UUID
    entries = snapshot['output_manifest'] if entries is None else entries
    selected = {kind: [UUID(e['id']) for e in entries if e['kind'] == kind] for kind in models}
    wanted_rows = selected[RebuildDataRow.__tablename__]
    wanted_values = selected[RebuildDataValue.__tablename__]
    related_sources = list(session.scalars(select(RebuildValueSource).where(
        RebuildValueSource.paper_id == paper_id,
        (RebuildValueSource.id.in_(selected[RebuildValueSource.__tablename__])) |
        (RebuildValueSource.row_id.in_(wanted_rows)) | (RebuildValueSource.value_id.in_(wanted_values)))))
    wanted_values = list(set(wanted_values + [s.value_id for s in related_sources]))
    related_values = list(session.scalars(select(RebuildDataValue).where(
        RebuildDataValue.paper_id == paper_id,
        (RebuildDataValue.id.in_(wanted_values)) | (RebuildDataValue.row_id.in_(wanted_rows)))))
    wanted_rows = list(set(wanted_rows + [v.row_id for v in related_values] + [s.row_id for s in related_sources]))
    current = {
        RebuildDataRow.__tablename__: {str(r.id): r for r in session.scalars(select(RebuildDataRow).where(RebuildDataRow.paper_id == paper_id, RebuildDataRow.id.in_(wanted_rows)))},
        RebuildDataValue.__tablename__: {str(r.id): r for r in related_values},
        RebuildValueSource.__tablename__: {str(r.id): r for r in related_sources},
        RebuildVisualAsset.__tablename__: {str(r.id): r for r in session.scalars(select(RebuildVisualAsset).where(RebuildVisualAsset.paper_id == paper_id, RebuildVisualAsset.id.in_(selected[RebuildVisualAsset.__tablename__])))},
    }
    values = current[RebuildDataValue.__tablename__]
    sources = list(current[RebuildValueSource.__tablename__].values())
    items = []
    for entry in entries:
        if entry['kind'] not in models:
            continue
        event = latest.get((entry['kind'], entry['id']))
        result = {**entry, 'save_status': 'stored_legacy_receipt_unknown',
                  'readback_status': 'not_proven_by_receipt', 'receipt_id': None,
                  'scientific_acceptance': 'not_implied'}
        record = current[entry['kind']].get(entry['id'])
        base = f'/api/rebuild/papers/{paper_id}'
        if isinstance(record, RebuildVisualAsset):
            original = event.payload.get('after') or {} if event else {}
            saved = (original.get('provenance') or {}).get('page_identity')
            if saved:
                saved = {**saved, 'file_id': original.get('file_id')}
            result.update(asset_key=record.asset_key,
                          object_read_endpoint=base + f'/assets/{record.id}/metadata',
                          source_bindings=[_source_binding(record.file_id, saved, (record.provenance or {}).get('page_identity'), snapshot,
                                                          pages=record.page_numbers)])
        else:
            row_id = str(record.id if isinstance(record, RebuildDataRow) else record.row_id)
            row = current[RebuildDataRow.__tablename__].get(row_id)
            result.update(row_id=row_id, row_key=row.row_key if row else None)
            if isinstance(record, RebuildValueSource):
                linked = [record]
                value = values.get(str(record.value_id))
                result.update(value_id=str(record.value_id), field_name=value.field_name if value else None,
                              object_read_endpoint=f'/api/rebuild/sources/{record.id}')
            elif isinstance(record, RebuildDataValue):
                linked = [s for s in sources if s.value_id == record.id]
                result.update(field_name=record.field_name, object_read_endpoint=f'/api/rebuild/values/{record.id}')
            else:
                linked = [s for s in sources if s.row_id == record.id]
                result.update(object_read_endpoint=base + '/ai-work-package?view=rows&rows_limit=50&expected_revision=' + snapshot['revision'],
                              value_read_endpoints=[f'/api/rebuild/values/{v.id}' for v in values.values() if str(v.row_id) == row_id])
            bindings = []
            for source in linked:
                value = values.get(str(source.value_id))
                pin = ((value.conflict or {}).get('source_page_identities') or {}).get(str(source.id)) if value else None
                # A source pin belongs to the value JSON image saved by the SAME
                # request/actor as this object's event, never a later value edit.
                saved = None
                if event:
                    key = (event.payload.get('operation'), event.payload.get('request_id'), event.payload.get('actor'))
                    image = saved_images.get((*key, RebuildDataValue.__tablename__, str(source.value_id)), {})
                    source_image = saved_images.get((*key, RebuildValueSource.__tablename__, str(source.id)))
                    saved = ((image.get('conflict') or {}).get('source_page_identities') or {}).get(str(source.id))
                    if saved and source_image:
                        saved = {**saved, 'file_id': source_image.get('file_id')}
                    else:
                        saved = None
                bindings.append(_source_binding(source.file_id, saved, pin, snapshot,
                    source_id=str(source.id), value_id=str(source.value_id), field_name=value.field_name if value else None,
                    page_number=source.page_number, label=source.label, table_row=source.table_row, table_column=source.table_column,
                    source_read_endpoint=f'/api/rebuild/sources/{source.id}'))
            result['source_bindings'] = bindings
        if event:
            data = event.payload
            receipt = receipt_keys.get((data.get('operation'), data.get('request_id'), data.get('actor')))
            result.update(request_id=data.get('request_id'), event_id=str(event.id))
            if receipt and str(event.id) in receipt.payload.get('result', {}).get('audit', {}).get('event_ids', []):
                image = safe_json({c.key: getattr(record, c.key) for c in inspect(type(record)).column_attrs}) if record else None
                result.update(receipt_id=str(receipt.id), save_status='saved_as_recorded',
                              readback_status='matches_saved_image' if image == data.get('after') else 'changed_since_save',
                              receipt_endpoint=f'/api/rebuild/papers/{paper_id}/extraction-import-receipts/{receipt.id}')
        items.append(result)
    return items


def _dedicated_checkpoints(session, paper, snapshot, events):
    from uuid import UUID
    from app.services.rebuild_sections import _checked_snapshot, VERSION as SECTION_VERSION
    section_events = {e.target_id: e for e in events if e.action == 'section_imported' and e.source == SECTION_VERSION}
    section_receipts = {(e.target_id, e.payload.get('actor'), e.payload.get('request_sha256')): e
                        for e in events if e.action == 'section_import_request' and e.source == SECTION_VERSION}
    items = []
    for entry in snapshot['output_manifest']:
        if entry['kind'] != 'paper_sections':
            continue
        event = section_events.get(entry['id'])
        result = {**entry, 'save_status': 'stored_legacy_receipt_unknown', 'readback_status': 'not_proven_by_receipt'}
        result['object_read_endpoint'] = f'/api/rebuild/papers/{paper.id}/sections'
        result['source_bindings'] = []
        if event:
            data = event.payload
            section = data.get('section') or {}
            pin = {key: section.get(key) for key in ('file_id', 'source_sha256', 'page_basis')}
            pin['reading_pdf_sha256'] = data.get('reading_pdf_sha256')
            result['source_bindings'] = [_source_binding(section.get('file_id'), pin if pin.get('source_sha256') else None,
                                                        pin, snapshot, pages=[section.get('page_start'), section.get('page_end')])]
            receipt = section_receipts.get((data.get('request_id'), data.get('actor'), data.get('request_sha256')))
            if receipt and any(item.get('section_id') == entry['id'] for item in receipt.payload.get('result', {}).get('items', [])):
                result.update(save_status='saved_as_recorded', receipt_id=str(receipt.id),
                              request_id=data.get('request_id'), event_id=str(event.id),
                              receipt_as_recorded=receipt.payload['result'])
                try:
                    _checked_snapshot(session.get(PaperSection, UUID(entry['id'])), event)
                    result['readback_status'] = 'matches_saved_image'
                except RebuildWorkflowError:
                    result['readback_status'] = 'changed_since_save'
        items.append(result)
    # Each reading module has one current projection; previous saves remain audit history.
    reading_events = [e for e in events if e.action == 'beginner_zh_v1' and e.source == 'beginner_zh_v1']
    current = paper.reading_guide or {}
    for module in ('abstract', 'guide'):
        field = 'abstract_zh' if module == 'abstract' else 'reading_guide'
        matching = [e for e in reading_events if e.payload.get('receipt') and
                    e.payload.get('after', {}).get(field) is not None and
                    e.payload.get('before', {}).get(field) != e.payload['after'][field]]
        event = matching[-1] if matching else None
        value = (paper.comprehensive_analysis or {}).get('abstract_zh') if module == 'abstract' else current.get('beginner_zh')
        result = {'kind': 'reading_module', 'id': module, 'content_sha256': _digest(value),
                  'save_status': 'stored_legacy_receipt_unknown' if value else 'missing',
                  'readback_status': 'not_proven_by_receipt',
                  'object_read_endpoint': f'/api/rebuild/papers/{paper.id}/reading-assistance',
                  'source_bindings': [], 'saved_source_pin_status': 'unknown_not_backfilled'}
        if event:
            saved = event.payload['after'][field]
            if module == 'guide':
                saved = saved.get('beginner_zh')
            result.update(save_status='saved_as_recorded', event_id=str(event.id),
                          request_id=event.target_id, receipt_as_recorded=event.payload['receipt'],
                          readback_status='matches_saved_image' if value == saved else 'changed_since_save')
            saved_reading = event.payload['after'].get('reading_guide') or {}
            if module == 'guide':
                for source in saved_reading.get('beginner_zh', {}).get('source_files', []):
                    result['source_bindings'].append(_source_binding(source.get('file_id'), source, None, snapshot))
            else:
                source = saved_reading.get('abstract_translation', {}).get('source') or {}
                if source.get('file_id'):
                    result['source_bindings'].append(_source_binding(source['file_id'], source, current.get('abstract_translation', {}).get('source'), snapshot,
                                                                    physical_page=source.get('physical_page')))
                elif source.get('kind') == 'metadata':
                    source_hash = hashlib.sha256((paper.abstract or '').encode()).hexdigest()
                    result['source_bindings'].append({'file_id': None, 'saved_source_pin': source,
                        'saved_pin_basis': 'original_save_event', 'current_metadata_sha256': source_hash,
                        'source_readback_status': 'matches_saved_source_identity' if source_hash == source.get('text_sha256') else 'source_changed_since_save',
                        'current_physical_identity': None, 'physical_identity_rule': 'metadata_has_no_physical_file'})
            if result['source_bindings']:
                result['saved_source_pin_status'] = 'recorded_in_original_save_event'
        items.append(result)
    return items


def delivery_package(session, paper_id, snapshot, settings, *, view='summary', offset=0, limit=50, expected_revision=None):
    from app.services.extraction_coverage import latest, report
    from app.services.reading_assistance import reading_revision
    from app.services.rebuild_quality import quality_report
    from app.services.rebuild_work_package import compact_package

    paper = session.get(Paper, paper_id)
    if paper is None:
        raise RebuildWorkflowError('paper_not_found')
    # Revision transfers only audit identities/digests. Original bodies are read
    # only for the requested page/protocol, never for a summary or job list.
    audit_manifest = list(session.execute(text("SELECT id, encode(sha256(convert_to(to_jsonb(a)::text,'UTF8')),'hex') AS digest FROM audit_logs a WHERE paper_id=:pid ORDER BY id"), {'pid': paper_id}))
    events = []
    receipts = []
    jobs = _jobs(session, paper_id)
    plan = latest(session, paper_id)
    acceptance = acceptance_status(session, paper_id, snapshot=snapshot)
    from app.services.rebuild_review_items import issue_projection
    independent_issues = issue_projection(session, paper_id)
    revision = _digest([snapshot['revision'], reading_revision(paper),
                        [(str(e.id), e.digest) for e in audit_manifest], jobs, acceptance])
    if offset and expected_revision is None:
        raise RebuildWorkflowError('delivery_page_revision_required')
    if expected_revision is not None and expected_revision != revision:
        raise RebuildWorkflowError('delivery_revision_changed')
    base = f'/api/rebuild/papers/{paper_id}'
    result = {'version': 'paper_delivery_v1', 'paper_id': str(paper_id), 'paper_code': paper.paper_code,
              'revision': revision, 'output_revision': snapshot['revision'], 'view': view,
              'read_only': True, 'occupancy_changed': False, 'scientific_acceptance': 'not_implied',
              'pages': {name: base + '/delivery-package?' + urlencode({'view': name, 'limit': limit, 'expected_revision': revision})
                        for name in ('checkpoints', 'gaps', 'receipts', 'jobs')},
              'coverage_endpoint': base + '/coverage-plan?limit=50',
              'reading_endpoint': base + '/reading-assistance', 'acceptance_endpoint': base + '/acceptance'}
    result['independent_issue_summary'] = {
        'total': len(independent_issues),
        'open': sum(i['status'] != 'independently_resolved' for i in independent_issues),
        'history_rule': 'Only explicit independent resolution closes a version-bound issue; new freezes never hide it.',
    }
    result['status_layers'] = {
        'execution': {'jobs_as_recorded': [{k: j[k] for k in ('job_id', 'type', 'status_as_recorded')} for j in jobs],
                      'current_native_execution': 'unknown_not_proven_here', 'can_unlock': False},
        'content': {'scope': 'explicit_declaration_only' if plan else 'unknown_no_coverage_declaration',
                    'declaration_freshness': ('current' if plan.payload.get('checked_output_revision') == snapshot['revision'] else 'stale') if plan else 'unknown',
                    'gaps_endpoint': result['pages']['gaps'], 'stored_results_preserved': True,
                    'completeness': 'not_inferred_from_counts'},
        'independent_review': acceptance,
    }
    if view == 'summary':
        return {**result, 'work_package': compact_package(session, paper_id, snapshot),
                'reading': reading_state(paper, snapshot['files'], snapshot.get('file_reading_identity', {})), 'acceptance': acceptance,
                'coverage_declaration': {'present': plan is not None, 'plan_revision': plan.payload['plan_revision'] if plan else None},
                'execution': {'current_native_execution': 'unknown_not_proven_here',
                              'occupancy_endpoint': base + '/history-reconciliation?view=attempts&limit=25', 'can_unlock': False},
                'rule': 'One current database projection. No counts imply complete source coverage; legacy content and original jobs remain unchanged.'}
    if view == 'checkpoints':
        kinds = {m.__tablename__ for m in (RebuildDataRow, RebuildDataValue, RebuildValueSource, RebuildVisualAsset, PaperSection)}
        entries = [e for e in snapshot['output_manifest'] if e['kind'] in kinds]
        entries.extend([{'kind': 'reading_module', 'id': module} for module in ('abstract', 'guide')])
        selected_entries = entries[offset:offset + limit]
        selected_ids = [e['id'] for e in selected_entries if e['kind'] != 'reading_module']
        # Mutation batches already have a bounded protocol limit. Load only
        # batches touching selected identities, plus dedicated reading/sections.
        requests = list(session.execute(select(AuditLog.payload['request_id'].astext).where(
            AuditLog.paper_id == paper_id, AuditLog.target_id.in_(selected_ids), AuditLog.action == EVENT)))
        request_ids = [r[0] for r in requests]
        events = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
            ((AuditLog.action.in_([EVENT, RECEIPT])) & (AuditLog.payload['request_id'].astext.in_(request_ids))) |
            AuditLog.action.in_(['section_imported', 'section_import_request', 'beginner_zh_v1'])).order_by(AuditLog.created_at, AuditLog.id)))
        receipts = [e for e in events if e.action == RECEIPT and e.source == VERSION]
        items = _checkpoints(session, paper_id, snapshot, events, receipts, entries=selected_entries)
        dedicated_snapshot = {**snapshot, 'output_manifest': [e for e in selected_entries if e['kind'] == 'paper_sections']}
        modules = {e['id'] for e in selected_entries if e['kind'] == 'reading_module'}
        items.extend(i for i in _dedicated_checkpoints(session, paper, dedicated_snapshot, events)
                     if i['kind'] != 'reading_module' or i['id'] in modules)
        next_offset = offset + limit if offset + limit < len(entries) else None
        return {**result, 'items': items, 'total': len(entries), 'offset': offset, 'limit': limit,
                'has_more': next_offset is not None, 'next_offset': next_offset,
                'next_page': base + '/delivery-package?' + urlencode({'view': view, 'offset': next_offset, 'limit': limit, 'expected_revision': revision}) if next_offset is not None else None}
    elif view == 'receipts':
        receipts = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == RECEIPT, AuditLog.source == VERSION).order_by(AuditLog.created_at, AuditLog.id)))
        items = [{'id': str(e.id), 'request_id': e.target_id, 'operation': e.target_type,
                  'receipt_as_recorded': e.payload, 'current_freshness': 'read_target_objects',
                  'endpoint': base + f'/extraction-import-receipts/{e.id}'} for e in receipts]
    elif view == 'jobs':
        items = jobs
    elif view == 'gaps':
        receipts = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == RECEIPT, AuditLog.source == VERSION).order_by(AuditLog.created_at, AuditLog.id)))
        coverage = report(session, paper_id, snapshot, limit=None)
        quality = quality_report(session, paper_id, settings, snapshot, limit=100000, coverage_plan_report=coverage)
        items = list(quality['issues'])
        items.extend({'kind': 'independent_review_issue', 'id': i['issue_id'],
                      'code': 'independent_issue_requires_recheck', 'issue': i,
                      'reason': i['required_change'], 'file_ids': i['source_file_ids']}
                     for i in independent_issues if i['status'] != 'independently_resolved')
        for receipt in receipts:
            errors = receipt.payload.get('result', {}).get('errors', [])
            for index, error in enumerate(errors):
                items.append({'kind': 'save_receipt_observation', 'id': str(receipt.id) + ':' + str(index),
                              'code': 'historical_save_error_requires_current_readback',
                              'request_id': receipt.target_id, 'receipt_id': str(receipt.id),
                              'error_as_recorded': error, 'current_resolution': 'unknown_not_inferred_from_old_receipt',
                              'endpoint': base + f'/extraction-import-receipts/{receipt.id}'})
        reading = reading_state(paper, snapshot['files'], snapshot.get('file_reading_identity', {}))
        for fid in reading['guide_uncovered_file_ids'] if reading['guide'] else []:
            items.append({'kind': 'reading_module', 'id': 'guide:' + fid, 'file_id': fid,
                          'code': 'reading_guide_additional_file_not_in_saved_scope',
                          'status': 'coverage_not_declared_for_this_file',
                          'reason': 'Additional registered file is outside the saved guide scope; the existing guide remains available.'})
        for module in ('abstract', 'guide'):
            status = reading[module + '_status']
            if status != 'source_identity_match_only':
                items.append({'kind': 'reading_module', 'id': module, 'code': 'reading_' + status,
                              'status': status, 'reason': 'Stored content is preserved; source review or targeted supplementation may be needed.'})
        result['coverage_summary'] = coverage['summary']
        result['scope_rule'] = 'Gaps are exact stored checks/declarations; absence of a declaration is unknown, never proof the original has no content.'
    else:
        raise RebuildWorkflowError('delivery_view_invalid')
    next_offset = offset + limit if offset + limit < len(items) else None
    return {**result, 'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit,
            'has_more': next_offset is not None, 'next_offset': next_offset,
            'next_page': base + '/delivery-package?' + urlencode({'view': view, 'offset': next_offset, 'limit': limit, 'expected_revision': revision}) if next_offset is not None else None}
