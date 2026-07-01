from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import DFTResult, PaperFigure, WritingCard
from app.rag.eligibility import (
    figure_is_rag_eligible,
    writing_card_is_rag_eligible,
    _figure_has_safe_review,
)
from app.utils.figure_summary import figure_summary_echoes_caption, flatten_figure_key_elements
from app.utils.review_safety import ExportGateResult, bulk_export_gate_results, writing_card_gate


def build_rag_quality_summary(
    session: Session,
    *,
    figures: list[PaperFigure],
    dft_results: list[DFTResult],
    writing_cards: list[WritingCard],
    dft_gate_by_id: dict[str, ExportGateResult] | None = None,
) -> dict[str, Any]:
    figure_eligibility = {
        str(item.id): figure_is_rag_eligible(session, item)
        for item in figures
    }
    figure_summary = _summarize_items(
        figures,
        lambda item: figure_eligibility.get(str(item.id), False),
        lambda item: _figure_block_reasons(session, item),
    )
    figure_summary["blocked_items"] = _figure_blocked_items(
        session,
        figures,
        eligibility_by_id=figure_eligibility,
    )
    dft_summary = _summarize_dft(session, dft_results, gate_by_id=dft_gate_by_id)
    writing_summary = _summarize_items(
        writing_cards,
        lambda item: writing_card_is_rag_eligible(session, item),
        _writing_card_block_reasons,
    )
    return {
        "figures": figure_summary,
        "dft_results": dft_summary,
        "writing_cards": writing_summary,
        "eligible_total": (
            figure_summary["eligible"]
            + dft_summary["eligible"]
            + writing_summary["eligible"]
        ),
        "blocked_total": (
            figure_summary["blocked"]
            + dft_summary["blocked"]
            + writing_summary["blocked"]
        ),
    }


def _summarize_items(items: list[Any], eligible_fn: Any, reasons_fn: Any) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    eligible = 0
    for item in items:
        if eligible_fn(item):
            eligible += 1
        else:
            for reason in reasons_fn(item):
                reason_counts[reason] += 1
    return {
        "total": len(items),
        "eligible": eligible,
        "blocked": len(items) - eligible,
        "blocked_reasons": dict(sorted(reason_counts.items())),
    }


def _summarize_dft(
    session: Session,
    rows: list[DFTResult],
    *,
    gate_by_id: dict[str, ExportGateResult] | None = None,
) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    eligible = 0
    gate_by_id = gate_by_id if gate_by_id is not None else (
        bulk_export_gate_results(session, rows, target_type="dft_results") if rows else {}
    )
    for row in rows:
        gate = gate_by_id.get(str(row.id))
        if gate is not None and gate.eligible and not _dft_minimum_field_reasons(row):
            eligible += 1
            continue
        row_reasons: list[str] = []
        if gate is not None:
            row_reasons.extend(gate.reasons)
        for reason in _dft_minimum_field_reasons(row):
            row_reasons.append(reason)
        if not row_reasons:
            row_reasons.append("not_export_safe")
        reason_counts.update(row_reasons)
    return {
        "total": len(rows),
        "eligible": eligible,
        "blocked": len(rows) - eligible,
        "blocked_reasons": dict(sorted(reason_counts.items())),
    }


def _figure_block_reasons(session: Session, figure: PaperFigure) -> list[str]:
    reasons: list[str] = []
    if not figure.image_path:
        reasons.append("missing_image")
    if figure.page is None:
        reasons.append("missing_page")
    if not str(figure.caption or "").strip():
        reasons.append("missing_caption")
    crop_status = str(figure.crop_status or "").strip().lower()
    if crop_status in {"missing", "missing_image", "failed", "full_page", "needs_repair", "needs_review", "caption_only", "noisy", "noise"}:
        reasons.append(f"crop_status:{crop_status}")
    elif crop_status == "needs_recrop" and not _figure_has_latest_precise_recrop(figure):
        reasons.append(f"crop_status:{crop_status}")
    if _figure_is_unlocated_full_page_recrop(figure):
        reasons.append("unlocated_full_page_recrop")
    role = str(figure.figure_role or "").strip().lower()
    if role in {"noise", "noisy", "decorative", "publisher_logo"}:
        reasons.append(f"figure_role:{role}")
    if not role or role in {"unknown", "uncategorized", "unclassified", "other"}:
        reasons.append("missing_figure_role")
    if not str(figure.content_summary or "").strip():
        reasons.append("missing_content_summary")
    elif figure_summary_echoes_caption(figure.content_summary, figure.caption):
        reasons.append("caption_echo_summary")
    key_elements = _normalize_figure_key_elements(figure.key_elements)
    if not key_elements:
        reasons.append("missing_key_elements")
    elif any(_is_placeholder_key_element(item) for item in key_elements):
        reasons.append("placeholder_key_elements")
    if not reasons:
        reasons.append("unclassified_or_unreviewed")
    return reasons


def _figure_blocked_items(
    session: Session,
    figures: list[PaperFigure],
    *,
    eligibility_by_id: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for figure in figures:
        eligible = (
            eligibility_by_id.get(str(figure.id), False)
            if eligibility_by_id is not None
            else figure_is_rag_eligible(session, figure)
        )
        if eligible:
            continue
        items.append(
            {
                "source_id": str(figure.id),
                "figure_label": figure.figure_label,
                "page": figure.page,
                "caption": figure.caption,
                "reasons": _figure_block_reasons(session, figure),
            }
        )
    return items


def _is_placeholder_key_element(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in {"verified_figure", "figure_verified", "reviewed_figure", "ai_verified", "verified", "reviewed", "ok"}


def _figure_is_unlocated_full_page_recrop(figure: PaperFigure) -> bool:
    crop_source = str(figure.crop_source or "").strip().lower()
    if crop_source.startswith("recrop:full_page"):
        return True
    if crop_source.startswith("recrop:"):
        return False
    prov = figure.prov
    if not isinstance(prov, list):
        return False
    for entry in reversed(prov):
        if not isinstance(entry, dict):
            continue
        if entry.get("action") == "recrop_figure":
            return str(entry.get("strategy") or "").lower() == "full_page"
    return False


def _figure_has_latest_precise_recrop(figure: PaperFigure) -> bool:
    crop_source = str(figure.crop_source or "").strip().lower()
    if crop_source.startswith("recrop:ai_bbox"):
        return True
    prov = figure.prov
    if not isinstance(prov, list):
        return False
    for entry in reversed(prov):
        if not isinstance(entry, dict):
            continue
        if entry.get("action") == "recrop_figure":
            return str(entry.get("strategy") or "").lower() == "ai_bbox"
    return False


def _normalize_figure_key_elements(value: Any) -> list[str]:
    return flatten_figure_key_elements(value)
def _dft_minimum_field_reasons(row: DFTResult) -> list[str]:
    reasons: list[str] = []
    property_type = str(row.property_type or "").strip()
    energy_type = ""
    if isinstance(row.evidence_payload, dict):
        energy_type = str(row.evidence_payload.get("energy_type") or "").strip()
    if not property_type and not energy_type:
        reasons.append("missing_property_type")
    if row.value is None:
        reasons.append("missing_value")
    if not str(row.unit or "").strip():
        reasons.append("missing_unit")
    return reasons


def _writing_card_block_reasons(card: WritingCard) -> list[str]:
    gate = writing_card_gate(card)
    if gate.blocked_reasons:
        return list(gate.blocked_reasons)
    return ["unreviewed"]
