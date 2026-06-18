from __future__ import annotations

from collections import Counter
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import DFTResult, PaperFigure, WritingCard
from app.rag.eligibility import (
    dft_result_is_rag_eligible,
    figure_is_rag_eligible,
    writing_card_is_rag_eligible,
    _figure_has_safe_review,
)
from app.utils.review_safety import bulk_export_gate_results, writing_card_gate


def build_rag_quality_summary(
    session: Session,
    *,
    figures: list[PaperFigure],
    dft_results: list[DFTResult],
    writing_cards: list[WritingCard],
) -> dict[str, Any]:
    figure_summary = _summarize_items(
        figures,
        lambda item: figure_is_rag_eligible(session, item),
        lambda item: _figure_block_reasons(session, item),
    )
    figure_summary["blocked_items"] = _figure_blocked_items(session, figures)
    dft_summary = _summarize_dft(session, dft_results)
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


def _summarize_dft(session: Session, rows: list[DFTResult]) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    eligible = 0
    gate_by_id = bulk_export_gate_results(session, rows, target_type="dft_results") if rows else {}
    for row in rows:
        if dft_result_is_rag_eligible(session, row):
            eligible += 1
            continue
        row_reasons: list[str] = []
        gate = gate_by_id.get(str(row.id))
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
    if crop_status in {"missing", "missing_image", "failed", "needs_review", "needs_recrop", "caption_only", "noisy", "noise"}:
        if not _figure_has_latest_precise_recrop(figure):
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
    elif _figure_summary_echoes_caption(figure.content_summary, figure.caption) and not _figure_has_safe_review(session, figure):
        reasons.append("caption_echo_summary")
    key_elements = _normalize_figure_key_elements(figure.key_elements)
    if not key_elements:
        reasons.append("missing_key_elements")
    elif any(_is_placeholder_key_element(item) for item in key_elements):
        reasons.append("placeholder_key_elements")
    if not reasons:
        reasons.append("unclassified_or_unreviewed")
    return reasons


def _figure_blocked_items(session: Session, figures: list[PaperFigure]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for figure in figures:
        if figure_is_rag_eligible(session, figure):
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
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, dict):
        return _flatten_figure_key_element_value(value)
    text = str(value).strip()
    return [text] if text else []


def _flatten_figure_key_element_value(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"crop_issues", "visual_quality"}:
                continue
            if isinstance(nested, (dict, list)):
                items.extend(_flatten_figure_key_element_value(nested))
            elif nested is not None:
                text = str(nested).strip()
                if text and len(text) <= 120:
                    items.append(text)
    elif isinstance(value, list):
        for nested in value:
            items.extend(_flatten_figure_key_element_value(nested))
    elif value is not None:
        text = str(value).strip()
        if text and len(text) <= 120:
            items.append(text)
    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
        if len(normalized) >= 16:
            break
    return normalized


def _figure_summary_echoes_caption(summary: str | None, caption: str | None) -> bool:
    summary_tokens = _meaningful_text_tokens(summary)
    caption_tokens = _meaningful_text_tokens(caption)
    if len(summary_tokens) < 8 or len(caption_tokens) < 8:
        return False
    summary_text = " ".join(summary_tokens)
    caption_text = " ".join(caption_tokens)
    if summary_text == caption_text:
        return True
    if summary_text.startswith(caption_text) or caption_text.startswith(summary_text):
        return True
    if len(summary_tokens) < max(10, int(len(caption_tokens) * 0.55)):
        return False
    summary_unique = set(summary_tokens)
    caption_unique = set(caption_tokens)
    extra_unique = summary_unique - caption_unique
    if len(summary_tokens) >= len(caption_tokens) * 2 and len(extra_unique) >= 6:
        return False
    overlap = len(summary_unique & caption_unique)
    return overlap / max(1, min(len(summary_unique), len(caption_unique))) >= 0.88


def _meaningful_text_tokens(value: str | None) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in {"fig", "figure"}
    ]


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
