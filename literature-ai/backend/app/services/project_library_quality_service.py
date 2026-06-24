from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExternalAnalysisCandidate, Paper
from app.domain.project_library_context import get_project_library_context
from app.services.dft_export_service import build_dft_ml_dataset_v3
from app.services.lis_sac_dac_feature_service import LiSSacDacFeatureService
from app.services.project_library_queue_service import ProjectLibraryQueueService
from app.utils.library_names import normalize_library_name


class ProjectLibraryQualityService:
    SCHEMA_VERSION = "project_library_quality_v1"
    FEATURE_CANDIDATE_TYPES = ("structure_features", "experimental_performance")

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_quality_panel(
        self,
        *,
        context_key: str,
        library_name: str | None = None,
    ) -> dict[str, Any]:
        context = get_project_library_context(context_key)
        effective_library_name = library_name if library_name is not None else context.default_library_name
        normalized_library_name = (
            normalize_library_name(effective_library_name)
            if effective_library_name is not None else None
        )

        queue_payload = ProjectLibraryQueueService(self.session).build_queue(
            context_key=context.key,
            library_name=effective_library_name,
        )
        papers = queue_payload["papers"]
        paper_ids = [paper["paper_id"] for paper in papers]
        feature_paper_blockers, feature_blocker_counts = self._feature_candidate_blockers_by_paper(
            paper_ids=paper_ids
        )

        task_summaries: list[dict[str, Any]] = []
        task_blocker_counts = Counter()
        total_task_candidates = 0
        total_label_ready = 0
        total_training_ready = 0
        for task in context.tabular_tasks:
            payload = build_dft_ml_dataset_v3(
                self.session,
                task=task,
                ready_only=False,
                library_name=effective_library_name,
            )
            blocker_counts = Counter()
            for record in payload["records"]:
                for blocker in (record.get("label_blockers") or []) + (record.get("feature_blockers") or []):
                    blocker_counts[str(blocker)] += 1
                    task_blocker_counts[str(blocker)] += 1
            manifest = payload["manifest"]
            total_task_candidates += int(manifest["task_candidate_count"])
            total_label_ready += int(manifest["label_ready_count"])
            total_training_ready += int(manifest["tabular_ready_count"])
            task_summaries.append(
                {
                    "task": manifest["task"],
                    "reaction_type": manifest["reaction_profile"],
                    "task_status": manifest["task_status"],
                    "task_candidate_count": int(manifest["task_candidate_count"]),
                    "label_ready_count": int(manifest["label_ready_count"]),
                    "training_ready_count": int(manifest["tabular_ready_count"]),
                    "excluded_counts": dict(sorted((manifest.get("excluded_counts") or {}).items())),
                    "blocker_counts": dict(sorted(blocker_counts.items())),
                }
            )

        needs_fields_papers: list[dict[str, Any]] = []
        feature_candidate_blocked_paper_count = 0
        for paper in papers:
            merged_feature_blockers = dict(sorted(feature_paper_blockers.get(paper["paper_id"], {}).items()))
            if merged_feature_blockers:
                feature_candidate_blocked_paper_count += 1
            if not paper["needs_fields"] and not merged_feature_blockers:
                continue
            needs_fields_papers.append(
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "library_name": paper["library_name"],
                    "dominant_state": paper["dominant_state"],
                    "task_candidate_count": paper["task_candidate_count"],
                    "label_ready_count": paper["label_ready_count"],
                    "training_ready_count": paper["tabular_ready_count"],
                    "matched_tasks": paper["matched_tasks"],
                    "blocker_counts": paper["blocker_counts"],
                    "feature_candidate_blocker_counts": merged_feature_blockers,
                }
            )

        queue_counts = queue_payload["counts"]
        return {
            "schema_version": self.SCHEMA_VERSION,
            "context_key": context.key,
            "context_version": context.version,
            "context_display_name_zh": context.display_name_zh,
            "library_name": normalized_library_name,
            "read_only": True,
            "auto_verification_applied": False,
            "counts": {
                "paper_count": int(queue_counts["paper_count"]),
                "parsed_count": int(queue_counts["parsed_count"]),
                "with_dft_count": int(queue_counts["with_dft_count"]),
                "needs_fields_count": int(queue_counts["needs_fields_count"]),
                "srr_lis_task_candidate_count": total_task_candidates,
                "label_ready_count": total_label_ready,
                "training_ready_count": total_training_ready,
                "feature_candidate_blocked_paper_count": feature_candidate_blocked_paper_count,
            },
            "blocker_counts": dict(sorted(task_blocker_counts.items())),
            "feature_candidate_blocker_counts": dict(sorted(feature_blocker_counts.items())),
            "tasks": task_summaries,
            "needs_fields_papers": needs_fields_papers,
        }

    def _feature_candidate_blockers_by_paper(
        self,
        *,
        paper_ids: list[str],
    ) -> tuple[dict[str, Counter], Counter]:
        if not paper_ids:
            return {}, Counter()

        rows = self.session.scalars(
            select(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.paper_id.in_(paper_ids),
                ExternalAnalysisCandidate.candidate_type.in_(self.FEATURE_CANDIDATE_TYPES),
            )
        ).all()
        blockers_by_paper: dict[str, Counter] = defaultdict(Counter)
        overall = Counter()
        feature_service = LiSSacDacFeatureService()

        for row in rows:
            if not isinstance(row.normalized_payload, dict):
                continue
            if row.candidate_type == "structure_features":
                payload = feature_service.extract_structure_features(
                    candidate_payload=row.normalized_payload,
                )
            elif row.candidate_type == "experimental_performance":
                payload = feature_service.extract_experimental_performance_features(
                    candidate_payload=row.normalized_payload,
                )
            else:
                continue
            if not payload.blockers:
                continue
            paper_key = str(row.paper_id)
            for blocker in payload.blockers:
                blockers_by_paper[paper_key][blocker] += 1
                overall[blocker] += 1

        return blockers_by_paper, overall

