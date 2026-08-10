from __future__ import annotations

import json
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
    @staticmethod
    def _is_dft_scoped_materialization_run(
        run: ExternalAnalysisRun,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        payload = payload or {}
        parts = [
            run.source,
            run.source_label,
            payload.get("source"),
            payload.get("source_label"),
            payload.get("agent_role"),
            payload.get("adjudication_scope"),
        ]
        text = " ".join(str(part or "") for part in parts).casefold()
        return "dft" in text

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
            payload = candidate.normalized_payload or {}
            if (
                candidate.candidate_type in {"note", "correction", "relationship"}
                and self._is_dft_scoped_materialization_run(run, payload)
            ):
                candidate.status = "requires_resolution"
                candidate.materialized_target_type = None
                candidate.materialized_target_id = None
                candidate.mapping_reason = "dft_scoped_run_rejects_non_dft_candidate"
                self.session.add(candidate)
                result.skipped_candidates += 1
                continue
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
                evidence_payload.update(
                    {
                        "protocol": protocol_snapshot("external_analysis_candidate_only"),
                        "writes_final_truth": False,
                        "requires_confirmation": True,
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
                    "writes_final_truth": False,
                    "requires_confirmation": result.created_corrections > 0,
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
        """Retain imported AI outputs for unified single-AI verification or exception handling."""

        run = self.get_run(run_id)
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.run_id == run.id)
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        result = MaterializationResult()
        for candidate in candidates:
            if candidate.status not in {"pending", "requires_resolution"}:
                result.skipped_candidates += 1
                continue
            payload = candidate.normalized_payload or {}
            if (
                candidate.candidate_type in {"note", "correction", "relationship"}
                and self._is_dft_scoped_materialization_run(run, payload)
            ):
                candidate.status = "requires_resolution"
                candidate.materialized_target_type = None
                candidate.materialized_target_id = None
                candidate.mapping_reason = "dft_scoped_run_rejects_non_dft_candidate"
                self.session.add(candidate)
                result.skipped_candidates += 1
                continue

            candidate.status = "requires_resolution"
            candidate.materialized_target_type = None
            candidate.materialized_target_id = None
            candidate.mapping_reason = "authenticated_human_review_required"
            self.session.add(candidate)
            result.skipped_candidates += 1

        self.session.add(
            AuditLog(
                paper_id=run.paper_id,
                action="defer_external_analysis_candidates_for_human_review",
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
                    "protocol": protocol_snapshot("external_analysis_candidate_only"),
                    "writes_final_truth": False,
                    "requires_confirmation": True,
                    "dft_outputs_excluded": True,
                    "write_lock": {
                        "required_modules": [],
                        "covered_modules": [],
                        "lock_ids": [],
                        "policy": "no_ai_overwrite",
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
        3. Runs the compatibility-named non-DFT path, which retains ordinary
           outputs for authenticated human review under ``no_ai_overwrite``.
        4. Runs the DFT candidate materialization path. Only ``new_candidate``
           may become an unverified ``DFTResult`` candidate. Final acceptance is
           performed only by the dedicated single-AI verification service;
           this pipeline never performs authoritative acceptance.
        5. Returns the combined ``auto_apply_summary`` mirroring the historical
           MCP response shape.

        Parameters
        ----------
        reviewer:
            Reviewer label recorded on retained/materialized candidate output. This is also used
            as the ``write_lock_owner`` fallback when the caller does not pass
            an explicit owner list.
        write_lock_tokens:
            Compatibility lock tokens supplied by the caller. For DFT candidate
            materialization, a non-empty list skips the internal auto-acquire step.
            These tokens do not authorize ordinary non-DFT overwrite.
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
        imports_dft = any(self._is_dft_import_candidate(candidate) for candidate in candidates)
        if imports_dft:
            self._guard_dft_import_prerequisites(run, candidates)

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
                    # original candidate-materialization error. Log an audit entry so that a
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

        response = {
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
        if imports_dft:
            response["dft_readback"] = self._dft_import_readback(run.paper_id, candidates, dft_summary or {})
        return response

    @staticmethod
    def _is_dft_import_candidate(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        target_type = str(payload.get("target_type") or "").strip().lower()
        field_name = str(payload.get("field_name") or "").strip().lower()
        target_path = str(payload.get("target_path") or "").strip().lower()
        candidate_type = str(candidate.candidate_type or "").strip().lower()
        corrected = payload.get("corrected_value")
        corrected_keys = (
            {str(key).strip().lower() for key in corrected}
            if isinstance(corrected, dict)
            else set()
        )
        looks_like_structured_dft_value = (
            candidate_type == "object_review_audit"
            and {"value", "unit"} <= corrected_keys
            and bool({"property_type", "energy_type", "property"} & corrected_keys)
            and bool({"material_identity", "material", "catalyst", "structure_name"} & corrected_keys)
        )
        return (
            target_type in {"dft_result", "dft_results"}
            or field_name in {"dft_result", "dft_results"}
            or target_path.startswith(("dft_result:", "dft_results:"))
            or candidate_type in {"dft_result", "dft_results"}
            or looks_like_structured_dft_value
        )

    def _guard_dft_import_prerequisites(
        self,
        run: ExternalAnalysisRun,
        candidates: list[ExternalAnalysisCandidate],
    ) -> None:
        from app.services.dft_review_bundle_service import DFTReviewBundleService

        dft_candidates = [candidate for candidate in candidates if self._is_dft_import_candidate(candidate)]
        validation = self._validate_dft_import_json(run, dft_candidates)
        missing_export_authorization: list[str] = []
        for candidate in dft_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            decision = str(payload.get("decision") or "").strip().upper()
            recommended_action = str(payload.get("recommended_action") or "").strip().lower()
            if decision not in {"PASS", "REVISE"} or recommended_action == "ready_for_ml_export":
                continue
            candidate.status = "requires_resolution"
            candidate.mapping_reason = "recommended_action_ready_for_ml_export_required"
            self.session.add(candidate)
            missing_export_authorization.append(str(candidate.id))
        if missing_export_authorization:
            self.session.flush()
            raise ValueError(
                "dft_export_authorization_required:PASS/REVISE requires "
                "recommended_action='ready_for_ml_export';candidate_ids="
                + ",".join(missing_export_authorization)
            )
        expected_snapshot = self._dft_import_expected_completed_snapshot(run, dft_candidates)
        bundle_service = DFTReviewBundleService(self.session, self.settings)
        state = bundle_service.get_review_state(run.paper_id)["review_gate"]
        if not expected_snapshot:
            raise ValueError("figure_table_review_not_completed:missing_completed_snapshot_fingerprint")
        bundle_service.ensure_figure_table_review_ready(
            state,
            expected_completed_snapshot_fingerprint=expected_snapshot,
        )
        validated_request = validation.get("import_analysis_request") or {}
        validated_raw_payload = (
            validated_request.get("raw_payload")
            if isinstance(validated_request.get("raw_payload"), dict)
            else {}
        )
        server_audits = validated_raw_payload.get("object_review_audits") or []
        server_audits_by_key = {
            self._dft_audit_identity(audit): audit
            for audit in server_audits
            if isinstance(audit, dict)
        }
        missing_local_verification: list[str] = []
        verification_failures: list[str] = []
        for candidate in dft_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            server_audit = server_audits_by_key.get(self._dft_audit_identity(payload))
            required_checks = (
                server_audit.get("required_evidence_checks")
                if isinstance(server_audit, dict)
                else []
            )
            failures = self._local_ai_verification_failures(payload, required_checks)
            if server_audit is None:
                failures.append("server_verification_requirements_missing")
            if not failures:
                continue
            candidate.status = "requires_resolution"
            candidate.mapping_reason = "local_ai_pdf_verification_required:" + ",".join(failures)
            self.session.add(candidate)
            missing_local_verification.append(str(candidate.id))
            verification_failures.extend(failures)
        if missing_local_verification:
            self.session.flush()
            raise ValueError(
                "local_ai_pdf_verification_required:"
                "DFT import_analysis requires complete server-derived evidence and source-page coverage per audit. "
                "Identical evidence_id and (source_paper_id, page) reads may be reused, but each audit must record "
                "its own checked_evidence_ids, checked_pages, required tools, and verification_note. failures="
                + ",".join(sorted(set(verification_failures)))
            )

    def _validate_dft_import_json(
        self,
        run: ExternalAnalysisRun,
        candidates: list[ExternalAnalysisCandidate],
    ) -> dict[str, Any]:
        from app.services.dft_review_bundle_service import DFTReviewBundleService

        raw_payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        metadata = raw_payload.get("review_metadata") if isinstance(raw_payload.get("review_metadata"), dict) else {}
        review_source = metadata.get("web_ai_review_source") or metadata.get("review_source")
        audits: list[dict[str, Any]] = []
        unsupported_candidate_ids: list[str] = []
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip().lower()
            if candidate.candidate_type != "object_review_audit" or target_type not in {
                "dft_result",
                "dft_results",
            }:
                unsupported_candidate_ids.append(str(candidate.id))
                continue
            evidence = payload.get("evidence_location") if isinstance(payload.get("evidence_location"), dict) else {}
            evidence_ids = payload.get("evidence_ids") or evidence.get("evidence_ids") or []
            audits.append(
                {
                    "target_type": "dft_results",
                    "target_id": payload.get("target_id"),
                    "temporary_id": payload.get("temporary_id"),
                    "field_name": payload.get("field_name") or "dft_results",
                    "decision": payload.get("decision"),
                    "evidence_checked": payload.get("evidence_checked") is True,
                    "evidence_ids": evidence_ids,
                    "corrected_value": payload.get("corrected_value"),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "blocking_errors": payload.get("blocking_errors") or [],
                    "recommended_action": payload.get("recommended_action"),
                    "dedupe_analysis": payload.get("dedupe_analysis"),
                }
            )
        if unsupported_candidate_ids:
            raise ValueError(
                "dft_json_validation_failed:unsupported_dft_import_candidate:"
                + ",".join(unsupported_candidate_ids)
            )
        candidate_payload = {
            "schema_version": metadata.get("schema_version"),
            "bundle_fingerprint": metadata.get("bundle_fingerprint"),
            "figure_table_completed_snapshot_fingerprint": (
                metadata.get("figure_table_completed_snapshot_fingerprint")
            ),
            "paper_id": str(run.paper_id),
            "paper_code": metadata.get("paper_code"),
            "chart_scope_type": metadata.get("chart_scope_type") or "paper_reviewed_aggregate",
            "chart_run_id": metadata.get("chart_run_id"),
            "catalyst_sample_id": metadata.get("catalyst_sample_id"),
            "dft_result_ids": metadata.get("dft_result_ids") or [],
            "review_mode": metadata.get("review_mode"),
            "review_source": review_source,
            "overall_status": metadata.get("overall_status"),
            "coverage_acknowledgement": raw_payload.get("coverage_acknowledgement"),
            "object_review_audits": audits,
            "uncertainties": raw_payload.get("uncertainties") or [],
            "notes": raw_payload.get("notes") or [],
        }
        validation = DFTReviewBundleService(self.session, self.settings).validate_result(
            run.paper_id,
            candidate_payload,
        )
        if validation.get("valid") is not True:
            raise ValueError(
                "dft_json_validation_failed:"
                + json.dumps(validation.get("errors") or [], ensure_ascii=False, default=str)
            )
        return validation

    @staticmethod
    def _dft_import_expected_completed_snapshot(
        run: ExternalAnalysisRun,
        candidates: list[ExternalAnalysisCandidate],
    ) -> str | None:
        fingerprints: set[str] = set()
        raw_payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        metadata = raw_payload.get("review_metadata") if isinstance(raw_payload.get("review_metadata"), dict) else {}
        for source in (raw_payload, metadata):
            value = source.get("figure_table_completed_snapshot_fingerprint") if isinstance(source, dict) else None
            if value:
                fingerprints.add(str(value).strip())
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            evidence = payload.get("evidence_location") if isinstance(payload.get("evidence_location"), dict) else {}
            for source in (payload, evidence):
                value = source.get("figure_table_completed_snapshot_fingerprint") if isinstance(source, dict) else None
                if value:
                    fingerprints.add(str(value).strip())
        fingerprints.discard("")
        if len(fingerprints) > 1:
            raise ValueError("conflicting_figure_table_completed_snapshot_fingerprint")
        return next(iter(fingerprints), None)

    @staticmethod
    def _dft_import_chart_run_id(
        run: ExternalAnalysisRun,
        candidates: list[ExternalAnalysisCandidate],
    ) -> UUID | None:
        values: set[str] = set()
        raw_payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        metadata = raw_payload.get("review_metadata") if isinstance(raw_payload.get("review_metadata"), dict) else {}
        for source in (raw_payload, metadata):
            value = source.get("chart_run_id") if isinstance(source, dict) else None
            if value:
                values.add(str(value).strip())
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            for source in (payload, payload.get("evidence_location") if isinstance(payload.get("evidence_location"), dict) else {}):
                value = source.get("chart_run_id") if isinstance(source, dict) else None
                if value:
                    values.add(str(value).strip())
        values.discard("")
        if len(values) > 1:
            raise ValueError("conflicting_chart_run_id")
        if not values:
            return None
        try:
            return UUID(next(iter(values)))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_chart_run_id") from exc

    @staticmethod
    def _dft_audit_identity(payload: dict[str, Any]) -> tuple[str, str, str | None]:
        target_id = str(payload.get("target_id") or "").strip()
        field_name = str(payload.get("field_name") or "dft_results").strip()
        temporary_id = (
            str(payload.get("temporary_id") or "").strip() or None
            if target_id.lower() == "new"
            else None
        )
        return target_id, field_name, temporary_id

    @classmethod
    def _payload_has_local_ai_verification(
        cls,
        payload: dict[str, Any],
        required_evidence_checks: Any = None,
    ) -> bool:
        return not cls._local_ai_verification_failures(payload, required_evidence_checks or [])

    @staticmethod
    def _local_ai_verification_failures(
        payload: dict[str, Any],
        required_evidence_checks: Any,
    ) -> list[str]:
        verification = payload.get("local_ai_verification")
        if not isinstance(verification, dict):
            evidence = payload.get("evidence_location") if isinstance(payload.get("evidence_location"), dict) else {}
            verification = evidence.get("local_ai_verification") if isinstance(evidence, dict) else None
        if not isinstance(verification, dict):
            return ["missing_local_ai_verification"]
        failures: list[str] = []
        if verification.get("verified_against_pdf") is not True:
            failures.append("verified_against_pdf_not_true")
        used_tools = {
            str(item).strip()
            for item in (
                verification.get("used_tools")
                or verification.get("tools_used")
                or verification.get("tool_calls")
                or []
            )
            if str(item).strip()
        }
        if not {"get_codex_item", "read_paper_page"} <= used_tools:
            failures.append("required_tools_missing")

        expected_evidence_ids: set[str] = set()
        expected_pages: set[tuple[str, int]] = set()
        for check in required_evidence_checks if isinstance(required_evidence_checks, list) else []:
            if not isinstance(check, dict):
                continue
            evidence_id = str(check.get("evidence_id") or "").strip()
            source_paper_id = str(check.get("source_paper_id") or "").strip()
            try:
                page = int(check.get("page"))
            except (TypeError, ValueError):
                continue
            if evidence_id:
                expected_evidence_ids.add(evidence_id)
            if source_paper_id:
                expected_pages.add((source_paper_id, page))

        checked_evidence_ids = {
            str(item).strip()
            for item in (verification.get("checked_evidence_ids") or [])
            if str(item).strip()
        }
        if not expected_evidence_ids <= checked_evidence_ids:
            failures.append("checked_evidence_ids_incomplete")

        checked_pages: set[tuple[str, int]] = set()
        for item in verification.get("checked_pages") or []:
            if not isinstance(item, dict):
                continue
            paper_id = str(item.get("paper_id") or item.get("source_paper_id") or "").strip()
            try:
                page = int(item.get("page"))
            except (TypeError, ValueError):
                continue
            if paper_id:
                checked_pages.add((paper_id, page))
        if not expected_pages <= checked_pages:
            failures.append("checked_pages_incomplete_or_wrong_source")

        if not str(verification.get("verification_note") or "").strip():
            failures.append("verification_note_required")
        return failures

    def _dft_import_readback(
        self,
        paper_id: UUID,
        candidates: list[ExternalAnalysisCandidate],
        dft_summary: dict[str, Any],
    ) -> dict[str, Any]:
        from app.db.models import DFTResult, ExtractionFieldReview
        from app.services.review_conflict_service import ReviewConflictAggregationService
        from app.utils.review_safety import bulk_export_gate_results

        target_ids: set[str] = set()
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_ids.add(str(payload.get("target_id") or "").strip())
        for item in (dft_summary.get("new_dft_candidates") or {}).get("materialized_items") or []:
            if item.get("dft_result_id"):
                target_ids.add(str(item["dft_result_id"]))
        target_ids.discard("")
        target_ids.discard("new")
        valid_target_ids: list[UUID] = []
        for target_id in sorted(target_ids):
            try:
                valid_target_ids.append(UUID(target_id))
            except (TypeError, ValueError):
                continue
        rows = []
        if valid_target_ids:
            rows = self.session.scalars(
                select(DFTResult)
                .where(DFTResult.paper_id == paper_id)
                .where(DFTResult.id.in_(valid_target_ids))
            ).all()
        gates = bulk_export_gate_results(self.session, rows, target_type="dft_results") if rows else {}
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id.in_([str(row.id) for row in rows] or ["__none__"]),
            )
        ).all()
        versions_by_target: dict[str, dict[str, int]] = {}
        for review in reviews:
            versions_by_target.setdefault(str(review.target_id), {})[str(review.field_name)] = int(review.write_version or 1)
        conflicts = ReviewConflictAggregationService(self.session).list_conflicts(
            paper_id=paper_id,
            target_type="dft_results",
            active_only=True,
            limit=100,
        )
        unfinished = (
            (dft_summary.get("object_reviews") or {}).get("pending_items")
            or []
        ) + (
            (dft_summary.get("object_reviews") or {}).get("skipped_items")
            or []
        )
        return {
            "object_versions": versions_by_target,
            "candidate_status": {
                str(row.id): row.candidate_status
                for row in rows
            },
            "export_safety": {
                str(row.id): {
                    "eligible": gates[str(row.id)].eligible,
                    "blocked_reasons": list(gates[str(row.id)].reasons),
                    "review_status": gates[str(row.id)].review_status,
                }
                for row in rows
                if str(row.id) in gates
            },
            "conflicts": conflicts.get("rows", []),
            "unfinished_items": unfinished,
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
                "requires_confirmation": True,
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
