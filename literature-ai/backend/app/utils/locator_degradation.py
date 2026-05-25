from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EXACT_PAGE = "exact_page"
TEXT_ONLY = "text_only"
MISSING_PAGE = "missing_page"
MISSING_LOCATOR = "missing_locator"
APPROXIMATE = "approximate"
UNRESOLVED = "unresolved"

EXACT_PDF_PAGE = "exact_pdf_page"
TEXT_EVIDENCE_ONLY = "text_evidence_only"
APPROXIMATE_PDF_PAGE = "approximate_pdf_page"
UNAVAILABLE = "unavailable"


LEGACY_STATUS_MAP = {
    "exact": EXACT_PAGE,
    "page_only": EXACT_PAGE,
    "missing": MISSING_LOCATOR,
    "needs_reparse": MISSING_PAGE,
    "approximate_candidate": APPROXIMATE,
    "ambiguous_match": APPROXIMATE,
}


@dataclass(frozen=True)
class LocatorDegradation:
    locator_status: str
    provenance_level: str
    can_jump_to_pdf_page: bool
    can_highlight_in_pdf: bool
    warning_reason: str | None = None


def valid_page(page: Any) -> bool:
    try:
        return int(page) > 0
    except (TypeError, ValueError):
        return False


def normalize_locator_status(raw_status: str | None) -> str:
    normalized = str(raw_status or "").strip().lower()
    if not normalized:
        return MISSING_LOCATOR
    return LEGACY_STATUS_MAP.get(normalized, normalized)


def locator_degradation(
    *,
    page: int | None,
    locator_status: str | None = None,
    evidence_text: str | None = None,
    bbox: dict[str, Any] | None = None,
    warning_reason: str | None = None,
) -> LocatorDegradation:
    status = normalize_locator_status(locator_status)
    has_page = valid_page(page)
    has_text = bool((evidence_text or "").strip())

    if status == APPROXIMATE:
        return LocatorDegradation(
            locator_status=APPROXIMATE,
            provenance_level=APPROXIMATE_PDF_PAGE if has_page else TEXT_EVIDENCE_ONLY,
            can_jump_to_pdf_page=False,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason or "page match is approximate and requires human confirmation",
        )

    if status == UNRESOLVED:
        return LocatorDegradation(
            locator_status=UNRESOLVED,
            provenance_level=UNAVAILABLE,
            can_jump_to_pdf_page=False,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason or "locator could not be resolved",
        )

    if has_page:
        return LocatorDegradation(
            locator_status=EXACT_PAGE,
            provenance_level=EXACT_PDF_PAGE,
            can_jump_to_pdf_page=True,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason,
        )

    if status == TEXT_ONLY:
        return LocatorDegradation(
            locator_status=TEXT_ONLY,
            provenance_level=TEXT_EVIDENCE_ONLY,
            can_jump_to_pdf_page=False,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason or "page missing; only evidence text is available",
        )

    if status == MISSING_PAGE or (has_text and status != MISSING_LOCATOR):
        return LocatorDegradation(
            locator_status=MISSING_PAGE,
            provenance_level=TEXT_EVIDENCE_ONLY,
            can_jump_to_pdf_page=False,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason or "page missing from evidence locator",
        )

    if has_text:
        return LocatorDegradation(
            locator_status=TEXT_ONLY,
            provenance_level=TEXT_EVIDENCE_ONLY,
            can_jump_to_pdf_page=False,
            can_highlight_in_pdf=False,
            warning_reason=warning_reason or "only evidence text is available",
        )

    return LocatorDegradation(
        locator_status=MISSING_LOCATOR,
        provenance_level=UNAVAILABLE,
        can_jump_to_pdf_page=False,
        can_highlight_in_pdf=False,
        warning_reason=warning_reason or "no locator evidence available",
    )
