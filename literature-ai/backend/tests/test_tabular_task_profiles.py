from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.domain.tabular_task_profiles import (
    TASK_PROFILE_VERSION,
    UNKNOWN_TASK,
    evaluate_tabular_readiness,
    get_tabular_task_profile,
    list_tabular_task_profiles,
    normalize_tabular_task,
)


def _ready_adsorption_record() -> dict:
    return {
        "reaction_type": "SRR_LiS",
        "reaction_validation_status": "valid",
        "canonical_property_type": "adsorption_energy",
        "normalized_value": -1.2,
        "normalized_unit": "eV",
        "safety_gate_passed": True,
        "evidence_present": True,
        "locator_status": "exact_page",
        "setting_link_status": "clear_primary",
        "linked_dft_setting": {"dft_setting_id": "setting-1", "functional": "PBE"},
        "paper_id": "paper-1",
        "catalyst_id": "catalyst-1",
        "catalyst_family": "Fe-N-C",
        "catalyst_type": "SAC",
        "metal_centers": ["Fe"],
        "coordination": "Fe-N4",
        "support": "carbon",
        "canonical_adsorbate": "Li2S6",
        "reaction_step": "Li2S8 -> Li2S6",
    }


def test_complete_srr_sac_adsorption_record_is_ready() -> None:
    result = evaluate_tabular_readiness("adsorption_energy", _ready_adsorption_record())

    assert result == {
        "label_ready": True,
        "tabular_ml_ready": True,
        "label_blockers": [],
        "feature_blockers": [],
        "task_profile": "SRR_LiS:adsorption_energy",
        "task_profile_version": TASK_PROFILE_VERSION,
    }
    json.dumps(result)


def test_label_gate_and_feature_gate_are_separate() -> None:
    record = _ready_adsorption_record()
    record.update(
        safety_gate_passed=False,
        evidence_present=False,
        locator_status="unsafe",
        setting_link_status="missing",
        linked_dft_setting=None,
    )
    for field in ("catalyst_id", "metal_centers", "coordination", "support"):
        record[field] = None

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["label_blockers"] == [
        "safety_gate_failed",
        "missing_evidence",
        "unsafe_locator",
        "missing_result_setting_link",
    ]
    assert result["feature_blockers"] == [
        "missing_catalyst_identity",
        "missing_metal_centers",
        "missing_coordination",
        "missing_support",
    ]
    assert result["label_ready"] is False
    assert result["tabular_ml_ready"] is False


@pytest.mark.parametrize(
    ("reaction_type", "validation_status", "expected_blocker"),
    [
        ("HER", "valid", "reaction_type_mismatch"),
        ("UNKNOWN", "valid", "unknown_reaction_type"),
        ("SRR_LiS", "ambiguous", "reaction_validation_ambiguous"),
        ("SRR_LiS", "out_of_scope", "reaction_validation_out_of_scope"),
    ],
)
def test_wrong_or_unvalidated_reactions_are_blocked(
    reaction_type: str, validation_status: str, expected_blocker: str
) -> None:
    record = _ready_adsorption_record()
    record["reaction_type"] = reaction_type
    record["reaction_validation_status"] = validation_status

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert expected_blocker in result["label_blockers"]
    assert result["label_ready"] is False
    assert result["tabular_ml_ready"] is False


@pytest.mark.parametrize(
    ("task", "target"),
    [
        ("reaction_barrier", "adsorption_energy"),
        ("adsorption_energy", "reaction_barrier"),
        ("rds_gibbs_free_energy", "reaction_barrier"),
    ],
)
def test_task_profiles_do_not_accept_each_others_targets(task: str, target: str) -> None:
    record = _ready_adsorption_record()
    record["canonical_property_type"] = target

    result = evaluate_tabular_readiness(task, record)

    assert result["label_blockers"] == ["target_property_not_allowed"]
    assert result["label_ready"] is False


@pytest.mark.parametrize("unit", ["kJ/mol", "meV", "EV", "unknown"])
def test_only_normalized_ev_is_accepted_without_mutating_raw_fields(unit: str) -> None:
    record = _ready_adsorption_record()
    record.update(value=-120.5, unit="kJ/mol", normalized_unit=unit)
    original = deepcopy(record)

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["label_blockers"] == ["unit_not_allowed"]
    assert record == original
    assert record["value"] == -120.5
    assert record["unit"] == "kJ/mol"


def test_missing_required_features_have_stable_blocker_order() -> None:
    record = _ready_adsorption_record()
    for field in ("metal_centers", "coordination", "support"):
        record[field] = None
    record["feature_blockers"] = ["missing_support", "instance_scope_unresolved"]

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["label_ready"] is True
    assert result["feature_blockers"] == [
        "missing_metal_centers",
        "missing_coordination",
        "missing_support",
        "instance_scope_unresolved",
    ]
    assert result["tabular_ml_ready"] is False


@pytest.mark.parametrize("scope", ["SAC", "single_atom", "DAC", "dual atom"])
def test_sac_and_dac_scopes_are_accepted(scope: str) -> None:
    record = _ready_adsorption_record()
    record["catalyst_type"] = scope

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert "unsupported_catalyst_scope" not in result["feature_blockers"]
    assert result["tabular_ml_ready"] is True


def test_other_catalyst_scope_is_blocked() -> None:
    record = _ready_adsorption_record()
    record["catalyst_type"] = "nanoparticle"

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["feature_blockers"] == ["unsupported_catalyst_scope"]
    assert result["label_ready"] is True
    assert result["tabular_ml_ready"] is False


def test_instance_ambiguity_is_a_feature_blocker() -> None:
    record = _ready_adsorption_record()
    record["descriptor_instance_ambiguous"] = True

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["label_ready"] is True
    assert result["feature_blockers"] == ["instance_ambiguous"]


def test_unknown_task_is_explicit_and_never_selects_a_default() -> None:
    assert normalize_tabular_task("not-a-task") == UNKNOWN_TASK
    with pytest.raises(KeyError, match="Unknown tabular task"):
        get_tabular_task_profile("not-a-task")
    with pytest.raises(KeyError, match="Unknown tabular task"):
        evaluate_tabular_readiness("not-a-task", _ready_adsorption_record())


def test_registered_profiles_are_candidates_with_fixed_split_groups() -> None:
    profiles = list_tabular_task_profiles()

    assert [profile.key for profile in profiles] == [
        "SRR_LiS:adsorption_energy",
        "SRR_LiS:rds_gibbs_free_energy",
        "SRR_LiS:reaction_barrier",
    ]
    for profile in profiles:
        assert profile.status == "candidate"
        assert profile.version == TASK_PROFILE_VERSION
        assert profile.reaction_type == "SRR_LiS"
        assert profile.split_group_keys == ("paper_id", "catalyst_family")
        assert "dft_result_id" not in profile.split_group_keys


def test_rds_gibbs_free_energy_profile_accepts_gibbs_targets() -> None:
    record = _ready_adsorption_record()
    record["canonical_property_type"] = "gibbs_free_energy_change"
    record["reaction_step"] = "RDS"

    result = evaluate_tabular_readiness("rds_gibbs_free_energy", record)

    assert result["task_profile"] == "SRR_LiS:rds_gibbs_free_energy"
    assert result["label_ready"] is True


def test_rds_gibbs_free_energy_does_not_require_canonical_adsorbate() -> None:
    record = _ready_adsorption_record()
    record["canonical_property_type"] = "gibbs_free_energy_change"
    record["canonical_adsorbate"] = None
    record["reaction_step"] = "RDS"

    result = evaluate_tabular_readiness("rds_gibbs_free_energy", record)

    assert result["label_ready"] is True
    assert result["tabular_ml_ready"] is True
    assert "missing_canonical_adsorbate" not in result["feature_blockers"]


def test_adsorption_energy_still_requires_canonical_adsorbate() -> None:
    record = _ready_adsorption_record()
    record["canonical_adsorbate"] = None

    result = evaluate_tabular_readiness("adsorption_energy", record)

    assert result["label_ready"] is True
    assert result["tabular_ml_ready"] is False
    assert result["feature_blockers"] == ["missing_canonical_adsorbate"]
