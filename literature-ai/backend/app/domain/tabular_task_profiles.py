from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from app.domain.reaction_taxonomy import normalize_reaction_type


TASK_PROFILE_VERSION = "tabular_task_profiles_v1"
UNKNOWN_TASK = "UNKNOWN"


@dataclass(frozen=True)
class TabularTaskProfile:
    key: str
    version: str
    reaction_type: str
    status: str
    allowed_target_properties: frozenset[str]
    allowed_units: frozenset[str]
    required_features: tuple[str, ...]
    optional_features: tuple[str, ...]
    split_group_keys: tuple[str, ...]


_COMMON_REQUIRED_FEATURES = (
    "paper_id",
    "catalyst_id",
    "catalyst_family",
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "canonical_adsorbate",
    "reaction_step",
)

_COMMON_OPTIONAL_FEATURES = (
    "functional",
    "dispersion_correction",
    "pseudopotential",
    "cutoff_energy_ev",
    "k_points",
    "d_band_center",
    "bader_charge",
    "charge_transfer",
)


def _profile(key: str, target_property: str) -> TabularTaskProfile:
    required_features = _COMMON_REQUIRED_FEATURES
    if key == "SRR_LiS:rds_gibbs_free_energy":
        required_features = tuple(
            feature for feature in _COMMON_REQUIRED_FEATURES if feature != "canonical_adsorbate"
        )
    return TabularTaskProfile(
        key=key,
        version=TASK_PROFILE_VERSION,
        reaction_type="SRR_LiS",
        status="candidate",
        allowed_target_properties=frozenset({target_property}),
        allowed_units=frozenset({"eV"}),
        required_features=required_features,
        optional_features=_COMMON_OPTIONAL_FEATURES,
        split_group_keys=("paper_id", "catalyst_family"),
    )


_PROFILES = MappingProxyType(
    {
        "SRR_LiS:adsorption_energy": _profile(
            "SRR_LiS:adsorption_energy", "adsorption_energy"
        ),
        "SRR_LiS:reaction_barrier": _profile(
            "SRR_LiS:reaction_barrier", "reaction_barrier"
        ),
        "SRR_LiS:rds_gibbs_free_energy": _profile(
            "SRR_LiS:rds_gibbs_free_energy", "gibbs_free_energy_change"
        ),
    }
)


def _task_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


_TASK_ALIASES = {
    _task_token("SRR_LiS:adsorption_energy"): "SRR_LiS:adsorption_energy",
    _task_token("srr_lis_adsorption_energy"): "SRR_LiS:adsorption_energy",
    _task_token("adsorption_energy"): "SRR_LiS:adsorption_energy",
    _task_token("SRR_LiS:reaction_barrier"): "SRR_LiS:reaction_barrier",
    _task_token("srr_lis_reaction_barrier"): "SRR_LiS:reaction_barrier",
    _task_token("reaction_barrier"): "SRR_LiS:reaction_barrier",
    _task_token("SRR_LiS:rds_gibbs_free_energy"): "SRR_LiS:rds_gibbs_free_energy",
    _task_token("srr_lis_rds_gibbs_free_energy"): "SRR_LiS:rds_gibbs_free_energy",
    _task_token("rds_gibbs_free_energy"): "SRR_LiS:rds_gibbs_free_energy",
}


def normalize_tabular_task(value: Any) -> str:
    return _TASK_ALIASES.get(_task_token(value), UNKNOWN_TASK)


def get_tabular_task_profile(value: Any) -> TabularTaskProfile:
    key = normalize_tabular_task(value)
    if key == UNKNOWN_TASK:
        raise KeyError(f"Unknown tabular task: {value!r}")
    return _PROFILES[key]


def list_tabular_task_profiles() -> tuple[TabularTaskProfile, ...]:
    return tuple(_PROFILES[key] for key in sorted(_PROFILES))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    return True


def _append_unique(blockers: list[str], blocker: str) -> None:
    if blocker and blocker not in blockers:
        blockers.append(blocker)


def _normalized_status(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _label_blockers(profile: TabularTaskProfile, record: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    reaction_type = normalize_reaction_type(record.get("reaction_type"))
    if reaction_type == "UNKNOWN":
        _append_unique(blockers, "unknown_reaction_type")
    elif reaction_type != profile.reaction_type:
        _append_unique(blockers, "reaction_type_mismatch")

    validation_status = _normalized_status(record.get("reaction_validation_status"))
    if validation_status != "valid":
        _append_unique(
            blockers,
            f"reaction_validation_{validation_status}" if validation_status else "missing_reaction_validation",
        )

    target_property = str(record.get("canonical_property_type") or "").strip()
    if not target_property:
        _append_unique(blockers, "missing_target_property")
    elif target_property not in profile.allowed_target_properties:
        _append_unique(blockers, "target_property_not_allowed")

    if record.get("normalized_value") is None:
        _append_unique(blockers, "missing_normalized_value")

    normalized_unit = str(record.get("normalized_unit") or "").strip()
    if not normalized_unit:
        _append_unique(blockers, "missing_normalized_unit")
    elif normalized_unit not in profile.allowed_units:
        _append_unique(blockers, "unit_not_allowed")

    if record.get("safety_gate_passed") is not True:
        _append_unique(blockers, "safety_gate_failed")
    if record.get("evidence_present") is not True:
        _append_unique(blockers, "missing_evidence")

    locator_status = _normalized_status(record.get("locator_status"))
    if locator_status not in {"exact", "exact_page", "verified"}:
        _append_unique(blockers, "unsafe_locator")

    setting_status = _normalized_status(record.get("setting_link_status"))
    has_linked_setting = _has_value(record.get("linked_dft_setting"))
    if setting_status == "ambiguous":
        _append_unique(blockers, "ambiguous_result_setting_link")
    elif setting_status != "clear_primary" or not has_linked_setting:
        _append_unique(blockers, "missing_result_setting_link")

    for blocker in record.get("label_blockers", ()) or ():
        _append_unique(blockers, str(blocker))
    return blockers


def _catalyst_scope(value: Any) -> str | None:
    token = _task_token(value)
    aliases = {
        "sac": "SAC",
        "singleatom": "SAC",
        "singleatomcatalyst": "SAC",
        "dac": "DAC",
        "dualatom": "DAC",
        "dualatomcatalyst": "DAC",
        "doubleatom": "DAC",
    }
    return aliases.get(token)


_FEATURE_BLOCKER_NAMES = {
    "catalyst_id": "missing_catalyst_identity",
    "catalyst_type": "missing_catalyst_scope",
}


def _feature_blockers(profile: TabularTaskProfile, record: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    for feature in profile.required_features:
        if not _has_value(record.get(feature)):
            _append_unique(blockers, _FEATURE_BLOCKER_NAMES.get(feature, f"missing_{feature}"))

    if _has_value(record.get("catalyst_type")) and _catalyst_scope(record.get("catalyst_type")) is None:
        _append_unique(blockers, "unsupported_catalyst_scope")

    if record.get("instance_ambiguous") is True or record.get("descriptor_instance_ambiguous") is True:
        _append_unique(blockers, "instance_ambiguous")

    for blocker in record.get("feature_blockers", ()) or ():
        _append_unique(blockers, str(blocker))
    return blockers


def evaluate_tabular_readiness(
    profile: TabularTaskProfile | str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    resolved_profile = (
        get_tabular_task_profile(profile) if isinstance(profile, str) else profile
    )
    if not isinstance(resolved_profile, TabularTaskProfile):
        raise TypeError("profile must be a TabularTaskProfile or registered task key")
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")

    label_blockers = _label_blockers(resolved_profile, record)
    feature_blockers = _feature_blockers(resolved_profile, record)
    label_ready = not label_blockers
    return {
        "label_ready": label_ready,
        "tabular_ml_ready": label_ready and not feature_blockers,
        "label_blockers": label_blockers,
        "feature_blockers": feature_blockers,
        "task_profile": resolved_profile.key,
        "task_profile_version": resolved_profile.version,
    }


__all__ = [
    "TASK_PROFILE_VERSION",
    "UNKNOWN_TASK",
    "TabularTaskProfile",
    "evaluate_tabular_readiness",
    "get_tabular_task_profile",
    "list_tabular_task_profiles",
    "normalize_tabular_task",
]
