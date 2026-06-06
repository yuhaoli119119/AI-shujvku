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


def test_dft_results_skips_reference_table_artifacts_for_limiting_potential():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="References",
                markdown_content=(
                    "| Ref. | U_L | Notes |\n"
                    "| --- | --- | --- |\n"
                    "| [22] | 436 e | journal reference artifact |\n"
                    "| [23] | 20 e | another reference artifact |\n"
                ),
                page=15,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] == "limiting_potential" for item in results)


def test_dft_results_keeps_non_numeric_electronic_claims_out_of_numeric_dft_table():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The DOS near the Fermi level is increased and the charge density difference indicates redistribution.",
                section_title="Electronic structure",
                page_start=6,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] in {"dos_claim", "charge_density_difference_claim"} for item in results)


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


def test_dft_results_extracts_graphene_defect_formation_and_migration_values():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "DFT calculations show that the formation energy of a single vacancy is 7.57 eV. "
                    "The Stone-Wales defect formation energy is 4.80 eV. "
                    "The migration barrier of the single vacancy is 1.30 eV in graphene."
                ),
                section_title="Defect energetics",
                page_start=4,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "formation_energy" and item["value"] == 7.57 for item in results)
    assert any(item["category"] == "formation_energy" and item["adsorbate"] == "Stone-Wales" and item["value"] == 4.80 for item in results)
    assert any(item["category"] == "migration_barrier" and item["value"] == 1.30 for item in results)


def test_dft_results_keeps_stone_wales_formation_takes_value():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The SW defect is also called the 5775 defect, and its formation takes approximately 5 eV.",
                section_title="Introduction",
                page_start=2,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "formation_energy" and item["value"] == 5.0 for item in results)


def test_dft_results_skips_formation_energy_error_scale_and_method_values():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "DFT methods underestimate monovacancy formation energies by around 1 eV. "
                    "The standard deviations of the DMC pure MV formation energies as functions of twist are 0.3 eV, 0.2 eV, and 0.1 eV. "
                    "A cutoff energy of 305 eV was used for defective graphene calculations. "
                    "The geometry was optimized to a force tolerance of 0.0025 eV A-1."
                ),
                section_title="Methods",
                page_start=5,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] == "formation_energy" for item in results)


def test_dft_results_band_gap_requires_explicit_gap_label():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "Evaluating defect formation energies in 2D materials can be non-negligible on a 0.5-1 eV energy scale. "
                    "The SWD-3x3 superlattice displays a clear gap of E g = 0.30 eV."
                ),
                section_title="Electronic structure",
                page_start=6,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    band_gaps = [item for item in results if item["category"] == "band_gap"]
    assert len(band_gaps) == 1
    assert band_gaps[0]["value"] == 0.30


def test_dft_results_extracts_graphene_defect_table_values():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 2 Formation energies and migration barriers for graphene defects",
                markdown_content=(
                    "| Defect | Formation energy (eV) | Migration barrier (eV) | Band gap (eV) |\n"
                    "| --- | --- | --- | --- |\n"
                    "| single vacancy | 7.57 | 1.30 | 0.12 |\n"
                    "| Stone-Wales | 4.80 | 10.0 | 0.05 |\n"
                ),
                page=6,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "formation_energy" and item["adsorbate"] == "single_vacancy" and item["value"] == 7.57 for item in results)
    assert any(item["category"] == "migration_barrier" and item["adsorbate"] == "Stone-Wales" and item["value"] == 10.0 for item in results)
    assert any(item["category"] == "band_gap" and item["value"] == 0.05 for item in results)


def test_dft_results_table_category_scan_uses_local_quality_evidence():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1. The defect formation energy takes 2.30 eV for single vacancy graphene.",
                markdown_content="",
                page=4,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "formation_energy"
        and item["adsorbate"] == "single_vacancy"
        and item["value"] == 2.30
        for item in results
    )


def test_dft_results_extracts_graphene_defect_inline_plain_text_table_values():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "TABLE II. Theoretical static-nucleus formation energies for various point defects in monolayer graphene. "
                    "Method Defect formation energy (eV) MV SiS SW "
                    "DMC-corrected DFT 9 .0(1) 4 .4(1) 4 .9(1) "
                    "The vibrationally corrected DMC defect formation energies are 8.3(1), 3.6(1), and 4.4(1) at 298 K for MV, SiS, and SW defects, respectively."
                ),
                section_title="Results",
                page_start=8,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "formation_energy"
        and item["adsorbate"] == "single_vacancy"
        and item["value"] == 9.0
        and item["reaction_step"] == "DMC-corrected DFT MV"
        for item in results
    )
    assert any(
        item["category"] == "formation_energy"
        and item["adsorbate"] == "Stone-Wales"
        and item["value"] == 4.4
        and item["reaction_step"] == "vibrationally corrected DMC at 298 K SW"
        for item in results
    )


def test_dft_results_extracts_adsorption_on_defective_graphene():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The adsorption energy of atomic hydrogen on single-vacancy graphene is -1.86 eV.",
                section_title="Adsorption",
                page_start=5,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "adsorption_energy"
        and item["adsorbate"] in {"H", "single_vacancy"}
        and item["value"] == -1.86
        for item in results
    )


def test_dft_results_skips_graphene_reference_table_artifacts():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="References",
                markdown_content=(
                    "| Ref. | Title | Year |\n"
                    "| --- | --- | --- |\n"
                    "| [22] | Defect formation energy in graphene, Journal of Carbon | 1998 |\n"
                    "| [23] | Vacancy migration barrier in graphite, DOI 10.1000/test | 2003 |\n"
                ),
                page=14,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] in {"formation_energy", "migration_barrier"} for item in results)
