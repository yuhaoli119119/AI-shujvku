from app.domain.lis_sac_dac_field_dictionary import (
    LI_S_SAC_DAC_FIELD_DICTIONARY_VERSION,
    build_topic_field_dictionary_payload,
    list_topic_field_definitions,
)
from app.domain.project_library_context import (
    PROJECT_LIBRARY_CONTEXT_VERSION,
    build_project_library_context_payload,
    get_project_library_context,
    normalize_project_library_context,
)


def test_li_s_project_library_context_has_expected_semantic_scope() -> None:
    context = get_project_library_context("锂硫双原子")

    assert context.key == "li_s_sac_dac"
    assert context.version == PROJECT_LIBRARY_CONTEXT_VERSION
    assert context.display_name_zh == "锂硫双原子"
    assert context.reaction_types == ("SRR_LiS",)
    assert "Li-S" in context.semantic_focus_terms
    assert "SRR_LiS" in context.semantic_focus_terms
    assert "Li2S" in context.semantic_focus_terms
    assert "dual atom" in context.semantic_focus_terms
    assert context.applies_to == ("parsing", "review", "filtering", "export")
    assert context.unknown_strategy == "preserve_unknown_or_null"


def test_project_library_context_normalizes_chinese_and_english_aliases() -> None:
    assert normalize_project_library_context("锂硫双原子") == "li_s_sac_dac"
    assert normalize_project_library_context("Li-S SAC/DAC") == "li_s_sac_dac"
    assert normalize_project_library_context("unregistered") == "UNKNOWN"


def test_li_s_topic_field_dictionary_covers_structure_dft_and_experiment() -> None:
    fields = list_topic_field_definitions("li_s_sac_dac")
    field_map = {field.canonical_key: field for field in fields}

    assert len(fields) >= 40
    assert field_map["metal_centers"].multi_value is True
    assert field_map["active_site_instance_key"].category == "identity"
    assert field_map["active_site_ref"].value_type == "object"
    assert field_map["catalyst_scope"].value_type == "enum"
    assert field_map["metal_metal_distance"].unit_suggestion == "angstrom"
    assert field_map["support_raw"].category == "structure"
    assert field_map["support_normalized"].category == "structure"
    assert field_map["support_confidence"].value_type == "number"
    assert field_map["adsorption_energy"].unit_suggestion == "eV"
    assert field_map["energy_kind"].value_type == "enum"
    assert field_map["li2s_dissociation_energy"].unit_suggestion == "eV"
    assert field_map["li2s_deposition_barrier"].unit_suggestion == "eV"
    assert field_map["bader_charge_M1"].unit_suggestion == "e"
    assert field_map["bader_charge_M2"].unit_suggestion == "e"
    assert field_map["d_band_center"].applies_to == ("DFT",)
    assert field_map["source_text"].category == "provenance"
    assert field_map["source_location"].value_type == "object"
    assert field_map["element_descriptor_source_version"].category == "postprocess"
    assert field_map["specific_capacity"].unit_suggestion == "mAh g^-1"
    assert field_map["electrolyte_to_sulfur_ratio"].unit_suggestion == "uL mg^-1"
    assert all(field.unknown_strategy == "mark_unknown_when_evidence_is_ambiguous" for field in fields)


def test_prompt_payload_builders_expose_read_only_project_library_metadata() -> None:
    contexts = build_project_library_context_payload()
    dictionaries = build_topic_field_dictionary_payload()

    assert contexts["li_s_sac_dac"]["version"] == PROJECT_LIBRARY_CONTEXT_VERSION
    assert "prompt_hints" in contexts["li_s_sac_dac"]
    assert dictionaries["li_s_sac_dac"]["version"] == LI_S_SAC_DAC_FIELD_DICTIONARY_VERSION
    assert any(
        item["canonical_key"] == "li2s_decomposition_barrier"
        for item in dictionaries["li_s_sac_dac"]["fields"]
    )
