from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import DFTSetting as DS
from app.db.models import Paper as P
from app.db.session import get_db_session
from app.utils.review_safety import is_export_eligible_extraction, summarize_gate_results

router = APIRouter()
logger = logging.getLogger(__name__)


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
):
    stmt = select(DR, P).join(P, DR.paper_id == P.id).order_by(P.year.desc().nulls_last(), P.title)
    if property_type:
        stmt = stmt.where(DR.property_type.ilike(f"%{property_type}%"))
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)
    return stmt


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


@router.get("/export/csv")
async def export_dft_results_csv(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    session: Session = Depends(get_db_session),
):
    rows = session.execute(
        _dft_rows_statement(
            property_type=property_type,
            adsorbate=adsorbate,
            year_min=year_min,
            year_max=year_max,
        )
    ).all()
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
    gate_results = []
    for dr, paper in rows:
        gate = is_export_eligible_extraction(session, dr, target_type="dft_results")
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
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=dft_results_export.csv",
            "X-D3-Export-Safety-Gate": "safe_verified_with_required_evidence",
            "X-D3-Export-Count": str(gate_summary["eligible"]),
            "X-D3-Block-Count": str(gate_summary["blocked"]),
            "X-D1-Exported-Count": str(gate_summary["eligible"]),
            "X-D1-Blocked-Count": str(gate_summary["blocked"]),
            "X-D1-Blocked-Reasons": json.dumps(gate_summary["blocked_reasons"], sort_keys=True),
        },
    )


@router.get("/export/dft-dataset")
async def export_dft_dataset(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    session: Session = Depends(get_db_session),
):
    rows = session.execute(
        _dft_rows_statement(
            property_type=property_type,
            adsorbate=adsorbate,
            year_min=year_min,
            year_max=year_max,
        )
    ).all()
    gate_results = []
    eligible_rows = []
    paper_ids = set()
    catalyst_sample_ids = set()

    for dr, paper in rows:
        gate = is_export_eligible_extraction(session, dr, target_type="dft_results")
        gate_results.append(gate)
        if not gate.eligible:
            continue
        eligible_rows.append((dr, paper, gate))
        paper_ids.add(paper.id)
        if dr.catalyst_sample_id:
            catalyst_sample_ids.add(dr.catalyst_sample_id)

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
        paper_id = str(paper.id)
        direct_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
        fallback_catalyst = catalysts_by_paper.get(paper_id, [None])[0]
        primary_catalyst = direct_catalyst or fallback_catalyst
        paper_settings = settings_by_paper.get(paper_id, [])

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
                },
                "catalyst": _catalyst_payload(primary_catalyst),
                "catalyst_candidates": [
                    payload
                    for payload in (_catalyst_payload(catalyst) for catalyst in catalysts_by_paper.get(paper_id, []))
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
            },
            "safety_gate": "safe_verified_with_required_evidence",
            "eligible_count": gate_summary["eligible"],
            "blocked_count": gate_summary["blocked"],
            "blocked_reasons": gate_summary["blocked_reasons"],
            "total_candidates": gate_summary["total_candidates"],
        },
        "records": records,
    }


@router.get("/export/dft-quality")
async def dft_dataset_quality(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    reason: str | None = Query(default=None, description="Optional blocked reason filter"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
):
    rows = session.execute(
        _dft_rows_statement(
            property_type=property_type,
            adsorbate=adsorbate,
            year_min=year_min,
            year_max=year_max,
        )
    ).all()
    gate_results = []
    quality_rows = []
    paper_ids = set()
    paper_meta_by_id = {}
    exportable_by_paper: dict[str, int] = defaultdict(int)
    blocked_by_paper: dict[str, int] = defaultdict(int)

    for row, paper in rows:
        gate = is_export_eligible_extraction(session, row, target_type="dft_results")
        gate_results.append(gate)
        paper_id = str(paper.id)
        paper_ids.add(paper.id)
        paper_meta_by_id[paper_id] = {
            "title": paper.title,
            "doi": paper.doi,
            "library_detail_url": f"../literature_library/index.html?paper_id={paper_id}&tab=dft",
            "review_workbench_url": f"../external_analysis_workbench/index.html?paper_id={paper_id}",
        }
        if gate.eligible:
            exportable_by_paper[paper_id] += 1
        else:
            blocked_by_paper[paper_id] += 1
        if reason and reason not in gate.reasons:
            continue
        quality_rows.append(_dft_quality_row_payload(row, paper, gate))

    catalyst_counts: Counter[str] = Counter()
    setting_counts: Counter[str] = Counter()
    if paper_ids:
        for paper_id in session.scalars(select(CS.paper_id).where(CS.paper_id.in_(paper_ids))).all():
            catalyst_counts[str(paper_id)] += 1
        for paper_id in session.scalars(select(DS.paper_id).where(DS.paper_id.in_(paper_ids))).all():
            setting_counts[str(paper_id)] += 1

    paper_completeness = []
    for paper_id in sorted({str(pid) for pid in paper_ids}):
        meta = paper_meta_by_id.get(paper_id, {})
        paper_completeness.append(
            {
                "paper_id": paper_id,
                "title": meta.get("title"),
                "doi": meta.get("doi"),
                "library_detail_url": meta.get("library_detail_url"),
                "review_workbench_url": meta.get("review_workbench_url"),
                "exportable_dft_results": exportable_by_paper.get(paper_id, 0),
                "blocked_dft_results": blocked_by_paper.get(paper_id, 0),
                "catalyst_samples": catalyst_counts.get(paper_id, 0),
                "dft_settings": setting_counts.get(paper_id, 0),
                "hints": [
                    hint
                    for hint, present in (
                        ("missing_catalyst_sample", catalyst_counts.get(paper_id, 0) == 0),
                        ("missing_dft_setting", setting_counts.get(paper_id, 0) == 0),
                        ("has_blocked_dft_results", blocked_by_paper.get(paper_id, 0) > 0),
                    )
                    if present
                ],
            }
        )

    gate_summary = summarize_gate_results(gate_results)
    return {
        "metadata": {
            "schema_version": "dft_quality_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "property_type": property_type,
                "adsorbate": adsorbate,
                "year_min": year_min,
                "year_max": year_max,
                "reason": reason,
            },
            "safety_gate": "safe_verified_with_required_evidence",
            "eligible_count": gate_summary["eligible"],
            "blocked_count": gate_summary["blocked"],
            "blocked_reasons": gate_summary["blocked_reasons"],
            "total_candidates": gate_summary["total_candidates"],
        },
        "rows": quality_rows[:limit],
        "paper_completeness": paper_completeness[:limit],
    }


@router.get("/compare")
async def compare_dft_results(
    property_type: str = Query(..., description="Property type to compare, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Optional adsorbate filter, e.g. Li2S4"),
    catalyst_type: str | None = Query(default=None, description="Optional catalyst type filter: single_atom or dual_atom"),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    min_confidence: float = Query(default=0.3, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
):
    stmt = (
        select(DR, P)
        .join(P, DR.paper_id == P.id)
        .where(DR.property_type.ilike(f"%{property_type}%"))
        .where(DR.confidence >= min_confidence)
        .order_by(DR.value.asc().nulls_last())
        .limit(limit)
    )
    if adsorbate:
        stmt = stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        stmt = stmt.where(P.year >= year_min)
    if year_max:
        stmt = stmt.where(P.year <= year_max)

    rows = session.execute(stmt).all()
    catalyst_by_paper: dict[str, list] = defaultdict(list)
    if rows:
        cat_rows = session.scalars(select(CS).where(CS.paper_id.in_([row[1].id for row in rows]))).all()
        for cat in cat_rows:
            catalyst_by_paper[str(cat.paper_id)].append(
                {
                    "name": cat.name,
                    "type": cat.catalyst_type,
                    "metal_centers": cat.metal_centers,
                    "coordination": cat.coordination,
                    "support": cat.support,
                }
            )

    items = []
    for dr, paper in rows:
        pid = str(paper.id)
        catalysts = catalyst_by_paper.get(pid, [])
        if catalyst_type:
            catalysts = [item for item in catalysts if (item.get("type") or "").lower() == catalyst_type.lower()]
            if not catalysts and catalyst_by_paper.get(pid):
                continue
        items.append(
            {
                "paper_id": pid,
                "title": paper.title,
                "doi": paper.doi,
                "journal": paper.journal,
                "year": paper.year,
                "property_type": dr.property_type,
                "adsorbate": dr.adsorbate,
                "value": dr.value,
                "unit": dr.unit,
                "reaction_step": dr.reaction_step,
                "confidence": dr.confidence,
                "evidence_text": dr.evidence_text,
                "source_section": dr.source_section,
                "source_figure": dr.source_figure,
                "catalysts": catalysts,
            }
        )

    numeric_values = [item["value"] for item in items if item["value"] is not None]
    stats = {}
    if numeric_values:
        stats = {
            "count": len(numeric_values),
            "min": round(min(numeric_values), 4),
            "max": round(max(numeric_values), 4),
            "mean": round(sum(numeric_values) / len(numeric_values), 4),
            "unit": items[0]["unit"] if items else None,
        }

    return {
        "query": {"property_type": property_type, "adsorbate": adsorbate, "catalyst_type": catalyst_type},
        "stats": stats,
        "total": len(items),
        "items": items,
    }


@router.get("/aggregate")
async def aggregate_papers(session: Session = Depends(get_db_session)):
    dft_rows = session.scalars(select(DR).order_by(DR.adsorbate.asc().nulls_last())).all()
    adsorbate_groups = defaultdict(list)
    for row in dft_rows:
        key_raw = (row.adsorbate or "").strip()
        if not key_raw:
            continue
        key = re.sub(r"[^a-zA-Z0-9]", "", key_raw).lower()
        adsorbate_groups[key].append(
            {
                "adsorbate": row.adsorbate,
                "property_type": row.property_type,
                "value": row.value,
                "unit": row.unit,
                "reaction_step": row.reaction_step,
                "paper_id": str(row.paper_id),
                "source_section": row.source_section,
                "source_figure": row.source_figure,
                "confidence": row.confidence,
            }
        )

    cat_rows = session.scalars(select(CS).order_by(CS.name.asc().nulls_last())).all()
    catalyst_groups = defaultdict(list)
    for row in cat_rows:
        key_raw = (row.name or "").strip()
        if not key_raw:
            continue
        key = re.sub(r"[^a-zA-Z0-9]", "", key_raw).lower()
        catalyst_groups[key].append(
            {
                "name": row.name,
                "catalyst_type": row.catalyst_type,
                "metal_centers": row.metal_centers,
                "coordination": row.coordination,
                "support": row.support,
                "synthesis_method": row.synthesis_method,
                "paper_id": str(row.paper_id),
            }
        )

    aliases = []
    for key, items in sorted(adsorbate_groups.items()):
        if len(items) < 2:
            continue
        raw_names = sorted(set(item["adsorbate"] for item in items if item["adsorbate"]))
        if len(raw_names) > 1:
            aliases.append(
                {
                    "type": "adsorbate",
                    "canonical_key": key,
                    "variants": raw_names,
                    "paper_count": len(set(item["paper_id"] for item in items)),
                }
            )
    for key, items in sorted(catalyst_groups.items()):
        if len(items) < 2:
            continue
        raw_names = sorted(set(item["name"] for item in items if item["name"]))
        if len(raw_names) > 1:
            aliases.append(
                {
                    "type": "catalyst",
                    "canonical_key": key,
                    "variants": raw_names,
                    "paper_count": len(set(item["paper_id"] for item in items)),
                }
            )

    return {
        "adsorbate_groups": dict(sorted(adsorbate_groups.items())),
        "catalyst_groups": dict(sorted(catalyst_groups.items())),
        "possible_name_aliases": aliases,
    }
