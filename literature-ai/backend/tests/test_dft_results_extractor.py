from types import SimpleNamespace

from app.extractors.dft_results_extractor import DFTResultListModel, DFTResultsExtractor


def test_dft_results_accepts_dict_input_and_unicode_patterns():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "ΔG = -0.45 eV and Bader charge is 0.42 e−.",
        "sections": [
            SimpleNamespace(
                text="The adsorption energy of Li2S4 is -1.23 eV and E_a ≈ 0.75 eV.",
                section_title="Results",
                page_start=3,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    categories = {item["category"] for item in results}
    assert "gibbs_free_energy_change" in categories
    assert "adsorption_energy" in categories
    assert "reaction_barrier" in categories
    assert any(item["value"] == -0.45 for item in results if item["category"] == "gibbs_free_energy_change")
    assert any(item["value"] == 0.75 for item in results if item["category"] == "reaction_barrier")


def test_dft_results_accepts_raw_section_list_input():
    extractor = DFTResultsExtractor()
    sections = [
        SimpleNamespace(
            text="The adsorption energy of Li2S4 is -1.23 eV.",
            section_title="Results",
            page_start=4,
        )
    ]

    results = extractor.extract(sections)

    assert any(item["category"] == "adsorption_energy" for item in results)


def test_dft_results_merges_rule_output_with_partial_llm_output():
    class DummyLLM:
        def is_configured(self):
            return True

        def structured_extract(self, system_prompt, user_prompt, response_format):
            assert response_format is DFTResultListModel
            return DFTResultListModel.model_validate(
                {
                    "results": [
                        {
                            "category": "charge_transfer",
                            "adsorbate": "Li2S4",
                            "value": 0.42,
                            "unit": "e",
                            "evidence_text": "Charge transfer of 0.42 e was observed for Li2S4.",
                            "source_location": {"section": "Results", "page": 3},
                            "confidence": 0.88,
                        }
                    ]
                }
            )

    extractor = DFTResultsExtractor()
    extractor.llm = DummyLLM()
    document = {
        "markdown": "Results section",
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The adsorption energy of Li2S4 is -1.23 eV.",
                section_title="Results",
                page_start=3,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)
    categories = {item["category"] for item in results}
    assert "adsorption_energy" in categories
    assert "charge_transfer" in categories


def test_dft_results_extracts_structured_markdown_table_values():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1 Adsorption energies",
                markdown_content=(
                    "| Adsorbate | E_ads (eV) | Bader charge |\n"
                    "| --- | --- | --- |\n"
                    "| Li2S4 | -1.23 | 0.42 |\n"
                    "| S8 | -0.88 | 0.15 |\n"
                ),
                page=4,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S4" and item["value"] == -1.23 for item in results)
    assert any(item["category"] == "bader_charge" and item["adsorbate"] == "S8" and item["value"] == 0.15 for item in results)
