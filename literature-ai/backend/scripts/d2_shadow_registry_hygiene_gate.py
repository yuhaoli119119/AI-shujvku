from __future__ import annotations

import argparse
import json
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.library_manager import LibraryManager
from app.utils.active_database import WINDOWS_MIRROR_COLON, WINDOWS_MIRROR_SEP, activate_active_library_database, get_active_database_info
from app.utils.project_paths import canonical_registry_path, shadow_registry_paths


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _active_entry(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    active_library = payload.get("active_library")
    for entry in payload.get("libraries", []):
        if entry.get("name") == active_library:
            return entry
    return None


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_summary(db_path: Path | None) -> dict[str, Any]:
    if db_path is None:
        return {"path": None, "exists": False, "papers_total": 0, "has_papers_table": False}

    resolved = db_path.resolve()
    summary = {
        "path": str(resolved),
        "exists": resolved.exists(),
        "papers_total": 0,
        "has_papers_table": False,
    }
    if not resolved.exists():
        return summary

    connection = sqlite3.connect(str(resolved))
    try:
        cursor = connection.cursor()
        tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        summary["has_papers_table"] = "papers" in tables
        if "papers" in tables:
            summary["papers_total"] = int(cursor.execute("SELECT COUNT(*) FROM papers").fetchone()[0] or 0)
    finally:
        connection.close()
    return summary


def _is_mirror_path(path: str | None) -> bool:
    if not path:
        return False
    return WINDOWS_MIRROR_COLON in path or WINDOWS_MIRROR_SEP in path


def _shadow_registry_detail(path: Path, active_database_path: Path | None) -> dict[str, Any]:
    payload = _load_json(path)
    entry = _active_entry(payload)
    active_root_path = str(Path(entry["root_path"]).resolve()) if entry and entry.get("root_path") else None
    registry_db_path = str((Path(entry["root_path"]).resolve() / "database.sqlite")) if entry and entry.get("root_path") else None

    points_to_active_db = bool(
        active_database_path is not None
        and registry_db_path is not None
        and Path(registry_db_path).resolve() == active_database_path.resolve()
    )

    reasons: list[str] = []
    if not path.exists():
        reasons.append("missing")
    elif payload is None:
        reasons.append("invalid_json")
    elif entry is None:
        reasons.append("missing_active_entry")
    else:
        if registry_db_path is None:
            reasons.append("missing_registered_database_path")
        elif not Path(registry_db_path).exists():
            reasons.append("registered_database_missing")
        if not points_to_active_db:
            reasons.append("points_to_different_database")
        if _is_mirror_path(active_root_path):
            reasons.append("contains_windows_mirror_root")

    is_stale_or_dangerous = bool(reasons and reasons != ["missing"])
    shadow_report_path = path.with_name("library_registry.shadow-report.json")
    return {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "sha256": _sha256(path),
        "active_library": payload.get("active_library") if payload else None,
        "active_root_path": active_root_path,
        "active_database_path": registry_db_path,
        "points_to_active_db": points_to_active_db,
        "is_stale_or_dangerous": is_stale_or_dangerous,
        "danger_reasons": reasons,
        "shadow_report_path": str(shadow_report_path.resolve()),
        "shadow_report_exists": shadow_report_path.exists(),
    }


def _proposed_actions(shadows: list[dict[str, Any]], active_database_path: Path | None) -> list[str]:
    actions = [
        "keep_canonical_registry_as_only_runtime_source_of_truth",
        "do_not_move_active_sqlite_or_artifact_files_in_d2_6",
        "do_not_delete_existing_shadow_registries_in_d2_6",
    ]
    if active_database_path is None:
        actions.append("investigate_missing_active_database_before_any_apply")
    if any(not item["points_to_active_db"] for item in shadows):
        actions.append("mark_shadow_registries_with_diagnostic_reports_only_if_apply_is_requested")
    if any(_is_mirror_path(item.get("active_root_path")) for item in shadows):
        actions.append("treat_windows_mirror_registry_targets_as_historical_residue")
    return actions


def _risk_level(*, active_database_exists: bool, shadows: list[dict[str, Any]], active_root_path: str | None) -> str:
    if not active_database_exists:
        return "high"
    if _is_mirror_path(active_root_path):
        return "high"
    if any(item["is_stale_or_dangerous"] for item in shadows):
        return "medium"
    return "low"


def build_report() -> dict[str, Any]:
    activation_info = activate_active_library_database()
    active_info = get_active_database_info()

    canonical_path = canonical_registry_path()
    canonical_payload = _load_json(canonical_path)
    canonical_entry = _active_entry(canonical_payload)
    active_library_root_path = str(Path(canonical_entry["root_path"]).resolve()) if canonical_entry and canonical_entry.get("root_path") else None
    active_database_path = Path(active_info["active_library_db_path"]).resolve() if active_info.get("active_library_db_path") else None
    active_database = _sqlite_summary(active_database_path)

    shadow_details = [
        _shadow_registry_detail(path, active_database_path)
        for path in shadow_registry_paths()
        if path.exists()
    ]

    return {
        "source_of_truth": "canonical_registry",
        "source_of_truth_registry_path": str(canonical_path),
        "canonical_registry_path": str(canonical_path),
        "canonical_registry_sha256": _sha256(canonical_path),
        "activation_info": activation_info,
        "active_library": active_info.get("active_library"),
        "active_library_root_path": active_library_root_path,
        "active_database_path": active_database["path"],
        "active_database_papers_total": active_database["papers_total"],
        "discovered_shadow_registry_paths": [item["path"] for item in shadow_details],
        "whether_each_shadow_registry_points_to_active_db": {
            item["path"]: item["points_to_active_db"] for item in shadow_details
        },
        "whether_each_shadow_registry_is_stale_or_dangerous": {
            item["path"]: item["is_stale_or_dangerous"] for item in shadow_details
        },
        "whether_each_shadow_report_exists": {
            item["path"]: item["shadow_report_exists"] for item in shadow_details
        },
        "shadow_registry_details": shadow_details,
        "proposed_actions": _proposed_actions(shadow_details, active_database_path),
        "risk_level": _risk_level(
            active_database_exists=bool(active_database["exists"]),
            shadows=shadow_details,
            active_root_path=active_library_root_path,
        ),
    }


def apply_hygiene() -> dict[str, Any]:
    pre_apply_report = build_report()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = canonical_registry_path().resolve().parents[2] / "backups" / f"d2_shadow_registry_lockdown_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backups: dict[str, str] = {}
    canonical_path = canonical_registry_path()
    if canonical_path.exists():
        target = backup_dir / "canonical.library_registry.json.bak"
        shutil.copy2(canonical_path, target)
        backups[str(canonical_path.resolve())] = str(target.resolve())

    shadow_labels = {
        str(path.resolve()): label for path, label in zip(shadow_registry_paths(), ["repo-root", "backend"], strict=False)
    }
    report_files: list[str] = []
    for shadow_path_str in pre_apply_report["discovered_shadow_registry_paths"]:
        shadow_path = Path(shadow_path_str)
        if shadow_path.exists():
            label = shadow_labels.get(str(shadow_path.resolve()), shadow_path.parent.name)
            target = backup_dir / f"{label}.library_registry.json.bak"
            shutil.copy2(shadow_path, target)
            backups[str(shadow_path.resolve())] = str(target.resolve())

        report_path = shadow_path.with_name("library_registry.shadow-report.json")
        shadow_detail = next(
            item for item in pre_apply_report["shadow_registry_details"] if item["path"] == str(shadow_path.resolve())
        )
        report_payload = {
            "status": "shadow_registry_deprecated",
            "canonical_registry_path": pre_apply_report["canonical_registry_path"],
            "active_database_path": pre_apply_report["active_database_path"],
            "shadow_registry_path": str(shadow_path.resolve()),
            "stale_or_dangerous": shadow_detail["is_stale_or_dangerous"],
            "do_not_use_as_source_of_truth": True,
            "source_of_truth": "canonical_registry",
            "generated_by": "d2_shadow_registry_hygiene_gate",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "danger_reasons": shadow_detail["danger_reasons"],
        }
        report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        report_files.append(str(report_path.resolve()))

    post_apply_report = build_report()
    return {
        "apply_executed": True,
        "lockdown_strategy": "diagnostic_report_only",
        "selected_option": "A",
        "mode": "diagnostic_reports_only",
        "backup_dir": str(backup_dir.resolve()),
        "backups": backups,
        "generated_shadow_reports": report_files,
        "pre_apply_report": pre_apply_report,
        "post_apply_report": post_apply_report,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D2-6 shadow registry / runtime data hygiene gate.")
    parser.add_argument("--apply", action="store_true", help="Backup registry files and write diagnostic shadow reports.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    args = _parse_args()
    output: dict[str, Any] = {"mode": "dry_run", "dry_run": build_report()}
    if args.apply:
        output["mode"] = "apply"
        output["apply"] = apply_hygiene()

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
