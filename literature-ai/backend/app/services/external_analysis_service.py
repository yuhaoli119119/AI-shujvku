from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperNote,
    PaperRelationship,
    PaperTable,
    WorkflowJob,
)
from app.services.dft_rescan_policy import build_dft_dedupe_signature, normalize_source_document_type
from app.services.llm_service import LLMService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_identity import PaperIdentityService
from app.services.review_service import ReviewService
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.evidence_anchors import has_evidence_anchor, has_material_correction_anchor
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.protocol_tracking import protocol_snapshot
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


class ExternalAuditOpinionModel(BaseModel):
    paper_id: str | None = None
    source: str | None = None
    verdict: str | None = None
    recommended_action: str | None = None
    suspected_missing: list[Any] = Field(default_factory=list)
    metadata_status: str | None = None
    section_structure_status: str | None = None
    table_status: str | None = None
    figure_status: str | None = None
    dft_status: str | None = None
    evidence_examples: list[Any] = Field(default_factory=list)
    raw_payload: dict[str, Any] | list[Any] | str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str = "candidate"
    verification_status: str = "unverified"
    mapping_reason: str | None = None


class ExternalObjectReviewAuditModel(BaseModel):
    paper_id: str | None = None
    target_type: str
    target_id: str
    field_name: str | None = None
    decision: str | None = None
    adjudication_role: str | None = None
    adjudication_scope: str | None = None
    selected_source_ids: list[str] = Field(default_factory=list)
    normalized_energy_type: str | None = None
    normalized_material: str | None = None
    structure_name: str | None = None
    adsorbate: str | None = None
    reaction_step: str | None = None
    evidence_checked: bool | None = None
    evidence_location: dict[str, Any] | list[Any] | str | None = None
    dedupe_signature: str | None = None
    borrowed_from_reference: bool = False
    supporting_evidence: list[Any] = Field(default_factory=list)
    blocking_errors: list[Any] = Field(default_factory=list)
    recommended_action: str | None = None
    corrected_value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None
    source_label: str | None = None
    agent_role: str | None = None
    model_name: str | None = None
    reason: str | None = None
    raw_payload: dict[str, Any] | list[Any] | str | None = None
    status: str = "candidate"
    verification_status: str = "unverified"
    writes_final_truth: bool = False
    human_confirmation_required: bool = True
    mapping_reason: str | None = None


class ExternalAnalysisNormalizedModel(BaseModel):
    review_notes: list[ExternalReviewNoteModel] = Field(default_factory=list)
    correction_proposals: list[ExternalCorrectionProposalModel] = Field(default_factory=list)
    supporting_papers: list[ExternalSupportingPaperModel] = Field(default_factory=list)
    external_audit_opinions: list[ExternalAuditOpinionModel] = Field(default_factory=list)
    object_review_audits: list[ExternalObjectReviewAuditModel] = Field(default_factory=list)
    unmapped_items: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class MaterializationResult:
    created_notes: int = 0
    created_corrections: int = 0
    created_relationships: int = 0
    auto_applied_corrections: int = 0
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
            source_paper=paper,
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
            normalized = self._with_paper_level_audit_opinion(
                normalized,
                raw_payload=sanitized_raw_payload,
                source=source,
                paper_id=paper_id,
            )
            self._reject_direct_tool_only_corrections(normalized)
            normalized_payload = normalized.model_dump(mode="json")
            external_audit_precondition = self._external_audit_precondition(paper) if normalized.external_audit_opinions else None
            if external_audit_precondition and external_audit_precondition["status"] != "ready":
                run.mapping_status = "artifact_precondition_failed"
                run.mapping_error = "artifact_precondition_failed:" + ",".join(
                    external_audit_precondition["blocking_errors"] or ["unknown"]
                )
                run.normalized_payload = {
                    **normalized_payload,
                    "external_audit_precondition": external_audit_precondition,
                }
            else:
                run.normalized_payload = normalized_payload
                self._create_candidates(run, normalized)

        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="import_external_analysis",
                source=source,
                target_type="external_analysis_run",
                target_id=str(run.id),
                payload={
                    "source_label": source_label,
                    "mapping_status": run.mapping_status,
                    "mapping_error": run.mapping_error,
                    "protocol": protocol_snapshot("gemini_audit_protocol"),
                    "writes_final_truth": False,
                    "requires_human_confirmation": True,
                },
            )
        )
        self._record_import_activity_job(
            paper=paper,
            run=run,
            normalized=normalized,
            source=source,
            source_label=source_label,
        )
        self.session.flush()
        self.session.refresh(run)
        return run

    def _record_import_activity_job(
        self,
        *,
        paper: Paper,
        run: ExternalAnalysisRun,
        normalized: ExternalAnalysisNormalizedModel | None,
        source: str,
        source_label: str | None,
    ) -> None:
        label = str(source_label or source or "ide_ai").strip() or "ide_ai"
        candidate_count = self._normalized_candidate_count(normalized)
        action = "import_analysis"
        title = f"IDE AI analysis imported: {label}"
        self.session.add(
            WorkflowJob(
                job_id=str(uuid4()),
                type="agent_activity",
                status="completed",
                library_name=normalize_library_name(paper.library_name),
                payload={
                    "agent": label,
                    "action": action,
                    "title": title,
                    "paper_id": str(paper.id),
                    "paper_code": paper.paper_code,
                    "paper_title": paper.title,
                    "source": source,
                    "source_label": source_label,
                    "external_analysis_run_id": str(run.id),
                },
                progress={
                    "phase": action,
                    "action": action,
                    "message": title,
                    "agent": label,
                    "paper_id": str(paper.id),
                    "paper_code": paper.paper_code,
                },
                result={
                    "metrics": {
                        "success_count": 1 if not run.mapping_error else 0,
                        "failure_count": 1 if run.mapping_error else 0,
                        "candidate_count": candidate_count,
                    },
                    "details": {
                        "mapping_status": run.mapping_status,
                        "mapping_error": run.mapping_error,
                        "paper_code": paper.paper_code,
                        "source": source,
                        "source_label": source_label,
                    },
                    "artifacts": [{"type": "external_analysis_run", "run_id": str(run.id)}],
                    "success_count": 1 if not run.mapping_error else 0,
                    "failure_count": 1 if run.mapping_error else 0,
                },
                runtime_context={},
            )
        )

    @staticmethod
    def _normalized_candidate_count(normalized: ExternalAnalysisNormalizedModel | None) -> int:
        if normalized is None:
            return 0
        return (
            len(normalized.external_audit_opinions)
            + len(normalized.object_review_audits)
            + len(normalized.review_notes)
            + len(normalized.correction_proposals)
            + len(normalized.supporting_papers)
            + len(normalized.unmapped_items)
        )

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

    def delete_run(self, run_id: UUID) -> ExternalAnalysisRun:
        run = self.get_run(run_id)
        self.session.execute(
            delete(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id)
        )
        self.session.delete(run)
        self.session.flush()
        return run

    def delete_runs_for_paper_source(self, paper_id: UUID, source: str) -> int:
        run_ids = self.session.scalars(
            select(ExternalAnalysisRun.id).where(
                ExternalAnalysisRun.paper_id == paper_id,
                ExternalAnalysisRun.source == source,
            )
        ).all()
        if not run_ids:
            return 0
        self.session.execute(
            delete(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id.in_(run_ids))
        )
        self.session.execute(delete(ExternalAnalysisRun).where(ExternalAnalysisRun.id.in_(run_ids)))
        self.session.flush()
        return len(run_ids)

    def list_candidates(self, run_id: UUID) -> list[ExternalAnalysisCandidate]:
        return self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.run_id == run_id)
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()

    def backfill_paper_level_audit_candidates(self, *, source: str | None = None, limit: int | None = None) -> int:
        """Create missing external_audit_opinion candidates for already-imported paper-level audit runs."""
        stmt = select(ExternalAnalysisRun).order_by(ExternalAnalysisRun.created_at.desc())
        if source:
            stmt = stmt.where(ExternalAnalysisRun.source == source)
        if limit:
            stmt = stmt.limit(limit)
        runs = self.session.scalars(stmt).all()
        created = 0
        for run in runs:
            existing_count = self.session.scalar(
                select(ExternalAnalysisCandidate.id)
                .where(
                    ExternalAnalysisCandidate.run_id == run.id,
                    ExternalAnalysisCandidate.candidate_type == "external_audit_opinion",
                )
                .limit(1)
            )
            if existing_count is not None:
                continue
            opinion = self._paper_level_audit_opinion(
                raw_payload=run.raw_payload,
                source=run.source,
                paper_id=run.paper_id,
            )
            if opinion is None:
                continue
            normalized = self._normalized_from_run(run)
            normalized.external_audit_opinions.append(opinion)
            run.normalized_payload = normalized.model_dump(mode="json")
            self.session.add(run)
            self._create_external_audit_candidate(run, opinion)
            created += 1
        self.session.flush()
        return created

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
                if payload.get("field_name") == "catalyst_samples" and not has_material_correction_anchor(
                    payload.get("evidence_payload")
                ):
                    candidate.status = "requires_resolution"
                    self.session.add(candidate)
                    result.skipped_candidates += 1
                    continue
                if (
                    payload.get("field_name") == "catalyst_samples"
                    and payload.get("operation") == "create"
                    and (
                        payload.get("target_path") != "catalyst_samples:new:create"
                        or not isinstance(payload.get("proposed_value"), dict)
                    )
                ):
                    candidate.status = "requires_resolution"
                    self.session.add(candidate)
                    result.skipped_candidates += 1
                    continue
                correction = PaperCorrection(
                    paper_id=candidate.paper_id,
                    source=run.source,
                    field_name=payload.get("field_name", ""),
                    target_path=payload.get("target_path", ""),
                    operation=payload.get("operation", "replace"),
                    proposed_value=payload.get("proposed_value"),
                    reason=payload.get("reason", ""),
                    evidence_payload=self._external_candidate_evidence_payload(
                        run,
                        payload.get("evidence_payload"),
                    ),
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
                    "source_run_id": str(run.id),
                    "protocol": protocol_snapshot("gemini_audit_protocol"),
                    "writes_final_truth": False,
                    "requires_human_confirmation": True,
                },
            )
        )
        self.session.flush()
        return result

    def auto_apply_non_dft_review_outputs(
        self,
        run_id: UUID,
        *,
        reviewer: str = "ide_ai",
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> MaterializationResult:
        """Materialize IDE AI outputs that are safe outside the DFT review lane.

        Non-DFT notes and corrections are operational outputs: notes mark a module as
        IDE-reviewed, and eligible corrections are immediately approved/applied so
        RAG can use the cleaned records. DFT results/settings stay out of this path
        and must go through the dedicated review/export workflow.
        """

        run = self.get_run(run_id)
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.run_id == run.id)
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        required_modules = self._required_auto_apply_modules(candidates)
        lock_check = ModuleWriteLockService(self.session).require_write(
            paper_id=run.paper_id,
            module_names=required_modules,
            lock_tokens=write_lock_tokens,
            locked_by=write_lock_owner or reviewer,
        )
        result = MaterializationResult()
        review_service = ReviewService(self.session)

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
                candidate.status = "ai_reviewed"
                candidate.materialized_target_type = "paper_note"
                candidate.materialized_target_id = str(note.id)
                result.created_notes += 1
                self.session.add(candidate)
                continue

            if candidate.candidate_type == "correction":
                if not self._is_auto_applicable_non_dft_correction(payload):
                    candidate.status = "requires_resolution"
                    self.session.add(candidate)
                    result.skipped_candidates += 1
                    continue
                correction = PaperCorrection(
                    paper_id=candidate.paper_id,
                    source=run.source,
                    field_name=payload.get("field_name", ""),
                    target_path=payload.get("target_path", ""),
                    operation=payload.get("operation", "replace"),
                    proposed_value=payload.get("proposed_value"),
                    reason=payload.get("reason", ""),
                    evidence_payload=self._external_candidate_evidence_payload(
                        run,
                        payload.get("evidence_payload"),
                    ),
                    status="pending",
                )
                self.session.add(correction)
                self.session.flush()
                try:
                    review_service.approve_correction(
                        correction.id,
                        reviewer,
                        write_lock_tokens=write_lock_tokens,
                        write_lock_owner=write_lock_owner,
                    )
                except Exception as exc:
                    correction.status = "requires_resolution"
                    correction.reviewed_by = reviewer
                    correction.reviewed_at = None
                    candidate.status = "requires_resolution"
                    candidate.mapping_reason = str(exc)
                    self.session.add(correction)
                    self.session.add(candidate)
                    result.skipped_candidates += 1
                    continue
                candidate.status = "ai_applied"
                candidate.materialized_target_type = "paper_correction"
                candidate.materialized_target_id = str(correction.id)
                result.created_corrections += 1
                result.auto_applied_corrections += 1
                self.session.add(candidate)
                continue

            if candidate.candidate_type == "relationship":
                target_paper_id = payload.get("target_paper_id")
                if not target_paper_id:
                    candidate.status = "requires_resolution"
                    result.skipped_candidates += 1
                    self.session.add(candidate)
                    continue
                relationship = PaperRelationship(
                    source_paper_id=candidate.paper_id,
                    target_paper_id=UUID(str(target_paper_id)),
                    relationship_type=payload.get("relationship_type", "supports"),
                    note=payload.get("note"),
                    created_by=reviewer,
                )
                self.session.add(relationship)
                self.session.flush()
                candidate.status = "ai_applied"
                candidate.materialized_target_type = "paper_relationship"
                candidate.materialized_target_id = str(relationship.id)
                result.created_relationships += 1
                self.session.add(candidate)
                continue

            result.skipped_candidates += 1

        self.session.add(
            AuditLog(
                paper_id=run.paper_id,
                action="auto_apply_non_dft_external_analysis",
                source=reviewer,
                target_type="external_analysis_run",
                target_id=str(run.id),
                payload={
                    "created_notes": result.created_notes,
                    "created_corrections": result.created_corrections,
                    "created_relationships": result.created_relationships,
                    "auto_applied_corrections": result.auto_applied_corrections,
                    "skipped_candidates": result.skipped_candidates,
                    "source_run_id": str(run.id),
                    "protocol": protocol_snapshot("ide_ai_non_dft_auto_apply"),
                    "writes_final_truth": True,
                    "requires_human_confirmation": False,
                    "dft_outputs_excluded": True,
                    "write_lock": {
                        "required_modules": lock_check.required_modules,
                        "covered_modules": lock_check.covered_modules,
                        "lock_ids": lock_check.lock_ids,
                    },
                },
            )
        )
        self.session.flush()
        return result

    def _required_auto_apply_modules(self, candidates: list[ExternalAnalysisCandidate]) -> list[str]:
        modules: set[str] = set()
        for candidate in candidates:
            if candidate.status not in {"pending", "requires_resolution"}:
                continue
            payload = candidate.normalized_payload or {}
            if candidate.candidate_type == "note":
                modules.add("notes")
            elif candidate.candidate_type == "correction":
                if self._is_auto_applicable_non_dft_correction(payload):
                    modules.add(
                        ModuleWriteLockService.module_from_field(
                            payload.get("field_name"),
                            payload.get("target_path"),
                        )
                    )
            elif candidate.candidate_type == "relationship" and payload.get("target_paper_id"):
                modules.add("relationships")
        return sorted(modules)

    @staticmethod
    def _is_auto_applicable_non_dft_correction(payload: dict[str, Any]) -> bool:
        field_name = str(payload.get("field_name") or "").strip()
        target_path = str(payload.get("target_path") or "").strip()
        operation = str(payload.get("operation") or "replace").strip().lower()
        if operation not in {"replace", "create", "delete"}:
            return False
        denied_fields = {
            "dft_results",
            "dft_result",
            "dft_settings",
            "dft_setting",
        }
        if field_name in denied_fields:
            return False
        if target_path.split(":", 1)[0] in denied_fields:
            return False
        allowed_top_level = ReviewService.ALLOWED_PAPER_FIELDS
        allowed_structured = {
            "figures",
            "tables",
            "sections",
            "writing_cards",
            "mechanism_claims",
            "electrochemical_performance",
            "catalyst_samples",
        }
        evidence_payload = payload.get("evidence_payload")
        if field_name == "catalyst_samples" and not has_material_correction_anchor(evidence_payload):
            return False
        if operation == "create":
            return (
                field_name in allowed_structured
                and target_path == f"{field_name}:new:create"
                and isinstance(payload.get("proposed_value"), dict)
                and has_evidence_anchor(evidence_payload)
            )
        if operation == "delete":
            parts = [part.strip() for part in target_path.split(":")]
            return (
                field_name == "figures"
                and len(parts) == 3
                and parts[0] == "figures"
                and parts[1]
                and parts[2] == "delete"
                and has_evidence_anchor(evidence_payload)
            )
        if field_name in allowed_top_level and target_path in {field_name, ""}:
            return True
        if field_name in allowed_structured and target_path.startswith(field_name + ":"):
            if field_name in {"mechanism_claims", "electrochemical_performance"} and not has_evidence_anchor(
                evidence_payload
            ):
                return False
            return True
        return False

    @staticmethod
    def _external_candidate_evidence_payload(
        run: ExternalAnalysisRun,
        raw_payload: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any]
        if isinstance(raw_payload, dict):
            payload = dict(raw_payload)
        elif raw_payload is None:
            payload = {}
        else:
            payload = {"external_evidence_payload": raw_payload}
        payload.update(
            {
                "source_external_analysis_run_id": str(run.id),
                "source": run.source,
                "source_label": run.source_label,
                "protocol": protocol_snapshot("gemini_audit_protocol"),
                "writes_final_truth": False,
                "requires_human_confirmation": True,
            }
        )
        return payload

    @staticmethod
    def _correction_candidate_status(correction: ExternalCorrectionProposalModel) -> str:
        if correction.field_name == "catalyst_samples" and not has_material_correction_anchor(
            correction.evidence_payload
        ):
            return "requires_resolution"
        if correction.field_name == "catalyst_samples" and correction.operation == "create":
            if correction.target_path != "catalyst_samples:new:create" or not isinstance(correction.proposed_value, dict):
                return "requires_resolution"
        if correction.operation == "create" and correction.field_name in ReviewService.STRUCTURED_CREATE_TARGETS:
            if (
                correction.target_path != f"{correction.field_name}:new:create"
                or not isinstance(correction.proposed_value, dict)
                or not has_evidence_anchor(correction.evidence_payload)
            ):
                return "requires_resolution"
        return "pending"

    @staticmethod
    def _reject_direct_tool_only_corrections(normalized: ExternalAnalysisNormalizedModel) -> None:
        direct_tool_ops = {
            "recrop_figure": "recrop_figure",
            "create_figure_from_bbox": "create_figure_from_bbox",
        }
        blocked: list[str] = []
        for correction in normalized.correction_proposals:
            operation = str(correction.operation or "").strip().lower()
            if operation in direct_tool_ops:
                blocked.append(operation)
        if blocked:
            tools = ", ".join(sorted(set(blocked)))
            raise ValueError(
                "direct_mcp_tool_required:"
                f"{tools} must be called directly through MCP and must not be submitted through import_analysis. "
                "Do the crop/create operation now with the real tool, then read back the figure image_path/crop_status."
            )

    def _normalize_input(
        self,
        raw_text: str | None,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source_paper: Paper | None = None,
    ) -> tuple[ExternalAnalysisNormalizedModel | None, str, str | None]:
        parsed = self._extract_structured_payload(raw_text=raw_text, raw_payload=raw_payload)
        if isinstance(parsed, dict):
            try:
                normalized = ExternalAnalysisNormalizedModel.model_validate(parsed)
                return self._post_process_normalized(normalized, parsed, source_paper=source_paper), "normalized", None
            except Exception:
                llm_normalized = self._llm_normalize(raw_text=raw_text, raw_payload=parsed)
                if llm_normalized:
                    return self._post_process_normalized(llm_normalized, parsed, source_paper=source_paper), "normalized_with_llm", None
                return self._post_process_normalized(
                    self._heuristic_normalize(parsed),
                    source_paper=source_paper,
                ), "heuristic", None

        if isinstance(parsed, list):
            return self._post_process_normalized(
                self._heuristic_normalize({"unmapped_items": parsed}),
                source_paper=source_paper,
            ), "heuristic", None

        if isinstance(parsed, str) and parsed.strip():
            llm_normalized = self._llm_normalize(raw_text=parsed, raw_payload=None)
            if llm_normalized:
                return self._post_process_normalized(llm_normalized, source_paper=source_paper), "normalized_with_llm", None
            return ExternalAnalysisNormalizedModel(
                review_notes=[ExternalReviewNoteModel(content=parsed, mapping_reason="Fallback free-text note import")]
            ), "free_text_fallback", None

        return ExternalAnalysisNormalizedModel(), "empty", None

    def _post_process_normalized(
        self,
        normalized: ExternalAnalysisNormalizedModel,
        raw_payload: dict[str, Any] | None = None,
        source_paper: Paper | None = None,
    ) -> ExternalAnalysisNormalizedModel:
        supporting = []
        for item in normalized.supporting_papers:
            resolved_target, resolution_reason = self._resolve_target_paper_id(item, source_paper=source_paper)
            mapping_reason = item.mapping_reason
            if resolution_reason and not resolved_target:
                mapping_reason = resolution_reason
            supporting.append(
                item.model_copy(
                    update={
                        "target_paper_id": resolved_target or item.target_paper_id,
                        "mapping_reason": mapping_reason,
                    }
                )
            )
        object_reviews = list(normalized.object_review_audits)
        if raw_payload is not None:
            existing_keys = {
                self._object_review_key(item.model_dump(mode="json")): index
                for index, item in enumerate(object_reviews)
            }
            for item in self._extract_object_review_audits(raw_payload):
                key = self._object_review_key(item)
                if key in existing_keys:
                    index = existing_keys[key]
                    if object_reviews[index].raw_payload is None:
                        object_reviews[index] = object_reviews[index].model_copy(update={"raw_payload": item})
                    continue
                object_reviews.append(ExternalObjectReviewAuditModel.model_validate(item))
                existing_keys[key] = len(object_reviews) - 1
        corrections = self._normalize_legacy_codex_item_corrections(
            normalized.correction_proposals,
            source_paper=source_paper,
        )
        return normalized.model_copy(
            update={
                "supporting_papers": supporting,
                "object_review_audits": object_reviews,
                "correction_proposals": corrections,
            }
        )

    def _normalize_legacy_codex_item_corrections(
        self,
        corrections: list[ExternalCorrectionProposalModel],
        *,
        source_paper: Paper | None,
    ) -> list[ExternalCorrectionProposalModel]:
        if not corrections or source_paper is None:
            return corrections
        normalized: list[ExternalCorrectionProposalModel] = []
        for correction in corrections:
            target_path = str(correction.target_path or "").strip()
            field_name = str(correction.field_name or "").strip()
            if not target_path.lower().startswith("codex_item:"):
                normalized.append(correction)
                continue
            item_id = target_path.split(":", 1)[1].strip()
            mapped = self._resolve_legacy_codex_item_target(
                paper_id=source_paper.id,
                item_id=item_id,
                field_name=field_name,
            )
            if mapped is None:
                normalized.append(correction)
                continue
            collection, attribute = mapped
            normalized.append(
                correction.model_copy(
                    update={
                        "field_name": collection,
                        "target_path": f"{collection}:{item_id}:{attribute}",
                        "mapping_reason": (
                            correction.mapping_reason
                            or f"Normalized legacy codex_item target to structured {collection} correction."
                        ),
                    }
                )
            )
        return normalized

    def _resolve_legacy_codex_item_target(
        self,
        *,
        paper_id: UUID,
        item_id: str,
        field_name: str,
    ) -> tuple[str, str] | None:
        normalized_field = str(field_name or "").strip().lower()
        try:
            item_uuid = UUID(item_id)
        except (TypeError, ValueError):
            return None
        table = self.session.get(PaperTable, item_uuid)
        if table is not None and table.paper_id == paper_id:
            table_fields = {"caption", "markdown_content", "page", "extraction_source", "prov"}
            return ("tables", normalized_field if normalized_field in table_fields else "markdown_content")
        figure = self.session.get(PaperFigure, item_uuid)
        if figure is not None and figure.paper_id == paper_id:
            figure_fields = {
                "caption",
                "image_path",
                "page",
                "figure_label",
                "figure_role",
                "role_confidence",
                "content_summary",
                "key_elements",
                "prov",
                "crop_status",
                "crop_confidence",
                "crop_source",
            }
            return ("figures", normalized_field if normalized_field in figure_fields else "content_summary")
        return None

    def _external_audit_precondition(self, paper: Paper) -> dict[str, Any]:
        artifact_status = build_paper_artifact_status(paper, settings=self.settings)
        blocking_errors = list(artifact_status.get("blocking_errors") or [])
        return {
            "status": "ready" if artifact_status.get("artifact_ready_for_external_audit") else "artifact_precondition_failed",
            "blocking_errors": blocking_errors,
            "artifact_ready_for_external_audit": bool(artifact_status.get("artifact_ready_for_external_audit")),
        }

    def _create_candidates(self, run: ExternalAnalysisRun, normalized: ExternalAnalysisNormalizedModel) -> None:
        for opinion in normalized.external_audit_opinions:
            self._create_external_audit_candidate(run, opinion)
        for audit in normalized.object_review_audits:
            self._create_object_review_candidate(run, audit)
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
            status = self._correction_candidate_status(correction)
            mapping_reason = correction.mapping_reason
            if (
                correction.field_name == "catalyst_samples"
                and status == "requires_resolution"
                and "evidence anchor" not in str(mapping_reason or "").lower()
            ):
                mapping_reason = (
                    "Catalyst sample corrections require at least one PDF evidence anchor: "
                    "page, section, quoted_text, table, or figure."
                )
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="correction",
                    normalized_payload=correction.model_dump(mode="json"),
                    confidence=correction.confidence,
                    mapping_reason=mapping_reason,
                    evidence_payload=correction.evidence_payload,
                    status=status,
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

    def _create_object_review_candidate(self, run: ExternalAnalysisRun, audit: ExternalObjectReviewAuditModel) -> None:
        payload = audit.model_dump(mode="json")
        payload.update(
            {
                "paper_id": str(run.paper_id),
                "run_id": str(run.id),
                "source": audit.source or run.source,
                "source_label": audit.source_label or run.source_label,
                "candidate_type": "object_review_audit",
                "status": "candidate",
                "verification_status": "unverified",
                "writes_final_truth": False,
                "human_confirmation_required": True,
            }
        )
        evidence_payload = {
            "source": payload.get("source"),
            "source_label": payload.get("source_label"),
            "target_type": payload.get("target_type"),
            "target_id": payload.get("target_id"),
            "field_name": payload.get("field_name"),
            "decision": payload.get("decision"),
            "evidence_checked": payload.get("evidence_checked"),
            "evidence_location": payload.get("evidence_location"),
            "source_document_type": normalize_source_document_type(
                (payload.get("evidence_location") or {}).get("source_document_type")
                if isinstance(payload.get("evidence_location"), dict)
                else None
            ),
            "dedupe_signature": payload.get("dedupe_signature"),
            "supporting_evidence": payload.get("supporting_evidence") or [],
            "borrowed_from_reference": bool(payload.get("borrowed_from_reference")),
            "blocking_errors": payload.get("blocking_errors") or [],
            "recommended_action": payload.get("recommended_action"),
            "verification_status": "unverified",
            "raw_payload": payload.get("raw_payload"),
            "protocol": protocol_snapshot("gemini_audit_protocol"),
            "writes_final_truth": False,
            "human_confirmation_required": True,
        }
        self.session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=run.paper_id,
                candidate_type="object_review_audit",
                normalized_payload=payload,
                confidence=audit.confidence,
                mapping_reason=audit.mapping_reason or "Imported object-level external review audit candidate",
                evidence_payload=evidence_payload,
                status="candidate",
            )
        )

    def _create_external_audit_candidate(self, run: ExternalAnalysisRun, opinion: ExternalAuditOpinionModel) -> None:
        payload = opinion.model_dump(mode="json")
        payload.update(
            {
                "paper_id": str(run.paper_id),
                "run_id": str(run.id),
                "source": run.source,
                "source_label": run.source_label,
                "candidate_type": "external_audit_opinion",
                "status": "candidate",
                "verification_status": "unverified",
                "writes_final_truth": False,
                "requires_human_confirmation": True,
            }
        )
        evidence_payload = {
            "source": run.source,
            "source_label": run.source_label,
            "verdict": payload.get("verdict"),
            "recommended_action": payload.get("recommended_action"),
            "suspected_missing": payload.get("suspected_missing") or [],
            "evidence_examples": payload.get("evidence_examples") or [],
            "verification_status": "unverified",
            "raw_payload": payload.get("raw_payload"),
            "protocol": protocol_snapshot("gemini_audit_protocol"),
            "writes_final_truth": False,
            "requires_human_confirmation": True,
        }
        self.session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=run.paper_id,
                candidate_type="external_audit_opinion",
                normalized_payload=payload,
                confidence=opinion.confidence,
                mapping_reason=opinion.mapping_reason or "Imported paper-level external audit opinion",
                evidence_payload=evidence_payload,
                status="candidate",
            )
        )

    def _with_paper_level_audit_opinion(
        self,
        normalized: ExternalAnalysisNormalizedModel,
        *,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source: str,
        paper_id: UUID,
    ) -> ExternalAnalysisNormalizedModel:
        if normalized.external_audit_opinions:
            return normalized
        opinion = self._paper_level_audit_opinion(raw_payload=raw_payload, source=source, paper_id=paper_id)
        if opinion is None:
            return normalized
        return normalized.model_copy(update={"external_audit_opinions": [opinion]})

    @staticmethod
    def _normalized_from_run(run: ExternalAnalysisRun) -> ExternalAnalysisNormalizedModel:
        if isinstance(run.normalized_payload, dict):
            try:
                return ExternalAnalysisNormalizedModel.model_validate(run.normalized_payload)
            except Exception:
                pass
        return ExternalAnalysisNormalizedModel()

    @staticmethod
    def _paper_level_audit_opinion(
        *,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source: str,
        paper_id: UUID,
    ) -> ExternalAuditOpinionModel | None:
        if not isinstance(raw_payload, dict):
            return None
        if isinstance(raw_payload.get("candidates"), list) and raw_payload.get("candidates"):
            return None
        if ExternalAnalysisService._extract_object_review_audits(raw_payload):
            return None
        audit_keys = {
            "verdict",
            "recommended_action",
            "suspected_missing",
            "metadata_status",
            "section_structure_status",
            "table_status",
            "figure_status",
            "dft_status",
            "evidence_examples",
        }
        if not any(key in raw_payload for key in audit_keys):
            return None
        suspected_missing = raw_payload.get("suspected_missing") or raw_payload.get("missing_items") or []
        if isinstance(suspected_missing, (str, int, float, bool)):
            suspected_missing = [suspected_missing]
        evidence_examples = raw_payload.get("evidence_examples") or raw_payload.get("evidence") or []
        if isinstance(evidence_examples, (str, int, float, bool, dict)):
            evidence_examples = [evidence_examples]
        confidence = raw_payload.get("confidence")
        try:
            confidence = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        return ExternalAuditOpinionModel(
            paper_id=str(raw_payload.get("paper_id") or paper_id),
            source=source,
            verdict=str(raw_payload.get("verdict") or "").strip().upper() or None,
            recommended_action=raw_payload.get("recommended_action"),
            suspected_missing=list(suspected_missing) if isinstance(suspected_missing, list) else [],
            metadata_status=raw_payload.get("metadata_status"),
            section_structure_status=raw_payload.get("section_structure_status"),
            table_status=raw_payload.get("table_status"),
            figure_status=raw_payload.get("figure_status"),
            dft_status=raw_payload.get("dft_status"),
            evidence_examples=list(evidence_examples) if isinstance(evidence_examples, list) else [],
            raw_payload=raw_payload,
            confidence=confidence,
            mapping_reason="Paper-level external audit payload imported as candidate opinion",
        )

    def _resolve_target_paper_id(
        self,
        relationship: ExternalSupportingPaperModel,
        *,
        source_paper: Paper | None = None,
    ) -> tuple[str | None, str | None]:
        if relationship.target_paper_id:
            return relationship.target_paper_id, None

        conditions = []
        if relationship.target_doi:
            normalized_doi = PaperIdentityService.normalize_doi(relationship.target_doi)
            if normalized_doi:
                conditions.append(Paper.doi == normalized_doi)
        if relationship.target_title:
            conditions.append(Paper.title.ilike(relationship.target_title))
        if not conditions:
            return None, None

        stmt = select(Paper).where(or_(*conditions))
        if source_paper is not None:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, source_paper.library_name))
            stmt = stmt.where(Paper.id != source_paper.id)
        stmt = stmt.order_by(Paper.created_at.asc(), Paper.id.asc()).limit(2)
        targets = self.session.scalars(stmt).all()
        if len(targets) == 1:
            return str(targets[0].id), None
        if len(targets) > 1:
            return None, "Relationship target is ambiguous within the source paper library; keep target_paper_id unresolved."
        return None, None

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
        object_reviews = self._extract_object_review_audits(payload)
        unmapped = payload.get("unmapped_items") or []
        candidate_items = payload.get("candidates")
        if isinstance(candidate_items, dict):
            candidate_items = [candidate_items]
        if isinstance(candidate_items, list):
            notes = list(notes) if isinstance(notes, list) else [notes]
            corrections = list(corrections) if isinstance(corrections, list) else [corrections]
            supporting = list(supporting) if isinstance(supporting, list) else [supporting]
            unmapped = list(unmapped) if isinstance(unmapped, list) else [unmapped]
            for item in candidate_items:
                if not isinstance(item, dict):
                    unmapped.append({"raw_item": str(item), "mapping_reason": "Unsupported candidates item"})
                    continue
                kind = str(item.get("candidate_type") or item.get("type") or "").strip().lower()
                if kind in {"paper_note", "note", "review_note"}:
                    notes.append(
                        {
                            "content": item.get("content") or item.get("summary") or item.get("reason") or "",
                            "field_name": item.get("field_name"),
                            "page": item.get("page"),
                            "section_title": item.get("section_title"),
                            "quoted_text": item.get("quoted_text"),
                            "confidence": item.get("confidence"),
                            "mapping_reason": item.get("mapping_reason") or "Mapped from raw_payload.candidates",
                        }
                    )
                elif kind in {"correction", "correction_proposal"}:
                    corrections.append(item)
                elif kind in {"relationship", "supporting_paper"}:
                    supporting.append(item)
                elif kind in {"object_review_audit", "object_review", "field_review"} and self._is_object_review_item(item):
                    object_reviews.append(self._normalize_object_review_item(item))
                else:
                    unmapped.append({"raw_payload": item, "mapping_reason": "Unrecognized raw_payload.candidates item"})

        if not any([notes, corrections, supporting, object_reviews, unmapped]):
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
            object_review_audits=[
                ExternalObjectReviewAuditModel.model_validate(item) for item in object_reviews if isinstance(item, dict)
            ],
            unmapped_items=[item if isinstance(item, dict) else {"raw_item": str(item)} for item in unmapped],
        )

    @staticmethod
    def _extract_object_review_audits(payload: dict[str, Any]) -> list[dict[str, Any]]:
        explicit = payload.get("object_review_audits") or payload.get("object_reviews") or payload.get("field_reviews")
        if isinstance(explicit, dict):
            explicit = [explicit]
        if isinstance(explicit, list):
            return [
                ExternalAnalysisService._normalize_object_review_item(item)
                for item in explicit
                if isinstance(item, dict) and ExternalAnalysisService._is_object_review_item(item)
            ]

        candidates: list[dict[str, Any]] = []
        for key in ("reviews", "audits", "opinions", "items"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and ExternalAnalysisService._is_object_review_item(item):
                    candidates.append(ExternalAnalysisService._normalize_object_review_item(item))
        if ExternalAnalysisService._is_object_review_item(payload):
            candidates.append(ExternalAnalysisService._normalize_object_review_item(payload))
        return candidates

    @staticmethod
    def _is_object_review_item(item: dict[str, Any]) -> bool:
        decision = str(item.get("decision") or item.get("verdict") or "").strip().lower()
        target_type = str(item.get("target_type") or "").strip().lower()
        if target_type in {"dft_results", "dft_result"} and decision == "new_candidate" and item.get("corrected_value"):
            return True
        return bool(
            (item.get("target_type") or item.get("target_path"))
            and (item.get("target_id") or item.get("target_path") or item.get("dft_result_id") or item.get("record_id"))
            and (item.get("field_name") or item.get("target_path") or item.get("field"))
            and any(key in item for key in ("decision", "verdict", "recommended_action", "corrected_value", "proposed_value", "evidence_checked"))
        )

    @staticmethod
    def _normalize_object_review_item(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        target_path = normalized.get("target_path")
        if isinstance(target_path, str):
            match = re.match(r"^([^:]+):([^:]+):([^:]+)$", target_path)
            if match:
                normalized.setdefault("target_type", match.group(1))
                normalized.setdefault("target_id", match.group(2))
                normalized.setdefault("field_name", match.group(3))
        if "field_name" not in normalized and "field" in normalized:
            normalized["field_name"] = normalized.get("field")
        if "target_id" not in normalized:
            normalized["target_id"] = normalized.get("dft_result_id") or normalized.get("record_id")
        decision_text = str(normalized.get("decision") or normalized.get("verdict") or "").strip().lower()
        target_type_text = str(normalized.get("target_type") or "").strip().lower()
        if not normalized.get("target_id") and target_type_text in {"dft_results", "dft_result"} and decision_text == "new_candidate":
            normalized["target_id"] = "new"
        if "field_name" not in normalized and target_type_text in {"dft_results", "dft_result"} and decision_text == "new_candidate":
            normalized["field_name"] = "dft_results"
        if "decision" not in normalized and "verdict" in normalized:
            normalized["decision"] = normalized.get("verdict")
        evidence_location = normalized.get("evidence_location")
        corrected_value = normalized.get("corrected_value") or normalized.get("proposed_value")
        if isinstance(evidence_location, dict):
            evidence_location = dict(evidence_location)
            evidence_location["source_document_type"] = normalize_source_document_type(
                evidence_location.get("source_document_type")
            )
            normalized["evidence_location"] = evidence_location
        if target_type_text in {"dft_results", "dft_result"}:
            source_type = normalize_source_document_type(
                (evidence_location or {}).get("source_document_type") if isinstance(evidence_location, dict) else None
            )
            if source_type == "supporting_reference":
                normalized["borrowed_from_reference"] = True
            signature_payload = {
                **normalized,
                "corrected_value": corrected_value if isinstance(corrected_value, dict) else {},
                "evidence_location": evidence_location if isinstance(evidence_location, dict) else {},
            }
            normalized["dedupe_signature"] = normalized.get("dedupe_signature") or build_dft_dedupe_signature(
                signature_payload
            )
        if "corrected_value" not in normalized and "proposed_value" in normalized:
            normalized["corrected_value"] = normalized.get("proposed_value")
        if "reason" not in normalized:
            normalized["reason"] = normalized.get("reviewer_note") or normalized.get("mapping_reason")
        blocking = normalized.get("blocking_errors") or normalized.get("blocking_error") or []
        if isinstance(blocking, (str, int, float, bool)):
            blocking = [blocking]
        normalized["blocking_errors"] = blocking if isinstance(blocking, list) else []
        normalized["writes_final_truth"] = False
        normalized["human_confirmation_required"] = True
        normalized["verification_status"] = "unverified"
        normalized["status"] = "candidate"
        normalized.setdefault("raw_payload", item)
        return normalized

    @staticmethod
    def _object_review_key(item: dict[str, Any]) -> str:
        return "|".join(
            str(item.get(key) or "")
            for key in ("paper_id", "target_type", "target_id", "field_name", "decision", "corrected_value")
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
    figures = []
    for item in detail.figures[:30]:
        figures.append(
            {
                "id": str(item.id),
                "caption": item.caption,
                "page": item.page,
                "figure_role": item.figure_role,
                "role_confidence": item.role_confidence,
                "content_summary": item.content_summary,
                "key_elements": item.key_elements,
                "has_image_crop": bool(item.image_path),
            }
        )
    tables = []
    for item in detail.tables[:20]:
        tables.append(
            {
                "id": str(item.id),
                "caption": item.caption,
                "page": item.page,
                "extraction_source": item.extraction_source,
                "markdown_excerpt": _truncate(item.markdown_content, 1600),
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
            "artifact_status": detail.artifact_status.model_dump(mode="json")
            if hasattr(detail.artifact_status, "model_dump")
            else detail.artifact_status,
        },
        "source_assets": {
            "pdf_url": f"/api/papers/{detail.id}/pdf",
            "pdf_path": detail.pdf_path,
            "workspace_path": detail.workspace_path,
        },
        "external_audit_precondition": {
            "status": "ready"
            if getattr(detail.artifact_status, "artifact_ready_for_external_audit", False)
            else "artifact_precondition_failed",
            "blocking_errors": list(getattr(detail.artifact_status, "blocking_errors", []) or []),
        },
        "comprehensive_analysis": detail.comprehensive_analysis,
        "dft_settings_items": [item.model_dump(mode="json") for item in detail.dft_settings_items[:20]],
        "catalyst_samples_items": [
            {
                **item.model_dump(mode="json"),
                "dependent_dft_count": sum(
                    1
                    for row in detail.dft_results_items
                    if str(row.catalyst_sample_id) == str(item.id)
                    or (len(detail.catalyst_samples_items) == 1 and not row.catalyst_sample_id)
                ),
                "single_sample_paper": len(detail.catalyst_samples_items) == 1,
            }
            for item in detail.catalyst_samples_items[:20]
        ],
        "dft_results_items": [item.model_dump(mode="json") for item in detail.dft_results_items[:40]],
        "electrochemical_performance_items": [
            item.model_dump(mode="json") for item in detail.electrochemical_performance_items[:30]
        ],
        "mechanism_claims_items": [item.model_dump(mode="json") for item in detail.mechanism_claims_items[:30]],
        "writing_cards_items": [item.model_dump(mode="json") for item in detail.writing_cards_items[:20]],
        "figures": figures,
        "tables": tables,
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
