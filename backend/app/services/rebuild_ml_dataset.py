"""Versioned, offline rebuild-only dataset export. No acceptance inference or writes.

The caller must supply independently reviewed UUID dispositions bound to this
snapshot's graph revision. A digest proves freshness, never scientific validity.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.db.models import (AuditLog, Paper, RebuildPaperFile, RebuildVisualAsset,
                           RebuildDataRow, RebuildDataValue, RebuildValueSource)
from app.services.rebuild_table_content import TableData
from app.services.rebuild_source_lifecycle import source_is_current
from app.services.rebuild_workflow_service import _validate_value

VERSION = 'rebuild_ml_v1'
ACCEPTANCE_VERSION = 'rebuild_ml_acceptance_v1'
MODELS = {'papers': Paper, 'files': RebuildPaperFile, 'assets': RebuildVisualAsset,
          'rows': RebuildDataRow, 'values': RebuildDataValue, 'sources': RebuildValueSource}
STATUSES = {'eligible', 'partial', 'rejected', 'unresolved', 'missing', 'conflict', 'leakage'}
SOURCE_KINDS = frozenset({'explicit', 'estimated', 'derived', 'table', 'text', 'figure'})
SHA = re.compile(r'^[0-9a-f]{64}$')
IDENTITY = ('reaction', 'material', 'support', 'active_site_type', 'active_site',
            'configuration', 'material_family', 'data_type', 'condition', 'properties')


def normalized(value):
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {str(k): normalized(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    return value


def encoded(value):
    return (json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def record(obj):
    return normalized({c.name: getattr(obj, c.name) for c in obj.__table__.columns})


def read_snapshot(connection, paper_ids):
    """Only SELECT in a server-enforced repeatable-read/read-only transaction.

    Accept a fresh Connection, not an application Session (which could autoflush).
    Roll back even on success. Never initialize schemas or application services.
    """
    ids = sorted({UUID(str(x)) for x in paper_ids}, key=str)
    if not ids:
        raise ValueError('explicit nonempty paper scope required')
    if connection.dialect.name != 'postgresql' or connection.in_transaction():
        raise ValueError('fresh PostgreSQL connection required')
    with connection.begin():
        connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        with Session(bind=connection, autoflush=False) as session:
            graph = {}
            queries = []
            for key in ('papers', 'rows', 'values', 'sources', 'assets', 'files'):
                model = MODELS[key]
                condition = model.id.in_(ids) if key == 'papers' else model.paper_id.in_(ids)
                # Keep inconsistent foreign references visible instead of silently
                # losing them in inner joins or filtering them before validation.
                if key == 'values':
                    condition = or_(condition, model.row_id.in_([UUID(x['id']) for x in graph['rows']]))
                if key == 'sources':
                    condition = or_(condition, model.value_id.in_([UUID(x['id']) for x in graph['values']]))
                if key in {'assets', 'files'}:
                    ref = 'asset_id' if key == 'assets' else 'file_id'
                    refs = {UUID(x[ref]) for x in graph['sources'] if x[ref]}
                    if key == 'files':
                        refs.update(UUID(x['file_id']) for x in graph['assets'] if x['file_id'])
                    condition = or_(condition, model.id.in_(refs))
                query = select(model).where(condition).order_by(model.id)
                queries.append(str(query.compile(dialect=connection.dialect)))
                graph[key] = [record(x) for x in session.scalars(query)]
            graph['file_lifecycle'] = [record(x) for x in session.scalars(select(AuditLog).where(
                AuditLog.paper_id.in_(ids), AuditLog.action == 'file_lifecycle_recorded',
                AuditLog.source == 'file_lifecycle_v1').order_by(AuditLog.id))]
            meta = dict(connection.execute(text("SELECT current_setting('transaction_isolation') AS isolation, "
                "current_setting('transaction_read_only') AS read_only")).mappings().one())
        connection.rollback()
    if sorted(x['id'] for x in graph['papers']) != [str(x) for x in ids]:
        raise ValueError('scope contains missing papers')
    return {'schema_version': VERSION, 'scope_paper_ids': [str(x) for x in ids],
            'transaction': meta, 'query_bundle_sha256': digest(queries + ['file_lifecycle_recorded:file_lifecycle_v1']),
            'graph': graph, 'snapshot_sha256': digest(graph)}


def paper_revision(snapshot, paper_id):
    return digest({k: [x for x in rows if x.get('paper_id', x.get('id')) == paper_id]
                   for k, rows in snapshot['graph'].items()})


def validate_manifest(manifest, snapshot):
    if manifest.get('schema_version') != ACCEPTANCE_VERSION:
        raise ValueError('unsupported acceptance schema')
    if manifest.get('scope_paper_ids') != snapshot['scope_paper_ids']:
        raise ValueError('acceptance scope must exactly match sorted snapshot scope')
    policy = manifest.get('policy', {})
    if not policy.get('fields') or not policy.get('required_identity'):
        raise ValueError('explicit field and identity policy required')
    if policy.get('data_type') not in {'dft', 'experimental'}:
        raise ValueError('choose exactly one data_type')
    if not isinstance(policy['required_identity'], list) or not all(
            isinstance(x, str) and x for x in policy['required_identity']):
        raise ValueError('required_identity must be a nonempty list of paths')
    for field, spec in policy['fields'].items():
        if not field or spec.get('kind') not in {'numeric', 'text'} or not isinstance(spec.get('allowed_units'), list):
            raise ValueError('invalid field policy')
        if spec['kind'] == 'numeric' and (not spec['allowed_units'] or not all(
                isinstance(unit, str) and unit.strip() for unit in spec['allowed_units'])):
            raise ValueError('numeric fields require explicit nonempty units; use 1 for unitless')
        if spec.get('role') not in {'feature', 'target', 'metadata'}:
            raise ValueError('explicit field role required')
    for kind in MODELS:
        known = {x['id'] for x in snapshot['graph'][kind]}
        if set(manifest.get(kind, {})) - known:
            raise ValueError('disposition references unknown ' + kind + ' UUID')
        for key, decision in manifest.get(kind, {}).items():
            if str(UUID(key)) != key or decision.get('status') not in STATUSES:
                raise ValueError('invalid UUID disposition')
            if not SHA.fullmatch(decision.get('sha256', '')):
                raise ValueError('dispositions require content sha256')
    for pid, decision in manifest.get('papers', {}).items():
        if not decision.get('review_receipt') or not SHA.fullmatch(decision.get('review_evidence_sha256', '')):
            raise ValueError('independent paper review receipt and evidence hash required')
        if not SHA.fullmatch(decision.get('graph_revision', '')):
            raise ValueError('paper review must bind graph revision')
    return policy


def _eligible(manifest, kind, obj):
    entry = manifest.get(kind, {}).get(obj['id'], {})
    return entry.get('status') == 'eligible' and entry.get('sha256') == digest(obj)


def _path(obj, path):
    for key in path.split('.'):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _groups(observations):
    """Connected components prevent transitive DOI/file/identity split leakage.

    Duplicate IDs are within-paper exact identity groups, never record merges.
    Cross-paper identity matches are conservative split constraints, not claims
    of scientific equivalence. Include excluded observations in these constraints.
    """
    parent = {x['paper_id']: x['paper_id'] for x in observations}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]; key = parent[key]
        return key
    owners = {}
    for x in observations:
        identity = x['identity']
        scientific = digest({k: v for k, v in identity.items() if k != 'properties'})
        x['duplicate_group'] = digest([x['paper_id'], identity, x['field_name'], x['raw_value'], x['unit'], x['value_type']])
        tokens = [('paper', x['paper_id']), ('identity', scientific)]
        doi = (x['doi'] or '').strip().lower()
        doi = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:\s*)', '', doi)
        if doi: tokens.append(('doi', doi))
        tokens += [('file', s['file']['sha256']) for s in x['sources'] if s.get('file')]
        for token in tokens:
            if token in owners:
                a, b = sorted([root(x['paper_id']), root(owners[token])]); parent[b] = a
            owners[token] = x['paper_id']
    members = defaultdict(list)
    for pid in parent: members[root(pid)].append(pid)
    group_ids = {pid: digest(sorted(members[root(pid)])) for pid in parent}
    for x in observations: x['leakage_group'] = group_ids[x['paper_id']]


def _source_locator_errors(src, asset):
    """Fail closed on source type and table locators, independent of dispositions.

    Formal table coordinates must be strict nonnegative integers under this export
    contract. Stored locators are preserved verbatim, never rebased or inferred.
    Any coordinates require table kind, so relabeling cannot bypass this gate.
    Paper/file identity, physical page identity and dispositions are checked by
    the caller for every current source, including each table source.
    """
    errors = []
    kind = src.get('source_kind')
    if kind not in SOURCE_KINDS:
        errors.append('source_kind_not_supported')
    coords = (src.get('table_row'), src.get('table_column'))
    if kind != 'table':
        if any(c is not None for c in coords):
            errors.append('table_coordinates_require_table_kind')
        if kind in {'figure', 'estimated'}:
            if not asset:
                errors.append('figure_asset_missing')
            # A source describes how a value was read; subfigure is an asset
            # granularity, not a separate source kind (schemas/rebuild.py).
            elif asset.get('asset_type') not in {'figure', 'subfigure'}:
                errors.append('figure_asset_type_mismatch')
        elif kind in {'explicit', 'text', 'derived'} and not (src.get('quote') or '').strip():
            errors.append('text_quote_missing')
        return errors
    if any(c is None for c in coords):
        errors.append('table_coordinates_missing')
    elif not all(type(c) is int and c >= 0 for c in coords):
        errors.append('table_coordinates_invalid')
    if not src.get('asset_id') or not asset:
        errors.append('table_asset_missing')
    else:
        if asset.get('asset_type') != 'table':
            errors.append('table_asset_type_mismatch')
        pages = asset.get('page_numbers')
        if (not isinstance(pages, list) or not pages or
                not all(type(p) is int and p > 0 for p in pages)):
            errors.append('table_asset_pages_missing_or_invalid')
        elif src.get('page_number') not in pages:
            errors.append('page_outside_asset')
        provenance = asset.get('provenance')
        data = provenance.get('table_data') if isinstance(provenance, dict) else None
        try:
            table = TableData.model_validate(data)
        except ValueError:
            errors.append('table_data_missing_or_invalid')
        else:
            if all(type(c) is int and c >= 0 for c in coords) and (
                    coords[0] >= len(table.rows) or coords[1] >= len(table.columns)):
                errors.append('table_coordinates_out_of_range')
    if not (src.get('quote') or '').strip():
        errors.append('table_quote_missing')
    return errors


def build_dataset(snapshot, manifest, *, seed=0, code_revision):
    """Return deterministic artifact bytes. No DB access and no filesystem writes."""
    if snapshot.get('schema_version') != VERSION or snapshot['snapshot_sha256'] != digest(snapshot['graph']):
        raise ValueError('invalid snapshot digest/version')
    if snapshot['transaction'] != {'isolation': 'repeatable read', 'read_only': 'on'}:
        raise ValueError('read-only repeatable-read snapshot required')
    scope = snapshot['scope_paper_ids']
    if (scope != sorted({str(UUID(x)) for x in scope}) or
            sorted(x['id'] for x in snapshot['graph']['papers']) != scope):
        raise ValueError('snapshot graph papers must exactly match explicit scope')
    policy = validate_manifest(manifest, snapshot)
    graph = snapshot['graph']
    indexes = {kind: {x['id']: x for x in graph[kind]} for kind in MODELS}
    superseded = {fid for event in graph['file_lifecycle'] for fid in event['payload'].get('superseded_file_ids', [])}
    sources = defaultdict(list)
    for src in graph['sources']: sources[src['value_id']].append(src)
    revisions = {pid: paper_revision(snapshot, pid) for pid in snapshot['scope_paper_ids']}
    observations = []
    for value in sorted(graph['values'], key=lambda x: x['id']):
        reasons = []
        def exclude(reason): reasons.append(reason)
        pid = value['paper_id']; row = indexes['rows'].get(value['row_id'])
        paper = indexes['papers'].get(pid)
        if paper is None: raise ValueError('value outside explicit paper scope')
        review = manifest.get('papers', {}).get(pid, {})
        # Partial paper reviews can authorize explicitly eligible local objects;
        # they never promote the whole paper or a final train/val/test split.
        if (review.get('status') not in {'eligible', 'partial'} or review.get('sha256') != digest(paper)
                or review.get('graph_revision') != revisions.get(pid)):
            exclude('paper_acceptance_missing_or_stale')
        if row is None or row['paper_id'] != pid:
            exclude('row_paper_mismatch')
        row = row or {}
        for kind, obj in [('rows', row), ('values', value)]:
            if not obj or not _eligible(manifest, kind, obj): exclude(kind + '_not_eligible_or_stale')
        identity = {key: row.get(key) for key in IDENTITY}
        missing_identity = [key for key in policy['required_identity'] if _path(row, key) in (None, '', {}, [])]
        if missing_identity: exclude('identity_missing')
        if row.get('data_type') != policy['data_type']: exclude('data_type_out_of_scope')
        spec = policy['fields'].get(value['field_name'])
        if not spec: exclude('field_not_allowlisted')
        if re.search(r'predict|similarity|fingerprint|^ml_|预测|相似|指纹', value['field_name'], re.I):
            exclude('leakage_field_blocked')
        decision = manifest.get('values', {}).get(value['id'], {})
        if decision.get('leakage') != 'clear': exclude('leakage_not_cleared')
        conflict = value['conflict'] or {}
        conflict_status = conflict.get('status')
        if (conflict_status not in (None, 'evidence_history', 'corrected_previous_retained', 'resolved')
                or decision.get('conflict') != 'clear'):
            exclude('conflict_unresolved')
        current = []; historical = []
        for src in sorted(sources[value['id']], key=lambda x: x['id']):
            file = indexes['files'].get(src['file_id']); asset = indexes['assets'].get(src['asset_id'])
            source = {**src, 'file': file, 'asset': asset,
                      'page_identity': conflict.get('source_page_identities', {}).get(src['id'])}
            # Use lifecycle contract without mutating the ORM or source history.
            class StoredValue: pass
            stored = StoredValue(); stored.conflict = conflict
            if not source_is_current(stored, src['id']):
                historical.append(source); continue
            current.append(source)
            if src['paper_id'] != pid or src['row_id'] != value['row_id']:
                exclude('source_identity_mismatch')
            if not _eligible(manifest, 'sources', src): exclude('source_not_eligible_or_stale')
            if file is None or file['paper_id'] != pid:
                exclude('source_file_paper_mismatch')
            elif file['id'] in superseded or not _eligible(manifest, 'files', file):
                exclude('file_not_eligible_or_superseded')
            if src['asset_id'] and (asset is None or asset['paper_id'] != pid or asset['file_id'] != src['file_id']):
                exclude('source_asset_file_mismatch')
            if asset and not _eligible(manifest, 'assets', asset): exclude('asset_not_eligible_or_stale')
            page = src['page_number']; page_identity = source['page_identity'] or {}
            if (type(page) is not int or page < 1 or not file or
                    page_identity.get('source_sha256') != file['sha256'] or
                    page_identity.get('page_basis') not in {'source_pdf', 'derived_pdf'} or
                    not SHA.fullmatch(page_identity.get('reading_pdf_sha256', ''))):
                exclude('page_identity_missing_or_mismatched')
            if file and not SHA.fullmatch(file['sha256']): exclude('file_sha256_invalid')
            if (file and page_identity.get('page_basis') == 'source_pdf' and
                    page_identity.get('reading_pdf_sha256') != file['sha256']):
                exclude('source_pdf_hash_mismatch')
            if asset and (not isinstance(asset.get('page_numbers'), list) or
                    page not in asset['page_numbers']): exclude('page_outside_asset')
            reasons.extend(_source_locator_errors(src, asset))
        if not current: exclude('current_sources_missing')
        if value['preferred_source_id'] and value['preferred_source_id'] not in {s['id'] for s in current}:
            exclude('preferred_source_not_current')
        try:
            _validate_value({**value, 'sources': current})
        except ValueError as exc:
            exclude('invalid_value:' + str(exc))
        if value['value_type'] not in {'explicit', 'estimated', 'derived', 'missing'}: exclude('unknown_value_type')
        if value['value_type'] == 'missing': exclude('missing')
        if value['value_type'] == 'estimated' and (value['numeric_value'] is None or
                type(value['precision_digits']) is not int or not 0 <= value['precision_digits'] <= 2):
            exclude('estimated_projection_or_precision_missing')
        if value['value_type'] == 'derived':
            # v1 deliberately withholds derived values until input graph closure
            # and task-specific formula semantics have an independently reviewed contract.
            exclude('derived_input_closure_not_supported_v1')
        if spec:
            if value['unit'] not in spec['allowed_units']: exclude('unit_not_allowed')
            if spec['kind'] == 'numeric':
                if value['numeric_value'] is None: exclude('numeric_missing')
                if type(value['precision_digits']) is not int or value['precision_digits'] < 0:
                    exclude('precision_unknown')
            elif value['numeric_value'] is not None: exclude('text_policy_numeric_value')
        if value['is_estimated'] != (value['value_type'] == 'estimated'): exclude('estimated_flag_inconsistent')
        observations.append({**value, 'value_id': value['id'], 'doi': paper['doi'],
            'paper_code': paper['paper_code'], 'identity': identity, 'row': row,
            'identity_missing_fields': missing_identity, 'sources': current, 'historical_sources': historical,
            'acceptance': decision, 'paper_acceptance': review,
            'missing_mask': {'is_missing': value['value_type'] == 'missing', 'reason': value['missing_reason'],
                             'numeric_missing': value['numeric_value'] is None},
            'exclusion_reasons': sorted(set(reasons)), 'formal_eligible': not reasons,
            'conflict_status': conflict_status, 'leakage_status': decision.get('leakage', 'unresolved')})
    _groups(observations)
    for x in observations:
        bucket = int(digest([seed, x['leakage_group']]), 16) % 100
        x['preview_split'] = 'train' if bucket < 80 else 'validation' if bucket < 90 else 'test'
    formal = [x for x in observations if x['formal_eligible']]
    payload = {'observations.jsonl': b''.join(encoded(x) for x in observations),
               'formal_numeric.jsonl': b''.join(encoded(x) for x in formal if x['numeric_value'] is not None),
               'formal_text.jsonl': b''.join(encoded(x) for x in formal if x['numeric_value'] is None),
               'exploratory.jsonl': b''.join(encoded(x) for x in observations if not x['formal_eligible']),
               'acceptance.json': encoded(manifest), 'snapshot.json': encoded(snapshot)}
    dictionary = {kind: {c.name: {'sql_type': str(c.type), 'nullable': c.nullable}
                        for c in model.__table__.columns} for kind, model in MODELS.items()}
    dictionary['export'] = {'value_id': 'Stable rebuild_data_values UUID; one observation per stored cell',
        'identity': list(IDENTITY), 'missing_mask': 'Null is never zero; reason and stored value_type retained',
        'source_kinds': sorted(SOURCE_KINDS),
        'formal_table_locator': 'table asset in same paper/file, physical page in positive asset pages, '
            'strict nonnegative integer row/column pair within structured table_data, nonempty quote; '
            'stored coordinates never rebased',
        'sources': 'All current sources with complete file/asset records and page identity',
        'historical_sources': 'Inactive evidence retained, never used for eligibility',
        'duplicate_group': 'Within-paper exact scientific identity and field; no scientific merge',
        'leakage_group': 'Transitive paper/DOI/file SHA/scientific identity component',
        'preview_split': 'Seeded 80/10/10 group hash assignment; always preview/not final in v1',
        'formal_eligible': 'Every acceptance and machine provenance check passes',
        'raw_value': 'Unmodified original text, never reconstructed from binary64 numeric_value',
        'exclusion_reasons': 'All failed checks; exploratory values never enter formal artifacts'}
    payload['schema.json'] = encoded({'schema_version': VERSION, 'dictionary': dictionary, 'policy': policy})
    stats = {'observations': len(observations), 'formal_values': len(formal),
             'exploratory_values': len(observations) - len(formal),
             'formal_rows': len({x['row_id'] for x in formal}),
             'exclusions': dict(sorted(Counter(r for x in observations for r in x['exclusion_reasons']).items())),
             'papers_without_disposition': sorted(set(snapshot['scope_paper_ids']) - set(manifest.get('papers', {})))}
    payload['build_manifest.json'] = encoded({'schema_version': VERSION, 'code_revision': code_revision,
        'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'snapshot_sha256': snapshot['snapshot_sha256'], 'query_bundle_sha256': snapshot['query_bundle_sha256'],
        'acceptance_sha256': digest(manifest),
        'seed': seed, 'split_status': 'preview/not final', 'formal_scope': 'explicit eligible cells only',
        'production_writes': 0, 'statistics': stats,
        'reproduce': 'PYTHONPATH=backend python backend/scripts/export_rebuild_ml.py build '
            '--snapshot SNAPSHOT --acceptance ACCEPTANCE --output NEW_DIRECTORY --seed ' + str(seed) +
            ' --code-revision ' + code_revision,
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}})
    payload['SHA256SUMS'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                 for name, data in sorted(payload.items())).encode()
    return payload


def write_dataset(payload, output):
    """Require a new directory; never overwrite an artifact or backup."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name, data in sorted(payload.items()):
        with (output / name).open('xb') as handle: handle.write(data)
