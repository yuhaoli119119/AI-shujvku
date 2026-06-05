from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    DFTSetting,
    MechanismClaim,
    Paper,
    PaperFigure,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.schemas.api import PaperListFilterParams
from app.services.paper_query import PaperQueryService


def test_paper_query_service_returns_counts_and_detail_payload():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'query.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Queryable Paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()

            session.add(PaperSection(paper_id=paper.id, section_title="Intro", section_type="introduction", text="Intro text", page_start=1, page_end=1))
            session.add(PaperTable(paper_id=paper.id, caption="Table 1", markdown_content="|a|b|", page=2, extraction_source="docling"))
            session.add(PaperFigure(paper_id=paper.id, caption="Figure 1", image_path=None, page=3, figure_role="summary"))
            session.add(DFTSetting(paper_id=paper.id, software="VASP", raw_json={}))
            session.add(CatalystSample(paper_id=paper.id, name="Fe-N4", metal_centers=["Fe"]))
            session.add(DFTResult(paper_id=paper.id, property_type="adsorption_energy", value=-1.23))
            session.add(MechanismClaim(paper_id=paper.id, claim_type="shuttle_suppression", claim_text="claim"))
            session.add(
                WritingCard(
                    paper_id=paper.id,
                    paper_type="mixed",
                    figure_logic='[{"fig_id":"Figure_1","purpose":"summary","supports_claim":"claim"}]',
                )
            )
            session.commit()

            service = PaperQueryService(session)
            listing = service.list_papers()
            detail = service.get_paper_detail(paper.id)

            assert len(listing) == 1
            assert listing[0].counts.dft_settings == 1
            assert listing[0].counts.catalyst_samples == 1
            assert listing[0].counts.writing_cards == 1

            assert detail is not None
            assert len(detail.sections) == 1
            assert len(detail.tables) == 1
            assert len(detail.figures) == 1
            assert len(detail.dft_settings_items) == 1
            assert len(detail.catalyst_samples_items) == 1
            assert len(detail.dft_results_items) == 1
            assert len(detail.mechanism_claims_items) == 1
            assert len(detail.writing_cards_items) == 1
            assert isinstance(detail.writing_cards_items[0].figure_logic, list)

        engine.dispose()


def test_list_papers_with_filters():
    """Verify year/journal/has_dft_results/has_writing_cards/limit/offset filtering."""
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'filter.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            # Paper A: 2024, Nature Catalysis, has DFT results + writing cards
            pa = Paper(title="Paper A", year=2024, journal="Nature Catalysis", pdf_path="a.pdf", authors=["A"])
            session.add(pa)
            session.flush()
            session.add(DFTResult(paper_id=pa.id, property_type="adsorption_energy", value=-1.0))
            session.add(WritingCard(paper_id=pa.id, paper_type="mixed"))

            # Paper B: 2023, JACS, no DFT, no writing cards
            pb = Paper(title="Paper B", year=2023, journal="JACS", pdf_path="b.pdf", authors=["B"])
            session.add(pb)
            session.flush()

            # Paper C: 2024, Angewandte, has DFT only
            pc = Paper(title="Paper C", year=2024, journal="Angewandte Chemie", pdf_path="c.pdf", authors=["C"])
            session.add(pc)
            session.flush()
            session.add(DFTResult(paper_id=pc.id, property_type="barrier", value=0.8))

            session.commit()
            service = PaperQueryService(session)

            # No filter -> all 3
            assert len(service.list_papers()) == 3

            # Filter by year
            result = service.list_papers(PaperListFilterParams(year=2024))
            assert len(result) == 2
            assert all(p.year == 2024 for p in result)

            # Filter by journal (fuzzy)
            result = service.list_papers(PaperListFilterParams(journal="JACS"))
            assert len(result) == 1
            assert result[0].title == "Paper B"

            # Keyword search across title / journal
            result = service.list_papers(PaperListFilterParams(q="Angewandte"))
            assert len(result) == 1
            assert result[0].title == "Paper C"

            result = service.list_papers(PaperListFilterParams(q="Nature"))
            assert len(result) == 1
            assert result[0].title == "Paper A"

            # Filter has_dft_results=True
            result = service.list_papers(PaperListFilterParams(has_dft_results=True))
            assert len(result) == 2
            titles = {p.title for p in result}
            assert titles == {"Paper A", "Paper C"}

            # Filter has_writing_cards=True
            result = service.list_papers(PaperListFilterParams(has_writing_cards=True))
            assert len(result) == 1
            assert result[0].title == "Paper A"

            # Filter has_writing_cards=False
            result = service.list_papers(PaperListFilterParams(has_writing_cards=False))
            assert len(result) == 2
            titles = {p.title for p in result}
            assert titles == {"Paper B", "Paper C"}

            # Pagination: limit=1 offset=0
            result = service.list_papers(PaperListFilterParams(limit=1, offset=0))
            assert len(result) == 1
            # Pagination: limit=1 offset=1
            result = service.list_papers(PaperListFilterParams(limit=1, offset=1))
            assert len(result) == 1
            assert result[0].title != service.list_papers(PaperListFilterParams(limit=1, offset=0))[0].title

        engine.dispose()


def test_list_papers_defaults_to_newest_year_then_serial_order():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'sort.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            p1 = Paper(title="Later serial", year=2019, serial_number=3, pdf_path="1.pdf", authors=["A"])
            p2 = Paper(title="Earlier year", year=2018, serial_number=9, pdf_path="2.pdf", authors=["B"])
            p3 = Paper(title="Earlier serial", year=2019, serial_number=1, pdf_path="3.pdf", authors=["C"])
            p4 = Paper(title="Missing year", year=None, serial_number=2, pdf_path="4.pdf", authors=["D"])
            session.add_all([p1, p2, p3, p4])
            session.commit()

            service = PaperQueryService(session)
            result = service.list_papers()

            assert [paper.title for paper in result] == [
                "Earlier serial",
                "Later serial",
                "Earlier year",
                "Missing year",
            ]

        engine.dispose()


def test_list_papers_supports_descending_year_serial_order():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'sort_desc.db'}", future=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            p1 = Paper(title="Year 2019 serial 1", year=2019, serial_number=1, pdf_path="1.pdf", authors=["A"])
            p2 = Paper(title="Year 2020 serial 2", year=2020, serial_number=2, pdf_path="2.pdf", authors=["B"])
            p3 = Paper(title="Year 2020 serial 1", year=2020, serial_number=1, pdf_path="3.pdf", authors=["C"])
            session.add_all([p1, p2, p3])
            session.commit()

            service = PaperQueryService(session)
            result = service.list_papers(PaperListFilterParams(sort_by="year_serial", sort_order="desc"))

            assert [paper.title for paper in result] == [
                "Year 2020 serial 1",
                "Year 2020 serial 2",
                "Year 2019 serial 1",
            ]

        engine.dispose()
