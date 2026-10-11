from datetime import datetime, timedelta
import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperRelationship, RebuildPaperFile, PaperSection
from app.services.catalog_attachments import supplementary_attachments
from app.services.paper_codes import main_paper_clause
from app.services.paper_list_readonly import PaperListReadonlyService
from app.schemas.api import PaperListFilterParams
from app.api.papers.listing import list_libraries, get_paper_type_stats, unassociated_supplementary
from app.api.visuals import _paper_count, _overview_summary_counts


def test_main_catalog_counts_pagination_and_actual_file_deduplication(setup_test_db, tmp_path):
    root = tmp_path / 'storage' / 'pdf'
    root.mkdir(parents=True)
    content = b'%PDF-1.4 preserved source bytes'
    (root / 'si.pdf').write_bytes(content)
    (root / 'si-copy.pdf').write_bytes(content)
    (root / 'main.pdf').write_bytes(b'%PDF-1.4 main')
    with Session(setup_test_db) as session:
        main = Paper(library_name='test-catalog', title='main', paper_type='A', pdf_path='pdf/main.pdf', created_at=datetime(2020, 1, 1))
        other = Paper(library_name='test-catalog', title='other', paper_type='B', pdf_path='pdf/missing-main.pdf', created_at=datetime(2020, 1, 2))
        si = Paper(library_name='test-catalog', title='attachment', paper_type='supplementary', pdf_path='pdf/si.pdf')
        orphan = Paper(library_name='test-catalog', title='unassociated', paper_type='si', pdf_path='pdf/missing-si.pdf')
        session.add_all([main, other, si, orphan]); session.flush()
        session.add(PaperRelationship(source_paper_id=main.id, target_paper_id=si.id, relationship_type='supplementary'))
        session.add(RebuildPaperFile(paper_id=main.id, role='si', storage_path='pdf/si-copy.pdf', original_filename='original-si.pdf', sha256=hashlib.sha256(content).hexdigest(), file_size=len(content)))
        session.add(PaperSection(paper_id=si.id, section_title='SI chapter', text='retained text'))
        session.commit()
        baseline = session.scalar(select(func.count(Paper.id)))
        assert session.scalar(select(func.count(Paper.id)).where(main_paper_clause())) == 2
        attachment = supplementary_attachments(session, [main.id])[main.id]
        assert len(attachment) == 1
        assert attachment[0]['paper_id'] == str(si.id)
        assert attachment[0]['file_id'] is not None
        assert attachment[0]['original_filename'] == 'original-si.pdf'
        assert attachment[0]['attachment_code'] == si.paper_code
        assert '/api/rebuild/files/' in attachment[0]['download_url']
        rows = PaperListReadonlyService(session).list_papers(PaperListFilterParams(library_name='test-catalog', limit=1, sort_by='created_at', sort_order='asc'))
        assert len(rows) == 1 and rows[0].id == main.id and rows[0].supplementary_count == 1
        next_rows = PaperListReadonlyService(session).list_papers(PaperListFilterParams(library_name='test-catalog', limit=1, offset=1, sort_by='created_at', sort_order='asc'))
        assert next_rows[0].id == other.id
        assert get_paper_type_stats('test-catalog', session)['total'] == 2
        assert _paper_count(session, 'test-catalog') == 2
        overview = _overview_summary_counts(session, [])
        assert overview['papers'] == 2 and overview['pdf_available'] == 1
        assert overview['parsed_papers'] == 0
        assert session.scalar(select(func.count(PaperSection.id))) == 1
        assert next(x for x in list_libraries(session) if x.name == 'test-catalog').paper_count == 2
        assert len(PaperListReadonlyService(session).list_papers(PaperListFilterParams(library_name='test-catalog', has_pdf=True))) == 1
        pending = unassociated_supplementary('test-catalog', session)
        assert pending['total'] == 1 and pending['items'][0]['paper_id'] == str(orphan.id)
        assert session.scalar(select(func.count(Paper.id))) == baseline
        assert not session.dirty


def test_registered_only_si_and_multiple_missing_sources_are_retained(setup_test_db, tmp_path):
    root = tmp_path / 'storage' / 'pdf'
    root.mkdir(parents=True)
    (root / 'registered.pdf').write_bytes(b'%PDF-1.4 registered')
    with Session(setup_test_db) as session:
        main = Paper(library_name='multi-si', title='main', paper_type='B', pdf_path='pdf/main.pdf')
        child = Paper(library_name='multi-si', title='missing attachment', paper_type='si', pdf_path='pdf/missing-1.pdf')
        session.add_all([main, child]); session.flush()
        session.add(PaperRelationship(source_paper_id=main.id, target_paper_id=child.id, relationship_type='supplementary'))
        for index, reference in enumerate(['pdf/registered.pdf', 'pdf/missing-2.pdf']):
            session.add(RebuildPaperFile(paper_id=main.id, role='si', storage_path=reference,
                original_filename=f'attachment-{index}.pdf', sha256=str(index)*64, file_size=10))
        session.commit()
        items = supplementary_attachments(session, [main.id])[main.id]
        assert len(items) == 3
        assert len({item['attachment_code'] for item in items}) == 3
        assert sum(item['pdf_exists'] for item in items) == 1
        assert {item['original_filename'] for item in items} == {'missing-1.pdf', 'attachment-0.pdf', 'attachment-1.pdf'}
