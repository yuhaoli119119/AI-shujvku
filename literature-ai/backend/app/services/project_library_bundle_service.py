from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample, DFTResult, DFTSetting, EvidenceSpan, Paper
from app.domain.element_descriptors import (
    ELEMENT_DESCRIPTOR_SOURCE,
    ELEMENT_DESCRIPTOR_SOURCE_VERSION,
    build_metal_descriptor_payload,
)
from app.domain.project_library_context import get_project_library_context
from app.normalizers.chemistry_normalizer import canonicalize_adsorbate, get_property_taxonomy
from app.normalizers.unit_normalizer import UnitNormalizer
from app.services.dft_rescan_policy import normalize_dft_reaction_step_for_identity
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results


PROJECT_LIBRARY_BUNDLE_SCHEMA_VERSION = "project_library_bundles_v1"
PROJECT_LIBRARY_ML_EXPORT_V4_SCHEMA_VERSION = "project_library_ml_export_v4"

_ADSORBATE_SPECIES = {"S8", "Li2S8", "Li2S6", "Li2S4", "Li2S2", "Li2S"}
_LI2S_REACTION_SUBTYPES = {
    "li2s_decomposition_barrier",
    "li2s_dissociation_energy",
    "li2s_deposition_barrier",
    "li2s_nucleation_barrier",
    "migration_barrier",
}
_ELECTRONIC_PROPERTIES = {"d_band_center", "bader_charge", "charge_transfer"}
_STRUCTURE_PROPERTIES = {"metal_metal_distance", "coordination_environment", "adsorption_site", "adsorption_mode"}
_VALID_EXPLICIT_ENERGY_KINDS = {
    "thermodynamic_energy",
    "activation_barrier",
    "free_energy_change",
    "electronic_descriptor",
    "structural_descriptor",
}
_LI2S_TASK_SUBTYPES = {
    "li2s_decomposition_barrier",
    "li2s_dissociation_energy",
    "li2s_deposition_barrier",
    "li2s_nucleation_barrier",
    "migration_barrier",
}
_TASK_ALIASES = {
    "adsorption_energy": "adsorption_energy",
    "srr_lis_adsorption_energy": "adsorption_energy",
    "li2s_barrier": "li2s_barrier",
    "li2s_reaction_energy": "li2s_reaction_energy",
    "srr_lis_li2s_reaction_energy": "li2s_reaction_energy",
    "rds_srr_multitask": "rds_srr_multitask",
    "rds_srr_multi_task": "rds_srr_multitask",
    "srr_multitask": "rds_srr_multitask",
    "srr_lis_multitask": "rds_srr_multitask",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _clean_text(value).lower()).strip("_")


def _canonical_task(value: Any) -> str:
    token = _token(value)
    return _TASK_ALIASES.get(token, token or "adsorption_energy")


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for nested in value.values():
            found.extend(_iter_dicts(nested))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_dicts(item))
    return found


def _payload_value(payload: Any, *keys: str) -> Any:
    key_set = set(keys)
    for item in _iter_dicts(payload):
        for key in key_set:
            value = item.get(key)
            if value not in (None, "", []):
                return value
    return None


def _top_level_payload_value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", []):
            return value
    return None


def _payload_bool(payload: Any, *keys: str) -> bool:
    value = _payload_value(payload, *keys)
    if isinstance(value, bool):
        return value
    return _token(value) in {"true", "yes", "y", "1", "manual_verification_required", "needs_user_decision"}


def _catalyst_scope(value: Any) -> str:
    token = _token(value)
    if token in {"sac", "single_atom", "single_atom_catalyst"}:
        return "SAC"
    if token in {"dac", "dual_atom", "dual_atom_catalyst", "double_atom"}:
        return "DAC"
    return "UNKNOWN"


def _support_payload(catalyst: CatalystSample, evidence_payload: Any = None) -> dict[str, Any]:
    support_raw = _payload_value(evidence_payload, "support_raw", "support_material", "support") or catalyst.support
    support_normalized = _payload_value(evidence_payload, "support_normalized") or support_raw
    return {
        "support_raw": support_raw,
        "support_normalized": support_normalized,
        "support_confidence": _payload_value(evidence_payload, "support_confidence"),
    }


def _payload_source(payload: Any, *keys: str) -> tuple[Any, str | None]:
    key_set = set(keys)
    for item in _iter_dicts(payload):
        for key in key_set:
            value = item.get(key)
            if value not in (None, "", []):
                return value, f"evidence_payload.{key}"
    return None, None


def _structure_payload(
    *,
    row: DFTResult,
    catalyst: CatalystSample,
    active_site_ref: dict[str, Any],
) -> dict[str, Any]:
    location = _source_location(row, {})
    values: dict[str, Any] = {}
    sources: dict[str, str | None] = {}

    def assign(field: str, *keys: str) -> None:
        value, source = _payload_source(row.evidence_payload, *keys)
        if value in (None, "", []):
            value, source = _dict_source(active_site_ref, "active_site_ref", *keys)
        if value in (None, "", []):
            value, source = _dict_source(location, "source_location", *keys)
        values[field] = value if value not in ("", []) else None
        sources[field] = source

    assign("metal_metal_distance_A", "metal_metal_distance_A", "metal_metal_distance", "metal_metal_distance_a")
    assign("coordination_environment", "coordination_environment", "coordination")
    if values["coordination_environment"] in (None, "", []):
        values["coordination_environment"] = catalyst.coordination
        sources["coordination_environment"] = "catalyst_sample.coordination" if catalyst.coordination else None
    assign("metal_ligand_distance_A", "metal_ligand_distance_A", "metal_ligand_distance", "metal_ligand_distance_a")
    taxonomy = get_property_taxonomy(row.property_type)
    adsorption_site_keys = (
        ("adsorption_site", "active_site")
        if taxonomy.get("property_family") == "electronic_descriptor"
        else ("adsorption_site", "site_label", "active_site")
    )
    assign("adsorption_site", *adsorption_site_keys)
    assign("adsorption_mode", "adsorption_mode")

    blockers: list[str] = []
    if values["metal_metal_distance_A"] is None:
        blockers.append("missing_metal_metal_distance")
    if values["coordination_environment"] is None:
        blockers.append("missing_coordination_environment")
    if values["adsorption_site"] is None:
        blockers.append("unknown_adsorption_site")

    return {
        **values,
        "structure_field_sources": sources,
        "structure_blockers": sorted(set(blockers)),
        "structure_source_version": "project_library_v4_structure_fields_v1",
    }


def _electronic_payload(
    *,
    row: DFTResult,
    active_site_ref: dict[str, Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    sources: dict[str, str | None] = {}

    def assign(field: str, *keys: str) -> None:
        value, source = _payload_source(row.evidence_payload, *keys)
        if value in (None, "", []):
            value, source = _dict_source(active_site_ref, "active_site_ref", *keys)
        values[field] = value if value not in ("", []) else None
        sources[field] = source

    assign("bader_charge_M1", "bader_charge_M1", "bader_charge_m1")
    assign("bader_charge_M2", "bader_charge_M2", "bader_charge_m2")
    assign("charge_transfer_e", "charge_transfer_e", "charge_transfer")
    assign("charge_transfer_direction", "charge_transfer_direction")
    assign("state_context", "state_context")
    assign("site_label", "site_label", "active_site", "adsorption_site")
    assign("metal_center_order_source", "metal_center_order_source")

    return {
        **values,
        "electronic_field_sources": sources,
        "electronic_source_version": "project_library_v4_electronic_fields_v1",
    }


def _dict_source(mapping: dict[str, Any], source_prefix: str, *keys: str) -> tuple[Any, str | None]:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value, f"{source_prefix}.{key}"
    return None, None


def _setting_ref(row: DFTResult, settings_by_paper: dict[str, list[DFTSetting]]) -> tuple[dict[str, Any] | None, str, list[str]]:
    explicit = _payload_value(row.evidence_payload, "dft_setting_id", "setting_id")
    settings = settings_by_paper.get(str(row.paper_id), [])
    if explicit:
        matched = next((setting for setting in settings if str(setting.id) == str(explicit)), None)
        if matched is None:
            return (
                {"dft_setting_id": str(explicit), "source": "evidence_payload"},
                "invalid_explicit_payload",
                ["invalid_result_setting_link"],
            )
        return (
            {
                "dft_setting_id": str(matched.id),
                "software": matched.software,
                "functional": matched.functional,
                "dispersion_correction": matched.dispersion_correction,
                "pseudopotential": matched.pseudopotential,
                "cutoff_energy_ev": matched.cutoff_energy_ev,
                "k_points": matched.k_points,
                "source": "evidence_payload",
            },
            "explicit_payload",
            [],
        )
    if len(settings) == 1:
        setting = settings[0]
        return (
            {
                "dft_setting_id": str(setting.id),
                "software": setting.software,
                "functional": setting.functional,
                "dispersion_correction": setting.dispersion_correction,
                "pseudopotential": setting.pseudopotential,
                "cutoff_energy_ev": setting.cutoff_energy_ev,
                "k_points": setting.k_points,
                "source": "singleton_paper_setting",
            },
            "singleton_paper_setting",
            [],
        )
    if not settings:
        return None, "missing", ["missing_result_setting_link"]
    return None, "ambiguous", ["ambiguous_result_setting_link"]


def _instance_payload(
    *,
    row: DFTResult,
    catalyst: CatalystSample,
    setting_ref: dict[str, Any] | None,
    setting_status: str,
) -> tuple[str, dict[str, Any], str, list[str]]:
    explicit_key = _payload_value(row.evidence_payload, "active_site_instance_key")
    explicit_ref = _payload_value(row.evidence_payload, "active_site_ref")
    source = "evidence_payload"
    blockers: list[str] = []
    site_context = _payload_value(
        row.evidence_payload,
        "active_site_context",
        "active_site",
        "site_label",
    )
    structure_context = _payload_value(
        row.evidence_payload,
        "structure_context",
        "structure_name",
        "configuration",
        "model_name",
    )
    if explicit_key:
        key = _clean_text(explicit_key)
    else:
        setting_component = (
            f"dft_setting:{setting_ref['dft_setting_id']}"
            if setting_ref and setting_ref.get("dft_setting_id")
            else f"setting_status:{setting_status}"
        )
        site_component = _token(site_context) or "unknown_site"
        structure_component = _token(structure_context) or "unknown_structure"
        key = (
            f"paper:{row.paper_id}|catalyst:{catalyst.id}|{setting_component}"
            f"|structure:{structure_component}|site:{site_component}"
        )
        source = "generated_read_only_bundle_key"
    if setting_status == "ambiguous":
        blockers.append("ambiguous_result_setting_link")
    ref = explicit_ref if isinstance(explicit_ref, dict) else {}
    if ref.get("paper_id") and str(ref.get("paper_id")) != str(row.paper_id):
        blockers.append("active_site_ref_paper_mismatch")
    if ref.get("catalyst_sample_id") and str(ref.get("catalyst_sample_id")) != str(catalyst.id):
        blockers.append("active_site_ref_catalyst_mismatch")
    ref_setting = ref.get("dft_setting_ref") if isinstance(ref.get("dft_setting_ref"), dict) else {}
    ref_setting_id = ref_setting.get("dft_setting_id") or ref.get("dft_setting_id")
    actual_setting_id = setting_ref.get("dft_setting_id") if setting_ref else None
    if ref_setting_id and str(ref_setting_id) != str(actual_setting_id or ""):
        blockers.append("active_site_ref_setting_mismatch")
    ref = {
        **ref,
        "paper_id": str(row.paper_id),
        "catalyst_sample_id": str(catalyst.id),
        "active_site_instance_key": key,
        "dft_setting_ref": setting_ref,
        "active_site_context": site_context,
        "structure_context": structure_context,
        "binding_granularity": (
            "active_site_instance"
            if site_context or structure_context or explicit_key
            else "catalyst_setting"
        ),
        "binding_warnings": (
            []
            if site_context or structure_context or explicit_key
            else ["missing_explicit_active_site_or_structure_context"]
        ),
        "binding_source": source,
    }
    return key, ref, source, blockers


def _source_location(row: DFTResult, pages_by_record: dict[str, list[int]]) -> dict[str, Any]:
    location = {
        "source_section": row.source_section,
        "source_figure": row.source_figure,
        "page_locators": sorted(set(pages_by_record.get(str(row.id), []))),
    }
    payload_location = _payload_value(row.evidence_payload, "source_location", "evidence_location", "locator")
    if isinstance(payload_location, dict):
        location.update({key: value for key, value in payload_location.items() if value not in (None, "", [])})
    return location


def energy_kind_for_property(property_type: Any) -> str:
    taxonomy = get_property_taxonomy(_clean_text(property_type))
    canonical = taxonomy["canonical_property_type"]
    subtype = taxonomy["property_subtype"]
    if canonical == "gibbs_free_energy_change":
        return "free_energy_change"
    if subtype in {"li2s_dissociation_energy", "reaction_energy"} or canonical in {"adsorption_energy", "reaction_energy"}:
        return "thermodynamic_energy"
    if canonical == "reaction_barrier" or subtype in _LI2S_REACTION_SUBTYPES:
        return "activation_barrier"
    return "unknown"


def _explicit_energy_kind(row: DFTResult) -> str | None:
    explicit = _clean_text(_payload_value(row.evidence_payload, "energy_kind"))
    if explicit in _VALID_EXPLICIT_ENERGY_KINDS:
        return explicit
    return None


def _normalize_non_energy_unit(value: Any, unit: Any, physical_dimension: str) -> tuple[Any, Any, bool, list[str]]:
    if physical_dimension == "charge":
        canonical = _token(unit)
        if value is None or canonical not in {"e", "electron", "electrons"}:
            return value, unit, False, ["invalid_or_unsupported_charge_unit"]
        return value, "e", True, []
    if physical_dimension == "length":
        normalized = UnitNormalizer().normalize_length(value, unit)
        blockers = list(normalized.blockers)
        if not normalized.is_valid or normalized.normalized_unit != "A":
            blockers.append("invalid_or_unsupported_length_unit")
        return normalized.normalized_value, normalized.normalized_unit, not blockers, sorted(set(blockers))
    return value, unit, True, []


def _material_identity_conflicts(payload_identity: Any, catalyst_name: Any) -> bool:
    payload_token = _token(payload_identity)
    catalyst_token = _token(catalyst_name)
    if not payload_token or not catalyst_token:
        return False
    return payload_token not in catalyst_token and catalyst_token not in payload_token


def _property_bundle(
    *,
    row: DFTResult,
    catalyst: CatalystSample,
    active_site_instance_key: str,
    active_site_ref: dict[str, Any],
    pages_by_record: dict[str, list[int]],
    gate_passed: bool,
) -> dict[str, Any]:
    taxonomy = get_property_taxonomy(row.property_type)
    canonical_adsorbate = canonicalize_adsorbate(row.adsorbate) or row.adsorbate
    explicit_energy_kind_raw = _clean_text(_payload_value(row.evidence_payload, "energy_kind"))
    energy_kind = _explicit_energy_kind(row) or energy_kind_for_property(row.property_type)
    canonical_property_type = taxonomy["canonical_property_type"]
    property_subtype = taxonomy["property_subtype"]
    is_energy_property = taxonomy["physical_dimension"] == "energy"
    normalized_energy = UnitNormalizer().normalize_energy(row.value, row.unit) if is_energy_property else None
    non_energy_value, non_energy_unit, non_energy_valid, non_energy_blockers = _normalize_non_energy_unit(
        row.value,
        row.unit,
        taxonomy["physical_dimension"],
    )
    source_text = _payload_value(row.evidence_payload, "source_text", "quoted_text", "evidence_text") or row.evidence_text
    payload_material_identity = _top_level_payload_value(
        row.evidence_payload,
        "material_identity",
        "catalyst_name",
        "material",
        "normalized_material",
    )
    return {
        "record_id": str(row.id),
        "active_site_instance_key": active_site_instance_key,
        "active_site_ref": active_site_ref,
        "catalyst_sample_name": catalyst.name,
        "payload_material_identity": payload_material_identity,
        "material_identity_conflict": _material_identity_conflicts(payload_material_identity, catalyst.name),
        "property_type": row.property_type,
        "canonical_property_type": canonical_property_type,
        "property_subtype": property_subtype,
        "energy_kind": energy_kind,
        "invalid_explicit_energy_kind": bool(
            explicit_energy_kind_raw
            and explicit_energy_kind_raw not in _VALID_EXPLICIT_ENERGY_KINDS
        ),
        "adsorbate": row.adsorbate,
        "canonical_adsorbate": canonical_adsorbate,
        "reaction_step": row.reaction_step,
        "reaction_type": row.reaction_type,
        "reaction_validation_status": row.reaction_validation_status,
        "value": row.value,
        "unit": row.unit,
        "is_energy_property": is_energy_property,
        "physical_dimension": taxonomy["physical_dimension"],
        "normalized_value": normalized_energy.normalized_value if normalized_energy else non_energy_value,
        "normalized_unit": normalized_energy.normalized_unit if normalized_energy else non_energy_unit,
        "unit_normalization_blockers": list(normalized_energy.blockers) if normalized_energy else non_energy_blockers,
        "unit_normalization_valid": normalized_energy.is_valid if normalized_energy else non_energy_valid,
        "source_text": source_text,
        "source_location": _source_location(row, pages_by_record),
        "confidence_level": row.confidence,
        "candidate_status": row.candidate_status,
        "safety_gate_passed": gate_passed,
        "manual_verification_required": _manual_verification_required(row),
    }


def _property_bucket(property_bundle: dict[str, Any]) -> str:
    canonical = property_bundle["canonical_property_type"]
    subtype = property_bundle["property_subtype"]
    if canonical == "adsorption_energy":
        return "adsorbate_properties"
    if canonical in {"reaction_barrier", "gibbs_free_energy_change", "reaction_energy"} or subtype in _LI2S_REACTION_SUBTYPES:
        return "reaction_step_properties"
    if canonical in _ELECTRONIC_PROPERTIES:
        return "electronic_properties"
    if canonical in _STRUCTURE_PROPERTIES or subtype in _STRUCTURE_PROPERTIES:
        return "structure_properties"
    return "other_properties"


def _feature_scope_for_property(prop: dict[str, Any]) -> str:
    canonical = prop["canonical_property_type"]
    subtype = prop["property_subtype"]
    if canonical == "adsorption_energy":
        return "adsorbate_property"
    if canonical in {"reaction_barrier", "gibbs_free_energy_change", "reaction_energy"} or subtype in _LI2S_TASK_SUBTYPES:
        return "reaction_step_property"
    if canonical in _ELECTRONIC_PROPERTIES:
        return "electronic_property"
    if canonical in _STRUCTURE_PROPERTIES or subtype in _STRUCTURE_PROPERTIES:
        return "structure_property"
    return "other_property"


def _is_li2s_related(prop: dict[str, Any]) -> bool:
    subtype = _clean_text(prop.get("property_subtype"))
    if subtype in _LI2S_TASK_SUBTYPES:
        return True
    reaction_step = _token(prop.get("reaction_step"))
    return "li2s" in reaction_step and any(
        marker in reaction_step
        for marker in ("decomposition", "dissociation", "deposition", "nucleation", "migration", "diffusion")
    )


def _is_missing_reaction_step(value: Any) -> bool:
    return not _clean_text(value)


def _manual_verification_required(row: DFTResult) -> bool:
    payload = row.evidence_payload
    status = _token(row.candidate_status)
    if status in {"ambiguous", "needs_user_decision", "manual_verification_required"}:
        return True
    if _payload_bool(payload, "manual_verification_required", "needs_user_decision", "ambiguous"):
        return True
    decision_status = _token(_payload_value(payload, "decision_status", "review_status", "status"))
    return decision_status in {"ambiguous", "needs_user_decision", "manual_verification_required"}


def _generated_instance_key_lacks_binding_evidence(prop: dict[str, Any], *, instance_source: str) -> bool:
    if instance_source == "evidence_payload":
        return False
    active_site_ref = prop.get("active_site_ref") if isinstance(prop.get("active_site_ref"), dict) else {}
    setting_ref = active_site_ref.get("dft_setting_ref") if isinstance(active_site_ref.get("dft_setting_ref"), dict) else {}
    return not (
        active_site_ref.get("paper_id")
        and active_site_ref.get("catalyst_sample_id")
        and setting_ref.get("dft_setting_id")
    )


def _record_blockers(prop: dict[str, Any], *, instance_source: str, instance_blockers: list[str]) -> list[str]:
    blockers = list(instance_blockers)
    if not prop["safety_gate_passed"]:
        blockers.append("safety_gate_failed")
    if prop["manual_verification_required"]:
        blockers.append("needs_user_decision")
    value = prop.get("normalized_value")
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        blockers.append("missing_or_invalid_numeric_value")
    if (
        prop.get("energy_kind") == "activation_barrier"
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) < 0
    ):
        blockers.append("negative_activation_barrier")
    if prop.get("is_energy_property") and (
        prop.get("normalized_unit") != "eV" or not prop.get("unit_normalization_valid")
    ):
        blockers.append("invalid_or_unsupported_energy_unit")
    if not prop.get("is_energy_property") and not prop.get("unit_normalization_valid"):
        blockers.append("invalid_or_unsupported_unit")
    if prop.get("material_identity_conflict"):
        blockers.append("material_identity_conflict")
    blockers.extend(prop.get("unit_normalization_blockers") or [])
    reaction_status = _token(prop.get("reaction_validation_status"))
    if reaction_status in {"ambiguous", "out_of_scope", "unsupported", "error"}:
        blockers.append(f"reaction_validation_{reaction_status}")
    if prop["energy_kind"] == "unknown" and prop["canonical_property_type"] in {
        "reaction_barrier",
        "gibbs_free_energy_change",
        "reaction_energy",
    }:
        blockers.append("unknown_energy_kind")
    if prop.get("is_energy_property") and prop.get("invalid_explicit_energy_kind"):
        blockers.append("invalid_energy_kind")
    canonical = prop.get("canonical_property_type")
    family = get_property_taxonomy(prop.get("property_type")).get("property_family")
    expected_energy_kind = (
        "activation_barrier"
        if canonical == "reaction_barrier"
        else "free_energy_change"
        if canonical == "gibbs_free_energy_change"
        else "electronic_descriptor"
        if family in {"electronic_descriptor", "optical_descriptor"}
        else "thermodynamic_energy"
    )
    if prop.get("is_energy_property") and prop.get("energy_kind") != expected_energy_kind:
        blockers.append("energy_kind_property_mismatch")
    if (
        prop.get("catalyst_scope") == "DAC"
        and (prop.get("bader_charge_M1") is not None or prop.get("bader_charge_M2") is not None)
        and _token(prop.get("metal_center_order_source")) in {"", "unknown"}
    ):
        blockers.append("unknown_metal_center_order")
    if prop["canonical_property_type"] == "adsorption_energy" and not prop["canonical_adsorbate"]:
        blockers.append("missing_adsorbate")
    if _generated_instance_key_lacks_binding_evidence(prop, instance_source=instance_source):
        blockers.append("generated_active_site_instance_key")
    return sorted(set(blockers))


def _task_contract(task: str) -> dict[str, Any]:
    canonical_task = _canonical_task(task)
    if canonical_task == "adsorption_energy":
        return {
            "task": canonical_task,
            "label_name": "adsorption_energy_eV",
            "feature_scope": "adsorbate_property",
        }
    if canonical_task == "li2s_reaction_energy":
        return {
            "task": canonical_task,
            "label_name": "li2s_reaction_energy_eV",
            "feature_scope": "reaction_step_property",
        }
    if canonical_task == "li2s_barrier":
        return {
            "task": canonical_task,
            "label_name": "li2s_barrier_eV",
            "feature_scope": "reaction_step_property",
        }
    if canonical_task == "rds_srr_multitask":
        return {
            "task": canonical_task,
            "label_name": "srr_multitask_energy_eV",
            "feature_scope": "reaction_step_property",
        }
    return {
        "task": canonical_task,
        "label_name": f"{canonical_task}_eV" if canonical_task else "adsorption_energy_eV",
        "feature_scope": "other_property",
    }


def _task_match_reasons(prop: dict[str, Any], task: str) -> tuple[bool, list[str]]:
    if _clean_text(prop.get("reaction_type")) != "SRR_LiS":
        return False, []
    canonical_task = _canonical_task(task)
    canonical = prop["canonical_property_type"]
    subtype = prop["property_subtype"]
    energy_kind = prop["energy_kind"]
    missing_reaction_step = _is_missing_reaction_step(prop.get("reaction_step"))
    reasons: list[str] = []
    if canonical_task == "adsorption_energy":
        return canonical == "adsorption_energy", reasons
    if canonical_task == "li2s_reaction_energy":
        if not _is_li2s_related(prop):
            if canonical in {"reaction_energy", "reaction_barrier"} and missing_reaction_step:
                reasons.append("missing_reaction_step")
                if energy_kind == "unknown":
                    reasons.append("unknown_energy_kind")
                elif energy_kind != "thermodynamic_energy" or canonical == "reaction_barrier":
                    reasons.append("energy_kind_task_mismatch")
                return True, reasons
            return False, reasons
        if missing_reaction_step:
            reasons.append("missing_reaction_step")
        if energy_kind == "unknown":
            reasons.append("unknown_energy_kind")
        elif energy_kind != "thermodynamic_energy" or canonical == "reaction_barrier":
            reasons.append("energy_kind_task_mismatch")
        return True, reasons
    if canonical_task == "li2s_barrier":
        if not _is_li2s_related(prop):
            if canonical in {"reaction_energy", "reaction_barrier"} and missing_reaction_step:
                reasons.append("missing_reaction_step")
                if energy_kind == "unknown":
                    reasons.append("unknown_energy_kind")
                elif energy_kind != "activation_barrier" or canonical == "reaction_energy":
                    reasons.append("energy_kind_task_mismatch")
                return True, reasons
            return False, reasons
        if missing_reaction_step:
            reasons.append("missing_reaction_step")
        if energy_kind == "unknown":
            reasons.append("unknown_energy_kind")
        elif energy_kind != "activation_barrier" or canonical == "reaction_energy":
            reasons.append("energy_kind_task_mismatch")
        return True, reasons
    if canonical_task == "rds_srr_multitask":
        is_rds = _token(prop.get("reaction_step")) == "rds" or "rds" in _token(prop.get("reaction_step"))
        is_srr_energy = canonical == "adsorption_energy" or canonical in {
            "reaction_barrier",
            "gibbs_free_energy_change",
            "reaction_energy",
        }
        if not (is_rds or is_srr_energy):
            return False, reasons
        if canonical in {"reaction_barrier", "gibbs_free_energy_change", "reaction_energy"} and missing_reaction_step:
            reasons.append("missing_reaction_step")
        return True, reasons
    return False, reasons


def _export_record_for_task(
    *,
    bundle: dict[str, Any],
    catalyst: dict[str, Any],
    instance: dict[str, Any],
    prop: dict[str, Any],
    task: str,
) -> dict[str, Any]:
    contract = _task_contract(task)
    _, task_blockers = _task_match_reasons(prop, task)
    blockers = sorted(set(prop["blockers"] + task_blockers))
    ml_ready = not blockers
    descriptor_payload = build_metal_descriptor_payload(catalyst["metal_centers"])
    return {
        "record_id": prop["record_id"],
        "paper_id": bundle["paper_id"],
        "title": bundle["paper_title"],
        "task": contract["task"],
        "label_name": contract["label_name"],
        "label_value": prop["normalized_value"],
        "label_unit": prop["normalized_unit"],
        "label_energy_kind": prop["energy_kind"],
        "label_property_subtype": prop["property_subtype"],
        "feature_scope": contract["feature_scope"],
        "catalyst_sample_id": catalyst["catalyst_sample_id"],
        "catalyst_name": catalyst["name"],
        "catalyst_type": catalyst["catalyst_type"],
        "metal_centers": catalyst["metal_centers"],
        "coordination": catalyst["coordination"],
        "support_raw": prop.get("support_raw", catalyst["support_raw"]),
        "support_normalized": prop.get("support_normalized", catalyst["support_normalized"]),
        "support_confidence": prop.get("support_confidence", catalyst["support_confidence"]),
        **descriptor_payload,
        "metal_metal_distance_A": prop.get("metal_metal_distance_A"),
        "coordination_environment": prop.get("coordination_environment"),
        "metal_ligand_distance_A": prop.get("metal_ligand_distance_A"),
        "adsorption_site": prop.get("adsorption_site"),
        "adsorption_mode": prop.get("adsorption_mode"),
        "structure_field_sources": prop.get("structure_field_sources", {}),
        "structure_source_version": prop.get("structure_source_version"),
        "structure_blockers": prop.get("structure_blockers", []),
        "bader_charge_M1": prop.get("bader_charge_M1"),
        "bader_charge_M2": prop.get("bader_charge_M2"),
        "charge_transfer_e": prop.get("charge_transfer_e"),
        "charge_transfer_direction": prop.get("charge_transfer_direction"),
        "state_context": prop.get("state_context"),
        "site_label": prop.get("site_label"),
        "electronic_field_sources": prop.get("electronic_field_sources", {}),
        "electronic_source_version": prop.get("electronic_source_version"),
        "active_site_instance_key": instance["active_site_instance_key"],
        "active_site_ref": instance["active_site_ref"],
        "energy_kind": prop["energy_kind"],
        "property_type": prop["property_type"],
        "canonical_property_type": prop["canonical_property_type"],
        "property_subtype": prop["property_subtype"],
        "adsorbate": prop["canonical_adsorbate"] or prop["adsorbate"],
        "reaction_step": prop["reaction_step"],
        "value": prop["normalized_value"],
        "unit": prop["normalized_unit"],
        "raw_value": prop["value"],
        "raw_unit": prop["unit"],
        "source_text": prop["source_text"],
        "source_location": prop["source_location"],
        "confidence_level": prop["confidence_level"],
        "ml_ready": ml_ready,
        "blockers": blockers,
        "manual_verification_required": prop["manual_verification_required"],
        "database_write_authority": "user_submit_only",
        "ai_consensus_auto_adopt_allowed": False,
        "element_descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
        "element_descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
    }


def _put_wide_value(target: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, "", []):
        return
    if key not in target:
        target[key] = value
        return
    existing = target[key]
    if isinstance(existing, list):
        if value not in existing:
            existing.append(value)
        return
    if existing != value:
        target[key] = [existing, value]


def _put_supplemental_wide_values(target: dict[str, Any], prop: dict[str, Any]) -> None:
    supplemental_fields = {
        "bader_charge_M1": "bader_charge_M1_e",
        "bader_charge_M2": "bader_charge_M2_e",
        "charge_transfer_e": "charge_transfer_e",
        "metal_metal_distance_A": "metal_metal_distance_A",
        "coordination_environment": "coordination_environment",
        "metal_ligand_distance_A": "metal_ligand_distance_A",
        "adsorption_site": "adsorption_site",
        "adsorption_mode": "adsorption_mode",
    }
    for field, key in supplemental_fields.items():
        _put_wide_value(target, key, prop.get(field))


def _wide_property_key(prop: dict[str, Any]) -> str:
    canonical = prop["canonical_property_type"]
    subtype = prop["property_subtype"]
    unit = _token(prop.get("normalized_unit") or prop.get("unit")) or "value"
    if canonical == "adsorption_energy":
        adsorbate = _token(prop.get("canonical_adsorbate") or prop.get("adsorbate")) or "unknown_adsorbate"
        return f"adsorption_energy_{adsorbate}_{unit}"
    if subtype in _LI2S_TASK_SUBTYPES:
        return f"{subtype}_{unit}"
    if canonical in {"reaction_barrier", "reaction_energy", "gibbs_free_energy_change"}:
        step = _token(prop.get("reaction_step")) or "unknown_step"
        return f"{canonical}_{step}_{unit}"
    if canonical in _ELECTRONIC_PROPERTIES:
        site_label = _token(prop.get("site_label"))
        if canonical == "bader_charge" and site_label in {"m1", "m2"}:
            return f"bader_charge_{site_label}_{unit}"
        adsorbate = _token(prop.get("canonical_adsorbate") or prop.get("adsorbate"))
        suffix = f"_{adsorbate}" if adsorbate else ""
        return f"{canonical}{suffix}_{unit}"
    if subtype in _STRUCTURE_PROPERTIES or canonical in _STRUCTURE_PROPERTIES:
        return f"{subtype or canonical}_{unit}"
    return f"{canonical or subtype or 'property'}_{unit}"


def _compact_property(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": prop["record_id"],
        "feature_scope": _feature_scope_for_property(prop),
        "property_type": prop["property_type"],
        "canonical_property_type": prop["canonical_property_type"],
        "property_subtype": prop["property_subtype"],
        "energy_kind": prop["energy_kind"],
        "adsorbate": prop["canonical_adsorbate"] or prop["adsorbate"],
        "reaction_step": prop["reaction_step"],
        "reaction_type": prop["reaction_type"],
        "reaction_validation_status": prop.get("reaction_validation_status"),
        "value": prop["normalized_value"],
        "unit": prop["normalized_unit"],
        "raw_value": prop["value"],
        "raw_unit": prop["unit"],
        "source_text": prop["source_text"],
        "source_location": prop["source_location"],
        "confidence_level": prop["confidence_level"],
        "bader_charge_M1": prop.get("bader_charge_M1"),
        "bader_charge_M2": prop.get("bader_charge_M2"),
        "charge_transfer_e": prop.get("charge_transfer_e"),
        "charge_transfer_direction": prop.get("charge_transfer_direction"),
        "state_context": prop.get("state_context"),
        "site_label": prop.get("site_label"),
        "electronic_field_sources": prop.get("electronic_field_sources", {}),
        "ml_ready": prop["ml_ready"],
        "blockers": prop["blockers"],
        "manual_verification_required": prop["manual_verification_required"],
    }


def _instance_properties(instance: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        prop
        for group_name in (
            "adsorbate_properties",
            "reaction_step_properties",
            "electronic_properties",
            "structure_properties",
            "other_properties",
        )
        for prop in instance["properties"][group_name]
    ]


def _task_records_for_instance(
    *,
    bundle: dict[str, Any],
    catalyst: dict[str, Any],
    instance: dict[str, Any],
    task: str,
) -> list[dict[str, Any]]:
    candidates_by_semantic_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for group_name in ("adsorbate_properties", "reaction_step_properties"):
        for prop in instance["properties"][group_name]:
            matches_task, _ = _task_match_reasons(prop, task)
            if not matches_task:
                continue
            record = _export_record_for_task(
                bundle=bundle,
                catalyst=catalyst,
                instance=instance,
                prop=prop,
                task=task,
            )
            semantic_key = (
                record["canonical_property_type"],
                record["label_property_subtype"],
                record["adsorbate"],
                ""
                if record["canonical_property_type"] == "adsorption_energy"
                else _token(normalize_dft_reaction_step_for_identity(record["reaction_step"])),
                record["label_energy_kind"],
            )
            candidates_by_semantic_key[semantic_key].append(record)

    records: list[dict[str, Any]] = []
    for semantic_key in sorted(candidates_by_semantic_key, key=lambda item: tuple(map(str, item))):
        value_groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
        for record in candidates_by_semantic_key[semantic_key]:
            value_groups[(record["label_value"], record["label_unit"])].append(record)

        representatives: list[dict[str, Any]] = []
        for value_key in sorted(value_groups, key=lambda item: tuple(map(str, item))):
            equivalent = sorted(
                value_groups[value_key],
                key=lambda item: (
                    not bool(item["ml_ready"]),
                    len(item["blockers"]),
                    item["record_id"],
                ),
            )
            representative = equivalent[0]
            representative["duplicate_record_ids"] = sorted(
                item["record_id"] for item in equivalent[1:]
            )
            representatives.append(representative)

        if len(representatives) > 1:
            for representative in representatives:
                representative["blockers"] = sorted(
                    set(representative["blockers"] + ["conflicting_property_values"])
                )
                representative["ml_ready"] = False
        records.extend(representatives)
    return records


def _export_sample_record_for_task(
    *,
    bundle: dict[str, Any],
    catalyst: dict[str, Any],
    instance: dict[str, Any],
    task_records: list[dict[str, Any]],
    task: str,
) -> dict[str, Any]:
    descriptor_payload = build_metal_descriptor_payload(catalyst["metal_centers"])
    all_props = _instance_properties(instance)
    safe_props = [prop for prop in all_props if prop.get("ml_ready")]
    property_groups = {
        group_name: [_compact_property(prop) for prop in instance["properties"][group_name]]
        for group_name in (
            "adsorbate_properties",
            "reaction_step_properties",
            "electronic_properties",
            "structure_properties",
            "other_properties",
        )
    }
    wide_properties: dict[str, Any] = {}
    for prop in safe_props:
        _put_wide_value(wide_properties, _wide_property_key(prop), prop["normalized_value"])
        _put_supplemental_wide_values(wide_properties, prop)
    conflicting_wide_keys = sorted(
        key
        for key, value in wide_properties.items()
        if isinstance(value, list) and len({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value}) > 1
    )

    distinct_safe_facts = {
        (
            prop.get("canonical_property_type"),
            prop.get("property_subtype"),
            prop.get("canonical_adsorbate") or prop.get("adsorbate"),
            prop.get("reaction_step"),
            prop.get("normalized_value"),
            prop.get("normalized_unit"),
        )
        for prop in safe_props
    }
    for prop in safe_props:
        electronic_sources = prop.get("electronic_field_sources") or {}
        structure_sources = prop.get("structure_field_sources") or {}
        source_by_field = {
            "bader_charge_M1": electronic_sources.get("bader_charge_M1"),
            "bader_charge_M2": electronic_sources.get("bader_charge_M2"),
            "charge_transfer_e": electronic_sources.get("charge_transfer_e"),
            "metal_metal_distance_A": structure_sources.get("metal_metal_distance_A"),
            "coordination_environment": structure_sources.get("coordination_environment"),
            "metal_ligand_distance_A": structure_sources.get("metal_ligand_distance_A"),
            "adsorption_site": structure_sources.get("adsorption_site"),
            "adsorption_mode": structure_sources.get("adsorption_mode"),
        }
        for field, source in source_by_field.items():
            value = prop.get(field)
            if value not in (None, "", []) and source and not str(source).startswith("catalyst_sample."):
                distinct_safe_facts.add(
                    ("supplemental", field, prop.get("state_context"), value, None, None)
                )
    blockers = sorted({blocker for record in task_records for blocker in record["blockers"]})
    if conflicting_wide_keys:
        blockers = sorted(set(blockers + ["conflicting_complementary_property_values"]))
        for record in task_records:
            record["blockers"] = sorted(
                set(record["blockers"] + ["conflicting_complementary_property_values"])
            )
            record["ml_ready"] = False
    if len(distinct_safe_facts) < 2:
        blockers = sorted(set(blockers + ["insufficient_bundle_completeness"]))
        for record in task_records:
            record["blockers"] = sorted(
                set(record["blockers"] + ["insufficient_bundle_completeness"])
            )
            record["ml_ready"] = False
    task_labels: list[dict[str, Any]] = []
    task_wide_labels: dict[str, Any] = {}
    for record in task_records:
        task_labels.append(
            {
                "record_id": record["record_id"],
                "label_name": record["label_name"],
                "label_value": record["label_value"],
                "label_unit": record["label_unit"],
                "label_energy_kind": record["label_energy_kind"],
                "label_property_subtype": record["label_property_subtype"],
                "adsorbate": record["adsorbate"],
                "reaction_step": record["reaction_step"],
                "ml_ready": record["ml_ready"],
                "blockers": record["blockers"],
            }
        )
        _put_wide_value(task_wide_labels, record["label_name"], record["label_value"])
    ml_ready = bool(task_records) and not blockers
    first_record = task_records[0]
    return {
        "sample_id": instance["active_site_instance_key"],
        "sample_unit": (instance.get("active_site_ref") or {}).get(
            "binding_granularity",
            "active_site_instance",
        ),
        "paper_id": bundle["paper_id"],
        "title": bundle["paper_title"],
        "task": _canonical_task(task),
        "catalyst_sample_id": catalyst["catalyst_sample_id"],
        "catalyst_name": catalyst["name"],
        "catalyst_type": catalyst["catalyst_type"],
        "metal_centers": catalyst["metal_centers"],
        "coordination": catalyst["coordination"],
        "support_raw": first_record.get("support_raw", catalyst["support_raw"]),
        "support_normalized": first_record.get("support_normalized", catalyst["support_normalized"]),
        "support_confidence": first_record.get("support_confidence", catalyst["support_confidence"]),
        **descriptor_payload,
        "active_site_instance_key": instance["active_site_instance_key"],
        "active_site_ref": instance["active_site_ref"],
        "dft_setting_ref": instance["dft_setting_ref"],
        "binding_source": instance["binding_source"],
        "task_record_ids": [record["record_id"] for record in task_records],
        "source_record_ids": [prop["record_id"] for prop in all_props],
        "safe_source_record_ids": [prop["record_id"] for prop in safe_props],
        "distinct_safe_property_count": len(distinct_safe_facts),
        "excluded_unsafe_property_count": len(all_props) - len(safe_props),
        "task_labels": task_labels,
        "task_wide_labels": task_wide_labels,
        "wide_properties": dict(sorted(wide_properties.items())),
        "property_group_counts": {
            group_name: len(values)
            for group_name, values in property_groups.items()
        },
        "property_groups": property_groups,
        "metal_metal_distance_A": first_record.get("metal_metal_distance_A"),
        "coordination_environment": first_record.get("coordination_environment"),
        "metal_ligand_distance_A": first_record.get("metal_ligand_distance_A"),
        "adsorption_site": first_record.get("adsorption_site"),
        "adsorption_mode": first_record.get("adsorption_mode"),
        "structure_field_sources": first_record.get("structure_field_sources", {}),
        "structure_blockers": first_record.get("structure_blockers", []),
        "bader_charge_M1": first_record.get("bader_charge_M1"),
        "bader_charge_M2": first_record.get("bader_charge_M2"),
        "charge_transfer_e": first_record.get("charge_transfer_e"),
        "charge_transfer_direction": first_record.get("charge_transfer_direction"),
        "state_context": first_record.get("state_context"),
        "site_label": first_record.get("site_label"),
        "electronic_field_sources": first_record.get("electronic_field_sources", {}),
        "descriptor_blockers": first_record.get("descriptor_blockers", []),
        "ml_ready": ml_ready,
        "blockers": blockers,
        "manual_verification_required": any(record["manual_verification_required"] for record in task_records),
        "database_write_authority": "user_submit_only",
        "ai_consensus_auto_adopt_allowed": False,
        "element_descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
        "element_descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
    }


class ProjectLibraryBundleService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def build_bundles(
        self,
        *,
        context_key: str,
        library_name: str | None = None,
        paper_id: UUID | None = None,
    ) -> dict[str, Any]:
        context = get_project_library_context(context_key)
        effective_library_name = library_name if library_name is not None else context.default_library_name
        papers = self._papers(effective_library_name=effective_library_name, paper_id=paper_id)
        paper_ids = [paper.id for paper in papers]
        papers_by_id = {str(paper.id): paper for paper in papers}
        catalysts_by_id, catalysts_by_paper = self._catalysts(paper_ids)
        settings_by_paper = self._settings(paper_ids)
        rows = self._dft_results(paper_ids)
        pages_by_record = self._pages([row.id for row in rows])
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="dft_results") if rows else {}

        bundles_by_catalyst: dict[str, dict[str, Any]] = {}
        active_instances: dict[tuple[str, str], dict[str, Any]] = {}
        ambiguous_records: list[dict[str, Any]] = []
        manual_required: list[dict[str, Any]] = []
        counts = Counter()
        explicit_key_owners: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)

        for catalyst in catalysts_by_id.values():
            paper = papers_by_id.get(str(catalyst.paper_id))
            bundle = self._empty_catalyst_bundle(catalyst=catalyst, paper=paper)
            bundles_by_catalyst[str(catalyst.id)] = bundle
            counts["catalyst_sample_count"] += 1
            scope = _catalyst_scope(catalyst.catalyst_type)
            if scope == "SAC":
                counts["sac_sample_count"] += 1
            elif scope == "DAC":
                counts["dac_sample_count"] += 1

        for row in rows:
            counts["dft_result_count"] += 1
            catalyst = catalysts_by_id.get(str(row.catalyst_sample_id or ""))
            if catalyst is None:
                ambiguous_records.append(
                    {
                        "record_id": str(row.id),
                        "paper_id": str(row.paper_id),
                        "reason": "missing_catalyst_sample_id",
                        "property_type": row.property_type,
                        "adsorbate": row.adsorbate,
                    }
                )
                counts["ambiguous_records_count"] += 1
                continue
            if catalyst.paper_id != row.paper_id:
                ambiguous_records.append(
                    {
                        "record_id": str(row.id),
                        "paper_id": str(row.paper_id),
                        "catalyst_sample_id": str(catalyst.id),
                        "reason": "catalyst_sample_paper_mismatch",
                        "property_type": row.property_type,
                        "adsorbate": row.adsorbate,
                    }
                )
                counts["ambiguous_records_count"] += 1
                continue
            bundle = bundles_by_catalyst.setdefault(
                str(catalyst.id),
                self._empty_catalyst_bundle(catalyst=catalyst, paper=papers_by_id.get(str(row.paper_id))),
            )
            setting_ref, setting_status, setting_blockers = _setting_ref(row, settings_by_paper)
            instance_key, active_site_ref, instance_source, instance_blockers = _instance_payload(
                row=row,
                catalyst=catalyst,
                setting_ref=setting_ref,
                setting_status=setting_status,
            )
            if instance_source == "evidence_payload":
                explicit_key_owners[(str(row.paper_id), instance_key)].add(
                    (
                        str(catalyst.id),
                        str(setting_ref.get("dft_setting_id")) if setting_ref else "",
                    )
                )
            prop = _property_bundle(
                row=row,
                catalyst=catalyst,
                active_site_instance_key=instance_key,
                active_site_ref=active_site_ref,
                pages_by_record=pages_by_record,
                gate_passed=bool(gate_by_id.get(str(row.id)) and gate_by_id[str(row.id)].eligible),
            )
            prop.update(_support_payload(catalyst, row.evidence_payload))
            prop.update(_structure_payload(row=row, catalyst=catalyst, active_site_ref=active_site_ref))
            prop.update(_electronic_payload(row=row, active_site_ref=active_site_ref))
            prop["catalyst_scope"] = _catalyst_scope(catalyst.catalyst_type)
            blockers = _record_blockers(
                prop,
                instance_source=instance_source,
                instance_blockers=instance_blockers + setting_blockers,
            )
            prop["blockers"] = blockers
            prop["ml_ready"] = not blockers
            if prop["manual_verification_required"]:
                manual_required.append(
                    {
                        "record_id": prop["record_id"],
                        "paper_id": str(row.paper_id),
                        "active_site_instance_key": instance_key,
                        "reason": "needs_user_decision",
                    }
                )
            instance = active_instances.get((str(catalyst.id), instance_key))
            if instance is None:
                instance = {
                    "active_site_instance_key": instance_key,
                    "active_site_ref": active_site_ref,
                    "binding_source": instance_source,
                    "dft_setting_ref": setting_ref,
                    "properties": {
                        "adsorbate_properties": [],
                        "reaction_step_properties": [],
                        "electronic_properties": [],
                        "structure_properties": [],
                        "other_properties": [],
                    },
                    "blockers": [],
                    "manual_verification_required": False,
                }
                active_instances[(str(catalyst.id), instance_key)] = instance
                bundle["active_site_instances"].append(instance)
                counts["active_site_instance_count"] += 1
            bucket = _property_bucket(prop)
            instance["properties"][bucket].append(prop)
            instance["blockers"] = sorted(set(instance["blockers"] + blockers))
            instance["manual_verification_required"] = (
                instance["manual_verification_required"] or prop["manual_verification_required"]
            )
            self._update_counts(counts, prop, catalyst_scope=_catalyst_scope(catalyst.catalyst_type))

        conflicting_explicit_keys = {
            scoped_key for scoped_key, owners in explicit_key_owners.items() if len(owners) > 1
        }
        for instance in active_instances.values():
            ref = instance.get("active_site_ref") if isinstance(instance.get("active_site_ref"), dict) else {}
            scoped_key = (str(ref.get("paper_id") or ""), instance["active_site_instance_key"])
            if scoped_key not in conflicting_explicit_keys:
                continue
            conflict_blocker = "conflicting_active_site_instance_key"
            instance["blockers"] = sorted(set(instance["blockers"] + [conflict_blocker]))
            for prop in _instance_properties(instance):
                prop["blockers"] = sorted(set(prop["blockers"] + [conflict_blocker]))
                prop["ml_ready"] = False

        counts["active_site_instance_key_missing_or_generated_count"] = sum(
            1
            for _bundle in bundles_by_catalyst.values()
            for instance in _bundle["active_site_instances"]
            if instance["binding_source"] != "evidence_payload"
        )
        counts["active_site_instance_key_conflict_count"] = len(conflicting_explicit_keys)
        counts["manual_verification_required_count"] = len(manual_required)
        counts.setdefault("ambiguous_records_count", len(ambiguous_records))
        public_counts = self._finalize_counts(counts)

        return {
            "schema_version": PROJECT_LIBRARY_BUNDLE_SCHEMA_VERSION,
            "context_key": context.key,
            "context_version": context.version,
            "context_display_name_zh": context.display_name_zh,
            "library_name": normalize_library_name(effective_library_name) if effective_library_name is not None else None,
            "read_only": True,
            "auto_verification_applied": False,
            "database_write_authority": "user_submit_only",
            "ai_review_policy": {
                "ai_1_extracts_candidates": True,
                "ai_2_ai_3_review_signal_only": True,
                "ai_consensus_auto_adopt_allowed": False,
                "user_submit_required_for_database_write": True,
            },
            "element_descriptor_contract": {
                "source": ELEMENT_DESCRIPTOR_SOURCE,
                "source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
                "generated_fields": [
                    "atomic_number",
                    "electronegativity",
                    "valence_electron_count",
                    "dac_combined_descriptors",
                ],
            },
            "counts": public_counts,
            "ambiguous_records": ambiguous_records,
            "manual_verification_required": manual_required,
            "bundles": list(bundles_by_catalyst.values()),
        }

    def build_ml_export_v4(
        self,
        *,
        context_key: str,
        task: str = "adsorption_energy",
        library_name: str | None = None,
        paper_id: UUID | None = None,
        ready_only: bool = True,
    ) -> dict[str, Any]:
        canonical_task = _canonical_task(task)
        bundle_payload = self.build_bundles(
            context_key=context_key,
            library_name=library_name,
            paper_id=paper_id,
        )
        records = []
        sample_records = []
        blocker_counts = Counter()
        sample_blocker_counts = Counter()
        candidate_sample_count = 0
        for bundle in bundle_payload["bundles"]:
            catalyst = bundle["catalyst_sample"]
            for instance in bundle["active_site_instances"]:
                instance_task_records = _task_records_for_instance(
                    bundle=bundle,
                    catalyst=catalyst,
                    instance=instance,
                    task=canonical_task,
                )
                if not instance_task_records:
                    continue
                candidate_sample_count += 1
                sample_record = _export_sample_record_for_task(
                    bundle=bundle,
                    catalyst=catalyst,
                    instance=instance,
                    task_records=instance_task_records,
                    task=canonical_task,
                )
                if not ready_only or sample_record["ml_ready"]:
                    sample_records.append(sample_record)
                for blocker in sample_record["blockers"]:
                    sample_blocker_counts[blocker] += 1

                for record in instance_task_records:
                    if ready_only and not record["ml_ready"]:
                        for blocker in record["blockers"]:
                            blocker_counts[blocker] += 1
                        continue
                    records.append(record)
                    for blocker in record["blockers"]:
                        blocker_counts[blocker] += 1

        records.sort(key=lambda item: (item["paper_id"], item["catalyst_sample_id"], item["record_id"]))
        sample_records.sort(
            key=lambda item: (item["paper_id"], item["catalyst_sample_id"], item["active_site_instance_key"])
        )
        manifest = {
            "schema_version": PROJECT_LIBRARY_ML_EXPORT_V4_SCHEMA_VERSION,
            "dataset_version": "project-library-ml-export-v4.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context_key": bundle_payload["context_key"],
            "context_version": bundle_payload["context_version"],
            "library_name": bundle_payload["library_name"],
            "task": canonical_task,
            "source_bundle_schema_version": bundle_payload["schema_version"],
            "source_export_versions_unchanged": ["dft_results_ml_v2", "dft_results_ml_v3"],
            "ready_only": ready_only,
            "candidate_count": sum(
                1
                for bundle in bundle_payload["bundles"]
                for instance in bundle["active_site_instances"]
                for group in ("adsorbate_properties", "reaction_step_properties")
                for prop in instance["properties"][group]
                if _task_match_reasons(prop, canonical_task)[0]
            ),
            "candidate_sample_count": candidate_sample_count,
            "returned_count": len(records),
            "returned_sample_count": len(sample_records),
            "ml_ready_count": sum(1 for record in records if record["ml_ready"]),
            "sample_ml_ready_count": sum(1 for record in sample_records if record["ml_ready"]),
            "blocked_count": sum(1 for record in records if not record["ml_ready"]),
            "sample_blocked_count": sum(1 for record in sample_records if not record["ml_ready"]),
            "blocker_counts": dict(sorted(blocker_counts.items())),
            "sample_blocker_counts": dict(sorted(sample_blocker_counts.items())),
            "sample_unit": "catalyst_setting_or_active_site_instance",
            "sample_records_contract": "one row per CatalystSample/setting or explicit ActiveSiteInstance with grouped task labels and same-instance properties",
            "database_write_authority": "user_submit_only",
            "ai_consensus_auto_adopt_allowed": False,
            "element_descriptor_source": ELEMENT_DESCRIPTOR_SOURCE,
            "element_descriptor_source_version": ELEMENT_DESCRIPTOR_SOURCE_VERSION,
        }
        return {
            "schema_version": PROJECT_LIBRARY_ML_EXPORT_V4_SCHEMA_VERSION,
            "read_only": True,
            "auto_verification_applied": False,
            "status": "ready" if records else "not_ready",
            "manifest": manifest,
            "records": records,
            "sample_records": sample_records,
        }

    def build_ml_export_v4_csv(self, *, unit: str = "sample", **kwargs: Any) -> tuple[str, dict[str, Any]]:
        payload = self.build_ml_export_v4(**kwargs)
        if unit == "record":
            return self._build_ml_export_v4_record_csv(payload)
        if unit != "sample":
            raise KeyError("unit must be one of: sample, record")
        return self._build_ml_export_v4_sample_csv(payload)

    def _build_ml_export_v4_record_csv(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        output = io.StringIO()
        fieldnames = [
            "record_id",
            "paper_id",
            "title",
            "task",
            "label_name",
            "label_value",
            "label_unit",
            "label_energy_kind",
            "label_property_subtype",
            "feature_scope",
            "catalyst_sample_id",
            "catalyst_name",
            "catalyst_type",
            "metal_centers",
            "metal_descriptor_summary",
            "metal_1_descriptors",
            "metal_2_descriptors",
            "dac_combined_descriptors",
            "descriptor_blockers",
            "metal_metal_distance_A",
            "coordination_environment",
            "metal_ligand_distance_A",
            "adsorption_site",
            "adsorption_mode",
            "structure_field_sources",
            "structure_blockers",
            "bader_charge_M1",
            "bader_charge_M2",
            "charge_transfer_e",
            "charge_transfer_direction",
            "state_context",
            "site_label",
            "electronic_field_sources",
            "active_site_instance_key",
            "energy_kind",
            "property_type",
            "canonical_property_type",
            "property_subtype",
            "adsorbate",
            "reaction_step",
            "value",
            "unit",
            "support_raw",
            "support_normalized",
            "support_confidence",
            "source_text",
            "source_location",
            "ml_ready",
            "blockers",
            "manual_verification_required",
            "database_write_authority",
            "ai_consensus_auto_adopt_allowed",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for record in payload["records"]:
            writer.writerow({key: _csv_cell(record.get(key)) for key in fieldnames})
        return output.getvalue(), payload["manifest"]

    def _build_ml_export_v4_sample_csv(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sample_records = payload["sample_records"]
        output = io.StringIO()
        base_fieldnames = [
            "sample_id",
            "sample_unit",
            "paper_id",
            "title",
            "task",
            "ml_ready",
            "blockers",
            "manual_verification_required",
            "catalyst_sample_id",
            "catalyst_name",
            "catalyst_type",
            "metal_centers",
            "coordination",
            "support_raw",
            "support_normalized",
            "support_confidence",
            "active_site_instance_key",
            "active_site_ref",
            "dft_setting_ref",
            "binding_source",
            "task_record_ids",
            "source_record_ids",
            "task_labels",
            "task_wide_labels",
            "wide_properties",
            "property_group_counts",
            "property_groups",
            "metal_descriptor_summary",
            "metal_1_descriptors",
            "metal_2_descriptors",
            "dac_combined_descriptors",
            "descriptor_blockers",
            "metal_metal_distance_A",
            "coordination_environment",
            "metal_ligand_distance_A",
            "adsorption_site",
            "adsorption_mode",
            "structure_field_sources",
            "structure_blockers",
            "bader_charge_M1",
            "bader_charge_M2",
            "charge_transfer_e",
            "charge_transfer_direction",
            "state_context",
            "site_label",
            "electronic_field_sources",
            "database_write_authority",
            "ai_consensus_auto_adopt_allowed",
            "element_descriptor_source",
            "element_descriptor_source_version",
        ]
        dynamic_label_fieldnames = sorted(
            {
                key
                for sample in sample_records
                for key in (sample.get("task_wide_labels") or {})
                if key not in base_fieldnames
            }
        )
        dynamic_wide_fieldnames = sorted(
            {
                key
                for sample in sample_records
                for key in (sample.get("wide_properties") or {})
                if key not in base_fieldnames and key not in dynamic_label_fieldnames
            }
        )
        fieldnames = base_fieldnames + dynamic_label_fieldnames + dynamic_wide_fieldnames
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for sample in sample_records:
            task_wide_labels = sample.get("task_wide_labels") or {}
            wide_properties = sample.get("wide_properties") or {}
            row = {key: _csv_cell(sample.get(key)) for key in base_fieldnames}
            row.update({key: _csv_cell(task_wide_labels.get(key)) for key in dynamic_label_fieldnames})
            row.update({key: _csv_cell(wide_properties.get(key)) for key in dynamic_wide_fieldnames})
            writer.writerow(row)
        return output.getvalue(), payload["manifest"]

    def _papers(self, *, effective_library_name: str | None, paper_id: UUID | None) -> list[Paper]:
        stmt = select(Paper).order_by(Paper.year.desc().nulls_last(), Paper.title, Paper.id)
        if effective_library_name is not None:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, effective_library_name))
        if paper_id is not None:
            stmt = stmt.where(Paper.id == paper_id)
        return list(self.session.scalars(stmt).all())

    def _catalysts(
        self,
        paper_ids: list[UUID],
    ) -> tuple[dict[str, CatalystSample], dict[str, list[CatalystSample]]]:
        if not paper_ids:
            return {}, {}
        rows = self.session.scalars(
            select(CatalystSample).where(CatalystSample.paper_id.in_(paper_ids))
        ).all()
        by_id = {str(row.id): row for row in rows}
        by_paper: dict[str, list[CatalystSample]] = defaultdict(list)
        for row in rows:
            by_paper[str(row.paper_id)].append(row)
        return by_id, by_paper

    def _settings(self, paper_ids: list[UUID]) -> dict[str, list[DFTSetting]]:
        if not paper_ids:
            return {}
        rows = self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id.in_(paper_ids))).all()
        by_paper: dict[str, list[DFTSetting]] = defaultdict(list)
        for row in rows:
            by_paper[str(row.paper_id)].append(row)
        return by_paper

    def _dft_results(self, paper_ids: list[UUID]) -> list[DFTResult]:
        if not paper_ids:
            return []
        return list(
            self.session.scalars(
                select(DFTResult)
                .where(DFTResult.paper_id.in_(paper_ids))
                .order_by(DFTResult.paper_id, DFTResult.id)
            ).all()
        )

    def _pages(self, row_ids: list[UUID]) -> dict[str, list[int]]:
        if not row_ids:
            return {}
        spans = self.session.scalars(
            select(EvidenceSpan).where(
                EvidenceSpan.object_type.in_(("dft_result", "dft_results")),
                EvidenceSpan.object_id.in_([str(row_id) for row_id in row_ids]),
            )
        ).all()
        pages: dict[str, list[int]] = defaultdict(list)
        for span in spans:
            if span.page is not None:
                pages[span.object_id].append(span.page)
        return pages

    def _empty_catalyst_bundle(self, *, catalyst: CatalystSample, paper: Paper | None) -> dict[str, Any]:
        support = _support_payload(catalyst)
        return {
            "paper_id": str(catalyst.paper_id),
            "paper_title": paper.title if paper is not None else None,
            "catalyst_sample_id": str(catalyst.id),
            "catalyst_sample": {
                "catalyst_sample_id": str(catalyst.id),
                "name": catalyst.name,
                "catalyst_type": catalyst.catalyst_type,
                "catalyst_scope": _catalyst_scope(catalyst.catalyst_type),
                "metal_centers": catalyst.metal_centers or [],
                "coordination": catalyst.coordination,
                "support": catalyst.support,
                **support,
            },
            "active_site_instances": [],
        }

    def _update_counts(self, counts: Counter, prop: dict[str, Any], *, catalyst_scope: str) -> None:
        canonical = prop["canonical_property_type"]
        subtype = prop["property_subtype"]
        adsorbate = prop["canonical_adsorbate"]
        active_key = prop["active_site_instance_key"]
        if canonical == "adsorption_energy":
            counts["adsorption_energy_record_count"] += 1
            counts[f"sample_with_adsorption_energy:{active_key}"] = 1
            if adsorbate == "Li2S":
                counts["li2s_adsorption_energy_record_count"] += 1
                counts[f"sample_with_li2s_adsorption_energy:{active_key}"] = 1
        if subtype in _LI2S_REACTION_SUBTYPES:
            counts[f"{subtype}_record_count"] += 1
            counts[f"sample_with_{subtype}:{active_key}"] = 1
        if prop["energy_kind"] != "unknown":
            counts[f"energy_kind:{prop['energy_kind']}"] += 1
        if _token(prop["reaction_step"]) == "rds" or "rds" in _token(prop["reaction_step"]):
            counts["rds_record_count"] += 1
            counts[f"sample_with_rds:{active_key}"] = 1
        if canonical == "bader_charge":
            counts["bader_charge_record_count"] += 1
            counts[f"sample_with_bader_charge:{active_key}"] = 1
        if canonical == "charge_transfer":
            counts["charge_transfer_record_count"] += 1
            counts[f"sample_with_charge_transfer:{active_key}"] = 1
        if subtype == "metal_metal_distance" and catalyst_scope == "DAC":
            counts["dac_metal_metal_distance_record_count"] += 1
            counts[f"dac_sample_with_metal_metal_distance:{active_key}"] = 1
        if prop["ml_ready"]:
            if canonical == "adsorption_energy":
                counts["adsorption_property_gate_passed_record_count"] += 1
            if subtype in _LI2S_REACTION_SUBTYPES:
                counts["li2s_reaction_property_gate_passed_record_count"] += 1
            if canonical in {"adsorption_energy", "reaction_barrier", "gibbs_free_energy_change", "reaction_energy"}:
                counts["srr_property_gate_passed_record_count"] += 1

    @staticmethod
    def _finalize_counts(counts: Counter) -> dict[str, int]:
        prefixes = {
            "sample_with_adsorption_energy:": "sample_with_adsorption_energy_count",
            "sample_with_li2s_adsorption_energy:": "sample_with_li2s_adsorption_energy_count",
            "sample_with_li2s_decomposition_barrier:": "sample_with_li2s_decomposition_barrier_count",
            "sample_with_li2s_dissociation_energy:": "sample_with_li2s_dissociation_energy_count",
            "sample_with_li2s_deposition_barrier:": "sample_with_li2s_deposition_barrier_count",
            "sample_with_li2s_nucleation_barrier:": "sample_with_li2s_nucleation_barrier_count",
            "sample_with_migration_barrier:": "sample_with_migration_barrier_count",
            "sample_with_rds:": "sample_with_rds_count",
            "sample_with_bader_charge:": "sample_with_bader_charge_count",
            "sample_with_charge_transfer:": "sample_with_charge_transfer_count",
            "dac_sample_with_metal_metal_distance:": "dac_sample_with_metal_metal_distance_count",
            "energy_kind:": "energy_kind_group_count",
        }
        finalized: dict[str, int] = {}
        for key, value in counts.items():
            if any(str(key).startswith(prefix) for prefix in prefixes):
                continue
            finalized[str(key)] = int(value)
        for prefix, public_key in prefixes.items():
            if prefix == "energy_kind:":
                continue
            finalized[public_key] = sum(1 for key in counts if str(key).startswith(prefix))
        energy_kind_counts = {
            str(key).split(":", 1)[1]: int(value)
            for key, value in counts.items()
            if str(key).startswith("energy_kind:")
        }
        for kind, value in energy_kind_counts.items():
            finalized[f"energy_kind_{kind}_count"] = value
        return dict(sorted(finalized.items()))

def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
