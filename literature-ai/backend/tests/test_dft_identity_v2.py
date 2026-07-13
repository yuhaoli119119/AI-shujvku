from __future__ import annotations

from decimal import Decimal
import json

import pytest

from app.services.dft_identity_service import (
    build_dft_identity_v2,
    get_dft_identity_v2_property_policy,
)


pytestmark = pytest.mark.no_test_database


def _payload(**overrides):
    corrected = {
        "material": "Fe-GDY",
        "property_type": "adsorption_energy",
        "adsorbate": "Li2S4",
        "site_label": "bridge",
        "value": "-1.000",
        "unit": "eV",
    }
    corrected.update(overrides)
    return {"paper_id": "paper-1", "corrected_value": corrected}


def test_identity_v2_decimal_and_known_unit_equivalence_without_float_drift():
    ev = build_dft_identity_v2(_payload(value=Decimal("1.0000"), unit="eV"))
    mev = build_dft_identity_v2(_payload(value="1000.000", unit="meV"))
    float_value = build_dft_identity_v2(_payload(value=1.0, unit="electronvolt"))
    different = build_dft_identity_v2(_payload(value="1.000001", unit="eV"))

    assert ev.observation_key == mev.observation_key == float_value.observation_key
    assert ev.identity_payload["observation"]["value"] == "1"
    assert ev.identity_payload["observation"]["unit"] == "eV"
    assert different.observation_key != ev.observation_key


def test_identity_v2_molar_energy_units_normalize_to_ev():
    ev = build_dft_identity_v2(_payload(value="-1", unit="eV"))
    kj = build_dft_identity_v2(_payload(value="-96.4853321233", unit="kJ/mol"))

    assert kj.error_codes == ()
    assert kj.identity_payload["observation"]["unit"] == "eV"
    assert abs(float(kj.identity_payload["observation"]["value"]) + 1.0) < 1e-10
    assert ev.subject_key == kj.subject_key


def test_identity_v2_known_length_units_are_equivalent():
    angstrom = build_dft_identity_v2(
        _payload(property_type="bond_length", atom_pair="Li1-S", value="2.1", unit="Å")
    )
    nanometer = build_dft_identity_v2(
        _payload(property_type="bond_length", bond_pair="S-Li1", value="0.210", unit="nm")
    )

    assert angstrom.observation_key == nanometer.observation_key
    assert nanometer.identity_payload["observation"] == {
        "value": "2.1",
        "value_upper": "",
        "value_kind": "point",
        "unit": "Å",
    }


def test_identity_v2_unknown_units_are_not_silently_deduplicated():
    first = build_dft_identity_v2(_payload(value="1", unit="mystery-unit-a"))
    second = build_dft_identity_v2(_payload(value="1", unit="mystery-unit-b"))

    assert first.error_code == "unsupported_unit_identity"
    assert first.observation_key is None
    assert second.observation_key is None
    assert first.identity_payload["observation"]["unit"] != second.identity_payload["observation"]["unit"]


@pytest.mark.parametrize(
    ("field_name", "error_code"),
    [
        ("paper_id", "missing_paper_identity"),
        ("material", "missing_material_identity"),
        ("property_type", "missing_property_type_identity"),
    ],
)
def test_identity_v2_common_required_fields_block_observation_key(field_name, error_code):
    payload = _payload()
    if field_name == "paper_id":
        payload.pop("paper_id")
        payload["corrected_value"]["paper_id"] = "nested-paper-is-not-authoritative"
    else:
        payload["corrected_value"][field_name] = None

    identity = build_dft_identity_v2(payload)

    assert identity.error_code == error_code
    assert error_code in identity.error_codes
    assert identity.observation_key is None
    assert identity.subject_key.startswith("dft-subject-v2:")
    assert identity.identity_payload["subject"]


def test_identity_v2_property_required_field_matrix_blocks_incomplete_subjects():
    adsorption = build_dft_identity_v2(_payload(adsorbate=None))
    reaction_without_step = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            reaction_step=None,
            state_context="transition_state",
        )
    )
    reaction_without_state = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            reaction_step="Li2S4 -> TS",
            state_context=None,
            method_context={},
        )
    )
    bader_without_atom = build_dft_identity_v2(
        _payload(property_type="Bader charge", site_label=None, unit="e")
    )

    assert adsorption.error_code == "missing_adsorbate_identity"
    assert reaction_without_step.error_code == "missing_reaction_step_identity"
    assert reaction_without_state.error_code == "missing_state_context_identity"
    assert bader_without_atom.error_code == "missing_atom_or_site_identity"
    assert all(
        identity.observation_key is None
        for identity in (
            adsorption,
            reaction_without_step,
            reaction_without_state,
            bader_without_atom,
        )
    )


def test_identity_v2_property_required_field_matrix_accepts_valid_context_aliases():
    reaction = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            reaction_step="Li2S4 -> TS",
            method_context={"site_configuration": "transition"},
        )
    )
    bader = build_dft_identity_v2(
        _payload(property_type="Lowdin charge", site_label=None, atom_label="Fe1", unit="e")
    )

    assert reaction.observation_key is not None
    assert reaction.identity_payload["subject"]["property_context"]["configuration"] == "transition"
    assert bader.observation_key is not None
    assert bader.identity_payload["subject"]["site_label"] == "fe1"


def test_identity_v2_unit_policy_requires_units_unless_explicitly_dimensionless():
    missing_unit = build_dft_identity_v2(_payload(unit=None))
    dimensionless = build_dft_identity_v2(
        _payload(property_type="coordination_number", unit=None, value="4")
    )
    wrongly_dimensional = build_dft_identity_v2(
        _payload(property_type="coordination_number", unit="eV", value="4")
    )

    assert "missing_unit_identity" in missing_unit.error_codes
    assert missing_unit.observation_key is None
    assert dimensionless.identity_payload["property_policy"] == "dimensionless"
    assert dimensionless.observation_key is not None
    assert dimensionless.identity_payload["observation"]["unit"] == ""
    assert "unsupported_unit_identity" in wrongly_dimensional.error_codes
    assert wrongly_dimensional.observation_key is None


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("single value", "point"),
        ("interval", "range"),
        ("energy window", "energy_window"),
    ],
)
def test_identity_v2_value_kind_accepts_only_explicit_aliases(alias, canonical):
    identity = build_dft_identity_v2(
        _payload(
            value_kind=alias,
            value_upper="-0.5" if canonical != "point" else None,
        )
    )

    assert identity.observation_key is not None
    assert identity.identity_payload["observation"]["value_kind"] == canonical


def test_identity_v2_unknown_value_kind_and_missing_range_upper_are_blocked():
    unknown = build_dft_identity_v2(_payload(value_kind="fuzzy-band"))
    missing_upper = build_dft_identity_v2(_payload(value_kind="range", value_upper=None))

    assert "unsupported_value_kind_identity" in unknown.error_codes
    assert unknown.observation_key is None
    assert "missing_value_upper_identity" in missing_upper.error_codes
    assert missing_upper.observation_key is None


def test_identity_v2_distinguishes_point_range_and_energy_window():
    def identity(kind: str):
        return build_dft_identity_v2(
            _payload(
                property_type="pdos_overlap",
                value="-2.500",
                value_upper="-0.5000",
                value_kind=kind,
            )
        )

    identities = [identity(kind) for kind in ("point", "range", "energy_window")]

    assert len({item.observation_key for item in identities}) == 3
    assert {item.identity_payload["observation"]["value_kind"] for item in identities} == {
        "point",
        "range",
        "energy_window",
    }


def test_identity_v2_atom_alias_symmetry_preserves_numbers_and_reports_errors():
    li1 = build_dft_identity_v2(
        _payload(property_type="ICOHP", atom_pair="Li1-S", value="-2.1")
    )
    reversed_li1 = build_dft_identity_v2(
        _payload(property_type="ICOHP", interaction_pair="S – Li1", value="-2.1")
    )
    li2 = build_dft_identity_v2(
        _payload(property_type="ICOHP", bond="Li2-S", value="-2.1")
    )
    conflicting = build_dft_identity_v2(
        {
            **_payload(property_type="ICOHP", atom_pair="Li1-S", value="-2.1"),
            "evidence_payload": {"bond_pair": "Li2-S"},
        }
    )
    missing = build_dft_identity_v2(_payload(property_type="ICOHP", value="-2.1"))

    assert li1.subject_key == reversed_li1.subject_key
    assert li1.observation_key == reversed_li1.observation_key
    assert li1.identity_payload["subject"]["canonical_atom_pair"] == "li1-s"
    assert li1.subject_key != li2.subject_key
    assert conflicting.error_code == "conflicting_atom_pair_aliases"
    assert conflicting.observation_key is None
    assert missing.error_code == "missing_atom_pair_identity"
    assert missing.observation_key is None


def test_identity_v2_provenance_does_not_affect_keys_or_payload():
    first = build_dft_identity_v2(
        {
            **_payload(
                paper_id="nested-paper-a",
                property_context={"source_candidate_id": "candidate-a", "source_row_index": 2},
            ),
            "candidate_id": "candidate-a",
            "source": "ide-ai-a",
            "evidence_location": {"page": 4, "table": "T1", "row": 2, "source": "main"},
        }
    )
    second = build_dft_identity_v2(
        {
            **_payload(
                paper_id="nested-paper-b",
                property_context={"source_candidate_id": "candidate-b", "source_row_index": 44},
            ),
            "candidate_id": "candidate-b",
            "source": "web-ai-b",
            "evidence_location": {"page": 99, "table": "S8", "row": 44, "source": "si"},
        }
    )

    assert first.subject_key == second.subject_key
    assert first.observation_key == second.observation_key
    assert first.identity_payload == second.identity_payload


def test_identity_v2_property_specific_context_changes_subject_and_is_serializable():
    pbe = build_dft_identity_v2(
        _payload(
            property_type="band_gap",
            value="1.2",
            method_context={"functional": "PBE", "soc": False, "page": 7},
        )
    )
    hse = build_dft_identity_v2(
        _payload(
            property_type="band_gap",
            value="1.2",
            method_context={"functional": "HSE06", "soc": False, "page": 42},
        )
    )

    assert pbe.subject_key != hse.subject_key
    assert pbe.observation_key != hse.observation_key
    assert pbe.identity_payload["subject"]["property_context"] == {
        "functional": "pbe",
        "soc": False,
    }
    assert "page" not in json.dumps(pbe.identity_payload, sort_keys=True)
    json.dumps(pbe.identity_payload, ensure_ascii=False, sort_keys=True)


def test_identity_v2_optional_method_context_is_reported_without_splitting_keys():
    first = build_dft_identity_v2(
        _payload(
            method_context={
                "functional": "PBE",
                "configuration": "bridge",
                "k_points": "3x3x1",
                "cutoff_energy_ev": 400,
                "smearing_width": "0.05",
                "source_specific_setting": "source-a",
                "source_candidate_id": "candidate-a",
                "page": 4,
            }
        )
    )
    second = build_dft_identity_v2(
        _payload(
            method_context={
                "functional": "PBE",
                "configuration": "bridge",
                "k_points": "5x5x1",
                "cutoff_energy_ev": 520,
                "smearing_width": "0.10",
                "source_specific_setting": "source-b",
                "source_candidate_id": "candidate-b",
                "page": 99,
            }
        )
    )
    different_functional = build_dft_identity_v2(
        _payload(
            method_context={
                "functional": "HSE06",
                "configuration": "bridge",
                "k_points": "3x3x1",
            }
        )
    )

    assert first.subject_key == second.subject_key
    assert first.observation_key == second.observation_key
    assert first.observation_key != different_functional.observation_key
    assert first.identity_payload["subject"]["property_context"] == {
        "configuration": "bridge",
        "functional": "pbe",
    }
    assert first.identity_payload["reported_context"] == {
        "cutoff_energy_ev": "400",
        "k_points": "3x3x1",
        "smearing_width": "0.05",
        "source_specific_setting": "source-a",
    }
    assert second.identity_payload["reported_context"] == {
        "cutoff_energy_ev": "520",
        "k_points": "5x5x1",
        "smearing_width": "0.1",
        "source_specific_setting": "source-b",
    }
    serialized = json.dumps(first.identity_payload, sort_keys=True)
    assert "source_candidate_id" not in serialized
    assert '"page"' not in serialized


def test_identity_v2_property_policy_centralizes_required_and_allowed_context():
    adsorption = get_dft_identity_v2_property_policy("adsorption energy")
    reaction = get_dft_identity_v2_property_policy("reaction_barrier")

    assert adsorption.name == "adsorption_energy"
    assert "functional" in adsorption.allowed_context_keys
    assert "configuration" in adsorption.allowed_context_keys
    assert "k_points" not in adsorption.allowed_context_keys
    assert {requirement.error_code for requirement in reaction.requirements} >= {
        "missing_paper_identity",
        "missing_material_identity",
        "missing_property_type_identity",
        "missing_reaction_step_identity",
        "missing_state_context_identity",
    }


def test_recorded_statistical_and_magnetic_units_are_supported_without_weakening_required_fields():
    r_squared = build_dft_identity_v2(
        _payload(
            property_type="adsorption_energy_vs_charge_transfer_r_squared",
            adsorbate="Li2S",
            value="0.95",
            unit="dimensionless",
        )
    )
    slope = build_dft_identity_v2(
        _payload(
            property_type="adsorption_energy_vs_charge_transfer_slope",
            adsorbate="Li2S",
            value="8.65",
            unit="eV/e",
        )
    )
    magnetic = build_dft_identity_v2(
        _payload(
            property_type="magnetic_moment",
            adsorbate=None,
            value="1.919",
            unit="μB",
            state="bare",
        )
    )
    assert r_squared.observation_key
    assert r_squared.identity_payload["observation"]["unit"] == ""
    assert slope.observation_key
    assert slope.identity_payload["observation"]["unit"] == "eV/e"
    assert magnetic.observation_key
    assert magnetic.identity_payload["observation"]["unit"] == "μB"
    assert magnetic.identity_payload["subject"]["state_context"] == "bare"


def test_aggregate_charge_transfer_and_explicit_reaction_pathway_are_central_policies():
    charge_transfer = build_dft_identity_v2(
        _payload(
            property_type="bader_charge_transfer",
            adsorbate="Li2S",
            site_label=None,
            value="-0.52",
            unit="e",
            sign_convention="positive=electron loss; negative=electron gain",
        )
    )
    reaction = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            adsorbate="Li2S",
            site_label=None,
            reaction_step="Li2S dissociation during charging",
            reaction_type="Li2S_dissociation",
            value="1.70",
            unit="eV",
        )
    )
    assert charge_transfer.observation_key
    assert charge_transfer.identity_payload["property_policy"] == "aggregate_charge_transfer"
    assert charge_transfer.identity_payload["subject"]["property_context"] == {
        "charge_scope": "aggregate_support_adsorbate_charge_transfer",
        "sign_convention": "positive=electron loss; negative=electron gain",
    }
    assert reaction.observation_key
    assert reaction.identity_payload["subject"]["property_context"]["pathway"] == "li2s_dissociation"

    atomic_charge = build_dft_identity_v2(
        _payload(
            property_type="bader_charge",
            adsorbate="Li2S",
            site_label=None,
            value="-0.52",
            unit="e",
        )
    )
    missing_pathway = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            adsorbate="Li2S",
            site_label=None,
            reaction_step="Li2S dissociation during charging",
            reaction_type=None,
            value="1.70",
            unit="eV",
        )
    )
    assert atomic_charge.observation_key is None
    assert "missing_atom_or_site_identity" in atomic_charge.error_codes
    assert missing_pathway.observation_key is None
    assert "missing_state_context_identity" in missing_pathway.error_codes

    explicit_state = build_dft_identity_v2(
        _payload(
            property_type="reaction_barrier",
            adsorbate="Li2S",
            site_label=None,
            reaction_step="Li2S dissociation during charging",
            reaction_type="Li2S_dissociation",
            state="initial-transition-final",
            value="1.70",
            unit="eV",
        )
    )
    assert explicit_state.observation_key
    assert explicit_state.identity_payload["subject"]["state_context"] == "initial-transition-final"
