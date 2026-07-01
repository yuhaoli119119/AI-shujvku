from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample as CS
from app.db.models import DFTResult as DR
from app.db.models import DFTSetting as DS
from app.db.models import Paper as P
from app.db.session import get_db_session
from app.normalizers.chemistry_normalizer import get_property_taxonomy
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_export_service import (
    _extract_evidence_context,
    _dft_quality_row_payload,
    _dft_rows_statement,
    _normalized_property_type,
    _optional_int_filter,
    _optional_text_filter,
    _property_type_filter_clause,
    build_dft_csv_rows,
    build_dft_ml_dataset,
    normalize_dft_display_value,
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
    catalyst_type: str | None = Query(default=None, description="Optional catalyst type filter: single_atom or dual_atom"),
    catalyst_name: str | None = Query(default=None, description="Optional catalyst name filter, e.g. Fe-GDY"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
    session: Session = Depends(get_db_session),
):
    csv_text, gate_summary = build_dft_csv_rows(
        session,
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
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
    catalyst_type: str | None = Query(default=None, description="Optional catalyst type filter: single_atom or dual_atom"),
    catalyst_name: str | None = Query(default=None, description="Optional catalyst name filter, e.g. Fe-GDY"),
    year_min: int | None = Query(default=None, description="Minimum publication year"),
    year_max: int | None = Query(default=None, description="Maximum publication year"),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
    session: Session = Depends(get_db_session),
):
    return build_dft_ml_dataset(
        session,
        property_type=property_type,
        adsorbate=adsorbate,
        catalyst_type=catalyst_type,
        catalyst_name=catalyst_name,
        year_min=year_min,
        year_max=year_max,
        library_name=library_name,
        min_confidence=min_confidence,
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
    property_type = _optional_text_filter(property_type)
    adsorbate = _optional_text_filter(adsorbate)
    year_min = _optional_int_filter(year_min)
    year_max = _optional_int_filter(year_max)
    library_name = _optional_text_filter(library_name)
    reason = _optional_text_filter(reason)
    limit = _optional_int_filter(limit) or 100
    rows = session.execute(
        _dft_rows_statement(
            property_type=property_type,
            adsorbate=adsorbate,
            catalyst_type=None,
            year_min=year_min,
            year_max=year_max,
            library_name=library_name,
            min_confidence=None,
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
    catalyst_name: str | None = Query(default=None, description="Optional catalyst name filter, e.g. Fe-GDY"),
    year_min: int | None = Query(default=None),
    year_max: int | None = Query(default=None),
    library_name: str | None = Query(default=None, description="Filter by literature library"),
    min_confidence: float = Query(default=0.3, ge=0.0, le=1.0),
    status: str = Query(default="all", description="exportable, needs_review, or all"),
    compact: bool = Query(default=False, description="Return a lighter payload optimized for list/table rendering"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    sort: str = Query(default="value", description="value, property_mix, or catalyst_group"),
    session: Session = Depends(get_db_session),
):
    normalized_status = (status or "all").strip().lower()
    normalized_sort = (sort or "value").strip().lower()
    base_stmt = (
        select(DR, P)
        .join(P, DR.paper_id == P.id)
        .order_by(DR.value.asc().nulls_last(), DR.id.asc())
    )
    if normalized_status in {"exportable", "eligible", "validated"}:
        # A verified export gate is authoritative even for legacy rows whose
        # optional extraction-confidence value was never populated.
        base_stmt = base_stmt.where(or_(DR.confidence >= min_confidence, DR.confidence.is_(None)))
    else:
        base_stmt = base_stmt.where(DR.confidence >= min_confidence)
    if property_type:
        property_clause = _property_type_filter_clause(DR.property_type, property_type)
        if property_clause is not None:
            base_stmt = base_stmt.where(property_clause)
    if adsorbate:
        base_stmt = base_stmt.where(DR.adsorbate.ilike(f"%{adsorbate}%"))
    if year_min:
        base_stmt = base_stmt.where(P.year >= year_min)
    if year_max:
        base_stmt = base_stmt.where(P.year <= year_max)
    if library_name is not None:
        base_stmt = base_stmt.where(build_library_name_clause(P.library_name, library_name))

    def _catalyst_map(chunk_rows: list[tuple[Any, Any]]) -> dict[str, dict[str, Any]]:
        catalyst_by_id: dict[str, dict[str, Any]] = {}
        if not chunk_rows:
            return catalyst_by_id
        cat_rows = session.scalars(select(CS).where(CS.paper_id.in_([row[1].id for row in chunk_rows]))).all()
        for cat in cat_rows:
            catalyst_by_id[str(cat.id)] = {
                "id": str(cat.id),
                "name": cat.name,
                "type": cat.catalyst_type,
                "metal_centers": cat.metal_centers,
                "coordination": cat.coordination,
                "support": cat.support,
            }
        return catalyst_by_id

    def _matches_catalyst_filters(dr: Any, catalysts: list[dict[str, Any]]) -> bool:
        if catalyst_type:
            if not any((item.get("type") or "").lower() == catalyst_type.lower() for item in catalysts):
                return False
        if catalyst_name:
            needle = catalyst_name.strip().lower()
            evidence_context = _extract_evidence_context(dr.evidence_payload)
            names = [
                item.get("name")
                for item in catalysts
                if item.get("name")
            ]
            names.extend(
                value
                for value in (
                    evidence_context.get("material_identity"),
                    evidence_context.get("material"),
                    evidence_context.get("structure_name"),
                )
                if value
            )
            if not any(needle in str(name).lower() for name in names):
                return False
        return True

    def _row_payload(dr: Any, paper: Any, catalysts: list[dict[str, Any]], gate: Any) -> dict[str, Any]:
        pid = str(paper.id)
        display_value, display_unit = normalize_dft_display_value(dr.value, dr.unit)
        normalized_property_type = _normalized_property_type(dr.property_type)
        taxonomy = get_property_taxonomy(dr.property_type)
        item = {
            "record_id": str(dr.id),
            "paper_id": pid,
            "title": paper.title,
            "doi": paper.doi,
            "property_type": dr.property_type,
            "normalized_property_type": normalized_property_type,
            "canonical_property_type": taxonomy["canonical_property_type"],
            "property_subtype": taxonomy["property_subtype"],
            "adsorbate": dr.adsorbate,
            "value": display_value,
            "unit": display_unit,
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
        evidence_context = _extract_evidence_context(dr.evidence_payload)
        item["display_catalyst_name"] = (
            (catalysts[0].get("name") if catalysts else None)
            or evidence_context.get("material_identity")
            or evidence_context.get("material")
            or evidence_context.get("structure_name")
            or "-"
        )
        item["material_binding_status"] = "bound" if dr.catalyst_sample_id else "derived_from_evidence"
        if not compact:
            item.update(
                {
                    "journal": paper.journal,
                    "year": paper.year,
                    "raw_value": dr.value,
                    "raw_unit": dr.unit,
                    "evidence_payload": dr.evidence_payload,
                }
            )
        return item

    filtered_items: list[dict[str, Any]] = []
    total: int | None = None
    has_more = False
    stats = {"count": 0}

    def _interleave_by_property(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in sorted(
            items,
            key=lambda row: (
                str(row.get("property_type") or ""),
                row.get("value") is None,
                row.get("value") if row.get("value") is not None else 0,
                str(row.get("record_id") or ""),
            ),
        ):
            grouped[str(item.get("property_type") or "unknown")].append(item)
        ordered: list[dict[str, Any]] = []
        property_keys = sorted(grouped)
        while any(grouped[key] for key in property_keys):
            for key in property_keys:
                if grouped[key]:
                    ordered.append(grouped[key].pop(0))
        return ordered

    def _sort_by_catalyst_group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            catalyst = str(item.get("display_catalyst_name") or "-").strip()
            catalyst_key = catalyst.casefold()
            unbound_or_missing = catalyst_key in {"", "-"}
            return (
                unbound_or_missing,
                catalyst_key,
                str(item.get("title") or "").casefold(),
                str(item.get("property_type") or "").casefold(),
                str(item.get("adsorbate") or "").casefold(),
                item.get("value") is None,
                item.get("value") if item.get("value") is not None else 0,
                str(item.get("record_id") or ""),
            )

        return sorted(items, key=sort_key)

    if compact and normalized_sort in {"catalyst", "catalyst_group", "catalyst_name", "material"}:
        fetch_limit = 2500
        rows = session.execute(base_stmt.limit(fetch_limit)).all()
        catalyst_by_id = _catalyst_map(rows)
        gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")
        all_items: list[dict[str, Any]] = []

        for dr, paper in rows:
            linked_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
            catalysts = [linked_catalyst] if linked_catalyst else []
            if not _matches_catalyst_filters(dr, catalysts):
                continue
            gate = gate_by_id[str(dr.id)]
            if normalized_status in {"exportable", "eligible", "validated"} and not gate.eligible:
                continue
            if normalized_status in {"needs_review", "candidate", "blocked"} and gate.eligible:
                continue
            all_items.append(_row_payload(dr, paper, catalysts, gate))

        ordered_items = _sort_by_catalyst_group(all_items)
        total = len(ordered_items)
        page_end = offset + limit
        items = ordered_items[offset:page_end]
        has_more = total > page_end
        stats = {
            "count": total,
            "property_type_counts": dict(Counter(item["property_type"] for item in all_items if item.get("property_type"))),
        }
    elif compact and normalized_sort in {"property_mix", "mixed", "property"} and not property_type:
        fetch_limit = 2500
        rows = session.execute(base_stmt.limit(fetch_limit)).all()
        catalyst_by_id = _catalyst_map(rows)
        gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")
        all_items: list[dict[str, Any]] = []

        for dr, paper in rows:
            linked_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
            catalysts = [linked_catalyst] if linked_catalyst else []
            if not _matches_catalyst_filters(dr, catalysts):
                continue
            gate = gate_by_id[str(dr.id)]
            if normalized_status in {"exportable", "eligible", "validated"} and not gate.eligible:
                continue
            if normalized_status in {"needs_review", "candidate", "blocked"} and gate.eligible:
                continue
            all_items.append(_row_payload(dr, paper, catalysts, gate))

        mixed_items = _interleave_by_property(all_items)
        total = len(mixed_items)
        page_end = offset + limit
        items = mixed_items[offset:page_end]
        has_more = total > page_end
        stats = {
            "count": total,
            "property_type_counts": dict(Counter(item["property_type"] for item in all_items if item.get("property_type"))),
        }
    elif compact:
        chunk_size = min(300, max(120, limit * 4))
        scanned = 0
        filtered_total = 0
        page_end = offset + limit
        items = []

        while True:
            chunk_rows = session.execute(base_stmt.offset(scanned).limit(chunk_size)).all()
            if not chunk_rows:
                break
            catalyst_by_id = _catalyst_map(chunk_rows)
            gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in chunk_rows], target_type="dft_results")

            for dr, paper in chunk_rows:
                linked_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
                catalysts = [linked_catalyst] if linked_catalyst else []
                if not _matches_catalyst_filters(dr, catalysts):
                    continue
                gate = gate_by_id[str(dr.id)]
                if normalized_status in {"exportable", "eligible", "validated"} and not gate.eligible:
                    continue
                if normalized_status in {"needs_review", "candidate", "blocked"} and gate.eligible:
                    continue
                if filtered_total >= offset and filtered_total < page_end:
                    items.append(_row_payload(dr, paper, catalysts, gate))
                filtered_total += 1

            scanned += len(chunk_rows)
            if len(chunk_rows) < chunk_size:
                break

        total = filtered_total
        has_more = total > page_end
        stats = {"count": total}
    else:
        fetch_limit = 2500
        rows = session.execute(base_stmt.limit(fetch_limit)).all()
        catalyst_by_id = _catalyst_map(rows)
        gate_by_id = bulk_export_gate_results(session, [dr for dr, _paper in rows], target_type="dft_results")

        for dr, paper in rows:
            linked_catalyst = catalyst_by_id.get(str(dr.catalyst_sample_id)) if dr.catalyst_sample_id else None
            catalysts = [linked_catalyst] if linked_catalyst else []
            if not _matches_catalyst_filters(dr, catalysts):
                continue
            gate = gate_by_id[str(dr.id)]
            if normalized_status in {"exportable", "eligible", "validated"} and not gate.eligible:
                continue
            if normalized_status in {"needs_review", "candidate", "blocked"} and gate.eligible:
                continue
            filtered_items.append(_row_payload(dr, paper, catalysts, gate))

        if normalized_sort in {"catalyst", "catalyst_group", "catalyst_name", "material"}:
            filtered_items = _sort_by_catalyst_group(filtered_items)
        total = len(filtered_items)
        items = filtered_items[offset : offset + limit]
        property_types = {item["property_type"] for item in filtered_items if item.get("property_type")}
        units = {item["unit"] for item in filtered_items if item.get("unit")}
        numeric_values = [item["value"] for item in filtered_items if item["value"] is not None]
        stats = {"count": total}
        if numeric_values and len(property_types) <= 1 and len(units) <= 1:
            stats = {
                "count": len(numeric_values),
                "min": round(min(numeric_values), 4),
                "max": round(max(numeric_values), 4),
                "mean": round(sum(numeric_values) / len(numeric_values), 4),
                "unit": filtered_items[0]["unit"] if filtered_items else None,
            }

    return {
        "query": {
            "property_type": property_type,
            "adsorbate": adsorbate,
            "catalyst_type": catalyst_type,
            "catalyst_name": catalyst_name,
            "library_name": normalize_library_name(library_name) if library_name is not None else None,
            "status": status,
            "compact": compact,
            "offset": offset,
            "limit": limit,
            "sort": normalized_sort,
        },
        "stats": stats,
        "total": total,
        "has_more": has_more,
        "items": items,
    }


@router.get("/aggregate")
def aggregate_papers(
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
