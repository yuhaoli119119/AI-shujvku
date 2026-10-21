"""Append-only file selection; file/scientific records and old bytes are immutable.

Legacy singleton main is preferred. Ambiguous legacy mains require an explicit
replacement declaration; we never choose a main by UUID or upload timestamp.
"""
from __future__ import annotations

import copy
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import AuditLog, Paper, RebuildPaperFile

ACTION = 'file_lifecycle_recorded'
VERSION = 'file_lifecycle_v1'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _records(session, paper_id):
    return list(session.scalars(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id)
                               .order_by(RebuildPaperFile.created_at, RebuildPaperFile.id)))


def file_set(session, paper_id):
    from app.services.rebuild_workflow_service import serialize_file, _required_paper
    _required_paper(session, paper_id)
    records = _records(session, paper_id)
    events = list(session.scalars(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == ACTION, AuditLog.source == VERSION).order_by(AuditLog.created_at, AuditLog.id)))
    superseded = set()
    for event in events:
        superseded.update(event.payload.get('superseded_file_ids', []))
    mains = [str(f.id) for f in records if f.role == 'main' and str(f.id) not in superseded]
    preferred = mains[0] if len(mains) == 1 else None
    items = [{**serialize_file(f), 'lifecycle_status': 'superseded' if str(f.id) in superseded else 'active',
              'preferred': str(f.id) not in superseded and (f.role == 'si' or str(f.id) == preferred),
              'scientific_review_required': str(f.id) in superseded} for f in records]
    revision = _hash({'files': items, 'events': sorted(str(e.id) for e in events)})
    return {'items': items, 'file_set_revision': revision, 'preferred_main_id': preferred,
            'active_file_ids': [f['id'] for f in items if f['preferred']],
            'ambiguous_main': len(mains) > 1,
            'scientific_review_required_file_ids': sorted(superseded),
            'automatic_parsing_started': False,
            'contract': {'upload': f'/api/rebuild/papers/{paper_id}/files',
                         'select_existing': f'/api/rebuild/papers/{paper_id}/files/lifecycle',
                         'fields': ['request_id', 'expected_revision', 'role', 'action', 'reason', 'supersede_file_ids'],
                         'actions': ['add', 'supersede'], 'history_preserved': True,
                         'scientific_bindings_remapped': False, 'scientific_acceptance': 'not_implied'}}


def active_files(session, paper_id):
    ids = set(file_set(session, paper_id)['active_file_ids'])
    return [f for f in _records(session, paper_id) if str(f.id) in ids]


def locked_replay(session, paper_id, request_id, fingerprint, actor):
    paper = session.scalar(select(Paper).where(Paper.id == paper_id).with_for_update())
    if paper is None:
        raise HTTPException(404, 'paper_not_found')
    previous = session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id,
        AuditLog.action == ACTION, AuditLog.source == VERSION,
        AuditLog.target_id == str(request_id)))
    if previous:
        if previous.payload['actor'] != actor or previous.payload['fingerprint'] != fingerprint:
            raise HTTPException(409, 'file_request_id_reused')
        receipt = copy.deepcopy(previous.payload['receipt'])
        receipt['audit']['replayed'] = True
        return receipt
    return None


def validate_change(session, paper_id, body):
    state = file_set(session, paper_id)
    if body['expected_revision'] != state['file_set_revision']:
        raise HTTPException(409, 'file_set_revision_conflict')
    targets = body['supersede_file_ids']
    if len(targets) != len(set(targets)):
        raise HTTPException(422, 'duplicate_supersede_file_id')
    if (body['action'] == 'add' and targets) or (body['action'] == 'supersede' and not targets):
        raise HTTPException(422, 'file_action_target_mismatch')
    by_id = {f['id']: f for f in state['items']}
    for target in targets:
        if target not in by_id:
            raise HTTPException(422, 'supersede_file_not_in_paper')
        if by_id[target]['role'] != body['role']:
            raise HTTPException(422, 'supersede_file_role_mismatch')
        if by_id[target]['lifecycle_status'] != 'active':
            raise HTTPException(409, 'file_already_superseded')
    return state


def apply_change(session, paper_id, body, actor, fingerprint, record, before, *, created):
    from app.services.rebuild_workflow_service import serialize_file
    fid = str(record.id)
    targets = body['supersede_file_ids']
    if fid in targets:
        raise HTTPException(409, 'file_cannot_supersede_itself')
    old = next((f for f in before['items'] if f['id'] == fid), None)
    if old and old['lifecycle_status'] == 'superseded':
        raise HTTPException(409, 'superseded_file_cannot_be_reactivated')
    remaining = [f for f in before['items'] if f['role'] == 'main' and f['lifecycle_status'] == 'active'
                 and f['id'] not in targets and f['id'] != fid]
    if record.role == 'main' and remaining:
        raise HTTPException(409, 'main_source_requires_explicit_supersede')
    event_id = uuid4()
    # Record all before/after identities, original filename/hash/size/storage,
    # and actor in one append-only event+receipt. No mutable lifecycle columns.
    after_items = copy.deepcopy(before['items'])
    if not old:
        after_items.append({**serialize_file(record), 'lifecycle_status': 'active',
                            'preferred': True, 'scientific_review_required': False})
    for item in after_items:
        if item['id'] in targets:
            item.update(lifecycle_status='superseded', preferred=False, scientific_review_required=True)
        elif item['role'] == 'main':
            item['preferred'] = item['id'] == fid if record.role == 'main' else item['preferred']
    receipt = {'file': serialize_file(record), 'created': created,
               'superseded_file_ids': targets, 'automatic_parsing_started': False,
               'scientific_bindings_remapped': False, 'scientific_acceptance': 'not_implied',
               'audit': {'event_ids': [str(event_id)], 'request_id': body['request_id'], 'replayed': False}}
    event = AuditLog(id=event_id, paper_id=paper_id, action=ACTION, source=VERSION,
        target_type='file_set', target_id=body['request_id'], payload={
            'actor': actor, 'request_id': body['request_id'], 'fingerprint': fingerprint,
            'request': body, 'reason': body['reason'], 'expected_revision': body['expected_revision'],
            'before': before['items'], 'after': after_items,
            'superseded_file_ids': targets, 'receipt': receipt})
    event_ids = list(session.scalars(select(AuditLog.id).where(AuditLog.paper_id == paper_id,
        AuditLog.action == ACTION, AuditLog.source == VERSION)))
    receipt['file_set_revision'] = _hash({'files': after_items,
        'events': sorted([*(str(e) for e in event_ids), str(event_id)])})
    active_mains = [f['id'] for f in after_items if f['role'] == 'main' and f['lifecycle_status'] == 'active']
    receipt['preferred_main_id'] = active_mains[0] if len(active_mains) == 1 else None
    event.payload = {**event.payload, 'receipt': copy.deepcopy(receipt)}
    session.add(event); session.flush()
    return receipt
