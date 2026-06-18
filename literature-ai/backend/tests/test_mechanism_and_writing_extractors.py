from types import SimpleNamespace

from app.extractors.mechanism_extractor import MechanismExtractor
from app.extractors.writing_card_extractor import WritingCardExtractor, WritingCardModel


def test_mechanism_extractor_accepts_raw_section_list_input():
    extractor = MechanismExtractor()
    sections = [
        SimpleNamespace(
            text="The Fe-N4 site can effectively adsorb polysulfides and suppress shuttle effect.",
            section_title="Results",
            page_start=5,
        )
    ]

    results = extractor.extract(sections)

    assert results
    assert any(item["mechanism_type"] == "polysulfide_adsorption" for item in results)


def test_mechanism_extractor_skips_weak_descriptive_hits_without_action_signal():
    extractor = MechanismExtractor()
    sections = [
        SimpleNamespace(
            text="Polysulfides on Fe-N4 were shown in Figure 3 and the shuttle effect is discussed in the text.",
            section_title="Results",
            page_start=5,
        )
    ]

    results = extractor.extract(sections)

    assert results == []


def test_writing_card_extractor_accepts_dict_input_and_builds_evidence_chain():
    extractor = WritingCardExtractor()
    document = {
        "abstract": "However, sluggish conversion remains a challenge. In this work, we propose Fe-N4 sites.",
        "sections": [
            SimpleNamespace(
                text="Lithium sulfur batteries are promising. However, sluggish conversion remains a challenge.",
                section_title="Introduction",
                page_start=1,
            ),
            SimpleNamespace(
                text="The catalyst shows adsorption energy of -1.2 eV and excellent cycling stability.",
                section_title="Results",
                page_start=3,
            ),
        ],
        "tables": [],
        "figures": [],
    }

    result = extractor.extract(document)

    assert result["paper_type"] in {"computational", "experimental", "mixed", "unknown"}
    assert result["research_gap"]
    assert result["evidence_chain"]


def test_writing_card_extractor_merges_llm_and_rule_outputs():
    class DummyLLM:
        def is_configured(self):
            return True

        def structured_extract(self, system_prompt, user_prompt, response_format):
            assert response_format is WritingCardModel
            return WritingCardModel.model_validate(
                {
                    "paper_type": "mixed",
                    "research_gap": "现有工作仍缺少对多硫化物转化动力学的系统解释。",
                    "proposed_solution": "提出 Fe-N4 活性位点来稳定中间体。",
                    "core_hypothesis": "Fe-N4 位点能够同时增强吸附与转化。",
                    "evidence_chain": [
                        {"text": "Charge transfer of 0.42 e was observed.", "source": "Results"}
                    ],
                    "section_strategy": {
                        "Introduction": {
                            "purpose": "提出问题与研究缺口",
                            "key_moves": ["背景", "挑战", "方案预告"],
                            "typical_length_hint": "~20%",
                        }
                    },
                    "figure_logic": [
                        {"fig_id": "Figure 1", "purpose": "conceptual_schematic", "supports_claim": "Overall design"}
                    ],
                    "abstract_logic": "背景-问题-方案-结果-意义",
                    "introduction_logic": "背景到缺口再到本文方案",
                    "discussion_logic": "结果到机理再到意义",
                }
            )

    extractor = WritingCardExtractor()
    extractor.llm = DummyLLM()
    document = {
        "markdown": "Results section",
        "abstract": "However, sluggish conversion remains a challenge. In this work, we propose Fe-N4 sites.",
        "sections": [
            SimpleNamespace(
                text="Lithium sulfur batteries are promising. However, sluggish conversion remains a challenge.",
                section_title="Introduction",
                page_start=1,
            ),
            SimpleNamespace(
                text="The catalyst shows adsorption energy of -1.2 eV and excellent cycling stability.",
                section_title="Results",
                page_start=3,
            ),
        ],
        "tables": [],
        "figures": [],
    }

    result = extractor.extract(document)

    assert result["paper_type"] == "mixed"
    # Unsupported LLM prose is rejected and the grounded rule value is retained.
    assert result["research_gap"] == "However, sluggish conversion remains a challenge."
    assert result["core_hypothesis"] == ""
    assert any("research_gap" in item.get("supports_fields", []) for item in result["evidence_chain"])
    assert result["evidence_chain"]
    assert any("adsorption energy" in item["text"].lower() for item in result["evidence_chain"])
