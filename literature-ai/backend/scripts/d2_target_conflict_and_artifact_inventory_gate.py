from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.utils.active_database import get_active_database_info
from scripts import d2_historical_mirror_migration_readiness as readiness


TARGET_CONFLICT_FILENAMES = ("database.sqlite", "library.json")


def _target_conflicts(
    readiness_report: dict[str, Any],
    *,
    source_root: Path,
    target_root: Path,
    target_database_summary: dict[str, Any],
    target_library_json_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    source_library_json = source_root / "library.json"
    source_summaries = {
        "database.sqlite": readiness.sqlite_file_summary(source_root / "database.sqlite"),
        "library.json": readiness.json_file_summary(source_library_json),
    }
    target_summaries = {
        "database.sqlite": target_database_summary,
        "library.json": target_library_json_summary,
    }

    conflicts: list[dict[str, Any]] = []
    for item in readiness_report.get("path_conflicts", []):
        relative_path = str(item.get("relative_path") or "")
        if relative_path not in TARGET_CONFLICT_FILENAMES:
            continue
        conflicts.append(
            {
                **item,
                "source_summary": source_summaries[relative_path],
                "target_summary": target_summaries[relative_path],
            }
        )
    return conflicts


def _target_database_origin_assessment(summary: dict[str, Any], *, runtime_db_path: Path | None) -> str:
    if not summary.get("exists"):
        return "target database does not exist"
    if runtime_db_path is not None and Path(str(summary["path"])).resolve() == runtime_db_path.resolve():
        return "target database matches the current runtime DB path"
    if summary.get("papers_total") == 0 and int(summary.get("table_count") or 0) > 0:
        return "inference: target database is an initialized-but-empty library SQLite and not the active runtime DB"
    return "inference: target database exists but needs manual provenance review before any apply"


def _target_library_json_origin_assessment(summary: dict[str, Any]) -> str:
    if not summary.get("exists"):
        return "target library.json does not exist"
    if summary.get("is_valid_json_object"):
        return "inference: target library.json looks like library metadata already materialized under the proposed canonical root"
    return "inference: target library.json exists but is not a valid JSON object"


def _target_quarantine_plan(target_root: Path, target_conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    backup_destination = (readiness.WORKSPACE_ROOT / "backups" / "d2_target_root_quarantine_YYYYMMDD_HHMMSS").resolve()
    sibling_destination = target_root.with_name(f"{target_root.name}.quarantine_YYYYMMDD_HHMMSS").resolve()
    return {
        "dry_run_only": True,
        "apply_supported": False,
        "backup_required_before_move": True,
        "manifest_required_before_move": True,
        "rollback_plan_required": True,
        "recommended_strategy": "move_conflicting_target_files_to_timestamped_backup_subdir",
        "recommended_destination_template": str(backup_destination),
        "alternative_destination_template": str(sibling_destination),
        "files_to_quarantine": [
            {
                "relative_path": item["relative_path"],
                "target_path": item["target_path"],
                "target_sha256": item["target_summary"].get("sha256"),
                "target_size": item["target_summary"].get("size"),
            }
            for item in target_conflicts
        ],
        "preflight_manifest_fields": ["path", "size", "sha256", "mtime_utc"],
        "preconditions": [
            "Recompute SHA256 for canonical registry, shadow registries, active SQLite, target database.sqlite, and target library.json immediately before any move.",
            "Capture a manifest for every file currently under the target root before quarantine.",
            "Move only files proven not to be the current runtime DB or active registry target.",
            "Do not touch source mirror artifacts, canonical registry pointers, or the active SQLite in this gate.",
        ],
        "rollback_plan": [
            f"Restore quarantined files from the selected destination back into {target_root}.",
            "Re-verify target file SHA256 values against the preflight manifest.",
            "Re-run the D2-9 gate and confirm is_current_runtime_db remains false for the target database.",
            "Re-run D2-8 readiness before any future migration apply.",
        ],
    }


def build_report() -> dict[str, Any]:
    readiness_report = readiness.build_report()
    source_root = Path(readiness_report["current_active_library_root"]).resolve()
    target_root = Path(readiness_report["proposed_canonical_library_root"]).resolve()
    active_database_path = Path(readiness_report["current_active_database_path"]).resolve()
    target_database_path = target_root / "database.sqlite"
    target_library_json_path = target_root / "library.json"

    active_info = get_active_database_info()
    runtime_db_path = (
        Path(str(active_info["effective_db_path"])).resolve() if active_info.get("effective_db_path") else None
    )
    target_database_summary = readiness.sqlite_file_summary(target_database_path)
    target_library_json_summary = readiness.json_file_summary(target_library_json_path)
    target_database_summary["is_current_runtime_db"] = bool(
        runtime_db_path is not None and target_database_path.resolve() == runtime_db_path.resolve()
    )
    target_database_summary["runtime_binding_evidence"] = {
        "configured_db_path": active_info.get("configured_db_path"),
        "active_library_db_path": active_info.get("active_library_db_path"),
        "effective_db_path": active_info.get("effective_db_path"),
        "canonical_registry_active_root": readiness_report["current_active_library_root"],
        "target_database_path": str(target_database_path.resolve()),
        "effective_matches_active_library_db_path": active_info.get("effective_matches_active_library_db_path"),
    }
    target_database_summary["origin_assessment"] = _target_database_origin_assessment(
        target_database_summary,
        runtime_db_path=runtime_db_path,
    )

    target_library_json_summary["origin_assessment"] = _target_library_json_origin_assessment(target_library_json_summary)
    target_conflicts = _target_conflicts(
        readiness_report,
        source_root=source_root,
        target_root=target_root,
        target_database_summary=target_database_summary,
        target_library_json_summary=target_library_json_summary,
    )
    target_quarantine_plan = _target_quarantine_plan(target_root, target_conflicts)

    recommended_next_gate = (
        "approve_target_quarantine_manifest_then_repeat_d2_9_and_d2_8_before_any_migration_apply"
        if target_conflicts
        else "classify_remaining_unreferenced_artifacts_then_plan_referenced_only_migration_gate"
    )
    risk_level = "high" if target_conflicts or readiness_report["missing_referenced_files_count"] > 0 else "medium"

    return {
        "mode": "dry_run",
        "apply_supported": False,
        "apply_executed": False,
        "target_root": str(target_root),
        "target_conflicts": target_conflicts,
        "target_database_summary": target_database_summary,
        "target_library_json_summary": target_library_json_summary,
        "target_quarantine_plan": target_quarantine_plan,
        "source_root": str(source_root),
        "active_database_path": str(active_database_path),
        "active_database_papers_total": readiness_report["active_db_papers_total"],
        "source_file_inventory_all": readiness_report["source_file_inventory_all"],
        "source_file_inventory_db_referenced": readiness_report["source_file_inventory_db_referenced"],
        "source_file_inventory_unreferenced": readiness_report["source_file_inventory_unreferenced"],
        "db_referenced_files_to_copy_count": readiness_report["db_referenced_files_to_copy_count"],
        "all_source_files_to_copy_count": readiness_report["all_source_files_to_copy_count"],
        "unreferenced_files_count": readiness_report["unreferenced_files_count"],
        "unreferenced_pdf_count": readiness_report["unreferenced_pdf_count"],
        "unreferenced_pdf_total_bytes": readiness_report["unreferenced_pdf_total_bytes"],
        "unreferenced_pdf_examples": readiness_report["unreferenced_pdf_examples"],
        "unreferenced_pdf_mtime_range": readiness_report["unreferenced_pdf_mtime_range"],
        "unreferenced_pdf_origin_hints": readiness_report["unreferenced_pdf_origin_hints"],
        "unreferenced_non_pdf_count": readiness_report["unreferenced_non_pdf_count"],
        "missing_referenced_files_count": readiness_report["missing_referenced_files_count"],
        "duplicate_or_suspect_files": readiness_report["duplicate_or_suspect_files"],
        "migration_mode_recommendation": readiness_report["migration_mode_recommendation"],
        "risk_level": risk_level,
        "recommended_next_gate": recommended_next_gate,
        "sha256_stability": readiness_report["sha256_stability"],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="D2-9 target conflict quarantine + source artifact inventory gate (dry-run only)."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--apply", action="store_true", help="Unsupported. This gate is read-only in this round.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    args = _parse_args()
    if args.apply:
        raise SystemExit("apply is not supported; this gate is dry-run only until explicitly approved")

    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
