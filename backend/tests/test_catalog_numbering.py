from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.db.models import AuditLog, Paper, PaperRelationship
from app.services.paper_codes import main_paper_clause, numbering_plan


def paper(*, library='one', type='computational', date=None, code=None, id=None):
    return Paper(id=id or uuid4(), library_name=library, paper_type=type, created_at=date or datetime(2020, 1, 1),
                 title='fixture', pdf_path='preserved.pdf', paper_code=code)


@pytest.mark.no_test_database
def test_chronological_shared_sequence_and_si_owner_without_guessing():
    early = paper(type='mixed', date=datetime(2019, 1, 1), code='B009')
    late = paper(type='computational', code='A009')
    si1 = paper(type='supplementary', date=datetime(2018, 1, 1))
    si2 = paper(type='si', date=datetime(2019, 1, 1))
    orphan = paper(type='supplementary')
    other = paper(library='two', type='mixed')
    rels = [PaperRelationship(source_paper_id=early.id, target_paper_id=child.id, relationship_type='supplementary')
            for child in (si1, si2)]
    result = {entry['paper_id']: entry for entry in numbering_plan([late, si2, early, orphan, si1, other], rels)}
    assert [result[str(row.id)]['new_code'] for row in (early, late, si1, si2, orphan, other)] == [
        'B001', 'A002', 'S001', 'S001-2', None, 'B001']
    assert result[str(si1.id)]['new_serial'] is None
    assert result[str(early.id)]['old_code'] == 'B009'


@pytest.mark.no_test_database
def test_identical_intake_timestamps_use_uuid_stable_order():
    rows = [paper(id=uuid4()) for _ in range(5)]
    codes = {entry['paper_id']: entry['new_code'] for entry in numbering_plan(rows[::-1], [])}
    assert [codes[str(row.id)] for row in sorted(rows, key=lambda row: str(row.id))] == ['A001', 'A002', 'A003', 'A004', 'A005']


def test_mutations_reorder_both_libraries_preserve_ids_and_audit(shared_test_database):
    factory = shared_test_database.session_factory
    with factory() as session:
        first = paper(type='mixed')
        second = paper(date=datetime(2021, 1, 1))
        other = paper(library='two', date=datetime(2019, 1, 1))
        session.add_all([first, second, other])
        session.commit()
        ids = [row.id for row in (first, second, other)]
        assert [row.paper_code for row in (first, second, other)] == ['B001', 'A002', 'A001']
        second.paper_type = 'experimental'
        session.commit()
        assert second.paper_code == 'C002'
        first.library_name = 'two'
        session.commit()
        session.refresh(second)
        assert (first.paper_code, second.paper_code, other.paper_code) == ('B002', 'C001', 'A001')
        assert [row.id for row in (first, second, other)] == ids
        assert all(row.pdf_path == 'preserved.pdf' for row in (first, second, other))
        logs = session.scalars(select(AuditLog).where(AuditLog.action == 'paper_code_reassigned')).all()
        assert any(log.payload.get('old_library_name') == 'one' and log.payload['new_code'] == 'B002' for log in logs)


def test_explicit_si_association_closes_main_gap(shared_test_database):
    with shared_test_database.session_factory() as session:
        rows = [paper(date=datetime(2020, 1, 1) + timedelta(days=i)) for i in range(3)]
        session.add_all(rows)
        session.commit()
        session.add(PaperRelationship(source_paper_id=rows[0].id, target_paper_id=rows[1].id,
                                      relationship_type='supplementary'))
        session.commit()
        for row in rows:
            session.refresh(row)
        assert [row.paper_code for row in rows] == ['A001', 'S001', 'A002']
        assert [row.serial_number for row in rows] == [1, None, 2]
        assert len(session.scalars(select(Paper).where(main_paper_clause())).all()) == 2


def test_parallel_intake_uses_one_contiguous_library_sequence(shared_test_database):
    factory = shared_test_database.session_factory
    barrier = Barrier(4)
    def intake(index):
        with factory() as session:
            row = paper(type='mixed' if index % 2 else 'computational', date=datetime(2020, 1, 1) + timedelta(days=index))
            session.add(row)
            barrier.wait(timeout=10)
            session.commit()
            return row.id
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(intake, range(4)))
    with factory() as session:
        rows = session.scalars(select(Paper).where(Paper.id.in_(ids)).order_by(Paper.created_at)).all()
        assert [row.paper_code for row in rows] == ['A001', 'B002', 'A003', 'B004']
        assert [row.serial_number for row in rows] == [1, 2, 3, 4]


def test_rollback_preserves_catalogue_and_change_log(shared_test_database):
    with shared_test_database.session_factory() as session:
        row = paper()
        session.add(row)
        session.commit()
        old_id = row.id
        old_logs = len(session.scalars(select(AuditLog)).all())
        row.paper_type = 'experimental'
        session.flush()
        assert row.paper_code == 'C001'
        session.rollback()
        assert session.get(Paper, old_id).paper_code == 'A001'
        assert len(session.scalars(select(AuditLog)).all()) == old_logs


def test_move_main_with_si_and_reassign_owner_across_libraries(shared_test_database):
    with shared_test_database.session_factory() as session:
        first = paper(type='mixed')
        second = paper(library='two', date=datetime(2019, 1, 1))
        si = paper(type='supplementary')
        session.add_all([first, second, si])
        session.flush()
        relation = PaperRelationship(source_paper_id=first.id, target_paper_id=si.id, relationship_type='si')
        session.add(relation)
        session.commit()
        first.library_name = 'two'
        session.commit()
        session.refresh(si)
        assert (first.paper_code, si.paper_code, si.library_name) == ('B002', 'S002', 'two')
        third = paper(library='three', date=datetime(2019, 1, 1))
        session.add(third)
        session.commit()
        relation.source_paper_id = third.id
        session.commit()
        session.refresh(si)
        assert (si.paper_code, si.library_name) == ('S001', 'three')
        session.expire(relation)
        relation.relationship_type = 'citation'
        si.paper_type = 'computational'
        session.commit()
        session.refresh(si)
        assert si.serial_number == 2 and si.paper_code == 'A002'


def test_parallel_http_uploads_persist_contiguous_codes_and_idempotent_files(shared_test_database, tmp_path):
    import io
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.db.models import RebuildPaperFile, WorkflowJob
    from app.db.session import get_db_session
    from app.api.papers.ingestion import router
    app = FastAPI()
    app.include_router(router, prefix='/api/papers')
    settings = get_settings().model_copy(update={'storage_root': tmp_path / 'storage'})
    def database():
        with shared_test_database.session_factory() as session:
            yield session
    app.dependency_overrides[get_db_session] = database
    app.dependency_overrides[get_settings] = lambda: settings
    barrier = Barrier(4)
    payloads = [f'%PDF-1.4 concurrent upload {index}\n%%EOF'.encode() for index in range(4)]
    def upload(index):
        with TestClient(app) as client:
            barrier.wait(timeout=10)
            response = client.post('/api/papers/ingest/upload/jobs', data={'library_name': 'parallel-http'},
                files={'file': (f'upload-{index}.pdf', io.BytesIO(payloads[index]), 'application/pdf')})
            assert response.status_code == 200, response.text
            return response.json()['paper_id']
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(upload, range(4)))
    with TestClient(app) as client:
        retry = client.post('/api/papers/ingest/upload/jobs', data={'library_name': 'parallel-http'},
            files={'file': ('retry.pdf', io.BytesIO(payloads[0]), 'application/pdf')})
        assert retry.status_code == 200
        assert retry.json()['paper_id'] == ids[0]
    with shared_test_database.session_factory() as session:
        rows = session.scalars(select(Paper).where(Paper.library_name == 'parallel-http').order_by(Paper.serial_number)).all()
        assert [row.paper_code for row in rows] == ['U001', 'U002', 'U003', 'U004']
        assert len(session.scalars(select(RebuildPaperFile)).all()) == 4
        jobs = session.scalars(select(WorkflowJob)).all()
        assert len(jobs) == 4 and all(job.status == 'completed' for job in jobs)
        assert all(job.progress['automatic_parsing_started'] is False for job in jobs)
