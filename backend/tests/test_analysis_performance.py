"""Version invalidation and concurrent query isolation; only isolated test DBs."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services import analysis_readonly as analysis

pytestmark = pytest.mark.no_test_database


def test_native_revision_detects_same_count_and_source_corrections():
    url = os.environ.get('LITAI_TEST_ROOT_DATABASE_URL')
    if not url:
        pytest.skip('requires isolated PostgreSQL')
    assert make_url(url).database == 'literature_ai_test'
    engine = create_engine(url)
    with Session(engine) as session:
        session.execute(text('CREATE TEMP TABLE papers (id int, library_name text, paper_code text, title text, doi text, journal text, year int)'))
        session.execute(text("INSERT INTO papers VALUES (1,'fixture','T1','Original',NULL,NULL,2026)"))
        for table in ('rebuild_data_rows', 'rebuild_data_values', 'rebuild_value_sources'):
            session.execute(text(f'CREATE TEMP TABLE {table} (id int, payload text)'))
            session.execute(text(f"INSERT INTO {table} VALUES (1,'original')"))
        before = analysis._content_revision(session)
        assert analysis._content_revision(session) == before
        for table in ('rebuild_data_rows', 'rebuild_data_values', 'rebuild_value_sources'):
            session.execute(text(f"UPDATE {table} SET payload='corrected source/value/identity'"))
            after = analysis._content_revision(session)
            assert after != before
            before = after
            # A second UPDATE in the same transaction must also invalidate.
            session.execute(text(f"UPDATE {table} SET payload='second correction'"))
            after = analysis._content_revision(session)
            assert after != before
            before = after
        session.execute(text("UPDATE papers SET title='Corrected title'"))
        assert analysis._content_revision(session) != before
        before = analysis._content_revision(session)
        session.execute(text('DELETE FROM rebuild_data_values'))
        session.execute(text("INSERT INTO rebuild_data_values VALUES(1,'replacement')"))
        assert analysis._content_revision(session) != before
        before = analysis._content_revision(session)
        session.execute(text('TRUNCATE rebuild_value_sources'))
        session.execute(text("INSERT INTO rebuild_value_sources VALUES(1,'replacement')"))
        assert analysis._content_revision(session) != before
        session.rollback()
    engine.dispose()


def test_cold_builds_for_other_scopes_do_not_wait():
    first_started, release_first, second_done = Event(), Event(), Event()
    def first():
        with analysis._projection_build('first'):
            first_started.set()
            assert release_first.wait(3)
    def second():
        with analysis._projection_build('second'):
            second_done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert first_started.wait(1)
        b = pool.submit(second)
        try:
            assert second_done.wait(1)
        finally:
            release_first.set()
        a.result(); b.result()
    assert not analysis._PROJECTION_BUILDS


def test_revision_reuses_only_matching_version_and_scope(monkeypatch):
    analysis._REVISION_RESULTS.clear()
    version = ['one']
    reads = []
    monkeypatch.setattr(analysis, '_content_revision', lambda session: tuple(version))
    def load(svc):
        reads.append(svc.library_name)
        svc.records = [{'record_id':'1','raw_value':version[0], 'sources':[{'quote':version[0]}]}]
    monkeypatch.setattr(analysis.AnalysisReadonlyService, '_load_rows', load)
    def svc(library='A'):
        session = MagicMock()
        session.get_bind().dialect.name = 'postgresql'
        session.get_bind().url = 'postgresql://isolated'
        return analysis.AnalysisReadonlyService(session, library_name=library)
    a = svc().data_revision()
    assert svc().data_revision() == a and len(reads) == 1
    svc('B').data_revision()
    assert len(reads) == 2
    version[0] = 'same-count source correction'
    b = svc().data_revision()
    assert b['revision'] != a['revision'] and b['record_count'] == a['record_count']
    assert len(reads) == 3
    analysis._REVISION_RESULTS.clear()


def test_compare_index_reuses_order_but_reads_only_page_sources(monkeypatch):
    analysis._COMPARE_RESULTS.clear()
    version = ['one']
    monkeypatch.setattr(analysis, '_content_revision', lambda session: tuple(version))
    indexed, pages = [], []
    def load(svc, *, index_only=False, value_ids=None):
        rows = [dict(record_id=str(i),paper_id='p',catalyst_name='c',property_type='x',
                     adsorbate=None,value=i,sources=[{'quote':version[0]}]) for i in range(235)]
        if index_only:
            indexed.append(True)
            return rows
        pages.append(value_ids)
        svc.records = [r for r in rows if r['record_id'] in value_ids]
    monkeypatch.setattr(analysis.AnalysisReadonlyService, '_read_rows', load)
    def svc():
        session = MagicMock()
        session.get_bind().dialect.name = 'postgresql'
        session.get_bind().url = 'postgresql://isolated'
        return analysis.AnalysisReadonlyService(session, library_name='A')
    for offset,limit in [(0,25),(95,25),(100,100),(201,100),(235,25),(500,25)]:
        page = svc().compare(offset=offset,limit=limit,sort='value')
        expected = list(range(235))[offset:offset+limit]
        assert [r['value'] for r in page['items']] == expected
        assert page['total'] == 235 and page['has_more'] == (offset+limit<235)
        if expected:
            assert pages[-1] == {str(i) for i in expected}
    assert len(indexed) == 1
    version[0] = 'corrected'
    assert svc().compare(sort='value')['items'][0]['sources'][0]['quote'] == 'corrected'
    assert len(indexed) == 2
    analysis._COMPARE_RESULTS.clear()


def test_scientific_output_cache_never_caches_export_permissions(monkeypatch):
    from types import SimpleNamespace
    from app.api import analysis_readonly as api
    analysis._ANALYSIS_OUTPUTS.clear()
    version = ['one']
    builds = []
    monkeypatch.setattr(analysis, '_content_revision', lambda session: tuple(version))
    monkeypatch.setattr(api, 'export_policy', lambda request: {'effective_exports_enabled':request.allowed})
    def service(args, session):
        svc = analysis.AnalysisReadonlyService(session, library_name='test')
        def summary():
            builds.append(True)
            svc.records = [{'catalyst_name':'A','property_type':'x','adsorbate':'Li2S','paper_id':'p'}]
            return {'total_records':1,'value':version[0]}
        svc.summary = summary
        return svc
    monkeypatch.setattr(api, 'service', service)
    session = MagicMock()
    session.get_bind().dialect.name = 'postgresql'
    session.get_bind().url = 'postgresql://isolated'
    first=api.summary(SimpleNamespace(allowed=True),args={},session=session)
    first['catalyst_groups']['A'] = 999
    second=api.summary(SimpleNamespace(allowed=False),args={},session=session)
    assert len(builds)==1
    assert not second['export_policy']['effective_exports_enabled']
    assert second['catalyst_groups']['A']==1
    version[0]='corrected'
    assert api.summary(SimpleNamespace(allowed=False),args={},session=session)['value']=='corrected'
    assert len(builds)==2
    analysis._ANALYSIS_OUTPUTS.clear()


@pytest.mark.parametrize('encoding,compressed', [('gzip',True),('gzip;q=0',False),('br',False),('gzip;q=1',True)])
def test_compressed_analysis_responses_keep_full_sources(encoding, compressed):
    import gzip,json
    from types import SimpleNamespace
    from app.api.analysis_readonly import scientific_response
    payload={'points':[{'x':1,'y':2,'sources':[{'quote':'真实来源'}]}]}
    response=scientific_response(SimpleNamespace(headers={'accept-encoding':encoding}),payload)
    body=response.body
    assert (response.headers.get('content-encoding')=='gzip')==compressed
    if compressed:body=gzip.decompress(body)
    assert json.loads(body)==payload
    assert response.headers['vary']=='Accept-Encoding'
