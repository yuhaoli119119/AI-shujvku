"""Tests for Fact-level fallback repair in Writer._repair_section_with_rule_seed."""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from app.rag.citation_guard import CitationGuard
from app.rag.writer import Writer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_verdict(ok=True, missing_values=None, missing_fact_claims=None, checked_count=1, checked_fact_count=0):
    """Build a verdict dict matching CitationGuard.validate() output."""
    return {
        "ok": ok,
        "missing_values": missing_values or [],
        "supported_values": [],
        "missing_fact_claims": missing_fact_claims or [],
        "supported_fact_claims": [],
        "checked_count": checked_count,
        "checked_fact_count": checked_fact_count,
    }


def _mock_retrieved():
    """Minimal retrieved dict for testing."""
    return {
        "sections": [],
        "dft_results": [],
        "electrochemical_performance": [],
        "mechanism_claims": [],
        "writing_cards": [],
    }


def _make_mock_writer(guard):
    """Create a MagicMock(spec=Writer) with real class attributes bound."""
    writer = MagicMock(spec=Writer)
    writer.citation_guard = guard
    writer._split_sentences = Writer._split_sentences.__get__(writer, Writer)
    writer._tokenize = Writer._tokenize.__get__(writer, Writer)
    # Fix: MagicMock(spec=Writer) intercepts class attribute access, replacing
    # SENTENCE_SPLIT_PATTERN / TOKEN_PATTERN with MagicMock objects.  We must
    # set them to the real compiled patterns so _split_sentences / _tokenize work.
    writer.SENTENCE_SPLIT_PATTERN = Writer.SENTENCE_SPLIT_PATTERN
    writer.TOKEN_PATTERN = Writer.TOKEN_PATTERN
    return writer


# ===========================================================================
# Tests for _repair_section_with_rule_seed
# ===========================================================================

class TestFactLevelFallbackRepair:
    """Tests for the fact-level fallback repair logic in writer.py."""

    # --- 1. verdict["ok"]=True → skip repair (no replacement) ---

    def test_ok_verdict_skips_repair(self):
        """When verdict is ok, the sentence is kept as-is."""
        guard = CitationGuard()
        # Patch validate to always return ok
        with patch.object(guard, "validate", return_value=_make_verdict(ok=True, checked_count=1, checked_fact_count=0)):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="replacement sentence.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The catalyst accelerates conversion.",
                rule_text="Rule seed text.",
                retrieved=_mock_retrieved(),
            )
            # The sentence passes ok, so no replacement
            assert "accelerates conversion" in result_text
            assert action == "guard_reviewed_without_replacement"

    # --- 2. Only missing_values → triggers repair (existing behavior) ---

    def test_missing_values_triggers_repair(self):
        """When only missing_values exist (no missing_fact_claims), repair is triggered."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: whole text has missing values
                return _make_verdict(ok=False, missing_values=[{"value": 1.5, "unit": "ev"}], checked_count=1, checked_fact_count=0)
            # Subsequent calls: per-sentence
            if "1.5 eV" in text or "1.5" in text:
                return _make_verdict(ok=False, missing_values=[{"value": 1.5, "unit": "ev"}], checked_count=1, checked_fact_count=0)
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Rule replacement sentence.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The adsorption energy is 1.5 eV.",
                rule_text="The adsorption energy is 0.8 eV. The catalyst is stable.",
                retrieved=_mock_retrieved(),
            )
            assert "Rule replacement" in result_text
            # "reverted_all_sentences" or "replaced_" — both indicate repair happened
            assert "replaced_" in action or "reverted" in action

    # --- 3. Only missing_fact_claims → triggers repair (NEW behavior) ---

    def test_missing_fact_claims_only_triggers_repair(self):
        """When only missing_fact_claims exist (no missing_values), repair is triggered.
        This is the new behavior — previously this would have been skipped."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            # The generated text has fact claim triggers but no numeric claims
            if "accelerates" in text.lower() and call_count <= 2:
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=0,
                    checked_fact_count=1,
                )
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Rule replacement sentence.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The catalyst accelerates the conversion process.",
                rule_text="The catalyst shows stable binding. The conversion is facilitated by coordination.",
                retrieved=_mock_retrieved(),
            )
            # Should have triggered repair (not just passed through)
            assert "Rule replacement" in result_text
            assert "replaced_" in action or "reverted" in action

    # --- 4. Both missing_values and missing_fact_claims → triggers repair ---

    def test_both_missing_triggers_repair(self):
        """When both missing_values and missing_fact_claims exist, repair is triggered."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_verdict(
                    ok=False,
                    missing_values=[{"value": 1.5, "unit": "ev"}],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=1,
                    checked_fact_count=1,
                )
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Rule replacement sentence.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The catalyst accelerates and the energy is 1.5 eV.",
                rule_text="The catalyst is stable. The energy is well documented.",
                retrieved=_mock_retrieved(),
            )
            assert "Rule replacement" in result_text

    # --- 5. fact_claim_only_count and action suffix logic ---

    def test_action_suffix_fact_claim_unsupported_all_replaced(self):
        """When ALL replaced sentences are fact-claim-only (no missing_values),
        the action suffix should include '_fact_claim_unsupported'."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # Only fact claim issues, no missing values
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=0,
                    checked_fact_count=1,
                )
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Rule replacement sentence.")

            # Only one sentence, and it will be replaced
            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The catalyst accelerates the conversion.",
                rule_text="The catalyst is stable. The conversion is well-documented.",
                retrieved=_mock_retrieved(),
            )
            # All replaced + fact_claim_only → suffix should include _fact_claim_unsupported
            assert "_fact_claim_unsupported" in action

    def test_action_no_suffix_when_mixed_issues(self):
        """When some replaced sentences have missing_values and some have only fact_claims,
        the _fact_claim_unsupported suffix should NOT appear."""
        guard = CitationGuard()
        sentences_checked = []

        def mock_validate(text, facts):
            sentences_checked.append(text)
            # First sentence (whole text check)
            if len(sentences_checked) == 1:
                return _make_verdict(
                    ok=False,
                    missing_values=[{"value": 1.5, "unit": "ev"}],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=1,
                    checked_fact_count=1,
                )
            # Per-sentence: sentence 1 has missing_values + fact_claims
            if "1.5 eV" in text:
                return _make_verdict(
                    ok=False,
                    missing_values=[{"value": 1.5, "unit": "ev"}],
                    missing_fact_claims=[],
                    checked_count=1,
                    checked_fact_count=0,
                )
            if "accelerates" in text.lower():
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=0,
                    checked_fact_count=1,
                )
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Replacement sentence.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The energy is 1.5 eV. The catalyst accelerates conversion.",
                rule_text="The energy is 0.8 eV. The catalyst is stable.",
                retrieved=_mock_retrieved(),
            )
            # Mixed: some missing_values, some only fact_claims → no suffix
            assert "_fact_claim_unsupported" not in action

    # --- 6. No actionable issues (checked_count=0, checked_fact_count=0) → skip ---

    def test_no_actionable_issues_skips_repair(self):
        """When not ok but checked_count==0 and checked_fact_count==0,
        the sentence should be kept as-is (no actionable issues to repair)."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[],
                    checked_count=0,
                    checked_fact_count=0,
                )
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Should not be called.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="Some non-checkable sentence.",
                rule_text="Some rule text.",
                retrieved=_mock_retrieved(),
            )
            # No actionable issues → sentence kept, no replacement
            assert "non-checkable" in result_text
            assert action == "guard_reviewed_without_replacement"

    # --- 7. Partial replacement with fact_claim_only suffix ---

    def test_partial_replacement_fact_claim_only_suffix(self):
        """When some (but not all) sentences are replaced, and ALL replaced are fact-claim-only,
        the suffix should include _fact_claim_unsupported."""
        guard = CitationGuard()
        call_count = 0

        def mock_validate(text, facts):
            nonlocal call_count
            call_count += 1
            # First call: whole text check
            if call_count == 1:
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=0,
                    checked_fact_count=1,
                )
            # "The catalyst accelerates conversion." → fact claim only
            if "accelerates" in text.lower() and "1.5" not in text:
                return _make_verdict(
                    ok=False,
                    missing_values=[],
                    missing_fact_claims=[{"sentence": text, "triggers": ["accelerates"]}],
                    checked_count=0,
                    checked_fact_count=1,
                )
            # "The binding energy is confirmed." → ok
            return _make_verdict(ok=True, checked_count=0, checked_fact_count=0)

        with patch.object(guard, "validate", side_effect=mock_validate):
            writer = _make_mock_writer(guard)
            writer._select_rule_replacement = MagicMock(return_value="Replacement from rule.")

            result_text, action = Writer._repair_section_with_rule_seed(
                writer,
                generated_text="The catalyst accelerates conversion. The binding energy is confirmed.",
                rule_text="The catalyst shows stable binding. The energy is documented.",
                retrieved=_mock_retrieved(),
            )
            # One replaced (fact claim only), one kept → partial replacement with suffix
            assert "_fact_claim_unsupported" in action
            assert "replaced_1" in action
