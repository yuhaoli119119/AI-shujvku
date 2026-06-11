from __future__ import annotations

import csv
import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import DFTSetting as DS
from app.db.models import Paper as P
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results, summarize_gate_results

logger = logging.getLogger(__name__)


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
    year_min: int | None,
    year_max: int | None,
    library_name: str | None,
):
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    stmt = select(DR, P).join(P, DR.paper_id == P.id).order_by(P.year.desc().nulls_last(), P.title)
    if property_type:
        stmt = stmt.where(DR.property_type.ilike(f"%{property_type}%"))
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)
    if library_name is not None:
        stmt = stmt.where(build_library_name_clause(P.library_name, library_name))
    return stmt


def _normalize_energy_value(value: float | None, unit: str | None, property_type: str | None) -> tuple[float | None, str | None]:
    """Normalize energy values to eV."""
    if value is None or not unit:
        return None, None
    unit_lower = unit.strip().lower()
    if property_type not in {
        "adsorption_energy",
        "formation_energy",
        "binding_energy",
        "reaction_energy",
        "reaction_barrier",
        "gibbs_free_energy_change",
    }:
        return None, None
    if unit_lower in ["kj/mol", "kj mol-1", "kjmol-1"]:
        return value / 96.485, "eV"
    elif unit_lower in ["kcal/mol", "kcal mol-1"]:
        return value / 23.06, "eV"
    elif unit_lower == "mev":
        return value / 1000.0, "eV"
    elif unit_lower == "ev":
        return value, "eV"
    return None, None


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
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    paper_id: UUID | None = None,
    limit: int | None = None,
) -> dict:
    """Build a structured ML-ready DFT dataset with safety gates, catalyst info, and normalized units.

    Shared core logic used by both the REST API (/export/dft-dataset) and MCP (export_ml_dataset).
    `limit` caps the number of eligible (gated) records returned.
    """
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    stmt = _dft_rows_statement(
        property_type=property_type,
        adsorbate=adsorbate,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
    )
    if paper_id is not None:
        stmt = stmt.where(DR.paper_id == paper_id)

    rows = session.execute(stmt).all()
    gate_results = []
    eligible_rows = []
    paper_ids = set()
    catalyst_sample_ids = set()

    gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")
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
        catalysts = session.scalars(select(CS).where(CS.paper_id.in_(paper_ids))).all()
        for catalyst in catalysts:
            catalyst_by_id[str(catalyst.id)] = catalyst
            catalysts_by_paper[str(catalyst.paper_id)].append(catalyst)

        settings = session.scalars(select(DS).where(DS.paper_id.in_(paper_ids))).all()
        for setting in settings:
            settings_by_paper[str(setting.paper_id)].append(setting)

    if catalyst_sample_ids:
        direct_catalysts = session.scalars(select(CS).where(CS.id.in_(catalyst_sample_ids))).all()
        for catalyst in direct_catalysts:
            catalyst_by_id[str(catalyst.id)] = catalyst

    records = []
    for dr, paper, gate in eligible_rows:
        paper_id_str = str(paper.id)
        direct_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
        paper_settings = settings_by_paper.get(paper_id_str, [])

        norm_val, norm_unit = _normalize_energy_value(dr.value, dr.unit, dr.property_type)

        records.append(
            {
                "record_id": str(dr.id),
                "paper": _paper_payload(paper),
                "target": {
                    "property_type": dr.property_type,
                    "adsorbate": dr.adsorbate,
                    "value": dr.value,
                    "unit": dr.unit,
                    "reaction_step": dr.reaction_step,
                    "normalized_value": norm_val,
                    "normalized_unit": norm_unit,
                },
                "catalyst": _catalyst_payload(direct_catalyst),
                "catalyst_candidates": [
                    payload
                    for payload in (_catalyst_payload(catalyst) for catalyst in catalysts_by_paper.get(paper_id_str, []))
                    if payload is not None
                ],
                "dft_settings": [_dft_setting_payload(setting) for setting in paper_settings],
                "provenance": {
                    "source_section": dr.source_section,
                    "source_figure": dr.source_figure,
                    "evidence_text": dr.evidence_text,
                    "confidence": dr.confidence,
                    "review_status": gate.review_status,
                    "review_gate_status": gate.review_gate_status,
                    "provenance_level": gate.provenance_level,
                    "locator_status": gate.locator_status,
                },
            }
        )

    gate_summary = summarize_gate_results(gate_results)
    logger.info("DFT ML dataset export safety gate summary: %s", gate_summary)
    return {
        "metadata": {
            "dataset_version": "dft-ml-dataset-v0.1",
            "schema_version": "dft_results_ml_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "property_type": property_type,
                "adsorbate": adsorbate,
                "year_min": year_min,
                "year_max": year_max,
                "library_name": normalize_library_name(library_name) if library_name is not None else None,
                "paper_id": str(paper_id) if paper_id else None,
            },
            "safety_gate": "safe_verified_with_required_evidence",
            "eligible_count": gate_summary["eligible"],
            "blocked_count": gate_summary["blocked"],
            "blocked_reasons": gate_summary["blocked_reasons"],
            "total_candidates": gate_summary["total_candidates"],
        },
        "records": records,
    }


def build_dft_csv_rows(
    session: Session,
    *,
    property_type: str | None = None,
    adsorbate: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    library_name: str | None = None,
    paper_id: UUID | None = None,
) -> tuple[str, dict]:
    """Build DFT CSV export as a UTF-8 encoded string, plus gate summary.

    Returns (csv_string, gate_summary_dict).
    """
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    stmt = _dft_rows_statement(
        property_type=property_type,
        adsorbate=adsorbate,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
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
            "adsorbate",
            "value",
            "unit",
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
                dr.adsorbate or "",
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
