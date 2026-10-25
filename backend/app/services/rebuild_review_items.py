"""Object-bound independent findings. No science writes or automatic closure."""
from uuid import UUID
from sqlalchemy import inspect, select
from app.db.models import (AuditLog, Paper, PaperSection, RebuildVisualAsset, RebuildDataRow,
                           RebuildDataValue, RebuildValueSource, WorkflowJob)
from app.services.rebuild_mutation_audit import safe_json, EVENT, RECEIPT, VERSION
from app.services.rebuild_workflow_service import RebuildWorkflowError

MODELS = {m.__tablename__: m for m in (PaperSection, RebuildVisualAsset, RebuildDataRow, RebuildDataValue, RebuildValueSource)}


def digest(value):
    from app.services.rebuild_acceptance import _digest
    return _digest(value)


def object_versions(session, paper_id, *, keys=None):
    items = []
    source_rows = list(session.execute(select(RebuildValueSource.row_id, RebuildValueSource.value_id, RebuildValueSource.file_id).where(RebuildValueSource.paper_id == paper_id)))
    section_files = {e.target_id: (e.payload.get('section') or {}).get('file_id') for e in session.scalars(
        select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == 'section_imported',
                              AuditLog.source == 'rebuild_sections_v1'))}
    for kind, model in MODELS.items():
        statement = select(model).where(model.paper_id == paper_id)
        if keys is not None:
            ids = [UUID(oid) for k, oid in keys if k == kind]
            if not ids:
                continue
            statement = statement.where(model.id.in_(ids))
        for row in session.scalars(statement.order_by(model.id)):
            image = safe_json({c.key: getattr(row, c.key) for c in inspect(model).column_attrs if c.key != 'embedding'})
            fids = ({str(row.file_id)} if model in (RebuildVisualAsset, RebuildValueSource) and row.file_id else
                    {str(s.file_id) for s in source_rows if s.file_id and
                     (model is RebuildDataRow and s.row_id == row.id or model is RebuildDataValue and s.value_id == row.id)})
            if model is PaperSection and section_files.get(str(row.id)):
                fids.add(section_files[str(row.id)])
            items.append({'kind': kind, 'object_id': str(row.id), 'object_version': digest(image),
                          'source_file_ids': sorted(fids), 'source_binding_status': 'recorded' if fids else 'unknown_legacy_or_fileless'})
    paper = session.get(Paper, paper_id)
    for key, value in [('abstract', (paper.comprehensive_analysis or {}).get('abstract_zh')),
                       ('guide', (paper.reading_guide or {}).get('beginner_zh'))]:
        if keys is None or ('reading_module', key) in keys:
            guide = paper.reading_guide or {}
            fids = ([f['file_id'] for f in (guide.get('beginner_zh') or {}).get('source_files', [])] if key == 'guide' else
                    [(guide.get('abstract_translation', {}).get('source') or {}).get('file_id')])
            items.append({'kind': 'reading_module', 'object_id': key, 'object_version': digest(value),
                          'source_file_ids': sorted(set(f for f in fids if f)),
                          'source_binding_status': 'recorded' if any(fids) else 'unknown_or_metadata'})
    return items


def issue_projection(session, paper_id, *, current=None):
    """Immutable review receipts remain the truth even after a new freeze."""
    issues = {}
    events = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == 'independent_review', AuditLog.source == 'rebuild_acceptance_v1').order_by(AuditLog.created_at, AuditLog.id)))
    keys = {(i['kind'], i['object_id']) for event in events
            for i in ((event.payload.get('receipt') or {}).get('review') or {}).get('failed_items', [])}
    keys.update((i['kind'], i.get('current_object_id') or i['object_id']) for event in events
                for i in ((event.payload.get('receipt') or {}).get('review') or {}).get('resolved_items', []))
    if not keys:
        return []
    current = current if current is not None else {(i['kind'], i['object_id']): i['object_version']
                                                  for i in object_versions(session, paper_id, keys=keys)}
    for event in events:
        review = (event.payload.get('receipt') or {}).get('review') or {}
        for item in review.get('failed_items', []):
            key = (event.target_id, item['issue_id'])
            if key not in issues:
                issues[key] = {**item, 'prior_freeze_id': event.target_id, 'review_event_id': str(event.id),
                               'status': 'open', 'current_object_version': current.get((item['kind'], item['object_id']))}
        for item in review.get('resolved_items', []):
            key = (item['prior_freeze_id'], item['issue_id'])
            if key in issues:
                issues[key].update(status='independently_resolved', resolution_event_id=str(event.id),
                                   resolved_object_version=item['current_object_version'],
                                   resolved_object_id=item.get('current_object_id') or item['object_id'])
    for item in issues.values():
        item['object_freshness'] = ('unchanged' if item['current_object_version'] == item['object_version'] else
                                    'changed_requires_independent_recheck' if item['current_object_version'] else 'object_unavailable')
        if item['status'] == 'independently_resolved' and current.get((item['kind'], item['resolved_object_id'])) != item['resolved_object_version']:
            item['status'] = 'changed_after_independent_resolution'
    return list(issues.values())


def validate_review_items(session, paper_id, frozen, payload, *, actor, identity):
    frozen_versions = {(i['kind'], i['object_id']): i['object_version'] for i in frozen.payload.get('object_versions', [])}
    current = {(i['kind'], i['object_id']): i['object_version'] for i in object_versions(session, paper_id)}
    files = {i['file_id'] for i in frozen.payload['coverage']}
    for item in payload.failed_items:
        key = (item.kind, item.object_id)
        binding = next((i for i in frozen.payload.get('object_versions', []) if (i['kind'], i['object_id']) == key), {})
        if (not item.required_change.strip() or frozen_versions.get(key) != item.object_version or
                current.get(key) != item.object_version or not {str(f) for f in item.source_file_ids} <=
                files.intersection(binding.get('source_file_ids', []))):
            raise RebuildWorkflowError('review_issue_object_version_or_source_mismatch')
    seen = set()
    for item in payload.resolved_items:
        resolution_key = (item.prior_freeze_id, item.issue_id)
        if resolution_key in seen:
            raise RebuildWorkflowError('review_resolution_repeated')
        seen.add(resolution_key)
        prior = session.get(WorkflowJob, item.prior_freeze_id)
        if (not prior or prior.type != frozen.type or prior.payload.get('paper_id') != str(paper_id) or
                prior.job_id == frozen.job_id or not prior.result or not item.note.strip()):
            raise RebuildWorkflowError('review_prior_issue_not_found')
        original = next((i for i in issue_projection(session, paper_id, current=current)
                         if i['prior_freeze_id'] == item.prior_freeze_id and i['issue_id'] == item.issue_id and i['status'] != 'independently_resolved'), None)
        key = (item.kind, item.object_id)
        new_key = (item.kind, item.current_object_id or item.object_id)
        if (not original or (original['kind'], original['object_id'], original['object_version']) !=
                (*key, item.prior_object_version) or frozen_versions.get(new_key) != item.current_object_version or
                current.get(new_key) != item.current_object_version or item.current_object_version == item.prior_object_version):
            raise RebuildWorkflowError('review_resolution_object_version_mismatch')
        event = session.get(AuditLog, item.correction_event_id)
        issue_event = session.get(AuditLog, UUID(original['review_event_id']))
        if (not event or not issue_event or event.created_at < issue_event.created_at or event.created_at > frozen.created_at):
            raise RebuildWorkflowError('review_correction_outside_issue_freeze_interval')
        verified_writer_binding = bool(event and event.payload.get('task_id') and
                                       event.payload['task_id'] == frozen.payload.get('writer_task_id'))
        if event and event.action == event.source == 'beginner_zh_v1':
            source_job = session.get(WorkflowJob, (event.payload.get('receipt') or {}).get('job_id', ''))
            verified_writer_binding = bool(source_job and source_job.payload.get('paper_id') == str(paper_id) and
                source_job.payload.get('actor') == actor and identity.get('writer_thread_id') and
                (source_job.progress or {}).get('thread_id') == identity['writer_thread_id'] and
                identity['writer_thread_id'] != identity.get('reviewer_thread_id'))
        if event and identity.get('correction_witness'):
            from app.services.rebuild_correction_witness import event_hash
            witnessed = next((r for r in identity['correction_witness'].get('events', []) if
                              r.get('event_id') == str(event.id) and r.get('prior_freeze_id') == item.prior_freeze_id and
                              r.get('issue_id') == item.issue_id and r.get('event_sha256') == event_hash(event)), None)
            verified_writer_binding = verified_writer_binding or bool(witnessed)
        if event and event.payload.get('actor') == actor and not (
                identity.get('kind') == 'coordinator_authorized_native_task' and
                verified_writer_binding and
                payload.reviewer_task_id != event.payload.get('task_id')):
            raise RebuildWorkflowError('correction_writer_cannot_close_own_issue')
        if event and item.kind == 'paper_sections' and event.action == 'section_boundary_corrected' and event.source == 'rebuild_section_corrections_v1':
            from app.services.rebuild_sections import _checked_snapshot
            declaration = event.payload.get('declaration') or {}
            targets = [declaration.get('old_section', {}), *declaration.get('new_sections', [])]
            expected_ids = [t.get('section_id') for t in targets]
            if (event.paper_id != paper_id or event.target_id != item.object_id or
                    (declaration.get('old_section') or {}).get('section_id') != item.object_id or
                    new_key[1] not in expected_ids[1:] or current.get(key) != item.prior_object_version):
                raise RebuildWorkflowError('review_section_replacement_not_bound')
            for target in targets:
                imported = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
                    AuditLog.action == 'section_imported', AuditLog.source == 'rebuild_sections_v1',
                    AuditLog.target_id == target['section_id']))
                _checked_snapshot(session.get(PaperSection, UUID(target['section_id'])), imported)
                if imported.payload.get('content_sha256') != target.get('content_sha256'):
                    raise RebuildWorkflowError('review_section_replacement_not_bound')
            receipt = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
                AuditLog.action == 'section_correction_request', AuditLog.source == event.source,
                AuditLog.target_id == event.payload.get('request_id')))
            if (not receipt or receipt.payload.get('actor') != event.payload.get('actor') or
                    not any(r.get('audit_id') == str(event.id) for r in receipt.payload.get('result', {}).get('items', []))):
                raise RebuildWorkflowError('review_section_correction_receipt_unverified')
            continue
        if item.current_object_id and item.current_object_id != item.object_id:
            raise RebuildWorkflowError('review_replacement_requires_section_correction')
        if event and item.kind == 'reading_module' and event.action == event.source == 'beginner_zh_v1':
            field = 'abstract_zh' if item.object_id == 'abstract' else 'reading_guide'
            before = (event.payload.get('before') or {}).get(field)
            after = (event.payload.get('after') or {}).get(field)
            if item.object_id == 'guide':
                before = (before or {}).get('beginner_zh')
                after = (after or {}).get('beginner_zh')
            if (event.paper_id != paper_id or digest(before) != item.prior_object_version or digest(after) != item.current_object_version or
                    not (event.payload.get('receipt') or {}).get('saved')):
                raise RebuildWorkflowError('review_reading_correction_not_bound_to_issue')
            continue
        # Actual formal mutation before/after is the retained preimage and
        # readback proof, never a client-supplied "fixed" assertion.
        if (not event or event.paper_id != paper_id or event.action != EVENT or event.source != VERSION or
                (event.target_type, event.target_id) != key or
                digest(event.payload.get('before')) != item.prior_object_version or
                digest(event.payload.get('after')) != item.current_object_version):
            raise RebuildWorkflowError('review_correction_event_not_bound_to_issue')
        receipts = session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
            AuditLog.action == RECEIPT, AuditLog.source == VERSION,
            AuditLog.target_id == event.payload.get('request_id'))).all()
        if not any(r.payload.get('actor') == event.payload.get('actor') and
                   str(event.id) in r.payload.get('result', {}).get('audit', {}).get('event_ids', []) for r in receipts):
            raise RebuildWorkflowError('review_correction_receipt_unverified')
    resolving = {(i.prior_freeze_id, i.issue_id) for i in payload.resolved_items}
    if payload.decision == 'accepted' and any(
            item['status'] != 'independently_resolved' and (item['prior_freeze_id'], item['issue_id']) not in resolving
            for item in issue_projection(session, paper_id, current=current)):
        raise RebuildWorkflowError('open_independent_issues_require_resolution')
