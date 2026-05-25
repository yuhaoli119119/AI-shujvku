from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import DFTResult, WritingCard
from app.db.session import session_scope
from app.utils.locator_degradation import locator_degradation
from app.utils.review_safety import is_export_eligible_extraction, writing_card_gate
from scripts.recover_evidence_pages import analyze_evidence_pages


ACTIVE_REVIEW_RESOLUTION_STATUSES = {"active", "remapped"}
UNSAFE_REVIEW_RESOLUTION_STATUSES = {"stale", "ambiguous", "unresolved", "unknown"}


EXTRACTION_EVIDENCE_COLUMNS = {
    "dft_results": ["evidence_text"],
    "mechanism_claims": ["evidence_text"],
    "electrochemical_performance": ["evidence_text"],
    "catalyst_samples": ["evidence_strength", "synthesis_method"],
    "dft_settings": ["raw_json"],
}


def _table_names(session: Session) -> set[str]:
    return set(inspect(session.bind).get_table_names())


def _columns(session: Session, table_name: str) -> set[str]:
    if table_name not in _table_names(session):
        return set()
    return {column["name"] for column in inspect(session.bind).get_columns(table_name)}


def _count(session: Session, table_name: str, where: str = "1=1", params: dict[str, Any] | None = None) -> int:
    if table_name not in _table_names(session):
        return 0
    sql = f"SELECT COUNT(*) FROM {table_name} WHERE {where}"
    return int(session.execute(text(sql), params or {}).scalar() or 0)


def _blank_condition(column_name: str) -> str:
    return f"({column_name} IS NULL OR TRIM(CAST({column_name} AS TEXT)) = '')"


def _json_blank_condition(column_name: str) -> str:
    return f"({column_name} IS NULL OR TRIM(CAST({column_name} AS TEXT)) IN ('', 'null', '{{}}', '[]'))"


def _paper_where(session: Session, table_name: str, paper_id: str | None) -> tuple[str, dict[str, Any]]:
    if not paper_id or "paper_id" not in _columns(session, table_name):
        return "1=1", {}
    return "CAST(paper_id AS TEXT) = :paper_id", {"paper_id": paper_id}


def _and(parts: list[str]) -> str:
    return " AND ".join(f"({part})" for part in parts if part) or "1=1"


def _parse_bbox(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _is_valid_bbox(value: Any) -> bool:
    bbox = _parse_bbox(value)
    if not isinstance(bbox, dict):
        return False
    keys = ("x0", "y0", "x1", "y1")
    if not all(key in bbox for key in keys):
        return False
    try:
        x0, y0, x1, y1 = (float(bbox[key]) for key in keys)
    except (TypeError, ValueError):
        return False
    return x1 > x0 and y1 > y0


def _bbox_abnormal_count(session: Session, paper_id: str | None = None) -> int:
    if "evidence_locators" not in _table_names(session) or "bbox" not in _columns(session, "evidence_locators"):
        return 0
    where, params = _paper_where(session, "evidence_locators", paper_id)
    rows = session.execute(
        text(f"SELECT bbox FROM evidence_locators WHERE bbox IS NOT NULL AND {where}"),
        params,
    ).all()
    return sum(1 for row in rows if not _is_valid_bbox(row[0]))


def _is_valid_page(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _evidence_locator_degradation_stats(session: Session, paper_id: str | None, limit: int) -> dict[str, int]:
    tables = _table_names(session)
    stats = Counter()

    if "evidence_claims" in tables:
        where, params = _paper_where(session, "evidence_claims", paper_id)
        rows = session.execute(
            text("SELECT page_start, page_end, evidence_text FROM evidence_claims WHERE " + where),
            params,
        ).all()
        for page_start, page_end, evidence_text in rows:
            status = "exact_page" if (_is_valid_page(page_start) or _is_valid_page(page_end)) else (
                "text_only" if str(evidence_text or "").strip() else "unresolved"
            )
            stats[f"status_{status}"] += 1

    if "evidence_spans" in tables:
        where, params = _paper_where(session, "evidence_spans", paper_id)
        rows = session.execute(text("SELECT page, text FROM evidence_spans WHERE " + where), params).all()
        for page, evidence_text in rows:
            status = "exact_page" if _is_valid_page(page) else ("text_only" if str(evidence_text or "").strip() else "unresolved")
            stats[f"status_{status}"] += 1

    if "evidence_locators" in tables:
        where, params = _paper_where(session, "evidence_locators", paper_id)
        rows = session.execute(
            text("SELECT page, locator_status, evidence_text, bbox, warning_reason FROM evidence_locators WHERE " + where),
            params,
        ).all()
        for page, status, evidence_text, bbox, warning_reason in rows:
            parsed_bbox = _parse_bbox(bbox)
            degradation = locator_degradation(
                page=page,
                locator_status=status,
                evidence_text=evidence_text,
                bbox=parsed_bbox if isinstance(parsed_bbox, dict) else None,
                warning_reason=warning_reason,
            )
            stats[f"status_{degradation.locator_status}"] += 1
            if parsed_bbox is not None:
                stats["evidence_with_bbox_count"] += 1
                if not _is_valid_page(page):
                    stats["evidence_bbox_without_page_count"] += 1

    total = (
        stats["status_exact_page"]
        + stats["status_text_only"]
        + stats["status_missing_page"]
        + stats["status_missing_locator"]
        + stats["status_approximate"]
        + stats["status_unresolved"]
    )
    recovery = analyze_evidence_pages(session, paper_id=paper_id, limit=None if limit <= 0 else None)
    recoverable = int(recovery["summary"]["proposed_apply_count"])
    unrecoverable = sum(
        1
        for item in recovery["decisions"]
        if not item.get("apply_eligible") and item.get("existing_page") is None
    )
    return {
        "evidence_total": total,
        "evidence_exact_page_count": stats["status_exact_page"],
        "evidence_missing_page_count": stats["status_missing_page"] + stats["status_text_only"],
        "evidence_text_only_count": stats["status_text_only"],
        "evidence_missing_locator_count": stats["status_missing_locator"],
        "evidence_approximate_count": stats["status_approximate"],
        "evidence_unresolved_count": stats["status_unresolved"],
        "evidence_with_bbox_count": stats["evidence_with_bbox_count"],
        "evidence_bbox_without_page_count": stats["evidence_bbox_without_page_count"],
        "evidence_recoverable_from_parsed_artifact_count": recoverable,
        "evidence_unrecoverable_count": unrecoverable,
        "pdf_jump_exact_eligible_count": stats["status_exact_page"],
        "pdf_jump_degraded_count": max(total - stats["status_exact_page"], 0),
    }


def _group_counts(
    session: Session,
    table_name: str,
    column_name: str,
    *,
    paper_id: str | None = None,
) -> dict[str, int]:
    if table_name not in _table_names(session) or column_name not in _columns(session, table_name):
        return {}
    where, params = _paper_where(session, table_name, paper_id)
    rows = session.execute(
        text(
            f"SELECT COALESCE(NULLIF(TRIM(CAST({column_name} AS TEXT)), ''), 'unknown') AS value, "
            f"COUNT(*) AS count FROM {table_name} WHERE {where} GROUP BY value ORDER BY value"
        ),
        params,
    ).mappings()
    return {str(row["value"]): int(row["count"]) for row in rows}


def _missing_all_columns_condition(columns: list[str]) -> str:
    return " AND ".join(_blank_condition(column) for column in columns)


def _count_missing_extraction_evidence(session: Session, table_name: str, paper_id: str | None) -> int:
    columns = [column for column in EXTRACTION_EVIDENCE_COLUMNS[table_name] if column in _columns(session, table_name)]
    if not columns:
        return 0
    where, params = _paper_where(session, table_name, paper_id)
    return _count(session, table_name, _and([where, _missing_all_columns_condition(columns)]), params)


def _safe_review_target_ids(session: Session, table_name: str, paper_id: str | None) -> set[str]:
    if "extraction_field_reviews" not in _table_names(session):
        return set()
    cols = _columns(session, "extraction_field_reviews")
    required = {"target_type", "target_id", "reviewer_status", "target_resolution_status"}
    if not required.issubset(cols):
        return set()
    where_parts = [
        "target_type = :target_type",
        "reviewer_status = 'verified'",
        "target_resolution_status IN ('active', 'remapped')",
    ]
    params: dict[str, Any] = {"target_type": table_name}
    if paper_id and "paper_id" in cols:
        where_parts.append("CAST(paper_id AS TEXT) = :paper_id")
        params["paper_id"] = paper_id
    rows = session.execute(
        text(f"SELECT DISTINCT target_id FROM extraction_field_reviews WHERE {_and(where_parts)}"),
        params,
    ).all()
    return {str(row[0]) for row in rows if row[0] is not None}


def _table_ids(session: Session, table_name: str, paper_id: str | None) -> list[str]:
    if table_name not in _table_names(session) or "id" not in _columns(session, table_name):
        return []
    where, params = _paper_where(session, table_name, paper_id)
    rows = session.execute(text(f"SELECT id FROM {table_name} WHERE {where}"), params).all()
    return [str(row[0]) for row in rows]


def _missing_safe_review_count(session: Session, table_name: str, paper_id: str | None) -> int:
    ids = _table_ids(session, table_name, paper_id)
    if not ids:
        return 0
    reviewed = _safe_review_target_ids(session, table_name, paper_id)
    return sum(1 for row_id in ids if row_id not in reviewed)


def _dft_export_gate_stats(session: Session, paper_id: str | None) -> dict[str, int]:
    if "dft_results" not in _table_names(session):
        return {
            "dft_export_total_candidates": 0,
            "dft_export_safe_eligible": 0,
            "dft_export_blocked_missing_review": 0,
            "dft_export_blocked_unsafe_review": 0,
            "dft_export_blocked_missing_evidence": 0,
            "dft_export_blocked_missing_evidence_text": 0,
        }
    stmt = select(DFTResult)
    if paper_id:
        stmt = stmt.where(DFTResult.paper_id == paper_id)
    rows = session.scalars(stmt).all()
    stats = Counter()
    stats["dft_export_total_candidates"] = len(rows)
    for row in rows:
        gate = is_export_eligible_extraction(session, row, target_type="dft_results")
        if gate.eligible:
            stats["dft_export_safe_eligible"] += 1
            continue
        for reason in gate.reasons:
            stats[f"dft_export_blocked_{reason}"] += 1
    return {
        "dft_export_total_candidates": stats["dft_export_total_candidates"],
        "dft_export_safe_eligible": stats["dft_export_safe_eligible"],
        "dft_export_blocked_missing_review": stats["dft_export_blocked_missing_review"],
        "dft_export_blocked_unsafe_review": stats["dft_export_blocked_unsafe_review"],
        "dft_export_blocked_missing_evidence": stats["dft_export_blocked_missing_evidence"],
        "dft_export_blocked_missing_evidence_text": stats["dft_export_blocked_missing_evidence_text"],
    }


def _writing_gate_stats(session: Session, paper_id: str | None) -> dict[str, int]:
    if "writing_cards" not in _table_names(session):
        return {
            "writing_cards_total": 0,
            "writing_cards_safe_usable": 0,
            "writing_cards_blocked_missing_evidence_chain": 0,
            "writing_cards_blocked_unsafe_review": 0,
        }
    stmt = select(WritingCard)
    if paper_id:
        stmt = stmt.where(WritingCard.paper_id == paper_id)
    rows = session.scalars(stmt).all()
    stats = Counter()
    stats["writing_cards_total"] = len(rows)
    for row in rows:
        gate = writing_card_gate(row)
        if gate.can_use_for_writing:
            stats["writing_cards_safe_usable"] += 1
            continue
        for reason in gate.blocked_reasons:
            if reason == "missing_evidence_chain":
                stats["writing_cards_blocked_missing_evidence_chain"] += 1
            elif reason in {"unsafe_review", "missing_review"}:
                stats["writing_cards_blocked_unsafe_review"] += 1
    return {
        "writing_cards_total": stats["writing_cards_total"],
        "writing_cards_safe_usable": stats["writing_cards_safe_usable"],
        "writing_cards_blocked_missing_evidence_chain": stats["writing_cards_blocked_missing_evidence_chain"],
        "writing_cards_blocked_unsafe_review": stats["writing_cards_blocked_unsafe_review"],
    }


def _orphan_count(session: Session, table_name: str, paper_id: str | None = None) -> int:
    if table_name not in _table_names(session) or "paper_id" not in _columns(session, table_name):
        return 0
    if paper_id:
        return 0
    return int(
        session.execute(
            text(
                f"SELECT COUNT(*) FROM {table_name} t "
                "LEFT JOIN papers p ON CAST(t.paper_id AS TEXT) = CAST(p.id AS TEXT) "
                "WHERE p.id IS NULL"
            )
        ).scalar()
        or 0
    )


def run_audit(session: Session, *, paper_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    tables = _table_names(session)

    evidence_counts: dict[str, int] = {
        "claims": _count(session, "evidence_claims", *_paper_where(session, "evidence_claims", paper_id)),
        "spans": _count(session, "evidence_spans", *_paper_where(session, "evidence_spans", paper_id)),
        "locators": _count(session, "evidence_locators", *_paper_where(session, "evidence_locators", paper_id)),
    }
    evidence_counts["total"] = sum(evidence_counts.values())

    evidence_missing_page = 0
    evidence_missing_text = 0
    evidence_invalid_page = 0
    if "evidence_claims" in tables:
        where, params = _paper_where(session, "evidence_claims", paper_id)
        evidence_missing_page += _count(session, "evidence_claims", _and([where, "(page_start IS NULL AND page_end IS NULL)"]), params)
        evidence_missing_text += _count(session, "evidence_claims", _and([where, _blank_condition("evidence_text")]), params)
        evidence_invalid_page += _count(session, "evidence_claims", _and([where, "((page_start IS NOT NULL AND page_start <= 0) OR (page_end IS NOT NULL AND page_end <= 0))"]), params)
    if "evidence_spans" in tables:
        where, params = _paper_where(session, "evidence_spans", paper_id)
        evidence_missing_page += _count(session, "evidence_spans", _and([where, "page IS NULL"]), params)
        evidence_missing_text += _count(session, "evidence_spans", _and([where, _blank_condition("text")]), params)
        evidence_invalid_page += _count(session, "evidence_spans", _and([where, "page IS NOT NULL AND page <= 0"]), params)
    if "evidence_locators" in tables:
        where, params = _paper_where(session, "evidence_locators", paper_id)
        evidence_missing_page += _count(session, "evidence_locators", _and([where, "page IS NULL"]), params)
        evidence_missing_text += _count(session, "evidence_locators", _and([where, _blank_condition("evidence_text")]), params)
        evidence_invalid_page += _count(session, "evidence_locators", _and([where, "page IS NOT NULL AND page <= 0"]), params)

    extraction_tables: dict[str, dict[str, int]] = {}
    extraction_total = 0
    extraction_missing_evidence = 0
    extraction_missing_safe_review = 0
    for table_name in EXTRACTION_EVIDENCE_COLUMNS:
        if table_name not in tables:
            continue
        where, params = _paper_where(session, table_name, paper_id)
        total = _count(session, table_name, where, params)
        missing_evidence = _count_missing_extraction_evidence(session, table_name, paper_id)
        missing_safe_review = _missing_safe_review_count(session, table_name, paper_id)
        extraction_tables[table_name] = {
            "total": total,
            "missing_evidence_text_or_payload": missing_evidence,
            "missing_safe_verified_review": missing_safe_review,
        }
        extraction_total += total
        extraction_missing_evidence += missing_evidence
        extraction_missing_safe_review += missing_safe_review

    external_candidate_total = 0
    external_candidate_missing_evidence = 0
    external_candidate_status_counts: dict[str, int] = {}
    if "external_analysis_candidates" in tables:
        where, params = _paper_where(session, "external_analysis_candidates", paper_id)
        external_candidate_total = _count(session, "external_analysis_candidates", where, params)
        if "evidence_payload" in _columns(session, "external_analysis_candidates"):
            external_candidate_missing_evidence = _count(
                session,
                "external_analysis_candidates",
                _and([where, _json_blank_condition("evidence_payload")]),
                params,
            )
        external_candidate_status_counts = _group_counts(
            session,
            "external_analysis_candidates",
            "status",
            paper_id=paper_id,
        )

    review_total = _count(session, "extraction_field_reviews", *_paper_where(session, "extraction_field_reviews", paper_id))
    review_status_empty = 0
    verified_missing_evidence = 0
    verified_bad_resolution = 0
    review_status_counts: dict[str, int] = {}
    target_resolution_counts: dict[str, int] = {}
    unsafe_resolution_counts: dict[str, int] = {}
    if "extraction_field_reviews" in tables:
        where, params = _paper_where(session, "extraction_field_reviews", paper_id)
        cols = _columns(session, "extraction_field_reviews")
        if "reviewer_status" in cols:
            review_status_empty = _count(session, "extraction_field_reviews", _and([where, _blank_condition("reviewer_status")]), params)
            review_status_counts = _group_counts(session, "extraction_field_reviews", "reviewer_status", paper_id=paper_id)
        if "target_resolution_status" in cols:
            target_resolution_counts = _group_counts(
                session,
                "extraction_field_reviews",
                "target_resolution_status",
                paper_id=paper_id,
            )
            unsafe_resolution_counts = {
                status: count for status, count in target_resolution_counts.items() if status in UNSAFE_REVIEW_RESOLUTION_STATUSES
            }
            verified_bad_resolution = _count(
                session,
                "extraction_field_reviews",
                _and(
                    [
                        where,
                        "reviewer_status = 'verified'",
                        "COALESCE(NULLIF(TRIM(CAST(target_resolution_status AS TEXT)), ''), 'unknown') NOT IN ('active', 'remapped')",
                    ]
                ),
                params,
            )
        if "evidence_text" in cols:
            verified_missing_evidence = _count(
                session,
                "extraction_field_reviews",
                _and([where, "reviewer_status = 'verified'", _blank_condition("evidence_text")]),
                params,
            )

    writing_total = 0
    writing_missing_evidence_chain = 0
    if "writing_cards" in tables:
        where, params = _paper_where(session, "writing_cards", paper_id)
        writing_total = _count(session, "writing_cards", where, params)
        if "evidence_chain" in _columns(session, "writing_cards"):
            writing_missing_evidence_chain = _count(
                session,
                "writing_cards",
                _and([where, _json_blank_condition("evidence_chain")]),
                params,
            )

    dft_export_gate = _dft_export_gate_stats(session, paper_id)
    writing_gate = _writing_gate_stats(session, paper_id)
    locator_degradation_stats = _evidence_locator_degradation_stats(session, paper_id, limit)

    orphan_counts = {
        table_name: _orphan_count(session, table_name, paper_id)
        for table_name in [
            "evidence_claims",
            "evidence_spans",
            "evidence_locators",
            "dft_results",
            "mechanism_claims",
            "electrochemical_performance",
            "catalyst_samples",
            "dft_settings",
            "writing_cards",
            "external_analysis_candidates",
            "extraction_field_reviews",
        ]
        if table_name in tables
    }

    samples = _collect_samples(session, paper_id=paper_id, limit=limit)

    return {
        "paper_id": paper_id,
        "evidence": {
            **evidence_counts,
            "missing_page": evidence_missing_page,
            "missing_evidence_text": evidence_missing_text,
            "invalid_page": evidence_invalid_page,
            "abnormal_bbox": _bbox_abnormal_count(session, paper_id=paper_id),
        },
        "locator_degradation": locator_degradation_stats,
        "extraction": {
            "total": extraction_total,
            "missing_evidence_reference": extraction_missing_evidence,
            "missing_safe_verified_review": extraction_missing_safe_review,
            "tables": extraction_tables,
        },
        "external_analysis_candidates": {
            "total": external_candidate_total,
            "missing_evidence_payload": external_candidate_missing_evidence,
            "status_counts": external_candidate_status_counts,
        },
        "reviews": {
            "total": review_total,
            "reviewer_status_empty": review_status_empty,
            "reviewer_status_counts": review_status_counts,
            "target_resolution_status_counts": target_resolution_counts,
            "unsafe_resolution_status_counts": unsafe_resolution_counts,
            "verified_missing_evidence_text": verified_missing_evidence,
            "verified_but_unsafe_resolution": verified_bad_resolution,
        },
        "export_writing_dataset": {
            **dft_export_gate,
            "dft_results_export_missing_evidence": extraction_tables.get("dft_results", {}).get(
                "missing_evidence_text_or_payload", 0
            ),
            "dft_results_export_missing_safe_verified_review": extraction_tables.get("dft_results", {}).get(
                "missing_safe_verified_review", 0
            ),
            **writing_gate,
            "writing_cards_total": writing_total,
            "writing_cards_missing_evidence_chain": writing_missing_evidence_chain,
            "dataset_tables_detected": [],
        },
        "orphans": orphan_counts,
        "samples": samples,
    }


def _collect_samples(session: Session, *, paper_id: str | None, limit: int) -> dict[str, list[dict[str, Any]]]:
    if limit <= 0:
        return {}
    samples: dict[str, list[dict[str, Any]]] = {}
    if "evidence_locators" in _table_names(session):
        where, params = _paper_where(session, "evidence_locators", paper_id)
        sample_where = _and(
            [
                where,
                "(page IS NULL OR evidence_text IS NULL OR TRIM(CAST(evidence_text AS TEXT)) = '')",
            ]
        )
        rows = session.execute(
            text(
                "SELECT id, paper_id, page, locator_status, evidence_text, bbox "
                f"FROM evidence_locators WHERE {sample_where} "
                "LIMIT :limit"
            ),
            {**params, "limit": limit},
        ).mappings()
        samples["evidence_locator_missing_page_or_text"] = [_json_safe(row) for row in rows]
    if "extraction_field_reviews" in _table_names(session):
        where, params = _paper_where(session, "extraction_field_reviews", paper_id)
        sample_where = _and(
            [
                where,
                "(reviewer_status = 'verified' AND (evidence_text IS NULL OR TRIM(CAST(evidence_text AS TEXT)) = ''))",
            ]
        )
        rows = session.execute(
            text(
                "SELECT id, paper_id, target_type, target_id, field_name, reviewer_status, target_resolution_status, evidence_text "
                f"FROM extraction_field_reviews WHERE {sample_where} "
                "LIMIT :limit"
            ),
            {**params, "limit": limit},
        ).mappings()
        samples["verified_reviews_missing_evidence_text"] = [_json_safe(row) for row in rows]
    return samples


def _json_safe(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            continue
        try:
            json.dumps(value)
        except TypeError:
            result[key] = str(value)
    return result


def print_report(report: dict[str, Any]) -> None:
    print("D1 evidence/extraction quality audit (read-only)")
    if report.get("paper_id"):
        print(f"Scope: paper_id={report['paper_id']}")
    print("")
    print("[Evidence]")
    evidence = report["evidence"]
    print(f"total={evidence['total']} claims={evidence['claims']} spans={evidence['spans']} locators={evidence['locators']}")
    print(
        "missing_page={missing_page} missing_evidence_text={missing_evidence_text} "
        "invalid_page={invalid_page} abnormal_bbox={abnormal_bbox}".format(**evidence)
    )
    locator = report["locator_degradation"]
    print(
        "locator_status: evidence_total={evidence_total} exact_page={evidence_exact_page_count} "
        "missing_page={evidence_missing_page_count} text_only={evidence_text_only_count} "
        "missing_locator={evidence_missing_locator_count} approximate={evidence_approximate_count} "
        "unresolved={evidence_unresolved_count} with_bbox={evidence_with_bbox_count} "
        "bbox_without_page={evidence_bbox_without_page_count}".format(**locator)
    )
    print(
        "recovery: recoverable_from_parsed_artifact={evidence_recoverable_from_parsed_artifact_count} "
        "unrecoverable={evidence_unrecoverable_count} pdf_jump_exact_eligible={pdf_jump_exact_eligible_count} "
        "pdf_jump_degraded={pdf_jump_degraded_count}".format(**locator)
    )
    print("")
    print("[Extraction]")
    extraction = report["extraction"]
    print(
        f"total={extraction['total']} missing_evidence_reference={extraction['missing_evidence_reference']} "
        f"missing_safe_verified_review={extraction['missing_safe_verified_review']}"
    )
    for table_name, table_stats in extraction["tables"].items():
        print(
            f"- {table_name}: total={table_stats['total']} "
            f"missing_evidence={table_stats['missing_evidence_text_or_payload']} "
            f"missing_safe_verified_review={table_stats['missing_safe_verified_review']}"
        )
    print("")
    print("[External AI Candidates]")
    candidates = report["external_analysis_candidates"]
    print(
        f"total={candidates['total']} missing_evidence_payload={candidates['missing_evidence_payload']} "
        f"status_counts={candidates['status_counts']}"
    )
    print("")
    print("[Reviews]")
    reviews = report["reviews"]
    print(
        f"total={reviews['total']} reviewer_status_empty={reviews['reviewer_status_empty']} "
        f"verified_missing_evidence_text={reviews['verified_missing_evidence_text']} "
        f"verified_but_unsafe_resolution={reviews['verified_but_unsafe_resolution']}"
    )
    print(f"reviewer_status_counts={reviews['reviewer_status_counts']}")
    print(f"target_resolution_status_counts={reviews['target_resolution_status_counts']}")
    print(f"unsafe_resolution_status_counts={reviews['unsafe_resolution_status_counts']}")
    print("")
    print("[Export/Writing/Dataset]")
    export = report["export_writing_dataset"]
    print(
        f"dft_export_total_candidates={export['dft_export_total_candidates']} "
        f"dft_export_safe_eligible={export['dft_export_safe_eligible']} "
        f"dft_export_blocked_missing_review={export['dft_export_blocked_missing_review']} "
        f"dft_export_blocked_unsafe_review={export['dft_export_blocked_unsafe_review']} "
        f"dft_export_blocked_missing_evidence={export['dft_export_blocked_missing_evidence']} "
        f"dft_export_blocked_missing_evidence_text={export['dft_export_blocked_missing_evidence_text']}"
    )
    print(
        f"dft_results_export_missing_evidence={export['dft_results_export_missing_evidence']} "
        f"dft_results_export_missing_safe_verified_review={export['dft_results_export_missing_safe_verified_review']} "
        f"writing_cards_total={export['writing_cards_total']} "
        f"writing_cards_safe_usable={export['writing_cards_safe_usable']} "
        f"writing_cards_blocked_missing_evidence_chain={export['writing_cards_blocked_missing_evidence_chain']} "
        f"writing_cards_blocked_unsafe_review={export['writing_cards_blocked_unsafe_review']} "
        f"writing_cards_missing_evidence_chain={export['writing_cards_missing_evidence_chain']} "
        f"dataset_tables_detected={export['dataset_tables_detected']}"
    )
    print("")
    print("[Orphans]")
    print(report["orphans"])
    print("")
    print("No changes written.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit for evidence/extraction/review provenance quality. "
            "This script never repairs or applies changes."
        )
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum sample rows per diagnostic bucket.")
    parser.add_argument("--paper-id", help="Optional paper UUID/text id to scope diagnostics.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        report = run_audit(session, paper_id=args.paper_id, limit=args.limit)
        session.rollback()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
