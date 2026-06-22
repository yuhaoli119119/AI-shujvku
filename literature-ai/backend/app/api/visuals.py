from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    EvidenceLocator,
    FigureDataPoint,
    Paper,
    PaperFigure,
    PaperSection,
    WorkflowJob,
)
from app.db.session import get_db_session
from app.services.dft_export_service import build_dft_ml_dataset
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results
from app.utils.text_cleaning import repair_mojibake_text

router = APIRouter()


ELEMENT_SYMBOLS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca",
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr",
    "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi",
}

REACTION_CATEGORY_ORDER = ["HER", "OER/ORR", "CO2RR", "NRR", "电池/离子", "分子/污染物", "其他"]
TARGET_PROPERTY_ORDER = [
    "adsorption_energy",
    "gibbs_free_energy_change",
    "overpotential",
    "reaction_barrier",
    "limiting_potential",
    "band_gap",
]
DESCRIPTOR_PROPERTY_ORDER = [
    "d_band_center",
    "charge_transfer",
    "bader_charge",
    "work_function",
    "band_gap",
    "metal_electronegativity",
    "coordination_number",
    "bond_length",
    "adsorption_distance",
]
DFT_TARGET_TYPES = {"dft_result", "dft_results"}


def _clean_pdf_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "/uniFB00": "ff",
        "/uniFB01": "fi",
        "/uniFB02": "fl",
        "/uniFB03": "ffi",
        "/uniFB04": "ffl",
        "\u00ee\u0084\u0080": "ff",
        "\u00ee\u0084\u0081": "fi",
        "\u00ee\u0084\u0082": "fl",
        "\u00ee\u0084\u0083": "fi",
        "\u00ee\u0084\u0084": "fl",
        "\ue100": "ff",
        "\ue101": "fi",
        "\ue102": "fl",
        "\ue103": "fi",
        "\ue104": "fl",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = repair_mojibake_text(text) or ""
    return re.sub(r"\s+", " ", text).strip()


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _canonical_property_type(value: Any) -> str:
    raw = _clean_pdf_text(value).lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    raw = re.sub(r"_+", "_", raw).strip("_")
    aliases = {
        "e_ads": "adsorption_energy",
        "eads": "adsorption_energy",
        "adsorption_energy_ev": "adsorption_energy",
        "binding_energy": "adsorption_energy",
        "gibbs_free_energy": "gibbs_free_energy_change",
        "free_energy_change": "gibbs_free_energy_change",
        "delta_g": "gibbs_free_energy_change",
        "reaction_energy_barrier": "reaction_barrier",
        "energy_barrier": "reaction_barrier",
        "d_band": "d_band_center",
        "d_band_centre": "d_band_center",
        "dband_center": "d_band_center",
        "bader": "bader_charge",
        "charge": "bader_charge",
        "efermi_work_function": "work_function",
        "bond_distance": "bond_length",
        "adsorbate_distance": "adsorption_distance",
    }
    return aliases.get(raw, raw)


def _canonical_adsorbate(value: Any) -> tuple[str | None, str, str | None]:
    raw = _clean_pdf_text(value)
    if not raw:
        return None, "未标注吸附物", "missing_adsorbate"

    normalized = raw.replace("−", "-").replace("–", "-").replace("—", "-")
    normalized = normalized.replace("₂", "2").replace("₃", "3").replace("₄", "4")
    normalized = normalized.replace("⁺", "+").replace("⁻", "-")
    normalized = normalized.strip().strip("[](){}.,;:")
    compact = re.sub(r"\s+", "", normalized)
    compact = compact.strip("*")
    lower = compact.lower()
    compact_key = _norm_key(compact)

    invalid_exact = {
        "pbe", "hse06", "hse", "structurechanged", "structurechangedstructurechanged",
        "graphene", "graphdiyne", "graphite", "gdy", "gdn", "position1gdn", "position2gdn",
        "position3gdn", "position4gdn", "n",
    }
    if lower in invalid_exact or compact_key in invalid_exact:
        return None, raw, "non_adsorbate_label"
    if any(token in compact_key for token in ("gdn", "gdy", "graphdiyne", "graphene", "graphite")):
        return None, raw, "non_adsorbate_label"
    if "Î" in raw or "ν" in raw or "nu" == compact_key:
        return None, raw, "non_adsorbate_label"
    if re.match(r"^position\d+[a-z]*$", compact_key):
        return None, raw, "non_adsorbate_label"
    if lower in {"n+", "n-"}:
        return None, raw, "non_adsorbate_label"
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", compact):
        return None, raw, "numeric_label"
    if "=" in compact and not re.search(r"(co2|h2o|h2|o2|n2)", lower):
        return None, raw, "condition_label"
    if len(normalized) > 36 and not re.fullmatch(r"[A-Za-z0-9+*()/-]+", compact):
        return None, raw, "long_non_species_label"

    species_map = {
        "h": "H",
        "h2": "H2",
        "h+": "H+",
        "h2o": "H2O",
        "o": "O",
        "o2": "O2",
        "oh": "OH",
        "ooh": "OOH",
        "h2o2": "H2O2",
        "co2": "CO2",
        "co": "CO",
        "cooh": "COOH",
        "hcoo": "HCOO",
        "hcooh": "HCOOH",
        "cho": "CHO",
        "ch4": "CH4",
        "ch3oh": "CH3OH",
        "n2": "N2",
        "nh3": "NH3",
        "nnh": "NNH",
        "nh2": "NH2",
        "nh": "NH",
        "li": "Li",
        "li+": "Li+",
        "na": "Na",
        "na+": "Na+",
        "k": "K",
        "k+": "K+",
        "mg": "Mg",
        "ca": "Ca",
        "al": "Al",
        "zn": "Zn",
        "dy3": "DY3",
        "r6g": "R6G",
    }
    canonical = species_map.get(lower)
    if canonical is None and re.fullmatch(r"[A-Z][a-z]?[0-9]?[+-]?", normalized):
        canonical = normalized
    if canonical is None:
        canonical = normalized

    category = _adsorbate_category(canonical)
    return canonical, category, None


def _adsorbate_category(species: str) -> str:
    key = species.upper().replace("*", "")
    if key in {"H", "H2", "H+"}:
        return "HER"
    if key in {"O", "O2", "OH", "OOH", "H2O", "H2O2"}:
        return "OER/ORR"
    if key in {"CO2", "CO", "COOH", "HCOO", "HCOOH", "CHO", "CH4", "CH3OH"}:
        return "CO2RR"
    if key in {"N2", "NNH", "NH", "NH2", "NH3"}:
        return "NRR"
    if key in {"LI", "LI+", "NA", "NA+", "K", "K+", "MG", "CA", "AL", "ZN"}:
        return "电池/离子"
    if key in {"DY3", "R6G"}:
        return "分子/污染物"
    return "其他"


def _metal_symbols(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else re.split(r"[-,/;\s]+", str(value or ""))
    metals = []
    for raw in raw_values:
        token = str(raw or "").strip()
        if not token:
            continue
        match = re.fullmatch(r"[A-Z][a-z]?", token)
        if match and token in ELEMENT_SYMBOLS and token not in metals:
            metals.append(token)
    return sorted(metals)


def _canonical_support(sample: CatalystSample | None, paper: Paper | None) -> str:
    support = _clean_pdf_text(getattr(sample, "support", None))
    name = _clean_pdf_text(getattr(sample, "name", None))
    title = _clean_pdf_text(getattr(paper, "title", None))
    combined = f"{support} {name} {title}".lower()
    if any(token in combined for token in ("graphdiyne", "gdy", "gdn", "graphdiynes")):
        if "nanotube" in combined:
            return "graphdiyne nanotube"
        if "nanoribbon" in combined:
            return "graphdiyne nanoribbon"
        return "graphdiyne"
    if "graphene" in combined:
        return "graphene"
    if "cnt" in combined or "carbon nanotube" in combined:
        return "carbon nanotube"
    if "tio2" in combined or "tio₂" in combined:
        return "TiO2"
    return support or name or "未标注载体"


def _canonical_catalyst(sample: CatalystSample | None, paper: Paper | None) -> dict[str, Any]:
    if sample is None:
        return {
            "key": "uncategorized",
            "label": "未标注催化剂",
            "support": "未标注载体",
            "metals": [],
            "raw_names": [],
        }
    metals = _metal_symbols(sample.metal_centers)
    support = _canonical_support(sample, paper)
    raw_name = _clean_pdf_text(sample.name)
    if metals:
        label = "-".join(metals) + " / " + support
    else:
        label = support if support != "未标注载体" else (raw_name or "未标注催化剂")
    return {
        "key": _norm_key(label),
        "label": label,
        "support": support,
        "metals": metals,
        "raw_names": [raw_name] if raw_name else [],
    }


def _build_dft_catalyst_adsorbate_matrix(
    session: Session,
    filters: list[Any],
    *,
    matrix_status: str = "all",
    limit: int = 240,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dft_stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id)
    for clause in filters:
        dft_stmt = dft_stmt.where(clause)
    dft_rows = session.execute(dft_stmt).all()
    if not dft_rows:
        return [], {
            "total_results": 0,
            "reviewed_exportable_results": 0,
            "blocked_candidate_results": 0,
            "included_results": 0,
            "excluded_results": 0,
            "excluded_reasons": {},
            "direct_catalyst_links": 0,
            "paper_level_fallback_links": 0,
            "category_counts": [],
        }

    paper_by_id = {paper.id: paper for _, paper in dft_rows}
    catalysts = session.scalars(select(CatalystSample).where(CatalystSample.paper_id.in_(list(paper_by_id)))).all()
    catalysts_by_id = {item.id: item for item in catalysts}
    catalysts_by_paper: dict[Any, list[CatalystSample]] = defaultdict(list)
    for catalyst in catalysts:
        catalysts_by_paper[catalyst.paper_id].append(catalyst)

    cells: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_reasons: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    direct_links = 0
    fallback_links = 0
    included_results = 0
    reviewed_exportable_results = 0
    blocked_candidate_results = 0
    reviewed_only = (matrix_status or "all").strip().lower() in {"reviewed", "exportable", "trusted"}
    gate_by_id = bulk_export_gate_results(session, [row for row, _paper in dft_rows], target_type="dft_results")

    for dft, paper in dft_rows:
        gate = gate_by_id[str(dft.id)]
        if not gate.eligible:
            blocked_candidate_results += 1
            if reviewed_only:
                for reason in gate.reasons:
                    excluded_reasons[f"candidate_blocked:{reason}"] += 1
                continue
        else:
            reviewed_exportable_results += 1
        adsorbate, category, reason = _canonical_adsorbate(dft.adsorbate)
        if reason or not adsorbate:
            excluded_reasons[reason or "invalid_adsorbate"] += 1
            continue

        matched_catalysts: list[CatalystSample | None]
        match_scope = "direct"
        if dft.catalyst_sample_id and dft.catalyst_sample_id in catalysts_by_id:
            matched_catalysts = [catalysts_by_id[dft.catalyst_sample_id]]
            direct_links += 1
        else:
            matched_catalysts = catalysts_by_paper.get(dft.paper_id) or [None]
            match_scope = "paper_level_fallback" if matched_catalysts[0] is not None else "missing_catalyst"
            fallback_links += 1

        for catalyst in matched_catalysts:
            catalyst_payload = _canonical_catalyst(catalyst, paper)
            key = (catalyst_payload["key"], _norm_key(adsorbate))
            cell = cells.setdefault(
                key,
                {
                    "catalyst": catalyst_payload["label"],
                    "catalyst_key": catalyst_payload["key"],
                    "support": catalyst_payload["support"],
                    "metals": catalyst_payload["metals"],
                    "raw_catalyst_names": set(catalyst_payload["raw_names"]),
                    "adsorbate": adsorbate,
                    "adsorbate_key": _norm_key(adsorbate),
                    "reaction_category": category,
                    "count": 0,
                    "paper_ids": set(),
                    "property_types": Counter(),
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                    "value_sum": 0.0,
                    "value_count": 0,
                    "match_scope_counts": Counter(),
                },
            )
            cell["count"] += 1
            cell["paper_ids"].add(str(dft.paper_id))
            cell["property_types"][dft.property_type or "未标注属性"] += 1
            if dft.confidence is not None:
                cell["confidence_sum"] += float(dft.confidence)
                cell["confidence_count"] += 1
            if dft.value is not None:
                try:
                    cell["value_sum"] += float(dft.value)
                    cell["value_count"] += 1
                except (ValueError, TypeError):
                    pass
            cell["match_scope_counts"][match_scope] += 1
            for raw_name in catalyst_payload["raw_names"]:
                cell["raw_catalyst_names"].add(raw_name)
        category_counts[category] += 1
        included_results += 1

    matrix_rows = []
    for cell in cells.values():
        confidence_count = cell.pop("confidence_count")
        confidence_sum = cell.pop("confidence_sum")
        value_count = cell.pop("value_count")
        value_sum = cell.pop("value_sum")
        paper_ids = cell.pop("paper_ids")
        property_types = cell.pop("property_types")
        match_scope_counts = cell.pop("match_scope_counts")
        raw_catalyst_names = cell.pop("raw_catalyst_names")
        row = {
            **cell,
            "paper_count": len(paper_ids),
            "paper_ids": sorted(list(paper_ids)),
            "property_types": [
                {"property_type": key, "count": value}
                for key, value in property_types.most_common(6)
            ],
            "avg_confidence": round(confidence_sum / confidence_count, 3) if confidence_count else None,
            "avg_value": round(value_sum / value_count, 3) if value_count else None,
            "match_scope_counts": dict(match_scope_counts),
            "raw_catalyst_names": sorted(raw_catalyst_names)[:6],
        }
        matrix_rows.append(row)

    matrix_rows.sort(key=lambda row: (-row["count"], row["reaction_category"], row["catalyst"], row["adsorbate"]))
    meta = {
        "total_results": len(dft_rows),
        "reviewed_exportable_results": reviewed_exportable_results,
        "blocked_candidate_results": blocked_candidate_results,
        "included_results": included_results,
        "excluded_results": sum(excluded_reasons.values()),
        "excluded_reasons": dict(excluded_reasons),
        "matrix_status": "reviewed" if reviewed_only else "all_candidates",
        "candidate_policy": (
            "Reviewed/exportable matrix excludes candidates; candidates remain in review center."
            if reviewed_only
            else "Legacy all-candidate matrix. Do not use for trusted analysis or export."
        ),
        "candidate_overlay_available": not reviewed_only,
        "direct_catalyst_links": direct_links,
        "paper_level_fallback_links": fallback_links,
        "category_counts": [
            {"category": category, "count": category_counts.get(category, 0)}
            for category in REACTION_CATEGORY_ORDER
            if category_counts.get(category, 0)
        ],
        "catalyst_count": len({row["catalyst_key"] for row in matrix_rows}),
        "adsorbate_count": len({row["adsorbate_key"] for row in matrix_rows}),
    }
    return matrix_rows[:limit], meta


def _dft_review_counts(session: Session, filters: list[Any]) -> dict[str, int]:
    stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id)
    for clause in filters:
        stmt = stmt.where(clause)
    rows = session.execute(stmt).all()
    gate_by_id = bulk_export_gate_results(session, [row for row, _paper in rows], target_type="dft_results")
    reviewed = 0
    blocked = 0
    for dft, _paper in rows:
        if gate_by_id[str(dft.id)].eligible:
            reviewed += 1
        else:
            blocked += 1
    return {
        "total": len(rows),
        "reviewed_exportable": reviewed,
        "candidates": blocked,
        "correlation_ready": reviewed,
    }


def _build_descriptor_correlation_summary(
    session: Session,
    filters: list[Any],
    *,
    min_n: int = 5,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
) -> dict[str, Any]:
    points = _descriptor_source_points(
        session,
        filters,
        reaction_category=reaction_category,
        adsorbate=adsorbate,
        material_family=material_family,
    )
    property_counts = Counter(point["property_type"] for point in points)
    cells = []
    for target in TARGET_PROPERTY_ORDER:
        for descriptor in DESCRIPTOR_PROPERTY_ORDER:
            pair_payload = _paired_descriptor_points(points, target, descriptor)
            stats = _correlation_stats(pair_payload)
            n = len(pair_payload)
            status = "ready" if n >= min_n and stats["pearson_r"] is not None else "insufficient_paired_data"
            color = _correlation_color(stats["pearson_r"]) if status == "ready" else "gray"
            cells.append(
                {
                    "target_property": target,
                    "descriptor": descriptor,
                    "n": n,
                    "pearson_r": stats["pearson_r"] if status == "ready" else None,
                    "spearman_rho": stats["spearman_rho"] if status == "ready" else None,
                    "slope": stats["slope"] if status == "ready" else None,
                    "intercept": stats["intercept"] if status == "ready" else None,
                    "status": status,
                    "color": color,
                    "message": (
                        f"已形成 {n} 个同类配对样本。"
                        if status == "ready"
                        else "Correlation is withheld until reviewed records provide paired descriptor/target "
                        f"values with n >= {min_n} under the selected reaction/adsorbate/material filters."
                    ),
                }
            )
    return {
        "schema_version": "descriptor_correlation_v1",
        "min_n": min_n,
        "target_properties": TARGET_PROPERTY_ORDER,
        "descriptor_properties": DESCRIPTOR_PROPERTY_ORDER,
        "cells": cells,
        "property_counts": dict(property_counts),
        "reviewed_numeric_points": len(points),
        "filters": {
            "reaction_category": reaction_category,
            "adsorbate": adsorbate,
            "material_family": material_family,
            "status": "reviewed_exportable",
        },
        "correlation_policy": (
            "Do not mix reaction types, adsorbates, or material families when calculating descriptor correlations. "
            "Unreviewed candidates are excluded by default."
        ),
    }


def _descriptor_source_points(
    session: Session,
    filters: list[Any],
    *,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id)
    for clause in filters:
        stmt = stmt.where(clause)
    rows = session.execute(stmt).all()
    gate_by_id = bulk_export_gate_results(session, [row for row, _paper in rows], target_type="dft_results")
    paper_by_id = {paper.id: paper for _row, paper in rows}
    catalyst_names_by_id = {
        str(row.id): _canonical_catalyst(row, paper_by_id.get(row.paper_id)).get("label", "")
        for row in session.scalars(select(CatalystSample)).all()
    }
    requested_adsorbate_key = _canonical_adsorbate(adsorbate)[0] if adsorbate else None
    requested_family_key = _norm_key(material_family or "")
    points: list[dict[str, Any]] = []
    for row, paper in rows:
        if row.value is None:
            continue
        if not gate_by_id[str(row.id)].eligible:
            continue
        property_type = _canonical_property_type(row.property_type)
        if property_type not in TARGET_PROPERTY_ORDER and property_type not in DESCRIPTOR_PROPERTY_ORDER:
            continue
        adsorbate_key, adsorbate_label, ads_reason = _canonical_adsorbate(row.adsorbate)
        if not adsorbate_key or ads_reason:
            continue
        if requested_adsorbate_key and adsorbate_key != requested_adsorbate_key:
            continue
        category = _adsorbate_category(adsorbate_label)
        if reaction_category and category != reaction_category:
            continue
        catalyst_label = catalyst_names_by_id.get(str(row.catalyst_sample_id or "")) or _paper_material_family(paper)
        if requested_family_key and requested_family_key not in _norm_key(catalyst_label):
            continue
        points.append(
            {
                "row": row,
                "paper": paper,
                "paper_id": str(row.paper_id),
                "paper_title": paper.title,
                "doi": paper.doi,
                "year": paper.year,
                "journal": paper.journal,
                "property_type": property_type,
                "value": float(row.value),
                "unit": row.unit,
                "adsorbate_key": adsorbate_key,
                "adsorbate": adsorbate_label,
                "reaction_category": category,
                "reaction_step": _clean_pdf_text(row.reaction_step).lower(),
                "catalyst_sample_id": str(row.catalyst_sample_id) if row.catalyst_sample_id else "",
                "catalyst": catalyst_label,
                "evidence_text": row.evidence_text,
                "source_section": row.source_section,
                "source_figure": row.source_figure,
                "candidate_status": row.candidate_status,
            }
        )
    return points


def _paper_material_family(paper: Paper) -> str:
    haystack = _clean_pdf_text(" ".join([paper.title or "", paper.abstract or ""])).lower()
    if "single atom" in haystack or "single-atom" in haystack or "sac" in haystack:
        return "SAC-GDY"
    if "doped" in haystack or "doping" in haystack:
        return "doped GDY"
    if "graphdiyne" in haystack or "gdy" in haystack:
        return "graphdiyne"
    return "未标注材料"


def _pair_group_key(point: dict[str, Any]) -> tuple[str, str, str, str]:
    catalyst_key = point.get("catalyst_sample_id") or _norm_key(point.get("catalyst") or "")
    return (
        str(point.get("paper_id") or ""),
        str(catalyst_key or ""),
        str(point.get("adsorbate_key") or ""),
        str(point.get("reaction_step") or ""),
    )


def _paired_descriptor_points(points: list[dict[str, Any]], target: str, descriptor: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        grouped[_pair_group_key(point)][point["property_type"]].append(point)
    pairs: list[dict[str, Any]] = []
    for _key, by_property in grouped.items():
        target_rows = by_property.get(target) or []
        descriptor_rows = by_property.get(descriptor) or []
        if not target_rows or not descriptor_rows:
            continue
        target_point = _median_point(target_rows)
        descriptor_point = _median_point(descriptor_rows)
        if not target_point or not descriptor_point:
            continue
        direct = bool(target_point.get("catalyst_sample_id") and descriptor_point.get("catalyst_sample_id"))
        pairs.append(
            {
                "x": descriptor_point["value"],
                "y": target_point["value"],
                "descriptor_unit": descriptor_point.get("unit"),
                "target_unit": target_point.get("unit"),
                "paper_id": target_point.get("paper_id"),
                "paper_title": target_point.get("paper_title"),
                "doi": target_point.get("doi"),
                "year": target_point.get("year"),
                "journal": target_point.get("journal"),
                "catalyst": target_point.get("catalyst") or descriptor_point.get("catalyst"),
                "adsorbate": target_point.get("adsorbate") or descriptor_point.get("adsorbate"),
                "reaction_category": target_point.get("reaction_category"),
                "reaction_step": target_point.get("reaction_step"),
                "match_scope": "direct_catalyst" if direct else "paper_adsorbate_fallback",
                "target_result_id": str(target_point["row"].id),
                "descriptor_result_id": str(descriptor_point["row"].id),
                "target_evidence": _clean_pdf_text(target_point.get("evidence_text"))[:420],
                "descriptor_evidence": _clean_pdf_text(descriptor_point.get("evidence_text"))[:420],
                "source_section": target_point.get("source_section") or descriptor_point.get("source_section"),
                "source_figure": target_point.get("source_figure") or descriptor_point.get("source_figure"),
            }
        )
    return pairs


def _median_point(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    sorted_rows = sorted(rows, key=lambda item: item["value"])
    return sorted_rows[len(sorted_rows) // 2]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom <= 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / denom


def _rank(values: list[float]) -> list[float]:
    order = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[order[k][0]] = avg_rank
        i = j + 1
    return ranks


def _correlation_stats(points: list[dict[str, Any]]) -> dict[str, float | None]:
    xs = [float(point["x"]) for point in points]
    ys = [float(point["y"]) for point in points]
    pearson = _pearson(xs, ys)
    spearman = _pearson(_rank(xs), _rank(ys)) if len(points) >= 2 else None
    slope = None
    intercept = None
    if pearson is not None:
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom > 0:
            slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
            intercept = mean_y - slope * mean_x
    return {
        "pearson_r": round(pearson, 3) if pearson is not None else None,
        "spearman_rho": round(spearman, 3) if spearman is not None else None,
        "slope": round(slope, 6) if slope is not None else None,
        "intercept": round(intercept, 6) if intercept is not None else None,
    }


def _correlation_color(value: float | None) -> str:
    if value is None:
        return "gray"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _paper_filters(library_name: str | None) -> list[Any]:
    if not library_name:
        return []
    return [build_library_name_clause(Paper.library_name, normalize_library_name(library_name))]


def _count(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def _paper_count(session: Session, library_name: str | None) -> int:
    stmt = select(func.count(Paper.id))
    for clause in _paper_filters(library_name):
        stmt = stmt.where(clause)
    return _count(session, stmt)


def _joined_count(session: Session, model, library_name: str | None) -> int:
    stmt = select(func.count(model.id)).join(Paper, model.paper_id == Paper.id)
    for clause in _paper_filters(library_name):
        stmt = stmt.where(clause)
    return _count(session, stmt)


def _visual_dataset(session: Session, library_name: str | None) -> dict[str, Any]:
    return build_dft_ml_dataset(
        session,
        library_name=normalize_library_name(library_name) if library_name else None,
    )


def _record_target(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("target") or {}


def _record_adsorbate(record: dict[str, Any]) -> str | None:
    target = _record_target(record)
    adsorbate = target.get("canonical_adsorbate") or target.get("adsorbate")
    adsorbate = _clean_pdf_text(adsorbate)
    return adsorbate or None


def _record_reaction_category(record: dict[str, Any]) -> str | None:
    adsorbate = _record_adsorbate(record)
    if not adsorbate:
        return None
    return _adsorbate_category(adsorbate)


def _record_material_family(record: dict[str, Any]) -> str:
    catalyst = record.get("catalyst") or {}
    candidates = record.get("catalyst_candidates") or []
    provenance = record.get("provenance") or {}
    evidence_payload = provenance.get("evidence_payload") or {}
    paper = record.get("paper") or {}
    combined = " ".join(
        str(value or "")
        for value in (
            catalyst.get("support"),
            catalyst.get("name"),
            " ".join(str(item or "") for item in (catalyst.get("metal_centers") or [])),
            " ".join(str(item.get("name") or "") for item in candidates if isinstance(item, dict)),
            paper.get("title"),
            paper.get("journal"),
            evidence_payload.get("material_identity") if isinstance(evidence_payload, dict) else "",
            evidence_payload.get("material") if isinstance(evidence_payload, dict) else "",
            evidence_payload.get("structure_name") if isinstance(evidence_payload, dict) else "",
        )
    ).lower()
    if "single atom" in combined or "single-atom" in combined or "sac" in combined:
        return "SAC-GDY"
    if "doped" in combined or "doping" in combined:
        return "doped GDY"
    if "graphdiyne" in combined or "gdy" in combined or "gdn" in combined:
        return "graphdiyne"
    return "未标注材料"


def _record_catalyst_payload(record: dict[str, Any]) -> dict[str, Any]:
    catalyst = record.get("catalyst") or {}
    candidates = [item for item in (record.get("catalyst_candidates") or []) if isinstance(item, dict)]
    support = _clean_pdf_text(catalyst.get("support"))
    name = _clean_pdf_text(catalyst.get("name"))
    metals = _metal_symbols(catalyst.get("metal_centers") or [])
    raw_names = [name] if name else []
    raw_names.extend(
        _clean_pdf_text(item.get("name"))
        for item in candidates
        if _clean_pdf_text(item.get("name")) and _clean_pdf_text(item.get("name")) not in raw_names
    )
    if metals:
        label = "-".join(metals) + " / " + (support or _record_material_family(record))
    else:
        label = support or name or _record_material_family(record) or "未标注催化剂"
    return {
        "key": _norm_key(label),
        "label": label,
        "support": support or _record_material_family(record),
        "metals": metals,
        "raw_names": [item for item in raw_names if item],
    }


def _binding_scope(record: dict[str, Any]) -> str:
    source = str(((record.get("provenance") or {}).get("catalyst_binding_source") or "")).strip().lower()
    if source in {"explicit_bound", "auto_bound"}:
        return "direct"
    if source in {"single_candidate_fallback"}:
        return "paper_level_fallback"
    return "missing_catalyst"


def _build_dft_catalyst_adsorbate_matrix_v2(
    dataset: dict[str, Any],
    review_counts: dict[str, int],
    *,
    matrix_status: str = "reviewed",
    limit: int = 240,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = dataset.get("records") or []
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    excluded_reasons: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    direct_links = 0
    fallback_links = 0
    missing_links = 0
    included_results = 0

    for record in records:
        target = _record_target(record)
        canonical_property_type = target.get("canonical_property_type")
        if canonical_property_type not in set(TARGET_PROPERTY_ORDER) | set(DESCRIPTOR_PROPERTY_ORDER):
            excluded_reasons["unsupported_property_type"] += 1
            continue
        adsorbate = _record_adsorbate(record)
        if not adsorbate:
            excluded_reasons["missing_canonical_adsorbate"] += 1
            continue
        category = _record_reaction_category(record)
        if not category:
            excluded_reasons["unknown_reaction_category"] += 1
            continue
        catalyst_payload = _record_catalyst_payload(record)
        binding_scope = _binding_scope(record)
        if binding_scope == "direct":
            direct_links += 1
        elif binding_scope == "paper_level_fallback":
            fallback_links += 1
        else:
            missing_links += 1

        key = (catalyst_payload["key"], _norm_key(adsorbate))
        cell = cells.setdefault(
            key,
            {
                "catalyst": catalyst_payload["label"],
                "catalyst_key": catalyst_payload["key"],
                "support": catalyst_payload["support"],
                "metals": catalyst_payload["metals"],
                "raw_catalyst_names": set(catalyst_payload["raw_names"]),
                "adsorbate": adsorbate,
                "adsorbate_key": _norm_key(adsorbate),
                "reaction_category": category,
                "count": 0,
                "paper_ids": set(),
                "property_types": Counter(),
                "confidence_sum": 0.0,
                "confidence_count": 0,
                "value_sum": 0.0,
                "value_count": 0,
                "match_scope_counts": Counter(),
            },
        )
        cell["count"] += 1
        cell["paper_ids"].add(str((record.get("paper") or {}).get("paper_id") or ""))
        cell["property_types"][canonical_property_type or "未标注属性"] += 1
        confidence = ((record.get("provenance") or {}).get("confidence"))
        if confidence is not None:
            cell["confidence_sum"] += float(confidence)
            cell["confidence_count"] += 1
        if canonical_property_type == "adsorption_energy":
            value = target.get("normalized_value")
            if value is None:
                value = target.get("value")
            if value is not None:
                cell["value_sum"] += float(value)
                cell["value_count"] += 1
        cell["match_scope_counts"][binding_scope] += 1
        for raw_name in catalyst_payload["raw_names"]:
            cell["raw_catalyst_names"].add(raw_name)
        category_counts[category] += 1
        included_results += 1

    matrix_rows = []
    for cell in cells.values():
        confidence_count = cell.pop("confidence_count")
        confidence_sum = cell.pop("confidence_sum")
        value_count = cell.pop("value_count")
        value_sum = cell.pop("value_sum")
        paper_ids = {pid for pid in cell.pop("paper_ids") if pid}
        property_types = cell.pop("property_types")
        match_scope_counts = cell.pop("match_scope_counts")
        raw_catalyst_names = cell.pop("raw_catalyst_names")
        matrix_rows.append(
            {
                **cell,
                "paper_count": len(paper_ids),
                "paper_ids": sorted(list(paper_ids)),
                "property_types": [
                    {"property_type": key, "count": value}
                    for key, value in property_types.most_common(6)
                ],
                "avg_confidence": round(confidence_sum / confidence_count, 3) if confidence_count else None,
                "avg_value": round(value_sum / value_count, 3) if value_count else None,
                "match_scope_counts": dict(match_scope_counts),
                "raw_catalyst_names": sorted(raw_catalyst_names)[:6],
            }
        )

    matrix_rows.sort(key=lambda row: (-row["count"], row["reaction_category"], row["catalyst"], row["adsorbate"]))
    meta = {
        "total_results": review_counts["total"],
        "reviewed_exportable_results": review_counts["reviewed_exportable"],
        "blocked_candidate_results": review_counts["candidates"],
        "included_results": included_results,
        "excluded_results": sum(excluded_reasons.values()),
        "excluded_reasons": dict(excluded_reasons),
        "matrix_status": "reviewed",
        "candidate_policy": "Matrix is derived from the current exportable DFT dataset logic only.",
        "candidate_overlay_available": False,
        "direct_catalyst_links": direct_links,
        "paper_level_fallback_links": fallback_links,
        "missing_catalyst_links": missing_links,
        "category_counts": [
            {"category": category, "count": category_counts.get(category, 0)}
            for category in REACTION_CATEGORY_ORDER
            if category_counts.get(category, 0)
        ],
        "catalyst_count": len({row["catalyst_key"] for row in matrix_rows}),
        "adsorbate_count": len({row["adsorbate_key"] for row in matrix_rows}),
        "dataset_schema_version": ((dataset.get("metadata") or {}).get("schema_version")),
    }
    return matrix_rows[:limit], meta


def _filtered_target_records_v2(
    dataset: dict[str, Any],
    *,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
) -> list[dict[str, Any]]:
    requested_adsorbate_key = _canonical_adsorbate(adsorbate)[0] if adsorbate else None
    requested_family_key = _norm_key(material_family or "")
    filtered: list[dict[str, Any]] = []
    for record in dataset.get("records") or []:
        target = _record_target(record)
        if target.get("ml_role") != "target":
            continue
        canonical_property_type = target.get("canonical_property_type")
        if canonical_property_type not in TARGET_PROPERTY_ORDER:
            continue
        canonical_adsorbate = _record_adsorbate(record)
        if not canonical_adsorbate:
            continue
        if requested_adsorbate_key and canonical_adsorbate != requested_adsorbate_key:
            continue
        category = _record_reaction_category(record)
        if reaction_category and category != reaction_category:
            continue
        family = _record_material_family(record)
        if requested_family_key and requested_family_key not in _norm_key(family):
            continue
        filtered.append(record)
    return filtered


def _descriptor_match_scope(target_record: dict[str, Any], descriptor_record: dict[str, Any]) -> str:
    target_context = target_record.get("sample_context") or {}
    descriptor_context = descriptor_record.get("sample_context") or {}
    if target_context.get("instance_key") == descriptor_context.get("instance_key"):
        return "direct_catalyst"
    if target_context.get("instance_anchor_key") == descriptor_context.get("instance_anchor_key"):
        return "direct_catalyst"
    return "paper_adsorbate_fallback"


def _paired_descriptor_points_v2(
    dataset: dict[str, Any],
    *,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
    target_property: str,
    descriptor: str,
) -> tuple[list[dict[str, Any]], int]:
    records_by_id = {
        record.get("record_id"): record
        for record in dataset.get("records") or []
        if record.get("record_id")
    }
    filtered_targets = _filtered_target_records_v2(
        dataset,
        reaction_category=reaction_category,
        adsorbate=adsorbate,
        material_family=material_family,
    )
    pairs: list[dict[str, Any]] = []
    for record in filtered_targets:
        target = _record_target(record)
        if target.get("canonical_property_type") != target_property:
            continue
        descriptor_field = (record.get("descriptor_fields") or {}).get(descriptor)
        if not isinstance(descriptor_field, dict):
            continue
        descriptor_record = records_by_id.get(descriptor_field.get("record_id"))
        if not descriptor_record:
            continue
        descriptor_target = _record_target(descriptor_record)
        if descriptor_target.get("canonical_property_type") != descriptor:
            continue
        x = descriptor_field.get("value")
        if x is None:
            x = descriptor_target.get("normalized_value")
        if x is None:
            x = descriptor_target.get("value")
        y = target.get("normalized_value")
        if y is None:
            y = target.get("value")
        if x is None or y is None:
            continue
        paper = record.get("paper") or {}
        catalyst = record.get("catalyst") or {}
        pairs.append(
            {
                "x": float(x),
                "y": float(y),
                "descriptor_unit": descriptor_field.get("unit") or descriptor_target.get("normalized_unit") or descriptor_target.get("unit"),
                "target_unit": target.get("normalized_unit") or target.get("unit"),
                "paper_id": paper.get("paper_id"),
                "paper_title": paper.get("title"),
                "doi": paper.get("doi"),
                "year": paper.get("year"),
                "journal": paper.get("journal"),
                "catalyst": catalyst.get("name") or _record_catalyst_payload(record).get("label"),
                "adsorbate": _record_adsorbate(record),
                "reaction_category": _record_reaction_category(record),
                "reaction_step": _clean_pdf_text(target.get("reaction_step")).lower(),
                "match_scope": _descriptor_match_scope(record, descriptor_record),
                "target_result_id": record.get("record_id"),
                "descriptor_result_id": descriptor_record.get("record_id"),
                "target_evidence": _clean_pdf_text(((record.get("provenance") or {}).get("evidence_text")))[:420],
                "descriptor_evidence": _clean_pdf_text(((descriptor_record.get("provenance") or {}).get("evidence_text")))[:420],
                "source_section": ((record.get("provenance") or {}).get("source_section") or (descriptor_record.get("provenance") or {}).get("source_section")),
                "source_figure": ((record.get("provenance") or {}).get("source_figure") or (descriptor_record.get("provenance") or {}).get("source_figure")),
            }
        )
    return pairs, len(filtered_targets)


def _median_numeric(values: list[float]) -> float | None:
    finite = sorted(float(value) for value in values if isinstance(value, int | float) and math.isfinite(float(value)))
    if not finite:
        return None
    middle = len(finite) // 2
    if len(finite) % 2:
        return finite[middle]
    return (finite[middle - 1] + finite[middle]) / 2


def _raw_exploratory_descriptor_points(
    session: Session,
    filters: list[Any],
    *,
    target_property: str,
    descriptor: str,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
) -> list[dict[str, Any]]:
    """Exploratory fallback for visualization when strict export pairing is empty."""
    requested_adsorbate = _canonical_adsorbate(adsorbate)[0] if adsorbate else None
    requested_family = _norm_key(material_family or "")
    stmt = (
        select(DFTResult, Paper, CatalystSample)
        .join(Paper, DFTResult.paper_id == Paper.id)
        .outerjoin(CatalystSample, DFTResult.catalyst_sample_id == CatalystSample.id)
        .where(DFTResult.value.is_not(None))
        .where(func.lower(func.coalesce(DFTResult.candidate_status, "")) != "rejected")
    )
    for clause in filters:
        stmt = stmt.where(clause)

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"targets": [], "descriptors": []})
    for result, paper, catalyst in session.execute(stmt).all():
        prop = _canonical_property_type(result.property_type)
        if prop not in {target_property, descriptor}:
            continue
        result_adsorbate = _canonical_adsorbate(result.adsorbate)[0]
        if requested_adsorbate and result_adsorbate != requested_adsorbate:
            continue
        category = _adsorbate_category(result_adsorbate) if result_adsorbate else "其他"
        if reaction_category and category != reaction_category:
            continue
        catalyst_payload = _canonical_catalyst(catalyst, paper)
        family = catalyst_payload.get("family") or catalyst_payload.get("label") or catalyst_payload.get("name") or ""
        if requested_family and requested_family not in _norm_key(family):
            continue
        group_key = str(result.catalyst_sample_id or result.paper_id)
        entry = {
            "value": float(result.value),
            "unit": result.unit,
            "result": result,
            "paper": paper,
            "catalyst": catalyst_payload,
            "adsorbate": result_adsorbate,
            "category": category,
        }
        if prop == target_property:
            grouped[group_key]["targets"].append(entry)
        else:
            grouped[group_key]["descriptors"].append(entry)

    points: list[dict[str, Any]] = []
    for group in grouped.values():
        targets = group["targets"]
        descriptors = group["descriptors"]
        if not targets or not descriptors:
            continue
        for target_entry in targets:
            compatible = [
                item
                for item in descriptors
                if not item.get("adsorbate")
                or item.get("adsorbate") == "none"
                or not target_entry.get("adsorbate")
                or item.get("adsorbate") == target_entry.get("adsorbate")
            ]
            if not compatible:
                compatible = descriptors
            x = _median_numeric([item["value"] for item in compatible])
            y = float(target_entry["value"])
            if x is None or not math.isfinite(y):
                continue
            descriptor_entry = compatible[0]
            paper = target_entry["paper"]
            target_result = target_entry["result"]
            descriptor_result = descriptor_entry["result"]
            points.append(
                {
                    "x": x,
                    "y": y,
                    "descriptor_unit": descriptor_entry.get("unit"),
                    "target_unit": target_entry.get("unit"),
                    "paper_id": str(paper.id),
                    "paper_title": paper.title,
                    "doi": paper.doi,
                    "year": paper.year,
                    "journal": paper.journal,
                    "catalyst": (target_entry.get("catalyst") or {}).get("label"),
                    "adsorbate": target_entry.get("adsorbate"),
                    "reaction_category": target_entry.get("category"),
                    "reaction_step": _clean_pdf_text(target_result.reaction_step).lower(),
                    "match_scope": "exploratory_same_sample",
                    "target_result_id": str(target_result.id),
                    "descriptor_result_id": str(descriptor_result.id),
                    "target_evidence": _clean_pdf_text(target_result.evidence_text)[:420],
                    "descriptor_evidence": _clean_pdf_text(descriptor_result.evidence_text)[:420],
                    "source_section": target_result.source_section or descriptor_result.source_section,
                    "source_figure": target_result.source_figure or descriptor_result.source_figure,
                }
            )
    return points


def _build_descriptor_correlation_summary_v2(
    dataset: dict[str, Any],
    session: Session,
    filters: list[Any],
    *,
    min_n: int = 3,
    reaction_category: str | None = None,
    adsorbate: str | None = None,
    material_family: str | None = None,
) -> dict[str, Any]:
    filtered_targets = _filtered_target_records_v2(
        dataset,
        reaction_category=reaction_category,
        adsorbate=adsorbate,
        material_family=material_family,
    )
    cells = []
    property_counts: Counter[str] = Counter()
    for record in filtered_targets:
        property_counts[_record_target(record).get("canonical_property_type") or ""] += 1
    for target in TARGET_PROPERTY_ORDER:
        for descriptor in DESCRIPTOR_PROPERTY_ORDER:
            pair_payload, _ = _paired_descriptor_points_v2(
                dataset,
                reaction_category=reaction_category,
                adsorbate=adsorbate,
                material_family=material_family,
                target_property=target,
                descriptor=descriptor,
            )
            source = "reviewed_exportable"
            if not pair_payload:
                pair_payload = _raw_exploratory_descriptor_points(
                    session,
                    filters,
                    target_property=target,
                    descriptor=descriptor,
                    reaction_category=reaction_category,
                    adsorbate=adsorbate,
                    material_family=material_family,
                )
                if pair_payload:
                    source = "exploratory_non_rejected"
            stats = _correlation_stats(pair_payload)
            n = len(pair_payload)
            status = "ready" if n >= min_n and stats["pearson_r"] is not None else "insufficient_paired_data"
            color = _correlation_color(stats["pearson_r"]) if status == "ready" else "gray"
            cells.append(
                {
                    "target_property": target,
                    "descriptor": descriptor,
                    "n": n,
                    "pearson_r": stats["pearson_r"] if status == "ready" else None,
                    "spearman_rho": stats["spearman_rho"] if status == "ready" else None,
                    "slope": stats["slope"] if status == "ready" else None,
                    "intercept": stats["intercept"] if status == "ready" else None,
                    "status": status,
                    "color": color,
                    "source": source,
                    "message": (
                        f"已形成 {n} 个最新导出逻辑下的配对样本。"
                        if status == "ready" and source == "reviewed_exportable"
                        else f"已形成 {n} 个同样本探索性配对样本，已排除 Rejected。"
                        if status == "ready"
                        else "Correlation is withheld until the current ML dataset logic yields paired descriptor/target "
                        f"values with n >= {min_n} under the selected reaction/adsorbate/material filters."
                    ),
                }
            )
    return {
        "schema_version": "descriptor_correlation_v2",
        "min_n": min_n,
        "target_properties": TARGET_PROPERTY_ORDER,
        "descriptor_properties": DESCRIPTOR_PROPERTY_ORDER,
        "cells": cells,
        "property_counts": dict(property_counts),
        "reviewed_numeric_points": len(filtered_targets),
        "filters": {
            "reaction_category": reaction_category,
            "adsorbate": adsorbate,
            "material_family": material_family,
            "status": "reviewed_exportable",
            "dataset_schema_version": (dataset.get("metadata") or {}).get("schema_version"),
        },
        "correlation_policy": (
            "Correlations are derived from the current DFT ML dataset export logic. "
            "Only reviewed/exportable numeric rows are considered, and descriptor matching follows instance/material scope rules."
        ),
    }


@router.get("/overview")
async def visualization_overview(
    library_name: str | None = Query(default=None),
    matrix_status: str = Query(default="all", description="all or reviewed; reviewed uses the current exportable dataset logic"),
    corr_reaction: str | None = Query(default=None),
    corr_adsorbate: str | None = Query(default=None),
    corr_family: str | None = Query(default=None),
    corr_min_n: int = Query(default=3, ge=3, le=50),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    filters = _paper_filters(library_name)

    total_papers = _paper_count(session, library_name)
    pdf_stmt = select(func.count(Paper.id)).where(Paper.pdf_path.is_not(None), Paper.pdf_path != "")
    parsed_stmt = (
        select(func.count(func.distinct(PaperSection.paper_id)))
        .join(Paper, PaperSection.paper_id == Paper.id)
    )
    for clause in filters:
        pdf_stmt = pdf_stmt.where(clause)
        parsed_stmt = parsed_stmt.where(clause)

    years = []
    year_stmt = select(Paper.year, func.count(Paper.id)).group_by(Paper.year).order_by(Paper.year.desc())
    for clause in filters:
        year_stmt = year_stmt.where(clause)
    for year, count in session.execute(year_stmt).all():
        years.append({"year": year, "count": int(count or 0)})

    journals = []
    journal_stmt = (
        select(Paper.journal, func.count(Paper.id))
        .group_by(Paper.journal)
        .order_by(func.count(Paper.id).desc())
        .limit(12)
    )
    for clause in filters:
        journal_stmt = journal_stmt.where(clause)
    for journal, count in session.execute(journal_stmt).all():
        journals.append({"journal": journal or "未记录期刊", "count": int(count or 0)})

    type_counts: Counter[str] = Counter()
    type_stmt = select(Paper.paper_type, func.count(Paper.id)).group_by(Paper.paper_type)
    for clause in filters:
        type_stmt = type_stmt.where(clause)
    for paper_type, count in session.execute(type_stmt).all():
        key = str(paper_type or "Unknown").strip() or "Unknown"
        type_counts[key] += int(count or 0)

    dft_review_counts = _dft_review_counts(session, filters)
    dataset = _visual_dataset(session, library_name)
    reviewed_only = (matrix_status or "all").strip().lower() in {"reviewed", "exportable", "trusted"}
    if reviewed_only:
        matrix_rows, matrix_meta = _build_dft_catalyst_adsorbate_matrix_v2(
            dataset,
            dft_review_counts,
            matrix_status=matrix_status,
        )
    else:
        matrix_rows, matrix_meta = _build_dft_catalyst_adsorbate_matrix(session, filters, matrix_status=matrix_status)
    descriptor_correlation = _build_descriptor_correlation_summary_v2(
        dataset,
        session,
        filters,
        min_n=corr_min_n,
        reaction_category=corr_reaction,
        adsorbate=corr_adsorbate,
        material_family=corr_family,
    )

    dft_status = []
    status_stmt = (
        select(DFTResult.candidate_status, func.count(DFTResult.id))
        .join(Paper, DFTResult.paper_id == Paper.id)
        .group_by(DFTResult.candidate_status)
        .order_by(func.count(DFTResult.id).desc())
    )
    for clause in filters:
        status_stmt = status_stmt.where(clause)
    for status, count in session.execute(status_stmt).all():
        dft_status.append({"status": status or "unknown", "count": int(count or 0)})

    tasks = []
    task_stmt = select(WorkflowJob).order_by(WorkflowJob.created_at.desc()).limit(12)
    if library_name:
        task_stmt = task_stmt.where(build_library_name_clause(WorkflowJob.library_name, library_name))
    for job in session.scalars(task_stmt).all():
        tasks.append(
            {
                "job_id": job.job_id,
                "type": job.type,
                "status": job.status,
                "title": (job.payload or {}).get("title") if isinstance(job.payload, dict) else None,
                "action": (job.payload or {}).get("action") if isinstance(job.payload, dict) else None,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
        )

    paper_lookup = {}
    papers_query = select(Paper.id, Paper.title, Paper.journal, Paper.year, Paper.doi)
    for clause in filters:
        papers_query = papers_query.where(clause)
    for p_id, p_title, p_journal, p_year, p_doi in session.execute(papers_query).all():
        paper_lookup[str(p_id)] = {
            "title": p_title or "无标题",
            "journal": p_journal or "未知期刊",
            "year": p_year,
            "doi": p_doi or "",
        }

    return {
        "library_name": normalize_library_name(library_name) if library_name else None,
        "paper_lookup": paper_lookup,
        "summary": {
            "papers": total_papers,
            "pdf_available": _count(session, pdf_stmt),
            "parsed_papers": _count(session, parsed_stmt),
            "figures": _joined_count(session, PaperFigure, library_name),
            "figure_data_points": _joined_count(session, FigureDataPoint, library_name),
            "dft_settings": _joined_count(session, DFTSetting, library_name),
            "catalyst_samples": _joined_count(session, CatalystSample, library_name),
            "dft_results": _joined_count(session, DFTResult, library_name),
            "reviewed_exportable_dft_results": dft_review_counts["reviewed_exportable"],
            "candidate_dft_results": dft_review_counts["candidates"],
            "correlation_ready_dft_results": dft_review_counts["correlation_ready"],
        },
        "years": years,
        "journals": journals,
        "paper_types": [{"type": key, "count": value} for key, value in sorted(type_counts.items())],
        "dft_matrix": matrix_rows,
        "dft_matrix_meta": matrix_meta,
        "descriptor_correlation": descriptor_correlation,
        "dft_status": dft_status,
        "recent_tasks": tasks,
    }


@router.get("/correlation-pairs")
async def descriptor_correlation_pairs(
    target_property: str = Query(...),
    descriptor: str = Query(...),
    library_name: str | None = Query(default=None),
    reaction_category: str | None = Query(default=None),
    adsorbate: str | None = Query(default=None),
    material_family: str | None = Query(default=None),
    min_n: int = Query(default=3, ge=3, le=50),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    target = _canonical_property_type(target_property)
    descriptor_key = _canonical_property_type(descriptor)
    dataset = _visual_dataset(session, library_name)
    pairs, filtered_target_count = _paired_descriptor_points_v2(
        dataset,
        reaction_category=reaction_category,
        adsorbate=adsorbate,
        material_family=material_family,
        target_property=target,
        descriptor=descriptor_key,
    )
    source = "reviewed_exportable"
    if not pairs:
        filters = _paper_filters(library_name)
        pairs = _raw_exploratory_descriptor_points(
            session,
            filters,
            target_property=target,
            descriptor=descriptor_key,
            reaction_category=reaction_category,
            adsorbate=adsorbate,
            material_family=material_family,
        )
        if pairs:
            source = "exploratory_non_rejected"
    stats = _correlation_stats(pairs)
    ready = len(pairs) >= min_n and stats["pearson_r"] is not None
    return {
        "schema_version": "descriptor_scatter_v2",
        "target_property": target,
        "descriptor": descriptor_key,
        "library_name": normalize_library_name(library_name) if library_name else None,
        "filters": {
            "reaction_category": reaction_category,
            "adsorbate": adsorbate,
            "material_family": material_family,
            "status": "reviewed_exportable",
            "source": source,
            "filtered_target_count": filtered_target_count,
            "dataset_schema_version": (dataset.get("metadata") or {}).get("schema_version"),
        },
        "min_n": min_n,
        "n": len(pairs),
        "ready": ready,
        "source": source,
        "pearson_r": stats["pearson_r"] if ready else None,
        "spearman_rho": stats["spearman_rho"] if ready else None,
        "slope": stats["slope"] if ready else None,
        "intercept": stats["intercept"] if ready else None,
        "points": pairs,
        "policy": (
            "Scatter pairs prefer the current reviewed/exportable DFT ML dataset. "
            "When strict descriptor assignment is empty, the visualization uses same-sample non-rejected exploratory pairs."
        ),
    }
