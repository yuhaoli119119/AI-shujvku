from app.domain.reaction_taxonomy import (
    PROFILE_VERSION,
    classify_reaction_record,
    get_reaction_profile,
    normalize_intermediate,
    normalize_property_type,
    normalize_reaction_type,
    validate_reaction_record,
)


def test_srr_li2s6_adsorption_energy_is_valid():
    result = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "Li2S6", "property_type": "adsorption energy", "unit": "eV"}
    )
    assert result["valid"] is True
    assert result["intermediate"] == "Li2S6"
    assert result["property_type"] == "adsorption_energy"
    assert result["canonical_unit"] == "eV"


def test_srr_li2s_decomposition_barrier_is_normalized_and_valid():
    result = validate_reaction_record(
        "lithium sulfur",
        {"intermediate": "lithium sulfide", "property": "decomposition barrier of Li2S"},
    )
    assert result["valid"] is True
    assert result["intermediate"] == "Li2S"
    assert result["property_type"] == "li2s_decomposition_barrier"


def test_srr_li2s_dissociation_and_deposition_are_distinct():
    dissociation = validate_reaction_record(
        "SRR_LiS",
        {"intermediate": "Li2S", "property": "Li2S dissociation energy"},
    )
    deposition = validate_reaction_record(
        "SRR_LiS",
        {"intermediate": "Li2S", "property": "Li2S deposition barrier"},
    )

    assert dissociation["valid"] is True
    assert dissociation["property_type"] == "li2s_dissociation_energy"
    assert deposition["valid"] is True
    assert deposition["property_type"] == "li2s_deposition_barrier"


def test_srr_rejects_intermediates_that_are_clearly_from_other_profiles():
    for intermediate, property_type in [
        ("ΔG_H*", "ΔG_H*"),
        ("*OOH", "gibbs free energy change"),
        ("*COOH", "adsorption energy"),
    ]:
        result = validate_reaction_record(
            "SRR_LiS", {"intermediate": intermediate, "property_type": property_type}
        )
        assert result["valid"] is False
        assert result["status"] == "out_of_scope"


def test_experimental_profiles_normalize_expected_intermediates():
    assert normalize_intermediate("HER", "ΔG_H*") == "*H"
    assert normalize_property_type("HER", "ΔG_H*") == "gibbs_free_energy_change"
    assert [normalize_intermediate("OER", value) for value in ("OH*", "*O", "OOH*")] == [
        "*OH", "*O", "*OOH"
    ]
    assert normalize_intermediate("CO2RR", "COOH*") == "*COOH"


def test_srr_rds_gibbs_free_energy_stays_free_energy_change():
    result = validate_reaction_record(
        "SRR_LiS",
        {"adsorbate": "Li2S4", "property_type": "RDS Gibbs free energy", "reaction_step": "RDS"},
    )
    assert result["valid"] is True
    assert result["property_type"] == "gibbs_free_energy_change"
    assert result["canonical_unit"] == "eV"


def test_classification_does_not_guess_from_shared_intermediate():
    result = classify_reaction_record(
        {"adsorbate": "*OOH", "property_type": "gibbs free energy change"}
    )
    assert result["reaction_type"] == "UNKNOWN"
    assert result["status"] == "ambiguous"


def test_classification_uses_context_and_srr_specific_signals():
    assert classify_reaction_record(
        {"adsorbate": "*COOH", "property_type": "adsorption energy"},
        "CO2 reduction reaction pathway",
    )["reaction_type"] == "CO2RR"
    assert classify_reaction_record(
        {"adsorbate": "Li2S6", "property_type": "adsorption energy"}
    )["reaction_type"] == "SRR_LiS"


def test_plain_s8_without_lithium_sulfur_context_is_not_srr_specific():
    result = classify_reaction_record(
        {
            "adsorbate": "S8",
            "property_type": "adsorption_energy",
            "evidence_text": "The CO2 reduction pathway is shown in Figure S8.",
        }
    )
    assert result["reaction_type"] != "SRR_LiS"
    assert result["reaction_type"] == "CO2RR"
    validation = validate_reaction_record(result["reaction_type"], {"adsorbate": "S8", "property_type": "adsorption_energy"})
    assert validation["valid"] is False
    assert "intermediate_out_of_scope" in validation["reasons"]

    contextual = classify_reaction_record(
        {"adsorbate": "S8", "property_type": "adsorption_energy"},
        "Lithium-sulfur polysulfide conversion",
    )
    assert contextual["reaction_type"] == "SRR_LiS"


def test_reaction_abbreviations_require_safe_boundaries():
    for evidence_text in (
        "The other catalyst shows a stable surface.",
        "The location where adsorption occurs is shown.",
        "The thermal stability was evaluated.",
    ):
        result = classify_reaction_record({"evidence_text": evidence_text})
        assert result["reaction_type"] == "UNKNOWN"
        assert result["status"] == "ambiguous"

    assert classify_reaction_record({"evidence_text": "HER activity was measured."})[
        "reaction_type"
    ] == "HER"
    assert classify_reaction_record(
        {"evidence_text": "The oxygen evolution reaction was evaluated."}
    )["reaction_type"] == "OER"


def test_material_level_srr_descriptors_do_not_require_intermediate():
    for property_type in ("d-band center", "bader charge", "charge transfer"):
        result = validate_reaction_record("SRR_LiS", {"property_type": property_type})
        assert result["valid"] is True
        assert result["reasons"] == []


def test_srr_adsorption_energy_still_requires_intermediate():
    result = validate_reaction_record("SRR_LiS", {"property_type": "adsorption energy"})
    assert result["valid"] is False
    assert "missing_intermediate" in result["reasons"]


def test_binding_energy_keeps_an_independent_canonical_meaning():
    assert normalize_property_type("SRR_LiS", "binding energy") == "binding_energy"
    profile = get_reaction_profile("SRR_LiS")
    assert "binding_energy" in profile.allowed_properties
    assert profile.canonical_units["binding_energy"] == "eV"


def test_profile_statuses_and_version_are_stable():
    assert get_reaction_profile("SRR_LiS").status == "production"
    for key in ("HER", "OER", "ORR", "CO2RR"):
        profile = get_reaction_profile(key)
        assert profile.status == "experimental"
        assert profile.version == PROFILE_VERSION == "reaction_profiles_v1"
    assert get_reaction_profile("UNKNOWN").status == "quarantine"
    assert normalize_reaction_type("not enough context") == "UNKNOWN"
