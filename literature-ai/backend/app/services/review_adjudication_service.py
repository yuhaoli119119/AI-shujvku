from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, Paper, WorkflowJob
from app.services.dft_review_service import DFTResultReviewService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.utils.active_database import get_registered_active_library_info
from app.utils.library_names import normalize_library_name


class ReviewAdjudicationService:
    """Explainable AI adjudication and low-risk auto-advance for review conflicts."""

    AUTO_TARGET_TYPES = {"dft_results"}
    DIRECT_REVIEW_TARGET_TYPES = {"writing_cards", "mechanism_claims", "figure", "figures", "table", "tables"}
    EXACT_LOCATOR_STATUSES = {"exact_page", "exact_bbox"}
    RELIABLE_LOCATOR_STATUSES = {"exact_page", "exact_bbox", "candidate"}
    HIGH_CONFIDENCE = 0.82
    MEDIUM_CONFIDENCE = 0.65

    def __init__(self, session: Session) -> None:
        self.session = session
        self.conflicts = ReviewConflictAggregationService(session)
        self.dft_reviews = DFTResultReviewService(session)

    def enrich_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["adjudication"] = self.evaluate_row(row)
            enriched.append(item)
        return enriched

    def summarize_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter()
        for row in rows:
            mode = ((row.get("adjudication") or {}).get("adjudication_mode") or "manual").strip().lower()
            counts[mode] += 1
        return {
            "auto": counts.get("auto", 0),
            "suggest": counts.get("suggest", 0),
            "manual": counts.get("manual", 0),
            "total": sum(counts.values()),
        }

    def evaluate_row(self, row: dict[str, Any]) -> dict[str, Any]:
        opinions = list(row.get("opinions") or [])
        target_type = str(row.get("target_type") or "")
        target_id = str(row.get("target_id") or "")
        field_name = str(row.get("field_name") or "")
        conflict_types = list(row.get("conflict_types") or [])
        has_conflict = bool(conflict_types)
        metrics = self._build_metrics(opinions)
        blocked_reasons: list[str] = []

        if not opinions:
            blocked_reasons.append("no_review_opinions")
            return self._payload(
                mode="manual",
                action="manual_review",
                source=None,
                summary="No review opinions are available for adjudication.",
                risk_level="high",
                auto_apply=False,
                blocked_reasons=blocked_reasons,
                metrics=metrics,
            )

        if not metrics["has_evidence_text"]:
            blocked_reasons.append("evidence_insufficient")
        if metrics["weak_locator_count"] > 0:
            blocked_reasons.append("weak_locator_present")
        if metrics["exact_locator_count"] == 0:
            blocked_reasons.append("no_exact_locator")
        if metrics["high_confidence_count"] == 0:
            blocked_reasons.append("confidence_below_threshold")

        best = self._pick_best_opinion(opinions)
        recommended_source = self._recommended_source(best)
        reason_bits = [
            f"conflict={'yes' if has_conflict else 'no'}",
            f"exact_locator={metrics['exact_locator_count']}",
            f"high_confidence={metrics['high_confidence_count']}",
            f"agreement={metrics['dominant_ratio']:.2f}",
        ]

        if target_type in self.DIRECT_REVIEW_TARGET_TYPES:
            blocked_reasons.append("requires_object_review")
            mode = "suggest" if metrics["dominant_ratio"] >= 0.6 and metrics["exact_locator_count"] > 0 else "manual"
            return self._payload(
                mode=mode,
                action="jump_to_review",
                source=recommended_source,
                summary="Object-level conflicts should stay in the review workflow; AI can only point to the most credible opinion.",
                risk_level="medium" if mode == "suggest" else "high",
                auto_apply=False,
                blocked_reasons=blocked_reasons,
                metrics=metrics,
                reason_bits=reason_bits,
                opinion=best,
            )

        if target_type not in self.AUTO_TARGET_TYPES:
            blocked_reasons.append("unsupported_target_type")
            return self._payload(
                mode="manual",
                action="manual_review",
                source=recommended_source,
                summary="This target type is not eligible for automated adjudication in the current workflow.",
                risk_level="high",
                auto_apply=False,
                blocked_reasons=blocked_reasons,
                metrics=metrics,
                reason_bits=reason_bits,
                opinion=best,
            )

        dft_row = self._get_dft_row(target_id)
        if dft_row is None:
            blocked_reasons.append("target_not_found")
            return self._payload(
                mode="manual",
                action="manual_review",
                source=recommended_source,
                summary="The target DFT result could not be loaded for safe adjudication.",
                risk_level="high",
                auto_apply=False,
                blocked_reasons=blocked_reasons,
                metrics=metrics,
                reason_bits=reason_bits,
                opinion=best,
            )

        recommended_action, recommended_payload = self._recommend_dft_action(
            row=row,
            dft_row=dft_row,
            best=best,
            metrics=metrics,
        )
        mode, risk_level = self._classify_mode_and_risk(
            has_conflict=has_conflict,
            conflict_types=conflict_types,
            metrics=metrics,
            recommended_action=recommended_action,
            blocked_reasons=blocked_reasons,
        )
        auto_apply = mode == "auto" and recommended_action in {"verify", "reject", "propose_correction"}
        return self._payload(
            mode=mode,
            action=recommended_action,
            source=recommended_source,
            summary=self._reason_summary(recommended_action, metrics, blocked_reasons, has_conflict),
            risk_level=risk_level,
            auto_apply=auto_apply,
            blocked_reasons=blocked_reasons if mode != "auto" else [item for item in blocked_reasons if item == "weak_locator_present"],
            metrics=metrics,
            reason_bits=reason_bits,
            opinion=best,
            payload=recommended_payload,
        )

    def list_with_adjudication(
        self,
        *,
        paper_id: UUID | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        field_name: str | None = None,
        include_non_conflicts: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        payload = self.conflicts.list_conflicts(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            include_non_conflicts=include_non_conflicts,
            limit=limit,
        )
        rows = self.enrich_rows(payload.get("rows") or [])
        payload["rows"] = rows
        payload["adjudication_summary"] = self.summarize_rows(rows)
        return payload

    def accept_recommendation(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        field_name: str,
        reviewer: str,
    ) -> dict[str, Any]:
        payload = self.list_with_adjudication(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            include_non_conflicts=True,
            limit=25,
        )
        row = next((item for item in payload.get("rows") or [] if str(item.get("target_id")) == str(target_id)), None)
        if row is None:
            raise LookupError("Conflict target not found")
        adjudication = row.get("adjudication") or {}
        action = adjudication.get("recommended_action")
        if action not in {"verify", "reject", "propose_correction"}:
            raise ValueError("This target does not expose a safe executable AI recommendation.")
        result = self._execute_action(row=row, adjudication=adjudication, reviewer=reviewer, auto_mode=False)
        self._record_adjudication_action(
            paper_id=paper_id,
            target_type=target_type,
            target_id=target_id,
            reviewer=reviewer,
            action="accept_ai_adjudication",
            adjudication=adjudication,
            result=result,
        )
        self.session.commit()
        return result

    def auto_advance_batch(
        self,
        *,
        paper_ids: list[UUID] | None = None,
        reviewer: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        requested_ids = [paper_id for paper_id in (paper_ids or []) if paper_id]
        rows: list[dict[str, Any]] = []
        if requested_ids:
            for paper_id in requested_ids:
                payload = self.list_with_adjudication(
                    paper_id=paper_id,
                    include_non_conflicts=True,
                    limit=limit,
                )
                rows.extend(payload.get("rows") or [])
        else:
            payload = self.list_with_adjudication(include_non_conflicts=True, limit=limit)
            rows = payload.get("rows") or []
        auto_rows = [row for row in rows if ((row.get("adjudication") or {}).get("adjudication_mode") == "auto")]
        executed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in auto_rows:
            adjudication = row.get("adjudication") or {}
            try:
                executed.append(self._execute_action(row=row, adjudication=adjudication, reviewer=reviewer, auto_mode=True))
            except Exception as exc:
                skipped.append(
                    {
                        "paper_id": row.get("paper_id"),
                        "target_type": row.get("target_type"),
                        "target_id": row.get("target_id"),
                        "field_name": row.get("field_name"),
                        "reason": str(exc),
                    }
                )
        self.session.add(
            AuditLog(
                paper_id=requested_ids[0] if len(requested_ids) == 1 else None,
                action="auto_advance_review_adjudication_batch",
                source=reviewer,
                target_type="review_adjudication_batch",
                target_id=str(len(executed)),
                payload={
                    "requested_paper_ids": [str(item) for item in requested_ids],
                    "eligible_count": len(auto_rows),
                    "executed_count": len(executed),
                    "skipped_count": len(skipped),
                },
            )
        )
        self.session.commit()
        return {
            "requested_paper_ids": [str(item) for item in requested_ids],
            "eligible": len(auto_rows),
            "executed": len(executed),
            "skipped": len(skipped),
            "executed_items": executed,
            "skipped_items": skipped,
        }

    def _execute_action(
        self,
        *,
        row: dict[str, Any],
        adjudication: dict[str, Any],
        reviewer: str,
        auto_mode: bool,
    ) -> dict[str, Any]:
        payload = adjudication.get("recommended_payload") or {}
        action = adjudication.get("recommended_action")
        paper_id = UUID(str(row["paper_id"]))
        target_id = UUID(str(row["target_id"]))
        action_note = payload.get("reason") or adjudication.get("reason_summary")
        if action == "verify":
            result = self.dft_reviews.verify_result(
                paper_id=paper_id,
                result_id=target_id,
                confirm_reviewed_against_pdf=True,
                reviewer=reviewer,
                reviewer_note=action_note,
                field_names=payload.get("field_names") or [str(row.get("field_name") or "value")],
            )
        elif action == "reject":
            result = self.dft_reviews.reject_result(
                paper_id=paper_id,
                result_id=target_id,
                confirm_reject_candidate=True,
                reviewer=reviewer,
                reviewer_note=action_note,
                field_names=payload.get("field_names") or [str(row.get("field_name") or "value")],
            )
        elif action == "propose_correction":
            result = self.dft_reviews.propose_correction(
                paper_id=paper_id,
                result_id=target_id,
                confirm_correction_proposal=True,
                field_name=str(payload.get("field_name") or row.get("field_name") or "value"),
                proposed_value=payload.get("proposed_value"),
                reason=action_note or "AI adjudication proposed a safer correction draft.",
                reviewer=reviewer,
                evidence_payload=payload.get("evidence_payload"),
            )
        else:
            raise ValueError(f"Unsupported adjudication action: {action}")

        self._record_adjudication_action(
            paper_id=paper_id,
            target_type=str(row.get("target_type") or ""),
            target_id=str(row.get("target_id") or ""),
            reviewer=reviewer,
            action="auto_apply_ai_adjudication" if auto_mode else "execute_ai_adjudication",
            adjudication=adjudication,
            result=result,
        )
        self.session.commit()
        return {
            "paper_id": str(paper_id),
            "target_type": row.get("target_type"),
            "target_id": row.get("target_id"),
            "field_name": row.get("field_name"),
            "action": action,
            "auto_mode": auto_mode,
            "result": result,
        }

    def _record_adjudication_action(
        self,
        *,
        paper_id: UUID,
        target_type: str,
        target_id: str,
        reviewer: str,
        action: str,
        adjudication: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        workflow_library_name = self._resolve_workflow_library_name(paper_id)
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action=action,
                source=reviewer,
                target_type=target_type,
                target_id=target_id,
                payload={
                    "adjudication_mode": adjudication.get("adjudication_mode"),
                    "recommended_action": adjudication.get("recommended_action"),
                    "risk_level": adjudication.get("risk_level"),
                    "reason_summary": adjudication.get("reason_summary"),
                    "result": result,
                },
            )
        )
        self.session.add(
            WorkflowJob(
                job_id=str(uuid4()),
                type="review_adjudication",
                status="completed",
                library_name=workflow_library_name,
                payload={
                    "action": action,
                    "paper_id": str(paper_id),
                    "target_type": target_type,
                    "target_id": target_id,
                    "recommended_action": adjudication.get("recommended_action"),
                },
                progress={"completed": True},
                result={"status": "recorded"},
            )
        )

    def _resolve_workflow_library_name(self, paper_id: UUID) -> str:
        paper = self.session.get(Paper, paper_id)
        paper_library_name = normalize_library_name(getattr(paper, "library_name", None))
        if paper_library_name:
            return paper_library_name
        try:
            active_library_name = normalize_library_name(get_registered_active_library_info().get("active_library"))
        except Exception:
            active_library_name = ""
        if active_library_name:
            stmt = select(Paper.library_name).where(Paper.id == paper_id)
            db_library_name = normalize_library_name(self.session.scalar(stmt))
            if db_library_name:
                return db_library_name
            return active_library_name
        return normalize_library_name(None)

    def _recommend_dft_action(
        self,
        *,
        row: dict[str, Any],
        dft_row: DFTResult,
        best: dict[str, Any],
        metrics: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        field_name = str(row.get("field_name") or "value")
        evidence_payload = self._build_evidence_payload(best, row)
        decision_bucket = self._decision_bucket(best.get("decision") or best.get("status"))
        dominant_value = metrics.get("dominant_value")
        same_as_current = self._same_value(dominant_value, getattr(dft_row, field_name, None) if hasattr(dft_row, field_name) else dft_row.value)

        if decision_bucket == "negative" and metrics["negative_count"] >= metrics["positive_count"] and metrics["exact_locator_count"] > 0:
            return (
                "reject",
                {
                    "field_names": [field_name],
                    "reason": "AI adjudication rejected the candidate because negative evidence dominates with a reliable locator.",
                    "evidence_payload": evidence_payload,
                },
            )

        if field_name == "value" and dominant_value is not None and not same_as_current:
            return (
                "propose_correction",
                {
                    "field_name": field_name,
                    "proposed_value": dominant_value,
                    "reason": "AI adjudication found a stronger evidence-backed value and prepared a correction draft instead of silently mutating final truth.",
                    "evidence_payload": evidence_payload,
                },
            )

        return (
            "verify",
            {
                "field_names": [field_name],
                "reason": "AI adjudication found the current candidate consistent with the strongest evidence-backed opinion.",
                "evidence_payload": evidence_payload,
            },
        )

    def _classify_mode_and_risk(
        self,
        *,
        has_conflict: bool,
        conflict_types: list[str],
        metrics: dict[str, Any],
        recommended_action: str,
        blocked_reasons: list[str],
    ) -> tuple[str, str]:
        if "evidence_insufficient" in blocked_reasons or "no_exact_locator" in blocked_reasons:
            return "manual", "high"
        if metrics["dominant_ratio"] < 0.6:
            return "manual", "high"
        if has_conflict and ("decision_conflict" in conflict_types) and metrics["dominant_ratio"] < 0.66:
            return "manual", "high"
        if has_conflict and metrics["exact_locator_count"] >= 2 and metrics["high_confidence_count"] >= 2 and metrics["dominant_ratio"] >= 0.75:
            return "auto", "low"
        if not has_conflict and metrics["exact_locator_count"] >= 1 and metrics["high_confidence_count"] >= 1:
            return "auto", "low"
        if recommended_action == "propose_correction" and metrics["dominant_ratio"] >= 0.66 and metrics["high_confidence_count"] >= 1:
            return "suggest", "medium"
        if metrics["dominant_ratio"] >= 0.66 and metrics["exact_locator_count"] >= 1:
            return "suggest", "medium"
        return "manual", "high"

    def _build_metrics(self, opinions: list[dict[str, Any]]) -> dict[str, Any]:
        locator_statuses: list[str] = []
        confidence_values: list[float] = []
        positive_count = 0
        negative_count = 0
        evidence_text_count = 0
        value_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for opinion in opinions:
            locator_status = self._locator_status(opinion)
            if locator_status:
                locator_statuses.append(locator_status)
            confidence = opinion.get("confidence")
            if isinstance(confidence, (int, float)):
                confidence_values.append(float(confidence))
            if self._decision_bucket(opinion.get("decision") or opinion.get("status")) == "positive":
                positive_count += 1
            elif self._decision_bucket(opinion.get("decision") or opinion.get("status")) == "negative":
                negative_count += 1
            if self._evidence_text(opinion):
                evidence_text_count += 1
            value_key = self._value_key(opinion.get("value"))
            if value_key:
                value_groups[value_key].append(opinion)
        dominant_group = max(value_groups.values(), key=len) if value_groups else []
        dominant_opinion = max(dominant_group, key=self._opinion_score, default=None)
        return {
            "opinion_count": len(opinions),
            "exact_locator_count": sum(1 for status in locator_statuses if status in self.EXACT_LOCATOR_STATUSES),
            "weak_locator_count": sum(1 for status in locator_statuses if status not in self.RELIABLE_LOCATOR_STATUSES),
            "high_confidence_count": sum(1 for value in confidence_values if value >= self.HIGH_CONFIDENCE),
            "medium_confidence_count": sum(1 for value in confidence_values if value >= self.MEDIUM_CONFIDENCE),
            "avg_confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else 0.0,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "has_evidence_text": evidence_text_count > 0,
            "dominant_value": dominant_opinion.get("value") if dominant_opinion else None,
            "dominant_ratio": round((len(dominant_group) / len(opinions)), 4) if opinions else 0.0,
            "locator_status_counts": dict(sorted(Counter(locator_statuses).items())),
        }

    def _payload(
        self,
        *,
        mode: str,
        action: str,
        source: dict[str, Any] | None,
        summary: str,
        risk_level: str,
        auto_apply: bool,
        blocked_reasons: list[str],
        metrics: dict[str, Any],
        reason_bits: list[str] | None = None,
        opinion: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "adjudication_mode": mode,
            "recommended_action": action,
            "recommended_source": source,
            "reason_summary": " ".join([summary] + (reason_bits or [])),
            "risk_level": risk_level,
            "eligible_for_auto_apply": auto_apply,
            "blocked_reasons": blocked_reasons,
            "recommended_payload": payload or {},
            "metrics": metrics,
            "recommended_opinion": opinion,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _reason_summary(
        self,
        action: str,
        metrics: dict[str, Any],
        blocked_reasons: list[str],
        has_conflict: bool,
    ) -> str:
        parts = []
        if action == "verify":
            parts.append("Current value already matches the strongest evidence-backed opinion.")
        elif action == "reject":
            parts.append("Negative reviews dominate and keep the candidate out of the ready queue.")
        elif action == "propose_correction":
            parts.append("A safer correction draft is preferred because the strongest evidence disagrees with the stored value.")
        else:
            parts.append("Human review remains the safer next step.")
        if has_conflict:
            parts.append("Conflict was evaluated explicitly.")
        parts.append(f"Exact locators={metrics['exact_locator_count']}, avg confidence={metrics['avg_confidence']:.2f}.")
        if blocked_reasons:
            parts.append("Blocked by: " + ", ".join(blocked_reasons) + ".")
        return " ".join(parts)

    @staticmethod
    def _recommended_source(opinion: dict[str, Any] | None) -> dict[str, Any] | None:
        if not opinion:
            return None
        return {
            "source_type": opinion.get("source_type"),
            "source_id": opinion.get("source_id"),
            "source": opinion.get("source"),
            "source_label": opinion.get("source_label"),
            "decision": opinion.get("decision") or opinion.get("status"),
        }

    def _pick_best_opinion(self, opinions: list[dict[str, Any]]) -> dict[str, Any]:
        return max(opinions, key=self._opinion_score)

    def _opinion_score(self, opinion: dict[str, Any]) -> float:
        score = 0.0
        decision_bucket = self._decision_bucket(opinion.get("decision") or opinion.get("status"))
        if decision_bucket == "positive":
            score += 1.2
        elif decision_bucket == "negative":
            score += 0.8
        locator_status = self._locator_status(opinion)
        if locator_status in self.EXACT_LOCATOR_STATUSES:
            score += 1.0
        elif locator_status in self.RELIABLE_LOCATOR_STATUSES:
            score += 0.4
        confidence = opinion.get("confidence")
        if isinstance(confidence, (int, float)):
            score += float(confidence)
        if self._evidence_text(opinion):
            score += 0.3
        return score

    def _build_evidence_payload(self, opinion: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        evidence = opinion.get("evidence") if isinstance(opinion.get("evidence"), dict) else {}
        locator = evidence.get("locator") if isinstance(evidence.get("locator"), dict) else {}
        return {
            "source_label": opinion.get("source_label") or opinion.get("source"),
            "agent_role": opinion.get("agent_role"),
            "model_name": opinion.get("model_name"),
            "confidence": opinion.get("confidence"),
            "unit": opinion.get("unit"),
            "target_type": row.get("target_type"),
            "target_id": row.get("target_id"),
            "field_name": row.get("field_name"),
            "evidence_text": self._evidence_text(opinion),
            "locator": locator,
        }

    @staticmethod
    def _locator_status(opinion: dict[str, Any]) -> str:
        evidence = opinion.get("evidence")
        if isinstance(evidence, list):
            evidence = evidence[0] if evidence else None
        if isinstance(evidence, dict):
            locator = evidence.get("locator") if isinstance(evidence.get("locator"), dict) else evidence
            status = locator.get("locator_status")
            if status:
                return str(status)
            if locator.get("page") is not None and locator.get("bbox"):
                return "exact_bbox"
            if locator.get("page") is not None:
                return "exact_page"
        return ""

    @staticmethod
    def _evidence_text(opinion: dict[str, Any]) -> str:
        evidence = opinion.get("evidence")
        if isinstance(evidence, list):
            evidence = evidence[0] if evidence else None
        if isinstance(evidence, dict):
            return str(evidence.get("evidence_text") or "").strip()
        return ""

    @staticmethod
    def _decision_bucket(decision: Any) -> str:
        normalized = str(decision or "").strip().upper()
        if normalized in {"PASS", "ACCEPT", "APPROVE", "APPROVED", "VERIFIED", "OK", "PROPOSED"}:
            return "positive"
        if normalized in {"REVISE", "FLAG", "INSUFFICIENT", "REJECT", "REJECTED", "NEEDS_FIX", "FIX", "BLOCK"}:
            return "negative"
        return "neutral"

    @staticmethod
    def _value_key(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.8g}"
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return ""
            try:
                return f"{float(value):.8g}"
            except ValueError:
                return value.lower()
        return str(value)

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        return ReviewAdjudicationService._value_key(left) == ReviewAdjudicationService._value_key(right)

    def _get_dft_row(self, target_id: str) -> DFTResult | None:
        try:
            return self.session.get(DFTResult, UUID(str(target_id)))
        except ValueError:
            return None
