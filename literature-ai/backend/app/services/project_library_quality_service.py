from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExternalAnalysisCandidate, Paper
from app.domain.element_descriptors import build_metal_descriptor_payload
from app.domain.project_library_context import get_project_library_context
from app.services.dft_export_service import build_dft_ml_dataset_v3
from app.services.lis_sac_dac_feature_service import LiSSacDacFeatureService
from app.services.project_library_bundle_service import ProjectLibraryBundleService
from app.services.project_library_queue_service import ProjectLibraryQueueService
from app.utils.library_names import normalize_library_name


_LI2S_BARRIER_SUBTYPES = {
    "li2s_decomposition_barrier",
    "li2s_deposition_barrier",
    "li2s_nucleation_barrier",
    "migration_barrier",
}


def _token(value: Any) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip().lower()).strip("_")


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
        bundle_payload = ProjectLibraryBundleService(self.session).build_bundles(
            context_key=context.key,
            library_name=effective_library_name,
        )
        bundle_counts = bundle_payload["counts"]
        sample_quality = self._sample_quality_summary(bundle_payload)

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
                "catalyst_sample_count": int(bundle_counts.get("catalyst_sample_count", 0)),
                "active_site_instance_count": int(bundle_counts.get("active_site_instance_count", 0)),
                "ambiguous_records_count": int(bundle_counts.get("ambiguous_records_count", 0)),
                "manual_verification_required_count": int(bundle_counts.get("manual_verification_required_count", 0)),
            },
            "blocker_counts": dict(sorted(task_blocker_counts.items())),
            "feature_candidate_blocker_counts": dict(sorted(feature_blocker_counts.items())),
            "sample_quality": sample_quality,
            "tasks": task_summaries,
            "needs_fields_papers": needs_fields_papers,
        }

    def _sample_quality_summary(self, bundle_payload: dict[str, Any]) -> dict[str, Any]:
        counts = Counter()
        gap_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

        def add_gap(name: str, *, bundle: dict[str, Any], catalyst: dict[str, Any], instance: dict[str, Any]) -> None:
            counts[name] += 1
            if len(gap_examples[name]) >= 5:
                return
            gap_examples[name].append(
                {
                    "paper_id": bundle.get("paper_id"),
                    "title": bundle.get("paper_title"),
                    "catalyst_sample_id": catalyst.get("catalyst_sample_id"),
                    "catalyst_name": catalyst.get("name"),
                    "active_site_instance_key": instance.get("active_site_instance_key"),
                }
            )

        for bundle in bundle_payload.get("bundles", []):
            catalyst = bundle.get("catalyst_sample") or {}
            descriptor_payload = build_metal_descriptor_payload(catalyst.get("metal_centers") or [])
            catalyst_scope = catalyst.get("catalyst_scope")
            for instance in bundle.get("active_site_instances", []):
                counts["total_sample_count"] += 1
                props = [
                    prop
                    for group_name in (
                        "adsorbate_properties",
                        "reaction_step_properties",
                        "electronic_properties",
                        "structure_properties",
                        "other_properties",
                    )
                    for prop in ((instance.get("properties") or {}).get(group_name) or [])
                ]
                safe_props = [prop for prop in props if prop.get("ml_ready")]
                has_li2s_adsorption = any(
                    prop.get("canonical_property_type") == "adsorption_energy"
                    and prop.get("canonical_adsorbate") == "Li2S"
                    for prop in safe_props
                )
                has_li2s_barrier = any(
                    prop.get("property_subtype") in _LI2S_BARRIER_SUBTYPES for prop in safe_props
                )
                has_rds = any("rds" in _token(prop.get("reaction_step")) for prop in safe_props)
                has_bader_or_charge = any(
                    prop.get("canonical_property_type") in {"bader_charge", "charge_transfer"}
                    or prop.get("bader_charge_M1") is not None
                    or prop.get("bader_charge_M2") is not None
                    or prop.get("charge_transfer_e") is not None
                    for prop in safe_props
                )
                has_metal_metal_distance = any(
                    prop.get("metal_metal_distance_A") is not None for prop in safe_props
                )
                has_unknown_descriptor = bool(descriptor_payload.get("descriptor_blockers"))

                if not has_li2s_adsorption:
                    add_gap("missing_li2s_adsorption_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)
                if not has_li2s_barrier:
                    add_gap("missing_li2s_barrier_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)
                if not has_rds:
                    add_gap("missing_rds_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)
                if not has_bader_or_charge:
                    add_gap("missing_bader_or_charge_transfer_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)
                if catalyst_scope == "DAC":
                    counts["dac_sample_count"] += 1
                    if not has_metal_metal_distance:
                        add_gap("dac_missing_metal_metal_distance_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)
                if has_unknown_descriptor:
                    add_gap("unknown_metal_descriptor_sample_count", bundle=bundle, catalyst=catalyst, instance=instance)

        return {
            "sample_unit": "active_site_instance",
            "counts": dict(sorted((key, int(value)) for key, value in counts.items())),
            "gap_examples": {key: value for key, value in sorted(gap_examples.items())},
            "notes": [
                "Counts are sample-level diagnostics over CatalystSample/ActiveSiteInstance bundles.",
                "Missing P1 structure/electronic fields do not automatically block P0 ML-ready labels.",
            ],
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
