"""Tests for Evidence pack dedup (_global_dedup) and round-robin (_round_robin_by_paper)."""
from __future__ import annotations

import pytest
from uuid import uuid4

from app.rag.retriever import Retriever
from app.rag.prompt_builder import PaperWriterPromptBuilder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(paper_id, text, score, itype="section", **extra):
    """Build a minimal retrieved item dict."""
    d = {"type": itype, "paper_id": paper_id, "score": score, "text": text}
    d.update(extra)
    return d


# ===========================================================================
# 1. _global_dedup  (Retriever static method)
# ===========================================================================

class TestGlobalDedup:
    """Tests for Retriever._global_dedup."""

    # --- 1.1 Same paper_id + text fingerprint keeps highest score ---

    def test_dedup_across_types_keeps_higher_score(self):
        """Two items with same paper_id + fingerprint but different types & scores:
        the higher-score one wins."""
        pid = uuid4()
        text = "Carbon nanotube composite shows improved cycling stability."
        retrieved = {
            "sections": [_item(pid, text, 0.9, "section")],
            "mechanism_claims": [_item(pid, text, 0.7, "mechanism_claim")],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        # The higher-score (0.9) item in "sections" is kept
        assert len(result["sections"]) == 1
        assert result["sections"][0]["score"] == 0.9
        # The lower-score duplicate in "mechanism_claims" is removed
        assert len(result["mechanism_claims"]) == 0

    def test_dedup_same_type_keeps_higher_score(self):
        """Two items in the same type with same dedup_key: keep higher score."""
        pid = uuid4()
        text = "Lithium polysulfide conversion mechanism."
        retrieved = {
            "sections": [
                _item(pid, text, 0.8),
                _item(pid, text, 0.5),
            ],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        assert len(result["sections"]) == 1
        assert result["sections"][0]["score"] == 0.8

    def test_dedup_same_score_keeps_first_emitted(self):
        """Two items with same paper_id + fingerprint and same score: first wins."""
        pid = uuid4()
        text = "Same content for dedup testing."
        retrieved = {
            "sections": [_item(pid, text, 0.6)],
            "dft_results": [_item(pid, text, 0.6, "dft_result")],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        total = sum(len(v) for v in result.values())
        assert total == 1
        # The first one iterated (sections comes first in dict order in Python 3.7+)
        assert result["sections"][0]["score"] == 0.6

    # --- 1.2 Different paper_id or fingerprint -> not deduped ---

    def test_different_paper_id_not_deduped(self):
        """Same text but different paper_id should NOT be deduped."""
        pid1, pid2 = uuid4(), uuid4()
        text = "Same text different papers."
        retrieved = {
            "sections": [
                _item(pid1, text, 0.9),
                _item(pid2, text, 0.8),
            ],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        assert len(result["sections"]) == 2

    def test_different_fingerprint_not_deduped(self):
        """Same paper_id but different text -> not deduped."""
        pid = uuid4()
        retrieved = {
            "sections": [
                _item(pid, "Text about adsorption energy.", 0.9),
                _item(pid, "Text about cycling performance.", 0.8),
            ],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        assert len(result["sections"]) == 2

    # --- 1.3 Limit per type is respected after dedup ---

    def test_limit_per_type_after_dedup(self):
        """After dedup, each type list is truncated to limit_per_type."""
        pid = uuid4()
        items = [_item(pid, f"Unique text number {i}.", round(0.9 - i * 0.05, 2)) for i in range(8)]
        retrieved = {"sections": items}
        result = Retriever._global_dedup(retrieved, limit_per_type=3)
        assert len(result["sections"]) == 3

    # --- 1.4 Fingerprint uses evidence_text fallback ---

    def test_dedup_uses_evidence_text_when_no_text(self):
        """When 'text' is empty/missing, dedup falls back to 'evidence_text'."""
        pid = uuid4()
        retrieved = {
            "sections": [{"type": "section", "paper_id": pid, "score": 0.9, "text": "", "evidence_text": "Fallback evidence text."}],
            "mechanism_claims": [{"type": "mechanism_claim", "paper_id": pid, "score": 0.5, "text": "", "evidence_text": "Fallback evidence text."}],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        assert len(result["sections"]) == 1
        assert len(result["mechanism_claims"]) == 0

    # --- 1.5 Edge cases ---

    def test_dedup_empty_input(self):
        """Empty input dict returns empty."""
        result = Retriever._global_dedup({}, limit_per_type=5)
        assert result == {}

    def test_dedup_empty_type_lists(self):
        """Type lists can be empty."""
        retrieved = {"sections": [], "dft_results": []}
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        assert result["sections"] == []
        assert result["dft_results"] == []

    def test_dedup_fingerprint_uses_first_80_chars(self):
        """Fingerprint uses first 80 characters stripped and lowered."""
        pid = uuid4()
        long_text_a = "A" * 100  # 100 chars
        long_text_b = "A" * 80 + "B" * 20  # same first 80 chars as long_text_a
        retrieved = {
            "sections": [
                {"type": "section", "paper_id": pid, "score": 0.9, "text": long_text_a},
                {"type": "section", "paper_id": pid, "score": 0.5, "text": long_text_b},
            ],
        }
        result = Retriever._global_dedup(retrieved, limit_per_type=5)
        # Both have same first-80-char fingerprint → deduped to 1
        assert len(result["sections"]) == 1
        assert result["sections"][0]["score"] == 0.9


# ===========================================================================
# 2. _round_robin_by_paper  (PaperWriterPromptBuilder static method)
# ===========================================================================

class TestRoundRobinByPaper:
    """Tests for PaperWriterPromptBuilder._round_robin_by_paper."""

    # --- 2.1 Does not change total item count ---

    def test_preserves_item_count(self):
        """Round-robin should reorder but not drop or add items."""
        pid1, pid2, pid3 = uuid4(), uuid4(), uuid4()
        items = [
            {"paper_id": pid1, "score": 0.9, "summary": "a"},
            {"paper_id": pid1, "score": 0.8, "summary": "b"},
            {"paper_id": pid2, "score": 0.7, "summary": "c"},
            {"paper_id": pid3, "score": 0.6, "summary": "d"},
            {"paper_id": pid1, "score": 0.5, "summary": "e"},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        assert len(result) == 5

    # --- 2.2 Same paper_id does not appear consecutively (when possible) ---

    def test_no_consecutive_same_paper_when_possible(self):
        """When multiple papers exist, same paper should not appear consecutively
        unless it's unavoidable (one paper has more items than the rest combined)."""
        pid1, pid2, pid3 = uuid4(), uuid4(), uuid4()
        # 2 items each from 3 papers — interleaving should prevent consecutive
        items = [
            {"paper_id": pid1, "score": 0.9, "summary": "a"},
            {"paper_id": pid2, "score": 0.85, "summary": "b"},
            {"paper_id": pid3, "score": 0.8, "summary": "c"},
            {"paper_id": pid1, "score": 0.7, "summary": "d"},
            {"paper_id": pid2, "score": 0.65, "summary": "e"},
            {"paper_id": pid3, "score": 0.6, "summary": "f"},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        pids = [str(item.get("paper_id") or "") for item in result]
        for i in range(1, len(pids)):
            assert pids[i] != pids[i - 1], f"Consecutive same paper at position {i}: {pids}"

    def test_consecutive_allowed_when_unavoidable(self):
        """When one paper has more items than the rest combined, consecutive is inevitable."""
        pid1, pid2 = uuid4(), uuid4()
        items = [
            {"paper_id": pid1, "score": 0.9, "summary": "a"},
            {"paper_id": pid1, "score": 0.8, "summary": "b"},
            {"paper_id": pid2, "score": 0.7, "summary": "c"},
            {"paper_id": pid1, "score": 0.6, "summary": "d"},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        # Total items preserved
        assert len(result) == 4
        # First two should alternate: pid1, pid2
        pids = [str(item.get("paper_id") or "") for item in result]
        assert pids[0] != pids[1], f"First two should alternate: {pids}"

    # --- 2.3 All items from same paper → no rearrangement needed (all consecutive is ok) ---

    def test_single_paper_all_consecutive(self):
        """When all items are from the same paper, consecutive is inevitable."""
        pid = uuid4()
        items = [
            {"paper_id": pid, "score": 0.9, "summary": "a"},
            {"paper_id": pid, "score": 0.8, "summary": "b"},
            {"paper_id": pid, "score": 0.7, "summary": "c"},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        assert len(result) == 3

    # --- 2.4 Empty list → empty result ---

    def test_empty_list(self):
        result = PaperWriterPromptBuilder._round_robin_by_paper([])
        assert result == []

    # --- 2.5 Single item ---

    def test_single_item(self):
        pid = uuid4()
        items = [{"paper_id": pid, "score": 0.9, "summary": "only"}]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        assert len(result) == 1
        assert result[0]["summary"] == "only"

    # --- 2.6 Greedy strategy does not deadlock ---

    def test_no_deadlock_with_many_papers(self):
        """Complex scenario with 4 papers, uneven distribution — must complete."""
        p1, p2, p3, p4 = uuid4(), uuid4(), uuid4(), uuid4()
        items = [
            {"paper_id": p1, "score": 0.9},
            {"paper_id": p1, "score": 0.85},
            {"paper_id": p1, "score": 0.80},
            {"paper_id": p2, "score": 0.75},
            {"paper_id": p3, "score": 0.70},
            {"paper_id": p4, "score": 0.65},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        assert len(result) == 6

    # --- 2.7 Round-robin prefers higher score among non-consecutive groups ---

    def test_prefers_higher_score_alternating(self):
        """When picking between groups, the one with highest next-item score is preferred."""
        pid1, pid2 = uuid4(), uuid4()
        items = [
            {"paper_id": pid1, "score": 0.9},
            {"paper_id": pid2, "score": 0.8},
            {"paper_id": pid1, "score": 0.7},
            {"paper_id": pid2, "score": 0.6},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        # First should be pid1 (0.9), then pid2 (0.8), then pid1 (0.7), then pid2 (0.6)
        assert str(result[0]["paper_id"]) == str(pid1)
        assert str(result[1]["paper_id"]) == str(pid2)

    # --- 2.8 None paper_id is treated as empty string group ---

    def test_none_paper_id_handled(self):
        """Items with None paper_id should be grouped under empty string."""
        items = [
            {"paper_id": None, "score": 0.9, "summary": "a"},
            {"paper_id": None, "score": 0.8, "summary": "b"},
        ]
        result = PaperWriterPromptBuilder._round_robin_by_paper(items)
        assert len(result) == 2
