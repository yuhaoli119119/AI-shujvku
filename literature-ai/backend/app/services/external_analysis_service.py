from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperNote,
    PaperRelationship,
)
from app.services.llm_service import LLMService
from app.utils.text_cleaning import normalize_text_tree, repair_mojibake_text


class ExternalReviewNoteModel(BaseModel):
    content: str
    field_name: str | None = None
    page: int | None = None
    section_title: str | None = None
    quoted_text: str | None = None
    confidence: float | None = Field(default=0.7, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalCorrectionProposalModel(BaseModel):
    field_name: str
    target_path: str
    operation: str = "replace"
    proposed_value: Any = None
    reason: str
    evidence_payload: dict[str, Any] | list[Any] | None = None
    confidence: float | None = Field(default=0.7, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalSupportingPaperModel(BaseModel):
    relationship_type: str = "supports"
    target_paper_id: str | None = None
    target_doi: str | None = None
    target_title: str | None = None
    note: str | None = None
    confidence: float | None = Field(default=0.6, ge=0.0, le=1.0)
    mapping_reason: str | None = None


class ExternalAnalysisNormalizedModel(BaseModel):
    review_notes: list[ExternalReviewNoteModel] = Field(default_factory=list)
    correction_proposals: list[ExternalCorrectionProposalModel] = Field(default_factory=list)
    supporting_papers: list[ExternalSupportingPaperModel] = Field(default_factory=list)
    unmapped_items: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class MaterializationResult:
    created_notes: int = 0
    created_corrections: int = 0
    created_relationships: int = 0
    skipped_candidates: int = 0


class ExternalAnalysisService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.llm = LLMService(settings)

    def import_run(
        self,
        paper_id: UUID,
        source: str,
        source_label: str | None,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
    ) -> ExternalAnalysisRun:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")

        sanitized_raw_text = repair_mojibake_text(raw_text)
        sanitized_raw_payload = normalize_text_tree(raw_payload)
        normalized, mapping_status, mapping_error = self._normalize_input(
            raw_text=sanitized_raw_text,
            raw_payload=sanitized_raw_payload,
        )
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source=source,
            source_label=source_label,
            raw_text=sanitized_raw_text,
            raw_payload=sanitized_raw_payload,
            normalized_payload=normalized.model_dump(mode="json") if normalized else None,
            mapping_status=mapping_status,
            mapping_error=mapping_error,
        )
        self.session.add(run)
        self.session.flush()

        if normalized:
            self._create_candidates(run, normalized)

        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="import_external_analysis",
                source=source,
                target_type="external_analysis_run",
                target_id=str(run.id),
                payload={"source_label": source_label, "mapping_status": mapping_status},
            )
        )
        self.session.flush()
        self.session.refresh(run)
        return run

    def list_runs(self, paper_id: UUID | None = None) -> list[ExternalAnalysisRun]:
        stmt = select(ExternalAnalysisRun).order_by(ExternalAnalysisRun.created_at.desc())
        if paper_id:
            stmt = stmt.where(ExternalAnalysisRun.paper_id == paper_id)
        return self.session.scalars(stmt).all()

    def get_run(self, run_id: UUID) -> ExternalAnalysisRun:
        run = self.session.get(ExternalAnalysisRun, run_id)
        if not run:
            raise ValueError("External analysis run not found")
        return run

    def list_candidates(self, run_id: UUID) -> list[ExternalAnalysisCandidate]:
        return self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.run_id == run_id)
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()

    def materialize_candidates(
        self,
        run_id: UUID,
        candidate_ids: list[UUID] | None = None,
        explicit_all: bool = False,
        created_by: str = "system",
    ) -> MaterializationResult:
        run = self.get_run(run_id)
        if candidate_ids == []:
            raise ValueError("candidate_ids=[] is an empty selection and will not materialize candidates")
        if candidate_ids is None and not explicit_all:
            raise ValueError("Materializing all candidates requires explicit_all=true")

        stmt = select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id)
        if candidate_ids is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.id.in_(candidate_ids))
        candidates = self.session.scalars(stmt.order_by(ExternalAnalysisCandidate.created_at.asc())).all()

        result = MaterializationResult()
        for candidate in candidates:
            if candidate.status not in {"pending", "requires_resolution"}:
                result.skipped_candidates += 1
                continue

            payload = candidate.normalized_payload or {}
            if candidate.candidate_type == "note":
                note = PaperNote(
                    paper_id=candidate.paper_id,
                    source=run.source,
                    content=payload.get("content", ""),
                    field_name=payload.get("field_name"),
                    page=payload.get("page"),
                    section_title=payload.get("section_title"),
                    quoted_text=payload.get("quoted_text"),
                )
                self.session.add(note)
                self.session.flush()
                candidate.status = "materialized"
                candidate.materialized_target_type = "paper_note"
                candidate.materialized_target_id = str(note.id)
                result.created_notes += 1
            elif candidate.candidate_type == "correction":
                correction = PaperCorrection(
                    paper_id=candidate.paper_id,
                    source=run.source,
                    field_name=payload.get("field_name", ""),
                    target_path=payload.get("target_path", ""),
                    operation=payload.get("operation", "replace"),
                    proposed_value=payload.get("proposed_value"),
                    reason=payload.get("reason", ""),
                    evidence_payload=payload.get("evidence_payload"),
                    status="pending",
                )
                self.session.add(correction)
                self.session.flush()
                candidate.status = "materialized"
                candidate.materialized_target_type = "paper_correction"
                candidate.materialized_target_id = str(correction.id)
                result.created_corrections += 1
            elif candidate.candidate_type == "relationship":
                target_paper_id = payload.get("target_paper_id")
                if not target_paper_id:
                    candidate.status = "requires_resolution"
                    result.skipped_candidates += 1
                    continue
                relationship = PaperRelationship(
                    source_paper_id=candidate.paper_id,
                    target_paper_id=UUID(str(target_paper_id)),
                    relationship_type=payload.get("relationship_type", "supports"),
                    note=payload.get("note"),
                    created_by=created_by,
                )
                self.session.add(relationship)
                self.session.flush()
                candidate.status = "materialized"
                candidate.materialized_target_type = "paper_relationship"
                candidate.materialized_target_id = str(relationship.id)
                result.created_relationships += 1
            else:
                candidate.status = "skipped"
                result.skipped_candidates += 1
            self.session.add(candidate)

        self.session.add(
            AuditLog(
                paper_id=run.paper_id,
                action="materialize_external_analysis_candidates",
                source=created_by,
                target_type="external_analysis_run",
                target_id=str(run.id),
                payload={
                    "created_notes": result.created_notes,
                    "created_corrections": result.created_corrections,
                    "created_relationships": result.created_relationships,
                    "skipped_candidates": result.skipped_candidates,
                },
            )
        )
        self.session.flush()
        return result

    def _normalize_input(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
    ) -> tuple[ExternalAnalysisNormalizedModel | None, str, str | None]:
        parsed = self._extract_structured_payload(raw_text=raw_text, raw_payload=raw_payload)
        if isinstance(parsed, dict):
            try:
                normalized = ExternalAnalysisNormalizedModel.model_validate(parsed)
                return self._post_process_normalized(normalized), "normalized", None
            except Exception:
                llm_normalized = self._llm_normalize(raw_text=raw_text, raw_payload=parsed)
                if llm_normalized:
                    return self._post_process_normalized(llm_normalized), "normalized_with_llm", None
                return self._heuristic_normalize(parsed), "heuristic", None

        if isinstance(parsed, list):
            return self._heuristic_normalize({"unmapped_items": parsed}), "heuristic", None

        if isinstance(parsed, str) and parsed.strip():
            llm_normalized = self._llm_normalize(raw_text=parsed, raw_payload=None)
            if llm_normalized:
                return self._post_process_normalized(llm_normalized), "normalized_with_llm", None
            return ExternalAnalysisNormalizedModel(
                review_notes=[ExternalReviewNoteModel(content=parsed, mapping_reason="Fallback free-text note import")]
            ), "free_text_fallback", None

        return ExternalAnalysisNormalizedModel(), "empty", None

    def _post_process_normalized(self, normalized: ExternalAnalysisNormalizedModel) -> ExternalAnalysisNormalizedModel:
        supporting = []
        for item in normalized.supporting_papers:
            resolved_target = self._resolve_target_paper_id(item)
            supporting.append(
                item.model_copy(
                    update={
                        "target_paper_id": resolved_target or item.target_paper_id,
                    }
                )
            )
        return normalized.model_copy(update={"supporting_papers": supporting})

    def _create_candidates(self, run: ExternalAnalysisRun, normalized: ExternalAnalysisNormalizedModel) -> None:
        for note in normalized.review_notes:
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="note",
                    normalized_payload=note.model_dump(mode="json"),
                    confidence=note.confidence,
                    mapping_reason=note.mapping_reason,
                    evidence_payload={
                        "page": note.page,
                        "section_title": note.section_title,
                        "quoted_text": note.quoted_text,
                    },
                    status="pending",
                )
            )
        for correction in normalized.correction_proposals:
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="correction",
                    normalized_payload=correction.model_dump(mode="json"),
                    confidence=correction.confidence,
                    mapping_reason=correction.mapping_reason,
                    evidence_payload=correction.evidence_payload,
                    status="pending",
                )
            )
        for relationship in normalized.supporting_papers:
            status = "pending" if relationship.target_paper_id else "requires_resolution"
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="relationship",
                    normalized_payload=relationship.model_dump(mode="json"),
                    confidence=relationship.confidence,
                    mapping_reason=relationship.mapping_reason,
                    evidence_payload={"note": relationship.note},
                    status=status,
                )
            )
        for item in normalized.unmapped_items:
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="unmapped",
                    normalized_payload=item,
                    confidence=item.get("confidence") if isinstance(item, dict) else None,
                    mapping_reason="Could not safely map this external analysis item",
                    status="requires_resolution",
                )
            )

    def _resolve_target_paper_id(self, relationship: ExternalSupportingPaperModel) -> str | None:
        if relationship.target_paper_id:
            return relationship.target_paper_id

        conditions = []
        if relationship.target_doi:
            conditions.append(Paper.doi == relationship.target_doi)
        if relationship.target_title:
            conditions.append(Paper.title.ilike(relationship.target_title))
        if not conditions:
            return None
        target = self.session.scalar(select(Paper).where(or_(*conditions)).limit(1))
        return str(target.id) if target else None

    def _extract_structured_payload(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
    ) -> dict[str, Any] | list[Any] | str | None:
        if isinstance(raw_payload, (dict, list)):
            return raw_payload
        if isinstance(raw_payload, str) and raw_payload.strip():
            parsed = self._try_parse_json(raw_payload)
            return parsed if parsed is not None else raw_payload
        if raw_text and raw_text.strip():
            parsed = self._try_parse_json(raw_text)
            return parsed if parsed is not None else raw_text
        return None

    def _llm_normalize(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | None,
    ) -> ExternalAnalysisNormalizedModel | None:
        source_blob = raw_text if raw_text else json.dumps(raw_payload, ensure_ascii=False, indent=2)
        system_prompt = (
            "You are a scientific data mapping assistant. Convert external AI analysis output into a safe intermediate "
            "schema with review_notes, correction_proposals, supporting_papers, and unmapped_items. "
            "Do not invent record ids. Only emit target_path when the input clearly specifies it. "
            "If a relationship target paper cannot be matched, keep target_paper_id null and preserve title/doi clues."
        )
        user_prompt = f"Normalize this external analysis output:\n\n{source_blob}"
        return self.llm.structured_extract(system_prompt, user_prompt, ExternalAnalysisNormalizedModel)

    def _heuristic_normalize(self, payload: dict[str, Any]) -> ExternalAnalysisNormalizedModel:
        notes = payload.get("review_notes") or payload.get("notes") or []
        corrections = payload.get("correction_proposals") or payload.get("corrections") or []
        supporting = payload.get("supporting_papers") or payload.get("relationships") or []
        unmapped = payload.get("unmapped_items") or []

        if not any([notes, corrections, supporting, unmapped]):
            unmapped = [{"raw_payload": payload}]

        return ExternalAnalysisNormalizedModel(
            review_notes=[
                ExternalReviewNoteModel.model_validate(item if isinstance(item, dict) else {"content": str(item)})
                for item in notes
            ],
            correction_proposals=[
                ExternalCorrectionProposalModel.model_validate(item) for item in corrections if isinstance(item, dict)
            ],
            supporting_papers=[
                ExternalSupportingPaperModel.model_validate(item) for item in supporting if isinstance(item, dict)
            ],
            unmapped_items=[item if isinstance(item, dict) else {"raw_item": str(item)} for item in unmapped],
        )

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
        stripped = text.strip()
        candidates = [stripped]
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S)
        candidates.extend(item.strip() for item in fenced if item.strip())
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None


# ---------------------------------------------------------------------------
# Shared helper functions (used by both API and MCP tools)
# ---------------------------------------------------------------------------


def _truncate(text: str | None, limit: int = 1200) -> str | None:
    """Truncate long text while preserving readability."""
    if not text:
        return text
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "\u2026"


def build_internal_ai_review_blob(detail) -> str:
    """Build a JSON string containing the full paper detail for AI review.

    Used by both the REST API ``internal-parse`` endpoint and the MCP
    ``review_paper`` tool.  ``detail`` is a ``PaperDetailResponse`` instance.
    """
    sections = []
    for item in detail.sections[:8]:
        sections.append(
            {
                "section_title": item.section_title,
                "section_type": item.section_type,
                "text_excerpt": _truncate(item.text, 1400),
            }
        )

    bundle = {
        "paper": {
            "id": str(detail.id),
            "title": detail.title,
            "doi": detail.doi,
            "year": detail.year,
            "journal": detail.journal,
            "authors": detail.authors,
            "abstract": _truncate(detail.abstract, 2200),
            "oa_status": detail.oa_status,
            "counts": detail.counts.model_dump(mode="json"),
        },
        "comprehensive_analysis": detail.comprehensive_analysis,
        "dft_settings_items": [item.model_dump(mode="json") for item in detail.dft_settings_items[:20]],
        "catalyst_samples_items": [item.model_dump(mode="json") for item in detail.catalyst_samples_items[:20]],
        "dft_results_items": [item.model_dump(mode="json") for item in detail.dft_results_items[:40]],
        "electrochemical_performance_items": [
            item.model_dump(mode="json") for item in detail.electrochemical_performance_items[:30]
        ],
        "mechanism_claims_items": [item.model_dump(mode="json") for item in detail.mechanism_claims_items[:30]],
        "writing_cards_items": [item.model_dump(mode="json") for item in detail.writing_cards_items[:20]],
        "references": [item.model_dump(mode="json") for item in detail.references[:40]],
        "outgoing_relationships": [item.model_dump(mode="json") for item in detail.outgoing_relationships[:20]],
        "incoming_relationships": [item.model_dump(mode="json") for item in detail.incoming_relationships[:20]],
        "section_excerpts": sections,
    }
    return json.dumps(bundle, ensure_ascii=False, indent=2)


def sanitize_internal_corrections(normalized: ExternalAnalysisNormalizedModel) -> ExternalAnalysisNormalizedModel:
    """Clean up correction target_path for top-level paper fields.

    If a correction targets one of the allowed top-level paper fields,
    force its ``target_path`` to equal the ``field_name`` so the review
    pipeline can apply it correctly.
    """
    # Lazy import to avoid circular dependency at module level
    from app.services.review_service import ReviewService

    corrected = []
    for item in normalized.correction_proposals:
        target_path = item.target_path
        if item.field_name in ReviewService.ALLOWED_PAPER_FIELDS:
            target_path = item.field_name
        corrected.append(item.model_copy(update={"target_path": target_path}))
    return normalized.model_copy(update={"correction_proposals": corrected})
