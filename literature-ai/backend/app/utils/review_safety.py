from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
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

LOCATOR_PAYLOAD_KEYS = {
    "locator_status",
    "provenance_level",
    "page",
    "bbox",
    "can_jump_to_pdf_page",
    "can_highlight_in_pdf",
    "evidence_locator",
}

_TABLE_NAMES_BY_BIND: dict[int, set[str]] = {}


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
    bind = session.get_bind()
    bind_key = id(bind)
    table_names = _TABLE_NAMES_BY_BIND.get(bind_key)
    if table_names is None:
        table_names = set(inspect(bind).get_table_names())
        _TABLE_NAMES_BY_BIND[bind_key] = table_names
    return table_name in table_names


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
    if isinstance(row, CatalystSample):
        return not _is_blank(row.evidence_strength)
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


def _catalyst_has_material_identity(catalyst: CatalystSample | None) -> bool:
    if catalyst is None:
        return False
    return any(
        not _is_blank(value)
        for value in (
            catalyst.name,
            catalyst.catalyst_type,
            catalyst.metal_centers,
            catalyst.coordination,
            catalyst.support,
        )
    )


def _dft_payload_has_material_identity(row: DFTResult) -> bool:
    payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
    corrected_value = payload.get("corrected_value")
    if not isinstance(corrected_value, dict):
        corrected_value = {}
    return any(
        not _is_blank(value)
        for value in (
            payload.get("material_identity"),
            payload.get("material"),
            payload.get("structure_name"),
            corrected_value.get("material_identity"),
            corrected_value.get("material"),
            corrected_value.get("structure_name"),
        )
    )


def has_required_material_identity(session: Session, row: Any) -> bool:
    if not isinstance(row, DFTResult):
        return True
    if _dft_payload_has_material_identity(row):
        return True
    if _is_blank(row.catalyst_sample_id):
        return False
    return _catalyst_has_material_identity(session.get(CatalystSample, row.catalyst_sample_id))


def is_borrowed_supporting_reference(row: Any) -> bool:
    payload = getattr(row, "evidence_payload", None)
    if not isinstance(payload, dict):
        return False
    source_type = str(payload.get("source_document_type") or "").strip().lower()
    return source_type == "supporting_reference" or bool(payload.get("borrowed_from_reference"))


def _safe_locator_from_parts(
    *,
    page: Any,
    locator_status: Any,
    evidence_text: Any = "",
    bbox: Any = None,
    warning_reason: Any = None,
    can_jump_to_pdf_page: Any = None,
) -> bool:
    degradation = locator_degradation(
        page=page,
        locator_status=locator_status,
        evidence_text=str(evidence_text or ""),
        bbox=bbox if isinstance(bbox, dict) else None,
        warning_reason=str(warning_reason) if warning_reason else None,
    )
    if can_jump_to_pdf_page is False:
        return False
    return degradation.locator_status == "exact_page" and degradation.can_jump_to_pdf_page


def _locator_summary(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
    reviews: list[ExtractionFieldReview] | None = None,
) -> tuple[str, str]:
    for review in reviews or []:
        if _review_has_safe_imported_page_anchor(review):
            return "exact_pdf_page", "exact_page"

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
        span_pages = []
        if _table_exists(session, "evidence_spans"):
            span_pages = list(
                session.scalars(
                    select(EvidenceSpan.page).where(
                        EvidenceSpan.paper_id == paper_id,
                        EvidenceSpan.object_id == target_id_str,
                        EvidenceSpan.object_type.in_(target_types),
                        EvidenceSpan.text.is_not(None),
                        EvidenceSpan.text != "",
                    )
                ).all()
            )
        if any(_safe_locator_from_parts(page=page, locator_status="exact_page") for page in span_pages):
            return "exact_pdf_page", "exact_page"
        if span_pages:
            return "text_evidence_only", "missing_page"

        claim_pages = []
        if _table_exists(session, "evidence_claims"):
            claim_pages = list(
                session.execute(
                    select(EvidenceClaim.page_start, EvidenceClaim.page_end).where(
                        EvidenceClaim.paper_id == paper_id,
                        EvidenceClaim.target_id == target_id_str,
                        EvidenceClaim.target_type.in_(target_types),
                        EvidenceClaim.evidence_text.is_not(None),
                        EvidenceClaim.evidence_text != "",
                    )
                ).all()
            )
        if any(
            _safe_locator_from_parts(page=page_start or page_end, locator_status="exact_page")
            for page_start, page_end in claim_pages
        ):
            return "exact_pdf_page", "exact_page"
        if claim_pages:
            return "text_evidence_only", "missing_page"

        return "text_evidence_only", "missing_locator"
    if any(
        _safe_locator_from_parts(
            page=locator.page,
            locator_status=locator.locator_status,
            evidence_text=locator.evidence_text,
            bbox=locator.bbox,
            warning_reason=locator.warning_reason,
        )
        for locator in locators
    ):
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
    has_safe_locator: bool,
    has_material_identity: bool = True,
    borrowed_supporting_reference: bool = False,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if borrowed_supporting_reference:
        reasons.append("supporting_reference_not_main_paper_data")
    if not has_material_identity:
        reasons.append("missing_material_identity")
    if not has_review:
        reasons.append("missing_review")
    elif not has_safe_review:
        reasons.append("unsafe_review")
    if not has_evidence_reference:
        reasons.append("missing_evidence")
    if not has_evidence_text:
        reasons.append("missing_evidence_text")
    if has_evidence_reference and not has_safe_locator:
        reasons.append("unsafe_locator")
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
    provenance_level, locator_status = _locator_summary(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
        reviews=reviews,
    )
    reasons = build_export_gate_reason(
        has_review=has_review,
        has_safe_review=safe_review is not None,
        has_evidence_reference=has_evidence_reference,
        has_evidence_text=has_evidence_text,
        has_safe_locator=provenance_level == "exact_pdf_page" and locator_status == "exact_page",
        has_material_identity=has_required_material_identity(session, row),
        borrowed_supporting_reference=is_borrowed_supporting_reference(row),
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


def bulk_export_gate_results(
    session: Session,
    rows: list[Any],
    *,
    target_type: str,
) -> dict[str, ExportGateResult]:
    """Build export gates for many extracted rows without per-row review/evidence queries."""
    if not rows:
        return {}
    target_types = _target_type_values(target_type)
    row_by_id = {str(row.id): row for row in rows}
    target_ids = set(row_by_id)
    paper_ids = {row.paper_id for row in rows}
    dft_aliases = {_normalized(value) for value in _target_type_values("dft_results")}
    is_dft_target = _normalized(target_type) in dft_aliases

    reviews_by_target: dict[str, list[ExtractionFieldReview]] = {target_id: [] for target_id in target_ids}
    if _table_exists(session, "extraction_field_reviews"):
        for review in session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id.in_(paper_ids),
                ExtractionFieldReview.target_id.in_(target_ids),
                ExtractionFieldReview.target_type.in_(target_types),
            )
        ).all():
            reviews_by_target.setdefault(str(review.target_id), []).append(review)

    locators_by_target: dict[str, list[EvidenceLocator]] = {target_id: [] for target_id in target_ids}
    evidence_reference_ids: set[str] = set()
    if _table_exists(session, "evidence_locators"):
        for locator in session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id.in_(paper_ids),
                EvidenceLocator.target_id.in_(target_ids),
                EvidenceLocator.target_type.in_(target_types),
            )
        ).all():
            target_id = str(locator.target_id)
            locators_by_target.setdefault(target_id, []).append(locator)
            if not _is_blank(locator.evidence_text):
                evidence_reference_ids.add(target_id)

    span_pages_by_target: dict[str, list[Any]] = defaultdict(list)
    if _table_exists(session, "evidence_spans"):
        for object_id, page in session.execute(
            select(EvidenceSpan.object_id, EvidenceSpan.page).where(
                EvidenceSpan.paper_id.in_(paper_ids),
                EvidenceSpan.object_id.in_(target_ids),
                EvidenceSpan.object_type.in_(target_types),
                EvidenceSpan.text.is_not(None),
                EvidenceSpan.text != "",
            )
        ).all():
            target_id = str(object_id)
            evidence_reference_ids.add(target_id)
            span_pages_by_target[target_id].append(page)

    claim_pages_by_target: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
    if _table_exists(session, "evidence_claims"):
        for target_id, page_start, page_end in session.execute(
            select(EvidenceClaim.target_id, EvidenceClaim.page_start, EvidenceClaim.page_end).where(
                EvidenceClaim.paper_id.in_(paper_ids),
                EvidenceClaim.target_id.in_(target_ids),
                EvidenceClaim.target_type.in_(target_types),
                EvidenceClaim.evidence_text.is_not(None),
                EvidenceClaim.evidence_text != "",
            )
        ).all():
            target_id_str = str(target_id)
            evidence_reference_ids.add(target_id_str)
            claim_pages_by_target[target_id_str].append((page_start, page_end))

    material_identity_ids: set[str] = set()
    if is_dft_target:
        catalyst_ids = {
            row.catalyst_sample_id
            for row in rows
            if isinstance(row, DFTResult) and not _is_blank(row.catalyst_sample_id)
        }
        if catalyst_ids:
            for catalyst in session.scalars(select(CatalystSample).where(CatalystSample.id.in_(catalyst_ids))).all():
                if _catalyst_has_material_identity(catalyst):
                    material_identity_ids.add(str(catalyst.id))

    gates: dict[str, ExportGateResult] = {}
    for target_id, row in row_by_id.items():
        reviews = reviews_by_target.get(target_id, [])
        safe_review = next((review for review in reviews if is_safe_verified_review(review)), None)
        provenance_level, locator_status = _bulk_locator_summary(
            locators_by_target.get(target_id, []),
            span_pages_by_target.get(target_id, []),
            claim_pages_by_target.get(target_id, []),
            reviews,
        )
        reasons = build_export_gate_reason(
            has_review=bool(reviews),
            has_safe_review=safe_review is not None,
            has_evidence_reference=target_id in evidence_reference_ids,
            has_evidence_text=has_required_evidence_text(row),
            has_safe_locator=provenance_level == "exact_pdf_page" and locator_status == "exact_page",
            has_material_identity=(
                _dft_payload_has_material_identity(row) or str(row.catalyst_sample_id) in material_identity_ids
                if is_dft_target and isinstance(row, DFTResult)
                else True
            ),
            borrowed_supporting_reference=is_dft_target and is_borrowed_supporting_reference(row),
        )
        review_status = safe_review.reviewer_status if safe_review is not None else (
            ",".join(sorted({_normalized(review.reviewer_status) or "unknown" for review in reviews}))
            if reviews
            else "missing"
        )
        gates[target_id] = ExportGateResult(
            eligible=not reasons,
            reasons=reasons,
            review_status=review_status,
            review_gate_status="safe_verified" if not reasons else "blocked",
            provenance_level=provenance_level,
            locator_status=locator_status,
        )
    return gates


def _bulk_locator_summary(
    locators: list[EvidenceLocator],
    span_pages: list[Any],
    claim_pages: list[tuple[Any, Any]],
    reviews: list[ExtractionFieldReview] | None = None,
) -> tuple[str, str]:
    for review in reviews or []:
        if _review_has_safe_imported_page_anchor(review):
            return "exact_pdf_page", "exact_page"

    if locators:
        if any(
            _safe_locator_from_parts(
                page=locator.page,
                locator_status=locator.locator_status,
                evidence_text=locator.evidence_text,
                bbox=locator.bbox,
                warning_reason=locator.warning_reason,
            )
            for locator in locators
        ):
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

    if any(_safe_locator_from_parts(page=page, locator_status="exact_page") for page in span_pages):
        return "exact_pdf_page", "exact_page"
    if span_pages:
        return "text_evidence_only", "missing_page"
    if any(
        _safe_locator_from_parts(page=page_start or page_end, locator_status="exact_page")
        for page_start, page_end in claim_pages
    ):
        return "exact_pdf_page", "exact_page"
    if claim_pages:
        return "text_evidence_only", "missing_page"
    return "text_evidence_only", "missing_locator"


def _review_has_safe_imported_page_anchor(review: ExtractionFieldReview) -> bool:
    if not is_safe_verified_review(review):
        return False
    review_payload = review.review_payload if isinstance(review.review_payload, dict) else {}
    imported = review_payload.get("imported_evidence_payload")
    imported_items = imported if isinstance(imported, list) else [imported]
    return any(
        isinstance(item, dict)
        and _safe_locator_from_parts(
            page=item.get("page"),
            locator_status="exact_page",
            evidence_text=(
                item.get("quoted_text")
                or item.get("evidence_text")
                or item.get("section")
                or item.get("table")
                or item.get("figure")
                or "reviewed PDF page"
            ),
        )
        for item in imported_items
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


def _locator_payloads(value: Any) -> list[dict[str, Any]]:
    payloads = []
    for item in _iter_dicts(value):
        nested = item.get("evidence_locator")
        if isinstance(nested, dict):
            payloads.append(nested)
        if any(key in item for key in LOCATOR_PAYLOAD_KEYS - {"evidence_locator"}):
            payloads.append(item)
    return payloads


def _safe_locator_payload(item: dict[str, Any]) -> bool:
    return _safe_locator_from_parts(
        page=item.get("page"),
        locator_status=item.get("locator_status"),
        evidence_text=item.get("evidence_text") or item.get("text") or "",
        bbox=item.get("bbox"),
        warning_reason=item.get("warning_reason"),
        can_jump_to_pdf_page=item.get("can_jump_to_pdf_page"),
    )


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
    locator_payloads = _locator_payloads(evidence_chain)
    if locator_payloads and not all(_safe_locator_payload(item) for item in locator_payloads):
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="present",
            review_gate_status="blocked",
            blocked_reasons=("unsafe_locator",),
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


# ---------------------------------------------------------------------------
# D1 Phase 3 Review Boundary Enforcement helpers
# ---------------------------------------------------------------------------


def normalize_review_status(review: ExtractionFieldReview | dict[str, Any] | None) -> str:
    """Return a normalized reviewer_status string."""
    if review is None:
        return "missing"
    if isinstance(review, dict):
        raw = review.get("reviewer_status") or review.get("review_status") or review.get("status")
        return _normalized(raw) or "unknown"
    return _normalized(review.reviewer_status) or "unknown"


def normalize_target_resolution_status(review: ExtractionFieldReview | dict[str, Any] | None) -> str:
    """Return a normalized target_resolution_status string."""
    if review is None:
        return "missing"
    if isinstance(review, dict):
        raw = (
            review.get("target_resolution_status")
            or review.get("resolution_status")
            or review.get("review_resolution_status")
        )
        return _normalized(raw) or "unknown"
    return _normalized(review.target_resolution_status) or "unknown"


def is_unsafe_review_status(review: ExtractionFieldReview | dict[str, Any] | None) -> bool:
    """Return True if the review has an unsafe reviewer_status or target_resolution_status."""
    if review is None:
        return True
    rs = normalize_review_status(review)
    if rs in UNSAFE_REVIEWER_STATUSES:
        return True
    trs = normalize_target_resolution_status(review)
    if trs in UNSAFE_TARGET_RESOLUTION_STATUSES:
        return True
    return False


def can_ai_candidate_update_target(
    *,
    existing_review: ExtractionFieldReview | None,
    candidate_source: str,
) -> bool:
    """AI / external candidates must never overwrite a human-verified review.

    Returns True only when the candidate is allowed to write.
    """
    if existing_review is None:
        return True
    if normalize_review_status(existing_review) != SAFE_REVIEWER_STATUS:
        return True
    # Existing review is verified — block AI/external overwrite
    ai_sources = {"internal_ai", "external", "mcp_review", "auto"}
    if candidate_source in ai_sources:
        return False
    # Manual source explicitly marking verified is allowed
    return True


def can_manual_review_mark_verified(
    *,
    target_exists: bool,
    evidence_reference_exists: bool,
    evidence_text_exists: bool,
    target_resolution_status: str,
) -> tuple[bool, str]:
    """Check whether a manual review can be marked verified.

    Returns (allowed, reason) where reason is empty when allowed.
    """
    if not target_exists:
        return False, "target_not_found"
    if not evidence_reference_exists:
        return False, "missing_evidence_reference"
    if not evidence_text_exists:
        return False, "missing_evidence_text"
    trs = _normalized(target_resolution_status)
    if trs not in SAFE_TARGET_RESOLUTION_STATUSES and trs not in {"active", "remapped"}:
        return False, f"unsafe_target_resolution_status:{trs or 'missing'}"
    return True, ""


def build_review_boundary_reason(
    *,
    review: ExtractionFieldReview | dict[str, Any] | None,
    is_ai_candidate: bool = False,
    is_external_candidate: bool = False,
    has_evidence_payload: bool = True,
) -> str:
    """Build a human-readable reason string for why a review is at the boundary."""
    rs = normalize_review_status(review)
    trs = normalize_target_resolution_status(review)
    parts: list[str] = []

    if is_ai_candidate:
        parts.append("ai_candidate")
    if is_external_candidate:
        parts.append("external_candidate")

    if rs != SAFE_REVIEWER_STATUS:
        parts.append(f"reviewer_status={rs}")
    elif trs not in SAFE_TARGET_RESOLUTION_STATUSES:
        parts.append(f"target_resolution={trs}")
    else:
        parts.append("safe_verified")

    if is_external_candidate and not has_evidence_payload:
        parts.append("missing_evidence_payload")

    return ";".join(parts) if parts else "ok"


@dataclass(frozen=True)
class ReviewBoundaryGate:
    """Result of a review boundary check for serialization / export."""
    is_safe_verified: bool
    reviewer_status: str
    target_resolution_status: str
    blocked_reasons: tuple[str, ...]
    boundary_label: str


def serialize_review_gate(
    review: ExtractionFieldReview | dict[str, Any] | None,
    *,
    is_ai_candidate: bool = False,
    is_external_candidate: bool = False,
    has_evidence_payload: bool = True,
) -> ReviewBoundaryGate:
    """Serialize a review through the boundary gate.

    Unsafe reviews get blocked_reasons and a non-safe boundary_label.
    This is the single canonical path for deciding whether a review
    can enter export/writing trusted paths.
    """
    rs = normalize_review_status(review)
    trs = normalize_target_resolution_status(review)
    safe = is_safe_verified_review(review)

    blocked: list[str] = []
    if not safe:
        if rs != SAFE_REVIEWER_STATUS:
            blocked.append(f"unsafe_reviewer_status:{rs}")
        if trs not in SAFE_TARGET_RESOLUTION_STATUSES:
            blocked.append(f"unsafe_target_resolution:{trs}")
        if review is None:
            blocked.append("missing_review")

    if is_ai_candidate and rs == SAFE_REVIEWER_STATUS:
        blocked.append("ai_candidate_cannot_be_verified")
    if is_external_candidate and not has_evidence_payload:
        blocked.append("external_candidate_missing_evidence_payload")
    if is_external_candidate and rs == SAFE_REVIEWER_STATUS:
        blocked.append("external_candidate_cannot_be_verified")

    label = "safe_verified" if not blocked else "blocked"
    return ReviewBoundaryGate(
        is_safe_verified=safe and not blocked,
        reviewer_status=rs,
        target_resolution_status=trs,
        blocked_reasons=tuple(blocked),
        boundary_label=label,
    )
