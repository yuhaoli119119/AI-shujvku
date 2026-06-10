from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AuditLog,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperNote,
    WorkflowJob,
    WritingCard,
)
from app.services.dft_review_service import DFTResultReviewService
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.review_conflict_service import (
    DECISION_NEGATIVE,
    DECISION_POSITIVE,
    ReviewConflictAggregationService,
)
from app.services.review_service import ReviewService


class VerificationSessionService:
    HIGH_RISK_IDE_TARGET_TYPES = {"dft_results", "figure", "figures", "table", "tables"}
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
        preparation_rows = []
        if refresh_materials:
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
        job = WorkflowJob(
            job_id=session_id,
            type="verification_session",
            status="completed",
            library_name=selected[0].library_name or "默认文献库",
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
        return self.get_session(session_id)

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

    def apply_import_rules_for_paper(self, *, paper_id: UUID, reviewer: str) -> dict[str, Any]:
        low_risk_summary = self._auto_materialize_single_ai_candidates(paper_id=paper_id, reviewer=reviewer)
        object_review_summary = self._auto_apply_object_review_candidates(paper_id=paper_id, reviewer=reviewer)
        summary = {
            "paper_id": str(paper_id),
            "single_ai": low_risk_summary,
            "object_reviews": object_review_summary,
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
        job.progress = {
            "completed": True,
            "consistent_auto_adopted": high_risk_summary["auto_applied_count"],
            "single_ai_auto_adopted": note_summary["auto_materialized_count"],
            "manual_conflicts": high_risk_summary["manual_conflict_count"],
        }
        self.session.add(job)
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
                    func.lower(Paper.title) == ref.lower(),
                )
            )
            exact = self.session.scalars(stmt).first()
            if exact is not None:
                resolved[str(exact.id)] = exact
                continue
            fuzzy = self.session.scalars(
                select(Paper)
                .where(or_(Paper.title.ilike(f"%{ref}%"), Paper.doi.ilike(f"%{ref}%")))
                .order_by(Paper.created_at.desc())
                .limit(1)
            ).first()
            if fuzzy is not None:
                resolved[str(fuzzy.id)] = fuzzy
        return list(resolved.values())

    def _paper_summary(self, paper: Paper) -> dict[str, Any]:
        dft_count = self.session.scalar(select(func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper.id)) or 0
        writing_count = self.session.scalar(select(func.count()).select_from(WritingCard).where(WritingCard.paper_id == paper.id)) or 0
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "journal": paper.journal,
            "workspace_prepare_url": f"/api/papers/{paper.id}/prepare-ai-context",
            "codex_context_url": f"/api/papers/{paper.id}/codex-context",
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
                        "Use MCP /api/papers/{paper_id}/codex-context and /codex-item to inspect evidence, "
                        "then import object_review_audits through import_analysis with the assigned source_label."
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

    def _auto_materialize_single_ai_candidates(self, *, paper_id: UUID, reviewer: str) -> dict[str, Any]:
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
                approved = ReviewService(self.session).approve_correction(UUID(str(candidate.materialized_target_id)), reviewer=reviewer)
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

    def _auto_apply_object_review_candidates(self, *, paper_id: UUID, reviewer: str) -> dict[str, Any]:
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
            target_type = str(payload.get("target_type") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip()
            field_name = str(payload.get("field_name") or "").strip()
            if not target_type or not target_id or not field_name:
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
                    "source_label": run.source_label,
                    "source": payload.get("source") or run.source,
                    "source_identity": identity,
                    "evidence_payload": payload.get("evidence_location") or payload.get("evidence_payload"),
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
                key = opinion["source_identity"]
                current = deduped.get(key)
                if current is None or (opinion.get("confidence") or 0) >= (current.get("confidence") or 0):
                    deduped[key] = opinion
            eligible = [item for item in deduped.values() if self._opinion_has_anchor(item)]
            if target_type in self.HIGH_RISK_IDE_TARGET_TYPES:
                if len(eligible) < 2:
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
                signature_groups = {
                    (str(item.get("decision") or ""), self._value_key(item.get("corrected_value"))): item
                    for item in eligible
                }
                if len(signature_groups) != 1:
                    pending.append(
                        {
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": "ai_disagreement",
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
                    (str(item.get("decision") or ""), self._value_key(item.get("corrected_value"))): item
                    for item in eligible
                }
                if len(signature_groups) != 1:
                    pending.append(
                        {
                            "target_type": target_type,
                            "target_id": target_id,
                            "field_name": field_name,
                            "reason": "ai_disagreement",
                            "eligible_opinion_count": len(eligible),
                        }
                    )
                    continue
                adopted = max(eligible, key=lambda item: item.get("confidence") or 0)
            try:
                result = self._apply_selected_opinion(
                    paper_id=paper_id,
                    target_type=target_type,
                    target_id=target_id,
                    field_name=field_name,
                    reviewer=reviewer,
                    opinion=adopted,
                    dual_ai_consensus=target_type in self.HIGH_RISK_IDE_TARGET_TYPES,
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
                opinion["candidate"].status = "materialized"
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
                }
            )
        auto_applied: list[dict[str, Any]] = []
        pending_conflicts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for (paper_id_text, target_type, target_id, field_name), opinions in grouped.items():
            decision = self._consensus_opinion(opinions, primary_label=primary_label, secondary_label=secondary_label)
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
            auto_applied.append(adopted)
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

    def _consensus_opinion(self, opinions: list[dict[str, Any]], *, primary_label: str, secondary_label: str) -> dict[str, Any]:
        by_label = {item.get("source_label"): item for item in opinions if item.get("source_label") in {primary_label, secondary_label}}
        if primary_label not in by_label or secondary_label not in by_label:
            return {"status": "pending", "reason": "awaiting_both_ai_reviews"}
        primary = by_label[primary_label]
        secondary = by_label[secondary_label]
        if not self._opinion_has_anchor(primary) or not self._opinion_has_anchor(secondary):
            return {"status": "manual", "reason": "missing_evidence_anchor"}
        if str(primary.get("decision") or "") != str(secondary.get("decision") or ""):
            return {"status": "manual", "reason": "decision_conflict"}
        if self._value_key(primary.get("corrected_value")) != self._value_key(secondary.get("corrected_value")):
            return {"status": "manual", "reason": "value_conflict"}
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
    ) -> dict[str, Any]:
        decision = str(opinion.get("decision") or "").upper()
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
            )
        if decision in DECISION_NEGATIVE and opinion.get("corrected_value") in (None, ""):
            raise ValueError("A structured non-DFT review cannot be auto-applied without a corrected value.")
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            reviewer=reviewer,
            proposed_value=opinion.get("corrected_value", opinion.get("value")),
            evidence_payload=opinion.get("evidence_payload"),
            dual_ai_consensus=dual_ai_consensus,
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
    ) -> dict[str, Any]:
        row = self.session.get(DFTResult, UUID(str(target_id)))
        if row is None or row.paper_id != paper_id:
            raise LookupError("DFT result not found for adjudication.")
        mapped_field = self.DFT_FIELD_ALIASES.get(field_name, field_name)
        proposed_value = opinion.get("corrected_value", opinion.get("value"))
        current_value = getattr(row, mapped_field, None) if hasattr(row, mapped_field) else None
        note = "Dual-AI consensus auto-adopted through the DFT safety gate." if dual_ai_consensus else "Manual adjudication selected this AI opinion."
        if mapped_field == "value" and self._value_key(proposed_value) == self._value_key(current_value):
            result = DFTResultReviewService(self.session).verify_result(
                paper_id=paper_id,
                result_id=UUID(str(target_id)),
                confirm_reviewed_against_pdf=True,
                reviewer=reviewer,
                reviewer_note=note,
                field_names=["value"],
                evidence_payload=opinion.get("evidence_payload"),
            )
            return {"action": "verify", "target_type": "dft_results", "target_id": target_id, "result": result}
        return self._apply_structured_correction(
            paper_id=paper_id,
            target_type="dft_results",
            target_id=target_id,
            field_name=mapped_field,
            reviewer=reviewer,
            proposed_value=proposed_value,
            evidence_payload=opinion.get("evidence_payload"),
            dual_ai_consensus=dual_ai_consensus,
        )

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
    ) -> dict[str, Any]:
        target_collection = self._correction_collection_name(target_type)
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name=target_collection,
            target_path=f"{target_collection}:{target_id}:{field_name}",
            operation="replace",
            proposed_value=proposed_value,
            reason=(
                "Dual-AI consensus auto-adopted through the correction approval gate."
                if dual_ai_consensus
                else "Manual adjudication adopted the selected AI opinion through the correction approval gate."
            ),
            evidence_payload=evidence_payload if isinstance(evidence_payload, (dict, list)) else None,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        approved = ReviewService(self.session).approve_correction(correction.id, reviewer=reviewer)
        self.session.flush()
        return {
            "action": "approve_correction",
            "target_type": target_collection,
            "target_id": target_id,
            "correction_id": str(approved.id),
            "field_name": field_name,
            "proposed_value": proposed_value,
            "result": {"status": approved.status, "reviewed_by": approved.reviewed_by},
        }

    @staticmethod
    def _correction_collection_name(target_type: str) -> str:
        lowered = str(target_type or "").strip().lower()
        if lowered in {"figure", "figures"}:
            return "figures"
        if lowered in {"table", "tables"}:
            return "tables"
        return lowered

    @staticmethod
    def _materialized_target_ref(result: dict[str, Any]) -> tuple[str | None, str | None]:
        action = str(result.get("action") or "").strip()
        if action == "approve_correction":
            return ("paper_correction", str(result.get("correction_id") or "") or None)
        target_type = str(result.get("target_type") or "").strip() or None
        target_id = str(result.get("target_id") or "") or None
        return (target_type, target_id)

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
        payload = opinion.get("evidence_payload")
        if isinstance(payload, dict):
            locator = payload.get("locator") if isinstance(payload.get("locator"), dict) else payload
            for key in ("page", "section", "figure", "table", "evidence_text", "quoted_text", "section_title"):
                value = locator.get(key) if isinstance(locator, dict) else None
                if value is not None and str(value).strip():
                    return True
        if isinstance(payload, list):
            return any(VerificationSessionService._opinion_has_anchor({"evidence_payload": item}) for item in payload if isinstance(item, dict))
        return False

    @staticmethod
    def _value_key(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 8)
        return value

    def _get_session_job(self, session_id: str) -> WorkflowJob:
        job = self.session.get(WorkflowJob, session_id)
        if job is None or job.type != "verification_session":
            raise LookupError("Verification session not found.")
        return job
