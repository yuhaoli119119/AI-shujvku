"""Real isolated PostgreSQL fixtures; frozen UUIDs; no production configuration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.db.models import (DFTResult, Paper, RebuildPaperFile, RebuildVisualAsset,
                           RebuildDataRow, RebuildDataValue, RebuildValueSource)
from app.services.rebuild_ml_dataset import (ACCEPTANCE_VERSION, MODELS, build_dataset,
    digest, encoded, paper_revision, read_snapshot, write_dataset)


def uid(n): return UUID(int=n)


@pytest.fixture
def frozen(default_test_database_mode):
    db = default_test_database_mode
    with db.session_factory() as session:
        for offset in (0, 100):
            p, f, a, r, v, s = [uid(offset + i) for i in range(1, 7)]
            session.add(Paper(id=p, library_name='fixture', doi='10.123/fixture-' + str(offset), pdf_path='fixture.pdf'))
            session.flush()
            session.add(RebuildPaperFile(id=f, paper_id=p, role='main', storage_path='fixture.pdf',
                original_filename='fixture.pdf', sha256=('a' if offset == 0 else 'b') * 64, file_size=12))
            session.flush()
            session.add(RebuildVisualAsset(id=a, paper_id=p, file_id=f, asset_type='table',
                asset_key='Table1', page_numbers=[1],
                provenance={'table_data': {'columns': ['energy'], 'rows': [['−2.40']]}}))
            session.add(RebuildDataRow(id=r, paper_id=p, row_key='row', reaction='SRR', material='FeN4',
                active_site_type='single_atom', active_site='Fe', configuration='planar', support='C',
                data_type='dft', condition={'functional': 'PBE', 'sample': 'same'}, properties={'adsorbate': 'S'}))
            session.flush()
            session.add(RebuildDataValue(id=v, paper_id=p, row_id=r, field_name='energy', raw_value='−2.40',
                numeric_value=-2.4, unit='eV', precision_digits=2, value_type='explicit',
                preferred_source_id=s, conflict={'source_page_identities': {str(s): {'page_basis': 'source_pdf',
                    'source_sha256': ('a' if offset == 0 else 'b') * 64,
                    'reading_pdf_sha256': ('a' if offset == 0 else 'b') * 64}}}))
            session.flush()
            session.add(RebuildValueSource(id=s, paper_id=p, row_id=r, value_id=v, file_id=f, asset_id=a,
                source_key='table', source_kind='table', page_number=1, table_row=0, table_column=0, quote='−2.40 eV'))
        session.commit()
    with db.engine.connect() as c: snap = read_snapshot(c, [uid(1), uid(101)])
    return db, snap, acceptance(snap)


def acceptance(snap):
    manifest = {'schema_version': ACCEPTANCE_VERSION, 'scope_paper_ids': snap['scope_paper_ids'],
        'policy': {'data_type': 'dft', 'required_identity': ['material', 'active_site', 'condition.functional'],
            'fields': {'energy': {'kind': 'numeric', 'allowed_units': ['eV'], 'role': 'target'}}}}
    for kind in MODELS:
        manifest[kind] = {x['id']: {'status': 'eligible', 'sha256': digest(x)} for x in snap['graph'][kind]}
    for pid, d in manifest['papers'].items():
        d.update(graph_revision=paper_revision(snap, pid), review_receipt='independent-fixture-review',
                 review_evidence_sha256='c'*64)
    for d in manifest['values'].values(): d.update(conflict='clear', leakage='clear')
    return manifest


def build(snap, manifest, **kw):
    return build_dataset(snap, manifest, code_revision='fixture-code', **kw)


def refresh(db):
    with db.engine.connect() as c: snap = read_snapshot(c, [uid(1), uid(101)])
    return snap, acceptance(snap)


def lines(payload, name='observations.jsonl'):
    return [json.loads(x) for x in payload[name].splitlines()]


def test_bytes_semantics_checksums_and_read_only(frozen, tmp_path):
    db, snap, manifest = frozen
    assert snap['transaction'] == {'isolation': 'repeatable read', 'read_only': 'on'}
    before = deepcopy(snap)
    one = build(snap, manifest, seed=42)
    two = build(snap, manifest, seed=42)
    assert one == two and snap == before
    records = lines(one, 'formal_numeric.jsonl')
    assert len(records) == 2
    value = records[0]
    assert value['value_id'] == str(uid(5)) and value['row_id'] == str(uid(4))
    assert value['raw_value'] == '−2.40' and value['numeric_value'] == -2.4
    assert value['unit'] == 'eV' and value['precision_digits'] == 2
    assert value['sources'][0]['id'] == str(uid(6))
    assert value['sources'][0]['file_id'] == str(uid(2))
    assert value['sources'][0]['asset_id'] == str(uid(3))
    assert value['sources'][0]['table_row'] == 0
    assert value['sources'][0]['table_column'] == 0
    assert value['sources'][0]['asset']['provenance']['table_data'] == {
        'columns': ['energy'], 'rows': [['−2.40']]}
    assert value['identity']['condition'] == {'functional': 'PBE', 'sample': 'same'}
    # Scientific identity creates a conservative cross-paper group, not a merge.
    assert records[0]['leakage_group'] == records[1]['leakage_group']
    assert records[0]['duplicate_group'] != records[1]['duplicate_group']
    assert records[0]['preview_split'] == records[1]['preview_split']
    for line in one['SHA256SUMS'].decode().splitlines():
        sha, name = line.split('  '); assert hashlib.sha256(one[name]).hexdigest() == sha
    info = json.loads(one['build_manifest.json'])
    for name, sha in info['files'].items(): assert hashlib.sha256(one[name]).hexdigest() == sha
    assert info['split_status'] == 'preview/not final'
    write_dataset(one, tmp_path / 'new')
    with pytest.raises(FileExistsError): write_dataset(one, tmp_path / 'new')
    with db.engine.connect() as c: assert read_snapshot(c, snap['scope_paper_ids']) == snap


@pytest.mark.parametrize('case,reason', [
    ('source_paper', 'source_identity_mismatch'), ('source_row', 'source_identity_mismatch'),
    ('file_paper', 'source_file_paper_mismatch'), ('asset_file', 'source_asset_file_mismatch'),
    ('page', 'page_outside_asset'), ('identity', 'page_identity_missing_or_mismatched'),
    ('missing', 'missing'), ('numeric', 'invalid_value:numeric_value_disagrees_with_raw'),
    ('precision', 'precision_unknown'), ('conflict', 'conflict_unresolved'),
    ('derived', 'derived_input_closure_not_supported_v1'), ('estimated', 'precision_unknown'),
    ('hash', 'source_pdf_hash_mismatch'), ('leak_field', 'leakage_field_blocked'),
    ('table', 'table_coordinates_missing'), ('preferred', 'preferred_source_not_current'),
    ('inactive', 'current_sources_missing'), ('unit', 'unit_not_allowed'),
])
def test_machine_negative_checks_even_with_eligible_manifest(frozen, case, reason):
    db, _, _ = frozen
    with db.session_factory() as s:
        source = s.get(RebuildValueSource, uid(6)); value = s.get(RebuildDataValue, uid(5))
        if case == 'source_paper': source.paper_id = uid(101)
        if case == 'source_row': source.row_id = uid(104)
        if case == 'file_paper': s.get(RebuildPaperFile, uid(2)).paper_id = uid(101)
        if case == 'asset_file': s.get(RebuildVisualAsset, uid(3)).file_id = uid(102)
        if case == 'page': source.page_number = 2
        if case == 'identity': value.conflict = {}
        if case == 'missing':
            value.value_type = 'missing'; value.raw_value = None; value.numeric_value = None; value.missing_reason = 'unreadable'
        if case == 'numeric': value.numeric_value = 0
        if case == 'precision': value.precision_digits = None
        if case == 'conflict': value.conflict = {**value.conflict, 'status': 'value_conflict', 'candidates': [{'raw_value': '−2.50'}]}
        if case == 'derived': value.value_type = 'derived'; source.estimate_basis = 'E = A - B'
        if case == 'estimated':
            value.value_type = 'estimated'; value.is_estimated = True; value.precision_digits = None
            source.estimate_basis = 'Figure 1 axis'; source.source_kind = 'estimated'
        if case == 'hash':
            value.conflict = {'source_page_identities': {str(uid(6)): {'page_basis': 'source_pdf',
                'source_sha256': 'a'*64, 'reading_pdf_sha256': 'b'*64}}}
        if case == 'leak_field': value.field_name = 'predicted_energy'
        if case == 'table': source.table_column = None
        if case == 'preferred': value.preferred_source_id = uid(106)
        if case == 'inactive': value.conflict = {**value.conflict, 'source_lifecycle': [{'source_id': str(uid(6)), 'action': 'deactivate'}]}
        if case == 'unit': value.unit = 'eV/atom'
        s.commit()
    snap, manifest = refresh(db)
    if case == 'leak_field':
        manifest['policy']['fields']['predicted_energy'] = manifest['policy']['fields']['energy']
    payload = build(snap, manifest)
    record = next(x for x in lines(payload) if x['id'] == str(uid(5)))
    assert reason in record['exclusion_reasons']
    assert not record['formal_eligible']
    assert len(lines(payload, 'formal_numeric.jsonl')) == 1
    assert len(lines(payload, 'exploratory.jsonl')) == 1


@pytest.mark.parametrize('kind', ['papers', 'rows', 'values', 'sources', 'files', 'assets'])
@pytest.mark.parametrize('state', ['missing', 'partial', 'rejected', 'unresolved', 'stale'])
def test_acceptance_fail_closed(frozen, kind, state):
    _, snap, manifest = frozen
    key = next(iter(manifest[kind]))
    if state == 'missing': del manifest[kind][key]
    elif state == 'stale': manifest[kind][key]['sha256'] = '0'*64
    else: manifest[kind][key]['status'] = state
    records = lines(build(snap, manifest), 'formal_numeric.jsonl')
    # Partial paper may contain accepted local objects; other partial objects cannot.
    assert len(records) == (2 if kind == 'papers' and state == 'partial' else 1)


def test_stale_graph_and_leakage_policy(frozen):
    _, snap, manifest = frozen
    manifest['papers'][str(uid(1))]['graph_revision'] = '0'*64
    manifest['values'][str(uid(105))]['leakage'] = 'unresolved'
    assert not lines(build(snap, manifest), 'formal_numeric.jsonl')
    manifest = acceptance(snap)
    manifest['policy']['fields'] = {'another': {'kind': 'numeric', 'role': 'target', 'allowed_units': ['eV']}}
    assert not lines(build(snap, manifest), 'formal_numeric.jsonl')


def test_text_and_missing_masks(frozen):
    db, _, _ = frozen
    with db.session_factory() as s:
        v = s.get(RebuildDataValue, uid(5)); v.raw_value = 'stable'; v.numeric_value = None
        v.precision_digits = None; v.unit = None; s.commit()
    snap, manifest = refresh(db)
    manifest['policy']['fields']['energy'] = {'kind': 'text', 'role': 'metadata', 'allowed_units': [None]}
    out = build(snap, manifest)
    assert len(lines(out, 'formal_text.jsonl')) == 1
    assert lines(out, 'formal_text.jsonl')[0]['missing_mask'] == {
        'is_missing': False, 'reason': None, 'numeric_missing': True}


def test_no_legacy_fallback_and_query_lineage(frozen):
    db, _, _ = frozen
    from sqlalchemy import event
    queries = []
    def log(conn, cursor, statement, parameters, context, executemany): queries.append(statement)
    event.listen(db.engine, 'before_cursor_execute', log)
    try:
        with db.engine.connect() as c: snap = read_snapshot(c, [uid(1)])
    finally: event.remove(db.engine, 'before_cursor_execute', log)
    assert all(q.lstrip().split()[0] in {'SELECT', 'SET'} for q in queries)
    for table in ('rebuild_data_rows', 'rebuild_data_values', 'rebuild_value_sources'):
        assert any(table in q for q in queries)
    assert not any('dft_results' in q.lower() for q in queries)
    assert not lines(build(snap, {'schema_version': ACCEPTANCE_VERSION,
        'scope_paper_ids': snap['scope_paper_ids'], 'policy': acceptance(snap)['policy']}), 'formal_numeric.jsonl')
    # Only legacy rows exist for a scoped empty paper: formal and observations empty.
    with db.session_factory() as s:
        s.add(Paper(id=uid(201), library_name='fixture', pdf_path='empty.pdf')); s.flush()
        s.add(DFTResult(id=uid(202), paper_id=uid(201), property_type='energy', value=-9.99,
                        unit='eV', candidate_status='ML_Ready')); s.commit()
    with db.engine.connect() as c: empty = read_snapshot(c, [uid(201)])
    assert lines(build(empty, acceptance(empty))) == []


def test_snapshot_boundary_and_invalid_manifest(frozen):
    db, snap, manifest = frozen
    with db.engine.connect() as c:
        c.execute(text('SELECT 1'))
        with pytest.raises(ValueError, match='fresh'): read_snapshot(c, [uid(1)])
    with db.engine.connect() as c:
        with pytest.raises(ValueError, match='missing papers'): read_snapshot(c, [uid(999)])
    manifest['sources'][str(uid(6))]['status'] = 'parsed'
    with pytest.raises(ValueError): build(snap, manifest)


def test_write_fixture_example(frozen):
    """Optional task-local deliverables, never enabled by routine test runs."""
    import os
    root = os.environ.get('LITAI_ML_FIXTURE_ARTIFACT_DIR')
    if not root: return
    _, snap, manifest = frozen
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    with (root / 'fixture-snapshot.json').open('xb') as handle: handle.write(encoded(snap))
    with (root / 'fixture-acceptance.json').open('xb') as handle: handle.write(encoded(manifest))
    out = build(snap, manifest, seed=42)
    write_dataset(out, root / 'fixture-build-a')
    write_dataset(build(snap, manifest, seed=42), root / 'fixture-build-b')


def test_duplicate_group_keeps_species_and_reaction_steps_separate(frozen):
    db, _, _ = frozen
    with db.session_factory() as s:
        row = s.get(RebuildDataRow, uid(4))
        copied = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        copied.update(id=uid(14), row_key='different-species', properties={'adsorbate': 'Li2S', 'reaction_step': 'terminal'})
        s.add(RebuildDataRow(**copied)); s.flush()
        value = s.get(RebuildDataValue, uid(5))
        copied = {c.name: getattr(value, c.name) for c in value.__table__.columns}
        copied.update(id=uid(15), row_id=uid(14), preferred_source_id=uid(16),
            conflict={'source_page_identities': {str(uid(16)): value.conflict['source_page_identities'][str(uid(6))]}})
        s.add(RebuildDataValue(**copied)); s.flush()
        source = s.get(RebuildValueSource, uid(6))
        copied = {c.name: getattr(source, c.name) for c in source.__table__.columns}
        copied.update(id=uid(16), row_id=uid(14), value_id=uid(15))
        s.add(RebuildValueSource(**copied)); s.commit()
    snap, manifest = refresh(db)
    records = {x['value_id']: x for x in lines(build(snap, manifest), 'formal_numeric.jsonl')}
    first, second = records[str(uid(5))], records[str(uid(15))]
    assert first['duplicate_group'] != second['duplicate_group']
    assert first['leakage_group'] == second['leakage_group']
    assert len(records) == 3


def test_offline_snapshot_scope_cannot_hide_extra_papers(frozen):
    _, snap, manifest = frozen
    snap['scope_paper_ids'] = [str(uid(1))]
    manifest['scope_paper_ids'] = snap['scope_paper_ids']
    with pytest.raises(ValueError, match='explicit scope'): build(snap, manifest)


def test_actual_readonly_transaction_rejects_insert(frozen):
    db, _, _ = frozen
    from sqlalchemy.exc import DBAPIError
    with db.engine.connect() as c:
        c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        with pytest.raises(DBAPIError, match='read-only'):
            c.execute(text("INSERT INTO papers (id, pdf_path, library_name) VALUES (:id, 'forbidden', 'fixture')"),
                      {'id': uid(999)})
        c.rollback()


@pytest.mark.parametrize('link', ['doi', 'file_sha', 'none'])
def test_leakage_groups_by_doi_or_file_without_identity_match(frozen, link):
    db, _, _ = frozen
    with db.session_factory() as s:
        s.get(RebuildDataRow, uid(104)).material = 'different-material'
        if link == 'doi': s.get(Paper, uid(101)).doi = 'https://doi.org/10.123/FIXTURE-0'
        if link == 'file_sha':
            s.get(RebuildPaperFile, uid(102)).sha256 = 'a'*64
            v = s.get(RebuildDataValue, uid(105))
            v.conflict = {'source_page_identities': {str(uid(106)): {'page_basis': 'source_pdf',
                'source_sha256': 'a'*64, 'reading_pdf_sha256': 'a'*64}}}
        s.commit()
    snap, manifest = refresh(db)
    a, b = lines(build(snap, manifest), 'formal_numeric.jsonl')
    assert (a['leakage_group'] == b['leakage_group']) == (link != 'none')


@pytest.mark.parametrize('case,reason', [
    ('no_locator', 'table_asset_missing'),
    ('no_asset', 'table_asset_missing'),
    ('no_quote', 'table_quote_missing'), ('blank_quote', 'table_quote_missing'),
    ('figure_asset', 'table_asset_type_mismatch'),
    ('unknown_kind', 'source_kind_not_supported'),
    ('subfigure_kind', 'source_kind_not_supported'),
    ('text_with_coords', 'table_coordinates_require_table_kind'),
    ('explicit_with_coords', 'table_coordinates_require_table_kind'),
    ('figure_with_coords', 'table_coordinates_require_table_kind'),
    ('negative_row', 'table_coordinates_invalid'), ('negative_column', 'table_coordinates_invalid'),
    ('null_row', 'table_coordinates_missing'), ('null_column', 'table_coordinates_missing'),
    ('null_pair', 'table_coordinates_missing'),
    ('row_out_of_range', 'table_coordinates_out_of_range'),
    ('column_out_of_range', 'table_coordinates_out_of_range'),
    ('no_provenance', 'table_data_missing_or_invalid'),
    ('no_table_data', 'table_data_missing_or_invalid'),
    ('ragged_table', 'table_data_missing_or_invalid'),
    ('empty_table', 'table_coordinates_out_of_range'),
    ('no_pages', 'table_asset_pages_missing_or_invalid'),
    ('null_pages', 'table_asset_pages_missing_or_invalid'),
    ('bad_pages', 'table_asset_pages_missing_or_invalid'),
    ('page_outside', 'page_outside_asset'),
    ('no_page', 'page_identity_missing_or_mismatched'),
    ('wrong_paper', 'source_asset_file_mismatch'),
    ('wrong_file', 'source_asset_file_mismatch'),
])
def test_table_locator_admission_with_fresh_eligible_db_graph(frozen, case, reason):
    """Persist malformed sources, re-read PG, refresh ALL review hashes, then build.

    The same value first passes formal. Failure after mutation must come from
    machine checks, never stale dispositions or an already ineligible value.
    """
    db, snap, manifest = frozen
    assert {x['id'] for x in lines(build(snap, manifest), 'formal_numeric.jsonl')} == {str(uid(5)), str(uid(105))}
    with db.session_factory() as s:
        source = s.get(RebuildValueSource, uid(6))
        asset = s.get(RebuildVisualAsset, uid(3))
        if case in {'no_locator', 'no_asset'}: source.asset_id = None
        if case == 'no_locator': source.quote = source.label = None
        if case == 'no_quote': source.quote = None
        if case == 'blank_quote': source.quote = '  '
        if case == 'figure_asset': asset.asset_type = 'figure'
        if case == 'unknown_kind': source.source_kind = 'unsupported-kind'
        if case == 'subfigure_kind': source.source_kind = 'subfigure'
        if case == 'text_with_coords': source.source_kind = 'text'
        if case == 'explicit_with_coords': source.source_kind = 'explicit'
        if case == 'figure_with_coords': source.source_kind = 'figure'; asset.asset_type = 'figure'
        if case == 'negative_row': source.table_row = -1
        if case == 'negative_column': source.table_column = -1
        if case in {'null_row', 'null_pair'}: source.table_row = None
        if case in {'null_column', 'null_pair'}: source.table_column = None
        if case == 'row_out_of_range': source.table_row = 1
        if case == 'column_out_of_range': source.table_column = 1
        if case == 'no_provenance': asset.provenance = None
        if case == 'no_table_data': asset.provenance = {'table_markdown': '|energy|'}
        if case == 'ragged_table': asset.provenance = {'table_data': {'columns': ['energy'], 'rows': [['a', 'b']]}}
        if case == 'empty_table': asset.provenance = {'table_data': {'columns': ['energy'], 'rows': []}}
        if case == 'no_pages': asset.page_numbers = []
        if case == 'null_pages': asset.page_numbers = None
        if case == 'bad_pages': asset.page_numbers = [True, '1']
        if case == 'page_outside': asset.page_numbers = [2]
        if case == 'no_page': source.page_number = None
        if case == 'wrong_paper':
            asset.paper_id = uid(101); asset.asset_key = 'other-paper-table'
        if case == 'wrong_file': asset.file_id = uid(102)
        s.commit()
    snap, manifest = refresh(db)
    payload = build(snap, manifest)
    formal = lines(payload, 'formal_numeric.jsonl')
    excluded, = lines(payload, 'exploratory.jsonl')
    assert [x['id'] for x in formal] == [str(uid(105))]
    assert not lines(payload, 'formal_text.jsonl')
    assert excluded['id'] == str(uid(5)) and not excluded['formal_eligible']
    assert reason in excluded['exclusion_reasons']
    assert not any('stale' in x for x in excluded['exclusion_reasons'])
    assert excluded['raw_value'] == '−2.40' and excluded['numeric_value'] == -2.4
    assert excluded['sources'][0]['table_row'] == snap['graph']['sources'][0]['table_row']
    assert excluded['sources'][0]['asset_id'] == snap['graph']['sources'][0]['asset_id']


@pytest.mark.parametrize('field', ['table_row', 'table_column'])
@pytest.mark.parametrize('bad', [True, False, 0.0, '0'])
def test_offline_table_coordinate_types_cannot_bypass_admission(frozen, field, bad):
    # PostgreSQL INTEGER normalizes/rejects these types; exercise the offline
    # snapshot path after a real PG read, with fresh eligible review hashes.
    _, snap, manifest = frozen
    assert len(lines(build(snap, manifest), 'formal_numeric.jsonl')) == 2
    snap['graph']['sources'][0][field] = bad
    snap['snapshot_sha256'] = digest(snap['graph'])
    payload = build(snap, acceptance(snap))
    assert [x['id'] for x in lines(payload, 'formal_numeric.jsonl')] == [str(uid(105))]
    excluded, = lines(payload, 'exploratory.jsonl')
    assert 'table_coordinates_invalid' in excluded['exclusion_reasons']


@pytest.mark.parametrize('kind', ['text', 'explicit', 'figure', 'estimated', 'derived'])
def test_applicable_source_kinds_keep_legal_semantics(frozen, kind):
    db, _, _ = frozen
    with db.session_factory() as s:
        source = s.get(RebuildValueSource, uid(6))
        source.source_kind = kind
        source.table_row = source.table_column = None
        if kind in {'figure', 'estimated'}:
            s.get(RebuildVisualAsset, uid(3)).asset_type = 'figure'
        if kind in {'text', 'explicit', 'derived'}: source.asset_id = None
        if kind in {'estimated', 'derived'}:
            source.estimate_basis = 'reviewed evidence basis'
            value = s.get(RebuildDataValue, uid(5)); value.value_type = kind
            value.is_estimated = kind == 'estimated'
        s.commit()
    snap, manifest = refresh(db)
    payload = build(snap, manifest)
    if kind == 'derived':
        excluded, = lines(payload, 'exploratory.jsonl')
        assert 'derived_input_closure_not_supported_v1' in excluded['exclusion_reasons']
    else:
        assert len(lines(payload, 'formal_numeric.jsonl')) == 2
        assert not lines(payload, 'exploratory.jsonl')


@pytest.mark.parametrize('kind', ['figure', 'estimated', 'table', 'unsupported-kind'])
@pytest.mark.parametrize('asset_type', ['figure', 'subfigure', 'table'])
def test_persisted_source_asset_compatibility_matrix(frozen, kind, asset_type):
    """Fresh PG graph and eligible receipts isolate source/asset compatibility."""
    db, _, _ = frozen
    with db.session_factory() as s:
        source = s.get(RebuildValueSource, uid(6))
        asset = s.get(RebuildVisualAsset, uid(3))
        source.source_kind = kind
        asset.asset_type = asset_type
        if kind != 'table':
            source.table_row = source.table_column = None
        if asset_type != 'table':
            asset.asset_key = 'Fig1a' if asset_type == 'subfigure' else 'Fig1'
            asset.figure_label = 'Fig. 1'
            asset.subfigure_label = 'a' if asset_type == 'subfigure' else None
            asset.provenance = None
            source.label = 'Fig. 1(a)' if asset_type == 'subfigure' else 'Fig. 1'
        if kind == 'estimated':
            source.estimate_basis = 'Fig. 1(a) axis, independently reviewed estimate'
            value = s.get(RebuildDataValue, uid(5))
            value.value_type = 'estimated'
            value.is_estimated = True
        s.commit()
    snap, manifest = refresh(db)
    source = next(x for x in snap['graph']['sources'] if x['id'] == str(uid(6)))
    asset = next(x for x in snap['graph']['assets'] if x['id'] == str(uid(3)))
    assert source['source_kind'] == kind and asset['asset_type'] == asset_type
    if kind != 'unsupported-kind':
        from app.schemas.rebuild import RebuildValueSourceInput, RebuildVisualAssetRequest
        RebuildValueSourceInput.model_validate({k: v for k, v in source.items()
                                               if k in RebuildValueSourceInput.model_fields})
        RebuildVisualAssetRequest.model_validate({k: asset[k] for k in
            ('asset_key', 'asset_type', 'file_id', 'page_numbers', 'figure_label', 'subfigure_label')})
    payload = build(snap, manifest)
    formal = {x['id']: x for x in lines(payload, 'formal_numeric.jsonl')}
    exploratory = lines(payload, 'exploratory.jsonl')
    allowed = (kind == 'table' and asset_type == 'table') or (
        kind in {'figure', 'estimated'} and asset_type in {'figure', 'subfigure'})
    assert str(uid(105)) in formal  # Unchanged control stays eligible.
    if allowed:
        assert set(formal) == {str(uid(5)), str(uid(105))}
        assert not exploratory
        assert not formal[str(uid(5))]['exclusion_reasons']
        if kind == 'table':
            assert source['table_row'] == source['table_column'] == 0
            assert asset['provenance']['table_data'] == {'columns': ['energy'], 'rows': [['−2.40']]}
    else:
        assert set(formal) == {str(uid(105))}
        excluded, = exploratory
        reason = ('source_kind_not_supported' if kind == 'unsupported-kind' else
                  'table_asset_type_mismatch' if kind == 'table' else 'figure_asset_type_mismatch')
        assert reason in excluded['exclusion_reasons']
        assert excluded['id'] == str(uid(5)) and not excluded['formal_eligible']
        assert not any('stale' in x for x in excluded['exclusion_reasons'])
