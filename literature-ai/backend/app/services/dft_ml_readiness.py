"""One shared answer to "may this DFT record claim ML readiness?".

Why this module exists
----------------------
``ai_verified_ml_ready`` (and its human counterpart ``ML_Ready``) are written by
*field review* code paths, but the name promises something the field review gate
never checked: that the record is usable as machine-learning training data.  The
export path does check it -- ``build_dft_ml_dataset`` applies the evidence gate,
``_v3_exclusion_reason`` applies the reaction contract, and
``select_training_records_v3`` re-checks the label contract on the way out -- so
a record can be labelled ``ai_verified_ml_ready`` while every export silently
drops it.

This module factors that export contract into a single read-only predicate so the
writers, the review queue, the review center and the exports cannot disagree.
It **mirrors** the export contract; it never relaxes it.  Anything this module
cannot prove is treated as not ML-ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.domain.reaction_taxonomy import normalize_reaction_type
from app.domain.tabular_task_profiles import (
    TabularTaskProfile,
    evaluate_tabular_readiness,
    list_tabular_task_profiles,
)
from app.normalizers.chemistry_normalizer import get_property_taxonomy
from app.services.dft_ml_policy import resolve_dft_unit
from app.utils.review_safety import ExportGateResult, is_export_eligible_extraction


SAFE_LOCATOR_STATUSES = frozenset({"exact", "exact_page", "verified"})


@dataclass(frozen=True)
class DFTRecordMLReadiness:
    """Verdict for one DFT record, with the exact reasons it was refused."""

    ml_ready: bool
    reasons: tuple[str, ...] = ()
    task_profile: str | None = None
    task_profile_version: str | None = None
    candidate_task_profiles: tuple[str, ...] = ()
    export_gate_eligible: bool = False
    export_gate_reasons: tuple[str, ...] = ()
    canonical_property_type: str | None = None
    normalized_unit: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "ml_ready": self.ml_ready,
            "reasons": list(self.reasons),
            "task_profile": self.task_profile,
            "task_profile_version": self.task_profile_version,
            "candidate_task_profiles": list(self.candidate_task_profiles),
            "export_gate_eligible": self.export_gate_eligible,
            "export_gate_reasons": list(self.export_gate_reasons),
            "canonical_property_type": self.canonical_property_type,
            "normalized_unit": self.normalized_unit,
        }


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _readiness_input(row: Any, gate: ExportGateResult, canonical_property: str | None, unit: str | None) -> dict[str, Any]:
    payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
    return {
        "reaction_type": row.reaction_type,
        "reaction_validation_status": row.reaction_validation_status,
        "canonical_property_type": canonical_property,
        "normalized_value": row.value,
        "normalized_unit": unit,
        "safety_gate_passed": bool(gate.eligible),
        "evidence_present": "missing_evidence_text" not in gate.reasons
        and "missing_evidence" not in gate.reasons,
        "locator_status": gate.locator_status,
        "label_blockers": tuple(payload.get("normalization_blockers") or ()),
    }


def _reaction_reasons(reaction_type: Any, validation_status: Any) -> list[str]:
    reasons: list[str] = []
    key = normalize_reaction_type(reaction_type)
    if key == "UNKNOWN":
        reasons.append("unknown_reaction_type")
    status = _clean(validation_status).lower()
    if status != "valid":
        reasons.append(f"reaction_validation_{status or 'missing'}")
    return reasons


def _candidate_profiles(reaction_type: Any, canonical_property: str | None) -> tuple[TabularTaskProfile, ...]:
    """Registered ML tasks that could accept this record's reaction + property."""
    key = normalize_reaction_type(reaction_type)
    if key == "UNKNOWN" or not canonical_property:
        return ()
    return tuple(
        profile
        for profile in list_tabular_task_profiles()
        if profile.reaction_type == key and canonical_property in profile.allowed_target_properties
    )


def evaluate_dft_record_ml_readiness(
    session: Session,
    row: Any,
    *,
    target_type: str = "dft_results",
    extra_reasons: tuple[str, ...] = (),
) -> DFTRecordMLReadiness:
    """Decide whether ``row`` may carry an ML-ready label.

    Mirrors the export contract exactly:

    1. the shared evidence/export gate must pass (evidence text, locator,
       material identity, required fields, unit resolution, open conflicts);
    2. the reaction must be registered, must be ``valid``, and the record's
       canonical property must belong to at least one registered task profile
       whose reaction type matches;
    3. the task profile's label contract must be satisfied.

    Any failure returns ``ml_ready=False`` with the reasons; nothing is inferred
    or defaulted to "ready".
    """
    reasons: list[str] = list(extra_reasons)
    gate = is_export_eligible_extraction(session, row, target_type=target_type)
    if not gate.eligible:
        reasons.extend(gate.reasons)

    canonical_property = get_property_taxonomy(_clean(row.property_type)).get("canonical_property_type")
    canonical_property = _clean(canonical_property) or None
    unit_resolution = resolve_dft_unit(row.property_type, row.unit)
    normalized_unit = unit_resolution.unit

    reasons.extend(_reaction_reasons(row.reaction_type, row.reaction_validation_status))

    locator_status = _clean(getattr(gate, "locator_status", None)).lower()
    if locator_status and locator_status not in SAFE_LOCATOR_STATUSES:
        reasons.append("unsafe_locator")
    elif not locator_status:
        reasons.append("missing_locator_status")

    profiles = _candidate_profiles(row.reaction_type, canonical_property)
    if not profiles:
        reasons.append("no_task_profile_for_property")

    readiness_input = _readiness_input(row, gate, canonical_property, normalized_unit)
    accepted_profile: TabularTaskProfile | None = None
    profile_blockers: list[str] = []
    # V3 has task-specific semantic exclusions in addition to the common label
    # gate (notably the RDS-only Gibbs profile).  Reuse that exact predicate so a
    # row can never carry an ML-ready label while the real V3 exporter drops it.
    from app.services.dft_export_service import _v3_exclusion_reason

    for profile in profiles:
        v3_exclusion = _v3_exclusion_reason(
            row,
            profile.key,
            profile.reaction_type,
            profile.allowed_target_properties,
        )
        if v3_exclusion is not None:
            tag = f"{profile.key}:{v3_exclusion}"
            if tag not in profile_blockers:
                profile_blockers.append(tag)
            continue
        readiness = evaluate_tabular_readiness(profile, readiness_input)
        if readiness["label_ready"] and readiness["tabular_ml_ready"]:
            accepted_profile = profile
            break
        for blocker in readiness["label_blockers"]:
            tag = f"{profile.key}:{blocker}"
            if tag not in profile_blockers:
                profile_blockers.append(tag)

    if accepted_profile is None and profiles:
        reasons.extend(profile_blockers or ["task_profile_not_satisfied"])

    deduped = tuple(dict.fromkeys(reason for reason in reasons if reason))
    return DFTRecordMLReadiness(
        ml_ready=not deduped,
        reasons=deduped,
        task_profile=accepted_profile.key if accepted_profile is not None else None,
        task_profile_version=accepted_profile.version if accepted_profile is not None else None,
        candidate_task_profiles=tuple(profile.key for profile in profiles),
        export_gate_eligible=bool(gate.eligible),
        export_gate_reasons=tuple(gate.reasons),
        canonical_property_type=canonical_property,
        normalized_unit=normalized_unit,
    )


__all__ = ["DFTRecordMLReadiness", "evaluate_dft_record_ml_readiness"]
