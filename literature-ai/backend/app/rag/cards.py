from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import DFTResult, Paper, PaperFigure, WritingCard
from app.utils.review_safety import ExportGateResult, WritingGateResult


def paper_code_for(session: Session, paper_id: Any) -> str | None:
    paper = session.get(Paper, paper_id) if paper_id is not None else None
    return paper.paper_code if paper is not None else None


def build_dft_card(
    session: Session,
    row: DFTResult,
    *,
    text: str,
    gate: ExportGateResult,
    page: int | None,
) -> dict[str, Any]:
    return _base_card(
        session,
        source_type="dft_result",
        source_id=row.id,
        paper_id=row.paper_id,
        page=page,
        evidence_text=row.evidence_text or text,
        review_status=gate.review_status,
    )


def build_figure_card(session: Session, row: PaperFigure, *, evidence_text: str) -> dict[str, Any]:
    return _base_card(
        session,
        source_type="figure",
        source_id=row.id,
        paper_id=row.paper_id,
        page=row.page,
        evidence_text=evidence_text,
        review_status="safe_verified_or_reliable_figure",
    )


def build_writing_card(
    session: Session,
    row: WritingCard,
    *,
    evidence_text: str,
    gate: WritingGateResult,
    review_status: str | None = None,
) -> dict[str, Any]:
    return _base_card(
        session,
        source_type="writing_card",
        source_id=row.id,
        paper_id=row.paper_id,
        page=_first_evidence_page(row.evidence_chain),
        evidence_text=evidence_text,
        review_status=review_status or ("safe_verified" if gate.can_use_for_writing else gate.review_gate_status),
    )


def build_evidence_card(
    session: Session,
    *,
    source_type: str,
    source_id: Any,
    paper_id: Any,
    evidence_text: str | None,
    review_status: str,
    page: int | None = None,
) -> dict[str, Any]:
    return _base_card(
        session,
        source_type=source_type,
        source_id=source_id,
        paper_id=paper_id,
        page=page,
        evidence_text=evidence_text,
        review_status=review_status,
    )


def _base_card(
    session: Session,
    *,
    source_type: str,
    source_id: Any,
    paper_id: Any,
    page: int | None,
    evidence_text: str | None,
    review_status: str,
) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_id": str(source_id) if source_id is not None else None,
        "paper_id": paper_id,
        "paper_code": paper_code_for(session, paper_id),
        "page": page,
        "evidence_text": evidence_text or "",
        "review_status": review_status,
    }


def _first_evidence_page(value: Any) -> int | None:
    if isinstance(value, dict):
        page = value.get("page")
        if isinstance(page, int):
            return page
        for nested in value.values():
            nested_page = _first_evidence_page(nested)
            if nested_page is not None:
                return nested_page
    if isinstance(value, list):
        for item in value:
            nested_page = _first_evidence_page(item)
            if nested_page is not None:
                return nested_page
    return None
