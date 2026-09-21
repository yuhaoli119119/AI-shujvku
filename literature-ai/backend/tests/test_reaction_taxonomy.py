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


def test_unicode_dashes_in_li_s_labels_normalize_to_sulfur_reduction():
    """ACS PDFs typeset ``Li-S`` with U+2212/U+2013/U+2014 minus/dash glyphs.

    Regression for the B0091 platform defect: ``_clean`` only handled ASCII
    hyphens, so the printed ``Li−S`` label matched no profile and the record was
    quarantined as UNKNOWN.
    """

    for label in ["Li\u2212S", "Li\u2013S", "Li\u2014S", "Li\u2010S", "Li\u2011S"]:
        assert normalize_reaction_type(label) == "SRR_LiS", label
    assert normalize_reaction_type("lithium\u2212sulfur") == "SRR_LiS"
    classified = classify_reaction_record(
        {"reaction_step": "discharge step 1"},
        paper_context="Li\u2212S battery sulfur reduction",
    )
    assert classified["reaction_type"] == "SRR_LiS"
    assert classified["reason"] == "reaction_context"


def test_bare_s_adsorbate_resolves_to_atomic_sulfur_and_not_s8():
    """ACS Li-S tables store the atomic sulfur adsorbate as bare ``S``.

    Regression for the B0091 platform defect: the SRR alias table only knew
    ``sulfur``/``s atom``/``s_atom``, so the stored ``S`` resolved to no
    intermediate and every single-S-atom binding energy was rejected as
    ``out_of_scope``.  ``S8`` must keep resolving to the ring molecule.
    """

    assert normalize_intermediate("SRR_LiS", "S") == "S_atom"
    assert normalize_intermediate("SRR_LiS", "s8") == "S8"
    assert normalize_intermediate("SRR_LiS", "s atom") == "S_atom"

    s_binding = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "S", "property_type": "binding energy", "unit": "eV"}
    )
    assert s_binding["valid"] is True
    assert s_binding["intermediate"] == "S_atom"
    assert s_binding["property_type"] == "binding_energy"


def test_srr_accepts_formation_energy_and_bond_length_observations():
    """User-approved scope (2026-09-22): SAC formation energies and M-S bond
    lengths count as sulfur-reduction observations.

    Regression for the B0091 platform defect: the SRR task profiles already
    allowed ``formation_energy`` (active_site_stability) and bond lengths
    (structure_bond_lengths), but the reaction profile did not, so those records
    could never receive a reaction attribution.
    """

    assert normalize_property_type("SRR_LiS", "formation_energy") == "formation_energy"
    assert normalize_property_type("SRR_LiS", "formation energy") == "formation_energy"
    assert normalize_property_type("SRR_LiS", "bond_length") == "bond_length"
    assert normalize_property_type("SRR_LiS", "bond length") == "bond_length"

    formation = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "S", "property_type": "formation energy", "unit": "eV"}
    )
    assert formation["valid"] is True
    assert formation["property_type"] == "formation_energy"

    bond_length = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "S", "property_type": "bond length", "unit": "\u00c5"}
    )
    assert bond_length["valid"] is True
    assert bond_length["property_type"] == "bond_length"


def test_metal_formation_energy_is_not_judged_on_the_adsorbate_column():
    """Regression for the B0091 defect D.

    Table 1 of the ACS Li-S paper stores the catalyst metal (Li/Sc/Ti/V/Cr/Mn/
    Fe/Co) in the ``adsorbate`` column of its formation-energy rows.
    ``SRR_LiS:active_site_stability`` declares ``require_adsorbate=False``, so
    those rows must be judged without a sulfur adsorbate -- while a shared
    electrocatalytic intermediate must stay out of scope for the default
    sulfur-reduction target.
    """

    for metal in ("Li", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co"):
        result = validate_reaction_record(
            "SRR_LiS", {"adsorbate": metal, "property_type": "formation energy", "unit": "eV"}
        )
        assert result["valid"] is True, metal
        assert result["property_type"] == "formation_energy"

    shared = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "*OOH", "property_type": "gibbs free energy change", "unit": "eV"}
    )
    assert shared["valid"] is False
    assert shared["status"] == "out_of_scope"
    assert "intermediate_out_of_scope" in shared["reasons"]


def test_corrected_sulfur_desorption_barrier_releases_the_stale_out_of_scope():
    """Precondition of the label writer's re-derivation guard.

    ``DFTReactionLabelService`` refuses to overwrite a stored ``out_of_scope``
    unless the record's own *current* fields no longer justify it.  This pins both
    halves: the un-corrected B0091 shape still fails, the reviewed correction
    (property_type=sulfur desorption barrier, adsorbate=S) passes.
    """

    stale = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "S8", "property_type": "reaction_barrier", "unit": "eV"}
    )
    assert stale["valid"] is False
    assert "property_out_of_scope" in stale["reasons"]

    corrected = validate_reaction_record(
        "SRR_LiS", {"adsorbate": "S", "property_type": "sulfur desorption barrier", "unit": "eV"}
    )
    assert corrected["valid"] is True
    assert corrected["property_type"] == "sulfur_desorption_barrier"
