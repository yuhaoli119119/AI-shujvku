"""No database writes: verify detached projection reuse and invalidation."""
from unittest.mock import MagicMock

import pytest

from app.services import analysis_readonly as analysis

pytestmark = pytest.mark.no_test_database


@pytest.fixture
def cache(monkeypatch):
    analysis._PROJECTIONS.clear()
    revision = ['original']
    builds = []
    monkeypatch.setattr(analysis, '_content_revision', lambda session: tuple(revision))

    def read(service):
        builds.append(True)
        service.records = [{'raw_value': revision[0], 'sources': [{'quote': revision[0]}]}]
        service._loaded = ([], {}, {'total_dft_rows': 1})
        return service._loaded

    monkeypatch.setattr(analysis.AnalysisReadonlyService, '_read_rows', read)
    yield revision, builds
    analysis._PROJECTIONS.clear()


def service(**kwargs):
    session = MagicMock()
    session.get_bind().dialect.name = 'postgresql'
    session.get_bind().url = 'postgresql://isolated-test'
    return analysis.AnalysisReadonlyService(session, library_name='库', **kwargs)


def test_reuse_is_independent_and_same_count_corrections_invalidate(cache):
    revision, builds = cache
    first = service(); first._load_rows()
    first.records[0]['sources'][0]['quote'] = 'response annotation'
    second = service(); second._load_rows()
    assert second.records[0]['sources'][0]['quote'] == 'original'
    assert len(builds) == 1
    revision[0] = 'corrected evidence and identity, same record count'
    third = service(); third._load_rows()
    assert third.records[0]['raw_value'] == revision[0]
    assert third._loaded[2]['total_dft_rows'] == 1
    assert len(builds) == 2


def test_filter_scopes_never_share_and_memory_is_bounded(cache):
    _, builds = cache
    for filters in ({'data_type': 'dft'}, {'data_type': 'experimental'}, {'reaction': 'SRR'}):
        service(filters=filters)._load_rows()
    assert len(builds) == 3
    assert len(analysis._PROJECTIONS) == 2


def test_concurrently_changed_readback_is_not_cached(cache, monkeypatch):
    revisions = iter([('before',), ('after',)])
    monkeypatch.setattr(analysis, '_content_revision', lambda session: next(revisions))
    service()._load_rows()
    assert not analysis._PROJECTIONS
