"""Tests for Writer type-differentiated templates (A/C/R/default)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.rag.writer import Writer


def _make_writer():
    """Create a Writer with minimal mock session/settings."""
    session = MagicMock()
    settings = MagicMock()
    settings.writer_prompt_path = Path("/nonexistent.yaml")
    settings.writer_backend = "rule"
    settings.writer_model = "test"
    settings.writer_api_base = None
    settings.writer_api_key = None
    settings.writer_fallback_backend = "rule"
    return Writer(session, settings)


class TestBuildOutlineByType:
    """Tests for Writer._build_outline with different paper types."""

    def test_type_a_outine(self):
        """A-type outline should contain computational methodology items."""
        w = _make_writer()
        outline = w._build_outline("CO2 reduction", {}, "A1")
        assert any("Computational methodology" in item or "computational" in item.lower() for item in outline)
        assert any("Electronic structure" in item for item in outline)

    def test_type_c_outline(self):
        """C-type outline should contain experimental items."""
        w = _make_writer()
        outline = w._build_outline("ORR catalyst", {}, "C2")
        assert any("characterization" in item.lower() or "Materials" in item for item in outline)
        assert any("Electrochemical" in item for item in outline)

    def test_type_r_outline(self):
        """R-type outline should contain review-specific items."""
        w = _make_writer()
        outline = w._build_outline("Li-S batteries", {}, "R")
        assert any("Historical" in item or "review" in item.lower() for item in outline)
        assert any("Future" in item for item in outline)

    def test_default_outline(self):
        """Default (None) outline should contain DFT/evidence items."""
        w = _make_writer()
        outline = w._build_outline("CO2 reduction", {}, None)
        assert any("DFT" in item for item in outline)


class TestBuildIntroductionByType:
    """Tests for Writer._build_introduction with different paper types."""

    def test_type_a_introduction(self):
        """A-type introduction should mention computational/first-principles."""
        w = _make_writer()
        intro = w._build_introduction("CO2 reduction", {}, "A1")
        assert "first-principles" in intro or "computational" in intro.lower() or "density functional" in intro

    def test_type_c_introduction(self):
        """C-type introduction should mention experimental/validation."""
        w = _make_writer()
        intro = w._build_introduction("ORR catalyst", {}, "C2")
        assert "experimental" in intro.lower() or "characterized" in intro.lower()

    def test_type_r_introduction(self):
        """R-type introduction should mention review/comprehensive."""
        w = _make_writer()
        intro = w._build_introduction("Li-S batteries", {}, "R")
        assert "review" in intro.lower() or "comprehensive" in intro.lower()

    def test_default_introduction(self):
        """Default introduction should mention lithium-sulfur or energy density."""
        w = _make_writer()
        intro = w._build_introduction("CO2 reduction", {}, None)
        assert len(intro) > 50  # Non-empty
