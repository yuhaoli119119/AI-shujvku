from __future__ import annotations

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
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperNote,
    WorkflowJob,
    WritingCard,
    VerificationSessionPaperClaim,
    utcnow,
)
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.external_analysis_identity import (
    UNTRUSTED_LEGACY_SOURCE_IDENTITY,
    review_source_identity,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.review_service import ReviewService
from app.services.verification_session_application import (
    VerificationSessionReviewApplicationMixin,
)
from app.services.verification_session_candidates import (
    VerificationSessionDFTCandidateMixin,
)
from app.services.verification_session_consensus import (
    VerificationSessionDFTConsensusMixin,
)
from app.utils.library_names import DEFAULT_LIBRARY_NAME, normalize_library_name


class VerificationSessionConflict(ValueError):
    def __init__(self, paper_id: UUID, session_id: str | None = None) -> None:
        self.code = "verification_session_paper_conflict"
        self.paper_id = paper_id
        self.session_id = session_id
        detail = f"paper_id={paper_id}"
        if session_id:
            detail += f",session_id={session_id}"
        super().__init__(f"{self.code}:{detail}")


class VerificationSessionService(
    VerificationSessionDFTCandidateMixin,
    VerificationSessionDFTConsensusMixin,
    VerificationSessionReviewApplicationMixin,
):
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

    @staticmethod
    def _is_dft_scoped_external_run(run: ExternalAnalysisRun | None, payload: dict[str, Any] | None = None) -> bool:
        payload = payload or {}
        parts = [
            getattr(run, "source", None),
            getattr(run, "source_label", None),
            payload.get("source"),
            payload.get("source_label"),
            payload.get("agent_role"),
            payload.get("adjudication_scope"),
        ]
        text = " ".join(str(part or "") for part in parts).casefold()
        return "dft" in text

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
            candidate_run_id=candidate_run_id,
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
            candidate_run_id=candidate_run_id,
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
                    dft_settlement_summary["audit_consensus_count"]
                    + dft_settlement_summary["waiting_second_ai_count"]
                    + dft_settlement_summary["need_third_ai_count"]
                    + dft_settlement_summary["need_repair_count"]
                ),
                "pending_items": (
                    dft_settlement_summary["audit_consensus_items"]
                    + dft_settlement_summary["waiting_second_ai_items"]
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
        new_dft_summary = self._materialize_new_dft_candidates(
            paper_id=paper_id,
            reviewer=reviewer,
            candidate_run_id=candidate_run_id,
        )
        rows = self.session.scalars(
            select(DFTResult)
            .where(DFTResult.paper_id == paper_id)
            .order_by(DFTResult.id.asc())
        ).all()
        audits_by_target = self._paper_dft_audit_candidates(paper_id)
        auto_applied: list[dict[str, Any]] = []
        audit_consensus_ready: list[dict[str, Any]] = []
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
            elif status == "audit_consensus_ready":
                audit_consensus_ready.append(row_summary)
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
            "audit_consensus_count": len(audit_consensus_ready),
            "audit_consensus_items": audit_consensus_ready,
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
                    "audit_consensus_count": summary["audit_consensus_count"],
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
            "audit_consensus_count": 0,
            "audit_consensus_items": [],
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
        candidate_run_id: UUID | None = None,
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        stmt = (
            select(ExternalAnalysisCandidate)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type.in_(("note", "correction")),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        )
        if candidate_run_id is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.run_id == candidate_run_id)
        candidates = self.session.scalars(stmt).all()
        external_service = ExternalAnalysisService(self.session, self.settings)
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            run = self.session.get(ExternalAnalysisRun, candidate.run_id)
            if self._is_dft_scoped_external_run(run, payload):
                candidate.status = "requires_resolution"
                candidate.mapping_reason = "dft_scoped_run_rejects_non_dft_candidate"
                self.session.add(candidate)
                skipped.append(
                    {
                        "candidate_id": str(candidate.id),
                        "reason": "dft_scoped_run_rejects_non_dft_candidate",
                    }
                )
                continue
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
        candidate_run_id: UUID | None = None,
        write_lock_tokens: list[str] | None = None,
        include_target_types: set[str] | None = None,
        exclude_target_types: set[str] | None = None,
    ) -> dict[str, Any]:
        stmt = (
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        )
        if candidate_run_id is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.run_id == candidate_run_id)
        rows = self.session.execute(stmt).all()
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        skipped: list[dict[str, Any]] = []
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            if target_type != "dft_results" and self._is_dft_scoped_external_run(run, payload):
                candidate.status = "requires_resolution"
                candidate.materialized_target_type = None
                candidate.materialized_target_id = None
                candidate.mapping_reason = "dft_scoped_run_rejects_non_dft_target"
                self.session.add(candidate)
                skipped.append(
                    {
                        "candidate_id": str(candidate.id),
                        "target_type": target_type,
                        "reason": "dft_scoped_run_rejects_non_dft_target",
                    }
                )
                continue
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
            identity = review_source_identity(
                run.source_identity,
                run.source_identity_verified,
                default_untrusted=UNTRUSTED_LEGACY_SOURCE_IDENTITY,
            )
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
                    "source_identity_verified": bool(run.source_identity_verified),
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
                    "adjudication_role": payload.get("adjudication_role"),
                    "adjudication_scope": payload.get("adjudication_scope"),
                    "selected_source_ids": payload.get("selected_source_ids"),
                    "raw_payload": payload,
                }
            )

        applied: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for (target_type, target_id, field_name), opinions in grouped.items():
            deduped: dict[str, dict[str, Any]] = {}
            for opinion in opinions:
                if opinion["candidate"].status not in {"candidate", "pending", "requires_resolution"}:
                    continue
                key = (
                    self._dft_review_submission_identity(opinion)
                    if target_type == "dft_results"
                    else opinion["source_identity"]
                )
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
            if target_type == "tables" and result.get("action") == "requires_direct_table_tool":
                for opinion in opinions:
                    opinion["candidate"].status = "requires_resolution"
                    opinion["candidate"].materialized_target_type = None
                    opinion["candidate"].materialized_target_id = None
                    opinion["candidate"].mapping_reason = str(
                        result.get("reason") or "requires_direct_table_tool"
                    )
                    self.session.add(opinion["candidate"])
                pending.append(
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "field_name": field_name,
                        "reason": result.get("reason") or "requires_direct_table_tool",
                        "recommended_tool": result.get("recommended_tool"),
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

    def _get_session_job(self, session_id: str) -> WorkflowJob:
        job = self.session.get(WorkflowJob, session_id)
        if job is None or job.type != "verification_session":
            raise LookupError("Verification session not found.")
        return job
