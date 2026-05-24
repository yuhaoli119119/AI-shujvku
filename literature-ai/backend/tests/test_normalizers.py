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
