"""Synthetic stored-asset quality checks: no DB, PDF rendering or science writes."""
from copy import deepcopy
import asyncio
from uuid import UUID
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from PIL import Image

from app.services import rebuild_quality as quality
from app.services.rebuild_table_content import table_content, representation_complete

pytestmark = pytest.mark.no_test_database
NORMAL = dict(units='normalized_page_fraction', x0=0., y0=0., x1=1., y1=1.)


def asset(**changes):
    data = dict(id=uuid4(), asset_type='figure', file_id=uuid4(), figure_label=None,
                logical_group_key=None, page_numbers=[1], bbox=deepcopy(NORMAL),
                image_path=None, version=7, provenance={}, context_text='exact raw 1.00s')
    data.update(changes)
    return NS(**data)


def report(records, root, *, offset=0, limit=100, rows=None, values=None, full=False,
           include_asset_mechanics=True, asset_offset=0, asset_limit=None):
    asset_limit = quality.IMAGE_CHECK_LIMIT if asset_limit is None else asset_limit
    paper_id = uuid4()
    files = [NS(id=record.file_id) for record in records]
    states = {str(file.id): {'status': 'verified', 'physical_pages': 1,
                            'file_id': str(file.id), 'role': 'main', 'source_sha256': 'a'*64}
              for file in files}
    session = MagicMock()
    session.scalars.side_effect = [rows or [], values or [], [], records, files, []]
    session.execute.return_value = []
    session.scalar.return_value = None
    with patch('app.services.rebuild_workflow_service._required_paper'), patch('app.services.rebuild_workflow_service._stored_identity'):
        if full:
            snapshot = dict(revision='synthetic', file_reading_identity=states,
                            table_completeness=[dict(asset_id=str(records[0].id), complete=False,
                                                    reason='pending_structure', format='text')])
            result = quality.quality_report(session, paper_id, NS(storage_root=root), snapshot,
                include_asset_mechanics=include_asset_mechanics, asset_offset=asset_offset, asset_limit=asset_limit,
                coverage_plan_report=dict(legacy_without_plan=False, summary={'files': []}, items=[],
                                          revision='test', plan_revision='test', checked_output_revision='test', next_page=None))
        else:
            result = quality.integrity_report(session, paper_id, NS(storage_root=root), file_states=states,
                                              offset=offset, limit=limit, include_asset_mechanics=include_asset_mechanics,
                                              asset_offset=asset_offset, asset_limit=asset_limit)
    for method in ('add', 'add_all', 'delete', 'flush', 'commit', 'merge'):
        getattr(session, method).assert_not_called()
    return result


def codes(result, *, mechanical=True):
    return {item['code'] for item in (result['asset_mechanics'] if mechanical else result)['issues']}


def png(root, name='valid.png', size=(12, 9)):
    path = root / name
    Image.new('RGB', size, 'white').save(path)
    return path


@pytest.mark.parametrize('changes,code', [
    ({'x0': -.01}, 'asset_bbox_out_of_bounds'),
    ({'y1': 1.01}, 'asset_bbox_out_of_bounds'),
    ({'x0': .9, 'x1': .1}, 'asset_bbox_reversed_or_empty'),
    ({'y0': .8, 'y1': .2}, 'asset_bbox_reversed_or_empty'),
    ({'x0': 1.}, 'asset_bbox_reversed_or_empty'),
    ({'x0': float('nan')}, 'asset_bbox_nonfinite'),
    ({'x1': float('inf')}, 'asset_bbox_nonfinite'),
    ({'y0': float('-inf')}, 'asset_bbox_nonfinite'),
    ({'x1': 10**400}, 'asset_bbox_out_of_bounds'),
])
def test_explicit_invalid_bbox(changes, code, tmp_path):
    result = report([asset(bbox={**NORMAL, **changes})], tmp_path)
    assert code in codes(result)
    assert next(i for i in result['asset_mechanics']['issues'] if i['code'] == code)['severity'] == 'error'


@pytest.mark.parametrize('bbox', [None, {}, {'x0': 0, 'y0': 0, 'x1': 1, 'y1': 1},
    {**NORMAL, 'units': 'pixels'}, {**NORMAL, 'units': 'points'},
    {**NORMAL, 'x0': '0'}, {**NORMAL, 'x0': False}, {'units': 'normalized_page_fraction', 'regions': []}])
def test_legacy_unknown_is_not_converted(bbox, tmp_path):
    record = asset(bbox=bbox)
    before = deepcopy(record.__dict__)
    result = report([record], tmp_path)
    assert 'asset_bbox_unknown' in codes(result)
    assert record.__dict__ == before


def test_normalized_regions_and_limits():
    bbox = dict(units='normalized_page_fraction', regions=[dict(page_number=1, bbox=[0, 0, 1, 1]),
                                                         dict(page_number=2, bbox=[.1, .2, .8, .9])])
    assert quality._bbox_check(bbox) == (None, None)
    bbox['regions'][1]['bbox'][0] = -.1
    assert quality._bbox_check(bbox) == ('error', 'asset_bbox_out_of_bounds')
    bbox['regions'] = [dict(bbox=[0, 0, 1, 1])] * (quality.BBOX_REGION_LIMIT + 1)
    assert quality._bbox_check(bbox) == ('unknown', 'asset_bbox_budget_exceeded')


def test_valid_image_and_legal_known_bad_crop_geometry_do_not_assert_science(tmp_path):
    png(tmp_path)
    for bbox in (NORMAL, dict(units='normalized_page_fraction', x0=.35, y0=.29, x1=.53, y1=.475),
                 dict(units='normalized_page_fraction', x0=.185, y0=.13, x1=.812, y1=.372)):
        result = report([asset(image_path='valid.png', bbox=bbox)], tmp_path)
        assert result['status'] == 'stored_checks_passed'
        assert result['scientific_acceptance'] == 'not_implied'
        assert result['asset_mechanics']['scientific_visual_completeness'] == 'not_checked'


@pytest.mark.parametrize('kind,code', [('missing', 'asset_image_missing'), ('corrupt', 'asset_image_corrupt'),
    ('crossroot', 'asset_image_outside_storage_root'), ('absolute', 'asset_image_outside_storage_root'),
    ('symlink', 'asset_image_outside_storage_root')])
def test_image_issues(kind, code, tmp_path):
    root = tmp_path / 'storage'; root.mkdir()
    outside = png(tmp_path, 'outside.png')
    image_path = 'never-existed.png'
    if kind == 'corrupt':
        (root / 'corrupt.png').write_bytes(b'not an image'); image_path = 'corrupt.png'
    elif kind == 'crossroot': image_path = '../outside.png'
    elif kind == 'absolute': image_path = str(outside)
    elif kind == 'symlink':
        (root / 'linked.png').symlink_to(outside); image_path = 'linked.png'
    result = report([asset(image_path=image_path)], root)
    assert code in codes(result)
    assert result['asset_mechanics']['status'] == 'errors'
    assert result['status'] == 'stored_checks_passed'


def test_draft_without_image_and_raw_only_table_remain_readable(tmp_path):
    assert 'asset_image_unknown' in codes(report([asset()], tmp_path))
    record = asset(asset_type='table', bbox=None)
    before = deepcopy(record.__dict__)
    assert not codes(report([record], tmp_path))
    assert table_content(record)['text'] == 'exact raw 1.00s'
    assert not representation_complete(table_content(record))
    result = report([record], tmp_path, full=True)
    assert 'table_representation_incomplete' in codes(result, mechanical=False)
    assert not any(code.startswith('asset_image') for code in codes(result))
    assert record.__dict__ == before


def test_read_only_science_versions_and_image_bytes(tmp_path):
    path = png(tmp_path)
    record = asset(image_path=path.name)
    row = NS(id=uuid4(), row_key='stable', properties={'original': '1.00s'})
    value = NS(id=uuid4(), row_id=row.id, paper_id=uuid4(), value_type='reported', raw_value='1.00s',
               numeric_value=1., missing_reason=None, preferred_source_id=None)
    before = deepcopy((record.__dict__, row.__dict__, value.__dict__))
    image_before = path.read_bytes()
    result = report([record], tmp_path, rows=[row], values=[value])
    assert result['read_only']
    assert (record.__dict__, row.__dict__, value.__dict__) == before
    assert path.read_bytes() == image_before
    assert 'value_source_missing' in codes(result, mechanical=False)  # existing quality rule preserved


def test_decode_cache_reuse_and_file_change_invalidation(tmp_path, monkeypatch):
    path = png(tmp_path)
    quality._image_at_stat.cache_clear()
    original = quality.Image.open
    calls = []
    def observed(*args, **kwargs):
        calls.append(args[0]); return original(*args, **kwargs)
    monkeypatch.setattr(quality.Image, 'open', observed)
    record = asset(image_path=path.name)
    for _ in range(3): assert not codes(report([record], tmp_path))
    assert len(calls) == 2  # verify + full load once, not three decodes
    path.write_bytes(b'changed corrupt bytes')
    assert 'asset_image_corrupt' in codes(report([record], tmp_path))
    assert len(calls) == 3


def test_asset_slice_independent_of_issue_pagination(tmp_path):
    png(tmp_path)
    records = [asset(image_path='valid.png') for _ in range(5)]
    result = report(records, tmp_path, offset=1, limit=1, asset_offset=2, asset_limit=2)
    mechanical = result['asset_mechanics']
    assert mechanical['scope']['image_attempts'] == 2
    assert mechanical['scope']['selected_asset_ids'] == [str(a.id) for a in records[2:4]]
    assert mechanical['scope']['not_checked_outside_slice_count'] == 3
    assert mechanical['scope']['next_asset_offset'] == 4
    assert mechanical['status'] == 'slice_checks_passed'
    assert result['issues_total'] == 0 and not result['has_more']


@pytest.mark.parametrize('limit_name,limit_value', [('IMAGE_BYTE_LIMIT', 1), ('IMAGE_TOTAL_BYTES', 1),
    ('IMAGE_PIXEL_LIMIT', 1), ('IMAGE_TOTAL_PIXELS', 1)])
def test_large_images_are_unknown_not_passed(limit_name, limit_value, tmp_path, monkeypatch):
    png(tmp_path)
    quality._image_at_stat.cache_clear()
    monkeypatch.setattr(quality, limit_name, limit_value)
    result = report([asset(image_path='valid.png')], tmp_path)
    assert 'asset_image_budget_exceeded' in codes(result)
    assert result['asset_mechanics']['status'] == 'unknown'
    assert result['status'] == 'stored_checks_passed'


def test_nonpositive_dimensions(tmp_path, monkeypatch):
    png(tmp_path)
    quality._image_at_stat.cache_clear()
    fake = MagicMock(); fake.__enter__.return_value.size = (0, 1)
    fake.__enter__.return_value.format = 'PNG'
    monkeypatch.setattr(quality.Image, 'open', lambda *a, **k: fake)
    assert 'asset_image_dimensions_invalid' in codes(report([asset(image_path='valid.png')], tmp_path))


def test_truncated_png_rejected_even_with_valid_header(tmp_path):
    path = png(tmp_path)
    path.write_bytes(path.read_bytes()[:40])
    quality._image_at_stat.cache_clear()
    assert 'asset_image_corrupt' in codes(report([asset(image_path=path.name)], tmp_path))


def test_document_decoders_are_not_invoked(tmp_path, monkeypatch):
    png(tmp_path)
    quality._image_at_stat.cache_clear()
    fake = MagicMock(); fake.__enter__.return_value.format = 'EPS'
    monkeypatch.setattr(quality.Image, 'open', lambda *a, **k: fake)
    assert 'asset_image_format_unknown' in codes(report([asset(image_path='valid.png')], tmp_path))
    fake.__enter__.return_value.load.assert_not_called()


def test_multiframe_is_unknown_not_verified(tmp_path):
    Image.new('RGB', (2, 2), 'white').save(tmp_path / 'multi.gif', save_all=True,
        append_images=[Image.new('RGB', (2, 2), 'black')])
    assert 'asset_image_multiframe_unknown' in codes(report([asset(image_path='multi.gif')], tmp_path))


def test_optional_checks_are_off_by_default_with_explicit_unchecked_scope(tmp_path, monkeypatch):
    png(tmp_path)
    records = [asset(image_path='valid.png') for _ in range(65)]
    def forbidden(*a, **k): raise AssertionError('legacy calls must not decode images')
    monkeypatch.setattr(quality, '_asset_image_check', forbidden)
    result = report(records, tmp_path, include_asset_mechanics=False)
    assert result['status'] == 'stored_checks_passed' and result['counts'] == {}
    mechanical = result['asset_mechanics']
    assert mechanical['status'] == 'not_requested'
    assert mechanical['scope']['image_attempts'] == 0
    assert mechanical['scope']['selected_asset_ids'] == []
    assert mechanical['scope']['not_checked_outside_slice_count'] == 65
    assert mechanical['formal_acceptance'] == 'not_evaluated'


def test_more_than_32_images_have_disjoint_bounded_continuation(tmp_path):
    png(tmp_path)
    records = [asset(image_path='valid.png', id=UUID(int=n+1)) for n in range(65)]
    selected = []
    for offset, count, next_offset in [(0, 32, 32), (32, 32, 64), (64, 1, None)]:
        result = report(records, tmp_path, asset_offset=offset)
        mechanical = result['asset_mechanics']; scope = mechanical['scope']
        assert mechanical['status'] == 'slice_checks_passed'
        assert scope['image_attempts'] == count and scope['selected_count'] == count
        assert scope['next_asset_offset'] == next_offset
        assert scope['not_checked_outside_slice_count'] == 65-count
        selected += scope['selected_asset_ids']
        assert result['status'] == 'stored_checks_passed' and result['counts'] == {}
    assert selected == [str(a.id) for a in records] and len(set(selected)) == 65


def test_later_slice_keeps_bad_geometry_and_image_errors_visible(tmp_path):
    png(tmp_path)
    records = [asset(image_path='valid.png', id=UUID(int=n+1)) for n in range(33)]
    records[32].bbox['x0'] = -.1; records[32].image_path = 'missing.png'
    first = report(records, tmp_path)
    assert first['asset_mechanics']['status'] == 'slice_checks_passed'
    assert first['asset_mechanics']['scope']['not_checked_after_count'] == 1
    later = report(records, tmp_path, asset_offset=32)
    assert later['status'] == 'stored_checks_passed' and later['counts'] == {}
    assert later['asset_mechanics']['status'] == 'errors'
    assert codes(later) == {'asset_bbox_out_of_bounds', 'asset_image_missing'}
    assert later['asset_mechanics']['scope']['selected_asset_ids'] == [str(records[32].id)]


@pytest.mark.parametrize('offset,limit', [(-1, 32), (0, 0), (0, 33), (True, 32)])
def test_invalid_mechanical_slice_rejected(offset, limit, tmp_path):
    with pytest.raises(ValueError, match='asset_mechanics_range_invalid'):
        quality._asset_mechanics([], NS(storage_root=tmp_path), requested=True, offset=offset, limit=limit)


def acceptance_fixture(records, root):
    from app.services import rebuild_acceptance as acceptance
    file_id = records[0].file_id
    for record in records: record.file_id = file_id
    physical = {file_id: dict(file_id=str(file_id), status='verified', physical_pages=1)}
    snapshot = dict(revision='stable', files=[{'file_id': str(file_id)}], unregistered_files=[],
                    output_manifest=[{'kind': 'synthetic'}],
                    file_reading_identity={str(file_id): physical[file_id]})
    coverage = [dict(file_id=str(file_id), covered_pages=[1], total_pages=1, reading_pdf_sha256='fixture')]
    paper_id = uuid4()
    session = MagicMock(); session.execute.return_value = []
    session.scalars.side_effect = [[], [], [], records, [NS(id=file_id)], [], [], [], [], records]
    with patch('app.services.rebuild_workflow_service._required_paper'), \
         patch('app.config.get_settings', return_value=NS(storage_root=root)):
        rules = acceptance._rules(session, paper_id, snapshot, coverage)
    job = NS(job_id='unchanged-accepted', status='accepted', progress={}, result={}, payload=dict(
        paper_id=str(paper_id), writer='fixture', writer_task_id='fixture', snapshot=snapshot, coverage=coverage))
    session = MagicMock(); session.execute.return_value = []; session.scalar.return_value = job
    session.scalars.side_effect = [[], [], [], records, [NS(id=file_id)], []]
    with patch('app.services.rebuild_workflow_service._required_paper'), \
         patch('app.config.get_settings', return_value=NS(storage_root=root)), \
         patch.object(acceptance, '_scope', return_value=([], [])), \
         patch.object(quality, 'file_inventory', return_value=physical):
        status = acceptance.acceptance_status(session, paper_id, snapshot=snapshot)
    return rules, status


def test_formal_original_callers_keep_65_valid_images_accepted(tmp_path, monkeypatch):
    png(tmp_path)
    records = [asset(image_path='valid.png') for _ in range(65)]
    def forbidden(*a, **k): raise AssertionError('formal caller must not run optional diagnostics')
    monkeypatch.setattr(quality, '_asset_image_check', forbidden)
    rules, status = acceptance_fixture(records, tmp_path)
    assert rules['passed'] and rules['issues'] == []
    assert status['status'] == 'accepted' and not status.get('stale', False)


def test_formal_existing_page_integrity_error_still_blocks(tmp_path):
    png(tmp_path)
    records = [asset(image_path='valid.png', page_numbers=[0])]
    rules, status = acceptance_fixture(records, tmp_path)
    assert not rules['passed']
    assert rules['issues'][0]['code'] == 'stored_output_integrity_incomplete'
    assert any(item['code'] == 'physical_page_invalid' for item in rules['issues'][0]['issues'])
    assert status['status'] == 'review_incomplete'


@pytest.mark.parametrize('offset,revision,allowed', [(0, None, True), (32, None, False),
    (32, 'a'*64, True), (32, 'b'*64, False)])
def test_quality_route_explicit_opt_in_and_revision_boundary(offset, revision, allowed, monkeypatch):
    from app.api import rebuild as api
    from app.services import rebuild_acceptance as acceptance
    from fastapi import HTTPException
    session = MagicMock(); output = dict(coverage_plan={'plan_revision': None}, quality_revision='c'*64)
    snapshot = {'revision': 'a'*64}
    monkeypatch.setattr(api, '_required_paper', lambda *a: None)
    monkeypatch.setattr(acceptance, 'current_snapshot', lambda *a: (snapshot, []))
    checked = MagicMock(return_value=output)
    monkeypatch.setattr(quality, 'quality_report', checked)
    args = dict(paper_id=uuid4(), offset=0, limit=100, include_asset_mechanics=True,
                asset_offset=offset, asset_limit=32, expected_coverage_revision=None,
                expected_revision=revision, session=session, _auth=NS())
    if allowed:
        assert asyncio.run(api.rebuild_quality_report(**args)) == output
        assert checked.call_args.kwargs['include_asset_mechanics'] is True
        assert checked.call_args.kwargs['asset_offset'] == offset
    else:
        with pytest.raises(HTTPException): asyncio.run(api.rebuild_quality_report(**args))
        checked.assert_not_called()
    assert 'READ ONLY' in str(session.execute.call_args.args[0])


def test_empty_slice_does_not_claim_a_pass(tmp_path):
    records = [asset()]
    result = report(records, tmp_path, asset_offset=99)
    mechanical = result['asset_mechanics']
    assert mechanical['status'] == 'not_checked'
    assert mechanical['scope']['selected_count'] == 0
    assert mechanical['scope']['not_checked_outside_slice_count'] == 1
    assert mechanical['scope']['image_attempts'] == 0


def test_quality_route_defaults_disable_new_diagnostics_and_bound_asset_limit():
    import inspect
    from app.api.rebuild import rebuild_quality_report
    params = inspect.signature(rebuild_quality_report).parameters
    assert params['include_asset_mechanics'].default.default is False
    assert params['asset_offset'].default.default == 0
    assert params['asset_limit'].default.default == 32
    assert any(getattr(item, 'le', None) == 32 for item in params['asset_limit'].default.metadata)


def test_producer_contract_has_direct_diagnostic_address():
    contract = quality.producer_contract()['asset_mechanics']
    assert contract['default'] == 'not_requested'
    assert 'include_asset_mechanics=true' in contract['endpoint_template']
    assert 'expected_revision' in contract['continuation']
