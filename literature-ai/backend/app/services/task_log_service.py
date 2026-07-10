from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun, Paper, WorkflowJob
from app.services.external_analysis_materialization import ExternalAnalysisMaterializationMixin
from app.services.external_analysis_models import ExternalAnalysisNormalizedModel
from app.utils.library_names import normalize_library_name


SOURCE_LABELS = {
    "web_ai": "网页AI审核",
    "ide_ai": "本地IDE AI审核",
    "internal_ai": "本地IDE AI审核",
    "local_ai": "本地IDE AI审核",
    "codex": "本地IDE AI审核",
    "mcp": "本地IDE AI审核",
    "system_parser": "系统解析",
    "human": "人工修改",
    "manual": "人工修改",
}

MODULE_LABELS = {
    "figure_table_evidence": "图表证据整理",
    "dft": "DFT数据审核",
    "content_knowledge": "内容知识审核",
    "writing": "写作RAG生成",
    "text_review": "草稿证据核验",
}


def _compact_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_task_source(source: str | None) -> tuple[str, str]:
    raw = _compact_text(source).lower()
    if raw in {"web_ai", "web", "chatgpt", "chatgpt_web", "gemini_web", "browser_ai"} or raw.endswith("_web"):
        normalized = "web_ai"
    elif raw in {"ide_ai", "internal_ai", "local_ai", "codex", "mcp"}:
        normalized = "ide_ai"
    elif raw in {"system_parser", "parser", "system"}:
        normalized = "system_parser"
    elif raw in {"human", "manual", "user"}:
        normalized = "human"
    else:
        normalized = raw or "ide_ai"
    return normalized, SOURCE_LABELS.get(normalized, _compact_text(source) or SOURCE_LABELS["ide_ai"])


def normalize_task_module(module: str | None) -> tuple[str, str]:
    raw = _compact_text(module).lower()
    if raw in {"figure", "table", "figures", "tables", "figure_table", "chart_review"}:
        normalized = "figure_table_evidence"
    elif raw in {"dft", "dft_review", "dft_data"}:
        normalized = "dft"
    elif raw in {"content", "knowledge", "content_knowledge", "overall"}:
        normalized = "content_knowledge"
    elif raw in {"writing", "rag", "writing_rag"}:
        normalized = "writing"
    elif raw in {"text_review", "draft_review", "evidence_check"}:
        normalized = "text_review"
    else:
        normalized = raw or "content_knowledge"
    return normalized, MODULE_LABELS.get(normalized, _compact_text(module) or MODULE_LABELS["content_knowledge"])


def infer_module_from_normalized(normalized: ExternalAnalysisNormalizedModel | None) -> str:
    if normalized is None:
        return "content_knowledge"
    targets: list[str] = []
    for audit in normalized.object_review_audits:
        targets.append(_compact_text(audit.target_type).lower())
        targets.append(_compact_text(audit.field_name).lower())
        corrected = audit.corrected_value if isinstance(audit.corrected_value, dict) else {}
        targets.extend(_compact_text(corrected.get(key)).lower() for key in ("target_type", "field_name"))
    for correction in normalized.correction_proposals:
        targets.append(_compact_text(correction.field_name).lower())
        targets.append(_compact_text(correction.target_path).lower())
    for opinion in normalized.external_audit_opinions:
        if opinion.dft_status:
            targets.append("dft")
        if opinion.figure_status:
            targets.append("figure")
        if opinion.table_status:
            targets.append("table")
    joined = " ".join(targets)
    if "dft" in joined:
        return "dft"
    if any(token in joined for token in ("figure", "table", "chart")):
        return "figure_table_evidence"
    if any(token in joined for token in ("writing", "card", "draft")):
        return "writing"
    return "content_knowledge"


def _count_candidates_by_type(normalized: ExternalAnalysisNormalizedModel | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if normalized is None:
        return counts
    counts["external_audit_opinion"] += len(normalized.external_audit_opinions)
    counts["object_review_audit"] += len(normalized.object_review_audits)
    counts["note"] += len(normalized.review_notes)
    counts["correction"] += len(normalized.correction_proposals)
    counts["relationship"] += len(normalized.supporting_papers)
    counts["unmapped"] += len(normalized.unmapped_items)
    return Counter({key: value for key, value in counts.items() if value})


def _candidate_status_counts(
    normalized: ExternalAnalysisNormalizedModel | None,
    *,
    mapping_error: str | None = None,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    if mapping_error:
        counts["failed"] += 1
        return counts
    if normalized is None:
        return counts
    counts["candidate"] += len(normalized.external_audit_opinions) + len(normalized.object_review_audits)
    counts["pending"] += len(normalized.review_notes)
    for correction in normalized.correction_proposals:
        status = ExternalAnalysisMaterializationMixin._correction_candidate_status(correction)
        counts[status] += 1
    for relationship in normalized.supporting_papers:
        counts["pending" if relationship.target_paper_id else "requires_resolution"] += 1
    counts["requires_resolution"] += len(normalized.unmapped_items)
    return Counter({key: value for key, value in counts.items() if value})


def _problem_items(
    normalized: ExternalAnalysisNormalizedModel | None,
    *,
    mapping_error: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if mapping_error:
        items.append({"status": "failed", "reason": mapping_error, "candidate_type": "external_analysis_run"})
    if normalized is None:
        return items[:limit]
    for item in normalized.unmapped_items:
        reason = item.get("mapping_reason") if isinstance(item, dict) else None
        items.append({"status": "unmapped", "candidate_type": "unmapped", "reason": reason or "Could not safely map item"})
        if len(items) >= limit:
            return items
    for relationship in normalized.supporting_papers:
        if relationship.target_paper_id:
            continue
        items.append(
            {
                "status": "requires_resolution",
                "candidate_type": "relationship",
                "reason": relationship.mapping_reason or "Supporting paper target is unresolved",
                "title": relationship.target_title,
            }
        )
        if len(items) >= limit:
            return items
    for correction in normalized.correction_proposals:
        status = ExternalAnalysisMaterializationMixin._correction_candidate_status(correction)
        if status != "requires_resolution":
            continue
        items.append(
            {
                "status": status,
                "candidate_type": "correction",
                "field_name": correction.field_name,
                "target_path": correction.target_path,
                "reason": correction.mapping_reason or correction.reason,
            }
        )
        if len(items) >= limit:
            return items
    return items


_SUCCESS_CANDIDATE_STATUSES = {"ai_applied", "materialized", "applied", "validated", "approved", "ai_reviewed"}
_PENDING_CANDIDATE_STATUSES = {"pending", "candidate"}
_PROBLEM_CANDIDATE_STATUSES = {"requires_resolution", "unmapped", "failed", "skipped", "needs_human"}
_BLOCKING_CANDIDATE_STATUSES = {"failed", "skipped", "unmapped"}


def _infer_module_from_candidates(candidates: list[ExternalAnalysisCandidate]) -> str:
    targets: list[str] = []
    for candidate in candidates:
        candidate_type = _compact_text(candidate.candidate_type).lower()
        targets.append(candidate_type)
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        for key in ("target_type", "field_name", "target_path", "module", "module_name"):
            targets.append(_compact_text(payload.get(key)).lower())
        nested = payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {}
        for key in ("target_type", "field_name", "target_path", "module", "module_name"):
            targets.append(_compact_text(nested.get(key)).lower())
    joined = " ".join(targets)
    if "dft" in joined:
        return "dft"
    if any(token in joined for token in ("figure", "table", "chart")):
        return "figure_table_evidence"
    if any(token in joined for token in ("writing", "card", "draft")):
        return "writing"
    if any(token in joined for token in ("text", "evidence_check")):
        return "text_review"
    return "content_knowledge"


def _external_analysis_candidate_summary(
    run: ExternalAnalysisRun,
    candidates: list[ExternalAnalysisCandidate],
) -> dict[str, Any]:
    statuses = Counter(_compact_text(item.status).lower() or "pending" for item in candidates)
    candidate_types = Counter(_compact_text(item.candidate_type).lower() or "unknown" for item in candidates)
    problem_items = [
        {
            "status": _compact_text(item.status) or "pending",
            "candidate_type": item.candidate_type,
            "reason": item.mapping_reason,
            "candidate_id": str(item.id),
        }
        for item in candidates
        if _compact_text(item.status).lower() in _PROBLEM_CANDIDATE_STATUSES
    ][:8]
    if run.mapping_error:
        problem_items.insert(
            0,
            {"status": "failed", "reason": run.mapping_error, "candidate_type": "external_analysis_run"},
        )
        problem_items = problem_items[:8]
    total = len(candidates)
    success = sum(statuses.get(status, 0) for status in _SUCCESS_CANDIDATE_STATUSES)
    pending = sum(statuses.get(status, 0) for status in _PENDING_CANDIDATE_STATUSES)
    problem = sum(statuses.get(status, 0) for status in _PROBLEM_CANDIDATE_STATUSES) + (1 if run.mapping_error else 0)
    blocking = sum(statuses.get(status, 0) for status in _BLOCKING_CANDIDATE_STATUSES) + (1 if run.mapping_error else 0)
    if run.mapping_error:
        lifecycle = "failed"
    elif any(statuses.get(status, 0) for status in ("needs_human", "requires_resolution", "unmapped")):
        lifecycle = "needs_human"
    elif pending:
        lifecycle = "awaiting_review"
    elif success:
        lifecycle = "applied"
    elif total:
        lifecycle = "validated"
    else:
        lifecycle = "imported"
    metrics = {
        "total": total,
        "candidate_count": total,
        "success": success,
        "success_count": success,
        "pending": pending,
        "pending_count": pending,
        "problem": problem,
        "problem_count": problem,
        "blocking": blocking,
        "blocking_count": blocking,
        "by_candidate_type": dict(candidate_types),
        "by_status": dict(statuses),
        "failure_count": statuses.get("failed", 0) + (1 if run.mapping_error else 0),
    }
    return {
        "module": _infer_module_from_candidates(candidates),
        "metrics": metrics,
        "problem_items": problem_items,
        "lifecycle": lifecycle,
    }


class TaskLogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def backfill_missing_external_analysis_tasks(
        self,
        *,
        paper_code: str | None = None,
        paper_id: UUID | str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Report or create one audit task for each candidate-bearing run without one.

        This method only adds missing WorkflowJob rows when ``apply`` is true. It
        never changes the run, candidates, content projection, or review state.
        """
        stmt = (
            select(ExternalAnalysisRun, Paper)
            .join(Paper, Paper.id == ExternalAnalysisRun.paper_id)
            .order_by(ExternalAnalysisRun.created_at.asc(), ExternalAnalysisRun.id.asc())
        )
        if paper_code:
            stmt = stmt.where(Paper.paper_code == str(paper_code).strip())
        if paper_id:
            stmt = stmt.where(Paper.id == UUID(str(paper_id)))

        report: dict[str, Any] = {
            "apply": apply,
            "paper_code": paper_code,
            "paper_id": str(paper_id) if paper_id else None,
            "scanned_runs": 0,
            "candidate_runs": 0,
            "existing_tasks": 0,
            "missing_runs": [],
            "expected_new_tasks": 0,
            "created_tasks": 0,
            "created_job_ids": [],
        }
        for run, paper in self.session.execute(stmt).all():
            report["scanned_runs"] += 1
            candidates = list(
                self.session.scalars(
                    select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id)
                ).all()
            )
            if not candidates:
                continue
            report["candidate_runs"] += 1
            existing = self.session.scalar(
                select(WorkflowJob).where(
                    WorkflowJob.type == "agent_activity",
                    WorkflowJob.payload["external_analysis_run_id"].astext == str(run.id),
                )
            )
            if existing is not None:
                report["existing_tasks"] += 1
                continue

            summary = _external_analysis_candidate_summary(run, candidates)
            task_source, task_source_label = normalize_task_source(run.source)
            task_module, module_label = normalize_task_module(summary["module"])
            display_name = f"{task_source_label}：{module_label}"
            source_display = (
                f"{task_source_label}（已认证：{run.source_identity}）"
                if run.source_identity_verified
                else f"{task_source_label}（声明来源；未认证 HTTP 载荷）"
            )
            metrics = summary["metrics"]
            summary_text = (
                f"历史补建：{metrics['total']} 个候选项；成功 {metrics['success']}，"
                f"待处理 {metrics['pending']}，问题 {metrics['problem']}。"
            )
            common = {
                "task_log_version": 1,
                "task_display_name": display_name,
                "task_source": task_source,
                "task_source_label": task_source_label,
                "source": task_source,
                "source_label": run.source_label or task_source_label,
                "source_display": source_display,
                "source_identity": run.source_identity,
                "source_identity_verified": bool(run.source_identity_verified),
                "source_trust": "verified" if run.source_identity_verified else "unverified",
                "module": task_module,
                "module_label": module_label,
                "paper_id": str(paper.id),
                "paper_code": paper.paper_code,
                "paper_title": paper.title,
                "external_analysis_run_id": str(run.id),
                "backfilled": True,
            }
            report["expected_new_tasks"] += 1
            report["missing_runs"].append(
                {
                    "run_id": str(run.id),
                    "paper_id": str(paper.id),
                    "paper_code": paper.paper_code,
                    "candidate_count": metrics["total"],
                    "source": task_source,
                    "source_label": run.source_label or task_source_label,
                    "source_identity": run.source_identity,
                    "source_identity_verified": bool(run.source_identity_verified),
                    "module": task_module,
                    "module_label": module_label,
                    "lifecycle": summary["lifecycle"],
                }
            )
            if not apply:
                continue
            job = WorkflowJob(
                job_id=str(uuid4()),
                type="agent_activity",
                status="failed" if summary["lifecycle"] == "failed" else "completed",
                library_name=normalize_library_name(paper.library_name),
                payload={
                    **common,
                    "action": "backfill_external_analysis_task",
                    "agent": run.source_label or task_source_label,
                    "title": display_name,
                },
                progress={
                    "phase": summary["lifecycle"],
                    "action": "backfilled",
                    "message": summary_text,
                    "paper_id": str(paper.id),
                    "paper_code": paper.paper_code,
                    "task_display_name": display_name,
                    "lifecycle": summary["lifecycle"],
                    "backfilled": True,
                },
                result={
                    **common,
                    "summary_text": summary_text,
                    "metrics": metrics,
                    "problem_items": summary["problem_items"],
                    "artifacts": [{"type": "external_analysis_run", "run_id": str(run.id)}],
                    "details": {
                        "mapping_status": run.mapping_status,
                        "mapping_error": run.mapping_error,
                        "paper_code": paper.paper_code,
                        "source": run.source,
                        "source_label": run.source_label,
                    },
                    "success_count": metrics["success"],
                    "failure_count": metrics["failure_count"],
                    "lifecycle": summary["lifecycle"],
                    "last_action": "backfilled",
                    "backfilled": True,
                },
                runtime_context={},
            )
            self.session.add(job)
            report["created_tasks"] += 1
            report["created_job_ids"].append(job.job_id)
        return report

    def record_external_analysis_import(
        self,
        *,
        paper: Paper,
        run: ExternalAnalysisRun,
        normalized: ExternalAnalysisNormalizedModel | None,
        source: str | None,
        source_label: str | None = None,
        module: str | None = None,
    ) -> WorkflowJob:
        inferred_module = module or infer_module_from_normalized(normalized)
        task_source, task_source_label = normalize_task_source(source)
        task_module, module_label = normalize_task_module(inferred_module)
        display_name = f"{task_source_label}：{module_label}"
        by_type = _count_candidates_by_type(normalized)
        by_status = _candidate_status_counts(normalized, mapping_error=run.mapping_error)
        candidate_count = sum(by_type.values())
        problem_items = _problem_items(normalized, mapping_error=run.mapping_error)
        problem_count = sum(by_status.get(status, 0) for status in ("requires_resolution", "unmapped", "failed", "skipped"))
        metrics = {
            "candidate_count": candidate_count,
            "total": candidate_count,
            "by_candidate_type": dict(by_type),
            "by_status": dict(by_status),
            "applied": 0,
            "created": 0,
            "requires_resolution": by_status.get("requires_resolution", 0),
            "skipped": by_status.get("skipped", 0),
            "success_count": 0 if run.mapping_error else candidate_count,
            "failure_count": 1 if run.mapping_error else 0,
            "problem_count": problem_count,
        }
        summary_text = (
            f"导入 {candidate_count} 个候选项"
            + (f"，{problem_count} 个需处理" if problem_count else "")
            + f"。映射状态：{run.mapping_status or '-'}"
        )
        common = {
            "task_log_version": 1,
            "task_display_name": display_name,
            "task_source": task_source,
            "task_source_label": task_source_label,
            "source": task_source,
            "source_label": source_label or task_source_label,
            "module": task_module,
            "module_label": module_label,
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "paper_title": paper.title,
            "external_analysis_run_id": str(run.id),
            "source_identity": run.source_identity,
            "source_identity_verified": bool(run.source_identity_verified),
            "source_trust": "verified" if run.source_identity_verified else "unverified",
            "source_display": (
                f"{task_source_label}（已认证：{run.source_identity}）"
                if run.source_identity_verified
                else f"{task_source_label}（声明来源；未认证 HTTP 载荷）"
            ),
        }
        # A run is a batch task.  Never create a top-level task per image/field.
        job = self.session.scalar(
            select(WorkflowJob).where(
                WorkflowJob.type == "agent_activity",
                WorkflowJob.payload["external_analysis_run_id"].astext == str(run.id),
            )
        )
        if job is not None:
            self.refresh_external_analysis_task(run.id, last_action="imported", lifecycle="failed" if run.mapping_error else "imported")
            return job
        job = WorkflowJob(
            job_id=str(uuid4()),
            type="agent_activity",
            status="completed",
            library_name=normalize_library_name(paper.library_name),
            payload={**common, "action": "import_analysis", "agent": source_label or task_source_label, "title": display_name},
            progress={
                "phase": "failed" if run.mapping_error else "imported",
                "action": "import_analysis",
                "message": summary_text,
                "paper_id": str(paper.id),
                "paper_code": paper.paper_code,
                "task_display_name": display_name,
                "lifecycle": "failed" if run.mapping_error else "imported",
            },
            result={
                **common,
                "summary_text": summary_text,
                "metrics": metrics,
                "problem_items": problem_items,
                "artifacts": [{"type": "external_analysis_run", "run_id": str(run.id)}],
                "details": {
                    "mapping_status": run.mapping_status,
                    "mapping_error": run.mapping_error,
                    "paper_code": paper.paper_code,
                    "source": source,
                    "source_label": source_label,
                },
                "success_count": metrics["success_count"],
                "failure_count": metrics["failure_count"],
                "lifecycle": "failed" if run.mapping_error else "imported",
                "last_action": "imported",
            },
            runtime_context={},
        )
        self.session.add(job)
        return job

    def refresh_external_analysis_task(
        self,
        run_id,
        *,
        last_action: str,
        lifecycle: str | None = None,
    ) -> WorkflowJob | None:
        """Refresh the single batch task from current candidate state."""
        run = self.session.get(ExternalAnalysisRun, run_id)
        if run is None:
            return None
        job = self.session.scalar(
            select(WorkflowJob).where(
                WorkflowJob.type == "agent_activity",
                WorkflowJob.payload["external_analysis_run_id"].astext == str(run.id),
            )
        )
        if job is None:
            return None
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == run.id)
        ).all()
        statuses = Counter(_compact_text(item.status).lower() or "pending" for item in candidates)
        candidate_types = Counter(_compact_text(item.candidate_type).lower() or "unknown" for item in candidates)
        total = len(candidates)
        problems = [
            {"status": _compact_text(item.status), "candidate_type": item.candidate_type,
             "reason": item.mapping_reason, "candidate_id": str(item.id)}
            for item in candidates if _compact_text(item.status).lower() in {"requires_resolution", "unmapped", "failed", "skipped", "needs_human"}
        ][:8]
        if lifecycle is None:
            if run.mapping_error:
                lifecycle = "failed"
            elif statuses.get("requires_resolution") or statuses.get("needs_human") or statuses.get("unmapped"):
                lifecycle = "needs_human"
            elif any(statuses.get(value) for value in ("pending", "candidate")):
                lifecycle = "awaiting_review"
            elif any(statuses.get(value) for value in ("ai_applied", "materialized", "applied")):
                lifecycle = "applied"
            elif total:
                lifecycle = "validated"
            else:
                lifecycle = "imported"
        success = sum(count for status, count in statuses.items() if status in {"ai_applied", "materialized", "applied", "validated", "approved", "ai_reviewed"})
        metrics = {
            "total": total, "candidate_count": total, "by_candidate_type": dict(candidate_types), "by_status": dict(statuses),
            "success_count": success, "pending_count": statuses.get("pending", 0) + statuses.get("candidate", 0),
            "problem_count": len(problems), "blocking_count": sum(count for status, count in statuses.items() if status in {"failed", "skipped", "unmapped"}),
            "failure_count": statuses.get("failed", 0),
        }
        result = dict(job.result or {})
        result.update({"metrics": metrics, "problem_items": problems, "last_action": last_action, "lifecycle": lifecycle,
                       "summary_text": f"{total} 个候选项；成功 {success}，待处理 {metrics['pending_count']}，问题 {len(problems)}。"})
        progress = dict(job.progress or {})
        progress.update({"phase": lifecycle, "action": last_action, "lifecycle": lifecycle, "message": result["summary_text"]})
        job.result, job.progress = result, progress
        job.status = "failed" if lifecycle == "failed" else "completed"
        self.session.add(job)
        self.session.flush()
        return job


__all__ = [
    "TaskLogService",
    "infer_module_from_normalized",
    "normalize_task_module",
    "normalize_task_source",
]
