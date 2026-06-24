from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, Paper, PaperSection
from app.domain.project_library_context import get_project_library_context
from app.services.dft_export_service import build_dft_ml_dataset_v3
from app.utils.library_names import build_library_name_clause, normalize_library_name


def _paper_has_parsed_content(paper: Paper, section_count: int) -> bool:
    return bool(
        section_count
        or str(paper.markdown_path or "").strip()
        or str(paper.docling_json_path or "").strip()
        or str(paper.tei_path or "").strip()
    )


def _needs_field_blocker(blocker: str) -> bool:
    return blocker.startswith("missing_") or blocker in {
        "ambiguous_result_setting_link",
        "instance_ambiguous",
        "unsupported_catalyst_scope",
    }


def _dominant_state(
    *,
    training_ready: bool,
    export_ready: bool,
    needs_fields: bool,
    pending_review: bool,
    has_dft: bool,
    parsed: bool,
) -> str:
    if training_ready:
        return "training_ready"
    if export_ready:
        return "export_ready"
    if needs_fields:
        return "needs_fields"
    if pending_review:
        return "pending_review"
    if has_dft:
        return "has_dft"
    if parsed:
        return "parsed"
    return "imported"


class ProjectLibraryQueueService:
    SCHEMA_VERSION = "project_library_queue_v1"

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_queue(
        self,
        *,
        context_key: str,
        library_name: str | None = None,
    ) -> dict[str, Any]:
        context = get_project_library_context(context_key)
        effective_library_name = (
            library_name if library_name is not None else context.default_library_name
        )
        normalized_library_name = (
            normalize_library_name(effective_library_name)
            if effective_library_name is not None else None
        )

        papers_stmt = select(Paper).order_by(Paper.year.desc().nulls_last(), Paper.title, Paper.id)
        if effective_library_name is not None:
            papers_stmt = papers_stmt.where(
                build_library_name_clause(Paper.library_name, effective_library_name)
            )
        papers = self.session.scalars(papers_stmt).all()
        paper_ids = [paper.id for paper in papers]

        dft_counts: dict[str, int] = {}
        section_counts: dict[str, int] = {}
        if paper_ids:
            dft_counts = {
                str(paper_id): count
                for paper_id, count in self.session.execute(
                    select(DFTResult.paper_id, func.count(DFTResult.id))
                    .where(DFTResult.paper_id.in_(paper_ids))
                    .group_by(DFTResult.paper_id)
                ).all()
            }
            section_counts = {
                str(paper_id): count
                for paper_id, count in self.session.execute(
                    select(PaperSection.paper_id, func.count(PaperSection.id))
                    .where(PaperSection.paper_id.in_(paper_ids))
                    .group_by(PaperSection.paper_id)
                ).all()
            }

        metrics_by_paper: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "task_candidate_count": 0,
                "label_ready_count": 0,
                "tabular_ready_count": 0,
                "matched_tasks": set(),
                "blocker_counts": Counter(),
            }
        )
        for task in context.tabular_tasks:
            payload = build_dft_ml_dataset_v3(
                self.session,
                task=task,
                ready_only=False,
                library_name=effective_library_name,
            )
            for record in payload["records"]:
                paper_id = str(record["paper"]["paper_id"])
                metrics = metrics_by_paper[paper_id]
                metrics["task_candidate_count"] += 1
                metrics["label_ready_count"] += int(record["label_ready"] is True)
                metrics["tabular_ready_count"] += int(record["tabular_ml_ready"] is True)
                metrics["matched_tasks"].add(task)
                for blocker in (record.get("label_blockers") or []) + (record.get("feature_blockers") or []):
                    metrics["blocker_counts"][str(blocker)] += 1

        counts = Counter()
        rows: list[dict[str, Any]] = []
        for paper in papers:
            paper_id = str(paper.id)
            metrics = metrics_by_paper[paper_id]
            blocker_counts = dict(sorted(metrics["blocker_counts"].items()))
            parsed = _paper_has_parsed_content(paper, section_counts.get(paper_id, 0))
            has_dft = dft_counts.get(paper_id, 0) > 0
            export_ready = metrics["label_ready_count"] > 0
            training_ready = metrics["tabular_ready_count"] > 0
            needs_fields = metrics["task_candidate_count"] > 0 and (
                any(_needs_field_blocker(name) for name in blocker_counts)
                or metrics["label_ready_count"] > metrics["tabular_ready_count"]
            )
            pending_review = has_dft and not training_ready
            dominant_state = _dominant_state(
                training_ready=training_ready,
                export_ready=export_ready,
                needs_fields=needs_fields,
                pending_review=pending_review,
                has_dft=has_dft,
                parsed=parsed,
            )

            row = {
                "paper_id": paper_id,
                "title": paper.title,
                "library_name": paper.library_name,
                "imported": True,
                "parsed": parsed,
                "has_dft": has_dft,
                "pending_review": pending_review,
                "export_ready": export_ready,
                "training_ready": training_ready,
                "needs_fields": needs_fields,
                "dominant_state": dominant_state,
                "dft_result_count": dft_counts.get(paper_id, 0),
                "task_candidate_count": metrics["task_candidate_count"],
                "label_ready_count": metrics["label_ready_count"],
                "tabular_ready_count": metrics["tabular_ready_count"],
                "matched_tasks": sorted(metrics["matched_tasks"]),
                "blocker_counts": blocker_counts,
            }
            rows.append(row)
            counts["paper_count"] += 1
            counts["imported_count"] += 1
            counts["parsed_count"] += int(parsed)
            counts["with_dft_count"] += int(has_dft)
            counts["pending_review_count"] += int(pending_review)
            counts["export_ready_count"] += int(export_ready)
            counts["training_ready_count"] += int(training_ready)
            counts["needs_fields_count"] += int(needs_fields)

        return {
            "schema_version": self.SCHEMA_VERSION,
            "context_key": context.key,
            "context_version": context.version,
            "context_display_name_zh": context.display_name_zh,
            "reaction_types": list(context.reaction_types),
            "tabular_tasks": list(context.tabular_tasks),
            "library_name": normalized_library_name,
            "read_only": True,
            "auto_verification_applied": False,
            "counts": {
                "paper_count": counts["paper_count"],
                "imported_count": counts["imported_count"],
                "parsed_count": counts["parsed_count"],
                "with_dft_count": counts["with_dft_count"],
                "pending_review_count": counts["pending_review_count"],
                "export_ready_count": counts["export_ready_count"],
                "training_ready_count": counts["training_ready_count"],
                "needs_fields_count": counts["needs_fields_count"],
            },
            "papers": rows,
        }
