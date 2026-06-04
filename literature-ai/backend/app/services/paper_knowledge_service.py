from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExternalAnalysisCandidate, PaperNote
from app.schemas.api import PaperDetailResponse
from app.services.paper_query import PaperQueryService


SECTION_CATEGORY_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("abstract", "abstract", ("abstract",)),
    ("research_context", "research context", ("intro", "background", "motivation")),
    ("computational_method", "computational method", ("comput", "method", "calculation", "theoretical", "dft")),
    ("synthesis_method", "preparation or synthesis method", ("synthesis", "preparation", "fabrication", "experimental")),
    ("mechanism_context", "mechanism context", ("mechanism", "discussion", "reactivity", "defect", "vacancy", "adsorption", "electronic", "charge", "strain")),
    ("conclusion", "conclusion", ("conclusion", "summary", "outlook")),
]

MECHANISM_TEXT_HINTS = (
    "mechanism",
    "origin",
    "because",
    "due to",
    "attribute",
    "defect",
    "vacancy",
    "stone-wales",
    "adsorption",
    "reactivity",
    "formation energy",
    "migration barrier",
    "charge density",
    "density of states",
    "electronic structure",
    "strain",
)


@dataclass
class KnowledgeCandidate:
    id: str
    paper_id: str
    category: str
    title: str
    content: str
    source_type: str
    source_id: str | None = None
    evidence_text: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    confidence: float | None = None
    candidate_status: str = "candidate_unverified"
    evidence_state: str = "text_only_candidate"
    recommended_action: str = "Review against the PDF and parsed evidence before using this in writing."
    metadata: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paper_id": self.paper_id,
            "category": self.category,
            "title": self.title,
            "content": self.content,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "evidence_text": self.evidence_text,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_title": self.section_title,
            "confidence": self.confidence,
            "candidate_status": self.candidate_status,
            "evidence_state": self.evidence_state,
            "recommended_action": self.recommended_action,
            "metadata": self.metadata or {},
        }


class PaperKnowledgeService:
    """Build Codex-ready paper knowledge candidates from structured and fallback sources.

    The service deliberately returns candidates, not final facts. It prevents blank
    mechanism/writing panels by falling back to evidence-bearing sections and
    imported external analysis when the domain-specific extractors miss a paper.
    """

    schema_version = "paper_knowledge_context_v1"

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_context(
        self,
        paper_id: UUID,
        *,
        max_candidates: int = 60,
        max_chars_per_candidate: int = 1200,
        category: str | None = None,
    ) -> dict[str, Any] | None:
        detail = PaperQueryService(self.session).get_paper_detail(paper_id)
        if detail is None:
            return None

        candidates = self.build_candidates(
            detail,
            max_candidates=max_candidates,
            max_chars_per_candidate=max_chars_per_candidate,
        )
        if category:
            normalized = category.strip().lower()
            candidates = [item for item in candidates if item["category"].lower() == normalized]

        counts = Counter(item["category"] for item in candidates)
        source_counts = Counter(item["source_type"] for item in candidates)
        payload = {
            "schema_version": self.schema_version,
            "paper_id": str(detail.id),
            "title": detail.title,
            "reliability_policy": {
                "knowledge_items_are_candidates": True,
                "section_fallbacks_are_not_final_claims": True,
                "external_ai_imports_are_unverified": True,
                "use_codex_or_human_review_before_citing": True,
            },
            "metadata": {
                "returned": len(candidates),
                "category_counts": dict(sorted(counts.items())),
                "source_type_counts": dict(sorted(source_counts.items())),
                "has_mechanism_claims": bool(detail.mechanism_claims_items),
                "has_writing_cards": bool(detail.writing_cards_items),
            },
            "candidates": candidates[:max_candidates],
            "markdown": self._markdown(detail, candidates[:max_candidates]),
        }
        return payload

    def build_candidates(
        self,
        detail: PaperDetailResponse,
        *,
        max_candidates: int = 60,
        max_chars_per_candidate: int = 1200,
    ) -> list[dict[str, Any]]:
        items: list[KnowledgeCandidate] = []
        paper_id = str(detail.id)

        for claim in detail.mechanism_claims_items or []:
            claim_payload = claim.model_dump(mode="json")
            items.append(
                KnowledgeCandidate(
                    id=f"mechanism_claim:{claim.id}",
                    paper_id=paper_id,
                    category="mechanism",
                    title=claim.claim_type or "Mechanism claim",
                    content=self._clip(claim.claim_text, max_chars_per_candidate),
                    source_type="mechanism_claim",
                    source_id=str(claim.id),
                    evidence_text=self._clip(claim.evidence_text, max_chars_per_candidate),
                    confidence=claim.confidence,
                    evidence_state="structured_extraction_candidate",
                    metadata={"evidence_types": claim_payload.get("evidence_types") or []},
                )
            )

        for card in detail.writing_cards_items or []:
            card_payload = card.model_dump(mode="json")
            for category, field_name, title in (
                ("research_gap", "research_gap", "Research gap"),
                ("proposed_solution", "proposed_solution", "Proposed solution"),
                ("core_hypothesis", "core_hypothesis", "Core hypothesis"),
                ("writing_logic", "abstract_logic", "Abstract logic"),
                ("writing_logic", "introduction_logic", "Introduction logic"),
                ("writing_logic", "discussion_logic", "Discussion logic"),
            ):
                value = card_payload.get(field_name)
                if not self._has_text(value):
                    continue
                items.append(
                    KnowledgeCandidate(
                        id=f"writing_card:{card.id}:{field_name}",
                        paper_id=paper_id,
                        category=category,
                        title=title,
                        content=self._clip(value, max_chars_per_candidate),
                        source_type="writing_card",
                        source_id=str(card.id),
                        evidence_text=self._evidence_chain_preview(card_payload.get("evidence_chain")),
                        evidence_state=card_payload.get("evidence_chain_status") or "writing_candidate",
                        candidate_status="candidate_unverified",
                        recommended_action="Use this writing logic only after checking its evidence chain.",
                        metadata={
                            "review_gate_status": card_payload.get("review_gate_status"),
                            "blocked_reasons": card_payload.get("blocked_reasons") or [],
                        },
                    )
                )

        items.extend(self._external_candidates(detail.id, max_chars=max_chars_per_candidate))
        items.extend(self._note_candidates(detail.id, max_chars=max_chars_per_candidate))
        items.extend(self._section_fallback_candidates(detail, max_chars=max_chars_per_candidate))

        return [item.payload() for item in self._deduplicate(items)[:max_candidates]]

    def _section_fallback_candidates(self, detail: PaperDetailResponse, *, max_chars: int) -> list[KnowledgeCandidate]:
        paper_id = str(detail.id)
        items: list[KnowledgeCandidate] = []
        if detail.abstract:
            items.append(
                KnowledgeCandidate(
                    id=f"section_fallback:{paper_id}:abstract",
                    paper_id=paper_id,
                    category="abstract",
                    title="Abstract candidate",
                    content=self._clip(detail.abstract, max_chars),
                    source_type="paper_abstract",
                    evidence_text=self._clip(detail.abstract, max_chars),
                    evidence_state="parsed_source_text",
                    candidate_status="source_text_candidate",
                )
            )

        for section in detail.sections or []:
            title = section.section_title or section.section_type or "Untitled section"
            category, category_title = self._classify_section(title, section.text)
            if category is None:
                continue
            content = self._best_section_snippet(section.text, category=category, max_chars=max_chars)
            if not content:
                continue
            items.append(
                KnowledgeCandidate(
                    id=f"section_fallback:{section.id}:{category}",
                    paper_id=paper_id,
                    category=category,
                    title=category_title,
                    content=content,
                    source_type="paper_section",
                    source_id=str(section.id),
                    evidence_text=content,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    section_title=title,
                    confidence=0.55,
                    candidate_status="section_candidate_unverified",
                    evidence_state="parsed_source_text",
                    recommended_action="Read this section context directly; summarize or cite only after Codex/human review.",
                    metadata={"section_type": section.section_type},
                )
            )
        return items

    def _external_candidates(self, paper_id: UUID, *, max_chars: int) -> list[KnowledgeCandidate]:
        rows = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .order_by(ExternalAnalysisCandidate.created_at.desc())
            .limit(40)
        ).all()
        items: list[KnowledgeCandidate] = []
        for row in rows:
            payload = row.normalized_payload if isinstance(row.normalized_payload, dict) else {}
            content = (
                payload.get("content")
                or payload.get("summary")
                or payload.get("reason")
                or payload.get("raw_item")
                or payload.get("raw_payload")
                or row.mapping_reason
                or ""
            )
            if not self._has_text(content):
                continue
            evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            items.append(
                KnowledgeCandidate(
                    id=f"external_analysis:{row.id}",
                    paper_id=str(paper_id),
                    category=self._external_category(row.candidate_type, payload),
                    title=f"External analysis {row.candidate_type}",
                    content=self._clip(content, max_chars),
                    source_type="external_analysis_candidate",
                    source_id=str(row.id),
                    evidence_text=self._clip(evidence.get("quoted_text") or evidence.get("note") or "", max_chars),
                    page_start=evidence.get("page") if isinstance(evidence.get("page"), int) else None,
                    section_title=evidence.get("section_title") if isinstance(evidence.get("section_title"), str) else None,
                    confidence=row.confidence,
                    candidate_status=f"external_{row.status}_candidate",
                    evidence_state="external_ai_import_unverified",
                    recommended_action="Treat imported AI analysis as a note candidate until checked against paper evidence.",
                    metadata={
                        "candidate_type": row.candidate_type,
                        "status": row.status,
                        "mapping_reason": row.mapping_reason,
                        "materialized_target_type": row.materialized_target_type,
                        "materialized_target_id": row.materialized_target_id,
                    },
                )
            )
        return items

    def _note_candidates(self, paper_id: UUID, *, max_chars: int) -> list[KnowledgeCandidate]:
        rows = self.session.scalars(
            select(PaperNote)
            .where(PaperNote.paper_id == paper_id)
            .order_by(PaperNote.created_at.desc())
            .limit(30)
        ).all()
        items: list[KnowledgeCandidate] = []
        for row in rows:
            if not self._has_text(row.content):
                continue
            items.append(
                KnowledgeCandidate(
                    id=f"paper_note:{row.id}",
                    paper_id=str(paper_id),
                    category="curation_note",
                    title=row.field_name or "Curator note",
                    content=self._clip(row.content, max_chars),
                    source_type="paper_note",
                    source_id=str(row.id),
                    evidence_text=self._clip(row.quoted_text, max_chars),
                    page_start=row.page,
                    section_title=row.section_title,
                    candidate_status="human_or_codex_note_candidate",
                    evidence_state="note_with_optional_quote",
                    recommended_action="Use as curator context; verify any quoted scientific claim before citation.",
                    metadata={"source": row.source},
                )
            )
        return items

    @staticmethod
    def _classify_section(title: str, text: str | None) -> tuple[str | None, str | None]:
        title_l = title.lower()
        for category, display, tokens in SECTION_CATEGORY_RULES:
            if any(token in title_l for token in tokens):
                return category, display

        haystack = f"{title}\n{text or ''}".lower()
        for category, display, tokens in SECTION_CATEGORY_RULES:
            if category == "conclusion":
                continue
            if any(token in haystack for token in tokens):
                return category, display
        if any(token in haystack for token in MECHANISM_TEXT_HINTS):
            return "mechanism_context", "mechanism context"
        return None, None

    def _best_section_snippet(self, text: str | None, *, category: str, max_chars: int) -> str:
        compact = self._clean(text)
        if not compact:
            return ""
        if category == "mechanism_context":
            match = self._first_match_window(compact, MECHANISM_TEXT_HINTS, max_chars=max_chars)
            if match:
                return match
        if category in {"research_context", "conclusion", "computational_method", "synthesis_method"}:
            return self._clip(compact, max_chars)
        return self._clip(compact, max_chars)

    @staticmethod
    def _first_match_window(text: str, tokens: tuple[str, ...], *, max_chars: int) -> str | None:
        lowered = text.lower()
        positions = [lowered.find(token) for token in tokens if lowered.find(token) >= 0]
        if not positions:
            return None
        pos = min(positions)
        start = max(0, pos - max_chars // 3)
        end = min(len(text), start + max_chars)
        return text[start:end].strip()

    @staticmethod
    def _external_category(candidate_type: str, payload: dict[str, Any]) -> str:
        field_name = str(payload.get("field_name") or "").lower()
        content = str(payload.get("content") or payload.get("reason") or "").lower()
        if "mechanism" in field_name or any(token in content for token in ("mechanism", "origin", "adsorption", "defect")):
            return "mechanism_context"
        if candidate_type == "correction":
            return "correction_candidate"
        if candidate_type == "relationship":
            return "citation_relationship"
        return "external_analysis"

    @staticmethod
    def _evidence_chain_preview(value: Any) -> str | None:
        if not value:
            return None
        if isinstance(value, list):
            pieces = []
            for item in value[:5]:
                if isinstance(item, dict):
                    pieces.append(str(item.get("text") or item.get("source") or ""))
                else:
                    pieces.append(str(item))
            return " | ".join(piece for piece in pieces if piece)
        if isinstance(value, dict):
            return " | ".join(str(v) for v in value.values() if v)[:800]
        return str(value)[:800]

    def _markdown(self, detail: PaperDetailResponse, candidates: list[dict[str, Any]]) -> str:
        lines = [
            f"# Paper Knowledge Candidates: {detail.title or 'Untitled paper'}",
            "",
            "All items below are candidates for Codex reading, review writing, or data curation. They are not final facts until checked against evidence.",
            "",
        ]
        if not candidates:
            lines.extend(["No knowledge candidates were generated.", ""])
            return "\n".join(lines)
        for item in candidates:
            page = self._page_label(item.get("page_start"), item.get("page_end"))
            lines.append(f"## {item.get('category')} - {item.get('title')}")
            lines.append(f"- Source: {item.get('source_type')} {item.get('source_id') or ''} {page}".rstrip())
            lines.append(f"- Status: {item.get('candidate_status')} / {item.get('evidence_state')}")
            lines.append("")
            lines.append(str(item.get("content") or ""))
            if item.get("evidence_text") and item.get("evidence_text") != item.get("content"):
                lines.append("")
                lines.append("Evidence:")
                lines.append(str(item.get("evidence_text")))
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _deduplicate(items: list[KnowledgeCandidate]) -> list[KnowledgeCandidate]:
        seen: dict[str, KnowledgeCandidate] = {}
        for item in items:
            key = f"{item.category}:{item.source_type}:{PaperKnowledgeService._clean(item.content)[:180].lower()}"
            if key not in seen:
                seen[key] = item
            else:
                existing = seen[key]
                if (item.confidence or 0) > (existing.confidence or 0):
                    seen[key] = item
        return list(seen.values())

    @staticmethod
    def _has_text(value: Any) -> bool:
        return bool(str(value or "").strip())

    @staticmethod
    def _clean(value: Any) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _clip(value: Any, max_chars: int) -> str:
        text = PaperKnowledgeService._clean(value)
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 12].rstrip() + " [truncated]"

    @staticmethod
    def _page_label(start: int | None, end: int | None) -> str:
        if start and end and start != end:
            return f"(pages {start}-{end})"
        if start:
            return f"(page {start})"
        return ""
