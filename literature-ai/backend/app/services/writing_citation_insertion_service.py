from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Paper
from app.services.writing_citation_candidate_service import WritingCitationCandidateService


@dataclass(frozen=True)
class CitationInsertionDraftRequest:
    text: str
    selected_paper_id: UUID
    citation_marker: str | None = None
    insertion_mode: str = "parenthetical"
    citation_style: str = "draft_author_year"
    candidate_evidence_status: str | None = None
    candidate_can_be_used_as_confirmed_citation: bool | None = None
    candidate_requires_human_verification: bool | None = None
    supporting_snippet: str | None = None
    user_note: str | None = None


class WritingCitationInsertionService:
    """Build a read-only citation insertion proposal; never writes final citations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def draft(self, request: CitationInsertionDraftRequest) -> dict[str, Any] | None:
        state = WritingCitationCandidateService(self.session).evaluate_selected_paper(
            paper_id=request.selected_paper_id,
            text=request.text,
        )
        if state is None:
            return None
        paper: Paper = state["paper"]
        priority = state["citation_priority"]
        if state["exclude_from_citation"] or priority == "exclude":
            return self._blocked_response(
                paper=paper,
                citation_marker=self._citation_marker(paper, request.citation_marker),
                insertion_mode=request.insertion_mode,
                evidence_status=state["evidence_status"],
                blocked_reason="exclude_from_citation=true" if state["exclude_from_citation"] else "citation_priority=exclude",
            )

        evidence_status = state["evidence_status"]
        safe_confirmed = bool(state["can_be_used_as_confirmed_citation"])
        proposal_status = self._proposal_status(evidence_status, safe_confirmed)
        marker = self._citation_marker(paper, request.citation_marker)
        warnings = self._warnings(
            evidence_status=evidence_status,
            impact_missing=state["impact"] is None or state["impact"].impact_factor is None,
            client_claimed_confirmed=bool(request.candidate_can_be_used_as_confirmed_citation),
            safe_confirmed=safe_confirmed,
        )
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "year": paper.year,
            "journal": paper.journal,
            "citation_marker": self._formatted_marker(marker, request.insertion_mode, safe_confirmed),
            "insertion_mode": request.insertion_mode,
            "citation_style": request.citation_style,
            "draft_text": self._draft_text(
                request.text,
                marker=marker,
                insertion_mode=request.insertion_mode,
                safe_confirmed=safe_confirmed,
            ),
            "proposal_status": proposal_status,
            "can_insert_as_confirmed_citation": safe_confirmed,
            "requires_human_verification": not safe_confirmed,
            "evidence_status": evidence_status,
            "supporting_snippets": state["supporting_snippets"],
            "warnings": warnings,
            "human_review_checklist": self._checklist(evidence_status),
            "blocked_actions": [
                "no_database_write",
                "no_verified_status_change",
                "no_bibliography_generation",
                "no_export_unlock",
            ],
            "safety": {
                "read_only": True,
                "writes_db": False,
                "marks_verified": False,
                "generates_bibliography": False,
                "inserts_final_citation": False,
                "trusts_client_safety_flags": False,
            },
        }

    def _blocked_response(
        self,
        *,
        paper: Paper,
        citation_marker: str,
        insertion_mode: str,
        evidence_status: str,
        blocked_reason: str,
    ) -> dict[str, Any]:
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "year": paper.year,
            "journal": paper.journal,
            "citation_marker": citation_marker,
            "insertion_mode": insertion_mode,
            "citation_style": "draft_author_year",
            "draft_text": None,
            "proposal_status": "blocked_excluded_from_citation",
            "can_insert_as_confirmed_citation": False,
            "requires_human_verification": True,
            "evidence_status": evidence_status,
            "blocked_reason": blocked_reason,
            "supporting_snippets": [],
            "warnings": [
                "This paper is excluded from citation and cannot receive a normal draft citation proposal.",
                "This is a draft citation proposal only; no database write was performed.",
            ],
            "human_review_checklist": [
                "Review citation eligibility before reconsidering this paper.",
                "Do not insert this citation unless the exclusion is explicitly lifted by a separate review action.",
            ],
            "blocked_actions": [
                "no_database_write",
                "no_verified_status_change",
                "no_bibliography_generation",
                "no_export_unlock",
                "no_normal_draft_text_for_excluded_paper",
            ],
            "safety": {
                "read_only": True,
                "writes_db": False,
                "marks_verified": False,
                "generates_bibliography": False,
                "inserts_final_citation": False,
                "trusts_client_safety_flags": False,
            },
        }

    def _citation_marker(self, paper: Paper, provided: str | None) -> str:
        if provided and provided.strip():
            return provided.strip()
        author = _author_label(paper.authors)
        year = str(paper.year or "n.d.")
        return f"{author}, {year}"

    def _formatted_marker(self, marker: str, insertion_mode: str, safe_confirmed: bool) -> str:
        if not safe_confirmed:
            return f"[DRAFT CITATION - VERIFY SOURCE BEFORE USE: {marker}]"
        if insertion_mode == "narrative":
            return _narrative_marker(marker)
        if insertion_mode == "comment_only":
            return f"[DRAFT CITATION COMMENT: {marker}]"
        return f"({marker})"

    def _draft_text(self, text: str, *, marker: str, insertion_mode: str, safe_confirmed: bool) -> str:
        formatted = self._formatted_marker(marker, insertion_mode, safe_confirmed)
        clean = text.strip()
        if insertion_mode == "comment_only":
            return f"{clean} {formatted}"
        if insertion_mode == "narrative" and safe_confirmed:
            return f"{_narrative_marker(marker)} {clean[0].lower() + clean[1:] if clean else clean}"
        return _append_marker(clean, formatted)

    def _proposal_status(self, evidence_status: str, safe_confirmed: bool) -> str:
        if safe_confirmed and evidence_status == "safe_verified":
            return "confirmed_candidate_draft"
        if evidence_status == "verified":
            return "verified_but_requires_safety_review"
        if evidence_status == "metadata_only":
            return "metadata_only_draft"
        return "needs_human_verification"

    def _warnings(
        self,
        *,
        evidence_status: str,
        impact_missing: bool,
        client_claimed_confirmed: bool,
        safe_confirmed: bool,
    ) -> list[str]:
        warnings = ["This is a draft citation proposal only; no database write was performed."]
        if evidence_status == "metadata_only":
            warnings.append("Metadata-only suggestion cannot be used as evidence yet.")
        elif evidence_status == "verified":
            warnings.append("Verified does not equal safe_verified; safety review is still required.")
        elif evidence_status in {"pending_with_locator", "pending_without_locator", "unverified_extraction", "unknown"}:
            warnings.append("VERIFY SOURCE BEFORE USE: this candidate requires human verification.")
        if impact_missing:
            warnings.append("Impact Factor metadata is missing; do not treat metadata completeness as evidence quality.")
        if client_claimed_confirmed and not safe_confirmed:
            warnings.append("Client-provided confirmed citation flag was ignored because current DB state is not safe_verified.")
        return warnings

    def _checklist(self, evidence_status: str) -> list[str]:
        checklist = [
            "Open the paper PDF or verified extraction.",
            "Confirm the sentence is supported by the source.",
            "Confirm page/section locator if required.",
            "Only then promote to confirmed citation.",
        ]
        if evidence_status == "metadata_only":
            checklist.insert(0, "Treat this as metadata-only relevance until a source passage is reviewed.")
        return checklist


def _append_marker(text: str, marker: str) -> str:
    if not text:
        return marker
    match = re.search(r"([.!?])\s*$", text)
    if match:
        return f"{text[:match.start()].rstrip()} {marker}{match.group(1)}"
    return f"{text} {marker}"


def _author_label(authors: Any) -> str:
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            name = first.get("last") or first.get("family") or first.get("name") or first.get("full_name")
        else:
            name = str(first)
        label = str(name or "").strip()
        if label:
            return f"{label} et al." if len(authors) > 1 else label
    return "Selected paper"


def _narrative_marker(marker: str) -> str:
    if "," in marker:
        author, year = marker.rsplit(",", 1)
        return f"{author.strip()} ({year.strip()})"
    return marker
