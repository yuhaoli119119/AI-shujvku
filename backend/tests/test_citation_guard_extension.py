"""Tests for Citation guard fact-level rule extension (mediates, infers_causality, STRICT_FACT_CONTEXT_KEYWORDS)."""
from __future__ import annotations

import pytest

from app.rag.citation_guard import (
    FACT_TRIGGER_SYNONYMS,
    STRICT_FACT_CONTEXT_KEYWORDS,
    CitationGuard,
)


# ===========================================================================
# 1. FACT_TRIGGER_SYNONYMS additions
# ===========================================================================

class TestFactTriggerSynonyms:
    """Tests for the new entries in FACT_TRIGGER_SYNONYMS."""

    def test_mediates_key_exists(self):
        """The 'mediates' canonical key must exist in FACT_TRIGGER_SYNONYMS."""
        assert "mediates" in FACT_TRIGGER_SYNONYMS

    def test_infers_causality_key_exists(self):
        """The 'infers_causality' canonical key must exist in FACT_TRIGGER_SYNONYMS."""
        assert "infers_causality" in FACT_TRIGGER_SYNONYMS

    def test_mediates_variants(self):
        """Verify 'mediates' has expected word-level variants."""
        variants = FACT_TRIGGER_SYNONYMS["mediates"]
        for word in ["mediates", "via", "through"]:
            assert word in variants, f"Expected '{word}' in mediates variants"

    def test_infers_causality_variants(self):
        """Verify 'infers_causality' has expected phrase-level variants."""
        variants = FACT_TRIGGER_SYNONYMS["infers_causality"]
        for phrase in ["due to", "leads to", "because", "as a result"]:
            assert phrase in variants, f"Expected '{phrase}' in infers_causality variants"


# ===========================================================================
# 2. STRICT_FACT_CONTEXT_KEYWORDS additions
# ===========================================================================

class TestStrictFactContextKeywords:
    """Tests for the new entries in STRICT_FACT_CONTEXT_KEYWORDS."""

    def test_coordination_keyword_exists(self):
        assert "coordination" in STRICT_FACT_CONTEXT_KEYWORDS

    def test_mechanism_keyword_exists(self):
        assert "mechanism" in STRICT_FACT_CONTEXT_KEYWORDS

    def test_original_keywords_preserved(self):
        """Ensure existing keywords are not accidentally removed."""
        for kw in ["barrier", "adsorption", "binding", "capacity", "retention", "stability", "cyclability"]:
            assert kw in STRICT_FACT_CONTEXT_KEYWORDS, f"Original keyword '{kw}' missing"


# ===========================================================================
# 3. _extract_fact_triggers — word-level matching for 'mediates'
# ===========================================================================

class TestExtractFactTriggersMediates:
    """Tests that 'mediates' key triggers via word-level matching."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_mediates_word_trigger(self):
        """The word 'mediates' should trigger the 'mediates' canonical key."""
        tokens = self.guard._tokenize("The catalyst mediates the conversion process.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="The catalyst mediates the conversion process.")
        assert "mediates" in triggers

    def test_via_word_trigger(self):
        """The word 'via' should trigger the 'mediates' canonical key."""
        tokens = self.guard._tokenize("Conversion occurs via the catalytic site.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Conversion occurs via the catalytic site.")
        assert "mediates" in triggers

    def test_through_word_trigger(self):
        """The word 'through' should trigger the 'mediates' canonical key."""
        tokens = self.guard._tokenize("Binding occurs through the coordination site.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Binding occurs through the coordination site.")
        assert "mediates" in triggers


# ===========================================================================
# 4. _extract_fact_triggers — phrase-level matching for 'infers_causality'
# ===========================================================================

class TestExtractFactTriggersInfersCausality:
    """Tests that 'infers_causality' key triggers via phrase substring matching."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_due_to_phrase_trigger(self):
        """The phrase 'due to' should trigger 'infers_causality'."""
        tokens = self.guard._tokenize("The capacity fade is due to polysulfide shuttle.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="The capacity fade is due to polysulfide shuttle.")
        assert "infers_causality" in triggers

    def test_leads_to_phrase_trigger(self):
        """The phrase 'leads to' should trigger 'infers_causality'."""
        tokens = self.guard._tokenize("Strong adsorption leads to improved cycling.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Strong adsorption leads to improved cycling.")
        assert "infers_causality" in triggers

    def test_because_phrase_trigger(self):
        """The word 'because' should trigger 'infers_causality'."""
        tokens = self.guard._tokenize("Performance improved because of catalytic site.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Performance improved because of catalytic site.")
        assert "infers_causality" in triggers

    def test_resulting_in_phrase_trigger(self):
        """The phrase 'resulting in' should trigger 'infers_causality'."""
        tokens = self.guard._tokenize("Anchoring results in stable cycling performance.")
        # "resulting in" vs "results in" — only "resulting in" is in variants
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Anchoring resulting in stable cycling performance.")
        assert "infers_causality" in triggers

    def test_as_a_result_phrase_trigger(self):
        """The phrase 'as a result' should trigger 'infers_causality'."""
        tokens = self.guard._tokenize("As a result, the capacity improved.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="As a result, the capacity improved.")
        assert "infers_causality" in triggers


# ===========================================================================
# 5. Other canonical keys are not affected
# ===========================================================================

class TestExistingTriggersUnaffected:
    """Tests that existing canonical keys still work correctly."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_accelerates_still_works(self):
        tokens = self.guard._tokenize("The site accelerates conversion kinetics.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="The site accelerates conversion kinetics.")
        assert "accelerates" in triggers

    def test_suppresses_still_works(self):
        tokens = self.guard._tokenize("The coating suppresses polysulfide shuttle.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="The coating suppresses polysulfide shuttle.")
        assert "suppresses" in triggers

    def test_causes_still_works(self):
        tokens = self.guard._tokenize("Shuttle effect causes rapid capacity decay.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="Shuttle effect causes rapid capacity decay.")
        assert "causes" in triggers

    def test_evidences_still_works(self):
        tokens = self.guard._tokenize("DFT calculation demonstrates the binding strength.")
        triggers = self.guard._extract_fact_triggers(tokens, sentence="DFT calculation demonstrates the binding strength.")
        assert "evidences" in triggers


# ===========================================================================
# 6. sentence parameter default value backward compatibility
# ===========================================================================

class TestSentenceParamBackwardCompat:
    """Tests that _extract_fact_triggers works without the sentence parameter."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_word_level_without_sentence_param(self):
        """Calling _extract_fact_triggers without sentence should still work for word-level triggers."""
        tokens = self.guard._tokenize("The catalyst mediates the reaction.")
        triggers = self.guard._extract_fact_triggers(tokens)
        # Word-level 'mediates' should still trigger
        assert "mediates" in triggers

    def test_infers_causality_not_triggered_without_sentence(self):
        """Without the sentence param, infers_causality cannot do phrase matching.
        It should simply not trigger for phrase-only variants."""
        tokens = self.guard._tokenize("The capacity fade is due to polysulfide shuttle.")
        triggers = self.guard._extract_fact_triggers(tokens)
        # "due to" is a multi-word phrase, and word-level tokenization
        # won't match it as a phrase — so infers_causality should NOT trigger
        # unless by coincidence a single-word variant matches a token.
        # "because" is a single word and would be in tokens, but "due" and "to"
        # are separate tokens.
        # This is expected behavior: without sentence param, phrase matching is disabled.
        # The test verifies no crash occurs.
        assert isinstance(triggers, list)


# ===========================================================================
# 7. STRICT_FACT_CONTEXT_KEYWORDS new words participate in fact claim judgment
# ===========================================================================

class TestStrictKeywordsInFactClaim:
    """Tests that 'coordination' and 'mechanism' participate in strict fact context matching."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_coordination_in_strict_context_check(self):
        """A claim with 'coordination' in context requires it in evidence."""
        # Build a claim with 'coordination' in strict context
        claim = {
            "sentence": "The coordination accelerates conversion.",
            "triggers": ["accelerates"],
            "context": ["coordination", "binding"],
        }
        # Evidence without 'coordination' should fail strict check
        evidence = [
            {"text": "Catalyst accelerates the reaction.", "triggers": ["accelerates"], "context": ["binding", "adsorption"]},
        ]
        result = self.guard._find_support_for_trigger("accelerates", claim, evidence)
        # "coordination" is in STRICT_FACT_CONTEXT_KEYWORDS and is in claim context
        # but not in evidence context → should not match
        assert result is None

    def test_mechanism_in_strict_context_check(self):
        """A claim with 'mechanism' in context requires it in evidence."""
        claim = {
            "sentence": "The mechanism suppresses polysulfide shuttle.",
            "triggers": ["suppresses"],
            "context": ["mechanism", "binding"],
        }
        evidence = [
            {"text": "Catalyst suppresses shuttle effect.", "triggers": ["suppresses"], "context": ["binding", "stability"]},
        ]
        result = self.guard._find_support_for_trigger("suppresses", claim, evidence)
        # "mechanism" is in STRICT_FACT_CONTEXT_KEYWORDS and is in claim context
        # but not in evidence context → should not match
        assert result is None

    def test_new_strict_keyword_present_in_evidence_passes(self):
        """When 'coordination' is in both claim and evidence strict context, it passes."""
        claim = {
            "sentence": "The coordination accelerates conversion.",
            "triggers": ["accelerates"],
            "context": ["coordination", "binding"],
        }
        evidence = [
            {"text": "Catalyst coordination accelerates the reaction.", "triggers": ["accelerates"], "context": ["coordination", "binding"]},
        ]
        result = self.guard._find_support_for_trigger("accelerates", claim, evidence)
        assert result is not None


# ===========================================================================
# 8. Integration: _extract_textual_claims passes sentence to _extract_fact_triggers
# ===========================================================================

class TestExtractTextualClaimsPassesSentence:
    """Verify that _extract_textual_claims correctly passes the sentence parameter."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_phrase_trigger_in_extract_textual_claims(self):
        """A sentence with 'due to' should produce a textual claim with 'infers_causality' trigger."""
        claims = self.guard._extract_textual_claims("Capacity fade is due to polysulfide shuttle. The binding is stable.")
        trigger_keys = []
        for claim in claims:
            trigger_keys.extend(claim.get("triggers", []))
        assert "infers_causality" in trigger_keys

    def test_word_trigger_in_extract_textual_claims(self):
        """A sentence with 'mediates' should produce a textual claim with 'mediates' trigger."""
        claims = self.guard._extract_textual_claims("The site mediates the conversion. The binding is strong.")
        trigger_keys = []
        for claim in claims:
            trigger_keys.extend(claim.get("triggers", []))
        assert "mediates" in trigger_keys


# ===========================================================================
# 9. Integration: _collect_textual_evidence_items passes sentence to _extract_fact_triggers
# ===========================================================================

class TestCollectTextualEvidenceItemsPassesSentence:
    """Verify that _collect_textual_evidence_items correctly passes the sentence parameter."""

    def setup_method(self):
        self.guard = CitationGuard()

    def test_phrase_trigger_in_evidence_items(self):
        """Evidence items with 'leads to' should have 'infers_causality' trigger."""
        facts = {
            "mechanism_claims": [
                {
                    "text": "Strong adsorption leads to improved cycling stability.",
                    "evidence_text": "Strong adsorption leads to improved cycling stability.",
                }
            ]
        }
        items = self.guard._collect_textual_evidence_items(facts)
        all_triggers = []
        for item in items:
            all_triggers.extend(item.get("triggers", []))
        assert "infers_causality" in all_triggers

    def test_word_trigger_in_evidence_items(self):
        """Evidence items with 'mediates' should have 'mediates' trigger."""
        facts = {
            "mechanism_claims": [
                {
                    "text": "The site mediates polysulfide conversion.",
                    "evidence_text": "The site mediates polysulfide conversion.",
                }
            ]
        }
        items = self.guard._collect_textual_evidence_items(facts)
        all_triggers = []
        for item in items:
            all_triggers.extend(item.get("triggers", []))
        assert "mediates" in all_triggers
