from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
    WritingCard,
)
from app.services.review_target_resolver import ReviewTargetResolver
from app.services.evidence_page_recovery import PaperPageTextProvider


AI_VERIFICATION_CAPABILITY = "ai_verify_content"
AI_VERIFICATION_POLICY_VERSION = "single_ai_verification.v1"
AI_VERIFIED_STATUS = "ai_verified"
AI_SUPPORTED_TARGET_TYPES = frozenset(
    {
        "mechanism_claims",
        "dft_results",
        "electrochemical_performance",
        "sections",
        "section_page_fragments",
        "writing_cards",
    }
)

_TARGET_ALIASES = {
    "mechanismclaim": "mechanism_claims",
    "mechanism_claim": "mechanism_claims",
    "mechanism_claims": "mechanism_claims",
    "dftresult": "dft_results",
    "dft_result": "dft_results",
    "dft_results": "dft_results",
    "electrochemicalperformance": "electrochemical_performance",
    "electrochemical_performance": "electrochemical_performance",
    "section": "sections",
    "sections": "sections",
    "paper_section": "sections",
    "papersection": "sections",
    "section_page_fragment": "section_page_fragments",
    "section_page_fragments": "section_page_fragments",
    "sectionpagefragment": "section_page_fragments",
    "writing_card": "writing_cards",
    "writing_cards": "writing_cards",
    "writingcard": "writing_cards",
}

_TARGET_MODELS = {
    "mechanism_claims": MechanismClaim,
    "dft_results": DFTResult,
    "electrochemical_performance": ElectrochemicalPerformance,
    "sections": PaperSection,
    "section_page_fragments": EvidenceClaim,
    "writing_cards": WritingCard,
}


def canonical_ai_target_type(value: str) -> str:
    normalized = re.sub(r"[-\s]+", "_", str(value or "").strip().casefold())
    canonical = _TARGET_ALIASES.get(normalized)
    if canonical not in AI_SUPPORTED_TARGET_TYPES:
        raise ValueError(f"Unsupported AI verification target_type: {value}")
    return canonical


def normalize_evidence_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "").replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", text).strip().casefold()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_ai_target(session: Session, *, paper_id: UUID, target_type: str, target_id: str) -> tuple[str, Any]:
    canonical = canonical_ai_target_type(target_type)
    try:
        normalized_id = UUID(str(target_id))
    except ValueError as exc:
        raise LookupError(f"Invalid target_id: {target_id}") from exc
    target = session.get(_TARGET_MODELS[canonical], normalized_id)
    if target is None or getattr(target, "paper_id", None) != paper_id:
        raise LookupError(f"Target not found for {canonical}:{target_id}")
    if canonical == "section_page_fragments" and str(target.source_type or "").casefold() != "section_page_fragment":
        raise LookupError(f"Target not found for {canonical}:{target_id}")
    return canonical, target


def ai_field_snapshot(target_type: str, target: Any, field_name: str) -> dict[str, Any]:
    canonical = canonical_ai_target_type(target_type)
    if canonical == "mechanism_claims":
        fields = {
            "claim_type": {"value": target.claim_type, "unit": None, "evidence_text": target.evidence_text or ""},
            "claim_text": {"value": target.claim_text, "unit": None, "evidence_text": target.evidence_text or ""},
            "key_species": {"value": target.evidence_types or [], "unit": None, "evidence_text": target.evidence_text or ""},
            "mechanism_direction": {"value": None, "unit": None, "evidence_text": target.evidence_text or ""},
        }
    elif canonical == "dft_results":
        fields = {
            "catalyst": {"value": str(target.catalyst_sample_id) if target.catalyst_sample_id else None, "unit": None, "evidence_text": target.evidence_text or ""},
            "adsorbate": {"value": target.adsorbate, "unit": None, "evidence_text": target.evidence_text or ""},
            "energy_type": {"value": target.property_type, "unit": None, "evidence_text": target.evidence_text or ""},
            "value": {"value": target.value, "unit": target.unit, "evidence_text": target.evidence_text or ""},
            "reaction_step": {"value": target.reaction_step, "unit": None, "evidence_text": target.evidence_text or ""},
        }
    elif canonical == "electrochemical_performance":
        fields = {
            "sulfur_loading": {"value": target.sulfur_loading_mg_cm2, "unit": "mg/cm2", "evidence_text": target.evidence_text or ""},
            "sulfur_content": {"value": target.sulfur_content_wt_percent, "unit": "wt%", "evidence_text": target.evidence_text or ""},
            "electrolyte_sulfur_ratio": {"value": target.electrolyte_sulfur_ratio, "unit": None, "evidence_text": target.evidence_text or ""},
            "capacity": {"value": target.capacity_value, "unit": "mAh/g", "evidence_text": target.evidence_text or ""},
            "cycle_number": {"value": target.cycle_number, "unit": None, "evidence_text": target.evidence_text or ""},
            "rate": {"value": target.rate, "unit": None, "evidence_text": target.evidence_text or ""},
            "decay_per_cycle": {"value": target.decay_per_cycle, "unit": "%/cycle", "evidence_text": target.evidence_text or ""},
        }
    elif canonical == "sections":
        fields = {"text": {"value": target.text, "unit": None, "evidence_text": target.text or ""}}
    elif canonical == "section_page_fragments":
        fields = {
            "text": {
                "value": target.claim_text,
                "unit": None,
                "evidence_text": target.evidence_text or "",
            }
        }
    else:
        fields = {
            "research_gap": {"value": target.research_gap, "unit": None, "evidence_text": ""},
            "proposed_solution": {"value": target.proposed_solution, "unit": None, "evidence_text": ""},
            "core_hypothesis": {"value": target.core_hypothesis, "unit": None, "evidence_text": ""},
            "evidence_chain": {"value": target.evidence_chain, "unit": None, "evidence_text": ""},
        }
    if field_name not in fields:
        raise ValueError(f"Unsupported field for {canonical}: {field_name}")
    return fields[field_name]


def ai_target_fingerprint(target_type: str, target: Any) -> str:
    canonical = canonical_ai_target_type(target_type)
    if canonical in {"mechanism_claims", "dft_results", "electrochemical_performance"}:
        return ReviewTargetResolver.build_target_fingerprint(ReviewTargetResolver.__new__(ReviewTargetResolver), canonical, target)
    if canonical == "sections":
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "section_title": target.section_title,
            "section_type": target.section_type,
            "text": target.text,
            "page_start": target.page_start,
            "page_end": target.page_end,
        }
    elif canonical == "section_page_fragments":
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "parent_section_id": str(target.section_id) if target.section_id else None,
            "claim_text": target.claim_text,
            "evidence_text": target.evidence_text,
            "page_start": target.page_start,
            "page_end": target.page_end,
            "metadata": target.meta,
        }
    else:
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "research_gap": target.research_gap,
            "proposed_solution": target.proposed_solution,
            "core_hypothesis": target.core_hypothesis,
            "evidence_chain": target.evidence_chain,
            "section_strategy": target.section_strategy,
        }
    return stable_hash(payload)


def locator_fingerprint(locator: EvidenceLocator) -> str:
    return stable_hash(
        {
            "id": str(locator.id),
            "paper_id": str(locator.paper_id),
            "target_type": canonical_ai_target_type(str(locator.target_type or "")),
            "target_id": str(locator.target_id or ""),
            "field_name": str(locator.field_name or ""),
            "page": locator.page,
            "bbox": locator.bbox,
            "evidence_text": normalize_evidence_text(locator.evidence_text),
            "locator_status": str(locator.locator_status or "").casefold(),
        }
    )


def matching_locator(
    session: Session,
    *,
    paper_id: UUID,
    target_type: str,
    target_id: str,
    field_name: str,
    page: int,
    evidence_text: str,
) -> EvidenceLocator | None:
    canonical = canonical_ai_target_type(target_type)
    rows = session.scalars(
        select(EvidenceLocator).where(
            EvidenceLocator.paper_id == paper_id,
            EvidenceLocator.target_id == str(target_id),
            EvidenceLocator.field_name == field_name,
            EvidenceLocator.page == page,
        )
    ).all()
    wanted = normalize_evidence_text(evidence_text)
    for locator in rows:
        try:
            locator_type = canonical_ai_target_type(str(locator.target_type or ""))
        except ValueError:
            continue
        if (
            locator_type == canonical
            and str(locator.locator_status or "").casefold() in {"exact_page", "exact_bbox"}
            and normalize_evidence_text(locator.evidence_text) == wanted
        ):
            return locator
    return None


def read_pdf_page_text(paper: Paper, page: int) -> tuple[str | None, str | None, Path | None]:
    record = PaperPageTextProvider(get_settings()).read_page(paper, page)
    pdf_path = Path(record.pdf_path) if record.pdf_path else None
    if record.status == "ok":
        return record.text, None, pdf_path
    if record.status == "extraction_failed":
        return None, "unreadable_pdf", pdf_path
    return None, record.status, pdf_path


def ai_review_payload_structurally_valid(review: ExtractionFieldReview | dict[str, Any]) -> bool:
    if isinstance(review, dict):
        status = str(review.get("reviewer_status") or review.get("review_status") or review.get("status") or "").casefold()
        resolution = str(review.get("target_resolution_status") or review.get("resolution_status") or "active").casefold()
        payload = review.get("review_payload")
        target_fingerprint = review.get("target_fingerprint")
    else:
        status = str(review.reviewer_status or "").casefold()
        resolution = str(review.target_resolution_status or "").casefold()
        payload = review.review_payload
        target_fingerprint = review.target_fingerprint
    verification = payload.get("ai_verification") if isinstance(payload, dict) else None
    if status != AI_VERIFIED_STATUS or resolution not in {"active", "remapped"} or not isinstance(verification, dict):
        return False
    evidence_checks = verification.get("evidence_checks")
    locator_checks = verification.get("locator_checks")
    required = (
        verification.get("actor_type") == "ai",
        verification.get("identity_verified") is True,
        verification.get("capability") == AI_VERIFICATION_CAPABILITY,
        verification.get("policy_version") == AI_VERIFICATION_POLICY_VERSION,
        verification.get("decision") in {"verified", "corrected"},
        verification.get("single_ai") is True,
        verification.get("second_ai_used") is False,
        bool(str(verification.get("source_identity") or "").strip()),
        bool(str(verification.get("source_label") or "").strip()),
        bool(str(verification.get("model_agent") or "").strip()),
        bool(str(verification.get("created_at") or "").strip()),
        float(verification.get("confidence") or 0) >= get_settings().ai_verification_min_confidence,
        bool(target_fingerprint),
        verification.get("target_snapshot_fingerprint") == target_fingerprint,
        bool(str(verification.get("locator_fingerprint") or "").strip()),
        isinstance(evidence_checks, dict) and bool(evidence_checks) and all(value is True for value in evidence_checks.values()),
        isinstance(locator_checks, dict) and bool(locator_checks) and all(value is True for value in locator_checks.values()),
    )
    return all(required)


def authoritative_ai_review_valid(session: Session, review: ExtractionFieldReview, target: Any) -> bool:
    if not ai_review_payload_structurally_valid(review):
        return False
    verification = review.review_payload["ai_verification"]
    try:
        canonical = canonical_ai_target_type(review.target_type)
        if ai_target_fingerprint(canonical, target) != review.target_fingerprint:
            return False
        page = int(verification.get("page"))
    except (TypeError, ValueError):
        return False
    locator = matching_locator(
        session,
        paper_id=review.paper_id,
        target_type=canonical,
        target_id=review.target_id,
        field_name=review.field_name,
        page=page,
        evidence_text=str(review.evidence_text or ""),
    )
    if locator is None or locator_fingerprint(locator) != verification.get("locator_fingerprint"):
        return False
    paper = session.get(Paper, review.paper_id)
    if paper is None:
        return False
    page_text, error, _path = read_pdf_page_text(paper, page)
    return error is None and normalize_evidence_text(review.evidence_text) in normalize_evidence_text(page_text)
