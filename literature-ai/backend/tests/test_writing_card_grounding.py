from types import SimpleNamespace
from pathlib import Path

from app.db.models import WritingCard
from app.extractors.writing_card_extractor import WritingCardExtractor, WritingCardModel
from app.utils.review_safety import writing_card_content_gate


def _document():
    return {
        "metadata": {"title": "Grounded catalyst design"},
        "abstract": (
            "However, slow conversion remains an unresolved limitation for sulfur hosts. "
            "In this work, we develop Fe-N4 sites to accelerate polysulfide conversion. "
            "We hypothesize that Fe-N4 coordination could lower the Li2S barrier by stabilizing Li2S2."
        ),
        "sections": [
            SimpleNamespace(
                section_title="Introduction",
                text=(
                    "However, slow conversion remains an unresolved limitation for sulfur hosts. "
                    "In this work, we develop Fe-N4 sites to accelerate polysulfide conversion. "
                    "We hypothesize that Fe-N4 coordination could lower the Li2S barrier by stabilizing Li2S2."
                ),
                page_start=2,
            ),
            SimpleNamespace(
                section_title="Results",
                text="The Fe-N4 catalyst shows a barrier of 0.42 eV and improved cycling.",
                page_start=5,
            ),
        ],
        "figures": [],
        "tables": [],
        "markdown": "paper text",
    }


def _llm_payload(**overrides):
    payload = {
        "paper_type": "mixed",
        "research_gap": "However, slow conversion remains an unresolved limitation for sulfur hosts.",
        "proposed_solution": "In this work, we develop Fe-N4 sites to accelerate polysulfide conversion.",
        "core_hypothesis": "We hypothesize that Fe-N4 coordination could lower the Li2S barrier by stabilizing Li2S2.",
        "evidence_chain": [],
        "section_strategy": {},
        "figure_logic": [],
        "abstract_logic": "",
        "introduction_logic": "",
        "discussion_logic": "",
    }
    payload.update(overrides)
    return payload


class _DummyLLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def is_configured(self):
        return True

    def structured_extract(self, system_prompt, user_prompt, response_format):
        assert "supports_fields" in system_prompt
        if self.error:
            raise self.error
        return WritingCardModel.model_validate(self.payload)


def test_all_explicit_core_fields_are_grounded_with_section_and_page():
    extractor = WritingCardExtractor()
    extractor.llm = _DummyLLM(_llm_payload())

    result = extractor.extract(_document())

    assert all(result[field] for field in ("research_gap", "proposed_solution", "core_hypothesis"))
    core = [item for item in result["evidence_chain"] if item.get("supports_fields")]
    assert {item["supports_fields"][0] for item in core} == {
        "research_gap", "proposed_solution", "core_hypothesis"
    }
    assert all(item["source"] == "Introduction" or item["source"] == "Abstract" for item in core)
    assert any(item["page"] == 2 and item["locator_status"] == "exact_page" for item in core)


def test_pdf_abstract_evidence_falls_back_to_page_one():
    extractor = WritingCardExtractor()
    doc = {
        "metadata": {"title": "Abstract grounding"},
        "abstract": (
            "However, oxygen reduction kinetics remain limited in neutral media. "
            "Herein, we report Fe-Co dual sites for accelerated oxygen reduction."
        ),
        "sections": [],
        "figures": [],
        "tables": [],
        "source_pdf_path": Path("paper.pdf"),
    }

    result = extractor._validate_and_ground_card(
        _llm_payload(
            research_gap="However, oxygen reduction kinetics remain limited in neutral media.",
            proposed_solution="Herein, we report Fe-Co dual sites for accelerated oxygen reduction.",
            core_hypothesis="",
        ),
        doc,
    )

    core = [item for item in result["evidence_chain"] if item.get("supports_fields")]
    assert {item["supports_fields"][0] for item in core} == {"research_gap", "proposed_solution"}
    assert all(item["source"] == "Abstract" for item in core)
    assert all(item["page"] == 1 and item["locator_status"] == "exact_page" for item in core)
    card = WritingCard(
        research_gap=result["research_gap"],
        proposed_solution=result["proposed_solution"],
        evidence_chain=result["evidence_chain"],
    )
    assert writing_card_content_gate(card).can_use_for_writing is True


def test_metadata_only_abstract_evidence_stays_text_only():
    extractor = WritingCardExtractor()
    doc = {
        "metadata": {"title": "Metadata only"},
        "abstract": (
            "However, oxygen reduction kinetics remain limited in neutral media. "
            "Herein, we report Fe-Co dual sites for accelerated oxygen reduction."
        ),
        "sections": [],
        "figures": [],
        "tables": [],
    }

    result = extractor._validate_and_ground_card(
        _llm_payload(
            research_gap="However, oxygen reduction kinetics remain limited in neutral media.",
            proposed_solution="Herein, we report Fe-Co dual sites for accelerated oxygen reduction.",
            core_hypothesis="",
        ),
        doc,
    )

    core = [item for item in result["evidence_chain"] if item.get("supports_fields")]
    assert core
    assert all(item["page"] is None and item["locator_status"] == "text_only" for item in core)
    card = WritingCard(
        research_gap=result["research_gap"],
        proposed_solution=result["proposed_solution"],
        evidence_chain=result["evidence_chain"],
    )
    assert writing_card_content_gate(card).can_use_for_writing is False


def test_invented_material_and_number_are_rejected():
    extractor = WritingCardExtractor()
    doc = _document()
    material = extractor._validate_and_ground_card(
        _llm_payload(proposed_solution="In this work, we develop Co-N4 sites to accelerate polysulfide conversion."), doc
    )
    number = extractor._validate_and_ground_card(
        _llm_payload(proposed_solution="In this work, we develop Fe-N4 sites with a 9.99 eV barrier."), doc
    )

    assert material["proposed_solution"] == ""
    assert number["proposed_solution"] == ""


def test_solution_derived_hypothesis_and_missing_source_evidence_are_rejected():
    extractor = WritingCardExtractor()
    doc = _document()
    derived = extractor._validate_and_ground_card(
        _llm_payload(core_hypothesis="The core hypothesis underlying this approach is Fe-N4 site development."), doc
    )
    absent = extractor._validate_and_ground_card(
        _llm_payload(research_gap="No prior study has measured an operando cobalt intermediate."), doc
    )

    assert derived["core_hypothesis"] == ""
    assert absent["research_gap"] == ""


def test_two_reliable_fields_pass_gate_but_one_and_legacy_evidence_do_not():
    evidence = [
        {"text": "A limitation remains unresolved.", "source": "Introduction", "supports_fields": ["research_gap"], "page": 2, "locator_status": "exact_page"},
        {"text": "This work develops Fe-N4 sites.", "source": "Introduction", "supports_fields": ["proposed_solution"], "page": 2, "locator_status": "exact_page"},
    ]
    two = WritingCard(research_gap="A limitation remains unresolved.", proposed_solution="This work develops Fe-N4 sites.", evidence_chain=evidence)
    one = WritingCard(research_gap=two.research_gap, evidence_chain=evidence[:1])
    legacy = WritingCard(research_gap=two.research_gap, proposed_solution=two.proposed_solution, evidence_chain={"pages": [2]})

    assert writing_card_content_gate(two).can_use_for_writing is True
    assert writing_card_content_gate(one).can_use_for_writing is False
    assert writing_card_content_gate(legacy).can_use_for_writing is False


def test_content_gate_rejects_unrelated_evidence_even_with_safe_pages():
    card = WritingCard(
        research_gap="An invented unsupported catalyst claim changes everything.",
        proposed_solution="A completely unrelated fabricated method solves the problem.",
        evidence_chain=[
            {
                "text": "The source discusses ordinary battery cycling.",
                "source": "Introduction",
                "supports_fields": ["research_gap"],
                "page": 2,
                "locator_status": "exact_page",
            },
            {
                "text": "The source reports standard electrochemical measurements.",
                "source": "Methods",
                "supports_fields": ["proposed_solution"],
                "page": 3,
                "locator_status": "exact_page",
            },
        ],
    )

    result = writing_card_content_gate(card)

    assert result.can_use_for_writing is False
    assert "field_evidence_mismatch:research_gap" in result.blocked_reasons
    assert "field_evidence_mismatch:proposed_solution" in result.blocked_reasons


def test_unlocated_supplemental_evidence_does_not_block_grounded_core_fields():
    card = WritingCard(
        research_gap="A catalytic limitation remains unresolved.",
        proposed_solution="This work develops Fe-N4 catalytic sites.",
        evidence_chain=[
            {
                "text": "A catalytic limitation remains unresolved.",
                "source": "Introduction",
                "supports_fields": ["research_gap"],
                "page": 2,
                "locator_status": "exact_page",
            },
            {
                "text": "This work develops Fe-N4 catalytic sites.",
                "source": "Introduction",
                "supports_fields": ["proposed_solution"],
                "page": 2,
                "locator_status": "exact_page",
            },
            {
                "text": "Supplemental contextual result without an exact page.",
                "source": "Results",
                "supports_fields": [],
                "page": None,
                "locator_status": "text_only",
            },
        ],
    )

    assert writing_card_content_gate(card).can_use_for_writing is True


def test_rule_only_and_llm_failure_keep_parsing_available():
    rule_result = WritingCardExtractor().extract(_document())
    failing = WritingCardExtractor()
    failing.llm = _DummyLLM(error=RuntimeError("provider unavailable"))
    failed_llm_result = failing.extract(_document())

    assert rule_result["research_gap"]
    assert rule_result["proposed_solution"]
    assert failed_llm_result["research_gap"] == rule_result["research_gap"]
    assert failed_llm_result["proposed_solution"] == rule_result["proposed_solution"]
