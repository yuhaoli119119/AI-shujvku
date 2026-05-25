from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.db.models import (
    DFTResult,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    WritingCard,
)
from app.utils.locator_degradation import locator_degradation


SAFE_REVIEWER_STATUS = "verified"
SAFE_TARGET_RESOLUTION_STATUSES = {"active", "remapped"}
UNSAFE_REVIEWER_STATUSES = {"stale", "ambiguous", "unresolved", "unknown", "pending", ""}
UNSAFE_TARGET_RESOLUTION_STATUSES = {"stale", "ambiguous", "unresolved", "unknown", ""}

TARGET_TYPE_ALIASES: dict[str, set[str]] = {
    "dft_results": {"dft_results", "dft_result", "DFTResult"},
    "mechanism_claims": {"mechanism_claims", "mechanism_claim", "MechanismClaim"},
    "electrochemical_performance": {
        "electrochemical_performance",
        "electrochemical",
        "ElectrochemicalPerformance",
    },
    "catalyst_samples": {"catalyst_samples", "catalyst_sample", "CatalystSample"},
    "dft_settings": {"dft_settings", "dft_setting", "DFTSetting"},
    "writing_cards": {"writing_cards", "writing_card", "WritingCard"},
}


@dataclass(frozen=True)
class ExportGateResult:
    eligible: bool
    reasons: tuple[str, ...]
    review_status: str
    review_gate_status: str
    provenance_level: str
    locator_status: str


@dataclass(frozen=True)
class WritingGateResult:
    can_use_for_writing: bool
    evidence_chain_status: str
    review_gate_status: str
    blocked_reasons: tuple[str, ...]


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _target_type_values(target_type: str) -> set[str]:
    return TARGET_TYPE_ALIASES.get(target_type, {target_type})


def _table_exists(session: Session, table_name: str) -> bool:
    return table_name in set(inspect(session.bind).get_table_names())


def is_safe_verified_review(review: ExtractionFieldReview | dict[str, Any] | None) -> bool:
    if review is None:
        return False
    if isinstance(review, dict):
        reviewer_status = _normalized(
            review.get("reviewer_status")
            or review.get("review_status")
            or review.get("status")
        )
        resolution_status = _normalized(
            review.get("target_resolution_status")
            or review.get("resolution_status")
            or review.get("review_resolution_status")
            or "active"
        )
    else:
        reviewer_status = _normalized(review.reviewer_status)
        resolution_status = _normalized(review.target_resolution_status)
    return reviewer_status == SAFE_REVIEWER_STATUS and resolution_status in SAFE_TARGET_RESOLUTION_STATUSES


def get_target_reviews(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> list[ExtractionFieldReview]:
    if not _table_exists(session, "extraction_field_reviews"):
        return []
    target_types = _target_type_values(target_type)
    return list(
        session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_id == str(target_id),
                ExtractionFieldReview.target_type.in_(target_types),
            )
        ).all()
    )


def has_safe_verified_review(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> bool:
    return any(
        is_safe_verified_review(review)
        for review in get_target_reviews(
            session,
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
        )
    )


def has_required_evidence_text(row: Any) -> bool:
    if isinstance(row, DFTResult):
        return not _is_blank(row.evidence_text)
    return not _is_blank(getattr(row, "evidence_text", None))


def has_required_evidence_reference(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> bool:
    target_id_str = str(target_id)
    target_types = _target_type_values(target_type)
    if _table_exists(session, "evidence_spans"):
        span_exists = session.scalar(
            select(EvidenceSpan.id)
            .where(
                EvidenceSpan.paper_id == paper_id,
                EvidenceSpan.object_id == target_id_str,
                EvidenceSpan.object_type.in_(target_types),
                EvidenceSpan.text.is_not(None),
                EvidenceSpan.text != "",
            )
            .limit(1)
        )
        if span_exists is not None:
            return True

    if _table_exists(session, "evidence_claims"):
        claim_exists = session.scalar(
            select(EvidenceClaim.id)
            .where(
                EvidenceClaim.paper_id == paper_id,
                EvidenceClaim.target_id == target_id_str,
                EvidenceClaim.target_type.in_(target_types),
                EvidenceClaim.evidence_text.is_not(None),
                EvidenceClaim.evidence_text != "",
            )
            .limit(1)
        )
        if claim_exists is not None:
            return True

    if _table_exists(session, "evidence_locators"):
        locator_exists = session.scalar(
            select(EvidenceLocator.id)
            .where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_id == target_id_str,
                EvidenceLocator.target_type.in_(target_types),
                EvidenceLocator.evidence_text.is_not(None),
                EvidenceLocator.evidence_text != "",
            )
            .limit(1)
        )
        return locator_exists is not None
    return False


def _locator_summary(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> tuple[str, str]:
    target_id_str = str(target_id)
    target_types = _target_type_values(target_type)
    if not _table_exists(session, "evidence_locators"):
        return "text_only", "missing_locator"
    locators = list(
        session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_id == target_id_str,
                EvidenceLocator.target_type.in_(target_types),
            )
        ).all()
    )
    if not locators:
        return "text_evidence_only", "missing_locator"
    if any(locator.page is not None for locator in locators):
        return "exact_pdf_page", "exact_page"
    statuses = [
        locator_degradation(
            page=locator.page,
            locator_status=locator.locator_status,
            evidence_text=locator.evidence_text,
            bbox=locator.bbox,
            warning_reason=locator.warning_reason,
        ).locator_status
        for locator in locators
    ]
    if "approximate" in statuses:
        return "approximate_pdf_page", "approximate"
    if "unresolved" in statuses:
        return "unavailable", "unresolved"
    if "text_only" in statuses:
        return "text_evidence_only", "text_only"
    return "text_evidence_only", "missing_page"


def build_export_gate_reason(
    *,
    has_review: bool,
    has_safe_review: bool,
    has_evidence_reference: bool,
    has_evidence_text: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not has_review:
        reasons.append("missing_review")
    elif not has_safe_review:
        reasons.append("unsafe_review")
    if not has_evidence_reference:
        reasons.append("missing_evidence")
    if not has_evidence_text:
        reasons.append("missing_evidence_text")
    return tuple(reasons)


def is_export_eligible_extraction(
    session: Session,
    row: Any,
    *,
    target_type: str,
) -> ExportGateResult:
    reviews = get_target_reviews(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
    )
    has_review = bool(reviews)
    safe_review = next((review for review in reviews if is_safe_verified_review(review)), None)
    has_evidence_reference = has_required_evidence_reference(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
    )
    has_evidence_text = has_required_evidence_text(row)
    reasons = build_export_gate_reason(
        has_review=has_review,
        has_safe_review=safe_review is not None,
        has_evidence_reference=has_evidence_reference,
        has_evidence_text=has_evidence_text,
    )
    provenance_level, locator_status = _locator_summary(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
    )
    review_status = safe_review.reviewer_status if safe_review is not None else (
        ",".join(sorted({_normalized(review.reviewer_status) or "unknown" for review in reviews})) if reviews else "missing"
    )
    return ExportGateResult(
        eligible=not reasons,
        reasons=reasons,
        review_status=review_status,
        review_gate_status="safe_verified" if not reasons else "blocked",
        provenance_level=provenance_level,
        locator_status=locator_status,
    )


def summarize_gate_results(results: list[ExportGateResult]) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    for result in results:
        for reason in result.reasons:
            reason_counts[reason] += 1
    return {
        "total_candidates": len(results),
        "eligible": sum(1 for result in results if result.eligible),
        "blocked": sum(1 for result in results if not result.eligible),
        "blocked_reasons": dict(sorted(reason_counts.items())),
    }


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        items.append(value)
        for nested in value.values():
            items.extend(_iter_dicts(nested))
    elif isinstance(value, list):
        for nested in value:
            items.extend(_iter_dicts(nested))
    return items


def writing_card_gate(card: WritingCard) -> WritingGateResult:
    evidence_chain = card.evidence_chain
    if _is_blank(evidence_chain):
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="missing",
            review_gate_status="blocked",
            blocked_reasons=("missing_evidence_chain",),
        )

    review_payloads = [
        item
        for item in _iter_dicts(evidence_chain)
        if any(key in item for key in ("reviewer_status", "review_status", "target_resolution_status", "resolution_status"))
    ]
    if not review_payloads:
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="present",
            review_gate_status="blocked",
            blocked_reasons=("missing_review",),
        )
    if not all(is_safe_verified_review(item) for item in review_payloads):
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="present",
            review_gate_status="blocked",
            blocked_reasons=("unsafe_review",),
        )
    return WritingGateResult(
        can_use_for_writing=True,
        evidence_chain_status="present",
        review_gate_status="safe_verified",
        blocked_reasons=(),
    )


def external_candidate_has_evidence(candidate: ExternalAnalysisCandidate) -> bool:
    return not _is_blank(candidate.evidence_payload)


def trusted_external_candidate(candidate: ExternalAnalysisCandidate) -> bool:
    return external_candidate_has_evidence(candidate) and _normalized(candidate.status) in {"pending", "materialized"}
