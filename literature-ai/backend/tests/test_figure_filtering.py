"""Tests for decorative figure filtering and figure number extraction."""
from __future__ import annotations

from app.parsers.docling_parser import DoclingParser
from app.services.pdf_image_extractor import PdfImageExtractor


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

    def test_morphology_caption_is_not_decorative(self):
        """SEM/TEM figure caption should NOT be decorative."""
        caption = "Fig. 2. SEM images of the as-prepared catalyst"
        assert DoclingParser._is_decorative_figure(caption, []) is False


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
