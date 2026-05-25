from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import Paper as P
from app.db.session import get_db_session
from app.utils.review_safety import is_export_eligible_extraction, summarize_gate_results

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/export/csv")
async def export_dft_results_csv(
    property_type: str | None = Query(default=None, description="Filter by property type, e.g. adsorption_energy"),
    adsorbate: str | None = Query(default=None, description="Filter by adsorbate, e.g. Li2S4"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    session: Session = Depends(get_db_session),
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
            "X-D1-Exported-Count": str(gate_summary["eligible"]),
            "X-D1-Blocked-Count": str(gate_summary["blocked"]),
            "X-D1-Blocked-Reasons": json.dumps(gate_summary["blocked_reasons"], sort_keys=True),
        },
    )


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
