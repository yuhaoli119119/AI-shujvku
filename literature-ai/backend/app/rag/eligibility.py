from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper, PaperChunk, PaperCorrection, PaperFigure, PaperSection, WritingCard
from app.utils.figure_summary import figure_summary_echoes_caption, flatten_figure_key_elements
from app.utils.review_safety import bulk_export_gate_results, has_safe_verified_review, writing_card_content_gate, writing_card_gate


def is_rag_eligible(session: Session, item: Any, item_type: str) -> bool:
    """Central RAG admission rule for parsed and AI-reviewed paper content."""

    normalized_type = str(item_type or "").strip().lower()
    if normalized_type in {"section", "paper_section", "chunk", "paper_chunk"}:
        return _section_item_is_eligible(session, item)
    if normalized_type in {"writing_card", "writing_cards"}:
        return writing_card_is_rag_eligible(session, item)
    if normalized_type in {"dft_result", "dft_results"}:
        return dft_result_is_rag_eligible(session, item)
    if normalized_type in {"figure_data_point", "figure_insight", "figure"}:
        return figure_is_rag_eligible(session, item)
    return False


def writing_card_is_rag_eligible(session: Session, item: Any) -> bool:
    """Writing cards enter formal RAG only when content quality cannot be bypassed."""

    if not isinstance(item, WritingCard):
        return False
    content_quality_passed = writing_card_content_gate(item).can_use_for_writing
    return bool(content_quality_passed and (
        writing_card_gate(item).can_use_for_writing
        or _writing_card_has_ai_verified_review(session, item)
    ))


def writing_card_rag_review_status(session: Session, item: Any) -> str:
    if not isinstance(item, WritingCard):
        return "blocked"
    gate = writing_card_gate(item)
    if gate.can_use_for_writing:
        return "content_verified"
    if writing_card_content_gate(item).can_use_for_writing and _writing_card_has_ai_verified_review(session, item):
        return "ai_verified"
    return gate.review_gate_status


def dft_result_is_rag_eligible(session: Session, item: Any) -> bool:
    """DFT facts enter formal RAG only after the export safety gate."""

    if not isinstance(item, DFTResult):
        return False
    gate = bulk_export_gate_results(session, [item], target_type="dft_results")[str(item.id)]
    return bool(gate.eligible and _dft_result_has_rag_minimum_fields(item))


def figure_is_rag_eligible(session: Session, item: Any) -> bool:
    """Figures enter formal RAG only when image, page, caption, and review risk are safe."""

    return _figure_item_is_eligible(session, item)


def _paper_has_ai_verified_content(session: Session, item: Any, field_names: set[str]) -> bool:
    paper_id = getattr(item, "paper_id", None)
    if paper_id is None and isinstance(item, PaperChunk):
        paper_id = item.paper_id
    if paper_id is None:
        return False
    normalized_fields = {field.lower() for field in field_names}
    candidates = session.scalars(
        select(ExternalAnalysisCandidate)
        .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
        .where(ExternalAnalysisCandidate.paper_id == paper_id)
        .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
        .order_by(ExternalAnalysisCandidate.created_at.desc())
        .limit(100)
    ).all()
    for candidate in candidates:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        field_name = str(payload.get("field_name") or "").strip().lower()
        target_path = str(payload.get("target_path") or "").strip().lower()
        if _review_field_matches(field_name, normalized_fields):
            return True
        if _review_field_matches(target_path, normalized_fields):
            return True
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == paper_id)
        .where(PaperCorrection.status == "approved")
        .order_by(PaperCorrection.created_at.desc())
        .limit(200)
    ).all()
    for correction in corrections:
        source = str(correction.source or "").lower()
        reviewer = str(correction.reviewed_by or "").lower()
        if source != "ide_ai" and "ide" not in reviewer:
            continue
        field_name = str(correction.field_name or "").strip().lower()
        target_path = str(correction.target_path or "").strip().lower()
        if _review_field_matches(field_name, normalized_fields):
            return True
        if _review_field_matches(target_path, normalized_fields):
            return True
    return False


def _review_field_matches(value: str, expected: set[str]) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return False
    if normalized in expected:
        return True
    return any(
        normalized.startswith(prefix + ":")
        or normalized.startswith(prefix + "/")
        or normalized.startswith(prefix + ".")
        for prefix in expected
    )


def _section_item_is_eligible(session: Session, item: Any) -> bool:
    if not section_is_retrieval_candidate(session, item):
        return False
    paper_id = getattr(item, "paper_id", None)
    if paper_id is None:
        return False
    target_ids = [getattr(item, "id", None)]
    if isinstance(item, PaperChunk) and item.section_id is not None:
        target_ids.insert(0, item.section_id)
    for target_id in target_ids:
        if target_id is None:
            continue
        if has_safe_verified_review(
            session,
            paper_id=paper_id,
            target_type="sections",
            target_id=target_id,
        ):
            return True
        if _object_has_ai_verified_content(
            session,
            paper_id=paper_id,
            target_id=target_id,
            field_names={"sections", "section"},
        ):
            return True
    return False


def _figure_item_is_eligible(session: Session, item: Any) -> bool:
    figure_id = getattr(item, "figure_id", None) or getattr(item, "id", None)
    paper_id = getattr(item, "paper_id", None)
    if figure_id is None and paper_id is None:
        return False
    query = select(PaperFigure)
    if figure_id is not None:
        query = query.where(PaperFigure.id == figure_id)
    elif paper_id is not None:
        query = query.where(PaperFigure.paper_id == paper_id)
    figure = session.scalars(query.limit(1)).first()
    if figure is None:
        return False
    if not figure.image_path:
        return False
    if figure.page is None:
        return False
    if not str(figure.caption or "").strip():
        return False
    review_verdict = _latest_figure_review_verdict(session, figure)
    if review_verdict in {"rejected", "needs_repair"}:
        return False
    crop_status = str(figure.crop_status or "").strip().lower()
    if crop_status in {
        "missing",
        "missing_image",
        "failed",
        "full_page",
        "needs_repair",
        "needs_review",
        "caption_only",
        "noisy",
        "noise",
    }:
        return False
    if crop_status == "needs_recrop" and not _figure_has_latest_precise_recrop(figure):
        return False
    if _figure_is_unlocated_full_page_recrop(figure):
        return False
    role = str(figure.figure_role or "").strip().lower()
    if role in {"noise", "noisy", "decorative", "publisher_logo"}:
        return False
    has_safe_review = review_verdict == "verified" or _figure_has_safe_review(session, figure)
    return _figure_has_required_ai_summary(
        figure,
        allow_caption_echo=False,
    ) and (
        _figure_has_ai_classification(figure) or has_safe_review
    )


def _figure_has_required_ai_summary(
    figure: PaperFigure,
    *,
    allow_caption_echo: bool = False,
) -> bool:
    role = str(figure.figure_role or "").strip().lower()
    if not role or role in {"unknown", "uncategorized", "unclassified", "other"}:
        return False
    summary = str(figure.content_summary or "").strip()
    if not summary:
        return False
    if not allow_caption_echo and figure_summary_echoes_caption(summary, figure.caption):
        return False
    key_elements = _normalize_figure_key_elements(figure.key_elements)
    if not key_elements:
        return False
    if any(_is_placeholder_figure_key_element(item) for item in key_elements):
        return False
    return True


def _figure_has_ai_classification(figure: PaperFigure) -> bool:
    role = str(figure.figure_role or "").strip().lower()
    meaningful_role = role and role not in {"unknown", "uncategorized", "unclassified", "other"}
    return any(
        (
            meaningful_role,
            str(figure.content_summary or "").strip(),
            bool(figure.key_elements),
            figure.role_confidence is not None,
        )
    )


def _is_placeholder_figure_key_element(value: Any) -> bool:
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


def _figure_has_safe_review(session: Session, figure: PaperFigure) -> bool:
    verdict = _latest_figure_review_verdict(session, figure)
    if verdict in {"rejected", "needs_repair"}:
        return False
    if verdict == "verified":
        return True
    if has_safe_verified_review(
        session,
        paper_id=figure.paper_id,
        target_type="figures",
        target_id=figure.id,
    ):
        return True
    if _object_has_ai_verified_content(
        session,
        paper_id=figure.paper_id,
        target_id=figure.id,
        field_names={"figures", "figure"},
    ):
        return True
    return _object_has_approved_ide_correction(
        session,
        paper_id=figure.paper_id,
        target_id=figure.id,
        field_names={"figures", "figure"},
    )


def _latest_figure_review_verdict(session: Session, figure: PaperFigure) -> str | None:
    """Return the single authoritative latest review_figure verdict."""

    review = session.scalars(
        select(AuditLog)
        .where(AuditLog.paper_id == figure.paper_id)
        .where(AuditLog.action == "review_figure")
        .where(AuditLog.target_id == str(figure.id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    ).first()
    payload = review.payload if review is not None and isinstance(review.payload, dict) else {}
    verdict = str(payload.get("verdict") or "").strip().lower()
    aliases = {
        "incorrect": "rejected",
        "needs_attention": "needs_repair",
    }
    normalized = aliases.get(verdict, verdict)
    return normalized if normalized in {"verified", "rejected", "needs_repair"} else None


def section_is_retrieval_candidate(session: Session, item: Any) -> bool:
    """Loose discovery gate; this never grants formal RAG or citation eligibility."""

    if not isinstance(item, (PaperSection, PaperChunk)):
        return False
    if item.paper_id is None or session.get(Paper, item.paper_id) is None:
        return False
    text = str(item.text or "").strip()
    if not text or _section_text_is_noise(text):
        return False
    section = item if isinstance(item, PaperSection) else session.get(PaperSection, item.section_id) if item.section_id else None
    title = str(getattr(section, "section_title", None) or "").strip()
    section_type = str(getattr(section, "section_type", None) or "").strip()
    if not title and not section_type:
        return False
    page_start = getattr(item, "page_start", None) or getattr(section, "page_start", None)
    page_end = getattr(item, "page_end", None) or getattr(section, "page_end", None)
    return page_start is not None or page_end is not None


def _section_text_is_noise(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if normalized in {"n/a", "na", "none", "null", "todo", "placeholder", "content unavailable"}:
        return True
    reference_markers = len(re.findall(r"(?:doi:|https?://|\[\d+\])", normalized))
    return reference_markers >= 5 and reference_markers * 35 >= len(normalized)


def _writing_card_has_ai_verified_review(session: Session, card: WritingCard) -> bool:
    if has_safe_verified_review(
        session,
        paper_id=card.paper_id,
        target_type="writing_cards",
        target_id=card.id,
    ):
        return True
    if _object_has_ai_verified_content(
        session,
        paper_id=card.paper_id,
        target_id=card.id,
        field_names={"writing_cards", "writing_card"},
    ):
        return True
    if _object_has_approved_ide_correction(
        session,
        paper_id=card.paper_id,
        target_id=card.id,
        field_names={"writing_cards", "writing_card"},
    ):
        return True
    if _writing_card_matches_approved_create_correction(session, card):
        return True
    return _collection_has_ai_verified_content(session, card.paper_id, {"writing_cards", "writing_card"})


def _writing_card_matches_approved_create_correction(session: Session, card: WritingCard) -> bool:
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == card.paper_id)
        .where(PaperCorrection.status == "approved")
        .where(PaperCorrection.operation == "create")
        .order_by(PaperCorrection.created_at.desc())
        .limit(100)
    ).all()
    for correction in corrections:
        source = str(correction.source or "").lower()
        reviewer = str(correction.reviewed_by or "").lower()
        if source != "ide_ai" and "ide" not in reviewer:
            continue
        if not _review_field_matches(str(correction.field_name or ""), {"writing_cards", "writing_card"}):
            continue
        proposed = correction.proposed_value if isinstance(correction.proposed_value, dict) else {}
        if _writing_card_matches_payload(card, proposed):
            return True
    return False


def _writing_card_matches_payload(card: WritingCard, payload: dict[str, Any]) -> bool:
    comparable_fields = [
        "paper_type",
        "research_gap",
        "proposed_solution",
        "core_hypothesis",
        "abstract_logic",
        "introduction_logic",
        "discussion_logic",
        "figure_logic",
    ]
    checked = 0
    for field in comparable_fields:
        expected = payload.get(field)
        if expected is None:
            continue
        checked += 1
        actual = getattr(card, field, None)
        if str(actual or "").strip() != str(expected or "").strip():
            return False
    return checked > 0


def _collection_has_ai_verified_content(session: Session, paper_id: Any, field_names: set[str]) -> bool:
    normalized_fields = {field.lower() for field in field_names}
    candidates = session.scalars(
        select(ExternalAnalysisCandidate)
        .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
        .where(ExternalAnalysisCandidate.paper_id == paper_id)
        .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
        .order_by(ExternalAnalysisCandidate.created_at.desc())
        .limit(100)
    ).all()
    for candidate in candidates:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        field_name = str(payload.get("field_name") or "").strip().lower()
        target_path = str(payload.get("target_path") or "").strip().lower()
        if _review_field_matches(field_name, normalized_fields) and _is_collection_level_target(target_path, normalized_fields):
            return True
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == paper_id)
        .where(PaperCorrection.status == "approved")
        .order_by(PaperCorrection.created_at.desc())
        .limit(200)
    ).all()
    for correction in corrections:
        source = str(correction.source or "").lower()
        reviewer = str(correction.reviewed_by or "").lower()
        if source != "ide_ai" and "ide" not in reviewer:
            continue
        field_name = str(correction.field_name or "").strip().lower()
        target_path = str(correction.target_path or "").strip().lower()
        if _review_field_matches(field_name, normalized_fields) and _is_collection_level_target(target_path, normalized_fields):
            return True
    return False


def _is_collection_level_target(value: str, expected: set[str]) -> bool:
    normalized = str(value or "").strip().lower()
    return not normalized or normalized in expected


def _object_has_ai_verified_content(
    session: Session,
    *,
    paper_id: Any,
    target_id: Any,
    field_names: set[str],
) -> bool:
    normalized_fields = {field.lower() for field in field_names}
    target_id_text = str(target_id).lower()
    candidates = session.scalars(
        select(ExternalAnalysisCandidate)
        .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
        .where(ExternalAnalysisCandidate.paper_id == paper_id)
        .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
        .order_by(ExternalAnalysisCandidate.created_at.desc())
        .limit(200)
    ).all()
    for candidate in candidates:
        materialized_id = str(candidate.materialized_target_id or "").lower()
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        field_name = str(payload.get("field_name") or "").strip().lower()
        target_path = str(payload.get("target_path") or "").strip().lower()
        if target_id_text != materialized_id and target_id_text not in target_path:
            continue
        if _review_field_matches(field_name, normalized_fields):
            return True
        if _review_field_matches(target_path, normalized_fields):
            return True
    return _object_has_approved_ide_correction(
        session,
        paper_id=paper_id,
        target_id=target_id,
        field_names=field_names,
    )


def _object_has_approved_ide_correction(
    session: Session,
    *,
    paper_id: Any,
    target_id: Any,
    field_names: set[str],
) -> bool:
    normalized_fields = {field.lower() for field in field_names}
    target_id_text = str(target_id).lower()
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == paper_id)
        .where(PaperCorrection.status == "approved")
        .order_by(PaperCorrection.created_at.desc())
        .limit(200)
    ).all()
    for correction in corrections:
        source = str(correction.source or "").lower()
        reviewer = str(correction.reviewed_by or "").lower()
        if source != "ide_ai" and "ide" not in reviewer:
            continue
        field_name = str(correction.field_name or "").strip().lower()
        target_path = str(correction.target_path or "").strip().lower()
        if target_id_text not in target_path:
            continue
        if _review_field_matches(field_name, normalized_fields):
            return True
        if _review_field_matches(target_path, normalized_fields):
            return True
    return False


def _dft_result_has_rag_minimum_fields(item: DFTResult) -> bool:
    property_type = str(item.property_type or "").strip()
    energy_type = ""
    if isinstance(item.evidence_payload, dict):
        energy_type = str(item.evidence_payload.get("energy_type") or "").strip()
    return bool(
        (property_type or energy_type)
        and item.value is not None
        and str(item.unit or "").strip()
    )
