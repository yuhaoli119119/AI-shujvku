from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import (
    AuditLog,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    PaperCorrection,
    PaperNote,
    PaperRelationship,
)
from app.services.external_analysis_models import (
    ExternalAnalysisNormalizedModel,
    ExternalCorrectionProposalModel,
    MaterializationResult,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.review_service import ReviewService
from app.utils.evidence_anchors import has_evidence_anchor, has_material_correction_anchor
from app.utils.protocol_tracking import protocol_snapshot


logger = logging.getLogger("app.services.external_analysis_service")


class ExternalAnalysisMaterializationMixin:
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
        review_service = ReviewService(self.session)
        for candidate in candidates:
            payload = candidate.normalized_payload or {}
            if (
                candidate.candidate_type == "object_review_audit"
                and candidate.status in {"candidate", "pending", "requires_resolution"}
            ):
                result.skipped_candidates += 1
                result.deferred_review_candidates += 1
                continue
            if (
                candidate.candidate_type == "correction"
                and candidate.status in {"pending", "requires_resolution"}
                and self._is_table_correction_payload(payload)
            ):
                candidate.status = "requires_resolution"
                candidate.mapping_reason = "direct_mcp_tool_required:table_object_mutation"
                self.session.add(candidate)
                result.skipped_candidates += 1
                continue
            if candidate.status not in {"pending", "requires_resolution"}:
                result.skipped_candidates += 1
                continue

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
                evidence_payload = self._external_candidate_evidence_payload(
                    run,
                    payload.get("evidence_payload"),
                )
                auto_apply_non_dft = self._is_auto_applicable_non_dft_correction(payload)
                if auto_apply_non_dft:
                    evidence_payload.update(
                        {
                            "protocol": protocol_snapshot("ide_ai_non_dft_auto_apply"),
                            "writes_final_truth": True,
                            "requires_human_confirmation": False,
                        }
                    )
                correction = PaperCorrection(
                    paper_id=candidate.paper_id,
                    source=run.source,
                    field_name=payload.get("field_name", ""),
                    target_path=payload.get("target_path", ""),
                    operation=payload.get("operation", "replace"),
                    proposed_value=payload.get("proposed_value"),
                    reason=payload.get("reason", ""),
                    evidence_payload=evidence_payload,
                    status="pending",
                )
                self.session.add(correction)
                self.session.flush()
                if auto_apply_non_dft:
                    review_service.approve_correction(correction.id, created_by)
                    candidate.status = "ai_applied"
                    result.auto_applied_corrections += 1
                else:
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
                    "auto_applied_corrections": result.auto_applied_corrections,
                    "idempotent_noops": result.idempotent_noops,
                    "skipped_candidates": result.skipped_candidates,
                    "deferred_review_candidates": result.deferred_review_candidates,
                    "source_run_id": str(run.id),
                    "protocol": protocol_snapshot("gemini_audit_protocol"),
                    "writes_final_truth": result.auto_applied_corrections > 0,
                    "requires_human_confirmation": result.created_corrections > result.auto_applied_corrections,
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
                if self._is_table_correction_payload(payload):
                    candidate.status = "requires_resolution"
                    candidate.mapping_reason = "direct_mcp_tool_required:table_object_mutation"
                    self.session.add(candidate)
                    result.skipped_candidates += 1
                    continue
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
                    "idempotent_noops": result.idempotent_noops,
                    "skipped_candidates": result.skipped_candidates,
                    "source_run_id": str(run.id),
                    "protocol": protocol_snapshot("ide_ai_non_dft_auto_apply"),
                    "writes_final_truth": True,
                    "requires_human_confirmation": False,
                    "dft_outputs_excluded": True,
                    "write_lock": {
                        "required_modules": [],
                        "covered_modules": [],
                        "lock_ids": [],
                        "policy": "not_required_for_non_dft_ai_overwrite",
                    },
                },
            )
        )
        self.session.flush()
        return result

    def apply_review_rules_for_run(
        self,
        run_id: UUID,
        *,
        reviewer: str,
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
        auto_lock_owner: str | None = None,
        lock_meta_source: str = "external_analysis_import",
    ) -> dict[str, Any]:
        """Apply IDE-AI review rules to an existing external analysis run.

        This is the single shared pipeline used by both the MCP ``import_analysis``
        tool and the HTTP ``POST /import`` endpoint.  It:

        1. Detects whether the run contains DFT ``object_review_audit`` candidates.
        2. When DFT candidates are present and no external ``write_lock_tokens``
           were supplied, auto-acquires a ``dft_results`` module write lock so the
           downstream ``apply_import_rules_for_paper`` gate does not reject the
           write.  The lock is released in a ``finally`` block to guarantee it
           never leaks on success or failure.
        3. Runs the non-DFT auto-apply path (notes/corrections/relationships).
        4. Runs the DFT settlement path (materialize new candidates + dual-AI
           consensus state machine) via ``VerificationSessionService``.
        5. Returns the combined ``auto_apply_summary`` mirroring the historical
           MCP response shape.

        Parameters
        ----------
        reviewer:
            Reviewer label recorded on auto-applied outputs.  This is also used
            as the ``write_lock_owner`` fallback when the caller does not pass
            an explicit owner list.
        write_lock_tokens:
            Lock tokens supplied by the caller (e.g. previously acquired via
            ``acquire_module_write_lock``).  When non-empty the auto-acquire
            step is skipped.
        write_lock_owner:
            Owner(s) allowed to validate the supplied tokens.  MCP passes a
            list ``[internal, reviewer]``; HTTP passes a single ``reviewer``.
            This deliberately preserves the two entry points' identity
            semantics rather than collapsing them.
        auto_lock_owner:
            ``locked_by`` to use when auto-acquiring a DFT lock.  MCP passes
            ``effective_internal_reviewer``; HTTP passes ``effective_reviewer``.
            Defaults to ``reviewer`` when not specified.
        lock_meta_source:
            Source tag recorded in the lock's metadata for audit traceability.
        """

        run = self.get_run(run_id)
        candidates = self.list_candidates(run.id)
        tokens: list[str] = [str(item).strip() for item in (write_lock_tokens or []) if str(item or "").strip()]
        imports_dft = any(
            str((c.normalized_payload or {}).get("target_type") or "").strip().lower()
            in {"dft_result", "dft_results"}
            for c in candidates
            if isinstance(c.normalized_payload, dict)
        )

        lock_service = ModuleWriteLockService(self.session)
        auto_lock = None
        if imports_dft and not tokens:
            acquire_owner = str(auto_lock_owner or reviewer or "ide_ai").strip() or "ide_ai"
            auto_lock = lock_service.acquire(
                paper_id=run.paper_id,
                module_name="dft_results",
                locked_by=acquire_owner,
                meta={"source": lock_meta_source, "run_id": str(run.id)},
            )
            tokens.append(auto_lock.lock_token)

        try:
            non_dft_summary = self.auto_apply_non_dft_review_outputs(
                run.id,
                reviewer=reviewer,
                write_lock_tokens=tokens or None,
                write_lock_owner=write_lock_owner,
            )
            from app.services.verification_session_service import VerificationSessionService

            dft_summary = VerificationSessionService(self.session, self.settings).apply_import_rules_for_paper(
                paper_id=run.paper_id,
                reviewer=reviewer,
                candidate_run_id=run.id,
                write_lock_tokens=tokens or None,
                write_lock_owner=write_lock_owner,
            )
        finally:
            if auto_lock is not None:
                release_owner = str(auto_lock_owner or reviewer or "ide_ai").strip() or "ide_ai"
                try:
                    lock_service.release(
                        lock_token=auto_lock.lock_token,
                        released_by=release_owner,
                    )
                except Exception as release_exc:
                    # Best-effort release; surface nothing that would mask the
                    # original apply error.  But log an audit entry so that a
                    # leaked lock is observable rather than silently lost.
                    # Stale locks are also reaped by TTL as a backstop.
                    logger.exception(
                        "Failed to release auto-acquired DFT module write lock",
                        extra={
                            "paper_id": str(run.paper_id),
                            "run_id": str(run.id),
                            "module_name": "dft_results",
                            "lock_token": auto_lock.lock_token,
                        },
                    )
                    self.session.add(
                        AuditLog(
                            paper_id=run.paper_id,
                            action="auto_lock_release_failed",
                            source=release_owner,
                            target_type="module_write_lock",
                            target_id=auto_lock.lock_token,
                            payload={
                                "run_id": str(run.id),
                                "module_name": "dft_results",
                                "error": str(release_exc),
                            },
                        )
                    )
                    try:
                        self.session.flush()
                    except Exception:
                        pass

        return {
            **(dft_summary or {}),
            "non_dft_auto_apply": {
                "created_notes": non_dft_summary.created_notes,
                "created_corrections": non_dft_summary.created_corrections,
                "created_relationships": non_dft_summary.created_relationships,
                "auto_applied_corrections": non_dft_summary.auto_applied_corrections,
                "idempotent_noops": non_dft_summary.idempotent_noops,
                "skipped_candidates": non_dft_summary.skipped_candidates,
            },
        }

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
        if ExternalAnalysisMaterializationMixin._is_table_correction_payload(payload):
            return False
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
                and parts[0] == field_name
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
    def _is_table_correction_payload(payload: dict[str, Any]) -> bool:
        field_name = str(payload.get("field_name") or "").strip().lower()
        target_path = str(payload.get("target_path") or "").strip().lower()
        return field_name in {"table", "tables"} or target_path.startswith("tables:")

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
            payload = correction.model_dump(mode="python")
            if ExternalAnalysisMaterializationMixin._is_table_correction_payload(payload):
                blocked.append(
                    {
                        "create": "create_table",
                        "delete": "delete_table",
                        "merge": "merge_table",
                        "merge_table": "merge_table",
                    }.get(operation, "update_table")
                )
                continue
            if operation in direct_tool_ops:
                blocked.append(operation)
        if blocked:
            tools = ", ".join(sorted(set(blocked)))
            raise ValueError(
                "direct_mcp_tool_required:"
                f"{tools} must be called directly through MCP and must not be submitted through import_analysis. "
                "Table object mutations must use update_table/create_table/merge_table/delete_table; figure image "
                "operations must use recrop_figure/create_figure_from_bbox. Call the real tool, then read back the object."
            )
