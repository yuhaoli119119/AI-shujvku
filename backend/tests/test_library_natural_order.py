from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.db.models import Paper
from app.schemas.api import PaperListFilterParams
from app.services.paper_list_readonly import _ordering


def test_natural_default_pagination_filter_search_and_ties(setup_test_db):
    codes=['B1','A10','A2','A002','A010','A999999999999999','AA1','bad!','',None]
    with Session(setup_test_db) as s:
        entries=[]
        for code in codes:
            p=Paper(title='Synthetic code',pdf_path='fixture.pdf',library_name='order fixture')
            s.add(p);s.flush();entries.append((p.id,code))
        s.commit()
        # Legacy/imported codes are fixture SQL, after the catalogue insert hook.
        for pid,code in entries:
            s.execute(update(Paper.__table__).where(Paper.id==pid).values(paper_code=code))
        s.commit()
        params=PaperListFilterParams()
        assert params.sort_by=='paper_code_numeric' and params.sort_order=='asc'
        query=select(Paper).where(Paper.library_name=='order fixture').order_by(*_ordering(params))
        whole=s.scalars(query).all()
        assert [p.paper_code for p in whole[:7]]==['A002','A2','A010','A10','A999999999999999','AA1','B1']
        pages=[p for offset in range(0,len(codes),3) for p in s.scalars(query.offset(offset).limit(3))]
        assert [p.id for p in whole]==[p.id for p in pages]
        assert len({p.id for p in pages})==len(codes)
        searched=s.scalars(query.where(Paper.paper_code.like('A%'))).all()
        assert [p.id for p in searched]==[p.id for p in whole if (p.paper_code or '').startswith('A')]
        assert [p.id for p in s.scalars(query)]==[p.id for p in whole]


from test_rebuild_mutation_audit import audit_env


def test_real_authenticated_api_default_all_pages_search_and_explicit_year(audit_env):
    e=audit_env;library='natural sorting fixture'
    codes=['B1','A10','A2','A002','A010','AA1','bad!']
    with e['factory']() as s:
        entries=[]
        for i,code in enumerate(codes):
            p=Paper(title='Natural code '+code,year=2010+i,pdf_path='fixture.pdf',library_name=library)
            s.add(p);s.flush();entries.append((p.id,code))
        s.commit()
        # Legacy/imported codes are fixture SQL, after the catalogue insert hook.
        for pid,code in entries:
            s.execute(update(Paper.__table__).where(Paper.id==pid).values(paper_code=code))
        s.commit()
    client=e['client']
    def read(**kw):
        r=client.get('/api/papers/',params={'library_name':library,**kw});assert r.status_code==200,r.text;return r.json()
    whole=read()
    assert [p['paper_code'] for p in whole]==['A002','A2','A010','A10','AA1','B1','bad!']
    paged=[p for offset in (0,3,6) for p in read(limit=3,offset=offset)]
    assert [p['paper_id'] for p in paged]==[p['paper_id'] for p in whole]
    assert len({p['paper_id'] for p in paged})==7
    assert [p['paper_code'] for p in read(q='A0')]==['A002','A010']
    explicit=read(sort_by='year_serial',sort_order='desc')
    assert [p['year'] for p in explicit]==sorted([p['year'] for p in whole],reverse=True)
