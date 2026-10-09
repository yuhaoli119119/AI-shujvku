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


def test_dft_results_llm_accepts_graphdiyne_material_properties():
    class DummyLLM:
        def is_configured(self):
            return True

        def structured_extract(self, system_prompt, user_prompt, response_format):
            assert response_format is DFTResultListModel
            assert "graphdiyne/graphyne" in system_prompt
            assert "cohesive_energy" in system_prompt
            assert "figure-only content" in system_prompt
            return DFTResultListModel.model_validate(
                {
                    "results": [
                        {
                            "category": "cohesive_energy",
                            "adsorbate": "alpha-GDY",
                            "value": -8.19,
                            "unit": "eV/atom",
                            "evidence_text": "The cohesive energy of alpha-GDY is -8.19 eV/atom.",
                            "source_location": {"section": "Results"},
                            "confidence": 0.9,
                        },
                        {
                            "category": "lattice_constant",
                            "adsorbate": "HsGDY-AA",
                            "value": 16.63,
                            "unit": "Å",
                            "evidence_text": "The optimized lattice parameter for HsGDY-AA is a = b = 16.63 Å.",
                            "source_location": {"section": "Results"},
                            "confidence": 0.9,
                        },
                    ]
                }
            )

    extractor = DFTResultsExtractor()
    extractor.llm = DummyLLM()
    document = {
        "markdown": "Graphdiyne results",
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The cohesive energy of alpha-GDY is -8.19 eV/atom.",
                section_title="Computational results",
                page_start=3,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "cohesive_energy" and item["value"] == -8.19 for item in results)
    assert any(item["category"] == "lattice_constant" and item["value"] == 16.63 for item in results)


def test_dft_results_llm_focus_text_excludes_figures():
    class CapturingLLM:
        prompt = ""

        def is_configured(self):
            return True

        def structured_extract(self, system_prompt, user_prompt, response_format):
            self.prompt = user_prompt
            return DFTResultListModel.model_validate({"results": []})

    llm = CapturingLLM()
    extractor = DFTResultsExtractor()
    extractor.llm = llm
    document = {
        "markdown": "Graphdiyne results",
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The band gap of graphdiyne is discussed in the text.",
                section_title="Electronic results",
                page_start=3,
            )
        ],
        "tables": [],
        "figures": [
            SimpleNamespace(caption="Figure-only icon caption with band gap 9.99 eV.", page=4)
        ],
    }

    extractor.extract(document)

    assert "The band gap of graphdiyne is discussed" in llm.prompt
    assert "Figure-only icon caption" not in llm.prompt


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


def test_dft_results_preserves_catalyst_identity_from_comparison_table():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1 Li2S adsorption on catalyst models",
                markdown_content=(
                    "| Catalyst | Li2S adsorption energy (eV) |\n"
                    "| --- | --- |\n"
                    "| Fe-N4/C | -1.20 |\n"
                    "| Co-N4/C | -1.20 |\n"
                ),
                page=4,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)
    adsorption = [item for item in results if item["category"] == "adsorption_energy"]

    assert {item["catalyst_name"] for item in adsorption} == {"Fe-N4/C", "Co-N4/C"}
    assert len(adsorption) == 2


def test_dft_results_rejects_band_gap_candidates_that_are_dimension_rows():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1 Electronic properties",
                markdown_content=(
                    "| Structure | Band gap (eV) |\n"
                    "| --- | --- |\n"
                    "| Pore diameter changed | 10.286 |\n"
                    "| Lattice constant | 15.542 |\n"
                ),
                page=8,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] == "band_gap" for item in results)


def test_dft_results_does_not_use_sentence_like_first_column_as_adsorbate():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 2 Adsorption energies",
                markdown_content=(
                    "| Sample | E_ads (eV) |\n"
                    "| --- | --- |\n"
                    "| Structure changed after adsorption | -1.23 |\n"
                ),
                page=9,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(item["category"] == "adsorption_energy" for item in results)


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
    assert not any(item["category"] == "potential_determining_step" for item in results)


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


def test_lis_sulfur_eads_is_adsorption_energy_not_limiting_potential():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The Eads of Sulfur on Fe-N4/C is -1.34 eV.",
                section_title="DFT results",
                page_start=6,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "adsorption_energy" and item["adsorbate"] == "S_atom" and item["value"] == -1.34
        for item in results
    )
    assert not any(item["category"] == "limiting_potential" for item in results)


def test_lis_adsorbate_sulfur_forms_are_normalized_without_figure_s8_pollution():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "Eads of Li2S is -2.10 eV. "
                    "E_ads of S8 molecule is -0.80 eV. "
                    "Figure S8 shows the optimized structure but gives no numeric adsorption value."
                ),
                section_title="DFT results",
                page_start=7,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S" for item in results)
    assert any(item["category"] == "adsorption_energy" and item["adsorbate"] == "S8" for item in results)

    figure_only = extractor.extract(
        {
            "abstract": "",
            "sections": [
                SimpleNamespace(
                    text="Figure S8 shows the optimized adsorption configuration with an energy of -0.20 eV.",
                    section_title="Supporting figures",
                    page_start=12,
                )
            ],
            "tables": [],
            "figures": [],
        }
    )
    assert not any(item["adsorbate"] == "S8" for item in figure_only)


def test_lis_eb_es_allow_null_adsorbate_and_binding_energy_stays_generic_when_unclear():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "The Eb of the Fe atom on the N-doped carbon support is -5.20 eV. "
                    "The stability parameter Es is -3.10 eV. "
                    "The binding energy is -0.60 eV in the optimized model."
                ),
                section_title="Stability",
                page_start=8,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "metal_support_binding_energy_Eb" and item["adsorbate"] is None
        for item in results
    )
    assert any(
        item["category"] == "stability_parameter_Es" and item["adsorbate"] is None
        for item in results
    )
    assert any(item["category"] == "binding_energy" and item["adsorbate"] is None for item in results)
    assert not any(
        item["category"] == "adsorption_energy" and item["adsorbate"] in {None, "H2"}
        for item in results
    )


def test_lis_binding_energy_of_li2s_is_adsorption_energy():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text="The binding energy of Li2S on the catalyst is -1.95 eV.",
                section_title="DFT results",
                page_start=8,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S" and item["value"] == -1.95
        for item in results
    )
    assert not any(item["category"] == "binding_energy" for item in results)


def test_lis_binding_energy_mixed_paragraph_does_not_cross_sentence_adsorbate():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "The binding energy of Li2S on the catalyst is -1.95 eV. "
                    "The Eb of the Fe atom on the N-doped carbon support is -5.20 eV. "
                    "The binding energy is -0.60 eV in the optimized model."
                ),
                section_title="DFT results",
                page_start=8,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert any(
        item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S" and item["value"] == -1.95
        for item in results
    )
    assert any(
        item["category"] == "binding_energy" and item["adsorbate"] is None and item["value"] == -0.60
        for item in results
    )
    assert any(
        item["category"] == "metal_support_binding_energy_Eb" and item["adsorbate"] is None and item["value"] == -5.20
        for item in results
    )
    assert not any(
        item["category"] == "adsorption_energy" and item["adsorbate"] == "Li2S" and item["value"] == -0.60
        for item in results
    )


def test_lis_bond_lengths_and_electronic_descriptors_extract_from_table_cells():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 2 Li-S DAC DFT descriptors",
                markdown_content=(
                    "| Site | Li-S bond length (A) | S-S bond length (A) | M-N bond length (A) | M-S bond length (A) | M-M bond length (A) | Lowdin charge (e) | ICOHP (eV) | d orbital occupancy | DOS at Fermi |\n"
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                    "| FeCo-N6 | 2.31 | 2.05 | 1.92 | 2.18 | 2.43 | 0.36 | -1.42 | 6.2 | 1.8 |\n"
                ),
                page=9,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)
    categories = {item["category"] for item in results}

    assert {"bond_length_Li-S", "bond_length_S-S", "bond_length_M-N", "bond_length_M-S", "bond_length_M-M"} <= categories
    assert {"Lowdin_charge", "ICOHP", "d_orbital_occupancy", "DOS_at_Fermi"} <= categories
    assert any(item["atom_pair"] == "Li-S" and item["source_row_index"] == 0 for item in results)
    checked_categories = {
        "bond_length_Li-S",
        "bond_length_S-S",
        "bond_length_M-N",
        "bond_length_M-S",
        "bond_length_M-M",
        "Lowdin_charge",
        "ICOHP",
        "DOS_at_Fermi",
    }
    assert all(item["adsorbate"] is None for item in results if item["category"] in checked_categories)


def test_lis_eads_matrix_headers_extract_adsorbates_from_columns():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 1 Adsorption matrix",
                markdown_content=(
                    "| System | Eads of Sulfur | Eads of Li2S | Eads of S8 |\n"
                    "| --- | --- | --- | --- |\n"
                    "| FeCo-N6 | -1.10 | -2.20 | -0.70 |\n"
                ),
                page=4,
            )
        ],
        "figures": [],
    }

    results = [
        item for item in extractor.extract(document)
        if item["category"] == "adsorption_energy"
    ]

    assert len(results) == 3
    assert {(item["adsorbate"], item["value"]) for item in results} == {
        ("S_atom", -1.10),
        ("Li2S", -2.20),
        ("S8", -0.70),
    }


def test_lis_descriptor_table_ignores_see_fig_s8_reference_numbers():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                caption="Table 3 Electronic descriptors",
                markdown_content=(
                    "| Site | ICOHP (eV) | DOS at Fermi (states/eV) |\n"
                    "| --- | --- | --- |\n"
                    "| FeCo-N6 | see Fig. S8 | 1.70; see Fig. S8 |\n"
                ),
                page=10,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(
        item["category"] in {"ICOHP", "DOS_at_Fermi"} and item["value"] == 8
        for item in results
    )
    assert any(item["category"] == "DOS_at_Fermi" and item["value"] == 1.70 for item in results)
    assert all(
        item["adsorbate"] is None
        for item in results
        if item["category"] in {"ICOHP", "DOS_at_Fermi"}
    )


def test_lis_s0101_table_s1_matrix_extracts_scalar_dft_candidates():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-table-s1",
                caption=(
                    "Table S1. Calculated 𝐸𝑏, 𝐸𝑠, average bond length of M-N (𝑑𝑀-𝑁), "
                    "bond length of M-S (𝑑𝑀-𝑆) and adsorption energy (Eads) of Li2S "
                    "and sulfur on the different M-N1-4/G systems."
                ),
                markdown_content=(
                    "| Structures | 𝑬 𝒃 (eV) | 𝑬 𝒔 (eV) | Average 𝒅 𝑴-𝑵 (Å) | 𝒅 𝑴-𝑺 (Å) | 𝐄 𝐚𝐝𝐬 of 𝐋𝐢 𝟐 𝐒 (eV) | 𝐄 𝐚𝐝𝐬 of Sulfur (eV) |\n"
                    "| --- | --- | --- | --- | --- | --- | --- |\n"
                    "| 𝑺𝒄-𝑵 𝟑 /𝑮 | -7.360 | -2.854 | 2.031 | 2.447 | -5.949 | -4.886 |\n"
                    "| 𝑺𝒄-𝑵 𝟒 /𝑮 | -7.419 | -2.913 | 1.990 | 2.411 | -5.186 | -5.249 |\n"
                ),
                page=8,
            )
        ],
        "figures": [],
    }

    results = extractor.extract(document)

    expected = {
        ("metal_support_binding_energy_Eb", None, -7.360, None),
        ("stability_parameter_Es", None, -2.854, None),
        ("bond_length_M-N", None, 2.031, "M-N"),
        ("bond_length_M-S", None, 2.447, "M-S"),
        ("adsorption_energy", "Li2S", -5.949, None),
        ("adsorption_energy", "S_atom", -4.886, None),
    }
    for category, adsorbate, value, atom_pair in expected:
        assert any(
            item["category"] == category
            and item["adsorbate"] == adsorbate
            and item["value"] == value
            and (atom_pair is None or item["atom_pair"] == atom_pair)
            for item in results
        )

    table_results = [item for item in results if item["source_table_id"] == "s0101-table-s1"]
    assert len(table_results) == 12
    for item in table_results:
        assert item["fact_family"]
        assert item["site_label"]
        assert item["state_context"]
        assert item["active_site_instance_key"]
        assert item["source_table_caption"].startswith("Table S1.")
        assert item["source_row_index"] is not None
        assert item["source_column_index"] is not None
        assert item["raw_row_text"]
        assert item["raw_column_header"]


def test_lis_s0101_table_s5_reaction_free_energy_extracts_scalar_candidates():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-table-s5",
                caption="Table S5. Free energy difference (ΔG) of Li2S2 to Li2S conversion for selected SMSCs.",
                markdown_content=(
                    "| SMSC | Reaction step | ΔG (eV) | RDS |\n"
                    "| --- | --- | --- | --- |\n"
                    "| FeN/G | Li2S2 -> Li2S | 4.76 | yes |\n"
                    "| CoN/G | Li2S2 -> Li2S | 4.98 | yes |\n"
                ),
                page=12,
            )
        ],
        "figures": [],
    }

    results = [item for item in extractor.extract(document) if item["source_table_id"] == "s0101-table-s5"]

    assert [(item["category"], item["value"]) for item in results] == [
        ("gibbs_free_energy_change", 4.76),
        ("gibbs_free_energy_change", 4.98),
    ]
    assert all(item["fact_family"] == "reaction_free_energy_table" for item in results)
    assert all(item["reaction_step"] == "Li2S2 -> Li2S" for item in results)
    assert all(item["source_row_index"] is not None and item["source_column_index"] == 2 for item in results)


def test_lis_s0101_reaction_barrier_table_extracts_deposition_and_diffusion_barriers():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-barrier-table",
                caption="Supplementary reaction barrier table for Li2S decomposition/deposition and Li diffusion.",
                markdown_content=(
                    "| SMSC | Li2S decomposition barrier (eV) | Li2S deposition barrier (eV) | Li diffusion barrier (eV) |\n"
                    "| --- | --- | --- | --- |\n"
                    "| FeN/G | 0.64 | 0.42 | 0.21 |\n"
                ),
                page=13,
            )
        ],
        "figures": [],
    }

    results = [item for item in extractor.extract(document) if item["source_table_id"] == "s0101-barrier-table"]

    assert {(item["category"], item["value"]) for item in results} == {
        ("li2s_decomposition_barrier", 0.64),
        ("li2s_deposition_barrier", 0.42),
        ("migration_barrier", 0.21),
    }
    assert all(item["fact_family"] == "reaction_barrier_table" for item in results)
    assert all(item["adsorbate"] is None for item in results)


def test_lis_s0101_s2_s3_electronic_descriptor_table_extracts_scalar_candidates():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-table-s2-s3",
                caption="Table S2/S3. Lowdin charge, d-band center, DOS/PDOS, ICOHP/COHP and magnetic moment descriptors.",
                markdown_content=(
                    "| SMSC | Lowdin charge of TM (e) | Bader charge (e) | charge transfer (e) | d-band center (eV) | DOS@EF | ICOHP (eV) | COHP (eV) | magnetic moment (μB) |\n"
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                    "| FeN/G | 0.31 | 0.42 | 0.18 | -1.22 | 1.70 | -1.43 | -0.62 | 2.10 |\n"
                ),
                page=10,
            )
        ],
        "figures": [],
    }

    results = [item for item in extractor.extract(document) if item["source_table_id"] == "s0101-table-s2-s3"]

    assert {
        "Lowdin_charge",
        "bader_charge",
        "charge_transfer",
        "d_band_center",
        "DOS_at_Fermi",
        "ICOHP",
        "COHP",
        "magnetic_moment",
    } <= {item["category"] for item in results}
    assert all(item["fact_family"] == "electronic_descriptor_table" for item in results)
    assert all(item["adsorbate"] is None for item in results)
    assert all(item["active_site_instance_key"] == "FeN/G" for item in results)


def test_lis_s0101_s6_ml_descriptor_table_marks_ml_descriptor_family():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-table-s6",
                caption="Table S6. Machine learning input features and descriptors for selected SMSCs.",
                markdown_content=(
                    "| SMSC | Es (eV) | L_TM-N/C (A) | En_sum | εd (eV) | Lowdin charge of TM (Δρ) | DOS@EF | Pun | ρ(dxz + dyz) | ρ(dz2) |\n"
                    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                    "| FeN/G | -1.02 | 1.91 | 4.98 | -1.22 | 0.31 | 1.70 | 0.24 | 1.82 | 0.77 |\n"
                ),
                page=14,
            )
        ],
        "figures": [],
    }

    results = [item for item in extractor.extract(document) if item["source_table_id"] == "s0101-table-s6"]

    assert {
        "stability_parameter_Es",
        "electronegativity_sum",
        "d_band_center",
        "Lowdin_charge",
        "DOS_at_Fermi",
        "unoccupied_d_state_fraction",
        "orbital_occupancy_dxz_dyz",
        "orbital_occupancy_dz2",
    } <= {item["category"] for item in results}
    assert all(item["fact_family"] == "ml_descriptor" for item in results)
    assert not any(item["category"] == "adsorption_energy" for item in results)


def test_lis_s0101_calculation_settings_table_keeps_settings_as_structured_payload():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [],
        "tables": [
            SimpleNamespace(
                id="s0101-calculation-settings",
                caption="Calculation settings used for the DFT simulations.",
                markdown_content=(
                    "| Setting | Value |\n"
                    "| --- | --- |\n"
                    "| functional | PBE |\n"
                    "| cutoff energy | 500 eV |\n"
                    "| k-point mesh | 5 x 5 x 1 |\n"
                    "| supercell | 5 x 5 x 1 graphene |\n"
                    "| vacuum space | 15 A |\n"
                    "| force convergence | 0.001 eV/A |\n"
                ),
                page=3,
            )
        ],
        "figures": [],
    }

    results = [item for item in extractor.extract(document) if item["source_table_id"] == "s0101-calculation-settings"]

    assert {
        "functional",
        "cutoff_energy",
        "k_points",
        "supercell",
        "vacuum_thickness",
        "convergence_force",
    } <= {item["category"] for item in results}
    assert all(item["fact_family"] == "calculation_settings" for item in results)
    assert all(item["adsorbate"] is None for item in results)
    assert any(item["category"] == "functional" and item["value"] is None for item in results)
    assert any(item["category"] == "cutoff_energy" and item["value"] == 500 and item["unit"] == "eV" for item in results)
    assert not any(item["category"] in {"adsorption_energy", "metal_support_binding_energy_Eb"} for item in results)


def test_lis_delithiation_energy_e1_does_not_pollute_adsorption_or_eb():
    extractor = DFTResultsExtractor()
    document = {
        "abstract": "",
        "sections": [
            SimpleNamespace(
                text=(
                    "When the binding energy of a single sulfur atom on an SMSC exceeds "
                    "the delithiation energy, E1 (4.53 eV), the charging process is "
                    "insufficient to remove the sulfur atom."
                ),
                section_title="Reduced sulfur poisoning on SMSCs",
                page_start=6,
            )
        ],
        "tables": [],
        "figures": [],
    }

    results = extractor.extract(document)

    assert not any(
        item["category"] in {"adsorption_energy", "metal_support_binding_energy_Eb"}
        and item["value"] == 4.53
        for item in results
    )
