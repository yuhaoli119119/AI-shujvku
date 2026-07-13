from __future__ import annotations

from decimal import Decimal
import json

import pytest

from app.services.dft_identity_service import build_dft_identity_v2


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
