from __future__ import annotations

import os
import tempfile
from typing import Any

from sqlalchemy.orm import Session

from app.domain.project_library_context import get_project_library_context
from app.domain.tabular_task_profiles import get_tabular_task_profile
from app.services.dft_export_service import build_dft_ml_dataset_v3, build_dft_ml_dataset_v3_csv
from app.utils.library_names import normalize_library_name
from tools.ml_baseline_srr_lis import run_baseline


class ProjectLibraryMLService:
    SCHEMA_VERSION = "project_library_ml_export_v1"

    def __init__(self, session: Session) -> None:
        self.session = session

    def build_ml_export_summary(
        self,
        *,
        context_key: str,
        task: str = "adsorption_energy",
        library_name: str | None = None,
    ) -> dict[str, Any]:
        context = get_project_library_context(context_key)
        profile = get_tabular_task_profile(task)
        effective_library_name = library_name if library_name is not None else context.default_library_name
        normalized_library_name = (
            normalize_library_name(effective_library_name)
            if effective_library_name is not None else None
        )
        candidate_payload = build_dft_ml_dataset_v3(
            self.session,
            task=task,
            ready_only=False,
            library_name=effective_library_name,
        )
        csv_text, training_manifest = build_dft_ml_dataset_v3_csv(
            self.session,
            task=task,
            ready_only=True,
            library_name=effective_library_name,
        )
        baseline = self._run_baseline(csv_text, target=task)
        blockers = []
        if int(candidate_payload["manifest"]["task_candidate_count"]) == 0:
            blockers.append("no_task_candidates")
        if int(training_manifest["returned_count"]) == 0:
            blockers.append("no_training_rows")
        if baseline["status"] == "insufficient":
            blockers.append("insufficient_data")
        if baseline["status"] == "skipped":
            blockers.append("baseline_skipped")
        blockers = sorted(set(blockers))

        return {
            "schema_version": self.SCHEMA_VERSION,
            "context_key": context.key,
            "context_version": context.version,
            "context_display_name_zh": context.display_name_zh,
            "library_name": normalized_library_name,
            "task": profile.key,
            "reaction_type": profile.reaction_type,
            "read_only": True,
            "auto_verification_applied": False,
            "status": "ready" if baseline["status"] == "ok" else "not_ready",
            "ready_for_baseline": baseline["status"] == "ok",
            "blockers": blockers,
            "csv_filename": self.csv_filename(task),
            "candidate_manifest": candidate_payload["manifest"],
            "training_manifest": training_manifest,
            "baseline": baseline,
        }

    def build_ml_export_csv(
        self,
        *,
        context_key: str,
        task: str = "adsorption_energy",
        library_name: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        context = get_project_library_context(context_key)
        effective_library_name = library_name if library_name is not None else context.default_library_name
        return build_dft_ml_dataset_v3_csv(
            self.session,
            task=task,
            ready_only=True,
            library_name=effective_library_name,
        )

    @staticmethod
    def csv_filename(task: str) -> str:
        safe_task = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in task).strip("_")
        return f"project_library_ml_export_{safe_task or 'task'}.csv"

    def _run_baseline(self, csv_text: str, *, target: str) -> dict[str, Any]:
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
        try:
            handle.write(csv_text)
            handle.close()
            return run_baseline(handle.name, target=target)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
