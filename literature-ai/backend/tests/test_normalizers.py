from app.normalizers.chemistry_normalizer import ChemistryNormalizer, canonicalize_adsorbate, get_property_taxonomy
from app.normalizers.unit_normalizer import UnitNormalizer


def test_energy_conversion_mev_to_ev():
    normalizer = UnitNormalizer()
    result = normalizer.normalize_energy(500.0, "meV")
    assert result.normalized_value == 0.5
    assert result.normalized_unit == "eV"


def test_capacity_conversion_ah_per_kg_to_mah_per_g():
    normalizer = UnitNormalizer()
    result = normalizer.normalize_capacity(1.0, "Ah/kg")
    assert result.normalized_value == 1.0
    assert result.normalized_unit == "mAh/g"


def test_loading_conversion_g_per_m2_to_mg_per_cm2():
    normalizer = UnitNormalizer()
    result = normalizer.normalize_loading(1.0, "g/m2")
    assert result.normalized_value == 0.1
    assert result.normalized_unit == "mg/cm2"


def test_clean_numeric_string_handles_ascii_units():
    normalizer = UnitNormalizer()
    value, unit = normalizer.clean_numeric_string("15 uL/mg")
    assert value == 15.0
    assert unit == "uL/mg"


def test_normalize_dict_preserves_payload_shape():
    normalizer = UnitNormalizer()
    result = normalizer.normalize({"field_name": "capacity", "value": 800.0, "unit": "mAh/g"})
    assert result["normalized_value"] == 800.0
    assert result["normalized_unit"] == "mAh/g"


def test_energy_conversion_supports_unicode_molar_units():
    normalizer = UnitNormalizer()
    result = normalizer.normalize_energy(-96.485, "kJ·mol⁻¹")
    assert result.normalized_value == -1.0
    assert result.normalized_unit == "eV"


def test_energy_conversion_keeps_basis_qualified_units_out_of_plain_ev():
    normalizer = UnitNormalizer()
    result = normalizer.normalize_energy(-8.19, "eV/atom")
    assert result.normalized_value is None
    assert result.normalized_unit == "eV/atom"
    assert result.basis == "per_atom"
    assert "energy_basis_requires_explicit_modeling" in result.blockers


def test_property_taxonomy_maps_li2s_barriers_to_reaction_barrier_with_subtype():
    taxonomy = get_property_taxonomy("li2s_decomposition_barrier")
    assert taxonomy["canonical_property_type"] == "reaction_barrier"
    assert taxonomy["property_family"] == "kinetics"
    assert taxonomy["property_subtype"] == "li2s_decomposition_barrier"
    assert taxonomy["physical_dimension"] == "energy"


def test_chemistry_normalizer_keeps_rds_gibbs_free_energy_out_of_reaction_barrier():
    normalizer = ChemistryNormalizer()
    normalized = normalizer.normalize(
        {"property_type": "RDS Gibbs free energy", "reaction_step": "RDS"}
    )
    taxonomy = normalized["_normalized"]["property_taxonomy"]
    assert normalized["property_type"] == "gibbs_free_energy_change"
    assert taxonomy["canonical_property_type"] == "gibbs_free_energy_change"
    assert taxonomy["property_subtype"] == "gibbs_free_energy_change"


def test_chemistry_normalizer_distinguishes_activation_and_migration_barriers():
    activation = ChemistryNormalizer().normalize({"property_type": "ΔG‡"})
    migration = ChemistryNormalizer().normalize({"property_type": "Li diffusion barrier"})
    assert activation["property_type"] == "activation_energy"
    assert activation["_normalized"]["property_taxonomy"]["canonical_property_type"] == "reaction_barrier"
    assert activation["_normalized"]["property_taxonomy"]["property_subtype"] == "activation_energy"
    assert migration["property_type"] == "migration_barrier"
    assert migration["_normalized"]["property_taxonomy"]["property_subtype"] == "migration_barrier"


def test_chemistry_normalizer_canonicalizes_common_electrocatalysis_adsorbates():
    normalizer = ChemistryNormalizer()
    normalized = normalizer.normalize({"adsorbate": "OH*"})
    assert normalized["adsorbate"] == "*OH"
    assert canonicalize_adsorbate("*COOH") == "*COOH"
