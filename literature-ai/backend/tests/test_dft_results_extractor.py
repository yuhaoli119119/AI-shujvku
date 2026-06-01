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


def test_dft_results_preserves_unicode_minus_and_avoids_markdown_duplicates():
    extractor = DFTResultsExtractor()
    sentence = "The adsorption energy of Li2S4 on MXene is −3.997 eV."
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(text=sentence, section_title="Results", page_start=5),
        ],
        "tables": [],
        "figures": [],
        "markdown": sentence,
    }

    results = extractor.extract(document)
    matches = [
        item for item in results
        if item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S4"
    ]

    assert len(matches) == 1
    assert matches[0]["value"] == -3.997


def test_dft_results_extracts_orr_limiting_potential_and_overpotential():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1 ORR limiting potential and overpotential",
                markdown_content="UL is 0.85 V; overpotential η is 0.38 V.",
                page=7,
            ),
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "limiting_potential" and item["value"] == 0.85 for item in results)
    assert any(item["category"] == "overpotential" and item["value"] == 0.38 for item in results)


def test_dft_results_does_not_extract_orr_potentials_from_loose_body_text():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The target Fermi energy is -5.33 eV at U = 0 V vs RHE, while the ORR equilibrium potential is 1.23 V.",
                section_title="Methods",
                page_start=10,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] in {"limiting_potential", "overpotential"} for item in results)


def test_dft_results_extracts_metric_rows_from_orr_table():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1: PDS, limiting potential (UL), and overpotential (η) of ORR",
                markdown_content=(
                    "|  | constant-Ne (vacuum) | constant-μe (ESM-RISM) |\n"
                    "| --- | --- | --- |\n"
                    "|  | Fe-N4-C |  |\n"
                    "| PDS | ∗O → ∗OH (∆G3) | ∗OH → H2O (∆G4) |\n"
                    "| UL | 0.66 V | 0.78 V |\n"
                    "| η | 0.57 V | 0.45 V |\n"
                ),
                page=13,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert sum(1 for item in results if item["category"] == "limiting_potential") == 2
    assert any(item["category"] == "overpotential" and item["value"] == 0.45 for item in results)
    assert any(item["category"] == "potential_determining_step" and item["reaction_step"].endswith("∗O → ∗OH (∆G3)") for item in results)
def test_dft_results_does_not_treat_range_dash_as_negative_sign():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "The T-S site had adsorption energies even to positive values "
                    "(about 0.5-7.1 eV), indicating unstable adsorption."
                ),
                section_title="Results",
                page_start=4,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(
        item["category"] == "adsorption_energy" and item["value"] == -7.1
        for item in results
    )


def test_dft_results_keeps_real_negative_adsorption_energy_with_adsorbate():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The adsorption energy for Li2S6 was -3.802 eV on Nb@VS2.",
                section_title="Results",
                page_start=5,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "adsorption_energy"
        and item["adsorbate"] == "Li2S6"
        and item["value"] == -3.802
        for item in results
    )
