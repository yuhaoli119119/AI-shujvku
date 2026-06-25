from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.services.artifact_reliability_audit_service import ArtifactReliabilityAuditService
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import bulk_export_gate_results, summarize_gate_results


DFT_TARGET_TYPES = ("dft_results", "dft_result", "DFTResult")
ENERGY_UNITS = {
    "ev",
    "mev",
    "kj/mol",
    "kj mol-1",
    "kjmol-1",
    "kcal/mol",
    "kcal mol-1",
    "kcalmol-1",
    "j/mol",
    "hartree",
    "ha",
}
POTENTIAL_UNITS = {"v", "mv", "ev"}


class DFTReviewQueueService:
    """Build a Codex-ready queue for reviewing DFT result candidates."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_queue(
        self,
        *,
        property_type: str | None = None,
        adsorbate: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        paper_id: UUID | None = None,
        library_name: str | None = None,
        reason: str | None = None,
        status: str = "needs_review",
        limit: int = 100,
        schema_version: str = "dft_review_queue_v1",
    ) -> dict[str, Any]:
        rows = self.session.execute(
            self._statement(
                property_type=property_type,
                adsorbate=adsorbate,
                year_min=year_min,
                year_max=year_max,
                paper_id=paper_id,
                library_name=library_name,
            )
        ).all()
        gate_results = []
        queue_candidates = []
        paper_ids = set()
        paper_meta_by_id: dict[str, dict[str, Any]] = {}
        exportable_by_paper: dict[str, int] = defaultdict(int)
        blocked_by_paper: dict[str, int] = defaultdict(int)
        parsed_by_paper: dict[str, int] = defaultdict(int)
        review_status_counts: Counter[str] = Counter()

        dft_rows = [row for row, _paper in rows]
        gate_by_id = bulk_export_gate_results(self.session, dft_rows, target_type="dft_results")

        for row, paper in rows:
            gate = gate_by_id.get(str(row.id))
            if gate is None:
                continue
            gate_results.append(gate)
            for review_status in self._review_statuses(gate.review_status):
                review_status_counts[review_status] += 1
            pid = str(paper.id)
            paper_ids.add(paper.id)
            paper_meta_by_id[pid] = {
                "title": paper.title,
                "doi": paper.doi,
                "library_detail_url": f"../literature_library/index.html?paper_id={pid}&tab=dft",
                "review_workbench_url": f"../external_analysis_workbench/index.html?paper_id={pid}",
            }
            parsed_by_paper[pid] += 1
            if gate.eligible:
                exportable_by_paper[pid] += 1
            else:
                blocked_by_paper[pid] += 1
            if reason and reason not in gate.reasons:
                continue
            if not self._status_matches(status, gate):
                continue
            queue_candidates.append((row, paper, gate, self._sanity_flags(row)))

        queue_candidates.sort(
            key=lambda item: (
                bool(item[3]),
                not (set(item[2].reasons) == {"missing_review"} and not item[3]),
                -(item[1].year or 0),
                str(item[1].title or ""),
            )
        )
        selected_candidates = queue_candidates[:limit]
        selected_dft_rows = [row for row, _paper, _gate, _sanity_flags in selected_candidates]
        selected_paper_ids = {paper.id for _row, paper, _gate, _sanity_flags in selected_candidates}
        locators_by_id = self._bulk_locator_payloads(selected_dft_rows)
        catalysts_by_paper = self._bulk_catalyst_payloads(selected_paper_ids)
        external_audits_by_paper = self._bulk_external_audit_payloads(selected_paper_ids)
        object_audits_by_target = self._bulk_object_review_audit_payloads(
            selected_paper_ids,
            target_ids={str(row.id) for row in selected_dft_rows},
        )
        conflicts_by_target = ReviewConflictAggregationService(self.session).conflicts_by_target(
            paper_ids=selected_paper_ids,
            target_type="dft_results",
            target_ids={str(row.id) for row in selected_dft_rows},
        )
        queue_rows = [
            self._row_payload(
                row,
                paper,
                gate,
                catalysts_by_paper.get(str(paper.id), []),
                locators_by_id.get(str(row.id), []),
                external_audits_by_paper.get(str(paper.id), []),
                object_audits_by_target.get(str(row.id), []),
                conflicts_by_target.get(str(row.id), []),
            )
            for row, paper, gate, _sanity_flags in selected_candidates
        ]

        catalyst_counts: Counter[str] = Counter()
        setting_counts: Counter[str] = Counter()
        if paper_ids:
            for pid in self.session.scalars(
                select(CatalystSample.paper_id).where(CatalystSample.paper_id.in_(paper_ids))
            ).all():
                catalyst_counts[str(pid)] += 1
            for pid in self.session.scalars(select(DFTSetting.paper_id).where(DFTSetting.paper_id.in_(paper_ids))).all():
                setting_counts[str(pid)] += 1

        paper_completeness = []
        auditor = DFTCompletenessAuditor(self.session)
        for pid in sorted({str(item) for item in paper_ids}):
            meta = paper_meta_by_id.get(pid, {})
            audit = auditor.audit_paper(
                UUID(pid),
                parsed_count=parsed_by_paper.get(pid, 0),
                exportable_count=exportable_by_paper.get(pid, 0),
                blocked_count=blocked_by_paper.get(pid, 0),
            )
            paper_completeness.append(
                {
                    "paper_id": pid,
                    "title": meta.get("title"),
                    "doi": meta.get("doi"),
                    "library_detail_url": meta.get("library_detail_url"),
                    "review_workbench_url": meta.get("review_workbench_url"),
                    "exportable_dft_results": exportable_by_paper.get(pid, 0),
                    "blocked_dft_results": blocked_by_paper.get(pid, 0),
                    "dft_audit": audit,
                    "dft_completeness_status": audit["coverage_status"],
                    "dft_completeness_label": audit["status_label"],
                    "catalyst_samples": catalyst_counts.get(pid, 0),
                    "dft_settings": setting_counts.get(pid, 0),
                    "hints": [
                        hint
                        for hint, present in (
                            ("missing_catalyst_sample", catalyst_counts.get(pid, 0) == 0),
                            ("missing_dft_setting", setting_counts.get(pid, 0) == 0),
                            ("has_blocked_dft_results", blocked_by_paper.get(pid, 0) > 0),
                            ("suspected_missing_dft", audit["suspected_missing_count"] > 0),
                        )
                        if present
                    ],
                }
            )

        gate_summary = summarize_gate_results(gate_results)
        return {
            "metadata": {
                "schema_version": schema_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "filters": {
                    "property_type": property_type,
                    "adsorbate": adsorbate,
                    "year_min": year_min,
                    "year_max": year_max,
                    "paper_id": str(paper_id) if paper_id else None,
                    "library_name": normalize_library_name(library_name) if library_name is not None else None,
                    "reason": reason,
                    "status": status,
                },
                "safety_gate": "safe_verified_with_required_evidence",
                "eligible_count": gate_summary["eligible"],
                "blocked_count": gate_summary["blocked"],
                "blocked_reasons": gate_summary["blocked_reasons"],
                "review_status_counts": dict(sorted(review_status_counts.items())),
                "total_candidates": gate_summary["total_candidates"],
                "returned": len(queue_rows),
            },
            "rows": queue_rows,
            "paper_completeness": paper_completeness[:limit],
        }

    def _statement(
        self,
        *,
        property_type: str | None,
        adsorbate: str | None,
        year_min: int | None,
        year_max: int | None,
        paper_id: UUID | None,
        library_name: str | None,
    ):
        stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id).order_by(
            Paper.year.desc().nulls_last(),
            Paper.title,
            DFTResult.property_type,
        )
        if property_type:
            stmt = stmt.where(DFTResult.property_type.ilike(f"%{property_type}%"))
        if adsorbate:
            stmt = stmt.where(DFTResult.adsorbate.ilike(f"%{adsorbate}%"))
        if year_min:
            stmt = stmt.where(Paper.year >= year_min)
        if year_max:
            stmt = stmt.where(Paper.year <= year_max)
        if paper_id:
            stmt = stmt.where(Paper.id == paper_id)
        if library_name is not None:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, library_name))
        return stmt

    def _row_payload(
        self,
        row: DFTResult,
        paper: Paper,
        gate: Any,
        paper_catalysts: list[dict[str, Any]] | None = None,
        locators: list[dict[str, Any]] | None = None,
        external_audits: list[dict[str, Any]] | None = None,
        object_review_audits: list[dict[str, Any]] | None = None,
        conflicts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        paper_id = str(paper.id)
        result_id = str(row.id)
        reasons = list(gate.reasons)
        locators = locators if locators is not None else self._locator_payloads(row)
        primary_locator = self._primary_locator(locators)
        locator_reliability = ArtifactReliabilityAuditService.locator_reliability_from_payload(primary_locator)
        sanity_flags = self._sanity_flags(row)
        issues = self._issue_payloads(row, reasons, sanity_flags, locators, gate)
        figure_reliability = self._figure_reliability(row, locators, gate)
        paper_catalysts = paper_catalysts or []
        linked_catalyst = next(
            (item for item in paper_catalysts if item["id"] == str(row.catalyst_sample_id)),
            None,
        )
        ai_review_display = self.build_ai_review_display_status(
            gate=gate,
            object_review_audits=object_review_audits or [],
            conflicts=conflicts or [],
        )
        workflow_state = self.build_dft_workflow_state(
            gate=gate,
            object_review_audits=object_review_audits or [],
            candidate_status=row.candidate_status,
        )
        return {
            "record_id": result_id,
            "dft_result_id": result_id,
            "paper_id": paper_id,
            "title": paper.title,
            "doi": paper.doi,
            "journal": paper.journal,
            "year": paper.year,
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "value": row.value,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "catalyst_sample_id": str(row.catalyst_sample_id) if row.catalyst_sample_id else None,
            "material_binding_status": "bound" if row.catalyst_sample_id else "unbound",
            "linked_catalyst_sample": linked_catalyst,
            "candidate_catalyst_samples": paper_catalysts[:8],
            "source_section": row.source_section,
            "source_figure": row.source_figure,
            "evidence_text": row.evidence_text,
            "evidence_payload": row.evidence_payload,
            "evidence_preview": self._shorten(row.evidence_text),
            "primary_evidence_locator": primary_locator,
            "primary_locator_reliability": locator_reliability["primary_locator"],
            "locator_reliability_status": locator_reliability["status"],
            "locator_reliability_warnings": locator_reliability["warnings"],
            "evidence_page": primary_locator.get("page") if primary_locator else None,
            "pdf_page_url": (
                f"/api/papers/{paper_id}/pdf#page={primary_locator.get('page')}"
                if primary_locator and primary_locator.get("page")
                else None
            ),
            "confidence": row.confidence,
            "candidate_status": row.candidate_status or "system_candidate",
            "candidate_source_label": self._candidate_source_label(row.candidate_status),
            "normalized_dedup_key": self._normalized_candidate_key(row),
            "review_status": gate.review_status,
            "decision_status": self._decision_status(gate.review_status),
            "review_gate_status": gate.review_gate_status,
            "provenance_level": gate.provenance_level,
            "locator_status": gate.locator_status,
            "blocked_reasons": reasons,
            "issue_flags": [item["code"] for item in issues],
            "issues": issues,
            "is_exportable": gate.eligible,
            "sanity_flags": sanity_flags,
            "figure_reliability": figure_reliability,
            "can_mark_verified": set(reasons) == {"missing_review"} and not sanity_flags,
            "recommended_action": self._recommended_action(reasons, gate, sanity_flags),
            "evidence_locators": locators,
            "latest_external_audit_opinions": (external_audits or [])[:5],
            "object_review_audits_count": len(object_review_audits or []),
            "object_review_audits": (object_review_audits or [])[:5],
            "ai_review_display_status": ai_review_display["status"],
            "ai_review_display_label": ai_review_display["label"],
            "ai_review_display_reason": ai_review_display["reason"],
            "ai_review_display_class": ai_review_display["class_name"],
            "dft_workflow_state": workflow_state["state"],
            "dft_workflow_label": workflow_state["label"],
            "dft_workflow_reason": workflow_state["reason"],
            "valid_ai_opinion_count": workflow_state["valid_ai_opinion_count"],
            "raw_ai_opinion_count": workflow_state["raw_ai_opinion_count"],
            "effective_ai_opinions": workflow_state["effective_ai_opinions"],
            "next_required_action": workflow_state["next_required_action"],
            "conflicts_count": len(conflicts or []),
            "field_conflicts": conflicts or [],
            "evidence_check": {
                "has_evidence_text": bool((row.evidence_text or "").strip()),
                "locator_count": len(locators),
                "has_exact_page_locator": gate.locator_status == "exact_page",
                "primary_page": primary_locator.get("page") if primary_locator else None,
                "primary_locator_status": primary_locator.get("locator_status") if primary_locator else None,
            },
            "paper_detail_url": f"../paper_detail/index.html?paper_id={paper_id}",
            "library_detail_url": f"../literature_library/index.html?paper_id={paper_id}&tab=dft",
            "codex_item_url": f"/api/papers/{paper_id}/codex-item/dft_result/{result_id}",
            "review_prompt": self._review_prompt(row, paper, gate, issues, locators, figure_reliability),
            "verify_url": f"/api/papers/{paper_id}/dft-results/{result_id}/verify",
            "reject_url": f"/api/papers/{paper_id}/dft-results/{result_id}/reject",
            "correction_url": f"/api/papers/{paper_id}/dft-results/{result_id}/corrections",
            "review_workbench_url": (
                f"../external_analysis_workbench/index.html?paper_id={paper_id}"
                if {"missing_review", "unsafe_review"} & set(reasons)
                else f"../literature_library/index.html?paper_id={paper_id}&tab=review"
            ),
        }

    def _bulk_catalyst_payloads(self, paper_ids: set[UUID]) -> dict[str, list[dict[str, Any]]]:
        if not paper_ids:
            return {}
        payloads: dict[str, list[dict[str, Any]]] = {str(pid): [] for pid in paper_ids}
        rows = self.session.scalars(
            select(CatalystSample)
            .where(CatalystSample.paper_id.in_(paper_ids))
            .order_by(CatalystSample.paper_id.asc(), CatalystSample.id.asc())
        ).all()
        for row in rows:
            payloads.setdefault(str(row.paper_id), []).append(
                {
                    "id": str(row.id),
                    "name": row.name,
                    "catalyst_type": row.catalyst_type,
                    "metal_centers": row.metal_centers or [],
                    "coordination": row.coordination,
                    "support": row.support,
                    "evidence_strength": row.evidence_strength,
                }
            )
        return payloads

    def _locator_payloads(self, row: DFTResult) -> list[dict[str, Any]]:
        locators = self.session.scalars(
            select(EvidenceLocator)
            .where(
                EvidenceLocator.paper_id == row.paper_id,
                EvidenceLocator.target_id == str(row.id),
                EvidenceLocator.target_type.in_(DFT_TARGET_TYPES),
            )
            .order_by(EvidenceLocator.page.asc().nulls_last(), EvidenceLocator.created_at.asc())
            .limit(5)
        ).all()
        return [
            {
                "id": str(locator.id),
                "page": locator.page,
                "source_type": locator.source_type,
                "section": locator.section,
                "figure_id": str(locator.figure_id) if locator.figure_id else None,
                "table_id": str(locator.table_id) if locator.table_id else None,
                "field_name": locator.field_name,
                "locator_status": locator.locator_status,
                "locator_confidence": locator.locator_confidence,
                "parser_source": locator.parser_source,
                "evidence_text": locator.evidence_text,
                "evidence_preview": self._shorten(locator.evidence_text),
                "bbox": locator.bbox,
                "warning_reason": locator.warning_reason,
            }
            for locator in locators
        ]

    def _bulk_locator_payloads(self, rows: list[DFTResult]) -> dict[str, list[dict[str, Any]]]:
        if not rows:
            return {}
        target_ids = {str(row.id) for row in rows}
        locators_by_id: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in target_ids}
        locators = self.session.scalars(
            select(EvidenceLocator)
            .where(
                EvidenceLocator.paper_id.in_({row.paper_id for row in rows}),
                EvidenceLocator.target_id.in_(target_ids),
                EvidenceLocator.target_type.in_(DFT_TARGET_TYPES),
            )
            .order_by(
                EvidenceLocator.target_id.asc(),
                EvidenceLocator.page.asc().nulls_last(),
                EvidenceLocator.created_at.asc(),
            )
        ).all()
        for locator in locators:
            target_id = str(locator.target_id)
            if len(locators_by_id.setdefault(target_id, [])) >= 5:
                continue
            locators_by_id[target_id].append(self._locator_to_payload(locator))
        return locators_by_id

    @staticmethod
    def _primary_locator(locators: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not locators:
            return None
        exact = [item for item in locators if item.get("locator_status") == "exact_page" and item.get("page")]
        if exact:
            return exact[0]
        with_page = [item for item in locators if item.get("page")]
        if with_page:
            return with_page[0]
        return locators[0]

    def _bulk_external_audit_payloads(self, paper_ids: set[UUID]) -> dict[str, list[dict[str, Any]]]:
        if not paper_ids:
            return {}
        audits_by_paper: dict[str, list[dict[str, Any]]] = {str(pid): [] for pid in paper_ids}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id.in_(paper_ids))
            .where(ExternalAnalysisCandidate.candidate_type == "external_audit_opinion")
            .order_by(ExternalAnalysisCandidate.paper_id.asc(), ExternalAnalysisCandidate.created_at.desc())
        ).all()
        for candidate in candidates:
            paper_id = str(candidate.paper_id)
            if len(audits_by_paper.setdefault(paper_id, [])) >= 5:
                continue
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            audits_by_paper[paper_id].append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate_type": candidate.candidate_type,
                    "status": candidate.status,
                    "source": str(payload.get("source") or "unknown"),
                    "source_label": payload.get("source_label"),
                    "agent_role": payload.get("agent_role"),
                    "model_name": payload.get("model_name"),
                    "verdict": payload.get("verdict"),
                    "recommended_action": payload.get("recommended_action"),
                    "verification_status": payload.get("verification_status", "unverified"),
                    "confidence": payload.get("confidence"),
                    "summary": payload.get("summary"),
                    "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
                }
            )
        return audits_by_paper

    def _bulk_object_review_audit_payloads(
        self,
        paper_ids: set[UUID],
        *,
        target_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not paper_ids or not target_ids:
            return {}
        audits_by_target: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in target_ids}
        deduped_by_target: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {target_id: {} for target_id in target_ids}
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id.in_(paper_ids))
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
            .order_by(ExternalAnalysisCandidate.created_at.desc())
        ).all()
        for candidate, run in rows:
            payload = dict(candidate.normalized_payload) if isinstance(candidate.normalized_payload, dict) else {}
            if not payload.get("source"):
                payload["source"] = run.source
            if not payload.get("source_label"):
                payload["source_label"] = run.source_label
            target_type = str(payload.get("target_type") or "").strip()
            target_id = str(payload.get("target_id") or payload.get("dft_result_id") or payload.get("record_id") or "")
            decision = str(payload.get("decision") or "").strip().lower()
            if (
                target_type.strip().lower() in {"dft_results", "dft_result"}
                and (target_id.strip().lower() == "new" or decision == "new_candidate")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                target_id = str(candidate.materialized_target_id).strip()
            if target_id not in target_ids or target_type not in DFT_TARGET_TYPES:
                continue
            audit_payload = self._object_review_audit_payload(candidate, payload)
            dedupe_key = (str(candidate.id),)
            target_bucket = deduped_by_target.setdefault(target_id, {})
            existing = target_bucket.get(dedupe_key)
            if existing is None or self._object_review_audit_payload_rank(audit_payload) > self._object_review_audit_payload_rank(existing):
                target_bucket[dedupe_key] = audit_payload
        for target_id, deduped in deduped_by_target.items():
            audits_by_target[target_id] = sorted(
                deduped.values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:5]
        return audits_by_target

    @staticmethod
    def _object_review_audit_payload(
        candidate: ExternalAnalysisCandidate,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "candidate_id": str(candidate.id),
            "candidate_type": candidate.candidate_type,
            "status": candidate.status,
            "target_type": payload.get("target_type"),
            "target_id": payload.get("target_id"),
            "field_name": payload.get("field_name"),
            "source": str(payload.get("source") or "unknown"),
            "source_label": payload.get("source_label"),
            "agent_role": payload.get("agent_role"),
            "model_name": payload.get("model_name"),
            "decision": payload.get("decision") or payload.get("verdict"),
            "recommended_action": payload.get("recommended_action"),
            "verification_status": payload.get("verification_status", "unverified"),
            "confidence": payload.get("confidence") if payload.get("confidence") is not None else candidate.confidence,
            "reason": payload.get("reason") or payload.get("reviewer_note") or payload.get("summary"),
            "evidence_checked": payload.get("evidence_checked"),
            "evidence_location": payload.get("evidence_location"),
            "blocking_errors": payload.get("blocking_errors") or [],
            "corrected_value": payload.get("corrected_value"),
            "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
        }

    @staticmethod
    def _object_review_audit_dedupe_key(payload: dict[str, Any]) -> tuple[Any, ...]:
        field_name = str(payload.get("field_name") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        if decision == "new_candidate" and field_name in {"", "dft_results"}:
            field_name = "dft_results"
        return (
            str(payload.get("source_label") or payload.get("source") or "").strip().lower(),
            decision,
            field_name,
            json.dumps(payload.get("corrected_value"), sort_keys=True, ensure_ascii=False, default=str),
            json.dumps(payload.get("evidence_location"), sort_keys=True, ensure_ascii=False, default=str),
        )

    @staticmethod
    def _object_review_audit_payload_rank(payload: dict[str, Any]) -> tuple[int, int]:
        field_name = str(payload.get("field_name") or "").strip()
        corrected = payload.get("corrected_value")
        return (
            1 if field_name else 0,
            1 if corrected not in (None, "", [], {}) else 0,
        )

    @staticmethod
    def _locator_to_payload(locator: EvidenceLocator) -> dict[str, Any]:
        return {
            "id": str(locator.id),
            "page": locator.page,
            "source_type": locator.source_type,
            "section": locator.section,
            "figure_id": str(locator.figure_id) if locator.figure_id else None,
            "table_id": str(locator.table_id) if locator.table_id else None,
            "field_name": locator.field_name,
            "locator_status": locator.locator_status,
            "locator_confidence": locator.locator_confidence,
            "parser_source": locator.parser_source,
            "evidence_text": locator.evidence_text,
            "evidence_preview": DFTReviewQueueService._shorten(locator.evidence_text),
            "bbox": locator.bbox,
            "warning_reason": locator.warning_reason,
        }

    @staticmethod
    def _status_matches(status: str | None, gate: Any) -> bool:
        normalized = (status or "needs_review").strip().lower()
        review_statuses = DFTReviewQueueService._review_statuses(gate.review_status)
        if normalized in {"all", "any", ""}:
            return True
        if normalized in {"needs_review", "blocked"}:
            if normalized == "blocked":
                return not gate.eligible
            return not gate.eligible and "rejected" not in review_statuses
        if normalized in {"rejected", "reject"}:
            return "rejected" in review_statuses
        if normalized in {"exportable", "eligible", "verified"}:
            return gate.eligible
        return normalized in set(gate.reasons)

    @staticmethod
    def build_ai_review_display_status(
        *,
        gate: Any,
        object_review_audits: list[dict[str, Any]] | None,
        conflicts: list[dict[str, Any]] | None,
    ) -> dict[str, str]:
        audits = DFTReviewQueueService._unique_dft_review_submissions(object_review_audits or [])
        conflict_items = conflicts or []
        exportable = DFTReviewQueueService._gate_is_eligible(gate)
        has_unresolved_conflicts = bool(conflict_items)

        if not audits:
            return {
                "status": "no_ai_opinion",
                "label": "无 AI 意见",
                "reason": (
                    "This DFT item has no object-level AI review audits; exportability is determined by the safety gate."
                ),
                "class_name": "ok" if exportable else "meta",
            }

        decisions = [DFTReviewQueueService._normalize_ai_review_decision(audit.get("decision")) for audit in audits]
        has_reject = any(DFTReviewQueueService._is_reject_decision(decision) for decision in decisions)
        has_proposed = any(decision == "PROPOSED" for decision in decisions)
        has_pass = any(decision == "PASS" for decision in decisions)
        has_needs_human = any(decision == "NEEDS_HUMAN" for decision in decisions)

        if exportable:
            if has_reject:
                return {
                    "status": "exportable_with_historical_reject",
                    "label": "AI 意见已收敛",
                    "reason": (
                        "The export safety gate is eligible; historical reject/conflict audits no longer describe "
                        "the current export state."
                    ),
                    "class_name": "ok",
                }
            if has_proposed:
                return {
                    "status": "converged_adopted",
                    "label": "已采纳 AI 修正",
                    "reason": "The export safety gate is eligible after AI-proposed corrections were adopted.",
                    "class_name": "ok",
                }
            if has_pass:
                return {
                    "status": "pass_exportable",
                    "label": "AI 字段通过",
                    "reason": "AI review audits are non-negative and the export safety gate is eligible.",
                    "class_name": "ok",
                }

        if has_unresolved_conflicts and has_reject and (has_pass or has_proposed):
            return {
                "status": "conflict",
                "label": "AI 冲突",
                "reason": "Unresolved object-review conflicts include both reject and pass/proposed AI opinions.",
                "class_name": "failed",
            }
        if not exportable and has_reject and not (has_pass or has_proposed):
            reject_submission_count = sum(
                1 for decision in decisions if DFTReviewQueueService._is_reject_decision(decision)
            )
            if reject_submission_count >= 2:
                return {
                    "status": "rejected",
                    "label": "AI 一致拒绝",
                    "reason": "At least two independent review submissions recommend rejecting this non-exportable DFT candidate.",
                    "class_name": "failed",
                }
            return {
                "status": "reject_suggested",
                "label": "AI 建议拒绝",
                "reason": "One review submission recommends rejecting this non-exportable DFT candidate.",
                "class_name": "failed",
            }
        if has_needs_human:
            return {
                "status": "needs_human",
                "label": "AI 无法确认",
                "reason": "At least one AI review audit requested human review.",
                "class_name": "meta",
            }
        if has_proposed:
            return {
                "status": "proposed",
                "label": "AI 已提修正",
                "reason": "AI proposed a correction that has not yet passed the current export safety gate.",
                "class_name": "meta",
            }
        if has_pass:
            return {
                "status": "pass_partial",
                "label": "AI 字段通过",
                "reason": "AI confirmed at least one field, but the item is not yet fully exportable.",
                "class_name": "ok",
            }
        return {
            "status": "unknown",
            "label": "AI 意见待判定",
            "reason": "Object-level AI review audits exist but do not match a known display status.",
            "class_name": "meta",
        }

    @staticmethod
    def _gate_is_eligible(gate: Any) -> bool:
        if isinstance(gate, dict):
            return bool(gate.get("eligible") or gate.get("is_exportable"))
        return bool(getattr(gate, "eligible", False) or getattr(gate, "is_exportable", False))

    @staticmethod
    def _normalize_ai_review_decision(value: Any) -> str:
        decision = str(value or "").strip().upper()
        if decision in {"CONFIRMED", "ACCEPT", "ACCEPTED", "APPROVED", "VERIFIED", "OK"}:
            return "PASS"
        if decision in {"CONFIRMED_WITH_CORRECTIONS", "CORRECTED", "REVISE", "REVISION"}:
            return "PROPOSED"
        return decision

    @staticmethod
    def _is_reject_decision(value: str) -> bool:
        return value in {"REJECT", "REJECTED", "BLOCK", "DENY", "DROP"}

    @staticmethod
    def build_dft_workflow_state(
        *,
        gate: Any,
        object_review_audits: list[dict[str, Any]] | None,
        candidate_status: str | None = None,
    ) -> dict[str, Any]:
        audits = DFTReviewQueueService._unique_dft_review_submissions(object_review_audits or [])
        exportable = DFTReviewQueueService._gate_is_eligible(gate)
        blocked_reasons = set(DFTReviewQueueService._gate_reasons(gate))
        review_statuses = DFTReviewQueueService._gate_review_statuses(gate)
        raw_count = len(audits)
        effective = [
            DFTReviewQueueService._effective_ai_opinion_payload(audit)
            for audit in audits
            if DFTReviewQueueService._is_countable_ai_review_decision(audit.get("decision"))
        ]
        valid = [opinion for opinion in effective if opinion["has_anchor"]]
        valid_count = len(valid)
        decisions = [DFTReviewQueueService._normalize_ai_review_decision(opinion["decision"]) for opinion in valid]
        has_reject = any(DFTReviewQueueService._is_reject_decision(decision) for decision in decisions)
        has_positive = any(decision in {"PASS", "PROPOSED", "REVISE"} for decision in decisions)
        all_reject = bool(decisions) and all(DFTReviewQueueService._is_reject_decision(decision) for decision in decisions)

        is_rejected = (
            str(candidate_status or "").strip().lower() == "rejected"
            or "rejected" in review_statuses
            or "rejected" in blocked_reasons
        )

        if is_rejected:
            state = "rejected"
            label = "已拒绝"
            reason = "这条 DFT 已被人工或审核结算拒绝，当前为终态，不再提供接受入库或重复拒绝操作。"
            action = "none"
        elif exportable:
            state = "exportable"
            label = "可导出"
            reason = "这条 DFT 已满足当前导出安全门要求，可进入导出/训练数据集。"
            action = "none"
        elif "missing_material_identity" in blocked_reasons:
            state = "missing_material_binding"
            label = "缺材料/结构绑定"
            reason = "不能入库：当前候选缺少可核验的材料/结构身份绑定，需要先补齐材料 identity 或 catalyst_sample 绑定。"
            action = "bind_material_identity"
        elif raw_count >= 2 and valid_count < 2:
            state = "missing_evidence_anchor"
            label = "第二意见缺证据定位"
            reason = "不能入库：已有多个 AI 审核记录，但带 page 和 quoted_text 的有效证据定位不足 2 条。"
            action = "provide_evidence_anchor"
        elif raw_count > 0 and valid_count < 2:
            state = "waiting_second_ai"
            label = "等待第二个有效 AI 意见"
            reason = "不能入库：当前只有一个带证据定位的有效审核提交，需要再导入一轮包含 page 和 quoted_text 的审核意见。"
            action = "run_second_ai_with_evidence"
        elif has_reject and has_positive:
            state = "needs_third_ai"
            label = "需第三轮 AI 裁决"
            reason = "不能入库：有效审核提交同时包含拒绝和通过/修正，需要第三轮 AI 审核提交根据 PDF 证据裁决。"
            action = "run_third_ai_adjudication"
        elif valid_count >= 2 and all_reject:
            state = "rejected_consensus_pending_write"
            label = "一致拒绝待写回"
            reason = "不能入库：至少两个带证据定位的有效 AI 意见一致拒绝，需点击重新检查写回或等待系统结算为已拒绝。"
            action = "settle_consensus"
        elif raw_count == 0:
            state = "waiting_second_ai"
            label = "等待第二个有效 AI 意见"
            reason = "不能入库：当前没有可计数的对象级 AI 审核意见，需要生成下一轮 AI 审核任务并提供证据定位。"
            action = "run_second_ai_with_evidence"
        else:
            state = "unknown_blocked"
            label = "阻塞原因待判定"
            reason = "不能入库：导出安全门仍阻塞，但现有 AI 意见尚未匹配到明确 workflow 状态，请重新检查写回或人工复核。"
            action = "none"

        return {
            "state": state,
            "label": label,
            "reason": reason,
            "valid_ai_opinion_count": valid_count,
            "raw_ai_opinion_count": raw_count,
            "effective_ai_opinions": effective[:3],
            "next_required_action": action,
        }

    @staticmethod
    def _unique_dft_review_submissions(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        for audit in audits:
            candidate_id = str(audit.get("candidate_id") or "").strip()
            if candidate_id:
                if candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)
            unique.append(audit)
        return unique

    @staticmethod
    def _gate_review_statuses(gate: Any) -> set[str]:
        value = gate.get("review_status") if isinstance(gate, dict) else getattr(gate, "review_status", None)
        return DFTReviewQueueService._review_statuses(value)

    @staticmethod
    def _gate_reasons(gate: Any) -> tuple[str, ...]:
        if isinstance(gate, dict):
            value = gate.get("reasons") or gate.get("blocked_reasons") or ()
        else:
            value = getattr(gate, "reasons", ())
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(str(item) for item in (value or ()) if str(item).strip())

    @staticmethod
    def _is_countable_ai_review_decision(value: Any) -> bool:
        decision = DFTReviewQueueService._normalize_ai_review_decision(value)
        return decision in {"PASS", "REJECT", "REJECTED", "PROPOSED", "REVISE", "NEEDS_HUMAN"}

    @staticmethod
    def _effective_ai_opinion_payload(audit: dict[str, Any]) -> dict[str, Any]:
        location = audit.get("evidence_location") if isinstance(audit, dict) else None
        has_anchor = DFTReviewQueueService._has_valid_evidence_anchor(location)
        return {
            "source_label": audit.get("source_label") or audit.get("source") or "unknown",
            "decision": DFTReviewQueueService._normalize_ai_review_decision(audit.get("decision")),
            "has_anchor": has_anchor,
            "anchor_summary": DFTReviewQueueService._anchor_summary(location),
            "reason_short": DFTReviewQueueService._shorten(audit.get("reason"), 160),
        }

    @staticmethod
    def _has_valid_evidence_anchor(location: Any) -> bool:
        if not isinstance(location, dict):
            return False
        page = location.get("page")
        quoted = location.get("quoted_text") or location.get("evidence_text")
        return page not in (None, "") and bool(str(quoted or "").strip())

    @staticmethod
    def _anchor_summary(location: Any) -> str:
        if not isinstance(location, dict):
            return ""
        parts = []
        if location.get("page") not in (None, ""):
            parts.append(f"p.{location.get('page')}")
        for key in ("table", "figure", "section", "section_title"):
            if location.get(key):
                parts.append(str(location.get(key)))
                break
        quoted = DFTReviewQueueService._shorten(location.get("quoted_text") or location.get("evidence_text"), 80)
        if quoted:
            parts.append(quoted)
        return " | ".join(parts)

    @staticmethod
    def _recommended_action(reasons: list[str], gate: Any, sanity_flags: list[str] | None = None) -> str:
        if "rejected" in DFTReviewQueueService._review_statuses(gate.review_status):
            return "rejected_candidate"
        if sanity_flags:
            return "inspect_suspicious_candidate"
        if gate.eligible:
            return "ready_for_ml_export"
        reason_set = set(reasons)
        if "missing_material_identity" in reason_set:
            return "bind_material_identity"
        if "missing_evidence_text" in reason_set:
            return "add_evidence_text"
        if "missing_evidence" in reason_set:
            return "repair_evidence_reference"
        if "unsafe_locator" in reason_set:
            return "repair_pdf_locator"
        if reason_set == {"missing_review"}:
            return "verify_against_pdf"
        if "unsafe_review" in reason_set:
            return "resolve_review_status"
        return "review_candidate"

    @staticmethod
    def _shorten(value: str | None, limit: int = 360) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."

    @staticmethod
    def _review_statuses(value: str | None) -> set[str]:
        statuses = {
            item.strip().lower()
            for item in str(value or "").split(",")
            if item.strip()
        }
        return statuses or {"missing"}

    @staticmethod
    def _decision_status(value: str | None) -> str:
        statuses = DFTReviewQueueService._review_statuses(value)
        if "rejected" in statuses:
            return "rejected"
        if "verified" in statuses:
            return "verified"
        if statuses == {"missing"}:
            return "unreviewed"
        return "needs_check"

    @staticmethod
    def _sanity_flags(row: DFTResult) -> list[str]:
        flags: list[str] = []
        property_type = str(row.property_type or "").strip().lower()
        unit = str(row.unit or "").strip().lower().replace(" ", "")
        adsorbate = str(row.adsorbate or "").strip()
        value = row.value

        if value is None:
            flags.append("missing_numeric_value")
        if adsorbate and re.fullmatch(r"\[?\d+(?:[-,]\d+)*\]?", adsorbate):
            flags.append("adsorbate_looks_like_reference")
        if adsorbate and len(adsorbate) > 40:
            flags.append("adsorbate_too_long")

        if property_type:
            expects_potential = "potential" in property_type or property_type in {"ul", "u_l"}
            expects_energy = any(
                token in property_type
                for token in (
                    "energy",
                    "barrier",
                    "formation",
                    "adsorption",
                    "binding",
                    "migration",
                    "gibbs",
                )
            )
            if expects_potential and unit and unit not in POTENTIAL_UNITS:
                flags.append(f"unexpected_potential_unit:{unit}")
            if expects_energy and unit and unit not in ENERGY_UNITS:
                flags.append(f"unexpected_energy_unit:{unit}")
            if expects_energy and unit == "ev" and value is not None and abs(float(value)) > 50:
                flags.append("energy_value_outside_typical_ev_range")
            if expects_potential and unit in {"v", "ev"} and value is not None and abs(float(value)) > 20:
                flags.append("potential_value_outside_typical_range")
        elif unit and unit not in ENERGY_UNITS | POTENTIAL_UNITS:
            flags.append(f"unexpected_unit:{unit}")

        return flags

    @staticmethod
    def _candidate_source_label(status: str | None) -> str:
        normalized = str(status or "system_candidate").strip()
        return {
            "system_candidate": "系统规则候选",
            "candidate_unverified": "未审核候选",
            "Gemini_Verified": "AI 复核候选",
            "Human_Confirmed": "人工确认",
            "ML_Ready": "已审核可导出",
            "Rejected": "已拒绝",
            "human_reviewed_needs_evidence": "人工审核后仍缺证据",
        }.get(normalized, normalized)

    @staticmethod
    def _normalized_candidate_key(row: DFTResult) -> str:
        def clean(value: Any) -> str:
            text = str(value or "").lower()
            text = re.sub(r"\s+", "", text)
            text = text.replace("pristinegdy", "gdy").replace("graphdiyne", "gdy")
            text = text.replace("water", "h2o")
            text = text.replace("adsorptionenergy", "adsorption_energy")
            text = text.replace("eads", "adsorption_energy").replace("e_ads", "adsorption_energy")
            return re.sub(r"[^a-z0-9_.+-]+", "", text)

        try:
            value_key = f"{float(row.value):.4f}" if row.value is not None else ""
        except (TypeError, ValueError):
            value_key = str(row.value or "").strip().lower()
        return "|".join(
            [
                str(row.paper_id),
                str(row.catalyst_sample_id or ""),
                clean(row.adsorbate),
                clean(row.property_type),
                value_key,
                clean(row.unit),
                clean(row.reaction_step),
                clean(row.source_section or row.source_figure),
            ]
        )

    @staticmethod
    def _issue_payloads(
        row: DFTResult,
        reasons: list[str],
        sanity_flags: list[str],
        locators: list[dict[str, Any]],
        gate: Any,
    ) -> list[dict[str, Any]]:
        issue_map = {
            "missing_material_identity": ("缺少材料/结构绑定", "danger"),
            "missing_review": ("缺人工确认", "warning"),
            "unsafe_review": ("复核状态不安全", "danger"),
            "missing_evidence_text": ("缺证据原文", "danger"),
            "missing_evidence": ("缺 PDF 定位", "danger"),
            "unsafe_locator": ("PDF 定位不可靠", "danger"),
            "rejected": ("候选已拒绝", "muted"),
        }
        issues: list[dict[str, Any]] = []
        for reason in reasons:
            label, severity = issue_map.get(reason, (reason, "warning"))
            issues.append({"code": reason, "label": label, "severity": severity})
        for flag in sanity_flags:
            issues.append({"code": flag, "label": f"疑似异常: {flag}", "severity": "warning"})
        if (row.evidence_payload or {}).get("duplicate_merge"):
            issues.append({"code": "merged_duplicate", "label": "疑似重复已合并", "severity": "info"})
        if row.source_figure and not locators:
            issues.append({"code": "figure_locator_missing", "label": "图片定位可疑", "severity": "warning"})
        if not gate.eligible and not issues:
            issues.append({"code": "candidate_not_exportable", "label": "候选不可入库", "severity": "warning"})
        return issues

    @staticmethod
    def _figure_reliability(row: DFTResult, locators: list[dict[str, Any]], gate: Any) -> dict[str, Any]:
        figure_like = [item for item in locators if item.get("source_type") == "figure" or item.get("figure_id")]
        flags: list[str] = []
        if row.source_figure and not figure_like:
            flags.append("figure_reference_without_locator")
        if any(item.get("warning_reason") for item in locators):
            flags.append("locator_warning")
        if row.source_figure and gate.locator_status != "exact_page":
            flags.append("not_exact_page")
        if not row.source_figure:
            status = "not_figure_based"
        elif flags:
            status = "needs_review"
        else:
            status = "reliable_candidate"
        return {
            "status": status,
            "label": {
                "not_figure_based": "非图片证据",
                "needs_review": "图片定位需复核",
                "reliable_candidate": "图片定位候选可靠",
            }.get(status, status),
            "flags": flags,
            "locator_count": len(figure_like),
        }

    def _review_prompt(
        self,
        row: DFTResult,
        paper: Paper,
        gate: Any,
        issues: list[dict[str, Any]],
        locators: list[dict[str, Any]],
        figure_reliability: dict[str, Any],
    ) -> str:
        return "\n".join(
            [
                "你是材料计算数据审核员。你的任务不是重新编造数据，而是检查候选 DFT 数据是否被 PDF 证据支持。",
                "",
                f"Paper: {paper.title or 'Untitled'}",
                f"DOI: {paper.doi or '-'}",
                f"Candidate ID: {row.id}",
                f"Candidate source: {row.candidate_status or 'system_candidate'}",
                f"Value: {row.property_type or '-'} / {row.adsorbate or '-'} = {row.value} {row.unit or ''}",
                f"Evidence excerpt: {self._shorten(row.evidence_text, 900) or '-'}",
                f"Source section/figure: {row.source_section or '-'} / {row.source_figure or '-'}",
                f"Review gate: exportable={gate.eligible}, reasons={list(gate.reasons)}",
                f"Issues: {[item['code'] for item in issues] or ['none']}",
                f"Figure reliability: {figure_reliability.get('status')}",
                f"Locators: {locators}",
                "",
                "必须检查：材料/催化剂、吸附物、性质类型、数值、单位、计算条件/方法、证据原文、页码/章节/表格/图号、重复项、漏提线索。",
                "输出只能是：accept / reject / needs_fix / suspected_duplicate / suspected_missing，并给出理由和证据位置。",
            ]
        )
