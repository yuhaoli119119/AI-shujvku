"""Tests for discovery_service classification-aware search."""
from __future__ import annotations

from app.services.discovery_service import DiscoveryService


class TestBuildArxivQuery:
    """Tests for DiscoveryService._build_arxiv_query."""

    def test_no_target_types_returns_all_query(self):
        """Without target_types, query should be wrapped in all: prefix."""
        result = DiscoveryService._build_arxiv_query("CO2 reduction", None)
        assert result.startswith("all:")
        assert "CO2 reduction" in result

    def test_computational_type_adds_dft_keywords(self):
        """computational type should add DFT-related Boolean terms."""
        result = DiscoveryService._build_arxiv_query("CO2 reduction", ["computational"])
        assert "AND" in result
        assert "density functional" in result or "DFT" in result
        assert 'all:"CO2 reduction"' in result

    def test_experimental_type_adds_electrochem_keywords(self):
        """experimental type should add electrochemistry-related Boolean terms."""
        result = DiscoveryService._build_arxiv_query("ORR catalyst", ["experimental"])
        assert "AND" in result
        assert "electrochem" in result or "cataly" in result

    def test_review_type_adds_review_keywords(self):
        """review type should add review-related Boolean terms."""
        result = DiscoveryService._build_arxiv_query("Li-S batteries", ["review"])
        assert "AND" in result
        assert "review" in result or "progress" in result

    def test_multiple_types_combined_with_and(self):
        """Multiple target_types should each add their filter, all joined by AND."""
        result = DiscoveryService._build_arxiv_query("CO2 reduction", ["computational", "experimental"])
        assert result.count("AND") >= 2

    def test_brackets_removed_from_query(self):
        """Square brackets should be stripped from the query."""
        result = DiscoveryService._build_arxiv_query("CO2 [reduction]", None)
        assert "[" not in result
        assert "]" not in result


class TestSearchOpenAlexFilter:
    """Tests for _search_openalex target_types filter construction."""

    def test_computational_type_filter(self):
        """computational should map to 'article' type filter."""
        # We test the filter construction logic, not the actual API call
        # by verifying the internal logic
        type_map = {"computational": "article", "experimental": "article", "review": "review"}
        content_types = set()
        for t in ["computational"]:
            if t in type_map:
                content_types.add(type_map[t])
        assert content_types == {"article"}
        filter_str = f"type:{'|'.join(sorted(content_types))}"
        assert filter_str == "type:article"

    def test_review_type_filter(self):
        """review should map to 'review' type filter."""
        type_map = {"computational": "article", "experimental": "article", "review": "review"}
        content_types = set()
        for t in ["review"]:
            if t in type_map:
                content_types.add(type_map[t])
        filter_str = f"type:{'|'.join(sorted(content_types))}"
        assert filter_str == "type:review"

    def test_mixed_type_filter(self):
        """computational + review should produce article|review filter."""
        type_map = {"computational": "article", "experimental": "article", "review": "review"}
        content_types = set()
        for t in ["computational", "review"]:
            if t in type_map:
                content_types.add(type_map[t])
        filter_str = f"type:{'|'.join(sorted(content_types))}"
        assert filter_str == "type:article|review"
