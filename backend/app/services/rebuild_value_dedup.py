"""Explicit same-fact consolidation. Preview never infers scientific equivalence.

No schema migration: complete restoration images and request receipts live in the
canonical value's conflict.merge_history, without fabricating a value conflict.
The endpoint owns the transaction; this service also uses a savepoint so a caller
cannot accidentally commit partial changes after an exception.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Paper, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.services.rebuild_workflow_service import RebuildWorkflowError, _canonical_json, _source_key, serialize_source


def _fail(code):
    raise RebuildWorkflowError('dedup_' + code)


def _image(obj):
    # JSON-safe full physical row, including all historic conflict/precision data.
    return json.loads(json.dumps({c.name: getattr(obj, c.name) for c in obj.__table__.columns}, default=str))


def _hash(data):
    return hashlib.sha256(_canonical_json(data).encode()).hexdigest()


def dedup_contract():
    return {
        'preview': 'POST /api/rebuild/papers/{paper_id}/value-dedup/preview',
        'apply': 'POST /api/rebuild/papers/{paper_id}/value-dedup/apply',
        'authentication': 'trusted workbench session; both POSTs require same-origin and X-CSRF-Token',
        'preview_body': {'pairs': [{'keep_value_id': 'UUID', 'duplicate_value_id': 'UUID',
                                  'same_fact': True, 'reason': 'mandatory scientific reason',
                                  'identity_assertion': 'mandatory explicit same-fact identity, explaining field/condition differences'}]},
        'apply_additions': {'expected_plan_revision': 'fresh preview.plan_revision',
                            'confirm_delete_value_ids': 'exact preview.delete_value_ids, no extras or repeats',
                            'confirm_delete_row_ids': 'exact preview.delete_row_ids, no extras or repeats',
                            'expected_move_source_ids': 'exact preview.move_source_ids',
                            'request_id': 'UUID, persist exact payload for retry', 'reason': 'explicit user deletion approval and reason'},
        'rules': [
            'Preview is read-only, provides full before/after images, and does not adjudicate science or authorize deletion.',
            'Apply requires separate explicit user approval of every deletion; deployment is not approval.',
            'Only specified same-paper pairs; exact material, support, site, configuration, family, reaction and data_type must agree.',
            'Keep the oldest value; preserve canonical UUID, raw precision, unit, type, preferred source and previous history.',
            'Every duplicate source UUID survives; recomputed identity key must match stored key and must not collide.',
            'Only listed duplicate values and rows becoming empty are deleted; no cascade or arbitrary delete.',
            'Paper/rows/values/sources are locked and preview recomputed; related reference tables take short transaction read locks to prevent reference races.',
            'External active references, malformed ownership, stale state or counts reject atomically with 409.',
            'Full retired row/value and all source before/after images, trusted actor/time/reason/request/revision are appended to merge_history; no fake scientific conflict.',
            'Same request_id + actor + payload replays without writes; changed actor/payload conflicts. request_id is global across merge requests.',
            'After an uncertain response read the authenticated work package and canonical merge_history before retrying.'
        ],
    }


def _tables(session):
    # current_schema keeps PostgreSQL fixture schemas fully isolated from public.
    return session.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name")).scalars().all()


def _quoted(session, name):
    return session.get_bind().dialect.identifier_preparer.quote(name)


def _references(session, retired_ids, exempt, tables):
    """Fail closed across FK, UUID, preferred, JSON, analysis and text references.

    Scan physical rows rather than an ORM-maintained, necessarily incomplete list.
    Retired beforeimages and moved-source ownership are the only exemptions.
    Historical merge receipts are removed only from surviving value audit JSON;
    all other conflict/history references remain subject to the active-ref check.
    """
    result = []
    for table in tables:
        quoted = _quoted(session, table)
        rows = session.execute(text('SELECT to_jsonb(t) FROM ' + quoted + ' t WHERE ' +
                                    ' OR '.join('strpos(lower(to_jsonb(t)::text), :id'+str(i)+') > 0' for i in range(len(retired_ids)))),
                               {'id'+str(i): ident for i, ident in enumerate(retired_ids)})
        for (row,) in rows:
            if str(row.get('id')) in exempt.get(table, set()):
                if table == RebuildValueSource.__tablename__:
                    row = deepcopy(row)
                    row.pop('value_id', None)
                    row.pop('row_id', None)
                else:
                    continue
            if table == RebuildDataValue.__tablename__ and isinstance(row.get('conflict'), dict):
                row = deepcopy(row)
                row['conflict'].pop('merge_history', None)
            encoded = json.dumps(row, sort_keys=True, default=str).lower()
            found = sorted(i for i in retired_ids if i in encoded)
            if found:
                result.append({'table': table, 'record_id': str(row.get('id')), 'references': found})
    return result


def _state(session, paper_id, *, lock=False):
    q = select(Paper).where(Paper.id == paper_id).execution_options(populate_existing=True)
    paper = session.scalar(q.with_for_update() if lock else q)
    if paper is None:
        _fail('paper_not_found')
    state = {Paper: [paper]}
    for model in (RebuildDataRow, RebuildDataValue, RebuildValueSource):
        q = select(model).where(model.paper_id == paper_id).order_by(model.id).execution_options(populate_existing=True)
        if lock:
            q = q.with_for_update().execution_options(populate_existing=True)
        state[model] = list(session.scalars(q))
    return state


def _preview(session, paper_id, payload, state, tables):
    rows = {r.id: r for r in state[RebuildDataRow]}
    values = {v.id: v for v in state[RebuildDataValue]}
    sources = state[RebuildValueSource]
    ids = [i for p in payload.pairs for i in (p.keep_value_id, p.duplicate_value_id)]
    related_sources = session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id.in_(ids))).all()
    for source in related_sources:
        value = values.get(source.value_id)
        if value is None or source.paper_id != paper_id or source.row_id != value.row_id:
            _fail('source_owner_inconsistent')
    if len(set(ids)) != len(ids):
        _fail('overlapping_pairs')
    moves, pairs, retired_values, retired_rows = [], [], [], []
    for pair in payload.pairs:
        if not pair.reason.strip() or not pair.identity_assertion.strip():
            _fail('pair_reason_identity_required')
        keep, dup = (values.get(i) for i in (pair.keep_value_id, pair.duplicate_value_id))
        if keep is None or dup is None:
            _fail('value_missing_or_cross_paper')
        kr, dr = rows.get(keep.row_id), rows.get(dup.row_id)
        if kr is None or dr is None:
            _fail('owner_inconsistent')
        identity = ('material', 'support', 'active_site_type', 'active_site', 'configuration', 'material_family', 'reaction', 'data_type')
        if any(getattr(kr, k) != getattr(dr, k) for k in identity):
            _fail('identity_mismatch')
        if keep.created_at is None or dup.created_at is None or keep.created_at > dup.created_at:
            _fail('canonical_not_oldest')
        # Different numbers/units/types are not a deduplication operation.
        if (keep.numeric_value, keep.unit, keep.value_type, keep.is_estimated, keep.missing_reason) != (dup.numeric_value, dup.unit, dup.value_type, dup.is_estimated, dup.missing_reason):
            _fail('value_mismatch')
        if keep.numeric_value is None and keep.raw_value != dup.raw_value:
            _fail('value_mismatch')
        owned = [s for s in sources if s.value_id in (keep.id, dup.id)]
        keys = set()
        for source in owned:
            parent = values[source.value_id]
            if source.row_id != parent.row_id or source.paper_id != parent.paper_id:
                _fail('source_owner_inconsistent')
            key = _source_key(serialize_source(source))
            if key != source.source_key:
                _fail('source_key_invalid')
            if key in keys:
                _fail('source_key_conflict')
            keys.add(key)
        for value in (keep, dup):
            if value.preferred_source_id and value.preferred_source_id not in {s.id for s in owned if s.value_id == value.id}:
                _fail('preferred_source_invalid')
        pair_moves = []
        for source in sorted(owned, key=lambda s: str(s.id)):
            before = _image(source)
            after = {**before, 'value_id': str(keep.id), 'row_id': str(kr.id)}
            if source.value_id == dup.id:
                moves.append(str(source.id)); pair_moves.append(str(source.id))
            # Include keeper sources too: full evidence for restoration and CAS.
            pairs.append({'pair_index': len(retired_values), 'source_before': before, 'source_after': after})
        retired_values.append(str(dup.id))
        pairs.append({'assertion': pair.model_dump(mode='json'), 'keep_row': _image(kr), 'keep_value': _image(keep),
                      'duplicate_row': _image(dr), 'duplicate_value': _image(dup), 'move_source_ids': pair_moves})
    # Source UUID references remain valid, but a different value's preferred
    # source must not silently acquire a new owner.
    external_preferred = session.scalar(select(RebuildDataValue.id).where(
        RebuildDataValue.preferred_source_id.in_([UUID(i) for i in moves]),
        RebuildDataValue.id.not_in(ids)).limit(1))
    if external_preferred is not None:
        _fail('external_preferred_source_conflict')
    retired_set = set(retired_values)
    candidate_rows = {values[UUID(i)].row_id for i in retired_values}
    for row_id in sorted(candidate_rows, key=str):
        if all(str(v.id) in retired_set for v in values.values() if v.row_id == row_id):
            if any(s.row_id == row_id and str(s.value_id) not in retired_set for s in sources):
                _fail('row_source_owner_inconsistent')
            retired_rows.append(str(row_id))
    retired = sorted(retired_values + retired_rows)
    refs = _references(session, retired, {
        RebuildDataValue.__tablename__: set(retired_values), RebuildDataRow.__tablename__: set(retired_rows),
        RebuildValueSource.__tablename__: set(moves)}, tables)
    if refs:
        _fail('external_reference_conflict')
    # Full paper state covers old history, membership, empty-row eligibility and
    # nonparticipating preferred/conflict records; changes invalidate the plan.
    full = {model.__tablename__: [_image(o) for o in objects] for model, objects in state.items()}
    result = {'paper_id': str(paper_id), 'pairs': pairs, 'delete_value_ids': sorted(retired_values),
              'delete_row_ids': sorted(retired_rows), 'move_source_ids': sorted(moves),
              'counts': {'delete_values': len(retired_values), 'delete_rows': len(retired_rows), 'move_sources': len(moves), 'delete_sources': 0},
              'reference_tables': tables, 'external_references': refs}
    result['plan_revision'] = _hash({'plan': result, 'paper_state': full})
    return result


def preview_dedup(session: Session, paper_id: UUID, payload):
    with session.no_autoflush:
        return _preview(session, paper_id, payload, _state(session, paper_id), _tables(session))


def apply_dedup(session: Session, paper_id: UUID, payload, *, actor: str):
    if not actor or not actor.strip() or not payload.reason.strip():
        _fail('actor_reason_required')
    fingerprint = _hash({'paper_id': str(paper_id), 'payload': payload.model_dump(mode='json')})
    with session.begin_nested():
        # Paper first, matching existing scientific writers; then reference read
        # locks before reading rows, so concurrent JSON/reference inserts cannot
        # slip between preflight and deletion. No locks in the preview path.
        session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update())
        tables = _tables(session)
        for table in tables:
            session.execute(text('LOCK TABLE ' + _quoted(session, table) + ' IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
        if tables != _tables(session):
            _fail('reference_schema_changed')
        state = _state(session, paper_id, lock=True)
        def receipts(node):
            if isinstance(node, dict):
                if 'receipt' in node and 'request_fingerprint' in node and 'plan_revision' in node:
                    yield node
                for child in node.values():
                    yield from receipts(child)
            elif isinstance(node, list):
                for child in node:
                    yield from receipts(child)
        for value in session.scalars(select(RebuildDataValue).where(RebuildDataValue.conflict.is_not(None)).execution_options(populate_existing=True)):
            for event in receipts(value.conflict or {}):
                if event.get('request_id') == str(payload.request_id):
                    if (event.get('actor'), event.get('request_fingerprint')) != (actor, fingerprint):
                        _fail('request_id_reused')
                    return deepcopy(event['receipt'])
        preview = _preview(session, paper_id, payload, state, tables)
        if preview['plan_revision'] != payload.expected_plan_revision:
            _fail('revision_conflict')
        for name, expected in [('confirm_delete_value_ids', 'delete_value_ids'), ('confirm_delete_row_ids', 'delete_row_ids'), ('expected_move_source_ids', 'move_source_ids')]:
            if sorted(map(str, getattr(payload, name))) != preview[expected]:
                _fail('confirmation_mismatch')
        receipt = {'paper_id': str(paper_id), 'request_id': str(payload.request_id), 'plan_revision': preview['plan_revision'],
                   **{k: preview[k] for k in ('delete_value_ids', 'delete_row_ids', 'move_source_ids', 'counts')}}
        event = {'actor': actor, 'recorded_at': datetime.now(timezone.utc).isoformat(), 'reason': payload.reason,
                 'request_id': str(payload.request_id), 'request_fingerprint': fingerprint,
                 'plan_revision': preview['plan_revision'], 'preview': preview, 'receipt': receipt}
        values = {v.id: v for v in state[RebuildDataValue]}
        sources = {str(s.id): s for s in state[RebuildValueSource]}
        for pair in payload.pairs:
            keep = values[pair.keep_value_id]
            conflict = deepcopy(keep.conflict or {})
            conflict['merge_history'] = [*conflict.get('merge_history', []), deepcopy(event)]
            keep.conflict = conflict
        for image in preview['pairs']:
            if 'source_before' in image and image['source_before']['id'] in preview['move_source_ids']:
                source = sources[image['source_before']['id']]
                source.value_id = UUID(image['source_after']['value_id'])
                source.row_id = UUID(image['source_after']['row_id'])
        session.flush()
        # Explicit Core DELETE avoids ORM cascades; reference preflight prevents
        # database cascades. RETURNING verifies the
        # affected UUID set, rather than trusting ORM scheduling/counts.
        for model, key in ((RebuildDataValue, 'delete_value_ids'), (RebuildDataRow, 'delete_row_ids')):
            from sqlalchemy import delete
            ids = [UUID(i) for i in preview[key]]
            if ids:
                actual = sorted(map(str, session.scalars(delete(model).where(model.id.in_(ids)).returning(model.id))))
                if actual != preview[key]:
                    _fail('delete_count_conflict')
        session.flush()
        return receipt
