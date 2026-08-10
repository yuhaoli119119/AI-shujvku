from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import re
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    CatalystSample,
    ContentEvidenceItem,
    DFTAuditIssue,
    DFTResult,
    EvidenceClaim,
    EvidenceLocator,
    EvidenceSpan,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperSection,
    WritingCard,
)
from app.utils.evidence_anchors import first_pdf_evidence_anchor
from app.utils.artifact_paths import resolve_paper_pdf_path
from app.services.evidence_page_recovery import PaperPageTextProvider, compact_page_text
from app.utils.locator_degradation import locator_degradation
from app.utils.writing_card_content import normalized_evidence_chain
from app.utils.ai_verification import (
    AI_VERIFIED_STATUS,
    ai_review_payload_structurally_valid,
    authoritative_ai_review_valid,
    get_ai_target,
)
from app.normalizers.chemistry_normalizer import get_property_taxonomy
from app.services.dft_identity_service import (
    property_requires_atom_pair,
    resolve_atom_pair_identity,
)
from app.services.dft_audit_issue_lifecycle_service import DFT_AUDIT_ISSUE_PENDING_STATUSES
from app.utils.dft_candidate_status import DFT_REJECTED_STATUSES


SAFE_REVIEWER_STATUS = "verified"
SAFE_REVIEWER_STATUSES = {SAFE_REVIEWER_STATUS, AI_VERIFIED_STATUS}
SAFE_TARGET_RESOLUTION_STATUSES = {"active", "remapped"}
UNSAFE_REVIEWER_STATUSES = {
    "stale",
    "ambiguous",
    "unresolved",
    "unknown",
    "pending",
    "rejected",
    "superseded",
    "conflict",
    "failed",
    "needs_human",
    "blocked",
    "exception",
    "",
}
UNSAFE_TARGET_RESOLUTION_STATUSES = {"stale", "ambiguous", "unresolved", "unknown", "conflict", "rejected", ""}

DFT_RESULT_CONFLICT_ISSUE_TYPES = {"duplicate_suspected", "negative_consensus"}
DFT_RESULT_CONFLICT_CODES = {"binding_conflict", "scientific_conflict", "identity_conflict"}
MISSING_UNIT_MARKERS = {
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "unspecified",
    "not reported",
    "not specified",
    "not specified in evidence",
    "source_unreported",
}

TARGET_TYPE_ALIASES: dict[str, set[str]] = {
    "abstract": {"abstract", "paper_abstract", "summary", "paper_summary"},
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
    "sections": {"sections", "section", "paper_section", "PaperSection"},
    "section_page_fragments": {
        "section_page_fragments",
        "section_page_fragment",
        "SectionPageFragment",
    },
}

CONTENT_OBJECT_GATE_POLICY_VERSION = "content_object_gate.v1"

REQUIRED_REVIEW_FIELDS_BY_TARGET_TYPE: dict[str, tuple[str, ...]] = {
    "mechanism_claims": ("claim_text",),
    "sections": ("text",),
    "section_page_fragments": ("text",),
    "writing_cards": ("evidence_chain",),
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

_TABLE_NAMES_BY_BIND: WeakKeyDictionary[Any, set[str]] = WeakKeyDictionary()
_BATCH_REVIEWS_CACHE_KEY = "dft_import_reviews_by_target"
_BATCH_EVIDENCE_CACHE_KEY = "dft_import_evidence_reference_ids"
_BATCH_CONFLICT_CACHE_KEY = "dft_import_open_conflict_ids"


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


@dataclass(frozen=True)
class ContentObjectGateResult:
    can_use_for_writing: bool
    can_use_for_citation: bool
    review_gate_status: str
    locator_status: str
    blocked_reasons: tuple[str, ...]
    policy_version: str = CONTENT_OBJECT_GATE_POLICY_VERSION


@dataclass(frozen=True)
class VerificationPromotionGateResult:
    eligible: bool
    reasons: tuple[str, ...]
    provenance_level: str
    locator_status: str


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
    table_names = _TABLE_NAMES_BY_BIND.get(bind)
    if table_names is None:
        table_names = set(inspect(bind).get_table_names())
        _TABLE_NAMES_BY_BIND[bind] = table_names
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
        review_payload = review.get("review_payload")
    else:
        reviewer_status = _normalized(review.reviewer_status)
        resolution_status = _normalized(review.target_resolution_status)
        review_payload = review.review_payload
    payload = review_payload if isinstance(review_payload, dict) else {}
    if reviewer_status == AI_VERIFIED_STATUS:
        return ai_review_payload_structurally_valid(review)
    if "ai_verification" in payload and "human_verification" not in payload:
        return False
    return reviewer_status == SAFE_REVIEWER_STATUS and resolution_status in SAFE_TARGET_RESOLUTION_STATUSES


def is_authoritative_verified_review(
    session: Session,
    review: ExtractionFieldReview,
    target: Any,
) -> bool:
    """Revalidate AI evidence, locator and target snapshots at admission time."""

    if _normalized(review.reviewer_status) == AI_VERIFIED_STATUS:
        return authoritative_ai_review_valid(session, review, target)
    return is_safe_verified_review(review)


def required_review_fields(target_type: str) -> tuple[str, ...]:
    """Return object-type-specific fields that authorize the whole object."""

    return REQUIRED_REVIEW_FIELDS_BY_TARGET_TYPE.get(_canonical_content_target_type(target_type), ())


def get_target_reviews(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> list[ExtractionFieldReview]:
    batch_reviews = session.info.get(_BATCH_REVIEWS_CACHE_KEY)
    cache_key = (str(paper_id), _normalized(target_type), str(target_id))
    if isinstance(batch_reviews, dict) and cache_key in batch_reviews:
        return list(batch_reviews[cache_key])
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


def dft_export_data_quality_reasons(row: DFTResult, session: Session | None = None) -> tuple[str, ...]:
    property_type = _normalized(row.property_type)
    reasons: list[str] = []
    if property_requires_atom_pair(property_type):
        unit = _normalized(row.unit)
        if not unit or unit in MISSING_UNIT_MARKERS:
            reasons.append("missing_required_unit")
        payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        atom_pair = resolve_atom_pair_identity(payload, property_type=property_type)
        if atom_pair.error_code:
            reasons.append(atom_pair.error_code)
    if session is not None:
        from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService

        taxonomy = get_property_taxonomy(row.property_type)
        is_text_claim = taxonomy["ml_role"] == "lm_auxiliary" or taxonomy["physical_dimension"] == "text"
        if not is_text_claim:
            identity = DFTAuditIssueLifecycleService(session).identity_for_result(row)
            reasons.extend(identity.error_codes)
    return tuple(dict.fromkeys(reasons))


def _open_dft_result_conflict_ids(session: Session, rows: list[DFTResult]) -> set[str]:
    if not rows or not _table_exists(session, "dft_audit_issues"):
        return set()
    paper_ids = {row.paper_id for row in rows}
    target_ids = {str(row.id) for row in rows}
    batch_cache = session.info.get(_BATCH_CONFLICT_CACHE_KEY)
    if isinstance(batch_cache, dict) and target_ids <= set(batch_cache.get("target_ids") or set()):
        return target_ids & set(batch_cache.get("conflict_ids") or set())
    result_ids = {row.id for row in rows}
    issues = session.scalars(
        select(DFTAuditIssue).where(
            DFTAuditIssue.paper_id.in_(paper_ids),
            DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
            or_(
                DFTAuditIssue.issue_type.in_(sorted(DFT_RESULT_CONFLICT_ISSUE_TYPES)),
                DFTAuditIssue.lifecycle_stage.in_(sorted(DFT_RESULT_CONFLICT_CODES)),
                DFTAuditIssue.resolution_code.in_(sorted(DFT_RESULT_CONFLICT_CODES)),
                DFTAuditIssue.last_error_code.in_(sorted(DFT_RESULT_CONFLICT_CODES)),
            ),
            or_(
                DFTAuditIssue.result_id.in_(result_ids),
                DFTAuditIssue.target_id.in_(target_ids),
            ),
        )
    ).all()
    conflict_ids: set[str] = set()
    for issue in issues:
        if issue.result_id is not None:
            conflict_ids.add(str(issue.result_id))
        target_id = str(issue.target_id or "").strip()
        if target_id in target_ids:
            conflict_ids.add(target_id)
    return conflict_ids


def has_safe_verified_review(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
) -> bool:
    for review in get_target_reviews(
        session,
        paper_id=paper_id,
        target_type=target_type,
        target_id=target_id,
    ):
        if _normalized(review.reviewer_status) != AI_VERIFIED_STATUS:
            if is_safe_verified_review(review):
                return True
            continue
        try:
            _canonical, target = get_ai_target(
                session,
                paper_id=UUID(str(paper_id)),
                target_type=target_type,
                target_id=str(target_id),
            )
        except (LookupError, ValueError):
            continue
        if is_authoritative_verified_review(session, review, target):
            return True
    return False


def has_required_evidence_text(row: Any) -> bool:
    if isinstance(row, DFTResult):
        return not _is_blank(row.evidence_text)
    if isinstance(row, CatalystSample):
        return not _is_blank(row.evidence_strength)
    if isinstance(row, PaperSection):
        return not _is_blank(row.text)
    if isinstance(row, WritingCard):
        return writing_card_content_gate(row).can_use_for_writing
    return not _is_blank(getattr(row, "evidence_text", None))


def has_required_evidence_reference(
    session: Session,
    *,
    paper_id: Any,
    target_type: str,
    target_id: Any,
    field_name: str | None = None,
    require_field_match: bool = False,
) -> bool:
    target_id_str = str(target_id)
    batch_evidence_ids = session.info.get(_BATCH_EVIDENCE_CACHE_KEY)
    cache_key = (str(paper_id), _normalized(target_type), target_id_str)
    if isinstance(batch_evidence_ids, set) and cache_key in batch_evidence_ids:
        return True
    target_types = _target_type_values(target_type)
    if not require_field_match and _table_exists(session, "evidence_spans"):
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

    if not require_field_match and _table_exists(session, "evidence_claims"):
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
        locator_stmt = select(EvidenceLocator.id).where(
            EvidenceLocator.paper_id == paper_id,
            EvidenceLocator.target_id == target_id_str,
            EvidenceLocator.target_type.in_(target_types),
            EvidenceLocator.evidence_text.is_not(None),
            EvidenceLocator.evidence_text != "",
        )
        if require_field_match:
            locator_stmt = locator_stmt.where(EvidenceLocator.field_name == field_name)
        locator_exists = session.scalar(locator_stmt.limit(1))
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
    field_name: str | None = None,
    require_field_match: bool = False,
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
    if require_field_match:
        locators = [locator for locator in locators if locator.field_name == field_name]
    if not locators:
        if require_field_match:
            return "text_evidence_only", "missing_locator"
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


def verification_promotion_gate(
    session: Session,
    *,
    paper: Paper | None,
    review: ExtractionFieldReview,
    evidence_text: Any | None = None,
) -> VerificationPromotionGateResult:
    """Shared exact-PDF gate for final review promotion.

    This deliberately evaluates canonical evidence rows rather than request
    booleans or projection state. The resulting locator semantics are the same
    ones used by export/content-object gates.
    """

    reasons: list[str] = []
    resolved_pdf = (
        resolve_paper_pdf_path(paper.pdf_path, get_settings().storage_root)
        if paper is not None and not _is_blank(paper.pdf_path)
        else None
    )
    if paper is None or resolved_pdf is None or _normalized(paper.oa_status) == "metadata_only":
        reasons.append("missing_real_pdf")
    resolved_evidence_text = review.evidence_text if evidence_text is None else evidence_text
    if _is_blank(resolved_evidence_text):
        reasons.append("missing_evidence_text")
    resolution_status = _normalized(review.target_resolution_status)
    if resolution_status not in SAFE_TARGET_RESOLUTION_STATUSES:
        reasons.append(f"unsafe_target_resolution_status:{resolution_status or 'missing'}")

    canonical_type = _canonical_content_target_type(review.target_type)
    strict_field_match = canonical_type == "mechanism_claims"
    provenance_level, locator_status = _locator_summary(
        session,
        paper_id=review.paper_id,
        target_type=review.target_type,
        target_id=review.target_id,
        reviews=[review],
        field_name=review.field_name,
        require_field_match=strict_field_match,
    )
    if provenance_level != "exact_pdf_page" or locator_status != "exact_page":
        reasons.append(f"locator_not_exact_page:{locator_status}")
    return VerificationPromotionGateResult(
        eligible=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        provenance_level=provenance_level,
        locator_status=locator_status,
    )


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
    is_dft_target = _normalized(target_type) in {
        _normalized(value) for value in _target_type_values("dft_results")
    }
    reviews = get_target_reviews(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
    )
    required_fields = required_review_fields(target_type)
    gate_reviews = (
        [review for review in reviews if review.field_name in required_fields]
        if required_fields
        else reviews
    )
    has_review = (
        all(any(review.field_name == field_name for review in gate_reviews) for field_name in required_fields)
        if required_fields
        else bool(gate_reviews)
    )
    safe_review = next((review for review in gate_reviews if is_authoritative_verified_review(session, review, row)), None)
    has_unsafe_review = any(is_unsafe_review_status(review) for review in gate_reviews)
    required_reviews_safe = (
        all(
            any(review.field_name == field_name and is_authoritative_verified_review(session, review, row) for review in gate_reviews)
            for field_name in required_fields
        )
        if required_fields
        else safe_review is not None
    )
    effective_safe_review = safe_review if required_reviews_safe and (is_dft_target or not has_unsafe_review) else None
    required_field_name = required_fields[0] if len(required_fields) == 1 else None
    has_evidence_reference = has_required_evidence_reference(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
        field_name=required_field_name,
        require_field_match=bool(required_fields),
    )
    has_evidence_text = has_required_evidence_text(row) or (
        not is_dft_target and has_evidence_reference
    )
    provenance_level, locator_status = _locator_summary(
        session,
        paper_id=row.paper_id,
        target_type=target_type,
        target_id=row.id,
        reviews=gate_reviews,
        field_name=required_field_name,
        require_field_match=bool(required_fields),
    )
    reasons = build_export_gate_reason(
        has_review=has_review,
        has_safe_review=effective_safe_review is not None,
        has_evidence_reference=has_evidence_reference,
        has_evidence_text=has_evidence_text,
        has_safe_locator=provenance_level == "exact_pdf_page" and locator_status == "exact_page",
        has_material_identity=has_required_material_identity(session, row),
        borrowed_supporting_reference=is_borrowed_supporting_reference(row),
    )
    if required_fields and not has_review:
        reasons = tuple(
            dict.fromkeys(
                (*reasons, *(f"missing_required_review:{field_name}" for field_name in required_fields))
            )
        )
    if is_dft_target and has_evidence_text:
        reasons = tuple(reason for reason in reasons if reason not in {"missing_evidence", "unsafe_locator"})
    if is_dft_target and _normalized(getattr(row, "candidate_status", None)) in DFT_REJECTED_STATUSES and "target_rejected" not in reasons:
        reasons = (*reasons, "target_rejected")
    if is_dft_target:
        if isinstance(row, DFTResult):
            reasons = (*reasons, *dft_export_data_quality_reasons(row, session))
            if str(row.id) in _open_dft_result_conflict_ids(session, [row]):
                reasons = (*reasons, "open_result_level_conflict")
        reasons = tuple(dict.fromkeys(reasons))
    review_status = effective_safe_review.reviewer_status if effective_safe_review is not None else (
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
    required_fields = required_review_fields(target_type)

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
    open_conflict_ids: set[str] = set()
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
        open_conflict_ids = _open_dft_result_conflict_ids(
            session,
            [row for row in rows if isinstance(row, DFTResult)],
        )

    gates: dict[str, ExportGateResult] = {}
    for target_id, row in row_by_id.items():
        reviews = reviews_by_target.get(target_id, [])
        gate_reviews = (
            [review for review in reviews if review.field_name in required_fields]
            if required_fields
            else reviews
        )
        has_required_reviews = (
            all(any(review.field_name == field_name for review in gate_reviews) for field_name in required_fields)
            if required_fields
            else bool(gate_reviews)
        )
        safe_review = next((review for review in gate_reviews if is_authoritative_verified_review(session, review, row)), None)
        has_unsafe_review = any(is_unsafe_review_status(review) for review in gate_reviews)
        required_reviews_safe = (
            all(
                any(review.field_name == field_name and is_authoritative_verified_review(session, review, row) for review in gate_reviews)
                for field_name in required_fields
            )
            if required_fields
            else safe_review is not None
        )
        effective_safe_review = (
            safe_review
            if required_reviews_safe and (is_dft_target or not has_unsafe_review)
            else None
        )
        required_field_name = required_fields[0] if len(required_fields) == 1 else None
        target_locators = locators_by_target.get(target_id, [])
        if required_fields:
            target_locators = [
                locator for locator in target_locators
                if locator.field_name == required_field_name
            ]
        provenance_level, locator_status = _bulk_locator_summary(
            target_locators,
            [] if required_fields else span_pages_by_target.get(target_id, []),
            [] if required_fields else claim_pages_by_target.get(target_id, []),
            gate_reviews,
        )
        has_evidence_reference = (
            any(not _is_blank(locator.evidence_text) for locator in target_locators)
            if required_fields
            else target_id in evidence_reference_ids
        )
        reasons = build_export_gate_reason(
            has_review=has_required_reviews,
            has_safe_review=effective_safe_review is not None,
            has_evidence_reference=has_evidence_reference,
            has_evidence_text=has_required_evidence_text(row) or (
                not is_dft_target and has_evidence_reference
            ),
            has_safe_locator=provenance_level == "exact_pdf_page" and locator_status == "exact_page",
            has_material_identity=(
                _dft_payload_has_material_identity(row) or str(row.catalyst_sample_id) in material_identity_ids
                if is_dft_target and isinstance(row, DFTResult)
                else True
            ),
            borrowed_supporting_reference=is_dft_target and is_borrowed_supporting_reference(row),
        )
        if required_fields and not has_required_reviews:
            reasons = tuple(
                dict.fromkeys(
                    (*reasons, *(f"missing_required_review:{field_name}" for field_name in required_fields))
                )
            )
        if is_dft_target and has_required_evidence_text(row):
            reasons = tuple(reason for reason in reasons if reason not in {"missing_evidence", "unsafe_locator"})
        if is_dft_target and _normalized(getattr(row, "candidate_status", None)) in DFT_REJECTED_STATUSES and "target_rejected" not in reasons:
            reasons = (*reasons, "target_rejected")
        if is_dft_target:
            if isinstance(row, DFTResult):
                reasons = (*reasons, *dft_export_data_quality_reasons(row, session))
                if target_id in open_conflict_ids:
                    reasons = (*reasons, "open_result_level_conflict")
            reasons = tuple(dict.fromkeys(reasons))
        review_status = effective_safe_review.reviewer_status if effective_safe_review is not None else (
            ",".join(sorted({_normalized(review.reviewer_status) or "unknown" for review in gate_reviews}))
            if gate_reviews
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


_WRITING_CORE_FIELDS = ("research_gap", "proposed_solution", "core_hypothesis", "section_strategy")
_WRITING_PLACEHOLDER_RE = re.compile(
    r"^(?:not explicitly stated|not clearly extracted|unknown|none|n/?a|"
    r"research gap not explicitly stated|solution not clearly extracted|hypothesis not explicitly stated)[.!]?$",
    re.IGNORECASE,
)
_WRITING_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:\s*[%°]|\s*[A-Za-z]+)?")
_WRITING_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+./-]{2,}")
_WRITING_SPECIFIC_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*\d[A-Za-z0-9+.-]*\b")
_WRITING_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "were", "was", "are", "our",
    "their", "into", "using", "used", "study", "work", "paper", "method", "approach", "based",
    "propose", "proposed", "results", "result", "show", "shows", "could", "would", "may", "can",
}


def _writing_content_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _WRITING_WORD_RE.findall(value)
        if token.lower() not in _WRITING_STOPWORDS
    }


def _writing_field_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value or "")


def writing_card_content_gate(card: WritingCard) -> WritingGateResult:
    """Pure, deterministic content gate for evidence-grounded WritingCards."""
    evidence_chain = card.evidence_chain
    if not isinstance(evidence_chain, list) or not evidence_chain:
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="missing" if _is_blank(evidence_chain) else "legacy_unscoped",
            review_gate_status="blocked",
            blocked_reasons=("missing_evidence_chain",) if _is_blank(evidence_chain) else ("unscoped_evidence_chain",),
        )

    reasons: list[str] = []
    # Preserve useful diagnostics for legacy chains without letting review flags
    # bypass the new field-scoped content requirements.
    review_payloads = [
        item for item in evidence_chain
        if isinstance(item, dict)
        and any(key in item for key in ("reviewer_status", "review_status", "target_resolution_status", "resolution_status"))
    ]
    if review_payloads and not all(is_safe_verified_review(item) for item in review_payloads):
        reasons.append("unsafe_review")
    scoped_evidence = [
        item for item in evidence_chain
        if isinstance(item, dict) and item.get("supports_fields")
    ]
    locator_payloads = _locator_payloads(evidence_chain)
    if not scoped_evidence and locator_payloads and not all(_safe_locator_payload(item) for item in locator_payloads):
        reasons.append("unsafe_locator")
    reliable_fields = 0
    for field_name in _WRITING_CORE_FIELDS:
        value = _writing_field_text(getattr(card, field_name, None)).strip()
        if not value or _WRITING_PLACEHOLDER_RE.match(value):
            continue
        supporting = [
            item for item in evidence_chain
            if isinstance(item, dict)
            and field_name in (item.get("supports_fields") or [])
            and str(item.get("text") or "").strip()
            and str(item.get("source") or "").strip()
        ]
        if not supporting:
            reasons.append(f"missing_field_evidence:{field_name}")
            continue
        safe_supporting = []
        for item in supporting:
            if _safe_locator_payload(item):
                safe_supporting.append(item)
        if not safe_supporting:
            reasons.append(f"unsafe_field_locator:{field_name}")
            continue
        evidence_text = " ".join(str(item.get("text") or "") for item in safe_supporting).lower()
        value_tokens = _writing_content_tokens(value)
        evidence_tokens = _writing_content_tokens(evidence_text)
        token_coverage = len(value_tokens & evidence_tokens) / max(1, len(value_tokens))
        if not value_tokens or token_coverage < 0.45:
            reasons.append(f"field_evidence_mismatch:{field_name}")
            continue
        unsupported_numbers = [
            number for number in _WRITING_NUMBER_RE.findall(value)
            if str(number).strip().lower() not in evidence_text
        ]
        if unsupported_numbers:
            reasons.append(f"unsupported_number:{field_name}")
            continue
        unsupported_specific_tokens = [
            token for token in _WRITING_SPECIFIC_TOKEN_RE.findall(value)
            if token.lower() not in evidence_text
        ]
        if unsupported_specific_tokens:
            reasons.append(f"unsupported_specific_token:{field_name}")
            continue
        reliable_fields += 1

    if reliable_fields < 2:
        reasons.append("insufficient_reliable_core_fields")
    if reasons:
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="present",
            review_gate_status="blocked",
            blocked_reasons=tuple(dict.fromkeys(reasons)),
        )
    return WritingGateResult(
        can_use_for_writing=True,
        evidence_chain_status="present",
        review_gate_status="content_verified",
        blocked_reasons=(),
    )


def _canonical_writing_source_type(value: Any) -> str | None:
    normalized = _normalized(value).replace("-", "_").replace(" ", "_")
    if normalized in {_normalized(item) for item in _target_type_values("mechanism_claims")}:
        return "mechanism_claims"
    if normalized in {_normalized(item) for item in _target_type_values("sections")}:
        return "sections"
    if normalized in {
        _normalized(item) for item in _target_type_values("section_page_fragments")
    }:
        return "section_page_fragments"
    return None


def _writing_chain_item_source(
    session: Session,
    card: WritingCard,
    item: dict[str, Any],
) -> tuple[str | None, Any | None, str | None]:
    text = str(item.get("text") or "").strip()
    if not text:
        return None, None, "empty_evidence_chain_item"
    page = item.get("page") if isinstance(item.get("page"), int) else None
    if page is None or _normalized(item.get("locator_status")) not in {"exact_page", "exact_bbox"}:
        return None, None, "unsafe_evidence_chain_locator"

    explicit_type = _canonical_writing_source_type(
        item.get("source_target_type") or item.get("target_type") or item.get("object_type")
    )
    explicit_id = item.get("source_target_id") or item.get("target_id") or item.get("object_id")
    locators = list(
        session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == card.paper_id,
                EvidenceLocator.page == page,
                EvidenceLocator.locator_status.in_(["exact_page", "exact_bbox"]),
                EvidenceLocator.target_type.in_(
                    sorted(
                        _target_type_values("mechanism_claims")
                        | _target_type_values("sections")
                        | _target_type_values("section_page_fragments")
                    )
                ),
            )
        ).all()
    )
    wanted = compact_page_text(text)
    matches: list[tuple[str, Any]] = []
    for locator in locators:
        canonical = _canonical_writing_source_type(locator.target_type)
        if canonical is None:
            continue
        if explicit_type and canonical != explicit_type:
            continue
        if explicit_id and str(locator.target_id) != str(explicit_id):
            continue
        located = compact_page_text(locator.evidence_text)
        if not located or (wanted not in located and located not in wanted):
            continue
        try:
            resolved_type, target = get_ai_target(
                session,
                paper_id=card.paper_id,
                target_type=canonical,
                target_id=str(locator.target_id),
            )
        except (LookupError, ValueError):
            continue
        gate = content_object_gate(session, resolved_type, target)
        if not gate.can_use_for_writing:
            continue
        matches.append((resolved_type, target))
    unique = {(target_type, str(target.id)): (target_type, target) for target_type, target in matches}
    if not unique:
        return None, None, "blocked_or_missing_authoritative_source"
    if len(unique) != 1:
        return None, None, "ambiguous_authoritative_source"
    return next(iter(unique.values())) + (None,)


def writing_card_authoritative_chain_gate(session: Session, card: WritingCard) -> WritingGateResult:
    """Require every WritingCard evidence item to bind to one safe source object."""

    content_gate = writing_card_content_gate(card)
    if not content_gate.can_use_for_writing:
        return content_gate
    reasons: list[str] = []
    for index, item in enumerate(card.evidence_chain or []):
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            reasons.append(f"invalid_evidence_chain_item:{index}")
            continue
        _target_type, _target, error = _writing_chain_item_source(session, card, item)
        if error:
            reasons.append(f"{error}:{index}")
    if reasons:
        return WritingGateResult(
            can_use_for_writing=False,
            evidence_chain_status="present",
            review_gate_status="blocked",
            blocked_reasons=tuple(dict.fromkeys(reasons)),
        )
    return WritingGateResult(
        can_use_for_writing=True,
        evidence_chain_status="present",
        review_gate_status="authoritative_chain_verified",
        blocked_reasons=(),
    )


def writing_card_gate(session: Session, card: WritingCard) -> WritingGateResult:
    """Final writing admission gate: grounded content plus object-level safe review."""

    content_gate = writing_card_authoritative_chain_gate(session, card)
    if not content_gate.can_use_for_writing:
        return content_gate

    unscoped_results = [
        item for item in (card.evidence_chain or [])
        if isinstance(item, dict)
        and not (item.get("supports_fields") or [])
        and str(item.get("text") or "").strip()
    ]
    if unscoped_results:
        reviews = get_target_reviews(
            session,
            paper_id=card.paper_id,
            target_type="writing_cards",
            target_id=card.id,
        )
        evidence_chain_reviewed = any(
            review.field_name == "evidence_chain" and is_authoritative_verified_review(session, review, card)
            for review in reviews
        )
        if not evidence_chain_reviewed:
            return WritingGateResult(
                can_use_for_writing=False,
                evidence_chain_status=content_gate.evidence_chain_status,
                review_gate_status="blocked",
                blocked_reasons=("missing_evidence_chain_review",),
            )

    export_gate = bulk_export_gate_results(session, [card], target_type="writing_cards")[str(card.id)]
    correction_approved = _writing_card_has_safe_approved_ide_correction(session, card)
    if export_gate.eligible or correction_approved:
        return WritingGateResult(
            can_use_for_writing=True,
            evidence_chain_status=content_gate.evidence_chain_status,
            review_gate_status="safe_verified",
            blocked_reasons=(),
        )
    return WritingGateResult(
        can_use_for_writing=False,
        evidence_chain_status=content_gate.evidence_chain_status,
        review_gate_status="blocked",
        blocked_reasons=tuple(dict.fromkeys((*content_gate.blocked_reasons, *export_gate.reasons))),
    )


def content_object_gate(
    session: Session,
    target_type: str,
    target: Any,
) -> ContentObjectGateResult:
    """Authoritative object-level admission gate for content writing and citation.

    ``ContentEvidenceItem`` is only a cache/projection.  When one is supplied,
    the source row is resolved again and the gate is evaluated against that
    canonical object.  A missing, stale, cross-paper, or unsupported mapping is
    always blocked instead of falling back to projected review fields.
    """

    canonical_type = _canonical_content_target_type(target_type)
    projection = target if isinstance(target, ContentEvidenceItem) else None
    resolved_target = target
    if isinstance(target, ContentEvidenceItem):
        canonical_type, resolved_target, resolution_error = _resolve_content_projection_target(
            session,
            target,
        )
        if resolution_error:
            return _blocked_content_object_gate(resolution_error, locator_status="unmapped")
        if not _content_projection_snapshot_matches(canonical_type, target, resolved_target):
            return _blocked_content_object_gate(
                "content_projection_snapshot_mismatch",
                locator_status="snapshot_mismatch",
            )

    model_by_type = {
        "abstract": Paper,
        "sections": PaperSection,
        "section_page_fragments": EvidenceClaim,
        "mechanism_claims": MechanismClaim,
        "writing_cards": WritingCard,
    }
    model = model_by_type.get(canonical_type)
    if model is None or not isinstance(resolved_target, model):
        return _blocked_content_object_gate("no_real_object_mapping", locator_status="unmapped")

    target_id = getattr(resolved_target, "id", None)
    if target_id is None or session.get(model, target_id) is None:
        return _blocked_content_object_gate("no_real_object_mapping", locator_status="unmapped")

    if canonical_type == "abstract":
        paper = resolved_target
        wrapped = SimpleNamespace(
            id=paper.id,
            paper_id=paper.id,
            evidence_text=paper.abstract,
        )
        export_gate = bulk_export_gate_results(session, [wrapped], target_type="abstract")[str(paper.id)]
        return _degrade_for_projection_cache(
            projection,
            _content_gate_from_export(export_gate),
        )

    if canonical_type == "writing_cards":
        writing_gate = writing_card_gate(session, resolved_target)
        export_gate = bulk_export_gate_results(
            session,
            [resolved_target],
            target_type="writing_cards",
        )[str(resolved_target.id)]
        return _degrade_for_projection_cache(projection, ContentObjectGateResult(
            can_use_for_writing=writing_gate.can_use_for_writing,
            can_use_for_citation=False,
            review_gate_status=writing_gate.review_gate_status,
            locator_status=export_gate.locator_status,
            blocked_reasons=writing_gate.blocked_reasons,
        ))

    if canonical_type == "sections" and _normalized(resolved_target.section_type) == "body":
        if not _body_section_has_complete_single_page_evidence(session, resolved_target):
            return _blocked_content_object_gate(
                "incomplete_body_section_page_coverage",
                locator_status="incomplete_page_coverage",
            )

    if canonical_type == "section_page_fragments":
        structure_reasons = _section_page_fragment_structure_reasons(session, resolved_target)
        export_gate = bulk_export_gate_results(
            session,
            [resolved_target],
            target_type=canonical_type,
        )[str(resolved_target.id)]
        if structure_reasons:
            return _blocked_content_object_gate(
                *structure_reasons,
                *export_gate.reasons,
                locator_status=export_gate.locator_status,
            )
        return _content_gate_from_export(export_gate)

    export_gate = bulk_export_gate_results(
        session,
        [resolved_target],
        target_type=canonical_type,
    )[str(resolved_target.id)]
    return _degrade_for_projection_cache(
        projection,
        _content_gate_from_export(export_gate),
    )


def _content_gate_from_export(export_gate: ExportGateResult) -> ContentObjectGateResult:
    return ContentObjectGateResult(
        can_use_for_writing=export_gate.eligible,
        can_use_for_citation=export_gate.eligible,
        review_gate_status=export_gate.review_gate_status,
        locator_status=export_gate.locator_status,
        blocked_reasons=export_gate.reasons,
    )


def _blocked_content_object_gate(
    *reasons: str,
    locator_status: str = "missing_locator",
) -> ContentObjectGateResult:
    return ContentObjectGateResult(
        can_use_for_writing=False,
        can_use_for_citation=False,
        review_gate_status="blocked",
        locator_status=locator_status,
        blocked_reasons=tuple(dict.fromkeys(reason for reason in reasons if reason)),
    )


def _degrade_for_projection_cache(
    projection: ContentEvidenceItem | None,
    gate: ContentObjectGateResult,
) -> ContentObjectGateResult:
    if projection is None:
        return gate
    projection_claims_access = (
        _normalized(projection.citation_status) in {"citable", "writing_only"}
        or _normalized(projection.review_status) in {"validated", "approved", "safe_verified"}
    )
    if gate.can_use_for_writing or gate.can_use_for_citation or not projection_claims_access:
        return gate
    return ContentObjectGateResult(
        can_use_for_writing=False,
        can_use_for_citation=False,
        review_gate_status=gate.review_gate_status,
        locator_status=gate.locator_status,
        blocked_reasons=tuple(dict.fromkeys((*gate.blocked_reasons, "content_projection_gate_mismatch"))),
    )


def _canonical_content_target_type(target_type: str) -> str:
    normalized = _normalized(target_type)
    aliases = {
        "abstract": {"abstract", "paper_abstract", "summary", "paper_summary"},
        "sections": {_normalized(value) for value in _target_type_values("sections")},
        "section_page_fragments": {
            _normalized(value) for value in _target_type_values("section_page_fragments")
        },
        "mechanism_claims": {_normalized(value) for value in _target_type_values("mechanism_claims")},
        "writing_cards": {_normalized(value) for value in _target_type_values("writing_cards")},
    }
    for canonical, values in aliases.items():
        if normalized in values:
            return canonical
    return normalized


def _resolve_content_projection_target(
    session: Session,
    item: ContentEvidenceItem,
) -> tuple[str, Any | None, str | None]:
    canonical_type = _canonical_content_target_type(item.source_type)
    if canonical_type == "abstract":
        # Abstract projections historically use language keys such as "en" as
        # source_id.  The canonical abstract is always the projection's own
        # paper, so source_id must never participate in object or cross-paper
        # resolution.
        target = session.get(Paper, item.paper_id)
        if target is None:
            return canonical_type, None, "no_real_object_mapping"
        return canonical_type, target, None

    model_by_type = {
        "sections": PaperSection,
        "section_page_fragments": EvidenceClaim,
        "mechanism_claims": MechanismClaim,
        "writing_cards": WritingCard,
    }
    model = model_by_type.get(canonical_type)
    if model is None:
        return canonical_type, None, "no_real_object_mapping"
    try:
        source_id = UUID(str(item.source_id))
    except (TypeError, ValueError):
        return canonical_type, None, "no_real_object_mapping"
    target = session.get(model, source_id)
    if target is None:
        return canonical_type, None, "no_real_object_mapping"
    target_paper_id = target.id if isinstance(target, Paper) else getattr(target, "paper_id", None)
    if target_paper_id != item.paper_id:
        return canonical_type, None, "content_object_paper_mismatch"
    return canonical_type, target, None


def _projection_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _content_projection_snapshot_matches(
    canonical_type: str,
    projection: ContentEvidenceItem,
    target: Any,
) -> bool:
    """Require projected text to be an exact normalized snapshot of its object.

    Locator and projected review fields are intentionally excluded: those are
    derived caches and may be absent while the canonical object gate is safe.
    The text returned by formal RAG, however, must never differ from the real
    object's current content/evidence snapshot.
    """

    if canonical_type == "abstract" and isinstance(target, Paper):
        expected_content = target.abstract
        expected_evidence = target.abstract
    elif canonical_type == "sections" and isinstance(target, PaperSection):
        expected_content = target.text
        expected_evidence = target.text
    elif canonical_type == "mechanism_claims" and isinstance(target, MechanismClaim):
        expected_content = target.claim_text
        expected_evidence = target.evidence_text
    elif canonical_type == "section_page_fragments" and isinstance(target, EvidenceClaim):
        expected_content = target.claim_text
        expected_evidence = target.evidence_text
    elif canonical_type == "writing_cards" and isinstance(target, WritingCard):
        expected_content = _writing_card_projection_content(target)
        expected_evidence = _projection_evidence_preview(target.evidence_chain)
    else:
        return False

    return (
        _projection_text(projection.content) == _projection_text(expected_content)
        and _projection_text(projection.evidence_text) == _projection_text(expected_evidence)
    )


def _body_section_has_complete_single_page_evidence(
    session: Session,
    section: PaperSection,
) -> bool:
    """A parent body section is safe only when its complete text fits one PDF page.

    Multi-page parents deliberately remain blocked; their independently reviewed
    section_page_fragments are the admissible objects.
    """

    wanted = compact_page_text(section.text)
    if not wanted:
        return False
    target_types = _target_type_values("sections")
    locators = list(
        session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == section.paper_id,
                EvidenceLocator.target_type.in_(target_types),
                EvidenceLocator.target_id == str(section.id),
                EvidenceLocator.field_name == "text",
                EvidenceLocator.page.is_not(None),
                EvidenceLocator.locator_status.in_(["exact_page", "exact_bbox"]),
            )
        ).all()
    )
    paper = session.get(Paper, section.paper_id)
    if paper is None:
        return False
    provider = PaperPageTextProvider()
    for locator in locators:
        quote = compact_page_text(locator.evidence_text)
        if quote != wanted:
            continue
        record = provider.read_page(paper, int(locator.page))
        if record.status == "ok" and quote in compact_page_text(record.text):
            return True
    return False


def _section_page_fragment_structure_reasons(
    session: Session,
    fragment: EvidenceClaim,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if _normalized(fragment.source_type) != "section_page_fragment":
        reasons.append("invalid_section_page_fragment_source_type")
    if fragment.section_id is None:
        reasons.append("missing_parent_section")
        parent = None
    else:
        parent = session.get(PaperSection, fragment.section_id)
        if parent is None or parent.paper_id != fragment.paper_id:
            reasons.append("missing_parent_section")
    page_start = fragment.page_start
    page_end = fragment.page_end
    if page_start is None or page_end is None or page_start < 1 or page_start != page_end:
        reasons.append("fragment_requires_one_physical_pdf_page")
    claim_text = compact_page_text(fragment.claim_text)
    evidence_text = compact_page_text(fragment.evidence_text)
    if not claim_text or claim_text != evidence_text:
        reasons.append("fragment_text_evidence_mismatch")
    if parent is not None and evidence_text not in compact_page_text(parent.text):
        reasons.append("fragment_not_contained_in_parent_section")
    metadata = fragment.meta if isinstance(fragment.meta, dict) else {}
    bound_parent_id = metadata.get("parent_section_id")
    if bound_parent_id and str(bound_parent_id) != str(fragment.section_id):
        reasons.append("fragment_parent_binding_mismatch")
    if page_start is not None and page_start >= 1 and evidence_text:
        paper = session.get(Paper, fragment.paper_id)
        if paper is None:
            reasons.append("missing_real_pdf")
        else:
            record = PaperPageTextProvider().read_page(paper, int(page_start))
            if record.status != "ok":
                reasons.append(record.status)
            elif evidence_text not in compact_page_text(record.text):
                reasons.append("fragment_text_not_on_pdf_page")
    return tuple(dict.fromkeys(reasons))


def _writing_card_projection_content(card: WritingCard) -> str:
    parts: list[str] = []
    for label, field_name in (
        ("research_gap", "research_gap"),
        ("proposed_solution", "proposed_solution"),
        ("core_hypothesis", "core_hypothesis"),
    ):
        value = _projection_text(getattr(card, field_name, None))
        if value:
            parts.append(f"{label}: {value}")
    for item in normalized_evidence_chain(card.evidence_chain, limit=8):
        if item["supports_fields"]:
            continue
        parts.append(f"{item['evidence_type']}: {item['text']}")
    return " | ".join(parts)


def _projection_evidence_preview(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _projection_text(value)
    if isinstance(value, dict):
        for key in ("evidence_text", "quoted_text", "text", "content", "reason"):
            text = _projection_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        texts = [text for item in value if (text := _projection_evidence_preview(item))]
        return " | ".join(texts[:3])
    return _projection_text(value)


def _writing_card_has_safe_approved_ide_correction(session: Session, card: WritingCard) -> bool:
    reviews = get_target_reviews(
        session,
        paper_id=card.paper_id,
        target_type="writing_cards",
        target_id=card.id,
    )
    if any(is_unsafe_review_status(review) for review in reviews):
        return False
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == card.paper_id)
        .where(PaperCorrection.status == "approved")
        .where(PaperCorrection.field_name.in_(["writing_cards", "writing_card"]))
        .order_by(PaperCorrection.created_at.desc())
        .limit(100)
    ).all()
    for correction in corrections:
        source = _normalized(correction.source)
        reviewer = _normalized(correction.reviewed_by)
        if source != "ide_ai" and "ide_ai" not in reviewer:
            continue
        anchor = first_pdf_evidence_anchor(correction.evidence_payload)
        if not isinstance(anchor, dict):
            continue
        evidence_text = str(anchor.get("quoted_text") or anchor.get("evidence_text") or "").strip()
        if not evidence_text or not _safe_locator_from_parts(
            page=anchor.get("page"),
            locator_status="exact_page",
            evidence_text=evidence_text,
            bbox=anchor.get("bbox"),
        ):
            continue
        if correction.operation == "replace" and _writing_card_replace_matches(correction, card):
            return True
        if correction.operation == "create" and _writing_card_create_matches(correction, card):
            return True
    return False


def _writing_card_replace_matches(correction: PaperCorrection, card: WritingCard) -> bool:
    parts = str(correction.target_path or "").split(":", 2)
    if len(parts) != 3 or _normalized(parts[0]) not in {"writing_cards", "writing_card"}:
        return False
    if parts[1] != str(card.id):
        return False
    field_name = parts[2]
    if field_name not in _WRITING_CORE_FIELDS and field_name not in {
        "paper_type",
        "evidence_chain",
        "section_strategy",
        "figure_logic",
        "abstract_logic",
        "introduction_logic",
        "discussion_logic",
    }:
        return False
    return getattr(card, field_name, None) == correction.proposed_value


def _writing_card_create_matches(correction: PaperCorrection, card: WritingCard) -> bool:
    if str(correction.target_path or "") != "writing_cards:new:create":
        return False
    payload = correction.evidence_payload if isinstance(correction.evidence_payload, dict) else {}
    structured = payload.get("structured_create") if isinstance(payload.get("structured_create"), dict) else {}
    target_id = str(structured.get("target_id") or "").strip()
    if target_id and target_id != str(card.id):
        return False
    proposed = correction.proposed_value if isinstance(correction.proposed_value, dict) else {}
    checked = 0
    for field_name in (
        "paper_type",
        "research_gap",
        "proposed_solution",
        "core_hypothesis",
        "abstract_logic",
        "introduction_logic",
        "discussion_logic",
    ):
        if field_name not in proposed:
            continue
        checked += 1
        if getattr(card, field_name, None) != proposed.get(field_name):
            return False
    return checked > 0


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
    if rs == AI_VERIFIED_STATUS and not is_safe_verified_review(review):
        return True
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
    if normalize_review_status(existing_review) not in SAFE_REVIEWER_STATUSES:
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

    if rs not in SAFE_REVIEWER_STATUSES:
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
        if rs == AI_VERIFIED_STATUS:
            blocked.append("invalid_ai_verification_payload")
        if rs not in SAFE_REVIEWER_STATUSES:
            blocked.append(f"unsafe_reviewer_status:{rs}")
        if trs not in SAFE_TARGET_RESOLUTION_STATUSES:
            blocked.append(f"unsafe_target_resolution:{trs}")
        if review is None:
            blocked.append("missing_review")

    if is_ai_candidate and rs == "verified":
        blocked.append("ai_candidate_cannot_be_verified")
    if is_external_candidate and not has_evidence_payload:
        blocked.append("external_candidate_missing_evidence_payload")
    if is_external_candidate and rs in SAFE_REVIEWER_STATUSES:
        blocked.append("external_candidate_cannot_be_verified")

    label = "safe_verified" if not blocked else "blocked"
    return ReviewBoundaryGate(
        is_safe_verified=safe and not blocked,
        reviewer_status=rs,
        target_resolution_status=trs,
        blocked_reasons=tuple(blocked),
        boundary_label=label,
    )
