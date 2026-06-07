from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import Paper as P
from app.db.session import get_db_session
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_export_service import (
    _dft_quality_row_payload,
    _dft_rows_statement,
    build_dft_csv_rows,
    build_dft_ml_dataset,
)
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results, is_export_eligible_extraction, summarize_gate_results

router = APIRouter()
logger = logging.getLogger(__name__)


# Note: export helpers (_authors_text, _paper_payload, _catalyst_payload, _dft_setting_payload,
# _dft_rows_statement, _dft_quality_row_payload) have been moved to app.services.dft_export_service.


@router.get("/export/csv")
async def export_dft_results_csv(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    session: Session = Depends(get_db_session),
):
    csv_text, gate_summary = build_dft_csv_rows(
        session,
        property_type=property_type,
        adsorbate=adsorbate,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
    )
    csv_bytes = csv_text.encode("utf-8-sig")
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
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    session: Session = Depends(get_db_session),
):
    return build_dft_ml_dataset(
        session,
        property_type=property_type,
        adsorbate=adsorbate,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
    )


@router.get("/export/dft-quality")
async def dft_dataset_quality(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
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
            library_name=library_name,
        )
    ).all()
    gate_results = []
    quality_rows = []
    paper_ids = set()
    paper_meta_by_id = {}
    exportable_by_paper: dict[str, int] = defaultdict(int)
    blocked_by_paper: dict[str, int] = defaultdict(int)
    parsed_by_paper: dict[str, int] = defaultdict(int)

    gate_by_id = bulk_export_gate_results(session, [row for row, _paper in rows], target_type="dft_results")

    for row, paper in rows:
        gate = gate_by_id[str(row.id)]
        gate_results.append(gate)
        paper_id = str(paper.id)
        paper_ids.add(paper.id)
        paper_meta_by_id[paper_id] = {
            "title": paper.title,
            "doi": paper.doi,
            "library_detail_url": f"../literature_library/index.html?paper_id={paper_id}&tab=dft",
            "review_workbench_url": f"../external_analysis_workbench/index.html?paper_id={paper_id}",
        }
        parsed_by_paper[paper_id] += 1
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
    auditor = DFTCompletenessAuditor(session)
    for paper_id in sorted({str(pid) for pid in paper_ids}):
        meta = paper_meta_by_id.get(paper_id, {})
        audit = auditor.audit_paper(
            UUID(paper_id),
            parsed_count=parsed_by_paper.get(paper_id, 0),
            exportable_count=exportable_by_paper.get(paper_id, 0),
            blocked_count=blocked_by_paper.get(paper_id, 0),
        )
        paper_completeness.append(
            {
                "paper_id": paper_id,
                "title": meta.get("title"),
                "doi": meta.get("doi"),
                "library_detail_url": meta.get("library_detail_url"),
                "review_workbench_url": meta.get("review_workbench_url"),
                "exportable_dft_results": exportable_by_paper.get(paper_id, 0),
                "blocked_dft_results": blocked_by_paper.get(paper_id, 0),
                "dft_audit": audit,
                "dft_completeness_status": audit["coverage_status"],
                "dft_completeness_label": audit["status_label"],
                "catalyst_samples": catalyst_counts.get(paper_id, 0),
                "dft_settings": setting_counts.get(paper_id, 0),
                "hints": [
                    hint
                    for hint, present in (
                        ("missing_catalyst_sample", catalyst_counts.get(paper_id, 0) == 0),
                        ("missing_dft_setting", setting_counts.get(paper_id, 0) == 0),
                        ("has_blocked_dft_results", blocked_by_paper.get(paper_id, 0) > 0),
                        ("suspected_missing_dft", audit["suspected_missing_count"] > 0),
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
                "library_name": normalize_library_name(library_name) if library_name is not None else None,
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


@router.get("/export/dft-review-queue")
async def dft_review_queue(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    paper_id: UUID | None = Query(default=None, description="Restrict queue to one paper"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    reason: str | None = Query(default=None, description="Optional blocked reason filter"),
    status: str = Query(default="needs_review", description="needs_review, exportable, all, or a blocked reason"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
):
    return DFTReviewQueueService(session).list_queue(
        property_type=property_type,
        adsorbate=adsorbate,
        year_min=year_min,
        year_max=year_max,
        paper_id=paper_id,
        library_name=library_name,
        reason=reason,
        status=status,
        limit=limit,
    )


@router.get("/compare")
async def compare_dft_results(
    property_type: str | None = Query(default=None, description="Optional property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Optional adsorbate filter, e.g. Li2S4"),
    catalyst_type: str | None = Query(default=None, description="Optional catalyst type filter: single_atom or dual_atom"),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    min_confidence: float = Query(default=0.3, ge=0.0, le=1.0),
    status: str = Query(default="all", description="exportable, needs_review, or all"),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
):
    fetch_limit = limit if (status or "").strip().lower() in {"all", "any", ""} else min(limit * 5, 2500)
    stmt = (
        select(DR, P)
        .join(P, DR.paper_id == P.id)
        .where(DR.confidence >= min_confidence)
        .order_by(DR.value.asc().nulls_last())
        .limit(fetch_limit)
    )
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
    gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")

    for dr, paper in rows:
        pid = str(paper.id)
        catalysts = catalyst_by_paper.get(pid, [])
        if catalyst_type:
            catalysts = [item for item in catalysts if (item.get("type") or "").lower() == catalyst_type.lower()]
            if not catalysts and catalyst_by_paper.get(pid):
                continue
        gate = gate_by_id[str(dr.id)]
        normalized_status = (status or "exportable").strip().lower()
        if normalized_status in {"exportable", "eligible", "validated"} and not gate.eligible:
            continue
        if normalized_status in {"needs_review", "candidate", "blocked"} and gate.eligible:
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
                "candidate_status": dr.candidate_status or "system_candidate",
                "evidence_text": dr.evidence_text,
                "source_section": dr.source_section,
                "source_figure": dr.source_figure,
                "catalysts": catalysts,
                "is_exportable": gate.eligible,
                "validation_status": "validated" if gate.eligible else "needs_review",
                "blocked_reasons": list(gate.reasons),
                "review_status": gate.review_status,
                "review_gate_status": gate.review_gate_status,
                "provenance_level": gate.provenance_level,
                "locator_status": gate.locator_status,
            }
        )
        if len(items) >= limit:
            break

    property_types = {item["property_type"] for item in items if item.get("property_type")}
    units = {item["unit"] for item in items if item.get("unit")}
    numeric_values = [item["value"] for item in items if item["value"] is not None]
    stats = {"count": len(items)}
    if numeric_values and len(property_types) <= 1 and len(units) <= 1:
        stats = {
            "count": len(numeric_values),
            "min": round(min(numeric_values), 4),
            "max": round(max(numeric_values), 4),
            "mean": round(sum(numeric_values) / len(numeric_values), 4),
            "unit": items[0]["unit"] if items else None,
        }

    return {
        "query": {
            "property_type": property_type,
            "adsorbate": adsorbate,
            "catalyst_type": catalyst_type,
            "library_name": normalize_library_name(library_name) if library_name is not None else None,
            "status": status,
        },
        "stats": stats,
        "total": len(items),
        "items": items,
    }


@router.get("/aggregate")
async def aggregate_papers(
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    session: Session = Depends(get_db_session),
):
    dft_stmt = select(DR).join(P, DR.paper_id == P.id).order_by(DR.adsorbate.asc().nulls_last())
    if library_name is not None:
        dft_stmt = dft_stmt.where(build_library_name_clause(P.library_name, library_name))
    dft_rows = session.scalars(dft_stmt).all()
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

    cat_stmt = select(CS).join(P, CS.paper_id == P.id).order_by(CS.name.asc().nulls_last())
    if library_name is not None:
        cat_stmt = cat_stmt.where(build_library_name_clause(P.library_name, library_name))
    cat_rows = session.scalars(cat_stmt).all()
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
        "library_name": normalize_library_name(library_name) if library_name is not None else None,
        "adsorbate_groups": dict(sorted(adsorbate_groups.items())),
        "catalyst_groups": dict(sorted(catalyst_groups.items())),
        "possible_name_aliases": aliases,
    }
