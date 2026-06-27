from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    WorkflowJob,
)
from app.services.external_analysis_candidates import ExternalAnalysisCandidatePersistenceMixin
from app.services.external_analysis_materialization import ExternalAnalysisMaterializationMixin
from app.services.external_analysis_models import (
    ExternalAnalysisNormalizedModel,
    ExternalAuditOpinionModel,
    ExternalCorrectionProposalModel,
    ExternalObjectReviewAuditModel,
    ExternalReviewNoteModel,
    ExternalSupportingPaperModel,
    MaterializationResult,
)
from app.services.external_analysis_normalization import ExternalAnalysisNormalizationMixin
from app.services.llm_service import LLMService
from app.utils.library_names import normalize_library_name
from app.utils.protocol_tracking import protocol_snapshot
from app.utils.text_cleaning import normalize_text_tree, repair_mojibake_text


__all__ = [
    "ExternalAnalysisNormalizedModel",
    "ExternalAnalysisService",
    "ExternalAuditOpinionModel",
    "ExternalCorrectionProposalModel",
    "ExternalObjectReviewAuditModel",
    "ExternalReviewNoteModel",
    "ExternalSupportingPaperModel",
    "MaterializationResult",
    "build_internal_ai_review_blob",
    "sanitize_internal_corrections",
]


class ExternalAnalysisService(
    ExternalAnalysisMaterializationMixin,
    ExternalAnalysisCandidatePersistenceMixin,
    ExternalAnalysisNormalizationMixin,
):
    COUNTABLE_DFT_REVIEW_DECISIONS = {"PASS", "REJECT", "REJECTED", "PROPOSED", "REVISE", "NEEDS_HUMAN"}
    DFT_REVIEW_DECISION_ALIASES = {
        "CONFIRMED": "PASS",
        "ACCEPT": "PASS",
        "ACCEPTED": "PASS",
        "APPROVED": "PASS",
        "VERIFIED": "PASS",
        "OK": "PASS",
        "CONFIRMED_WITH_CORRECTIONS": "PROPOSED",
        "CORRECTED": "PROPOSED",
        "REVISION": "PROPOSED",
        "NEEDS_USER_DECISION": "NEEDS_HUMAN",
        "AMBIGUOUS": "NEEDS_HUMAN",
    }
    OBJECT_REVIEW_CONTAINER_KEYS = {"object_review_audits", "object_reviews", "field_reviews"}
    GENERIC_OBJECT_REVIEW_CONTAINER_KEYS = {"reviews", "audits", "opinions", "items"}

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

    def diagnose_import_warnings(
        self,
        run: ExternalAnalysisRun,
        *,
        candidates: list[ExternalAnalysisCandidate] | None = None,
        auto_apply_summary: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-fatal import diagnostics for silent no-op-prone payloads."""

        warnings: list[dict[str, Any]] = []
        raw_payload = run.raw_payload
        if isinstance(raw_payload, dict):
            for key, value in raw_payload.items():
                if not self._is_unrecognized_object_review_container(key, value):
                    continue
                warnings.append(
                    {
                        "code": "unrecognized_object_review_container",
                        "severity": "warning",
                        "key": key,
                        "message": (
                            f"raw_payload.{key} looks like object-level review data but is not imported. "
                            "Use raw_payload.object_review_audits."
                        ),
                        "expected_key": "object_review_audits",
                    }
                )

        rows = candidates if candidates is not None else self.list_candidates(run.id)
        for candidate in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            if candidate.candidate_type != "object_review_audit":
                continue
            target_type = self._normalize_dft_warning_target_type(payload.get("target_type"))
            if target_type != "dft_results":
                continue
            decision = payload.get("decision") or payload.get("verdict")
            normalized_decision = self._normalize_dft_review_decision_for_warning(decision)
            target_id = str(payload.get("target_id") or "").strip()
            if normalized_decision == "NEW_CANDIDATE" or target_id.lower() == "new":
                continue
            if not normalized_decision:
                continue
            if normalized_decision in self.COUNTABLE_DFT_REVIEW_DECISIONS:
                continue
            warnings.append(
                {
                    "code": "non_countable_dft_decision",
                    "severity": "warning",
                    "candidate_id": str(candidate.id),
                    "target_type": payload.get("target_type"),
                    "target_id": payload.get("target_id"),
                    "field_name": payload.get("field_name"),
                    "decision": decision,
                    "normalized_decision": normalized_decision,
                    "allowed_decisions": sorted(self.COUNTABLE_DFT_REVIEW_DECISIONS),
                    "message": (
                        f"DFT object review decision {decision!r} is not counted as a valid AI opinion. "
                        "Use PASS, REJECT, REJECTED, PROPOSED, REVISE, or NEEDS_HUMAN for existing DFT rows. "
                        "needs_user_decision and ambiguous are normalized to NEEDS_HUMAN and stay in manual adjudication; "
                        "they are not auto-adoptable opinions."
                    ),
                }
            )

        new_dft_summary = (auto_apply_summary or {}).get("new_dft_candidates") if isinstance(auto_apply_summary, dict) else None
        if isinstance(new_dft_summary, dict):
            for item in new_dft_summary.get("skipped_items") or []:
                if not isinstance(item, dict):
                    continue
                warnings.append(
                    {
                        "code": "new_dft_candidate_materialization_skipped",
                        "severity": "warning",
                        "candidate_id": item.get("candidate_id"),
                        "reason": item.get("reason"),
                        "message": (
                            "A DFT new_candidate audit was imported but not materialized into dft_results: "
                            f"{item.get('reason') or 'unknown'}."
                        ),
                    }
                )
        return warnings

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
