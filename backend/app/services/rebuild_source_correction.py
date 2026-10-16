"""Authenticated, UUID-preserving provenance correction; never edits scientific values."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Paper, RebuildDataRow, RebuildDataValue, RebuildValueSource
from app.schemas.rebuild import RebuildSourceCorrectionRequest
from app.services.rebuild_workflow_service import (
    RebuildWorkflowError, _canonical_json, _source_key, _source_is_sufficient,
    get_file, get_asset, serialize_source,
)


def _snapshot(source):
    return {**serialize_source(source), 'paper_id': str(source.paper_id),
            'row_id': str(source.row_id), 'value_id': str(source.value_id),
            'created_at': source.created_at.isoformat() if source.created_at else None}


def source_state(session: Session, source_id: UUID):
    source = session.get(RebuildValueSource, source_id)
    if source is None:
        raise RebuildWorkflowError('source_not_found')
    value = session.get(RebuildDataValue, source.value_id)
    if value is None:
        raise RebuildWorkflowError('value_not_found')
    history = [entry for entry in ((value.conflict or {}).get('source_corrections') or [])
               if entry.get('source_id') == str(source.id)]
    snapshot = _snapshot(source)
    revision = hashlib.sha256(_canonical_json({'source': snapshot, 'history': history}).encode()).hexdigest()
    return {**snapshot, 'source_revision': revision, 'source_corrections': history,
            'preferred_source_id': str(value.preferred_source_id) if value.preferred_source_id else None}


def source_correction_contract():
    return {
        'read': 'GET /api/rebuild/sources/{source_id}',
        'write': 'PUT /api/rebuild/sources/{source_id}',
        'authentication': 'workbench session; PUT also requires same-origin and X-CSRF-Token',
        'body': {'paper_id': 'owning paper UUID', 'expected_revision': 'fresh source_revision from GET',
                 'request_id': 'new UUID; persist exact body for retries', 'correction_reason': 'mandatory nonblank reason',
                 'source': 'complete replacement: file_id, asset_id, source_kind, page_number, label, table_row, table_column, quote, estimate_basis (all keys required; nullable except file_id and source_kind)'},
        'rules': ['page_number is 1-based physical ORIGINAL PDF page; DOCX must use null, never derived PDF pages',
                  'table coordinates are zero-based nonnegative integers, both required for table kind; other kinds must use null',
                  'file and asset must belong to paper; asset.file_id must equal file_id',
                  'source_key is recomputed from all identity fields; collision is 409 without merge or deletion',
                  'revision covers complete stored identity, old key and history; stale revision is 409',
                  'UUIDs, preferred source and scientific values stay unchanged; complete before/after and trusted actor are appended to value.conflict.source_corrections',
                  'exact request_id replay returns current source without writing; changed request or actor is 409',
                  'after unknown outcome GET and compare history/request_id before resending; scientific acceptance is separate'],
    }


def correct_source(session: Session, source_id: UUID, payload: RebuildSourceCorrectionRequest,
                   settings: Settings, *, actor: str):
    # Same lock order as imports/value/row edits. Refresh ORM objects after waiting.
    paper_id = session.scalar(select(RebuildValueSource.paper_id).where(RebuildValueSource.id == source_id))
    if paper_id is None:
        raise RebuildWorkflowError('source_not_found')
    if paper_id != payload.paper_id:
        raise RebuildWorkflowError('source_paper_mismatch')
    session.scalar(select(Paper.id).where(Paper.id == paper_id).with_for_update())
    value_id = session.scalar(select(RebuildValueSource.value_id).where(RebuildValueSource.id == source_id))
    value = session.scalar(select(RebuildDataValue).where(RebuildDataValue.id == value_id)
                           .with_for_update().execution_options(populate_existing=True))
    source = session.scalar(select(RebuildValueSource).where(RebuildValueSource.id == source_id)
                            .with_for_update().execution_options(populate_existing=True))
    row = session.get(RebuildDataRow, source.row_id)
    if value is None or row is None or (value.paper_id, value.row_id, row.paper_id) != (paper_id, source.row_id, paper_id):
        raise RebuildWorkflowError('source_owner_inconsistent')
    if not actor or not actor.strip():
        raise RebuildWorkflowError('source_actor_required')
    fingerprint = hashlib.sha256(_canonical_json(payload.model_dump(mode='json')).encode()).hexdigest()
    state = source_state(session, source_id)
    for entry in ((value.conflict or {}).get('source_corrections') or []):
        if entry.get('request_id') == str(payload.request_id):
            if (entry.get('request_fingerprint'), entry.get('actor'), entry.get('source_id')) != (fingerprint, actor, str(source_id)):
                raise RebuildWorkflowError('source_request_id_reused')
            return state
    if state['source_revision'] != payload.expected_revision:
        raise RebuildWorkflowError('source_revision_conflict')
    if not payload.correction_reason.strip():
        raise RebuildWorkflowError('correction_reason_required')
    data = payload.source.model_dump()
    if any(isinstance(v, str) and '\x00' in v for v in data.values()) or '\x00' in payload.correction_reason:
        raise RebuildWorkflowError('database_constraint_rejected')
    if not _source_is_sufficient(data):
        raise RebuildWorkflowError('source_requires_page_label_quote_or_asset')
    file = get_file(session, data['file_id'])
    if file.paper_id != paper_id:
        raise RebuildWorkflowError('source_file_belongs_to_other_paper')
    if data['asset_id'] is not None:
        asset = get_asset(session, data['asset_id'])
        if asset.paper_id != paper_id:
            raise RebuildWorkflowError('source_asset_belongs_to_other_paper')
        if asset.file_id != file.id:
            raise RebuildWorkflowError('source_asset_file_mismatch')
    coords = (data['table_row'], data['table_column'])
    if data['source_kind'] == 'table':
        if any(c is None for c in coords):
            raise RebuildWorkflowError('source_table_coordinates_required')
    elif any(c is not None for c in coords):
        raise RebuildWorkflowError('source_coordinates_require_table_kind')
    if data['source_kind'] in {'estimated', 'derived'} and not str(data['estimate_basis'] or '').strip():
        raise RebuildWorkflowError('source_estimate_basis_required')
    # Check evidence compatibility only; provenance repair must not rewrite or
    # revalidate unrelated legacy numeric representations.
    if value.value_type in {'estimated', 'derived'} and not str(data['estimate_basis'] or '').strip():
        raise RebuildWorkflowError('estimate_or_derivation_basis_required_for_each_source')
    if value.value_type == 'explicit' and data['source_kind'] in {'estimated', 'derived'}:
        raise RebuildWorkflowError('explicit_value_requires_explicit_evidence')
    from app.services.rebuild_file_preview import _checked_source
    path, kind = _checked_source(file, settings)
    if data['page_number'] is not None:
        import fitz
        if kind != 'pdf':
            raise RebuildWorkflowError('source_physical_page_requires_original_pdf')
        try:
            with fitz.open(path) as doc:
                if doc.needs_pass or data['page_number'] > doc.page_count:
                    raise RebuildWorkflowError('source_physical_page_out_of_range')
        except (fitz.FileDataError, fitz.EmptyFileError, OSError) as exc:
            raise RebuildWorkflowError('source_original_pdf_unreadable') from exc
    key = _source_key(data)
    for other in session.scalars(select(RebuildValueSource).where(RebuildValueSource.value_id == value.id,
                                                                  RebuildValueSource.id != source.id)):
        if other.source_key == key or _source_key(serialize_source(other)) == key:
            raise RebuildWorkflowError('source_key_conflict')
    before = _snapshot(source)
    # No mutation before all preflight checks. Endpoint owns commit/rollback.
    for field, val in data.items():
        setattr(source, field, val)
    source.source_key = key
    conflict = dict(value.conflict or {})
    conflict['source_corrections'] = [*(conflict.get('source_corrections') or []), {
        'source_id': str(source.id), 'value_id': str(value.id), 'paper_id': str(paper_id),
        'request_id': str(payload.request_id), 'request_fingerprint': fingerprint,
        'expected_revision': payload.expected_revision, 'before': before, 'after': _snapshot(source),
        'actor': actor, 'reason': payload.correction_reason,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
    }]
    value.conflict = conflict
    session.flush()
    return source_state(session, source.id)
