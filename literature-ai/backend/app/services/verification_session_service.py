from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperNote,
    WorkflowJob,
    WritingCard,
    VerificationSessionPaperClaim,
    utcnow,
)
from app.services.dft_rescan_policy import (
    is_dft_method_only_reaction_step,
    normalize_dft_reaction_step_for_identity,
)
from app.services.dft_review_helpers import normalize_dft_value_for_comparison, same_normalized_dft_value
from app.services.dft_review_service import DFTResultReviewService
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.review_conflict_service import (
    DECISION_NEGATIVE,
    DECISION_POSITIVE,
    ReviewConflictAggregationService,
)
from app.services.review_service import ReviewService
from app.services.review_target_resolver import canonical_target_type
from app.utils.evidence_anchors import has_evidence_anchor
from app.utils.library_names import DEFAULT_LIBRARY_NAME, normalize_library_name
from app.utils.review_safety import is_safe_verified_review


class VerificationSessionConflict(ValueError):
    def __init__(self, paper_id: UUID, session_id: str | None = None) -> None:
        self.code = "verification_session_paper_conflict"
        self.paper_id = paper_id
        self.session_id = session_id
        detail = f"paper_id={paper_id}"
        if session_id:
            detail += f",session_id={session_id}"
        super().__init__(f"{self.code}:{detail}")


class VerificationSessionService:
    HIGH_RISK_IDE_TARGET_TYPES = {"dft_results", "catalyst_samples"}
    PROJECT_LIBRARY_V4_USER_SUBMIT_REASON = "project_library_v4_requires_user_submit"
    HIGH_RISK_SCOPES = {
        "all": {"dft_results", "mechanism_claims", "electrochemical_performance", "catalyst_samples", "dft_settings"},
        "dft_only": {"dft_results"},
        "writing_only": set(),
    }
    LOW_RISK_NOTE_SCOPES = {
        "all": {"review_notes"},
        "dft_only": set(),
        "writing_only": {"review_notes"},
    }
    DFT_FIELD_ALIASES = {
        "catalyst": "catalyst_sample_id",
        "catalyst_id": "catalyst_sample_id",
        "catalyst_sample": "catalyst_sample_id",
        "catalyst_sample_id": "catalyst_sample_id",
        "material_binding": "catalyst_sample_id",
        "structure_binding": "catalyst_sample_id",
        "energy_type": "property_type",
        "property_type": "property_type",
        "energy": "property_type",
        "value": "value",
        "energy_value": "value",
        "unit": "unit",
        "adsorbate": "adsorbate",
        "reaction_step": "reaction_step",
        "source_section": "source_section",
        "source_figure": "source_figure",
        "confidence": "confidence",
        "evidence_text": "evidence_text",
    }
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.conflicts = ReviewConflictAggregationService(session)

    def create_session(
        self,
        *,
        paper_ids: list[UUID] | None = None,
        paper_refs: list[str] | None = None,
        scope: str,
        refresh_materials: bool,
        reviewer: str,
    ) -> dict[str, Any]:
        selected = self._resolve_papers(paper_ids or [], paper_refs or [])
        if not selected:
            raise ValueError("No papers matched the requested selection.")
        session_id = str(uuid4())
        lane_labels = {
            "primary": f"verify:{session_id}:primary",
            "secondary": f"verify:{session_id}:secondary",
            "single": f"verify:{session_id}:single",
        }
        payload = {
            "session_id": session_id,
            "scope": scope,
            "paper_ids": [str(paper.id) for paper in selected],
            "paper_refs": [item for item in paper_refs or [] if str(item or "").strip()],
            "refresh_materials": refresh_materials,
            "lane_labels": lane_labels,
            "created_by": reviewer,
            "created_at": datetime.utcnow().isoformat(),
        }
        preparation_rows: list[dict[str, Any]] = []
        job = WorkflowJob(
            job_id=session_id,
            type="verification_session",
            status="active",
            library_name=selected[0].library_name or DEFAULT_LIBRARY_NAME,
            payload=payload,
            progress={"prepared": len(selected), "completed": True},
            result={
                "selection": [self._paper_summary(paper) for paper in selected],
                "scope_summary": self._scope_summary(selected, scope),
                "preparation_rows": preparation_rows,
                "lanes": self._lane_plan(scope, lane_labels),
            },
        )
        self.session.add(job)
        self._claim_session_papers(session_id, [paper.id for paper in selected])
        self.session.add(
            AuditLog(
                action="create_verification_session",
                source=reviewer,
                target_type="verification_session",
                target_id=session_id,
                payload={
                    "scope": scope,
                    "paper_ids": payload["paper_ids"],
                    "lane_labels": lane_labels,
                    "refresh_materials": refresh_materials,
                },
            )
        )
        self.session.commit()
        if refresh_materials:
            try:
                reprocessing = PaperReprocessingService(session=self.session, settings=self.settings)
                for paper in selected:
                    summary = reprocessing.rerun_stage2(paper.id)
                    preparation_rows.append(
                        {
                            "paper_id": str(paper.id),
                            "status": summary.get("status") or "completed",
                            "external_ai_ready": bool(summary.get("external_ai_ready")),
                            "workspace_path": summary.get("workspace_path"),
                        }
                    )
                job = self._get_session_job(session_id)
                result = dict(job.result or {})
                result["preparation_rows"] = preparation_rows
                job.result = result
                self.session.add(job)
                self.session.commit()
            except Exception:
                self._release_session_papers(session_id, status="released")
                job = self._get_session_job(session_id)
                job.status = "failed"
                self.session.add(job)
                self.session.commit()
                raise
        return self.get_session(session_id)

    def _claim_session_papers(self, session_id: str, paper_ids: list[UUID]) -> None:
        now = utcnow()
        ordered = sorted(set(paper_ids), key=str)
        bind = self.session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            for paper_id in ordered:
                key = int.from_bytes(paper_id.bytes[:8], byteorder="big", signed=True)
                if not self.session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
                ).scalar():
                    self.session.rollback()
                    raise VerificationSessionConflict(paper_id)
        stale = self.session.scalars(
            select(VerificationSessionPaperClaim).where(
                VerificationSessionPaperClaim.paper_id.in_(ordered),
                VerificationSessionPaperClaim.status == "active",
                VerificationSessionPaperClaim.expires_at <= now,
            )
        ).all()
        for claim in stale:
            claim.status = "expired"
            claim.released_at = now
            self.session.add(claim)
        if stale:
            self.session.flush()
        claims = [
            VerificationSessionPaperClaim(
                session_id=session_id,
                paper_id=paper_id,
                status="active",
                expires_at=now + timedelta(hours=4),
            )
            for paper_id in ordered
        ]
        try:
            with self.session.begin_nested():
                self.session.add_all(claims)
                self.session.flush()
        except IntegrityError:
            self.session.rollback()
            conflict = self.session.scalar(
                select(VerificationSessionPaperClaim).where(
                    VerificationSessionPaperClaim.paper_id.in_(ordered),
                    VerificationSessionPaperClaim.status == "active",
                    VerificationSessionPaperClaim.expires_at > now,
                )
            )
            raise VerificationSessionConflict(
                conflict.paper_id if conflict else ordered[0],
                conflict.session_id if conflict else None,
            )

    def _release_session_papers(self, session_id: str, *, status: str = "released") -> None:
        now = utcnow()
        claims = self.session.scalars(
            select(VerificationSessionPaperClaim).where(
                VerificationSessionPaperClaim.session_id == session_id,
                VerificationSessionPaperClaim.status == "active",
            )
        ).all()
        for claim in claims:
            claim.status = status
            claim.released_at = now
            self.session.add(claim)

    def get_session(self, session_id: str) -> dict[str, Any]:
        job = self._get_session_job(session_id)
        payload = job.payload or {}
        result = job.result or {}
        return {
            "session_id": session_id,
            "status": job.status,
            "scope": payload.get("scope") or "all",
            "paper_ids": payload.get("paper_ids") or [],
            "lane_labels": payload.get("lane_labels") or {},
            "selection": result.get("selection") or [],
            "scope_summary": result.get("scope_summary") or {},
            "preparation_rows": result.get("preparation_rows") or [],
            "lanes": result.get("lanes") or self._lane_plan(payload.get("scope") or "all", payload.get("lane_labels") or {}),
            "settlement": result.get("settlement"),
            "created_at": payload.get("created_at"),
        }

    def apply_import_rules_for_paper(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        candidate_run_id: UUID | None = None,
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        # Only a DFT candidate created by this import may require a lock. Pending
        # candidates from older runs must never block unrelated non-DFT writes.
        required_modules = self._required_direct_write_modules_for_paper(
            paper_id,
            candidate_run_id=candidate_run_id,
        )
        lock_check = ModuleWriteLockService(self.session).require_write(
            paper_id=paper_id,
            module_names=required_modules,
            lock_tokens=write_lock_tokens,
            locked_by=write_lock_owner or reviewer,
        )
        low_risk_summary = self._auto_materialize_single_ai_candidates(
            paper_id=paper_id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
            write_lock_owner=write_lock_owner,
        )
        dft_settlement_summary = self.settle_ai_dft_reviews_for_paper(
            paper_id=paper_id,
            reviewer=reviewer,
            candidate_run_id=candidate_run_id,
            write_lock_tokens=write_lock_tokens,
        )
        new_dft_summary = dft_settlement_summary["new_dft_candidates"]
        object_review_summary = self._auto_apply_object_review_candidates(
            paper_id=paper_id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
            exclude_target_types={"dft_results"},
        )
        summary = {
            "paper_id": str(paper_id),
            "new_dft_candidates": new_dft_summary,
            "single_ai": low_risk_summary,
            "object_reviews": {
                "applied_count": dft_settlement_summary["auto_applied_count"],
                "applied_items": dft_settlement_summary["auto_applied_items"],
                "pending_count": (
                    dft_settlement_summary["waiting_second_ai_count"]
                    + dft_settlement_summary["need_third_ai_count"]
                    + dft_settlement_summary["need_repair_count"]
                ),
                "pending_items": (
                    dft_settlement_summary["waiting_second_ai_items"]
                    + dft_settlement_summary["need_third_ai_items"]
                    + dft_settlement_summary["need_repair_items"]
                ),
                "skipped_count": dft_settlement_summary["skipped_count"],
                "skipped_items": dft_settlement_summary["skipped_items"],
            },
            "dft_settlement": dft_settlement_summary,
            "non_dft_object_reviews": object_review_summary,
            "write_lock": {
                "required_modules": lock_check.required_modules,
                "covered_modules": lock_check.covered_modules,
                "lock_ids": lock_check.lock_ids,
            },
        }
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="apply_ide_review_rules",
                source=reviewer,
                target_type="paper",
                target_id=str(paper_id),
                payload=summary,
            )
        )
        self.session.flush()
        return summary

    def settle_ai_dft_reviews_for_paper(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        candidate_run_id: UUID | None = None,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        if candidate_run_id is not None and "dft_results" not in self._required_direct_write_modules_for_paper(
            paper_id,
            candidate_run_id=candidate_run_id,
        ):
            return self._empty_dft_settlement_summary(paper_id)
        new_dft_summary = self._materialize_new_dft_candidates(paper_id=paper_id, reviewer=reviewer)
        rows = self.session.scalars(
            select(DFTResult)
            .where(DFTResult.paper_id == paper_id)
            .order_by(DFTResult.id.asc())
        ).all()
        audits_by_target = self._paper_dft_audit_candidates(paper_id)
        auto_applied: list[dict[str, Any]] = []
        need_third_ai: list[dict[str, Any]] = []
        need_repair: list[dict[str, Any]] = []
        waiting_second_ai: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for row in rows:
            row_id = str(row.id)
            audits = audits_by_target.get(row_id, [])
            if self._has_settled_dft_review(paper_id=paper_id, target_id=row_id):
                if not self._has_pending_dft_adjudication(audits):
                    continue
                if self._consume_matching_settled_dft_adjudication(row=row, audits=audits):
                    auto_applied.append(
                        {
                            "record_id": row_id,
                            "field_name": "dft_results",
                            "property_type": row.property_type,
                            "value": row.value,
                            "unit": row.unit,
                            "action": "already_settled",
                        }
                    )
                    continue
            row_summary = self._settle_dft_row_from_existing_audits(
                row=row,
                audits=audits,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            status = row_summary.pop("status")
            if status == "auto_applied":
                auto_applied.append(row_summary)
            elif status == "need_third_ai":
                need_third_ai.append(row_summary)
            elif status == "need_repair":
                need_repair.append(row_summary)
            elif status == "waiting_second_ai":
                waiting_second_ai.append(row_summary)
            else:
                skipped.append(row_summary)

        self.session.flush()
        summary = {
            "paper_id": str(paper_id),
            "new_dft_candidates": new_dft_summary,
            "auto_applied_count": len(auto_applied),
            "auto_applied_items": auto_applied,
            "need_third_ai_count": len(need_third_ai),
            "need_third_ai_items": need_third_ai,
            "need_repair_count": len(need_repair),
            "need_repair_items": need_repair,
            "waiting_second_ai_count": len(waiting_second_ai),
            "waiting_second_ai_items": waiting_second_ai,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }
        gate_counts = self._dft_settlement_counts(paper_id)
        summary.update(
            {
                "exportable_count": gate_counts["exportable_count"],
                "blocked_reason_counts": gate_counts["blocked_reason_counts"],
                "gate_need_third_ai_count": gate_counts["need_third_ai_count"],
                "gate_need_repair_count": gate_counts["need_repair_count"],
                "gate_waiting_second_ai_count": gate_counts["waiting_second_ai_count"],
            }
        )
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="settle_ai_dft_reviews",
                source=reviewer,
                target_type="paper",
                target_id=str(paper_id),
                payload={
                    "paper_id": str(paper_id),
                    "auto_applied_count": summary["auto_applied_count"],
                    "need_third_ai_count": summary["need_third_ai_count"],
                    "need_repair_count": summary["need_repair_count"],
                    "blocked_reason_counts": summary["blocked_reason_counts"],
                    "exportable_count": summary["exportable_count"],
                    "waiting_second_ai_count": summary["waiting_second_ai_count"],
                },
            )
        )
        self.session.flush()
        return summary

    def _empty_dft_settlement_summary(self, paper_id: UUID) -> dict[str, Any]:
        gate_counts = self._dft_settlement_counts(paper_id)
        empty_candidates = {
            "materialized_count": 0,
            "materialized_items": [],
            "skipped_count": 0,
            "skipped_items": [],
        }
        return {
            "paper_id": str(paper_id),
            "new_dft_candidates": empty_candidates,
            "auto_applied_count": 0,
            "auto_applied_items": [],
            "need_third_ai_count": 0,
            "need_third_ai_items": [],
            "need_repair_count": 0,
            "need_repair_items": [],
            "waiting_second_ai_count": 0,
            "waiting_second_ai_items": [],
            "skipped_count": 0,
            "skipped_items": [],
            "exportable_count": gate_counts["exportable_count"],
            "blocked_reason_counts": gate_counts["blocked_reason_counts"],
            "gate_need_third_ai_count": gate_counts["need_third_ai_count"],
            "gate_need_repair_count": gate_counts["need_repair_count"],
            "gate_waiting_second_ai_count": gate_counts["waiting_second_ai_count"],
        }

    def _materialize_new_dft_candidates(self, *, paper_id: UUID, reviewer: str) -> dict[str, Any]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
                ExternalAnalysisCandidate.status.in_(("candidate", "pending", "requires_resolution")),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        existing_by_signature = self._existing_new_dft_signatures(paper_id)
        existing_by_semantic_signature = self._existing_new_dft_semantic_signatures(paper_id)
        existing_by_method_step_signature = self._existing_new_dft_method_step_signatures(paper_id)
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            decision = str(payload.get("decision") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip().lower()
            if target_type != "dft_results" or (decision != "new_candidate" and target_id != "new"):
                continue
            if bool(payload.get("borrowed_from_reference")):
                skipped.append({"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"})
                self._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")
                continue
            candidate_item, reason = self._new_dft_candidate_item(payload, run=run)
            if candidate_item is None:
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                self._retire_skipped_new_dft_candidate(candidate, reason=reason)
                continue
            signature = candidate_item["signature"]
            existing = existing_by_signature.get(signature)
            if existing is None:
                semantic_matches = existing_by_semantic_signature.get(
                    self._new_dft_semantic_signature(candidate_item),
                    [],
                )
                if len(semantic_matches) == 1:
                    existing = semantic_matches[0]
            if existing is None:
                method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
                if method_step_signature is not None:
                    existing = self._method_step_compatible_existing(
                        candidate_item,
                        existing_by_method_step_signature.get(method_step_signature, []),
                    )
            if existing is None:
                existing = self._insert_new_dft_candidate(
                    paper_id=paper_id,
                    candidate_item=candidate_item,
                    source_label=run.source_label or run.source or reviewer,
                )
                existing_by_signature[signature] = existing
                semantic_signature = self._new_dft_semantic_signature(candidate_item)
                existing_by_semantic_signature.setdefault(semantic_signature, []).append(existing)
                method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
                if method_step_signature is not None:
                    existing_by_method_step_signature.setdefault(method_step_signature, []).append(existing)
                action = "created"
            else:
                self._maybe_upgrade_method_only_reaction_step(existing, candidate_item)
                action = "deduplicated"
            candidate.status = "materialized"
            candidate.materialized_target_type = "dft_results"
            candidate.materialized_target_id = str(existing.id)
            self.session.add(candidate)
            materialized.append(
                {
                    "candidate_id": str(candidate.id),
                    "action": action,
                    "dft_result_id": str(existing.id),
                    "property_type": existing.property_type,
                    "value": existing.value,
                    "unit": existing.unit,
                }
            )
        if materialized:
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="materialize_new_dft_candidates",
                    source=reviewer,
                    target_type="paper",
                    target_id=str(paper_id),
                    payload={
                        "created_or_linked_count": len(materialized),
                        "skipped_count": len(skipped),
                        "policy": "IDE AI new_candidate rows become unverified DFTResult candidates only; they are not exportable/RAG-ready until the existing DFT safety gate passes.",
                    },
                )
            )
        self.session.flush()
        return {
            "materialized_count": len(materialized),
            "materialized_items": materialized,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }

    def _new_dft_candidate_item(
        self,
        payload: dict[str, Any],
        *,
        run: ExternalAnalysisRun,
    ) -> tuple[dict[str, Any] | None, str]:
        corrected = payload.get("corrected_value")
        if not isinstance(corrected, dict):
            return None, "missing_structured_corrected_value"
        material_identity = self._first_text(
            corrected.get("material_identity"),
            corrected.get("material"),
            corrected.get("catalyst"),
            payload.get("normalized_material"),
            payload.get("normalized_material_or_catalyst"),
        )
        property_type = self._normalize_dft_property(
            self._first_text(
                corrected.get("property_type"),
                corrected.get("property"),
                corrected.get("energy_type"),
                payload.get("normalized_energy_type"),
            )
        )
        value = self._float_or_none(corrected.get("value"))
        unit = self._first_text(corrected.get("unit"))
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        if not material_identity:
            return None, "missing_material_identity"
        if not property_type:
            return None, "missing_property_type"
        if value is None:
            return None, "missing_value"
        if not unit:
            return None, "missing_unit"
        if not has_evidence_anchor(evidence):
            return None, "missing_evidence_anchor"
        evidence_payload = evidence if isinstance(evidence, dict) else {"evidence": evidence}
        source_table = self._first_text(corrected.get("source_table"), evidence_payload.get("table"))
        source_section = self._first_text(
            evidence_payload.get("section"),
            evidence_payload.get("section_title"),
            f"Page {evidence_payload.get('page')}" if evidence_payload.get("page") not in (None, "") else None,
        )
        source_figure = self._first_text(corrected.get("source_figure"), source_table, evidence_payload.get("figure"))
        method = self._first_text(corrected.get("method"), corrected.get("calculation_method"))
        temperature = self._first_text(corrected.get("temperature"), corrected.get("temperature_label"))
        reaction_step = self._first_text(
            corrected.get("reaction_step"),
            " | ".join(part for part in [method, temperature] if part),
        )
        adsorbate = self._first_text(corrected.get("adsorbate"), payload.get("adsorbate"), "H2")
        evidence_text = self._first_text(
            evidence_payload.get("quoted_text"),
            evidence_payload.get("evidence_text"),
            payload.get("reason"),
        )
        merged_evidence_payload = {
            **evidence_payload,
            "material_identity": material_identity,
            "source_label": run.source_label,
            "source": run.source,
            "corrected_value": corrected,
            "dedupe_signature": payload.get("dedupe_signature"),
            "import_policy": "new_candidate_unverified_dft_result",
        }
        signature = self._new_dft_signature(
            material_identity=material_identity,
            property_type=property_type,
            value=value,
            unit=unit,
            reaction_step=reaction_step,
            source_figure=source_figure,
            page=evidence_payload.get("page"),
        )
        return (
            {
                "material_identity": material_identity,
                "property_type": property_type,
                "adsorbate": adsorbate,
                "value": value,
                "unit": unit,
                "reaction_step": reaction_step,
                "source_section": source_section,
                "source_figure": source_figure,
                "evidence_text": evidence_text,
                "confidence": payload.get("confidence"),
                "evidence_payload": merged_evidence_payload,
                "signature": signature,
            },
            "",
        )

    def _insert_new_dft_candidate(
        self,
        *,
        paper_id: UUID,
        candidate_item: dict[str, Any],
        source_label: str,
    ) -> DFTResult:
        identity = self._new_dft_identity(candidate_item["signature"])
        existing = self.session.scalar(
            select(DFTResult).where(
                DFTResult.paper_id == paper_id,
                DFTResult.candidate_identity == identity,
            )
        )
        if existing is not None:
            return existing
        row = DFTResult(
            paper_id=paper_id,
            adsorbate=candidate_item["adsorbate"],
            property_type=candidate_item["property_type"],
            value=candidate_item["value"],
            unit=candidate_item["unit"],
            reaction_step=candidate_item["reaction_step"],
            source_section=candidate_item["source_section"],
            source_figure=candidate_item["source_figure"],
            evidence_text=candidate_item["evidence_text"],
            confidence=candidate_item["confidence"],
            candidate_status="new_candidate",
            evidence_payload=candidate_item["evidence_payload"],
            extraction_protocol_version="ide_ai_new_candidate_v1",
            candidate_identity=identity,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            winner = self.session.scalar(
                select(DFTResult).where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.candidate_identity == identity,
                )
            )
            if winner is None:
                raise
            return winner
        self._upsert_new_dft_locator(row, candidate_item["evidence_payload"], source_label=source_label)
        return row

    @staticmethod
    def _new_dft_identity(signature: tuple[str, ...]) -> str:
        canonical = json.dumps(list(signature), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _upsert_new_dft_locator(self, row: DFTResult, evidence_payload: dict[str, Any], *, source_label: str) -> None:
        page = self._int_or_none(evidence_payload.get("page"))
        if page is None:
            return
        locator = EvidenceLocator(
            paper_id=row.paper_id,
            source_type="table" if evidence_payload.get("table") else "pdf",
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            page=page,
            section=evidence_payload.get("section") or evidence_payload.get("section_title") or row.source_section,
            evidence_text=str(evidence_payload.get("quoted_text") or evidence_payload.get("evidence_text") or row.evidence_text or "PDF evidence"),
            locator_status="exact_page",
            locator_confidence=float(row.confidence or 0.8),
            parser_source=str(source_label or "external_ai_review")[:32],
        )
        self.session.add(locator)

    def _existing_new_dft_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], DFTResult]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], DFTResult] = {}
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            signature = self._new_dft_signature(
                material_identity=material_identity,
                property_type=row.property_type,
                value=row.value,
                unit=row.unit,
                reaction_step=row.reaction_step,
                source_figure=row.source_figure,
                page=evidence_payload.get("page"),
            )
            signatures.setdefault(signature, row)
        return signatures

    def _existing_new_dft_semantic_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], list[DFTResult]]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], list[DFTResult]] = defaultdict(list)
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                if sample is not None and str(sample.name or "").strip():
                    material_identity = str(sample.name).strip()
            signature = self._new_dft_semantic_signature(
                {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                }
            )
            signatures[signature].append(row)
        return signatures

    def _existing_new_dft_method_step_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], list[DFTResult]]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], list[DFTResult]] = defaultdict(list)
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                if sample is not None and str(sample.name or "").strip():
                    material_identity = str(sample.name).strip()
            signature = self._new_dft_method_step_compatible_signature(
                {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                }
            )
            if signature is not None:
                signatures[signature].append(row)
        return signatures

    @staticmethod
    def _new_dft_semantic_signature(candidate_item: dict[str, Any]) -> tuple[str, ...]:
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                candidate_item.get("material_identity"),
                candidate_item.get("property_type"),
                value_part,
                candidate_item.get("unit"),
                candidate_item.get("adsorbate"),
                normalize_dft_reaction_step_for_identity(candidate_item.get("reaction_step")),
            )
        )

    @staticmethod
    def _new_dft_method_step_compatible_signature(candidate_item: dict[str, Any]) -> tuple[str, ...] | None:
        property_type = str(candidate_item.get("property_type") or "").strip().lower()
        if property_type != "adsorption_energy":
            return None
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                "method_step_compatible",
                candidate_item.get("material_identity"),
                candidate_item.get("property_type"),
                value_part,
                candidate_item.get("unit"),
                candidate_item.get("adsorbate"),
            )
        )

    @staticmethod
    def _method_step_compatible_existing(candidate_item: dict[str, Any], rows: list[DFTResult]) -> DFTResult | None:
        if not rows:
            return None
        candidate_method_only = is_dft_method_only_reaction_step(candidate_item.get("reaction_step"))
        if candidate_method_only:
            specific_rows = [row for row in rows if not is_dft_method_only_reaction_step(row.reaction_step)]
            candidates = specific_rows or rows
            return candidates[0] if len(candidates) == 1 else None

        method_only_rows = [row for row in rows if is_dft_method_only_reaction_step(row.reaction_step)]
        return method_only_rows[0] if len(rows) == 1 and len(method_only_rows) == 1 else None

    def _maybe_upgrade_method_only_reaction_step(self, row: DFTResult, candidate_item: dict[str, Any]) -> None:
        candidate_step = self._first_text(candidate_item.get("reaction_step"))
        if not candidate_step:
            return
        if is_dft_method_only_reaction_step(candidate_step):
            return
        if not is_dft_method_only_reaction_step(row.reaction_step):
            return
        if str(row.candidate_status or "").strip().lower() != "new_candidate":
            return
        row.reaction_step = candidate_step
        self.session.add(row)

    @staticmethod
    def _new_dft_signature(
        *,
        material_identity: Any,
        property_type: Any,
        value: Any,
        unit: Any,
        reaction_step: Any,
        source_figure: Any,
        page: Any,
    ) -> tuple[str, ...]:
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                material_identity,
                property_type,
                value_part,
                unit,
                normalize_dft_reaction_step_for_identity(reaction_step),
                source_figure,
                page,
            )
        )

    @staticmethod
    def _normalize_dft_property(value: Any) -> str | None:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "activation_energy": "activation_energy",
            "activation": "activation_energy",
            "permeance": "permeance",
            "permeability": "permeance",
            "adsorption_energy": "adsorption_energy",
            "reaction_barrier": "reaction_barrier",
            "permeation_barrier": "permeation_barrier",
        }
        return aliases.get(text, text or None)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _required_direct_write_modules_for_paper(
        self,
        paper_id: UUID,
        *,
        candidate_run_id: UUID | None = None,
    ) -> list[str]:
        stmt = (
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .where(ExternalAnalysisCandidate.status.in_(("candidate", "pending", "requires_resolution")))
        )
        if candidate_run_id is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.run_id == candidate_run_id)
        rows = self.session.execute(stmt).all()
        modules: set[str] = set()
        for candidate, _run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            if candidate.candidate_type != "object_review_audit":
                continue
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            if target_type == "dft_results":
                modules.add("dft_results")
        return sorted(modules)

    def settle_session(self, session_id: str, *, reviewer: str) -> dict[str, Any]:
        job = self._get_session_job(session_id)
        payload = job.payload or {}
        scope = str(payload.get("scope") or "all")
        paper_ids = [UUID(str(item)) for item in payload.get("paper_ids") or []]
        lane_labels = payload.get("lane_labels") or {}

        note_summary = self._settle_low_risk_notes(
            paper_ids=paper_ids,
            single_label=str(lane_labels.get("single") or ""),
            reviewer=reviewer,
        )
        high_risk_summary = self._settle_high_risk_targets(
            paper_ids=paper_ids,
            primary_label=str(lane_labels.get("primary") or ""),
            secondary_label=str(lane_labels.get("secondary") or ""),
            scope=scope,
            reviewer=reviewer,
        )
        settlement = {
            "settled_at": datetime.utcnow().isoformat(),
            "scope": scope,
            "low_risk_notes": note_summary,
            "high_risk": high_risk_summary,
        }
        result = dict(job.result or {})
        result["settlement"] = settlement
        job.result = result
        job.status = "settled"
        job.progress = {
            "completed": True,
            "consistent_auto_adopted": high_risk_summary["auto_applied_count"],
            "single_ai_auto_adopted": note_summary["auto_materialized_count"],
            "manual_conflicts": high_risk_summary["manual_conflict_count"],
        }
        self.session.add(job)
        self._release_session_papers(session_id)
        self.session.add(
            AuditLog(
                action="settle_verification_session",
                source=reviewer,
                target_type="verification_session",
                target_id=session_id,
                payload=settlement,
            )
        )
        self.session.commit()
        return self.get_session(session_id)

    def resolve_conflict(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        resolution: str,
        reviewer: str,
        opinion_source_id: str | None,
    ) -> dict[str, Any]:
        payload = self.conflicts.list_conflicts(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            include_non_conflicts=True,
            limit=50,
        )
        row = next(
            (
                item
                for item in payload.get("rows") or []
                if str(item.get("target_id")) == str(target_id) and str(item.get("field_name")) == str(field_name)
            ),
            None,
        )
        if row is None:
            raise LookupError("Conflict target not found.")
        if resolution == "reject_all":
            result = self._apply_reject_all(paper_id=paper_id, target_type=target_type, target_id=target_id, reviewer=reviewer)
        else:
            if not opinion_source_id:
                raise ValueError("opinion_source_id is required when adopting a specific opinion.")
            opinion = next(
                (item for item in row.get("opinions") or [] if str(item.get("source_id")) == str(opinion_source_id)),
                None,
            )
            if opinion is None:
                raise LookupError("Selected opinion was not found.")
            result = self._apply_selected_opinion(
                paper_id=paper_id,
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=opinion,
            )
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="manual_conflict_resolution",
                source=reviewer,
                target_type=target_type,
                target_id=str(target_id),
                payload={
                    "field_name": field_name,
                    "resolution": resolution,
                    "opinion_source_id": opinion_source_id,
                    "result": result,
                },
            )
        )
        self.session.commit()
        return result

    def _resolve_papers(self, paper_ids: list[UUID], paper_refs: list[str]) -> list[Paper]:
        resolved: dict[str, Paper] = {}
        if paper_ids:
            rows = self.session.scalars(select(Paper).where(Paper.id.in_(paper_ids))).all()
            for paper in rows:
                resolved[str(paper.id)] = paper
        for raw_ref in paper_refs:
            ref = str(raw_ref or "").strip()
            if not ref:
                continue
            by_id = None
            try:
                by_id = self.session.get(Paper, UUID(ref))
            except Exception:
                by_id = None
            if by_id is not None:
                resolved[str(by_id.id)] = by_id
                continue
            stmt = select(Paper).where(
                or_(
                    func.lower(Paper.doi) == ref.lower(),
                    func.lower(Paper.paper_code) == ref.lower(),
                    func.lower(Paper.title) == ref.lower(),
                )
            ).order_by(Paper.created_at.desc(), Paper.id.desc())
            exact = self._select_unambiguous_paper(ref, self.session.scalars(stmt).all())
            if exact is not None:
                resolved[str(exact.id)] = exact
                continue
            fuzzy = self._select_unambiguous_paper(
                ref,
                self.session.scalars(
                    select(Paper)
                    .where(or_(Paper.title.ilike(f"%{ref}%"), Paper.doi.ilike(f"%{ref}%"), Paper.paper_code.ilike(f"%{ref}%")))
                    .order_by(Paper.created_at.desc(), Paper.id.desc())
                ).all(),
            )
            if fuzzy is not None:
                resolved[str(fuzzy.id)] = fuzzy
        return list(resolved.values())

    @staticmethod
    def _select_unambiguous_paper(ref: str, papers: list[Paper]) -> Paper | None:
        if not papers:
            return None
        libraries = {normalize_library_name(paper.library_name) for paper in papers}
        if len(libraries) > 1:
            ordered_libraries = ", ".join(sorted(libraries))
            raise ValueError(
                f"Ambiguous paper reference {ref!r}: matched papers in multiple libraries ({ordered_libraries}). "
                "Use a paper UUID to select the intended paper."
            )
        return papers[0]

    def _paper_summary(self, paper: Paper) -> dict[str, Any]:
        dft_count = self.session.scalar(select(func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper.id)) or 0
        writing_count = self.session.scalar(select(func.count()).select_from(WritingCard).where(WritingCard.paper_id == paper.id)) or 0
        return {
            "paper_id": str(paper.id),
            "paper_code": getattr(paper, "paper_code", None),
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "journal": paper.journal,
            "workspace_prepare_url": f"/api/papers/{paper.id}/prepare-ai-context",
            "codex_context_url": f"/api/papers/{paper.id}/codex-context",
            "read_paper_page_url_template": f"/api/papers/{paper.id}/pages/{{page_no}}",
            "dft_result_count": int(dft_count),
            "writing_card_count": int(writing_count),
        }

    def _scope_summary(self, papers: list[Paper], scope: str) -> dict[str, Any]:
        summaries = [self._paper_summary(paper) for paper in papers]
        return {
            "paper_count": len(summaries),
            "high_risk_target_types": sorted(self.HIGH_RISK_SCOPES.get(scope, set())),
            "low_risk_modes": sorted(self.LOW_RISK_NOTE_SCOPES.get(scope, set())),
            "dft_result_count": sum(item["dft_result_count"] for item in summaries),
            "writing_card_count": sum(item["writing_card_count"] for item in summaries),
        }

    def _lane_plan(self, scope: str, lane_labels: dict[str, str]) -> list[dict[str, Any]]:
        lanes: list[dict[str, Any]] = []
        if self.HIGH_RISK_SCOPES.get(scope):
            lanes.append(
                {
                    "lane": "high_risk_dual_ai",
                    "review_mode": "dual_ai",
                    "source_labels": [lane_labels.get("primary"), lane_labels.get("secondary")],
                    "import_mode": "object_review_audits",
                    "required_fields": ["target_type", "target_id", "field_name", "decision", "corrected_value", "evidence_location"],
                    "instruction": (
                        "First compare the parsed materials with the original PDF page via read_paper_page, then use "
                        "MCP /api/papers/{paper_id}/codex-context and /codex-item to inspect evidence. "
                        "Record parse defects if the system split tables/figures/locators incorrectly, then import "
                        "object_review_audits through import_analysis with the assigned source_label."
                    ),
                }
            )
        if self.LOW_RISK_NOTE_SCOPES.get(scope):
            lanes.append(
                {
                    "lane": "low_risk_single_ai",
                    "review_mode": "single_ai",
                    "source_labels": [lane_labels.get("single")],
                    "import_mode": "review_notes",
                    "required_fields": ["content", "field_name", "page|section_title|quoted_text"],
                    "instruction": (
                        "Import writing summaries as review_notes with page, section_title, or quoted_text so the backend can "
                        "auto-materialize only anchored notes."
                    ),
                }
            )
        return lanes

    def _settle_low_risk_notes(self, *, paper_ids: list[UUID], single_label: str, reviewer: str) -> dict[str, Any]:
        if not paper_ids or not single_label:
            return {"eligible_note_count": 0, "auto_materialized_count": 0, "skipped_note_count": 0, "materialized_note_ids": []}
        runs = self.session.scalars(
            select(ExternalAnalysisRun).where(
                ExternalAnalysisRun.paper_id.in_(paper_ids),
                ExternalAnalysisRun.source_label == single_label,
            )
        ).all()
        run_ids = [run.id for run in runs]
        if not run_ids:
            return {"eligible_note_count": 0, "auto_materialized_count": 0, "skipped_note_count": 0, "materialized_note_ids": []}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.run_id.in_(run_ids),
                ExternalAnalysisCandidate.candidate_type == "note",
            )
        ).all()
        eligible = [candidate for candidate in candidates if self._note_has_anchor(candidate)]
        materialized_note_ids: list[str] = []
        auto_materialized = 0
        skipped = 0
        external_service = ExternalAnalysisService(self.session, self.settings)
        for candidate in eligible:
            if candidate.status not in {"pending", "requires_resolution"}:
                skipped += 1
                continue
            result = external_service.materialize_candidates(
                run_id=candidate.run_id,
                candidate_ids=[candidate.id],
                explicit_all=False,
                created_by="verification_session",
            )
            auto_materialized += result.created_notes
            if candidate.materialized_target_id:
                materialized_note_ids.append(str(candidate.materialized_target_id))
            note = self.session.get(PaperNote, UUID(str(candidate.materialized_target_id))) if candidate.materialized_target_id else None
            self.session.add(
                AuditLog(
                    paper_id=candidate.paper_id,
                    action="single_ai_auto_materialize_note",
                    source=reviewer,
                    target_type="paper_note",
                    target_id=str(candidate.materialized_target_id) if candidate.materialized_target_id else None,
                    payload={
                        "candidate_id": str(candidate.id),
                        "review_mode": "single_ai",
                        "single_ai_auto_adopted": True,
                        "evidence_attached": True,
                        "source_label": single_label,
                        "field_name": (candidate.normalized_payload or {}).get("field_name"),
                        "page": getattr(note, "page", None),
                        "section_title": getattr(note, "section_title", None),
                    },
                )
            )
        self.session.flush()
        return {
            "eligible_note_count": len(eligible),
            "auto_materialized_count": auto_materialized,
            "skipped_note_count": skipped + max(0, len(candidates) - len(eligible)),
            "materialized_note_ids": materialized_note_ids,
        }

    def _auto_materialize_single_ai_candidates(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type.in_(("note", "correction")),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        external_service = ExternalAnalysisService(self.session, self.settings)
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.status not in {"pending", "requires_resolution"}:
                skipped.append({"candidate_id": str(candidate.id), "reason": f"status={candidate.status}"})
                continue
            if candidate.candidate_type == "note" and not self._note_has_anchor(candidate):
                skipped.append({"candidate_id": str(candidate.id), "reason": "missing_evidence_anchor"})
                continue
            if candidate.candidate_type == "correction" and not self._correction_candidate_has_anchor(candidate):
                skipped.append({"candidate_id": str(candidate.id), "reason": "missing_evidence_anchor"})
                continue
            result = external_service.materialize_candidates(
                run_id=candidate.run_id,
                candidate_ids=[candidate.id],
                explicit_all=False,
                created_by=reviewer,
            )
            approved_status = None
            if candidate.candidate_type == "correction" and candidate.materialized_target_id:
                approved = ReviewService(self.session).approve_correction(
                    UUID(str(candidate.materialized_target_id)),
                    reviewer=reviewer,
                    write_lock_tokens=write_lock_tokens,
                    write_lock_owner=write_lock_owner,
                )
                approved_status = approved.status
            materialized.append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate_type": candidate.candidate_type,
                    "materialized_target_type": candidate.materialized_target_type,
                    "materialized_target_id": candidate.materialized_target_id,
                    "approved_status": approved_status,
                    "created_notes": result.created_notes,
                    "created_corrections": result.created_corrections,
                }
            )
        self.session.flush()
        return {
            "materialized_count": len(materialized),
            "materialized_items": materialized,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }

    def _auto_apply_object_review_candidates(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        write_lock_tokens: list[str] | None = None,
        include_target_types: set[str] | None = None,
        exclude_target_types: set[str] | None = None,
    ) -> dict[str, Any]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            if include_target_types is not None and target_type not in include_target_types:
                continue
            if exclude_target_types is not None and target_type in exclude_target_types:
                continue
            target_id = str(payload.get("target_id") or "").strip()
            field_name = str(payload.get("field_name") or "").strip()
            if not target_type or not target_id or not field_name:
                continue
            if target_type == "dft_results" and (
                target_id.lower() == "new"
                or str(payload.get("decision") or "").strip().lower() == "new_candidate"
                or bool(payload.get("borrowed_from_reference"))
            ):
                continue
            identity = str(
                payload.get("source_label")
                or run.source_label
                or payload.get("source")
                or run.source
                or candidate.id
            ).strip()
            grouped[(target_type, target_id, field_name)].append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate": candidate,
                    "paper_id": str(candidate.paper_id),
                    "target_type": target_type,
                    "target_id": target_id,
                    "field_name": field_name,
                    "decision": str(payload.get("decision") or "").upper(),
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "normalized_material": payload.get("normalized_material"),
                    "normalized_material_or_catalyst": payload.get("normalized_material_or_catalyst"),
                    "material": payload.get("material"),
                    "catalyst": payload.get("catalyst"),
                    "structure_name": payload.get("structure_name"),
                    "adsorbate": payload.get("adsorbate"),
                    "reaction_step": payload.get("reaction_step"),
                    "normalized_energy_type": payload.get("normalized_energy_type"),
                    "source_label": run.source_label,
                    "source": payload.get("source") or run.source,
                    "source_identity": identity,
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "adjudication_role": payload.get("adjudication_role"),
                    "adjudication_scope": payload.get("adjudication_scope"),
                    "selected_source_ids": payload.get("selected_source_ids"),
                    "raw_payload": payload,
                }
            )

        applied: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for (target_type, target_id, field_name), opinions in grouped.items():
            deduped: dict[str, dict[str, Any]] = {}
            for opinion in opinions:
                if opinion["candidate"].status not in {"candidate", "pending", "requires_resolution"}:
                    continue
                key = opinion["candidate_id"] if target_type == "dft_results" else opinion["source_identity"]
                current = deduped.get(key)
                if current is None or (opinion.get("confidence") or 0) >= (current.get("confidence") or 0):
                    deduped[key] = opinion
            eligible = [item for item in deduped.values() if self._opinion_has_anchor(item)]
            third_ai = [item for item in eligible if str(item.get("adjudication_role") or "").strip().lower() == "third_ai"]
            resolves_as_rejected = (
                any(self._is_negative_dft_decision(item.get("decision")) for item in third_ai)
                or (
                    len(eligible) >= 2
                    and all(self._is_negative_dft_decision(item.get("decision")) for item in eligible)
                )
            )
            if target_type == "dft_results" and any(self._is_project_library_v4_opinion(item) for item in eligible):
                pending.append(
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": self.PROJECT_LIBRARY_V4_USER_SUBMIT_REASON,
                        "eligible_opinion_count": len(eligible),
                    }
                )
                continue
            if target_type == "dft_results" and eligible and not all(
                self._dft_has_material_identity(item, target_id=target_id, field_name=field_name)
                for item in eligible
            ) and not resolves_as_rejected:
                pending.append(
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": "missing_dft_material_identity",
                        "eligible_opinion_count": len(eligible),
                    }
                )
                continue
            if target_type in self.HIGH_RISK_IDE_TARGET_TYPES:
                if third_ai:
                    adopted = max(third_ai, key=lambda item: item.get("confidence") or 0)
                elif len(eligible) < 2:
                    pending.append(
                        {
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": "awaiting_two_ai_reviews",
                            "eligible_opinion_count": len(eligible),
                        }
                    )
                    continue
                else:
                    signature_groups = {
                        self._review_consensus_key(item, target_type=target_type, target_id=target_id, field_name=field_name): item
                        for item in eligible
                    }
                    if len(signature_groups) != 1:
                        pending.append(
                            {
                                "target_type": target_type,
                                "target_id": target_id,
                                "field_name": field_name,
                                "reason": self._consensus_disagreement_reason(
                                    eligible,
                                    target_type=target_type,
                                    target_id=target_id,
                                    field_name=field_name,
                                ),
                                "eligible_opinion_count": len(eligible),
                            }
                        )
                        continue
                    adopted = max(eligible, key=lambda item: item.get("confidence") or 0)
            else:
                if not eligible:
                    skipped.append(
                        {
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": "missing_evidence_anchor",
                        }
                    )
                    continue
                signature_groups = {
                    self._review_consensus_key(item, target_type=target_type, target_id=target_id, field_name=field_name): item
                    for item in eligible
                }
                if not third_ai and len(signature_groups) != 1:
                    pending.append(
                        {
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": self._consensus_disagreement_reason(
                                eligible,
                                target_type=target_type,
                                target_id=target_id,
                                field_name=field_name,
                            ),
                            "eligible_opinion_count": len(eligible),
                        }
                    )
                    continue
                adopted = max(third_ai, key=lambda item: item.get("confidence") or 0) if third_ai else max(eligible, key=lambda item: item.get("confidence") or 0)
            try:
                result = self._apply_selected_opinion(
                    paper_id=paper_id,
                    target_type=target_type,
                    target_id=target_id,
                    field_name=field_name,
                    reviewer=reviewer,
                    opinion=adopted,
                    dual_ai_consensus=target_type in self.HIGH_RISK_IDE_TARGET_TYPES,
                    adjudicated_by_third_ai=bool(third_ai and adopted in third_ai),
                    write_lock_tokens=write_lock_tokens,
                )
            except Exception as exc:
                skipped.append(
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": str(exc),
                    }
                )
                continue
            materialized_target_type, materialized_target_id = self._materialized_target_ref(result)
            for opinion in opinions:
                opinion["candidate"].status = self._object_review_candidate_status_for_result(result)
                opinion["candidate"].materialized_target_type = materialized_target_type
                opinion["candidate"].materialized_target_id = materialized_target_id
                self.session.add(opinion["candidate"])
            applied.append(
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "field_name": field_name,
                    "action": result.get("action"),
                    "materialized_target_type": materialized_target_type,
                    "materialized_target_id": materialized_target_id,
                    "dual_ai_required": target_type in self.HIGH_RISK_IDE_TARGET_TYPES,
                    "adjudication_role": adopted.get("adjudication_role"),
                }
            )
        self.session.flush()
        return {
            "applied_count": len(applied),
            "applied_items": applied,
            "pending_count": len(pending),
            "pending_items": pending,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }

    def _paper_dft_audit_candidates(self, paper_id: UUID) -> dict[str, list[dict[str, Any]]]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc(), ExternalAnalysisCandidate.id.asc())
        ).all()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            target_id = str(payload.get("target_id") or "").strip()
            decision = str(payload.get("decision") or "").strip().lower()
            if (
                target_type == "dft_results"
                and (target_id.lower() == "new" or decision == "new_candidate")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                # Repeated new_candidate imports for the same missing row should
                # participate in later DFT settlement against the materialized row.
                target_id = str(candidate.materialized_target_id).strip()
            if target_type != "dft_results" or not target_id or target_id.lower() == "new":
                continue
            grouped[target_id].append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate": candidate,
                    "target_id": target_id,
                    "field_name": str(payload.get("field_name") or "").strip(),
                    "decision": str(payload.get("decision") or "").strip().upper(),
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "normalized_material": payload.get("normalized_material"),
                    "normalized_material_or_catalyst": payload.get("normalized_material_or_catalyst"),
                    "material": payload.get("material"),
                    "catalyst": payload.get("catalyst"),
                    "structure_name": payload.get("structure_name"),
                    "adsorbate": payload.get("adsorbate"),
                    "reaction_step": payload.get("reaction_step"),
                    "normalized_energy_type": payload.get("normalized_energy_type"),
                    "source_label": str(payload.get("source_label") or run.source_label or run.source or "").strip(),
                    "source": str(payload.get("source") or run.source or "").strip(),
                    "source_identity": str(
                        payload.get("source_label")
                        or run.source_label
                        or payload.get("source")
                        or run.source
                        or candidate.id
                    ).strip(),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "adjudication_role": payload.get("adjudication_role"),
                    "adjudication_scope": payload.get("adjudication_scope"),
                    "selected_source_ids": payload.get("selected_source_ids"),
                    "raw_payload": payload,
                    "status": candidate.status,
                }
            )
        return grouped

    def _settle_dft_row_from_existing_audits(
        self,
        *,
        row: DFTResult,
        audits: list[dict[str, Any]],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        row_ref = {
            "record_id": str(row.id),
            "field_name": "value",
            "property_type": row.property_type,
            "value": row.value,
            "unit": row.unit,
        }
        if not audits:
            row_ref["reason"] = "awaiting_two_ai_reviews"
            row_ref["status"] = "waiting_second_ai"
            return row_ref

        deduped: dict[str, dict[str, Any]] = {}
        for audit in audits:
            if audit["status"] not in {"candidate", "pending", "requires_resolution", "materialized"}:
                continue
            submission_id = str(audit.get("candidate_id") or "").strip()
            if not submission_id:
                continue
            current = deduped.get(submission_id)
            if current is None or (audit.get("confidence") or 0) >= (current.get("confidence") or 0):
                deduped[submission_id] = audit
        opinions = list(deduped.values())
        opinions = [self._inherit_selected_dft_evidence(opinion, opinions) for opinion in opinions]
        anchored = [audit for audit in opinions if self._opinion_has_anchor(audit)]
        if opinions and not anchored:
            row_ref["reason"] = "missing_evidence_anchor"
            row_ref["status"] = "need_repair"
            return row_ref

        if any(self._is_project_library_v4_opinion(audit) for audit in anchored):
            row_ref["reason"] = self.PROJECT_LIBRARY_V4_USER_SUBMIT_REASON
            row_ref["eligible_opinion_count"] = len(anchored)
            row_ref["status"] = "need_repair"
            return row_ref

        third_ai = [
            audit for audit in anchored
            if str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
        ]
        if third_ai:
            adopted = max(third_ai, key=lambda item: item.get("confidence") or 0)
            if self._is_negative_dft_decision(adopted.get("decision")):
                result = self._apply_reject_all(
                    paper_id=row.paper_id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    reviewer=reviewer,
                )
            else:
                adopted = self._complete_dft_third_ai_adjudication(row, adopted, anchored)
                result = self._apply_dft_consensus_outcome(
                    row=row,
                    adopted=adopted,
                    reviewer=reviewer,
                    write_lock_tokens=write_lock_tokens,
                )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(result)
            row_ref["status"] = "auto_applied"
            return row_ref

        if len(anchored) < 2:
            row_ref["reason"] = "awaiting_two_ai_reviews"
            row_ref["eligible_opinion_count"] = len(anchored)
            row_ref["status"] = "waiting_second_ai"
            return row_ref

        if all(self._is_negative_dft_decision(audit.get("decision")) for audit in anchored):
            result = self._apply_reject_all(
                paper_id=row.paper_id,
                target_type="dft_results",
                target_id=str(row.id),
                reviewer=reviewer,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(
                {
                    "action": result.get("action"),
                    "review_result": result.get("result"),
                }
            )
            row_ref["status"] = "auto_applied"
            return row_ref

        has_reject = any(self._is_negative_dft_decision(audit.get("decision")) for audit in anchored)
        has_positive = any(str(audit.get("decision") or "").strip().upper() in {"PASS", "PROPOSED"} for audit in anchored)
        if has_reject and has_positive:
            row_ref["reason"] = "decision_conflict"
            row_ref["status"] = "need_third_ai"
            return row_ref

        whole_row = self._latest_dft_whole_row_proposal(anchored)
        supporting_pass = self._supporting_pass_for_row(row, anchored, whole_row)
        if whole_row and supporting_pass and self._all_nonnegative_dft_opinions_match(row, anchored):
            result = self._apply_dft_whole_row_consensus(
                row=row,
                proposal=whole_row,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(result)
            row_ref["status"] = "auto_applied"
            return row_ref
        if whole_row:
            row_ref["reason"] = "value_conflict"
            row_ref["status"] = "need_third_ai"
            return row_ref

        supported_field_proposal = self._latest_supported_dft_field_proposal(row, anchored)
        if supported_field_proposal is not None:
            proposal, supporting_pass = supported_field_proposal
            result = self._apply_dft_whole_row_consensus(
                row=row,
                proposal=self._synthesize_dft_whole_row_proposal(
                    row=row,
                    proposal=proposal,
                    supporting_pass=supporting_pass,
                ),
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(result)
            row_ref["status"] = "auto_applied"
            return row_ref

        pass_values = [audit for audit in anchored if str(audit.get("decision") or "").strip().upper() == "PASS"]
        if len(pass_values) >= 2 and self._all_pass_opinions_match(row, pass_values):
            if not self._dft_material_identities_compatible(
                pass_values,
                target_id=str(row.id),
                field_name="value",
            ):
                row_ref["reason"] = "material_identity_conflict"
                row_ref["status"] = "need_repair"
                return row_ref
            adopted = max(pass_values, key=lambda item: item.get("confidence") or 0)
            result = self._apply_dft_consensus_outcome(
                row=row,
                adopted=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(result)
            row_ref["status"] = "auto_applied"
            return row_ref

        if any(not self._dft_has_material_identity(audit, target_id=str(row.id), field_name=str(audit.get("field_name") or "")) for audit in anchored):
            row_ref["reason"] = "missing_dft_material_identity"
            row_ref["status"] = "need_repair"
            return row_ref

        identity_keys = {
            self._dft_identity_key(
                audit,
                target_id=str(row.id),
                field_name=str(audit.get("field_name") or ""),
            )
            for audit in anchored
        }
        if len(identity_keys) > 1:
            row_ref["reason"] = "material_identity_conflict"
            row_ref["status"] = "need_repair"
            return row_ref

        same_field_consensus = {
            self._review_consensus_key(
                audit,
                target_type="dft_results",
                target_id=str(row.id),
                field_name=str(audit.get("field_name") or ""),
            ): audit
            for audit in anchored
            if str(audit.get("field_name") or "").strip() not in {"", "dft_results"}
            and not self._is_negative_dft_decision(audit.get("decision"))
        }
        if len(same_field_consensus) == 1 and len(anchored) >= 2:
            adopted = max(same_field_consensus.values(), key=lambda item: item.get("confidence") or 0)
            result = self._apply_dft_consensus_outcome(
                row=row,
                adopted=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            for audit in audits:
                audit["candidate"].status = self._object_review_candidate_status_for_result(result)
                self.session.add(audit["candidate"])
            row_ref.update(result)
            row_ref["status"] = "auto_applied"
            return row_ref

        row_ref["reason"] = "value_conflict"
        row_ref["status"] = "need_third_ai"
        return row_ref

    def _apply_dft_consensus_outcome(
        self,
        *,
        row: DFTResult,
        adopted: dict[str, Any],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        field_name = str(adopted.get("field_name") or "value").strip() or "value"
        if field_name == "dft_results":
            return self._apply_dft_whole_row_consensus(
                row=row,
                proposal=adopted,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
        result = self._apply_dft_opinion(
            paper_id=row.paper_id,
            target_id=str(row.id),
            field_name=field_name,
            reviewer=reviewer,
            opinion=adopted,
            dual_ai_consensus=True,
            adjudicated_by_third_ai=str(adopted.get("adjudication_role") or "").strip().lower() == "third_ai",
            evidence_payload=self._materialize_evidence_payload(adopted),
            write_lock_tokens=write_lock_tokens,
        )
        return {
            "action": result.get("action"),
            "review_result": result.get("result"),
        }

    def _apply_dft_whole_row_consensus(
        self,
        *,
        row: DFTResult,
        proposal: dict[str, Any],
        reviewer: str,
        write_lock_tokens: list[str] | None,
    ) -> dict[str, Any]:
        corrected = proposal.get("corrected_value")
        if not isinstance(corrected, dict):
            raise ValueError("Whole-row DFT consensus is missing corrected_value.")
        evidence_payload = self._materialize_evidence_payload(proposal)
        self._apply_dft_material_binding_if_needed(
            row=row,
            opinion=proposal,
            reviewer=reviewer,
            evidence_payload=evidence_payload,
            write_lock_tokens=write_lock_tokens,
        )
        self.session.flush()
        self.session.refresh(row)
        for source_field, target_field in (
            ("property_type", "property_type"),
            ("property", "property_type"),
            ("energy_type", "property_type"),
            ("adsorbate", "adsorbate"),
            ("reaction_step", "reaction_step"),
            ("unit", "unit"),
            ("value", "value"),
        ):
            if source_field not in corrected:
                continue
            proposed_value = corrected.get(source_field)
            current_value = getattr(row, target_field, None)
            if self._value_key(proposed_value) == self._value_key(current_value):
                continue
            self._apply_structured_correction(
                paper_id=row.paper_id,
                target_type="dft_results",
                target_id=str(row.id),
                field_name=target_field,
                reviewer=reviewer,
                proposed_value=proposed_value,
                evidence_payload=evidence_payload,
                dual_ai_consensus=True,
                adjudicated_by_third_ai=str(proposal.get("adjudication_role") or "").strip().lower() == "third_ai",
                write_lock_tokens=write_lock_tokens,
            )
            self.session.flush()
            self.session.refresh(row)
        verify_value = corrected.get("value", row.value)
        verify_opinion = {
            **proposal,
            "field_name": "value",
            "decision": "PASS",
            "corrected_value": verify_value,
        }
        result = self._apply_dft_opinion(
            paper_id=row.paper_id,
            target_id=str(row.id),
            field_name="value",
            reviewer=reviewer,
            opinion=verify_opinion,
            dual_ai_consensus=True,
            adjudicated_by_third_ai=str(proposal.get("adjudication_role") or "").strip().lower() == "third_ai",
            evidence_payload=evidence_payload,
            write_lock_tokens=write_lock_tokens,
        )
        return {
            "action": result.get("action"),
            "review_result": result.get("result"),
        }

    def _dft_settlement_counts(self, paper_id: UUID) -> dict[str, Any]:
        from app.services.dft_review_queue_service import DFTReviewQueueService

        queue = DFTReviewQueueService(self.session).list_queue(
            paper_id=paper_id,
            status="all",
            limit=1000,
        )
        rows = list(queue.get("rows") or [])
        blocked_reason_counts = dict((queue.get("metadata") or {}).get("blocked_reasons") or {})
        need_repair_count = 0
        need_third_ai_count = 0
        waiting_second_ai_count = 0
        for row in rows:
            reasons = set(row.get("blocked_reasons") or [])
            audits = row.get("object_review_audits") or []
            anchored = [audit for audit in audits if self._opinion_has_anchor({"evidence_payload": audit.get("evidence_location")})]
            if row.get("is_exportable"):
                continue
            if reasons & {"missing_material_identity", "missing_evidence", "missing_evidence_text", "unsafe_locator"}:
                need_repair_count += 1
                continue
            if len(anchored) < 2 and audits:
                waiting_second_ai_count += 1
                continue
            if audits:
                need_third_ai_count += 1
        return {
            "exportable_count": sum(1 for row in rows if row.get("is_exportable")),
            "blocked_reason_counts": blocked_reason_counts,
            "need_third_ai_count": need_third_ai_count,
            "need_repair_count": need_repair_count,
            "waiting_second_ai_count": waiting_second_ai_count,
        }

    def _has_settled_dft_review(self, *, paper_id: UUID, target_id: str) -> bool:
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(target_id),
            )
        ).all()
        return any(
            is_safe_verified_review(review)
            or str(review.reviewer_status or "").strip().lower() == "rejected"
            for review in reviews
        )

    @staticmethod
    def _has_pending_dft_adjudication(audits: list[dict[str, Any]]) -> bool:
        return any(
            str(audit.get("status") or "").strip().lower() in {"candidate", "pending", "requires_resolution"}
            and str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
            for audit in audits
        )

    def _consume_matching_settled_dft_adjudication(
        self,
        *,
        row: DFTResult,
        audits: list[dict[str, Any]],
    ) -> bool:
        eligible = [
            audit
            for audit in audits
            if str(audit.get("status") or "").strip().lower()
            in {"candidate", "pending", "requires_resolution", "materialized"}
        ]
        adjudications = [
            audit
            for audit in eligible
            if str(audit.get("adjudication_role") or "").strip().lower() == "third_ai"
        ]
        if not adjudications:
            return False
        adopted = max(adjudications, key=lambda item: item.get("confidence") or 0)
        adopted = self._complete_dft_third_ai_adjudication(row, adopted, eligible)
        if not self._dft_adjudication_matches_row(row=row, adjudication=adopted):
            return False
        for audit in audits:
            if str(audit.get("status") or "").strip().lower() not in {"candidate", "pending", "requires_resolution"}:
                continue
            candidate = audit.get("candidate")
            if candidate is None:
                continue
            candidate.status = "ai_reviewed"
            self.session.add(candidate)
        return True

    def _dft_adjudication_matches_row(self, *, row: DFTResult, adjudication: dict[str, Any]) -> bool:
        corrected = adjudication.get("corrected_value")
        if not isinstance(corrected, dict):
            return False
        current_target = {"corrected_value": {"value": row.value, "unit": row.unit}, "field_name": "dft_results"}
        proposed_target = self._normalized_dft_audit_target(row, adjudication)
        if not self._same_normalized_dft_value(
            self._normalized_dft_audit_target(row, current_target),
            proposed_target,
        ):
            return False
        for source_fields, target_field in (
            (("property_type", "property", "energy_type"), "property_type"),
            (("adsorbate",), "adsorbate"),
            (("reaction_step",), "reaction_step"),
        ):
            proposed_value = next((corrected.get(key) for key in source_fields if key in corrected), None)
            if self._value_key(proposed_value) != self._value_key(getattr(row, target_field, None)):
                return False
        proposed_material = self._dft_material_identity_value(
            adjudication,
            target_id=str(row.id),
            field_name="dft_results",
        )
        if proposed_material:
            current_material = ""
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                current_material = str(sample.name or "").strip() if sample is not None else ""
            if not current_material or not self._material_identity_values_compatible(proposed_material, current_material):
                return False
        return True

    @staticmethod
    def _is_negative_dft_decision(decision: Any) -> bool:
        return str(decision or "").strip().upper() in {"REJECT", "REJECTED", "BLOCK", "DENY", "DROP"}

    @staticmethod
    def _latest_dft_whole_row_proposal(audits: list[dict[str, Any]]) -> dict[str, Any] | None:
        proposals = [
            audit for audit in audits
            if str(audit.get("decision") or "").strip().upper() in {"PROPOSED", "REVISE", "NEW_CANDIDATE"}
            and str(audit.get("field_name") or "").strip() == "dft_results"
            and isinstance(audit.get("corrected_value"), dict)
        ]
        if not proposals:
            return None
        proposals.sort(
            key=lambda item: (item.get("confidence") or 0, str(item.get("candidate_id") or "")),
            reverse=True,
        )
        return proposals[0]

    def _complete_dft_third_ai_adjudication(
        self,
        row: DFTResult,
        adopted: dict[str, Any],
        audits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_ids = {
            str(item).strip()
            for item in (adopted.get("selected_source_ids") or [])
            if str(item).strip()
        }
        selected = [
            audit for audit in audits
            if selected_ids & {
                str(audit.get("candidate_id") or "").strip(),
                str(audit.get("source_identity") or "").strip(),
                str(audit.get("source_label") or "").strip(),
            }
        ]
        corrected: dict[str, Any] = {
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "reaction_step": row.reaction_step,
            "value": row.value,
            "unit": row.unit,
        }
        for opinion in selected:
            payload = opinion.get("corrected_value")
            if isinstance(payload, dict):
                corrected.update({key: value for key, value in payload.items() if value not in (None, "")})
        adopted_corrected = adopted.get("corrected_value")
        if isinstance(adopted_corrected, dict):
            corrected.update({key: value for key, value in adopted_corrected.items() if value not in (None, "")})
        elif adopted_corrected not in (None, ""):
            adopted_field = str(adopted.get("field_name") or "").strip()
            mapped_field = self.DFT_FIELD_ALIASES.get(adopted_field, adopted_field)
            if mapped_field in corrected:
                corrected[mapped_field] = adopted_corrected

        material_identity = next(
            (
                value for value in (
                    corrected.get("material_identity"),
                    corrected.get("material"),
                    corrected.get("catalyst"),
                    adopted.get("normalized_material"),
                    adopted.get("normalized_material_or_catalyst"),
                )
                if value not in (None, "")
            ),
            None,
        )
        if material_identity is None and row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            if sample is not None and str(sample.name or "").strip():
                corrected["material_identity"] = sample.name

        evidence_payload = adopted.get("evidence_payload")
        if not self._opinion_has_anchor({"evidence_payload": evidence_payload}):
            evidence_payload = next(
                (audit.get("evidence_payload") for audit in selected if self._opinion_has_anchor(audit)),
                adopted.get("evidence_payload"),
            )
        return {
            **adopted,
            "field_name": "dft_results",
            "corrected_value": corrected,
            "evidence_payload": evidence_payload,
        }

    def _inherit_selected_dft_evidence(
        self,
        opinion: dict[str, Any],
        opinions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._opinion_has_anchor(opinion):
            return opinion
        if str(opinion.get("adjudication_role") or "").strip().lower() != "third_ai":
            return opinion
        selected_ids = {
            str(item).strip()
            for item in (opinion.get("selected_source_ids") or [])
            if str(item).strip()
        }
        if not selected_ids:
            return opinion
        selected = next(
            (
                candidate for candidate in opinions
                if candidate is not opinion
                and self._opinion_has_anchor(candidate)
                and selected_ids & {
                    str(candidate.get("candidate_id") or "").strip(),
                    str(candidate.get("source_identity") or "").strip(),
                    str(candidate.get("source_label") or "").strip(),
                }
            ),
            None,
        )
        if selected is None:
            return opinion
        return {**opinion, "evidence_payload": selected.get("evidence_payload")}

    def _supporting_pass_for_row(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
        proposal: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if proposal is None:
            return None
        proposed_target = self._normalized_dft_audit_target(row, proposal)
        for audit in audits:
            decision = str(audit.get("decision") or "").strip().upper()
            if decision not in {"PASS", "PROPOSED", "REVISE", "NEW_CANDIDATE"}:
                continue
            if self._same_dft_review_submission(audit, proposal):
                continue
            field_name = str(audit.get("field_name") or "").strip()
            if field_name == "value":
                if not self._same_normalized_dft_value(proposed_target, self._normalized_dft_audit_target(row, audit)):
                    continue
            elif field_name == "dft_results":
                if not self._same_normalized_dft_value(
                    proposed_target,
                    self._normalized_dft_audit_target(row, audit),
                ):
                    continue
            else:
                continue
            left_material = self._dft_material_identity_value(proposal, target_id=str(row.id))
            right_material = self._dft_material_identity_value(audit, target_id=str(row.id))
            if left_material and right_material and not self._material_identity_values_compatible(
                left_material,
                right_material,
            ):
                continue
            return audit
        return None

    def _latest_supported_dft_field_proposal(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        proposals = [
            audit for audit in audits
            if str(audit.get("decision") or "").strip().upper() in {"PROPOSED", "NEW_CANDIDATE"}
            and str(audit.get("field_name") or "").strip() not in {"", "dft_results"}
        ]
        proposals.sort(
            key=lambda item: (item.get("confidence") or 0, str(item.get("candidate_id") or "")),
            reverse=True,
        )
        for proposal in proposals:
            supporting_pass = self._supporting_value_pass_for_field_proposal(row, audits, proposal)
            if supporting_pass is not None:
                return proposal, supporting_pass
        return None

    def _supporting_value_pass_for_field_proposal(
        self,
        row: DFTResult,
        audits: list[dict[str, Any]],
        proposal: dict[str, Any],
    ) -> dict[str, Any] | None:
        proposal_field = str(proposal.get("field_name") or "").strip()
        row_value_target = {"value": row.value, "unit": str(row.unit or "").strip().lower().replace(" ", "")}
        for audit in audits:
            if str(audit.get("decision") or "").strip().upper() != "PASS":
                continue
            if str(audit.get("field_name") or "").strip() != "value":
                continue
            if self._same_dft_review_submission(audit, proposal):
                continue
            if not self._material_identity_values_compatible(
                self._dft_material_identity_value(proposal, target_id=str(row.id)),
                self._dft_material_identity_value(audit, target_id=str(row.id)),
            ):
                continue
            if proposal_field == "value":
                if not self._same_normalized_dft_value(
                    self._normalized_dft_audit_target(row, proposal),
                    self._normalized_dft_audit_target(row, audit),
                ):
                    continue
            elif not self._same_normalized_dft_value(row_value_target, self._normalized_dft_audit_target(row, audit)):
                continue
            return audit
        return None

    @staticmethod
    def _same_dft_review_submission(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_id = str(left.get("candidate_id") or "").strip()
        right_id = str(right.get("candidate_id") or "").strip()
        if left_id and right_id:
            return left_id == right_id
        return left is right

    def _synthesize_dft_whole_row_proposal(
        self,
        *,
        row: DFTResult,
        proposal: dict[str, Any],
        supporting_pass: dict[str, Any],
    ) -> dict[str, Any]:
        corrected = {
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "reaction_step": row.reaction_step,
            "unit": row.unit,
            "value": row.value,
        }
        supporting_corrected = supporting_pass.get("corrected_value")
        if isinstance(supporting_corrected, dict):
            if supporting_corrected.get("unit") not in (None, ""):
                corrected["unit"] = supporting_corrected.get("unit")
            if supporting_corrected.get("value") not in (None, ""):
                corrected["value"] = supporting_corrected.get("value")
        elif supporting_corrected not in (None, ""):
            corrected["value"] = supporting_corrected
        mapped_field = self.DFT_FIELD_ALIASES.get(str(proposal.get("field_name") or "").strip(), str(proposal.get("field_name") or "").strip())
        corrected[mapped_field] = proposal.get("corrected_value")
        return {
            **proposal,
            "field_name": "dft_results",
            "corrected_value": corrected,
        }

    def _all_pass_opinions_match(self, row: DFTResult, audits: list[dict[str, Any]]) -> bool:
        normalized = [self._normalized_dft_audit_target(row, audit) for audit in audits]
        first = next((item for item in normalized if item.get("value") is not None), None)
        if first is None:
            return False
        return all(self._same_normalized_dft_value(first, item) for item in normalized if item.get("value") is not None)

    def _all_nonnegative_dft_opinions_match(self, row: DFTResult, audits: list[dict[str, Any]]) -> bool:
        opinions = [audit for audit in audits if not self._is_negative_dft_decision(audit.get("decision"))]
        if len(opinions) < 2:
            return False
        normalized = [self._normalized_dft_audit_target(row, audit) for audit in opinions]
        if any(item.get("value") is None for item in normalized):
            return False
        if not all(self._same_normalized_dft_value(normalized[0], item) for item in normalized[1:]):
            return False
        return self._dft_material_identities_compatible(opinions, target_id=str(row.id), field_name="dft_results")

    def _dft_material_identities_compatible(
        self,
        audits: list[dict[str, Any]],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> bool:
        materials = [
            self._dft_material_identity_value(audit, target_id=target_id, field_name=field_name)
            for audit in audits
        ]
        materials = [material for material in materials if material]
        if len(materials) < 2:
            return True
        first = materials[0]
        return all(self._material_identity_values_compatible(first, material) for material in materials[1:])

    def _dft_material_identity_value(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> str:
        return str(self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)[1] or "")

    def _material_identity_values_compatible(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        return self._material_identity_parts_compatible(left, right)

    @staticmethod
    def _material_identity_parts_compatible(left: str, right: str) -> bool:
        left_normalized = str(left or "").strip().lower()
        right_normalized = str(right or "").strip().lower()
        if left_normalized == right_normalized:
            return True
        if left_normalized in right_normalized or right_normalized in left_normalized:
            return True
        left_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", left_normalized)
            if len(token) >= 5
        }
        right_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", right_normalized)
            if len(token) >= 5
        }
        return bool(left_tokens & right_tokens)

    @staticmethod
    def _normalized_dft_audit_target(row: DFTResult, audit: dict[str, Any]) -> dict[str, Any]:
        corrected = audit.get("corrected_value")
        field_name = str(audit.get("field_name") or "").strip()
        if isinstance(corrected, dict):
            value = corrected.get("value")
            unit = corrected.get("unit")
        elif corrected not in (None, ""):
            value = corrected
            unit = row.unit
        elif field_name in {"value", "dft_results"}:
            value = row.value
            unit = row.unit
        else:
            value = None
            unit = row.unit
        return normalize_dft_value_for_comparison(value, unit)

    @staticmethod
    def _same_normalized_dft_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return same_normalized_dft_value(left, right)

    def _settle_high_risk_targets(
        self,
        *,
        paper_ids: list[UUID],
        primary_label: str,
        secondary_label: str,
        scope: str,
        reviewer: str,
    ) -> dict[str, Any]:
        target_types = self.HIGH_RISK_SCOPES.get(scope, set())
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisRun.paper_id.in_(paper_ids),
                ExternalAnalysisRun.source_label.in_([primary_label, secondary_label]),
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
        ).all()
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip()
            if target_type not in target_types:
                continue
            key = (
                str(candidate.paper_id),
                target_type,
                str(payload.get("target_id") or ""),
                str(payload.get("field_name") or ""),
            )
            grouped[key].append(
                {
                    "candidate": candidate,
                    "candidate_id": str(candidate.id),
                    "paper_id": str(candidate.paper_id),
                    "target_type": target_type,
                    "target_id": str(payload.get("target_id") or ""),
                    "field_name": str(payload.get("field_name") or ""),
                    "decision": str(payload.get("decision") or "").upper(),
                    "corrected_value": payload.get("corrected_value", payload.get("value")),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "source_label": run.source_label,
                    "source_id": str(candidate.id),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "human_confirmation_required": bool(payload.get("human_confirmation_required", True)),
                    "raw_payload": payload,
                }
            )
        auto_applied: list[dict[str, Any]] = []
        pending_conflicts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for (paper_id_text, target_type, target_id, field_name), opinions in grouped.items():
            decision = self._consensus_opinion(
                opinions,
                primary_label=primary_label,
                secondary_label=secondary_label,
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
            )
            if decision["status"] != "consensus":
                pending_conflicts.append(
                    {
                        "paper_id": paper_id_text,
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": decision["reason"],
                        "opinion_count": len(opinions),
                    }
                )
                continue
            adopted = self._apply_selected_opinion(
                paper_id=UUID(paper_id_text),
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=decision["opinion"],
                dual_ai_consensus=True,
            )
            materialized_target_type, materialized_target_id = self._materialized_target_ref(adopted)
            for opinion in opinions:
                candidate = opinion.get("candidate")
                if candidate is None:
                    continue
                candidate.status = "materialized"
                candidate.materialized_target_type = materialized_target_type
                candidate.materialized_target_id = materialized_target_id
                self.session.add(candidate)
            auto_applied.append(adopted)
        self.session.flush()
        missing_dual = max(0, len(grouped) - len(auto_applied) - len(pending_conflicts))
        if missing_dual:
            skipped.append({"reason": "insufficient_dual_ai_pairs", "count": missing_dual})
        return {
            "candidate_group_count": len(grouped),
            "auto_applied_count": len(auto_applied),
            "manual_conflict_count": len(pending_conflicts),
            "skipped_count": sum(int(item.get("count", 1)) for item in skipped),
            "auto_applied_items": auto_applied,
            "manual_conflicts": pending_conflicts,
            "skipped_items": skipped,
        }

    def _retire_skipped_new_dft_candidate(
        self,
        candidate: ExternalAnalysisCandidate,
        *,
        reason: str,
    ) -> None:
        candidate.status = "ignored" if reason == "borrowed_supporting_reference" else "requires_resolution"
        self.session.add(candidate)

    def _consensus_opinion(
        self,
        opinions: list[dict[str, Any]],
        *,
        primary_label: str,
        secondary_label: str,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> dict[str, Any]:
        by_label = {item.get("source_label"): item for item in opinions if item.get("source_label") in {primary_label, secondary_label}}
        if primary_label not in by_label or secondary_label not in by_label:
            return {"status": "pending", "reason": "awaiting_both_ai_reviews"}
        primary = by_label[primary_label]
        secondary = by_label[secondary_label]
        if not self._opinion_has_anchor(primary) or not self._opinion_has_anchor(secondary):
            return {"status": "manual", "reason": "missing_evidence_anchor"}
        if target_type == "dft_results" and (
            not self._dft_has_material_identity(primary, target_id=target_id, field_name=field_name)
            or not self._dft_has_material_identity(secondary, target_id=target_id, field_name=field_name)
        ):
            return {"status": "manual", "reason": "missing_dft_material_identity"}
        if str(primary.get("decision") or "") != str(secondary.get("decision") or ""):
            return {"status": "manual", "reason": "decision_conflict"}
        if self._value_key(primary.get("corrected_value")) != self._value_key(secondary.get("corrected_value")):
            return {"status": "manual", "reason": "value_conflict"}
        if target_type == "dft_results" and self._dft_identity_key(primary, target_id=target_id, field_name=field_name) != self._dft_identity_key(
            secondary,
            target_id=target_id,
            field_name=field_name,
        ):
            return {"status": "manual", "reason": "identity_conflict"}
        adopted = primary if (primary.get("confidence") or 0) >= (secondary.get("confidence") or 0) else secondary
        return {"status": "consensus", "reason": "dual_ai_match", "opinion": adopted}

    def _apply_selected_opinion(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        reviewer: str,
        opinion: dict[str, Any],
        dual_ai_consensus: bool = False,
        adjudicated_by_third_ai: bool = False,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        decision = str(opinion.get("decision") or "").upper()
        evidence_payload = self._materialize_evidence_payload(opinion)
        if target_type == "dft_results":
            if decision in {"REJECT", "REJECTED", "BLOCK"} and opinion.get("corrected_value") in (None, ""):
                return self._apply_reject_all(paper_id=paper_id, target_type=target_type, target_id=target_id, reviewer=reviewer)
            return self._apply_dft_opinion(
                paper_id=paper_id,
                target_id=target_id,
                field_name=field_name,
                reviewer=reviewer,
                opinion=opinion,
                dual_ai_consensus=dual_ai_consensus,
                adjudicated_by_third_ai=adjudicated_by_third_ai,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
        if target_type in {"tables", "figures"} and decision in DECISION_POSITIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "mark_reviewed", "target_type": target_type, "target_id": target_id}
        if decision in DECISION_NEGATIVE and opinion.get("corrected_value") in (None, ""):
            return {"action": "reject", "target_type": target_type, "target_id": target_id}
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            reviewer=reviewer,
            proposed_value=opinion.get("corrected_value", opinion.get("value")),
            evidence_payload=evidence_payload,
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
            write_lock_tokens=write_lock_tokens,
        )

    def _apply_reject_all(self, *, paper_id: UUID, target_type: str, target_id: str, reviewer: str) -> dict[str, Any]:
        if target_type != "dft_results":
            raise ValueError("reject_all is currently only supported for DFT result candidates.")
        result = DFTResultReviewService(self.session).reject_result(
            paper_id=paper_id,
            result_id=UUID(str(target_id)),
            confirm_reject_candidate=True,
            reviewer=reviewer,
            reviewer_note="Rejected after AI verification conflict adjudication.",
            expected_write_versions=self._current_dft_review_versions(
                paper_id=paper_id,
                target_id=target_id,
            ),
            commit=False,
        )
        return {"action": "reject", "target_type": target_type, "target_id": target_id, "result": result}

    def _apply_dft_opinion(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_name: str,
        reviewer: str,
        opinion: dict[str, Any],
        dual_ai_consensus: bool,
        adjudicated_by_third_ai: bool,
        evidence_payload: Any,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, UUID(str(target_id)))
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for adjudication.")
        mapped_field = self.DFT_FIELD_ALIASES.get(field_name, field_name)
        proposed_value = opinion.get("corrected_value", opinion.get("value"))
        current_value = getattr(row, mapped_field, None) if hasattr(row, mapped_field) else None
        if not (mapped_field == "catalyst_sample_id" and proposed_value not in (None, "")):
            self._apply_dft_material_binding_if_needed(
                row=row,
                opinion=opinion,
                reviewer=reviewer,
                evidence_payload=evidence_payload,
                write_lock_tokens=write_lock_tokens,
            )
            self.session.flush()
            self.session.refresh(row)
        note = self._materialization_note(
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
        )
        if mapped_field == "value" and self._value_key(proposed_value) == self._value_key(current_value):
            result = DFTResultReviewService(self.session).verify_result(
                paper_id=paper_id,
                result_id=UUID(str(target_id)),
                confirm_reviewed_against_pdf=True,
                reviewer=reviewer,
                reviewer_note=note,
                field_names=["value"],
                expected_write_versions=self._current_dft_review_versions(
                    paper_id=paper_id,
                    target_id=target_id,
                    field_names=["value"],
                ),
                evidence_payload=evidence_payload,
                commit=False,
            )
            return {"action": "verify", "target_type": "dft_results", "target_id": target_id, "result": result}
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=target_id,
            field_name=mapped_field,
            reviewer=reviewer,
            proposed_value=proposed_value,
            evidence_payload=evidence_payload,
            dual_ai_consensus=dual_ai_consensus,
            adjudicated_by_third_ai=adjudicated_by_third_ai,
            write_lock_tokens=write_lock_tokens,
        )

    def _current_dft_review_versions(
        self,
        *,
        paper_id: UUID,
        target_id: str,
        field_names: list[str] | None = None,
    ) -> dict[str, int]:
        stmt = select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == "dft_results",
            ExtractionFieldReview.target_id == str(target_id),
        )
        if field_names:
            stmt = stmt.where(ExtractionFieldReview.field_name.in_(field_names))
        reviews = self.session.scalars(stmt).all()
        return {
            str(review.field_name): int(review.write_version or 1)
            for review in reviews
        }

    def _apply_structured_correction(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        reviewer: str,
        proposed_value: Any,
        evidence_payload: Any,
        dual_ai_consensus: bool,
        adjudicated_by_third_ai: bool,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        target_collection = self._correction_collection_name(target_type)
        if target_collection == "paper":
            correction = PaperCorrection(
                paper_id=paper_id,
                source=reviewer,
                field_name=field_name,
                target_path=field_name,
                operation="replace",
                proposed_value=proposed_value,
                reason=self._materialization_note(
                    dual_ai_consensus=dual_ai_consensus,
                    adjudicated_by_third_ai=adjudicated_by_third_ai,
                ),
                evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
                status="pending",
            )
            self.session.add(correction)
            self.session.flush()
            approved = ReviewService(self.session).approve_correction(
                correction.id,
                reviewer=reviewer,
                write_lock_tokens=write_lock_tokens,
            )
            self.session.flush()
            return {
                "action": "approve_correction",
                "target_type": "paper",
                "target_id": str(paper_id),
                "correction_id": str(approved.id),
                "field_name": field_name,
                "proposed_value": proposed_value,
                "result": {"status": approved.status, "reviewed_by": approved.reviewed_by},
            }
        is_sample_create = (
            target_collection == "catalyst_samples"
            and str(target_id).strip().lower() in {"new", "create"}
            and str(field_name).strip().lower() == "create"
        )
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name=target_collection,
            target_path="catalyst_samples:new:create" if is_sample_create else f"{target_collection}:{target_id}:{field_name}",
            operation="create" if is_sample_create else "replace",
            proposed_value=proposed_value,
            reason=self._materialization_note(
                dual_ai_consensus=dual_ai_consensus,
                adjudicated_by_third_ai=adjudicated_by_third_ai,
            ),
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        approved = ReviewService(self.session).approve_correction(
            correction.id,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
        )
        self.session.flush()
        sample_resolution = (
            (approved.evidence_payload or {}).get("sample_resolution")
            if isinstance(approved.evidence_payload, dict)
            else None
        )
        resolved_target_id = (
            sample_resolution.get("catalyst_sample_id")
            if isinstance(sample_resolution, dict)
            else target_id
        )
        return {
            "action": "approve_correction",
            "target_type": target_collection,
            "target_id": resolved_target_id,
            "correction_id": str(approved.id),
            "field_name": field_name,
            "proposed_value": proposed_value,
            "result": {"status": approved.status, "reviewed_by": approved.reviewed_by},
        }

    def _apply_dft_material_binding_if_needed(
        self,
        *,
        row: DFTResult,
        opinion: dict[str, Any],
        reviewer: str,
        evidence_payload: Any,
        write_lock_tokens: list[str] | None = None,
    ) -> None:
        corrected_value = opinion.get("corrected_value")
        raw_payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        material_identity = self._first_text(
            corrected_value.get("material_identity") if isinstance(corrected_value, dict) else None,
            corrected_value.get("material") if isinstance(corrected_value, dict) else None,
            corrected_value.get("catalyst") if isinstance(corrected_value, dict) else None,
            opinion.get("normalized_material"),
            opinion.get("normalized_material_or_catalyst"),
            raw_payload.get("normalized_material"),
            raw_payload.get("normalized_material_or_catalyst"),
            raw_payload.get("material"),
            raw_payload.get("catalyst"),
        )
        if not material_identity and not row.catalyst_sample_id:
            return
        DFTResultReviewService(self.session)._apply_material_binding(  # noqa: SLF001 - reuse existing safe binding flow
            row=row,
            material_identity=material_identity,
            reviewer=reviewer,
            reason=str(opinion.get("reason") or "").strip() or "Applied AI-reviewed DFT material binding through the verification safety gate.",
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            write_lock_tokens=write_lock_tokens,
        )

    @staticmethod
    def _correction_collection_name(target_type: str) -> str:
        lowered = str(target_type or "").strip().lower()
        if lowered == "paper":
            return "paper"
        if lowered in {"figure", "figures"}:
            return "figures"
        if lowered in {"table", "tables"}:
            return "tables"
        return lowered

    @staticmethod
    def _normalize_object_review_target_type(value: Any) -> str:
        lowered = str(value or "").strip().lower()
        if lowered == "paper":
            return "paper"
        return canonical_target_type(lowered)

    @staticmethod
    def _materialized_target_ref(result: dict[str, Any]) -> tuple[str | None, str | None]:
        action = str(result.get("action") or "").strip()
        if action == "approve_correction" and result.get("target_type") == "catalyst_samples":
            return ("catalyst_sample", str(result.get("target_id") or "") or None)
        if action == "approve_correction":
            return ("paper_correction", str(result.get("correction_id") or "") or None)
        target_type = str(result.get("target_type") or "").strip() or None
        target_id = str(result.get("target_id") or "") or None
        return (target_type, target_id)

    @staticmethod
    def _object_review_candidate_status_for_result(result: dict[str, Any]) -> str:
        action = str(result.get("action") or "").strip().lower()
        if action == "approve_correction":
            return "ai_applied"
        if action in {"mark_reviewed", "reject"}:
            return "ai_reviewed"
        return "materialized"

    @staticmethod
    def _correction_candidate_has_anchor(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        evidence_payload = payload.get("evidence_payload")
        if VerificationSessionService._opinion_has_anchor({"evidence_payload": evidence_payload}):
            return True
        return VerificationSessionService._opinion_has_anchor({"evidence_payload": candidate.evidence_payload})

    @staticmethod
    def _note_has_anchor(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        if payload.get("page") is not None:
            return True
        if str(payload.get("section_title") or "").strip():
            return True
        if str(payload.get("quoted_text") or "").strip():
            return True
        evidence_payload = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
        return any(
            evidence_payload.get(key) is not None and str(evidence_payload.get(key)).strip()
            for key in ("page", "section", "locator", "figure", "table", "evidence_text")
        )

    @staticmethod
    def _opinion_has_anchor(opinion: dict[str, Any]) -> bool:
        return has_evidence_anchor(opinion.get("evidence_payload"))

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return value

    def _review_consensus_key(
        self,
        opinion: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> tuple[Any, ...]:
        key: tuple[Any, ...] = (
            str(opinion.get("decision") or ""),
            self._value_key(opinion.get("corrected_value")),
        )
        if target_type == "dft_results":
            key = key + self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)
        return key

    def _consensus_disagreement_reason(
        self,
        opinions: list[dict[str, Any]],
        *,
        target_type: str,
        target_id: str,
        field_name: str,
    ) -> str:
        if target_type != "dft_results":
            return "ai_disagreement"
        value_keys = {
            (str(item.get("decision") or ""), self._value_key(item.get("corrected_value")))
            for item in opinions
        }
        if len(value_keys) > 1:
            return "ai_disagreement"
        identity_keys = {
            self._dft_identity_key(item, target_id=target_id, field_name=field_name)
            for item in opinions
        }
        if len(identity_keys) > 1:
            return "ai_identity_disagreement"
        return "ai_disagreement"

    def _dft_identity_key(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> tuple[Any, ...]:
        payload = opinion.get("raw_payload") if isinstance(opinion.get("raw_payload"), dict) else {}
        if not payload:
            payload = opinion
        row = None
        if target_id:
            try:
                row = self.session.get(DFTResult, UUID(str(target_id)))
            except (TypeError, ValueError):
                row = None
        mapped_field = self.DFT_FIELD_ALIASES.get(str(field_name or "").strip(), str(field_name or "").strip())
        corrected_value = opinion.get("corrected_value")

        def pick(field: str, *keys: str, fallback: Any = None) -> Any:
            if mapped_field == field and corrected_value not in (None, ""):
                return corrected_value
            for key in keys:
                value = payload.get(key)
                if value not in (None, "", []):
                    return value
            return fallback

        row_material = None
        if isinstance(row, DFTResult) and row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            row_material = sample.name if sample and sample.name else str(row.catalyst_sample_id)
        material_identity = pick(
            "catalyst_sample_id",
            "catalyst_sample_id",
            "normalized_material",
            "normalized_material_or_catalyst",
            "material",
            "catalyst",
            fallback=row_material,
        )
        property_type = pick(
            "property_type",
            "normalized_energy_type",
            "property_type",
            "energy_type",
            fallback=row.property_type if isinstance(row, DFTResult) else None,
        )
        structure_name = pick("structure_name", "structure_name")
        adsorbate = pick("adsorbate", "adsorbate", fallback=row.adsorbate if isinstance(row, DFTResult) else None)
        reaction_step = pick(
            "reaction_step",
            "reaction_step",
            fallback=row.reaction_step if isinstance(row, DFTResult) else None,
        )
        return tuple(
            self._normalized_identity_part(value)
            for value in (property_type, material_identity, structure_name, adsorbate, reaction_step)
        )

    @staticmethod
    def _normalized_identity_part(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).strip().lower()
        return str(value or "").strip().lower()

    def _dft_has_material_identity(
        self,
        opinion: dict[str, Any],
        *,
        target_id: str | None = None,
        field_name: str | None = None,
    ) -> bool:
        identity = self._dft_identity_key(opinion, target_id=target_id, field_name=field_name)
        return len(identity) > 1 and bool(identity[1])

    @classmethod
    def _is_project_library_v4_opinion(cls, opinion: dict[str, Any]) -> bool:
        markers = list(cls._iter_payload_markers(opinion))
        text = " ".join(markers)
        has_context = "li_s_sac_dac" in text
        has_v4_contract = (
            "project_library_ml_export_v4" in text
            or "project_library_bundles_v1" in text
            or "project_library_v4" in text
        )
        user_submit_only = "database_write_authority=user_submit_only" in text
        auto_adopt_disabled = "ai_consensus_auto_adopt_allowed=false" in text
        return has_context and (has_v4_contract or user_submit_only or auto_adopt_disabled)

    @classmethod
    def _iter_payload_markers(cls, value: Any, *, prefix: str = ""):
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key or "").strip().lower()
                next_prefix = f"{prefix}.{key_text}" if prefix else key_text
                if isinstance(nested, (dict, list, tuple)):
                    yield from cls._iter_payload_markers(nested, prefix=next_prefix)
                elif nested is not None:
                    nested_text = str(nested).strip().lower()
                    if nested_text:
                        yield nested_text
                        yield f"{key_text}={nested_text}"
                        yield f"{next_prefix}={nested_text}"
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from cls._iter_payload_markers(item, prefix=prefix)

    @staticmethod
    def _materialization_note(*, dual_ai_consensus: bool, adjudicated_by_third_ai: bool) -> str:
        if adjudicated_by_third_ai:
            return "Third-AI adjudication adopted this opinion through the existing verify/correction safety gate."
        if dual_ai_consensus:
            return "Dual-AI consensus auto-adopted through the existing verify/correction safety gate."
        return "Manual adjudication adopted this AI opinion through the existing verify/correction safety gate."

    @staticmethod
    def _materialize_evidence_payload(opinion: dict[str, Any]) -> Any:
        payload = opinion.get("evidence_payload")
        if not isinstance(payload, dict):
            return payload
        merged = dict(payload)
        extra = {
            "adjudication_role": opinion.get("adjudication_role"),
            "adjudication_scope": opinion.get("adjudication_scope"),
            "selected_source_ids": opinion.get("selected_source_ids"),
            "review_decision": opinion.get("decision"),
            "review_source": opinion.get("source"),
            "review_source_label": opinion.get("source_label"),
        }
        merged.update({key: value for key, value in extra.items() if value not in (None, "", [])})
        return merged

    def _get_session_job(self, session_id: str) -> WorkflowJob:
        job = self.session.get(WorkflowJob, session_id)
        if job is None or job.type != "verification_session":
            raise LookupError("Verification session not found.")
        return job
