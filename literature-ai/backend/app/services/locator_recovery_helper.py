from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


MATCH_EXACT = "exact_match"
MATCH_NORMALIZED_WHITESPACE = "normalized_whitespace_match"
MATCH_SUBSTRING = "substring_match"
MATCH_TARGET_TOKEN = "target_token_match"


@dataclass(frozen=True)
class LocatorRecoveryCandidate:
    text: str
    source_artifact: str
    page: int | None = None
    bbox: dict[str, Any] | None = None
    candidate_id: str | None = None
    source_type: str = "artifact_text"

    @classmethod
    def from_mapping(
        cls,
        item: dict[str, Any],
        *,
        default_source_artifact: str = "artifact_text",
        source_type: str = "artifact_text",
    ) -> "LocatorRecoveryCandidate":
        text = _first_text_value(item)
        source_artifact = str(
            item.get("source_artifact")
            or item.get("artifact")
            or item.get("path")
            or default_source_artifact
        )
        page, bbox = _extract_page_bbox(item)
        candidate_id = item.get("candidate_id") or item.get("id") or item.get("self_ref")
        return cls(
            text=text,
            source_artifact=source_artifact,
            page=page,
            bbox=bbox,
            candidate_id=str(candidate_id) if candidate_id is not None else None,
            source_type=str(item.get("source_type") or source_type),
        )


@dataclass(frozen=True)
class LocatorRecoveryRequest:
    paper_id: str
    field_name: str
    target_value: Any
    review_id: str | None = None
    evidence_text: str | None = None
    evidence_reference: Any = None
    candidate_artifacts: tuple[LocatorRecoveryCandidate | dict[str, Any], ...] = ()
    evidence_spans: tuple[LocatorRecoveryCandidate | dict[str, Any], ...] = ()
    docling_blocks: tuple[LocatorRecoveryCandidate | dict[str, Any], ...] = ()


@dataclass(frozen=True)
class LocatorRepairProposal:
    paper_id: str
    review_id: str | None
    field_name: str
    target_value: Any
    status: str
    proposed_locator_status: str
    source_artifact: str | None = None
    page: int | None = None
    bbox: dict[str, Any] | None = None
    matched_text: str | None = None
    match_method: str | None = None
    confidence: float = 0.0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)
    should_write_locator: bool = False
    requires_human_confirmation: bool = True
    mark_verified: bool = False
    safe_verified: bool = False
    export_eligible: bool = False
    writing_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "review_id": self.review_id,
            "field_name": self.field_name,
            "target_value": self.target_value,
            "status": self.status,
            "proposed_locator_status": self.proposed_locator_status,
            "source_artifact": self.source_artifact,
            "page": self.page,
            "bbox": self.bbox,
            "matched_text": self.matched_text,
            "match_method": self.match_method,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "should_write_locator": self.should_write_locator,
            "requires_human_confirmation": self.requires_human_confirmation,
            "mark_verified": self.mark_verified,
            "safe_verified": self.safe_verified,
            "export_eligible": self.export_eligible,
            "writing_eligible": self.writing_eligible,
        }


@dataclass(frozen=True)
class _Match:
    candidate: LocatorRecoveryCandidate
    method: str
    rank: int
    confidence: float
    query: str


class ControlledLocatorRecoveryHelper:
    """Read-only helper for locator repair proposals.

    This helper never accepts a database session and has no persistence path.
    It returns proposal metadata only; downstream code must keep human review,
    locator writing, and export/writing eligibility as separate gates.
    """

    def build_proposal(self, request: LocatorRecoveryRequest) -> LocatorRepairProposal:
        if self._must_remain_red(request):
            return self._red(
                request,
                blockers=(
                    "d4_3e_red_field_not_repairable",
                    "convergence_settings_requires_new_source_evidence",
                ),
                match_method=None,
            )

        candidates = self._collect_candidates(request)
        query_terms = self._query_terms(request)
        if not query_terms:
            return self._red(request, blockers=("missing_match_query",), match_method=None)
        if not candidates:
            return self._red(request, blockers=("missing_candidate_artifacts",), match_method=None)

        matches = self._find_matches(candidates, query_terms)
        if not matches:
            return self._red(
                request,
                blockers=("no_text_match",),
                match_method="no_match",
            )

        best = matches[0]
        tied = [match for match in matches if match.rank == best.rank and match.method == best.method]
        ambiguous = len({self._candidate_key(match.candidate) for match in tied}) > 1

        warnings = ["proposal_not_verified", "does_not_unlock_export_or_writing"]
        blockers: list[str] = []
        if ambiguous:
            warnings.append("ambiguous_multiple_matches")
            blockers.append("ambiguous_match_requires_human_selection")
        if best.candidate.page is None:
            warnings.append("page_unavailable")
            blockers.append("no_page_in_source")
        if best.candidate.bbox is None:
            warnings.append("bbox_unavailable")

        proposed_locator_status = "exact_page" if best.candidate.page is not None else "text_only"
        status = self._proposal_status(best, ambiguous=ambiguous)
        confidence = best.confidence
        if best.candidate.page is None:
            status = "yellow"
            confidence = min(confidence, 0.35)
        if ambiguous:
            status = "yellow"
            confidence = min(confidence, 0.49)

        return LocatorRepairProposal(
            paper_id=request.paper_id,
            review_id=request.review_id,
            field_name=request.field_name,
            target_value=request.target_value,
            status=status,
            proposed_locator_status=proposed_locator_status,
            source_artifact=best.candidate.source_artifact,
            page=best.candidate.page,
            bbox=best.candidate.bbox,
            matched_text=best.candidate.text,
            match_method=best.method,
            confidence=round(confidence, 3),
            warnings=tuple(dict.fromkeys(warnings)),
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _collect_candidates(self, request: LocatorRecoveryRequest) -> list[LocatorRecoveryCandidate]:
        candidates: list[LocatorRecoveryCandidate] = []
        candidates.extend(
            _coerce_candidate(item, source_type="artifact_text")
            for item in request.candidate_artifacts
        )
        candidates.extend(
            _coerce_candidate(item, source_type="evidence_span")
            for item in request.evidence_spans
        )
        candidates.extend(
            _coerce_candidate(item, source_type="docling_block")
            for item in request.docling_blocks
        )
        return [candidate for candidate in candidates if candidate.text.strip()]

    def _query_terms(self, request: LocatorRecoveryRequest) -> list[str]:
        terms: list[str] = []
        _append_term(terms, request.evidence_text)
        for value in _evidence_reference_terms(request.evidence_reference):
            _append_term(terms, value)
        for value in _target_value_terms(request.target_value):
            _append_term(terms, value)
        for item in request.evidence_spans:
            _append_term(terms, _coerce_candidate(item, source_type="evidence_span").text)
        return list(dict.fromkeys(terms))

    def _find_matches(
        self,
        candidates: list[LocatorRecoveryCandidate],
        query_terms: list[str],
    ) -> list[_Match]:
        matches: list[_Match] = []
        for candidate in candidates:
            candidate_text = candidate.text.strip()
            candidate_ws = _normalize_whitespace(candidate_text)
            candidate_compact = _compact(candidate_text)
            for query in query_terms:
                query_text = query.strip()
                if not query_text:
                    continue
                query_ws = _normalize_whitespace(query_text)
                query_compact = _compact(query_text)
                if candidate_text == query_text:
                    matches.append(_Match(candidate, MATCH_EXACT, 4, 0.92, query_text))
                elif candidate_ws == query_ws:
                    matches.append(_Match(candidate, MATCH_NORMALIZED_WHITESPACE, 3, 0.85, query_text))
                elif query_ws and (query_ws in candidate_ws or candidate_ws in query_ws):
                    matches.append(_Match(candidate, MATCH_SUBSTRING, 2, 0.68, query_text))
                elif query_compact and len(query_compact) >= 3 and query_compact in candidate_compact:
                    matches.append(_Match(candidate, MATCH_TARGET_TOKEN, 1, 0.56, query_text))
        matches.sort(key=lambda match: (match.rank, match.confidence), reverse=True)
        return matches

    @staticmethod
    def _proposal_status(match: _Match, *, ambiguous: bool) -> str:
        if ambiguous:
            return "yellow"
        if match.method in {MATCH_EXACT, MATCH_NORMALIZED_WHITESPACE} and match.candidate.page is not None:
            return "green"
        return "yellow"

    @staticmethod
    def _candidate_key(candidate: LocatorRecoveryCandidate) -> tuple[Any, ...]:
        return (
            candidate.source_artifact,
            candidate.candidate_id,
            candidate.page,
            _normalize_whitespace(candidate.text),
        )

    @staticmethod
    def _must_remain_red(request: LocatorRecoveryRequest) -> bool:
        if request.field_name != "convergence_settings":
            return False
        return True

    @staticmethod
    def _red(
        request: LocatorRecoveryRequest,
        *,
        blockers: tuple[str, ...],
        match_method: str | None,
    ) -> LocatorRepairProposal:
        return LocatorRepairProposal(
            paper_id=request.paper_id,
            review_id=request.review_id,
            field_name=request.field_name,
            target_value=request.target_value,
            status="red",
            proposed_locator_status="missing_locator",
            match_method=match_method,
            confidence=0.0,
            warnings=("proposal_not_verified", "does_not_unlock_export_or_writing"),
            blockers=blockers,
        )


def build_locator_repair_proposal(request: LocatorRecoveryRequest) -> LocatorRepairProposal:
    return ControlledLocatorRecoveryHelper().build_proposal(request)


def _coerce_candidate(item: LocatorRecoveryCandidate | dict[str, Any], *, source_type: str) -> LocatorRecoveryCandidate:
    if isinstance(item, LocatorRecoveryCandidate):
        return item
    return LocatorRecoveryCandidate.from_mapping(
        item,
        default_source_artifact=source_type,
        source_type=source_type,
    )


def _first_text_value(item: dict[str, Any]) -> str:
    for key in ("text", "content", "caption", "evidence_text", "matched_text", "orig"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_page_bbox(item: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None]:
    page = _coerce_page(item.get("page") or item.get("page_no") or item.get("page_number"))
    bbox = _coerce_bbox(item.get("bbox"))
    prov = item.get("prov")
    if isinstance(prov, list):
        prov = next((entry for entry in prov if isinstance(entry, dict)), None)
    if isinstance(prov, dict):
        if page is None:
            page = _coerce_page(prov.get("page") or prov.get("page_no") or prov.get("page_number"))
        if bbox is None:
            bbox = _coerce_bbox(prov.get("bbox"))
    return page, bbox


def _coerce_page(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _coerce_bbox(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if {"x0", "y0", "x1", "y1"} <= set(value):
        return {
            "x0": float(value["x0"]),
            "y0": float(value["y0"]),
            "x1": float(value["x1"]),
            "y1": float(value["y1"]),
            **_optional_bbox_size(value),
            "coordinate_system": value.get("coordinate_system") or "pdf_points",
        }
    if {"l", "t", "r", "b"} <= set(value):
        return {
            "x0": float(value["l"]),
            "y0": float(value["t"]),
            "x1": float(value["r"]),
            "y1": float(value["b"]),
            **_optional_bbox_size(value),
            "coordinate_system": value.get("coordinate_system") or value.get("coord_origin") or "pdf_points",
        }
    return None


def _optional_bbox_size(value: dict[str, Any]) -> dict[str, Any]:
    size: dict[str, Any] = {}
    if value.get("width") is not None:
        size["width"] = float(value["width"])
    if value.get("height") is not None:
        size["height"] = float(value["height"])
    return size


def _append_term(terms: list[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = json.dumps(value, ensure_ascii=True, sort_keys=True)
    if cleaned and cleaned not in {"{}", "[]", "null"}:
        terms.append(cleaned)


def _evidence_reference_terms(value: Any) -> list[str]:
    terms: list[str] = []
    if isinstance(value, str):
        _append_term(terms, value)
    elif isinstance(value, dict):
        for key in ("text", "evidence_text", "quote", "caption", "matched_text"):
            _append_term(terms, value.get(key))
    elif isinstance(value, list):
        for item in value:
            terms.extend(_evidence_reference_terms(item))
    return terms


def _target_value_terms(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value, value.replace("_", "-"), value.replace("_", " ")]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, dict):
        return []
    if value is None:
        return []
    return [str(value)]


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value).lower()
