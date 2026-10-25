"""Explicit coordinator witness for exact legacy correction events, never backfill."""
import hashlib
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import select
from app.db.models import AuditLog
from app.services.rebuild_mutation_audit import safe_json
from app.services.rebuild_workflow_service import RebuildWorkflowError


def event_hash(event):
    body = {'id': event.id, 'paper_id': event.paper_id, 'action': event.action,
                     'source': event.source, 'target_type': event.target_type, 'target_id': event.target_id,
                     'created_at': event.created_at, 'payload': event.payload}
    # Hash actual stored payload, not a second redaction projection: a
    # scientific key resembling a credential must not conceal a mutation.
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def load_artifact(root, path, sha256):
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise RebuildWorkflowError('correction_witness_path_invalid')
    try:
        with path.open('rb') as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024 or hashlib.sha256(body).hexdigest() != sha256:
            raise ValueError('witness_bytes_changed')
        data = json.loads(body)
        if data.get('format') != 'coordinator_correction_witness_v1' or not isinstance(data.get('events'), list):
            raise ValueError('witness_format_invalid')
        if any(not isinstance(row, dict) or set(row) != {'event_id', 'event_sha256', 'prior_freeze_id', 'issue_id'} or
               any(not isinstance(value, str) or not value or len(value) > 100 for value in row.values()) for row in data['events']):
            raise ValueError('witness_event_shape_invalid')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RebuildWorkflowError('correction_witness_artifact_invalid') from exc
    return data, str(path.resolve().relative_to(root))


def validate_entries(session, frozen, owner, writer_thread_id, ids, data):
    from app.services.rebuild_review_items import issue_projection
    paper_id = UUID(frozen.payload['paper_id'])
    rows = data.get('events') or []
    if (data.get('paper_id') != str(paper_id) or data.get('writer_thread_id') != writer_thread_id or
            not 1 <= len(rows) <= 100 or len(ids) != len(set(ids)) or
            sorted(str(i) for i in ids) != sorted(r.get('event_id', '') for r in rows)):
        raise RebuildWorkflowError('correction_witness_scope_mismatch')
    issues = {(i['prior_freeze_id'], i['issue_id']): i for i in issue_projection(session, paper_id)}
    result = []
    for row in rows:
        try:
            event = session.get(AuditLog, UUID(row['event_id']))
        except (ValueError, KeyError, TypeError) as exc:
            raise RebuildWorkflowError('correction_witness_event_invalid') from exc
        issue = issues.get((row.get('prior_freeze_id'), row.get('issue_id')))
        if (not event or not issue or event.paper_id != paper_id or event.payload.get('actor') != owner or
                event_hash(event) != row.get('event_sha256') or event.created_at > frozen.created_at):
            raise RebuildWorkflowError('correction_witness_event_mismatch')
        original = session.get(AuditLog, UUID(issue['review_event_id']))
        if not original or event.created_at < original.created_at:
            raise RebuildWorkflowError('correction_witness_event_before_issue')
        if event.action == 'section_boundary_corrected' and event.source == 'rebuild_section_corrections_v1':
            if issue['kind'] != 'paper_sections' or event.target_id != issue['object_id']:
                raise RebuildWorkflowError('correction_witness_issue_target_mismatch')
            receipt_action = 'section_correction_request'
        elif event.action == event.source == 'beginner_zh_v1':
            if issue['kind'] != 'reading_module' or not (event.payload.get('receipt') or {}).get('saved'):
                raise RebuildWorkflowError('correction_witness_issue_target_mismatch')
            result.append(dict(row));continue
        elif event.action == 'rebuild_mutation' and event.source == 'rebuild_mutation_v1':
            if (event.target_type, event.target_id) != (issue['kind'], issue['object_id']):
                raise RebuildWorkflowError('correction_witness_issue_target_mismatch')
            receipt_action = 'rebuild_mutation_receipt'
        else:
            raise RebuildWorkflowError('correction_witness_protocol_unsupported')
        receipts = session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
            AuditLog.action == receipt_action, AuditLog.source == event.source,
            AuditLog.target_id == event.payload.get('request_id'))).all()
        if not any(r.payload.get('actor') == owner and (
                str(event.id) in r.payload.get('result', {}).get('audit', {}).get('event_ids', []) or
                any(i.get('audit_id') == str(event.id) for i in r.payload.get('result', {}).get('items', []))) for r in receipts):
            raise RebuildWorkflowError('correction_witness_receipt_unverified')
        result.append(dict(row))
    return result
