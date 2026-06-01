"""Tests for decorative figure filtering and figure number extraction."""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Paper, PaperFigure
from app.parsers.docling_parser import DoclingParser
from app.services.paper_ingestion import PaperIngestionService
from app.services.pdf_image_extractor import PdfImageExtractor
from app.utils.figure_filtering import decorative_figure_reason, is_decorative_figure
from scripts.repair_decorative_figures import repair_decorative_figures
from scripts.repair_polluted_doi_metadata import repair_polluted_dois


def test_ingestion_derives_title_and_doi_from_docling_markdown():
    markdown = """
<!-- image -->
Article
## Enhancing Lithium-Sulfur Battery Performance by MXene, Graphene, and Ionic Liquids: A DFT Investigation
Jianghui Cao 1 , Sensen Xue 1 , Jian Zhang 1
Abstract: The efficacy of lithium-sulfur batteries...
Citation: Cao, J. Molecules 2024, 29, 2. https://doi.org/10.3390/molecules 29010002
"""

    assert PaperIngestionService._derive_title_from_docling(markdown, []) == (
        "Enhancing Lithium-Sulfur Battery Performance by MXene, Graphene, and Ionic Liquids: "
        "A DFT Investigation"
    )
    assert PaperIngestionService._derive_doi_from_text(markdown) == "10.3390/molecules29010002"


class TestIsDecorativeFigure:
    """Tests for DoclingParser._is_decorative_figure."""

    def test_no_caption_no_prov_is_decorative(self):
        """Figure with no caption and no prov should be decorative."""
        assert DoclingParser._is_decorative_figure(None, []) is True

    def test_no_caption_small_bbox_is_decorative(self):
        """Figure with no caption and small bbox should be decorative."""
        prov = [{"bbox": {"l": 0, "t": 0, "r": 30, "b": 30}}]
        assert DoclingParser._is_decorative_figure(None, prov) is True

    def test_no_caption_large_bbox_is_decorative(self):
        """Figure with no caption but large bbox is still decorative (no caption = decorative)."""
        prov = [{"bbox": {"l": 0, "t": 0, "r": 200, "b": 200}}]
        assert DoclingParser._is_decorative_figure(None, prov) is True

    def test_crossmark_caption_is_decorative(self):
        """Caption containing 'crossmark' should be decorative."""
        assert DoclingParser._is_decorative_figure("CrossMark status", []) is True

    def test_elsevier_caption_is_decorative(self):
        """Caption containing 'elsevier' should be decorative."""
        assert DoclingParser._is_decorative_figure("Elsevier logo", []) is True

    def test_copyright_symbol_is_decorative(self):
        """Caption containing copyright symbol should be decorative."""
        assert DoclingParser._is_decorative_figure("\u00a9 2024 Elsevier", []) is True

    def test_creative_commons_is_decorative(self):
        """Caption containing 'creative commons' should be decorative."""
        assert DoclingParser._is_decorative_figure("Creative Commons license", []) is True

    def test_doi_in_caption_is_decorative(self):
        """Caption containing 'doi:' should be decorative."""
        assert DoclingParser._is_decorative_figure("doi: 10.1234/test", []) is True

    def test_normal_caption_is_not_decorative(self):
        """Normal academic figure caption should NOT be decorative."""
        caption = "Figure 3. Schematic illustration of CO2 reduction on Fe-N4 catalyst"
        assert DoclingParser._is_decorative_figure(caption, []) is False

    def test_short_caption_only_is_decorative(self):
        """Bare 'Figure 1' / 'Scheme 1' labels should not enter the figure tab."""
        assert DoclingParser._is_decorative_figure("Figure 1", []) is True
        assert DoclingParser._is_decorative_figure("Fig. 1.", []) is True
        assert DoclingParser._is_decorative_figure("Scheme 1", []) is True

    def test_science_china_press_logo_is_decorative(self):
        assert DoclingParser._is_decorative_figure("Science China Press logo", []) is True

    def test_morphology_caption_is_not_decorative(self):
        """SEM/TEM figure caption should NOT be decorative."""
        caption = "Fig. 2. SEM images of the as-prepared catalyst"
        assert DoclingParser._is_decorative_figure(caption, []) is False

    def test_docling_parser_uses_shared_filter_behavior(self):
        """Parser behavior should match the shared helper used by cleanup scripts."""
        caption = "Science China Press logo"
        assert DoclingParser._is_decorative_figure(caption, []) == is_decorative_figure(caption, [])
        assert decorative_figure_reason(caption, []) == "decorative keyword: science china press"


class TestExtractFigureNumber:
    """Tests for PdfImageExtractor._extract_figure_number."""

    def test_figure_with_number(self):
        """'Figure 3. Schematic...' should extract 3."""
        assert PdfImageExtractor._extract_figure_number("Figure 3. Schematic diagram") == 3

    def test_fig_abbreviated(self):
        """'Fig. 5: XRD patterns' should extract 5."""
        assert PdfImageExtractor._extract_figure_number("Fig. 5: XRD patterns") == 5

    def test_scheme_with_number(self):
        """'Scheme 1: Synthesis route' should extract 1."""
        assert PdfImageExtractor._extract_figure_number("Scheme 1: Synthesis route") == 1

    def test_no_figure_number(self):
        """Caption without figure number should return None."""
        assert PdfImageExtractor._extract_figure_number("SEM image of catalyst") is None

    def test_none_caption(self):
        """None caption should return None."""
        assert PdfImageExtractor._extract_figure_number(None) is None

    def test_empty_caption(self):
        """Empty caption should return None."""
        assert PdfImageExtractor._extract_figure_number("") is None

    def test_case_insensitive(self):
        """'FIGURE 7' should extract 7 (case insensitive)."""
        assert PdfImageExtractor._extract_figure_number("FIGURE 7. Performance comparison") == 7

    def test_fig_without_period(self):
        """'Fig 2' (no period) should extract 2."""
        assert PdfImageExtractor._extract_figure_number("Fig 2: TEM images") == 2


class TestExtractFiguresFiltersDecorative:
    """Integration test: _extract_figures should filter out decorative figures."""

    def test_crossmark_filtered(self):
        """CrossMark figure should be filtered out."""
        payload = {
            "figures": [
                {
                    "captions": [{"text": "CrossMark"}],
                    "prov": [{"page_no": 1, "bbox": {"l": 0, "t": 0, "r": 20, "b": 20}}],
                },
                {
                    "captions": [{"text": "Figure 3. SEM images of catalyst morphology"}],
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "t": 50, "r": 300, "b": 300}}],
                },
            ]
        }
        result = DoclingParser._extract_figures(payload)
        assert len(result) == 1
        assert "SEM" in result[0]["caption"]

    def test_missing_caption_is_not_autofilled_and_is_filtered(self):
        payload = {
            "figures": [
                {"prov": [{"page_no": 1, "bbox": {"l": 0, "t": 0, "r": 300, "b": 300}}]},
                {
                    "captions": [{"text": "Figure 2. XRD patterns of the prepared catalyst"}],
                    "prov": [{"page_no": 2, "bbox": {"l": 50, "t": 50, "r": 300, "b": 300}}],
                },
            ]
        }
        result = DoclingParser._extract_figures(payload)
        assert len(result) == 1
        assert result[0]["caption"].startswith("Figure 2.")
        assert result[0]["page"] == 2

    def test_all_decorative_returns_empty(self):
        """If all figures are decorative, result should be empty."""
        payload = {
            "figures": [
                {"captions": [{"text": "CrossMark"}], "prov": []},
                {"captions": [{"text": "\u00a9 2024 Springer"}], "prov": []},
            ]
        }
        result = DoclingParser._extract_figures(payload)
        assert len(result) == 0

    def test_docling_table_cells_are_converted_to_markdown(self):
        payload = {
            "texts": [{"text": "Table 1: ORR metrics"}],
            "tables": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "prov": [{"page_no": 13}],
                    "data": {
                        "table_cells": [
                            {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "metric"},
                            {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "constant μe"},
                            {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "UL"},
                            {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "0.85 V"},
                        ]
                    },
                }
            ],
        }

        result = DoclingParser._extract_tables(payload)

        assert result[0]["caption"] == "Table 1: ORR metrics"
        assert result[0]["page"] == 13
        assert "| metric | constant μe |" in result[0]["markdown_content"]
        assert "| UL | 0.85 V |" in result[0]["markdown_content"]

    def test_fallback_caption_extraction_finds_tables_and_figures(self):
        page_blocks = [
            {
                "page": 4,
                "text": "Table 1: The limiting potential UL is 0.85 V.\nPDS ∗O to ∗OH\nFigure 2. Free energy diagram for ORR.",
            }
        ]

        tables = DoclingParser._extract_fallback_tables(page_blocks)
        figures = DoclingParser._extract_fallback_figures(page_blocks)

        assert tables[0]["page"] == 4
        assert "limiting potential" in tables[0]["caption"]
        assert figures[0]["caption"].startswith("Figure 2.")


class TestRepairScripts:
    """Tests for dry-run-first cleanup helpers used by maintenance scripts."""

    @staticmethod
    def _session(tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cleanup.db'}"
        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        return engine, Session

    def test_doi_repair_dry_run_does_not_modify_database(self, tmp_path):
        engine, Session = self._session(tmp_path)
        with Session() as session:
            paper = Paper(
                title="Polluted DOI Paper",
                doi="10.1000/main 10.2000/reference",
                pdf_path="paper.pdf",
            )
            session.add(paper)
            session.commit()

            flagged = repair_polluted_dois(session, apply=False)
            session.commit()

            stored = session.scalars(select(Paper).where(Paper.id == paper.id)).one()
            assert len(flagged) == 1
            assert stored.doi == "10.1000/main 10.2000/reference"
        engine.dispose()

    def test_decorative_figure_repair_dry_run_does_not_modify_database(self, tmp_path):
        engine, Session = self._session(tmp_path)
        with Session() as session:
            paper = Paper(title="Decorative Figure Paper", pdf_path="paper.pdf")
            session.add(paper)
            session.flush()
            figure = PaperFigure(
                paper_id=paper.id,
                caption="CrossMark",
                image_path="figures/crossmark.png",
            )
            session.add(figure)
            session.commit()

            flagged = repair_decorative_figures(session, apply=False)
            session.commit()

            remaining = session.scalars(select(PaperFigure)).all()
            assert len(flagged) == 1
            assert len(remaining) == 1
            assert remaining[0].caption == "CrossMark"
        engine.dispose()

    def test_decorative_figure_apply_keeps_real_caption_figure(self, tmp_path):
        engine, Session = self._session(tmp_path)
        with Session() as session:
            paper = Paper(title="Mixed Figures Paper", pdf_path="paper.pdf")
            session.add(paper)
            session.flush()
            decorative = PaperFigure(
                paper_id=paper.id,
                caption="Publisher logo",
                image_path="figures/logo.png",
            )
            real = PaperFigure(
                paper_id=paper.id,
                caption="Figure 2. SEM images of the prepared catalyst",
                image_path="figures/sem.png",
            )
            session.add_all([decorative, real])
            session.commit()

            flagged = repair_decorative_figures(session, apply=True)
            session.commit()

            remaining = session.scalars(select(PaperFigure)).all()
            assert len(flagged) == 1
            assert len(remaining) == 1
            assert remaining[0].caption == "Figure 2. SEM images of the prepared catalyst"
        engine.dispose()
