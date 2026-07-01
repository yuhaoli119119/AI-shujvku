from __future__ import annotations

import csv
import copy
import io
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import DFTSetting as DS
from app.db.models import EvidenceSpan as ES
from app.db.models import Paper as P
from app.domain.reaction_taxonomy import PROFILE_VERSION as REACTION_PROFILE_VERSION
from app.domain.tabular_task_profiles import evaluate_tabular_readiness, get_tabular_task_profile
from app.normalizers.chemistry_normalizer import (
    ChemistryNormalizer,
    canonicalize_adsorbate,
    get_property_taxonomy,
    property_type_filter_aliases,
)
from app.normalizers.unit_normalizer import UnitNormalizer
from app.services.catalyst_sample_identity import resolve_sample_identity
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results, summarize_gate_results

logger = logging.getLogger(__name__)
_CHEMISTRY_NORMALIZER = ChemistryNormalizer()
_UNIT_NORMALIZER = UnitNormalizer()
_RDS_SEMANTIC_PATTERNS = (
    re.compile(r"\brds\b", re.IGNORECASE),
    re.compile(r"rate[-\s]?determining step", re.IGNORECASE),
    re.compile(r"rate[-\s]?limiting step", re.IGNORECASE),
    re.compile(r"决速步骤"),
    re.compile(r"限速步骤"),
)
_OVERALL_SRR_GIBBS_PATTERNS = (
    re.compile(r"overall\s+srr", re.IGNORECASE),
    re.compile(r"overall\s+sulfur\s+reduction", re.IGNORECASE),
    re.compile(r"overall.*s8.*li2s", re.IGNORECASE),
    re.compile(r"整体.*自由能"),
)


def _fastapi_default(value: Any) -> Any:
    if value.__class__.__module__.startswith("fastapi.") and hasattr(value, "default"):
        default = value.default
        if str(default) == "PydanticUndefined":
            return None
        return default
    return value


def _optional_text_filter(value: Any) -> str | None:
    value = _fastapi_default(value)
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_int_filter(value: Any) -> int | None:
    value = _fastapi_default(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float_filter(value: Any) -> float | None:
    value = _fastapi_default(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _property_type_filter_clause(column: Any, property_type: str | None):
    aliases = property_type_filter_aliases(property_type)
    if not aliases:
        return None
    return or_(*(column.ilike(f"%{alias}%") for alias in aliases))


def _authors_text(authors) -> str:
    if isinstance(authors, list):
        return ", ".join(str(author) for author in authors if author)
    return authors or ""


def _paper_payload(paper: P) -> dict:
    return {
        "paper_id": str(paper.id),
        "title": paper.title,
        "doi": paper.doi,
        "journal": paper.journal,
        "year": paper.year,
        "authors": paper.authors if isinstance(paper.authors, list) else _authors_text(paper.authors),
    }


def _catalyst_payload(catalyst: CS | None) -> dict | None:
    if catalyst is None:
        return None
    return {
        "catalyst_sample_id": str(catalyst.id),
        "name": catalyst.name,
        "catalyst_type": catalyst.catalyst_type,
        "metal_centers": catalyst.metal_centers,
        "coordination": catalyst.coordination,
        "support": catalyst.support,
        "synthesis_method": catalyst.synthesis_method,
        "evidence_strength": catalyst.evidence_strength,
    }


def _dft_setting_payload(setting: DS) -> dict:
    return {
        "dft_setting_id": str(setting.id),
        "software": setting.software,
        "functional": setting.functional,
        "dispersion_correction": setting.dispersion_correction,
        "pseudopotential": setting.pseudopotential,
        "cutoff_energy_ev": setting.cutoff_energy_ev,
        "k_points": setting.k_points,
        "convergence_settings": setting.convergence_settings,
        "vacuum_thickness_a": setting.vacuum_thickness_a,
        "raw_json": setting.raw_json,
    }


def _dft_rows_statement(
    *,
    property_type: str | None,
    adsorbate: str | None,
    catalyst_type: str | None,
    year_min: int | None,
    year_max: int | None,
    library_name: str | None,
    min_confidence: float | None = None,
    catalyst_name: str | None = None,
):
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    catalyst_type = _optional_text_filter(catalyst_type)
    catalyst_name = _optional_text_filter(catalyst_name)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    min_confidence = _optional_float_filter(min_confidence)
    stmt = select(DR, P).join(P, DR.paper_id == P.id).order_by(P.year.desc().nulls_last(), P.title)
    if property_type:
        property_clause = _property_type_filter_clause(DR.property_type, property_type)
        if property_clause is not None:
            stmt = stmt.where(property_clause)
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if min_confidence is not None:
        stmt = stmt.where(DR.confidence >= min_confidence)
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)
    if library_name is not None:
        stmt = stmt.where(build_library_name_clause(P.library_name, library_name))
    if catalyst_type or catalyst_name:
        stmt = stmt.join(CS, DR.catalyst_sample_id == CS.id)
        if catalyst_type:
            stmt = stmt.where(CS.catalyst_type.ilike(catalyst_type))
        if catalyst_name:
            stmt = stmt.where(CS.name.ilike(f"%{catalyst_name}%"))
    return stmt


def _normalize_energy_value(value: float | None, unit: str | None, property_type: str | None) -> tuple[float | None, str | None]:
    """Backward-compatible energy normalization helper used by existing callers/tests."""
    taxonomy = get_property_taxonomy(property_type)
    if taxonomy["physical_dimension"] != "energy":
        return None, None
    normalized = _UNIT_NORMALIZER.normalize_energy(value, unit)
    return normalized.normalized_value, normalized.normalized_unit or None


def normalize_dft_display_value(value: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """Normalize final DFT display/export values while keeping raw values separately."""
    if value is None:
        return value, unit
    unit_text = str(unit or "").strip()
    unit_key = unit_text.lower().replace(" ", "")
    if unit_key == "mev":
        return value / 1000.0, "eV"
    if unit_key == "ev":
        return value, "eV"
    if "gpu" in unit_key:
        ascii_key = "".join(ch for ch in unit_key if ch.isascii())
        if any(marker in ascii_key for marker in ("10^3", "x10^3", "103")) or (
            ascii_key.startswith("10") and ascii_key != "gpu"
        ):
            return value * 1000.0, "GPU"
        return value, "GPU"
    return value, unit_text or unit


def _normalized_property_type(raw_property_type: str | None) -> str:
    return _CHEMISTRY_NORMALIZER._normalize_property(raw_property_type or "")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tokenize(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _clean_text(value).lower()))


def _iter_strings(payload: Any) -> list[str]:
    values: list[str] = []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        for item in payload.values():
            values.extend(_iter_strings(item))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(_iter_strings(item))
    return values


def _first_non_blank(*values: Any) -> str | None:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return None


def _context_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _context_token(value: Any, *, fallback: str = "unknown") -> str:
    text = (_context_text(value) or fallback).lower()
    text = re.sub(r"[^a-z0-9.+_-]+", "_", text)
    return text.strip("_") or fallback


def _payload_context_layers(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    layers = [payload]
    for key in ("corrected_value", "imported_evidence_payload", "material_identity_payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            layers.append(nested)
    return layers


def _get_payload_value(payload: Any, *keys: str) -> Any:
    for layer in _payload_context_layers(payload):
        for key in keys:
            if key in layer and layer.get(key) not in (None, "", [], {}):
                return layer.get(key)
    return None


def _extract_evidence_context(payload: Any) -> dict[str, Any]:
    target_properties = _get_payload_value(
        payload,
        "descriptor_target_context",
        "target_property_type",
        "target_property_types",
        "associated_property_type",
        "associated_property_types",
    )
    if isinstance(target_properties, list):
        normalized_properties = sorted(
            {
                _normalized_property_type(item)
                for item in target_properties
                if _normalized_property_type(item)
            }
        )
    else:
        normalized_value = _normalized_property_type(target_properties) if target_properties else None
        normalized_properties = [normalized_value] if normalized_value else []
    return {
        "material_identity": _first_non_blank(
            _get_payload_value(payload, "material_identity"),
            _get_payload_value(payload, "material"),
            _get_payload_value(payload, "structure_name"),
        ),
        "material": _context_text(_get_payload_value(payload, "material")),
        "structure_name": _context_text(_get_payload_value(payload, "structure_name", "structure")),
        "surface_facet": _context_text(_get_payload_value(payload, "surface_facet", "facet")),
        "adsorption_site": _context_text(_get_payload_value(payload, "adsorption_site", "site")),
        "coverage": _context_text(_get_payload_value(payload, "coverage")),
        "slab": _context_text(_get_payload_value(payload, "slab")),
        "termination": _context_text(_get_payload_value(payload, "termination")),
        "target_property_types": normalized_properties,
    }


def _material_identity_key(row: DR, catalyst: CS | None, evidence_context: dict[str, Any]) -> str:
    catalyst_id = row.catalyst_sample_id or (catalyst.id if catalyst is not None else None)
    if catalyst_id:
        return f"catalyst_sample:{catalyst_id}"
    material_identity = _first_non_blank(
        evidence_context.get("material_identity"),
        evidence_context.get("material"),
        evidence_context.get("structure_name"),
        catalyst.name if catalyst else None,
        catalyst.coordination if catalyst else None,
    )
    if material_identity:
        return f"material:{material_identity}"
    return f"paper:{row.paper_id}:unlinked"


def _target_context_key(
    *,
    ml_role: str,
    canonical_property_type: str,
    canonical_adsorbate: str | None,
    reaction_step: str | None,
    evidence_context: dict[str, Any],
) -> str:
    if ml_role != "descriptor":
        return canonical_property_type
    target_property_types = evidence_context.get("target_property_types") or []
    if target_property_types:
        return "+".join(sorted(set(target_property_types)))
    instance_specific_values = (
        canonical_adsorbate,
        reaction_step,
        evidence_context.get("adsorption_site"),
        evidence_context.get("coverage"),
        evidence_context.get("surface_facet"),
        evidence_context.get("slab"),
        evidence_context.get("termination"),
    )
    if any(_clean_text(value) for value in instance_specific_values):
        return "descriptor_instance_scope"
    return "descriptor_material_scope"


def _instance_scope_level(target_context_key: str) -> str:
    if target_context_key == "descriptor_material_scope":
        return "material_scope"
    if target_context_key == "descriptor_instance_scope":
        return "instance_scope"
    return "target_context"


def _build_instance_context(
    *,
    row: DR,
    catalyst: CS | None,
    target_payload: dict[str, Any],
    evidence_context: dict[str, Any],
    linked_dft_setting: dict[str, Any] | None,
    setting_link_status: str,
) -> dict[str, Any]:
    material_key = _material_identity_key(row, catalyst, evidence_context)
    setting_binding = (
        f"dft_setting:{linked_dft_setting['dft_setting_id']}"
        if linked_dft_setting and linked_dft_setting.get("dft_setting_id")
        else f"setting_status:{setting_link_status}"
    )
    target_context_key = _target_context_key(
        ml_role=target_payload["ml_role"],
        canonical_property_type=target_payload["canonical_property_type"],
        canonical_adsorbate=target_payload["canonical_adsorbate"],
        reaction_step=target_payload["reaction_step"],
        evidence_context=evidence_context,
    )
    components = {
        "material_key": material_key,
        "canonical_adsorbate": _context_text(target_payload["canonical_adsorbate"]),
        "target_context_key": target_context_key,
        "reaction_step": _context_text(target_payload["reaction_step"]),
        "source_section": _context_text(row.source_section),
        "setting_binding": setting_binding,
        "material_identity": evidence_context.get("material_identity"),
        "material": evidence_context.get("material"),
        "structure_name": evidence_context.get("structure_name"),
        "surface_facet": evidence_context.get("surface_facet"),
        "adsorption_site": evidence_context.get("adsorption_site"),
        "coverage": evidence_context.get("coverage"),
        "slab": evidence_context.get("slab"),
        "termination": evidence_context.get("termination"),
    }
    material_scope_key = "|".join(
        [
            f"material={_context_token(components['material_key'])}",
            f"setting={_context_token(components['setting_binding'])}",
            f"matid={_context_token(components['material_identity'])}",
            f"material_name={_context_token(components['material'])}",
            f"structure={_context_token(components['structure_name'])}",
            f"facet={_context_token(components['surface_facet'])}",
            f"site={_context_token(components['adsorption_site'])}",
            f"coverage={_context_token(components['coverage'])}",
            f"slab={_context_token(components['slab'])}",
            f"termination={_context_token(components['termination'])}",
        ]
    )
    instance_anchor_key = "|".join(
        [
            material_scope_key,
            f"adsorbate={_context_token(components['canonical_adsorbate'])}",
            f"reaction={_context_token(components['reaction_step'])}",
        ]
    )
    instance_key = "|".join(
        [
            instance_anchor_key,
            f"context={_context_token(target_context_key)}",
        ]
    )
    return {
        "material_key": material_key,
        "setting_binding": setting_binding,
        "target_context_key": target_context_key,
        "instance_scope_level": _instance_scope_level(target_context_key),
        "material_scope_key": material_scope_key,
        "instance_anchor_key": instance_anchor_key,
        "instance_key": instance_key,
        "components": components,
    }


def _effective_export_catalyst(
    session: Session,
    *,
    row: DR,
    paper_catalysts: list[CS],
    catalyst_by_id: dict[str, CS],
    evidence_context: dict[str, Any],
) -> tuple[CS | None, str]:
    if row.catalyst_sample_id:
        bound = catalyst_by_id.get(str(row.catalyst_sample_id))
        return bound, "explicit_bound" if bound is not None else "explicit_missing"

    proposed_value = {
        "name": evidence_context.get("material_identity") or evidence_context.get("material"),
        "structure_name": evidence_context.get("structure_name"),
    }
    resolution = resolve_sample_identity(
        session,
        paper_id=row.paper_id,
        proposed_value=proposed_value,
        samples=paper_catalysts,
    )
    if resolution.status == "reuse" and resolution.sample is not None:
        return resolution.sample, "auto_bound"

    if len(paper_catalysts) == 1:
        return paper_catalysts[0], "single_candidate_fallback"

    return None, "unbound"


def _normalize_numeric_target(
    *,
    value: float | None,
    unit: str | None,
    physical_dimension: str,
) -> tuple[float | None, str | None, str, list[str], str | None]:
    if value is None:
        return None, unit, "missing_value", ["missing_numeric_value"], None
    if physical_dimension == "energy":
        normalized = _UNIT_NORMALIZER.normalize_energy(value, unit)
        status = "normalized"
        if normalized.blockers:
            if "energy_basis_requires_explicit_modeling" in normalized.blockers:
                status = "basis_qualified"
            elif "unrecognized_energy_unit" in normalized.blockers:
                status = "unrecognized_unit"
        return (
            normalized.normalized_value,
            normalized.normalized_unit or None,
            status,
            list(normalized.blockers),
            normalized.basis,
        )
    return value, unit, "identity", ([] if unit else ["missing_unit"]), None


def _setting_match_payload(
    setting: DS,
    *,
    property_type: str | None,
    canonical_property_type: str,
    adsorbate: str | None,
    reaction_step: str | None,
    source_section: str | None,
) -> dict[str, Any]:
    payload = setting.raw_json if isinstance(setting.raw_json, dict) else {}
    raw_text = _clean_text(" ".join(_iter_strings(payload))).lower()
    tokens = _tokenize(raw_text)
    score = 0
    reasons: list[str] = []

    property_candidates = {
        _clean_text(property_type).lower(),
        canonical_property_type.lower(),
    } - {""}
    if property_candidates and any(candidate in raw_text for candidate in property_candidates):
        score += 4
        reasons.append("property_match")
    target_properties = {
        _clean_text(item).lower()
        for key in ("property_type", "property_types", "target_property_type", "target_property_types")
        for item in ((payload.get(key) or []) if isinstance(payload.get(key), list) else [payload.get(key)])
        if item
    }
    if property_candidates & target_properties:
        score += 4
        reasons.append("target_property_match")

    adsorbate_candidates = {_clean_text(adsorbate).lower()} - {""}
    target_adsorbates = {
        _clean_text(item).lower()
        for key in ("adsorbate", "adsorbates", "target_adsorbate", "target_adsorbates")
        for item in ((payload.get(key) or []) if isinstance(payload.get(key), list) else [payload.get(key)])
        if item
    }
    if adsorbate_candidates & target_adsorbates:
        score += 3
        reasons.append("adsorbate_match")
    if adsorbate_candidates and adsorbate_candidates & tokens:
        score += 2
        reasons.append("adsorbate_text_match")

    reaction_tokens = _tokenize(reaction_step)
    if reaction_tokens and reaction_tokens & tokens:
        score += 2
        reasons.append("reaction_step_match")

    section_tokens = _tokenize(source_section)
    source_sections = _tokenize(
        " ".join(
            str(item)
            for item in (
                payload.get("section"),
                payload.get("source_section"),
                (payload.get("source_location") or {}).get("section") if isinstance(payload.get("source_location"), dict) else None,
            )
            if item
        )
    )
    if section_tokens and source_sections and section_tokens & source_sections:
        score += 2
        reasons.append("section_match")

    if str(payload.get("setting_scope") or payload.get("scope") or "").lower() == "result":
        score += 1
        reasons.append("result_scoped_setting")

    return {
        "setting": setting,
        "score": score,
        "reasons": reasons,
    }


def _resolve_setting_link(
    row: DR,
    paper_settings: list[DS],
    *,
    canonical_property_type: str,
    canonical_adsorbate: str | None,
) -> dict[str, Any]:
    if not paper_settings:
        return {
            "setting_link_status": "missing",
            "setting_link_reason": "no_paper_settings",
            "linked_dft_setting": None,
            "setting_link_candidates": [],
        }
    if len(paper_settings) == 1:
        return {
            "setting_link_status": "clear_primary",
            "setting_link_reason": "singleton_paper_setting",
            "linked_dft_setting": _dft_setting_payload(paper_settings[0]),
            "setting_link_candidates": [],
        }

    scored = [
        _setting_match_payload(
            setting,
            property_type=row.property_type,
            canonical_property_type=canonical_property_type,
            adsorbate=canonical_adsorbate,
            reaction_step=row.reaction_step,
            source_section=row.source_section,
        )
        for setting in paper_settings
    ]
    positive = [item for item in scored if item["score"] > 0]
    positive.sort(key=lambda item: item["score"], reverse=True)

    if len(positive) == 1:
        winner = positive[0]
        return {
            "setting_link_status": "clear_primary",
            "setting_link_reason": "heuristic_unique_match",
            "linked_dft_setting": _dft_setting_payload(winner["setting"]),
            "setting_link_candidates": [],
        }

    if len(positive) >= 2 and positive[0]["score"] >= 4 and positive[0]["score"] > positive[1]["score"]:
        winner = positive[0]
        return {
            "setting_link_status": "clear_primary",
            "setting_link_reason": "heuristic_highest_score",
            "linked_dft_setting": _dft_setting_payload(winner["setting"]),
            "setting_link_candidates": [],
        }

    candidates = positive if positive else scored
    candidate_payloads = []
    for item in candidates:
        payload = _dft_setting_payload(item["setting"])
        payload["match_score"] = item["score"]
        payload["match_reasons"] = item["reasons"]
        candidate_payloads.append(payload)
    return {
        "setting_link_status": "ambiguous",
        "setting_link_reason": "multiple_or_unmatched_paper_settings",
        "linked_dft_setting": None,
        "setting_link_candidates": candidate_payloads,
    }


def _descriptor_entry(record: dict[str, Any]) -> dict[str, Any]:
    target = record["target"]
    return {
        "record_id": record["record_id"],
        "canonical_property_type": target["canonical_property_type"],
        "property_subtype": target["property_subtype"],
        "value": target["normalized_value"] if target["normalized_value"] is not None else target["value"],
        "unit": target["normalized_unit"] if target["normalized_unit"] else target["unit"],
        "raw_value": target["value"],
        "raw_unit": target["unit"],
        "adsorbate": target["canonical_adsorbate"],
        "setting_link_status": record["setting_link_status"],
        "instance_key": record["sample_context"]["instance_key"],
    }


def _descriptor_assignment_maps(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, set[str]]]:
    assignments: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    ambiguous: dict[str, set[str]] = defaultdict(set)

    targets = [record for record in records if record["target"]["ml_role"] == "target"]
    descriptors = [record for record in records if record["target"]["ml_role"] == "descriptor"]
    targets_by_instance_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    targets_by_instance_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    targets_by_material_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for target in targets:
        sample_context = target["sample_context"]
        targets_by_instance_key[sample_context["instance_key"]].append(target)
        targets_by_instance_anchor[sample_context["instance_anchor_key"]].append(target)
        targets_by_material_scope[sample_context["material_scope_key"]].append(target)

    for descriptor in descriptors:
        descriptor_type = descriptor["target"]["canonical_property_type"]
        sample_context = descriptor["sample_context"]
        exact_targets = targets_by_instance_key.get(sample_context["instance_key"], [])
        if len(exact_targets) == 1:
            assignments[exact_targets[0]["record_id"]][descriptor_type].append(_descriptor_entry(descriptor))
            continue

        scope_level = sample_context["instance_scope_level"]
        target_context_key = sample_context["target_context_key"]
        requires_explicit_target_context = not target_context_key.startswith("descriptor_")
        if scope_level == "instance_scope":
            scope_targets = targets_by_instance_anchor.get(sample_context["instance_anchor_key"], [])
        else:
            scope_targets = targets_by_material_scope.get(sample_context["material_scope_key"], [])

        compatible_targets = [
            target
            for target in scope_targets
            if (not requires_explicit_target_context or target["sample_context"]["target_context_key"] == target_context_key)
        ]

        if len(compatible_targets) == 1:
            assignments[compatible_targets[0]["record_id"]][descriptor_type].append(_descriptor_entry(descriptor))
            continue

        if len(compatible_targets) > 1:
            for target in compatible_targets:
                ambiguous[target["record_id"]].add(descriptor_type)

    return assignments, ambiguous


def _descriptor_fields_for_record(record: dict[str, Any], assignments: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    grouped = assignments.get(record["record_id"], {})
    resolved: dict[str, Any] = {}
    for key, items in grouped.items():
        resolved[key] = items[0] if len(items) == 1 else items
    return resolved


def _has_recommended_ml_setting(record: dict[str, Any]) -> bool:
    """Downstream ML readiness must only trust the result-level linked setting."""
    linked_setting = record.get("linked_dft_setting")
    setting_status = str(record.get("setting_link_status") or "").strip().lower()
    return bool(linked_setting) and setting_status == "clear_primary"


def _ml_blockers_for_record(record: dict[str, Any], ambiguous_descriptor_types: set[str] | None = None) -> list[str]:
    target = record["target"]
    blockers = list(target["normalization_blockers"])
    if target["ml_role"] in {"target", "descriptor"} and target["value"] is None:
        blockers.append("missing_numeric_value")
    if (
        target["ml_role"] in {"target", "descriptor"}
        and target["normalized_value"] is None
        and not target["normalization_blockers"]
    ):
        blockers.append("missing_normalized_value")
    if target["ml_role"] == "target":
        if not _has_recommended_ml_setting(record) and record["setting_link_status"] == "ambiguous":
            blockers.append("ambiguous_result_setting_link")
        elif not _has_recommended_ml_setting(record):
            blockers.append("missing_result_setting_link")
        if target["canonical_property_type"] == "adsorption_energy" and not target["canonical_adsorbate"]:
            blockers.append("missing_canonical_adsorbate")
        if ambiguous_descriptor_types:
            blockers.append("descriptor_instance_ambiguous")
    if target["normalized_unit"] in {None, ""} and target["physical_dimension"] not in {"dimensionless", "text"}:
        blockers.append("missing_unit")
    deduped: list[str] = []
    for blocker in blockers:
        if blocker and blocker not in deduped:
            deduped.append(blocker)
    return deduped


def _ml_readiness_score(blockers: list[str]) -> int:
    weights = {
        "missing_numeric_value": 60,
        "missing_normalized_value": 45,
        "unrecognized_energy_unit": 45,
        "energy_basis_requires_explicit_modeling": 35,
        "descriptor_instance_ambiguous": 35,
        "ambiguous_result_setting_link": 25,
        "missing_result_setting_link": 20,
        "missing_canonical_adsorbate": 20,
        "missing_unit": 20,
    }
    score = 100
    for blocker in blockers:
        score -= weights.get(blocker, 15)
    return max(score, 0)


def _dft_quality_row_payload(row: DR, paper: P, gate) -> dict:
    reasons = list(gate.reasons)
    has_blocking_review_reason = bool({"missing_review", "unsafe_review"} & set(reasons))
    paper_id = str(paper.id)
    return {
        "record_id": str(row.id),
        "paper_id": paper_id,
        "title": paper.title,
        "doi": paper.doi,
        "year": paper.year,
        "property_type": row.property_type,
        "adsorbate": row.adsorbate,
        "value": row.value,
        "unit": row.unit,
        "reaction_step": row.reaction_step,
        "source_section": row.source_section,
        "review_status": gate.review_status,
        "review_gate_status": gate.review_gate_status,
        "provenance_level": gate.provenance_level,
        "locator_status": gate.locator_status,
        "blocked_reasons": reasons,
        "is_exportable": gate.eligible,
        "paper_detail_url": f"../paper_detail/index.html?paper_id={paper_id}",
        "library_detail_url": f"../literature_library/index.html?paper_id={paper_id}&tab=dft",
        "review_workbench_url": (
            f"../external_analysis_workbench/index.html?paper_id={paper_id}"
            if has_blocking_review_reason
            else f"../literature_library/index.html?paper_id={paper_id}&tab=review"
        ),
    }


def build_dft_ml_dataset(
    session: Session,
    *,
    property_type: str | None = None,
    adsorbate: str | None = None,
    catalyst_type: str | None = None,
    catalyst_name: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    min_confidence: float | None = None,
    paper_id: UUID | None = None,
    limit: int | None = None,
    _source_rows: list[tuple[DR, P]] | None = None,
    _gate_by_id: dict[str, Any] | None = None,
    _catalysts: list[CS] | None = None,
    _settings: list[DS] | None = None,
) -> dict:
    """Build a structured ML-ready DFT dataset with safety gates, catalyst info, and normalized units.

    Shared core logic used by both the REST API (/export/dft-dataset) and MCP (export_ml_dataset).
    `limit` caps the number of eligible (gated) records returned.
    """
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    catalyst_name = _optional_text_filter(catalyst_name)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    stmt = _dft_rows_statement(
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
    )
    if paper_id is not None:
        stmt = stmt.where(DR.paper_id == paper_id)

    rows = _source_rows if _source_rows is not None else session.execute(stmt).all()
    gate_results = []
    eligible_rows = []
    paper_ids = set()
    catalyst_sample_ids = set()

    gate_by_id = (
        _gate_by_id
        if _gate_by_id is not None
        else bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")
    )
    for dr, paper in rows:
        gate = gate_by_id.get(str(dr.id))
        if gate is None:
            continue
        gate_results.append(gate)
        if not gate.eligible:
            continue
        eligible_rows.append((dr, paper, gate))
        paper_ids.add(paper.id)
        if dr.catalyst_sample_id:
            catalyst_sample_ids.add(dr.catalyst_sample_id)
        if limit is not None and len(eligible_rows) >= limit:
            break

    catalyst_by_id: dict[str, CS] = {}
    catalysts_by_paper: dict[str, list[CS]] = defaultdict(list)
    settings_by_paper: dict[str, list[DS]] = defaultdict(list)

    if paper_ids:
        catalysts = (
            _catalysts
            if _catalysts is not None
            else session.scalars(select(CS).where(CS.paper_id.in_(paper_ids))).all()
        )
        for catalyst in catalysts:
            catalyst_by_id[str(catalyst.id)] = catalyst
            catalysts_by_paper[str(catalyst.paper_id)].append(catalyst)

        settings = (
            _settings
            if _settings is not None
            else session.scalars(select(DS).where(DS.paper_id.in_(paper_ids))).all()
        )
        for setting in settings:
            settings_by_paper[str(setting.paper_id)].append(setting)

    if catalyst_sample_ids and _catalysts is None:
        direct_catalysts = session.scalars(select(CS).where(CS.id.in_(catalyst_sample_ids))).all()
        for catalyst in direct_catalysts:
            catalyst_by_id[str(catalyst.id)] = catalyst

    records: list[dict[str, Any]] = []
    lm_records: list[dict[str, Any]] = []

    for dr, paper, gate in eligible_rows:
        paper_id_str = str(paper.id)
        paper_catalysts = catalysts_by_paper.get(paper_id_str, [])
        paper_settings = settings_by_paper.get(paper_id_str, [])
        normalized_property_type = _normalized_property_type(dr.property_type)
        taxonomy = get_property_taxonomy(dr.property_type)
        canonical_adsorbate = canonicalize_adsorbate(dr.adsorbate) or dr.adsorbate
        (
            normalized_value,
            normalized_unit,
            normalization_status,
            normalization_blockers,
            normalization_basis,
        ) = _normalize_numeric_target(
            value=dr.value,
            unit=dr.unit,
            physical_dimension=taxonomy["physical_dimension"],
        )
        setting_link = _resolve_setting_link(
            dr,
            paper_settings,
            canonical_property_type=taxonomy["canonical_property_type"],
            canonical_adsorbate=canonical_adsorbate,
        )
        evidence_context = _extract_evidence_context(dr.evidence_payload)
        effective_catalyst, catalyst_binding_source = _effective_export_catalyst(
            session,
            row=dr,
            paper_catalysts=paper_catalysts,
            catalyst_by_id=catalyst_by_id,
            evidence_context=evidence_context,
        )

        common_payload = {
            "record_id": str(dr.id),
            "paper": _paper_payload(paper),
            "catalyst": _catalyst_payload(effective_catalyst),
            "catalyst_candidates": [
                payload
                for payload in (_catalyst_payload(catalyst) for catalyst in paper_catalysts)
                if payload is not None
            ],
            "dft_settings": [_dft_setting_payload(setting) for setting in paper_settings],
            "paper_level_dft_settings": [_dft_setting_payload(setting) for setting in paper_settings],
            "linked_dft_setting": setting_link["linked_dft_setting"],
            "setting_link_status": setting_link["setting_link_status"],
            "setting_link_reason": setting_link["setting_link_reason"],
            "setting_link_candidates": setting_link["setting_link_candidates"],
            "recommended_ml_setting_field": "linked_dft_setting",
            "provenance": {
                "source_section": dr.source_section,
                "source_figure": dr.source_figure,
                "evidence_text": dr.evidence_text,
                "confidence": dr.confidence,
                "review_status": gate.review_status,
                "review_gate_status": gate.review_gate_status,
                "provenance_level": gate.provenance_level,
                "locator_status": gate.locator_status,
                "gate_reasons": list(gate.reasons),
                "safety_gate": "safe_verified_with_required_evidence",
                "evidence_payload": dr.evidence_payload,
                "catalyst_binding_source": catalyst_binding_source,
            },
        }
        target_payload = {
            "property_type": dr.property_type,
            "normalized_property_type": normalized_property_type,
            "canonical_property_type": taxonomy["canonical_property_type"],
            "property_family": taxonomy["property_family"],
            "property_subtype": taxonomy["property_subtype"],
            "physical_dimension": taxonomy["physical_dimension"],
            "ml_role": taxonomy["ml_role"],
            "adsorbate": dr.adsorbate,
            "canonical_adsorbate": canonical_adsorbate,
            "value": dr.value,
            "unit": dr.unit,
            "reaction_step": dr.reaction_step,
            "normalized_value": normalized_value,
            "normalized_unit": normalized_unit,
            "normalization_status": normalization_status,
            "normalization_blockers": normalization_blockers,
            "normalization_basis": normalization_basis,
        }
        instance_context = _build_instance_context(
            row=dr,
            catalyst=effective_catalyst,
            target_payload=target_payload,
            evidence_context=evidence_context,
            linked_dft_setting=setting_link["linked_dft_setting"],
            setting_link_status=setting_link["setting_link_status"],
        )

        if taxonomy["ml_role"] == "lm_auxiliary" or taxonomy["physical_dimension"] == "text" or dr.value is None:
            lm_records.append(
                {
                    **common_payload,
                    "sample_context": {
                        "sample_key": instance_context["instance_key"],
                        "instance_key": instance_context["instance_key"],
                        "instance_anchor_key": instance_context["instance_anchor_key"],
                        "material_scope_key": instance_context["material_scope_key"],
                        "target_context_key": instance_context["target_context_key"],
                        "instance_scope_level": instance_context["instance_scope_level"],
                        "instance_components": instance_context["components"],
                        "history_backfill_applied": True,
                    },
                    "claim": {
                        **target_payload,
                        "evidence_text": dr.evidence_text,
                    },
                }
            )
            continue

        record = {
            **common_payload,
            "target": target_payload,
            "descriptor_fields": {},
            "sample_context": {
                "sample_key": instance_context["instance_key"],
                "instance_key": instance_context["instance_key"],
                "instance_anchor_key": instance_context["instance_anchor_key"],
                "material_scope_key": instance_context["material_scope_key"],
                "target_context_key": instance_context["target_context_key"],
                "instance_scope_level": instance_context["instance_scope_level"],
                "instance_components": instance_context["components"],
                "history_backfill_applied": True,
            },
        }
        records.append(record)

    assignments, ambiguous_descriptors = _descriptor_assignment_maps(records)
    ready_numeric_count = 0
    blocked_numeric_count = 0
    records_by_material_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        records_by_material_scope[record["sample_context"]["material_scope_key"]].append(record)

    for material_scope_key, sample_group in records_by_material_scope.items():
        target_count = sum(1 for record in sample_group if record["target"]["ml_role"] == "target")
        descriptor_count = sum(1 for record in sample_group if record["target"]["ml_role"] == "descriptor")
        for record in sample_group:
            record["descriptor_fields"] = _descriptor_fields_for_record(record, assignments)
            blockers = _ml_blockers_for_record(record, ambiguous_descriptors.get(record["record_id"]))
            readiness = _ml_readiness_score(blockers)
            record["ml_blockers"] = blockers
            record["ml_readiness_score"] = readiness
            record["is_ml_ready"] = not blockers
            record["sample_context"] = {
                **record["sample_context"],
                "numeric_record_count": len(sample_group),
                "target_record_count": target_count,
                "descriptor_record_count": descriptor_count,
                "material_scope_count": len(sample_group),
                "descriptor_instance_ambiguous": bool(ambiguous_descriptors.get(record["record_id"])),
                "history_backfill_applied": True,
            }
            if record["is_ml_ready"]:
                ready_numeric_count += 1
            else:
                blocked_numeric_count += 1

    gate_summary = summarize_gate_results(gate_results)
    logger.info("DFT ML dataset export safety gate summary: %s", gate_summary)
    return {
        "metadata": {
            "dataset_version": "dft-ml-dataset-v0.2",
            "schema_version": "dft_results_ml_v2",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "property_type": property_type,
                "adsorbate": adsorbate,
                "catalyst_type": catalyst_type,
                "year_min": year_min,
                "year_max": year_max,
                "library_name": normalize_library_name(library_name) if library_name is not None else None,
                "min_confidence": min_confidence,
                "paper_id": str(paper_id) if paper_id else None,
            },
            "safety_gate": "safe_verified_with_required_evidence",
            "eligible_count": gate_summary["eligible"],
            "blocked_count": gate_summary["blocked"],
            "blocked_reasons": gate_summary["blocked_reasons"],
            "total_candidates": gate_summary["total_candidates"],
            "numeric_record_count": len(records),
            "numeric_ml_ready_count": ready_numeric_count,
            "numeric_blocked_count": blocked_numeric_count,
            "lm_record_count": len(lm_records),
            "history_backfill_mode": "export_time_enrichment",
            "ml_setting_field": "linked_dft_setting",
        },
        "records": records,
        "lm_records": lm_records,
    }


def _v3_catalyst_family(catalyst: dict[str, Any] | None) -> str | None:
    if not catalyst:
        return None
    parts: list[str] = []
    for key in ("name", "catalyst_type", "metal_centers", "coordination", "support"):
        value = catalyst.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            text = "+".join(sorted(_clean_text(item).lower() for item in value if _clean_text(item)))
        else:
            text = _clean_text(value).lower()
        if text:
            parts.append(f"{key}={text}")
    return "|".join(parts) or None


def _has_rds_semantics(*values: Any) -> bool:
    text = " ".join(_clean_text(value) for value in values if _clean_text(value))
    return any(pattern.search(text) for pattern in _RDS_SEMANTIC_PATTERNS)


def _is_overall_srr_gibbs_record(*values: Any) -> bool:
    text = " ".join(_clean_text(value) for value in values if _clean_text(value))
    return any(pattern.search(text) for pattern in _OVERALL_SRR_GIBBS_PATTERNS)


def _v3_exclusion_reason(
    row: DR,
    profile_key: str,
    allowed_reaction: str,
    allowed_properties: frozenset[str],
) -> str | None:
    reaction_type = _clean_text(row.reaction_type) or "UNKNOWN"
    validation_status = _clean_text(row.reaction_validation_status).lower()
    if reaction_type == "UNKNOWN":
        return "unknown_reaction_type"
    if reaction_type != allowed_reaction:
        return f"reaction_type_{reaction_type}"
    if validation_status != "valid":
        return f"reaction_validation_{validation_status or 'missing'}"
    canonical_property = get_property_taxonomy(row.property_type)["canonical_property_type"]
    if canonical_property not in allowed_properties:
        return "target_property_not_allowed"
    if profile_key == "SRR_LiS:rds_gibbs_free_energy":
        payload_strings = _iter_strings(row.evidence_payload)
        if not _has_rds_semantics(row.reaction_step, row.evidence_text, *payload_strings):
            return "missing_rds_semantics"
        if _is_overall_srr_gibbs_record(row.reaction_step, row.evidence_text, *payload_strings):
            return "overall_srr_free_energy_not_rds"
    return None


def _v3_readiness_input(record: dict[str, Any], row: DR, catalyst_family: str | None) -> dict[str, Any]:
    target = record["target"]
    provenance = record["provenance"]
    catalyst = record.get("catalyst") or {}
    setting = record.get("linked_dft_setting") or {}
    sample_context = record.get("sample_context") or {}
    return {
        "reaction_type": row.reaction_type,
        "reaction_validation_status": row.reaction_validation_status,
        "canonical_property_type": target.get("canonical_property_type"),
        "normalized_value": target.get("normalized_value"),
        "normalized_unit": target.get("normalized_unit"),
        "safety_gate_passed": True,
        "evidence_present": bool(provenance.get("evidence_text")),
        "locator_status": provenance.get("locator_status"),
        "setting_link_status": record.get("setting_link_status"),
        "linked_dft_setting": record.get("linked_dft_setting"),
        "label_blockers": tuple(target.get("normalization_blockers") or ()),
        "paper_id": (record.get("paper") or {}).get("paper_id"),
        "catalyst_id": catalyst.get("catalyst_sample_id"),
        "catalyst_family": catalyst_family,
        "catalyst_type": catalyst.get("catalyst_type"),
        "metal_centers": catalyst.get("metal_centers"),
        "coordination": catalyst.get("coordination"),
        "support": catalyst.get("support"),
        "canonical_adsorbate": target.get("canonical_adsorbate"),
        "reaction_step": target.get("reaction_step"),
        "functional": setting.get("functional"),
        "dispersion_correction": setting.get("dispersion_correction"),
        "pseudopotential": setting.get("pseudopotential"),
        "cutoff_energy_ev": setting.get("cutoff_energy_ev"),
        "k_points": setting.get("k_points"),
        "descriptor_instance_ambiguous": sample_context.get("descriptor_instance_ambiguous") is True,
    }


def build_dft_ml_dataset_v3(
    session: Session,
    *,
    task: str,
    ready_only: bool = False,
    property_type: str | None = None,
    adsorbate: str | None = None,
    catalyst_type: str | None = None,
    catalyst_name: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    min_confidence: float | None = None,
    paper_id: UUID | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a task-scoped v3 JSON dataset by enriching the v2 export payload."""
    profile = get_tabular_task_profile(task)
    limit = _fastapi_default(limit)
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be a non-negative integer or None") from exc
        if limit < 0:
            raise ValueError("limit must be a non-negative integer or None")
    v2 = build_dft_ml_dataset(
        session,
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
        paper_id=paper_id,
        limit=None,
    )
    v2_records = v2["records"]
    ids = [UUID(record["record_id"]) for record in v2_records]
    rows_by_id = {
        str(row.id): row
        for row in session.scalars(select(DR).where(DR.id.in_(ids))).all()
    } if ids else {}
    pages_by_id: dict[str, list[int]] = defaultdict(list)
    if ids:
        spans = session.scalars(
            select(ES).where(
                ES.object_type.in_(("dft_result", "dft_results")),
                ES.object_id.in_([str(value) for value in ids]),
            )
        ).all()
        for span in spans:
            if span.page is not None:
                pages_by_id[span.object_id].append(span.page)

    excluded = Counter()
    candidates: list[dict[str, Any]] = []
    for source_record in v2_records:
        row = rows_by_id.get(source_record["record_id"])
        if row is None:
            excluded["missing_source_record"] += 1
            continue
        reason = _v3_exclusion_reason(
            row,
            profile.key,
            profile.reaction_type,
            profile.allowed_target_properties,
        )
        if reason:
            excluded[reason] += 1
            continue

        record = copy.deepcopy(source_record)
        record["provenance"]["page_locators"] = sorted(set(pages_by_id.get(record["record_id"], [])))
        catalyst_family = _v3_catalyst_family(record.get("catalyst"))
        readiness = evaluate_tabular_readiness(profile, _v3_readiness_input(record, row, catalyst_family))
        record.update(
            {
                "reaction_type": row.reaction_type,
                "reaction_profile_version": row.reaction_profile_version,
                "reaction_validation_status": row.reaction_validation_status,
                **readiness,
                "split_group_values": {
                    "paper_id": record["paper"]["paper_id"],
                    "catalyst_family": catalyst_family,
                },
            }
        )
        record["label_blockers"] = sorted(set(record["label_blockers"]))
        record["feature_blockers"] = sorted(set(record["feature_blockers"]))
        candidates.append(record)

    return_candidates = [
        record for record in candidates
        if not ready_only or (record["label_ready"] and record["tabular_ml_ready"])
    ]
    return_candidates.sort(key=lambda record: record["record_id"])
    returned = return_candidates if limit is None else return_candidates[:limit]
    filters = {
        **v2["metadata"]["filters"],
        "task": profile.key,
        "reaction_type": profile.reaction_type,
        "ready_only": bool(ready_only),
        "limit": limit,
    }
    counts = {
        "source_candidate_count": len(v2_records),
        "candidate_count": len(candidates),
        "task_candidate_count": len(return_candidates),
        "returned_count": len(returned),
        "label_ready_count": sum(bool(record["label_ready"]) for record in candidates),
        "tabular_ready_count": sum(bool(record["tabular_ml_ready"]) for record in candidates),
        "excluded_counts": dict(sorted(excluded.items())),
    }
    created_at = datetime.now(timezone.utc).isoformat()
    contract = {
        "schema_version": "dft_results_ml_v3",
        "dataset_version": "dft-ml-dataset-v0.3",
        "source_schema_version": "dft_results_ml_v2",
        "source_dataset_version": v2["metadata"]["dataset_version"],
        "task": profile.key,
        "task_status": profile.status,
        "task_profile_version": profile.version,
        "reaction_profile": profile.reaction_type,
        "reaction_profile_version": REACTION_PROFILE_VERSION,
        "normalization_version": "dft_results_ml_v2_normalization",
        "created_at": created_at,
        "filters": filters,
        "property_type_fields": [
            "property_type",
            "normalized_property_type",
            "canonical_property_type",
            "property_subtype",
        ],
        "property_type_display_priority": [
            "property_subtype",
            "normalized_property_type",
            "property_type",
            "canonical_property_type",
        ],
        **counts,
    }
    return {
        "metadata": dict(contract),
        "manifest": dict(contract),
        "records": returned,
    }


DFT_ML_DATASET_V3_CSV_COLUMNS = (
    "record_id",
    "paper_id",
    "title",
    "year",
    "catalyst_name",
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "reaction_type",
    "task_profile",
    "property_type",
    "normalized_property_type",
    "canonical_property_type",
    "property_subtype",
    "normalized_value",
    "normalized_unit",
    "raw_value",
    "raw_unit",
    "adsorbate",
    "intermediate",
    "reaction_step",
    "dft_software",
    "dft_functional",
    "evidence_text",
    "page_locators",
    "label_ready",
    "tabular_ml_ready",
    "label_blockers",
    "feature_blockers",
    "split_paper_id",
    "split_catalyst_family",
    "reaction_profile_version",
    "task_profile_version",
)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _v3_csv_row(record: dict[str, Any]) -> dict[str, Any]:
    paper = record.get("paper") or {}
    catalyst = record.get("catalyst") or {}
    target = record.get("target") or {}
    setting = record.get("linked_dft_setting") or {}
    provenance = record.get("provenance") or {}
    split = record.get("split_group_values") or {}
    return {
        "record_id": record.get("record_id"),
        "paper_id": paper.get("paper_id"),
        "title": paper.get("title"),
        "year": paper.get("year"),
        "catalyst_name": catalyst.get("name"),
        "catalyst_type": catalyst.get("catalyst_type"),
        "metal_centers": catalyst.get("metal_centers"),
        "coordination": catalyst.get("coordination"),
        "support": catalyst.get("support"),
        "reaction_type": record.get("reaction_type"),
        "task_profile": record.get("task_profile"),
        "property_type": target.get("property_type"),
        "normalized_property_type": target.get("normalized_property_type"),
        "canonical_property_type": target.get("canonical_property_type"),
        "property_subtype": target.get("property_subtype"),
        "normalized_value": target.get("normalized_value"),
        "normalized_unit": target.get("normalized_unit"),
        "raw_value": target.get("value"),
        "raw_unit": target.get("unit"),
        "adsorbate": target.get("adsorbate"),
        "intermediate": target.get("canonical_adsorbate"),
        "reaction_step": target.get("reaction_step"),
        "dft_software": setting.get("software"),
        "dft_functional": setting.get("functional"),
        "evidence_text": provenance.get("evidence_text"),
        "page_locators": provenance.get("page_locators") or [],
        "label_ready": record.get("label_ready"),
        "tabular_ml_ready": record.get("tabular_ml_ready"),
        "label_blockers": record.get("label_blockers") or [],
        "feature_blockers": record.get("feature_blockers") or [],
        "split_paper_id": split.get("paper_id"),
        "split_catalyst_family": split.get("catalyst_family"),
        "reaction_profile_version": record.get("reaction_profile_version"),
        "task_profile_version": record.get("task_profile_version"),
    }


def build_dft_ml_dataset_v3_csv(
    session: Session,
    *,
    task: str,
    ready_only: bool = True,
    property_type: str | None = None,
    adsorbate: str | None = None,
    catalyst_type: str | None = None,
    catalyst_name: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    min_confidence: float | None = None,
    paper_id: UUID | None = None,
    limit: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a pandas-friendly v3 CSV from the strict v3 dataset contract."""
    payload = build_dft_ml_dataset_v3(
        session,
        task=task,
        ready_only=ready_only,
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
        paper_id=paper_id,
        limit=limit,
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=DFT_ML_DATASET_V3_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in payload["records"]:
        writer.writerow(
            {
                column: _csv_cell(value)
                for column, value in _v3_csv_row(record).items()
            }
        )
    return output.getvalue(), payload["manifest"]


def build_dft_csv_rows(
    session: Session,
    *,
    property_type: str | None = None,
    adsorbate: str | None = None,
    catalyst_type: str | None = None,
    catalyst_name: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    min_confidence: float | None = None,
    paper_id: UUID | None = None,
) -> tuple[str, dict]:
    """Build DFT CSV export as a UTF-8 encoded string, plus gate summary.

    Returns (csv_string, gate_summary_dict).
    """
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    catalyst_name = _optional_text_filter(catalyst_name)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    stmt = _dft_rows_statement(
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
    )
    if paper_id is not None:
        stmt = stmt.where(DR.paper_id == paper_id)
    rows = session.execute(stmt).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "paper_id",
            "title",
            "doi",
            "journal",
            "year",
            "authors",
            "property_type",
            "normalized_property_type",
            "canonical_property_type",
            "property_subtype",
            "adsorbate",
            "value",
            "unit",
            "raw_value",
            "raw_unit",
            "reaction_step",
            "source_section",
            "source_figure",
            "confidence",
            "evidence_text",
            "review_status",
            "review_gate_status",
            "provenance_level",
            "locator_status",
        ]
    )
    gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")
    gate_results = []
    for dr, paper in rows:
        gate = gate_by_id.get(str(dr.id))
        if gate is None:
            continue
        gate_results.append(gate)
        if not gate.eligible:
            continue
        normalized_property_type = _normalized_property_type(dr.property_type)
        taxonomy = get_property_taxonomy(dr.property_type)
        display_value, display_unit = normalize_dft_display_value(dr.value, dr.unit)
        authors_str = ", ".join(paper.authors) if isinstance(paper.authors, list) else (paper.authors or "")
        writer.writerow(
            [
                str(paper.id),
                paper.title or "",
                paper.doi or "",
                paper.journal or "",
                paper.year or "",
                authors_str,
                dr.property_type or "",
                normalized_property_type or "",
                taxonomy["canonical_property_type"],
                taxonomy["property_subtype"] or "",
                dr.adsorbate or "",
                display_value if display_value is not None else "",
                display_unit or "",
                dr.value if dr.value is not None else "",
                dr.unit or "",
                dr.reaction_step or "",
                dr.source_section or "",
                dr.source_figure or "",
                dr.confidence if dr.confidence is not None else "",
                (dr.evidence_text or "").replace("\n", " "),
                gate.review_status,
                gate.review_gate_status,
                gate.provenance_level,
                gate.locator_status,
            ]
        )

    gate_summary = summarize_gate_results(gate_results)
    logger.info("DFT CSV export safety gate summary: %s", gate_summary)
    return output.getvalue(), gate_summary
