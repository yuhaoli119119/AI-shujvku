"""Tests for decorative figure filtering and figure number extraction."""
from __future__ import annotations

import os

from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Paper, PaperFigure
from app.parsers.docling_parser import DoclingParser
from app.schemas.documents import UnifiedFigure, UnifiedTable
from app.services.paper_ingestion import PaperIngestionService
from app.services.parse_quality_auditor import ParseQualityAuditor
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


class TestPdfImageExtractor:
    def test_extract_uses_image_block_when_bbox_is_missing(self, tmp_path):
        pdf_path = tmp_path / "raster_article.pdf"
        _write_pdf_with_raster_figure(pdf_path)
        output_dir = tmp_path / "figures"
        figure = UnifiedFigure(caption="Figure 1. Red composite panel", page=1, prov=[])

        PdfImageExtractor.extract_figures(pdf_path=pdf_path, figures=[figure], output_dir=output_dir)

        assert figure.image_path is not None
        assert Path(figure.image_path).parts[0] == "raster_article"
        image_path = output_dir / figure.image_path
        assert image_path.exists()
        with Image.open(image_path) as image:
            width, height = image.size
        assert width >= 520
        assert height >= 260
        assert figure.prov[-1]["source"] == "image_block_near_caption"

    def test_extract_uses_caption_anchor_for_vector_figure(self, tmp_path):
        pdf_path = tmp_path / "vector_article.pdf"
        _write_pdf_with_vector_figure(pdf_path)
        output_dir = tmp_path / "figures"
        figure = UnifiedFigure(caption="Figure 2. Vector defect model", page=1, prov=[])

        PdfImageExtractor.extract_figures(pdf_path=pdf_path, figures=[figure], output_dir=output_dir)

        assert figure.image_path is not None
        assert Path(figure.image_path).parts[0] == "vector_article"
        image_path = output_dir / figure.image_path
        assert image_path.exists()
        with Image.open(image_path) as image:
            width, height = image.size
        assert width > 600
        assert 180 <= height <= 520
        assert figure.prov[-1]["source"] == "caption_anchor_above"

    def test_duplicate_figure_numbers_do_not_overwrite_files(self, tmp_path):
        pdf_path = tmp_path / "duplicate_figures.pdf"
        _write_pdf_with_raster_figure(pdf_path)
        output_dir = tmp_path / "figures"
        figures = [
            UnifiedFigure(caption="Figure 1. Red composite panel", page=1, prov=[]),
            UnifiedFigure(caption="Fig. 1. Red composite panel duplicate", page=1, prov=[]),
        ]

        PdfImageExtractor.extract_figures(pdf_path=pdf_path, figures=figures, output_dir=output_dir)

        assert figures[0].image_path
        assert figures[1].image_path
        assert figures[0].image_path != figures[1].image_path
        assert (output_dir / figures[0].image_path).exists()
        assert (output_dir / figures[1].image_path).exists()

    def test_extract_keeps_same_page_left_and_right_image_blocks_separate(self, tmp_path):
        pdf_path = tmp_path / "two_column_figures.pdf"
        _write_pdf_with_two_column_raster_figures(pdf_path)
        output_dir = tmp_path / "figures"
        figures = [
            UnifiedFigure(caption="FIG. 1: Left red defect panel", page=1, prov=[]),
            UnifiedFigure(caption="FIG. 2: Right blue band panel", page=1, prov=[]),
        ]

        PdfImageExtractor.extract_figures(pdf_path=pdf_path, figures=figures, output_dir=output_dir)

        assert figures[0].image_path
        assert figures[1].image_path
        assert figures[0].image_path != figures[1].image_path
        left_bbox = figures[0].prov[-1]["bbox"]
        right_bbox = figures[1].prov[-1]["bbox"]
        assert left_bbox["r"] < right_bbox["l"]
        assert figures[0].prov[-1]["source"] == "image_block_near_caption"
        assert figures[1].prov[-1]["source"] == "image_block_near_caption"

    def test_extract_stores_all_figure_images_under_per_paper_directory(self, tmp_path):
        pdf_path = tmp_path / "per_paper.pdf"
        _write_pdf_with_raster_figure(pdf_path)
        output_dir = tmp_path / "figures"
        figures = [
            UnifiedFigure(caption="Figure 1. Red composite panel", page=1, prov=[]),
            UnifiedFigure(caption="Fig. 1. Red composite panel duplicate", page=1, prov=[]),
        ]

        PdfImageExtractor.extract_figures(pdf_path=pdf_path, figures=figures, output_dir=output_dir)

        for figure in figures:
            assert figure.image_path is not None
            relative = Path(figure.image_path)
            assert relative.parts[0] == "per_paper"
            assert len(relative.parts) == 2
            assert (output_dir / relative).exists()

    def test_find_caption_rect_prefers_exact_caption_over_body_reference(self):
        doc = fitz.open()
        page = doc.new_page(width=420, height=520)
        page.insert_text((72, 80), "Fig. 2 shows a body reference, not a caption.", fontsize=11)
        page.insert_text((72, 300), "Fig. 2. Actual vector defect model", fontsize=11)

        rect = PdfImageExtractor._find_caption_rect(page, "Fig. 2. Actual vector defect model")

        assert rect is not None
        assert rect.y0 > 250
        doc.close()


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

    def test_docling_caption_resolution_deduplicates_repeated_figure_caption(self):
        payload = {
            "texts": [
                {
                    "text": (
                        "Figure 2. Adsorption energies for Li2Sn on g-C3N4 and P-doped substrates. "
                        "Figure 2. Adsorption energies for Li2Sn on g-C3N4 and P-doped substrates."
                    )
                }
            ],
            "figures": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "prov": [{"page_no": 7, "bbox": {"l": 50, "t": 50, "r": 300, "b": 300}}],
                }
            ],
        }

        result = DoclingParser._extract_figures(payload)

        assert len(result) == 1
        assert result[0]["caption"] == (
            "Figure 2. Adsorption energies for Li2Sn on g-C3N4 and P-doped substrates."
        )

    def test_docling_caption_resolution_repairs_common_fragmented_words(self):
        payload = {
            "texts": [
                {
                    "text": (
                        "Figure 11. Energy pro fi les for ad -sorption on P-doped sheets. "
                        "The dashe line marks the Fermi level."
                    )
                }
            ],
            "figures": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "prov": [{"page_no": 15, "bbox": {"l": 50, "t": 50, "r": 300, "b": 300}}],
                }
            ],
        }

        result = DoclingParser._extract_figures(payload)

        assert len(result) == 1
        assert result[0]["caption"] == (
            "Figure 11. Energy profiles for adsorption on P-doped sheets. "
            "The dashed line marks the Fermi level."
        )

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

    def test_docling_table_caption_drops_leading_body_text(self):
        payload = {
            "texts": [
                {
                    "text": (
                        "listed in Table 4 and Tables S2 and S3 of SI. "
                        "The average Li-S bond length decreases. "
                        "Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV)."
                    )
                }
            ],
            "tables": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "prov": [{"page_no": 7}],
                }
            ],
        }

        result = DoclingParser._extract_tables(payload)

        assert result[0]["caption"] == (
            "Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV)."
        )

    def test_docling_table_grid_is_preferred_and_cleans_caption_pollution(self):
        payload = {
            "texts": [
                {
                    "text": (
                        "listed in Table 4 and Tables S2 and S3 of SI. "
                        "Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV). "
                        "Molecules S-S Li-S Li-N E ad"
                    )
                }
            ],
            "tables": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "prov": [{"page_no": 7}],
                    "data": {
                        "grid": [
                            [
                                {"text": "Molecules", "column_header": True},
                                {"text": "S-S", "column_header": True},
                                {"text": "Li-S", "column_header": True},
                                {"text": "Li-N", "column_header": True},
                                {"text": "E ad", "column_header": True},
                            ],
                            [
                                {"text": "Li 2 S 4", "column_header": False},
                                {"text": "2.093", "column_header": False},
                                {"text": "Table 4. The 2.386", "column_header": False},
                                {"text": "average S-S, Li-S, 3.040", "column_header": False},
                                {"text": "adsorption energy 5.225", "column_header": False},
                            ],
                            [
                                {"text": "S 8", "column_header": False},
                                {"text": "-", "column_header": False},
                                {"text": "LiPSs 2.021", "column_header": False},
                                {"text": "P C 2.250", "column_header": False},
                                {"text": "3.502", "column_header": False},
                            ],
                        ]
                    },
                }
            ],
        }

        result = DoclingParser._extract_tables(payload)
        markdown = result[0]["markdown_content"]

        assert "| Molecules | S-S | Li-S | Li-N | E ad |" in markdown
        assert "| Li 2 S 4 | 2.093 | 2.386 | 3.040 | 5.225 |" in markdown
        assert "| S 8 | - | 2.021 | 2.250 | 3.502 |" in markdown

    def test_docling_table_grid_combines_multilevel_headers(self):
        payload = {
            "texts": [{"text": "Table 4. Bond lengths and adsorption energies."}],
            "tables": [
                {
                    "captions": [{"$ref": "#/texts/0"}],
                    "data": {
                        "grid": [
                            [
                                {"text": "Molecules", "column_header": True},
                                {"text": "LiPSs", "column_header": True},
                                {"text": "g-C 3 N 4", "column_header": True},
                                {"text": "P C", "column_header": True},
                            ],
                            [
                                {"text": "Molecules", "column_header": True},
                                {"text": "S-S", "column_header": True},
                                {"text": "Li-N", "column_header": True},
                                {"text": "E ad", "column_header": True},
                            ],
                            [
                                {"text": "Li 2 S 4", "column_header": False},
                                {"text": "2.093", "column_header": False},
                                {"text": "2.351", "column_header": False},
                                {"text": "5.225", "column_header": False},
                            ],
                        ]
                    },
                }
            ],
        }

        result = DoclingParser._extract_tables(payload)

        assert "| Molecules | LiPSs S-S | g-C3N4 Li-N | PC E ad |" in result[0]["markdown_content"]
        assert "| Li 2 S 4 | 2.093 | 2.351 | 5.225 |" in result[0]["markdown_content"]

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
        assert tables[0]["markdown_content"] == tables[0]["caption"]
        assert figures[0]["caption"].startswith("Figure 2.")

    def test_fallback_table_extraction_skips_body_references(self):
        page_blocks = [
            {
                "page": 5,
                "text": (
                    "Table 2, and the results show that the formation energies are similar.\n"
                    "Table 3. Band gaps of the g-C3N4 monolayer and P-g-C3N4."
                ),
            }
        ]

        tables = DoclingParser._extract_fallback_tables(page_blocks)

        assert len(tables) == 1
        assert tables[0]["caption"].startswith("Table 3.")

    def test_parse_quality_auditor_cleans_and_deduplicates_tables(self):
        tables = [
            UnifiedTable(
                caption="Table 2, and the results show this is a body reference.",
                markdown_content="Table 2, and the results show this is a body reference.",
                page=5,
                extraction_source="pypdf_caption_fallback",
            ),
            UnifiedTable(
                caption=(
                    "Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV). "
                    "Molecules LiPSs g-C3N4 PC Li2S 2.108"
                ),
                markdown_content="Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV).",
                page=7,
                extraction_source="pypdf_caption_fallback",
            ),
            UnifiedTable(
                caption="Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV).",
                markdown_content="| Molecules | S-S |\n| --- | --- |\n| S8 | 2.060 |",
                page=7,
                extraction_source="docling",
            ),
        ]

        cleaned = ParseQualityAuditor.clean_tables(tables)

        assert len(cleaned) == 1
        assert cleaned[0].extraction_source == "docling"
        assert cleaned[0].caption == "Table 4. The average S-S, Li-S, and Li-N distances (Å) and adsorption energy (eV)."

    def test_parse_quality_auditor_filters_figures_without_images_and_duplicates(self, tmp_path):
        image_dir = tmp_path / "figures" / "paper"
        image_dir.mkdir(parents=True)
        (image_dir / "fig_1.png").write_bytes(b"same-image")
        (image_dir / "fig_1_dup.png").write_bytes(b"same-image")
        figures = [
            UnifiedFigure(caption="Figure 1. Actual structure.", image_path="paper/fig_1.png", page=2),
            UnifiedFigure(caption="Figure 1. Actual structure duplicate.", image_path="paper/fig_1_dup.png", page=2),
            UnifiedFigure(caption="Figure 2. Missing file.", image_path="paper/missing.png", page=3),
            UnifiedFigure(caption="Figure 3. No image.", image_path=None, page=4),
        ]

        cleaned = ParseQualityAuditor.clean_figures_after_extraction(figures, tmp_path / "figures")

        assert len(cleaned) == 1
        assert cleaned[0].caption == "Figure 1. Actual structure."

    def test_fallback_caption_extraction_skips_body_figure_references(self):
        page_blocks = [
            {
                "page": 7,
                "text": (
                    "Figure 6 shows DMC and DFT defect formation energies against system size.\n"
                    "Fig. 3a presents the process of a single vacancy migration.\n"
                    "Fig. 6 The calculated defect formation energies for monovacancies.\n"
                    "FIG. 7. Band structure of defect-patterned graphene."
                ),
            }
        ]

        figures = DoclingParser._extract_fallback_figures(page_blocks)

        captions = [item["caption"] for item in figures]
        assert len(captions) == 2
        assert captions[0].startswith("Fig. 6 The calculated")
        assert captions[1].startswith("FIG. 7.")
        assert not any("shows DMC" in caption for caption in captions)
        assert not any("3a presents" in caption for caption in captions)

    def test_fallback_caption_extraction_deduplicates_same_page_figure_number(self):
        page_blocks = [
            {
                "page": 2,
                "text": (
                    "FIG. 1: Calculated energies relative to graphene.\n"
                    "Fig. 1. The most stable graphene allotropes are discussed in the text.\n"
                    "FIG. 2: Band structures and charge density maps."
                ),
            }
        ]

        figures = DoclingParser._extract_fallback_figures(page_blocks)

        captions = [item["caption"] for item in figures]
        assert len(captions) == 2
        assert captions[0].startswith("FIG. 1:")
        assert captions[1].startswith("FIG. 2:")


class TestRepairScripts:
    """Tests for dry-run-first cleanup helpers used by maintenance scripts."""

    @staticmethod
    def _session(tmp_path):
        db_url = os.environ["LITAI_TEST_DATABASE_URL"]
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


def _write_pdf_with_raster_figure(pdf_path):
    image = Image.new("RGB", (240, 120), color=(220, 20, 40))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=420, height=520)
    page.insert_image(fitz.Rect(70, 70, 350, 210), stream=buffer.getvalue())
    page.insert_text((72, 240), "Figure 1. Red composite panel", fontsize=12)
    doc.save(pdf_path)
    doc.close()


def _write_pdf_with_vector_figure(pdf_path):
    doc = fitz.open()
    page = doc.new_page(width=420, height=520)
    page.draw_rect(fitz.Rect(80, 70, 340, 210), color=(0, 0, 1), fill=(0.75, 0.85, 1), width=1)
    page.draw_line(fitz.Point(105, 185), fitz.Point(315, 95), color=(1, 0, 0), width=2)
    page.insert_text((100, 130), "Graphene vacancy defect", fontsize=13)
    page.insert_text((72, 240), "Figure 2. Vector defect model", fontsize=12)
    doc.save(pdf_path)
    doc.close()


def _write_pdf_with_two_column_raster_figures(pdf_path):
    red = Image.new("RGB", (160, 110), color=(220, 20, 40))
    blue = Image.new("RGB", (160, 110), color=(30, 80, 220))
    red_buffer = BytesIO()
    blue_buffer = BytesIO()
    red.save(red_buffer, format="PNG")
    blue.save(blue_buffer, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=520, height=520)
    page.insert_image(fitz.Rect(60, 70, 220, 180), stream=red_buffer.getvalue())
    page.insert_image(fitz.Rect(300, 70, 460, 180), stream=blue_buffer.getvalue())
    page.insert_text((62, 210), "FIG. 1: Left red defect panel", fontsize=11)
    page.insert_text((302, 210), "FIG. 2: Right blue band panel", fontsize=11)
    doc.save(pdf_path)
    doc.close()
