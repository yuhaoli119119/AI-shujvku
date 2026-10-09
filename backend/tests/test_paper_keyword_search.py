import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperSection
from app.schemas.api import PaperListFilterParams
from app.services.paper_query import PaperQueryService


@pytest.fixture
def keyword_session():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _search(session: Session, query: str, *, library_name: str | None = None) -> set[str]:
    results = PaperQueryService(session).list_papers(
        PaperListFilterParams(q=query, library_name=library_name)
    )
    return {paper.title for paper in results}


def test_lis_battery_aliases_match_lithium_sulfur_title(keyword_session):
    keyword_session.add(
        Paper(
            title="Lithium-sulfur batteries with catalytic hosts",
            library_name="Search Library",
            pdf_path="",
        )
    )
    keyword_session.commit()

    assert _search(keyword_session, "LiS 电池") == {
        "Lithium-sulfur batteries with catalytic hosts"
    }


@pytest.mark.parametrize("query", ["LiS电池", "Li-S电池", "锂硫电池", "锂-硫电池"])
def test_compound_lis_battery_aliases_match_lithium_sulfur_title(keyword_session, query):
    keyword_session.add_all(
        [
            Paper(
                title="Lithium-sulfur batteries with catalytic hosts",
                library_name="Search Library",
                pdf_path="",
            ),
            Paper(
                title="Lithium-sulfur catalysis without device testing",
                library_name="Search Library",
                pdf_path="",
            ),
        ]
    )
    keyword_session.commit()

    assert _search(keyword_session, query) == {
        "Lithium-sulfur batteries with catalytic hosts"
    }


def test_polysulfide_alias_matches_section_text(keyword_session):
    paper = Paper(title="Fe-N4 catalytic study", library_name="Search Library", pdf_path="")
    keyword_session.add(paper)
    keyword_session.flush()
    keyword_session.add(
        PaperSection(
            paper_id=paper.id,
            section_title="Results",
            section_type="results",
            text="Polysulfide conversion is accelerated on Fe-N4 sites.",
        )
    )
    keyword_session.commit()

    assert _search(keyword_session, "多硫化物 conversion") == {"Fe-N4 catalytic study"}


def test_unicode_dash_and_sulfur_reduction_aliases_match(keyword_session):
    keyword_session.add(
        Paper(
            title="Li–S sulfur redox kinetics on single-atom sites",
            library_name="Search Library",
            pdf_path="",
        )
    )
    keyword_session.commit()

    assert _search(keyword_session, "lithium-sulfur 硫还原") == {
        "Li–S sulfur redox kinetics on single-atom sites"
    }


def test_multiple_keyword_groups_remain_and_combined(keyword_session):
    keyword_session.add_all(
        [
            Paper(
                title="Lithium-sulfur catalysis without device testing",
                library_name="Search Library",
                pdf_path="",
            ),
            Paper(
                title="Lithium sulfur cell catalysis",
                library_name="Search Library",
                pdf_path="",
            ),
        ]
    )
    keyword_session.commit()

    assert _search(keyword_session, "LiS 电池") == {"Lithium sulfur cell catalysis"}


def test_short_lis_alias_does_not_match_inside_catalysis(keyword_session):
    keyword_session.add_all(
        [
            Paper(
                title="Catalysis and unit cell modeling",
                abstract="Catalysis descriptors are discussed with battery references.",
                library_name="Search Library",
                pdf_path="",
            ),
            Paper(
                title="LiS@graphene battery model",
                library_name="Search Library",
                pdf_path="",
            ),
        ]
    )
    keyword_session.commit()

    assert _search(keyword_session, "LiS 电池") == {"LiS@graphene battery model"}


def test_keyword_search_respects_library_boundary(keyword_session):
    keyword_session.add_all(
        [
            Paper(
                title="Lithium-sulfur battery in Library A",
                library_name="Library A",
                pdf_path="",
            ),
            Paper(
                title="Lithium-sulfur battery in Library B",
                library_name="Library B",
                pdf_path="",
            ),
        ]
    )
    keyword_session.commit()

    assert _search(keyword_session, "LiS 电池", library_name="Library A") == {
        "Lithium-sulfur battery in Library A"
    }


def test_plain_keyword_search_still_matches_title(keyword_session):
    keyword_session.add_all(
        [
            Paper(title="Operando spectroscopy of catalytic hosts", pdf_path=""),
            Paper(title="Electrochemical modeling study", pdf_path=""),
        ]
    )
    keyword_session.commit()

    assert _search(keyword_session, "Operando") == {
        "Operando spectroscopy of catalytic hosts"
    }
