from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.services.library_manager import DEFAULT_LIBRARY_NAME, LibraryManager
from app.utils.active_database import WINDOWS_MIRROR_COLON, WINDOWS_MIRROR_SEP, activate_active_library_database, get_active_database_info
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference


ARTIFACT_FIELDS = {
    "pdf_path": "pdf",
    "tei_path": "tei",
    "docling_json_path": "docling_json",
    "markdown_path": "markdown",
}


@dataclass
class ArtifactChange:
    paper_id: str
    title: str
    field: str
    before: str
    after: str


def _canonical_registry_path() -> Path:
    return Path(LibraryManager.REGISTRY_PATH).resolve()


def _registry_candidates() -> dict[str, Path]:
    candidates = {
        "canonical": _canonical_registry_path(),
        "workspace_shadow": (PROJECT_ROOT.parent / "data" / "library_registry.json").resolve(),
        "backend_shadow": (BACKEND_ROOT / "data" / "library_registry.json").resolve(),
    }
    unique: dict[str, Path] = {}
    seen: set[Path] = set()
    for label, path in candidates.items():
        if path in seen:
            continue
        seen.add(path)
        unique[label] = path
    return unique


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _registry_snapshot(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    active_root = None
    active_library = None
    if payload:
        active_library = payload.get("active_library")
        for entry in payload.get("libraries", []):
            if entry.get("name") == active_library:
                active_root = entry.get("root_path")
                break
    return {
        "path": str(path),
        "exists": path.exists(),
        "active_library": active_library,
        "active_root_path": active_root,
        "library_count": len(payload.get("libraries", [])) if payload else 0,
    }


def _sqlite_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": None, "exists": False, "papers_total": 0, "has_papers_table": False}
    resolved = path.resolve()
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


def _build_runtime_settings(storage_root: Path):
    return get_settings().model_copy(update={"storage_root": storage_root})


def _artifact_changes(db_path: Path, *, library_root: Path) -> tuple[list[ArtifactChange], dict[str, int], int]:
    settings = _build_runtime_settings(library_root / "storage")
    connection = sqlite3.connect(str(db_path))
    try:
        cursor = connection.cursor()
        rows = cursor.execute(
            "SELECT id, title, pdf_path, tei_path, docling_json_path, markdown_path FROM papers ORDER BY title ASC"
        ).fetchall()
    finally:
        connection.close()

    changes: list[ArtifactChange] = []
    unresolved = 0
    counts = {field: 0 for field in ARTIFACT_FIELDS}
    for row in rows:
        paper_id = str(row[0])
        title = str(row[1] or "")
        values = dict(zip(ARTIFACT_FIELDS.keys(), row[2:]))
        for field, category in ARTIFACT_FIELDS.items():
            before = values.get(field)
            if before is None or not str(before).strip():
                continue
            after = canonicalize_persisted_artifact_reference(before, category=category, settings=settings)
            if after is None:
                unresolved += 1
                continue
            if after != before:
                counts[field] += 1
                changes.append(
                    ArtifactChange(
                        paper_id=paper_id,
                        title=title,
                        field=field,
                        before=str(before),
                        after=after,
                    )
                )
    return changes, counts, unresolved


def _mismatch_reasons(
    *,
    info: dict[str, Any],
    canonical_registry: dict[str, Any],
    shadow_registries: list[dict[str, Any]],
    artifact_changes: list[ArtifactChange],
) -> list[str]:
    reasons: list[str] = []
    if shadow_registries:
        reasons.append("multiple_registry_files_from_cwd_relative_library_manager")
    if info.get("recovered_from_candidate_scan"):
        reasons.append("runtime_depends_on_candidate_scan_to_find_populated_sqlite")
    if canonical_registry.get("active_root_path") and info.get("effective_db_path"):
        expected = str(Path(canonical_registry["active_root_path"]) / "database.sqlite")
        if Path(expected).resolve() != Path(info["effective_db_path"]).resolve():
            reasons.append("canonical_registry_root_path_points_to_different_sqlite")
    if info.get("effective_db_path") and (
        WINDOWS_MIRROR_COLON in str(info["effective_db_path"]) or WINDOWS_MIRROR_SEP in str(info["effective_db_path"])
    ):
        reasons.append("historical_windows_mirror_library_root_residue")
    if artifact_changes:
        reasons.append("historical_container_app_absolute_artifact_paths")
    if not reasons:
        reasons.append("no_mismatch_detected")
    return reasons


def _risk_level(
    *,
    effective_papers_total: int,
    unresolved_artifacts: int,
    shadow_registry_count: int,
    proposed_root: Path | None,
) -> str:
    if proposed_root is None or effective_papers_total <= 0:
        return "high"
    if unresolved_artifacts > 0:
        return "high"
    if shadow_registry_count > 0 or WINDOWS_MIRROR_COLON in str(proposed_root) or WINDOWS_MIRROR_SEP in str(proposed_root):
        return "medium"
    return "low"


def build_report() -> dict[str, Any]:
    get_settings.cache_clear()
    info = get_active_database_info()
    registry_candidates = _registry_candidates()
    registry_snapshots = {label: _registry_snapshot(path) for label, path in registry_candidates.items()}
    shadow_registries = [
        snapshot
        for label, snapshot in registry_snapshots.items()
        if label != "canonical" and snapshot["exists"]
    ]

    canonical_registry = registry_snapshots["canonical"]
    effective_db_path = Path(info["effective_db_path"]).resolve() if info.get("effective_db_path") else None
    effective_db = _sqlite_summary(effective_db_path)
    proposed_root = effective_db_path.parent if effective_db_path else None
    changes, change_counts, unresolved = (
        _artifact_changes(effective_db_path, library_root=proposed_root)
        if effective_db_path is not None
        else ([], {field: 0 for field in ARTIFACT_FIELDS}, 0)
    )
    reasons = _mismatch_reasons(
        info=info,
        canonical_registry=canonical_registry,
        shadow_registries=shadow_registries,
        artifact_changes=changes,
    )
    risk = _risk_level(
        effective_papers_total=effective_db["papers_total"],
        unresolved_artifacts=unresolved,
        shadow_registry_count=len(shadow_registries),
        proposed_root=proposed_root,
    )

    return {
        "current_registry_path": canonical_registry["path"],
        "current_effective_db_path": info.get("effective_db_path"),
        "current_active_library_db_path": info.get("active_library_db_path"),
        "effective_db_has_15_papers_sqlite": bool(effective_db["papers_total"] == 15),
        "path_mismatch_reasons": reasons,
        "proposed_registry_root_path": str(proposed_root) if proposed_root else None,
        "proposed_database_path": str(effective_db_path) if effective_db_path else None,
        "proposed_artifact_path_normalization_count": len(changes),
        "proposed_artifact_path_normalization_breakdown": change_counts,
        "unresolved_artifact_path_count": unresolved,
        "risk_level": risk,
        "runtime_database": info,
        "effective_sqlite_summary": effective_db,
        "registry_candidates": registry_snapshots,
        "shadow_registries_detected": shadow_registries,
        "artifact_path_changes_preview": [
            {
                "paper_id": change.paper_id,
                "title": change.title,
                "field": change.field,
                "before": change.before,
                "after": change.after,
            }
            for change in changes[:12]
        ],
    }


def _write_registry(path: Path, *, root_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _load_json(path) or {"version": 2, "active_library": DEFAULT_LIBRARY_NAME, "libraries": []}
    after = json.loads(json.dumps(before, ensure_ascii=False))
    libraries = list(after.get("libraries", []))
    default_entry = None
    for entry in libraries:
        if entry.get("name") == DEFAULT_LIBRARY_NAME:
            default_entry = entry
            break
    if default_entry is None:
        default_entry = {
            "name": DEFAULT_LIBRARY_NAME,
            "description": DEFAULT_LIBRARY_NAME,
            "created_at": datetime.utcnow().isoformat(),
        }
        libraries.append(default_entry)
    default_entry["root_path"] = str(root_path.resolve())
    default_entry.setdefault("description", DEFAULT_LIBRARY_NAME)
    after["version"] = 2
    after["active_library"] = DEFAULT_LIBRARY_NAME
    after["libraries"] = libraries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    return before, after


def _apply_artifact_changes(db_path: Path, changes: list[ArtifactChange]) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        for change in changes:
            connection.execute(
                f"UPDATE papers SET {change.field} = ? WHERE id = ?",
                (change.after, change.paper_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _backup_files(registry_path: Path, db_path: Path) -> dict[str, Any]:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_dir = PROJECT_ROOT / "backups" / f"d2_active_library_path_repair_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    registry_backup = backup_dir / "library_registry.json.bak"
    db_backup = backup_dir / "database.sqlite.bak"
    shutil.copy2(registry_path, registry_backup)
    shutil.copy2(db_path, db_backup)
    return {
        "backup_dir": str(backup_dir),
        "registry_backup": str(registry_backup),
        "database_backup": str(db_backup),
        "artifact_metadata_backup_strategy": "sqlite_file_backup_and_transaction_rollback",
    }


def apply_repairs() -> dict[str, Any]:
    report = build_report()
    proposed_root_raw = report.get("proposed_registry_root_path")
    proposed_db_raw = report.get("proposed_database_path")
    if not proposed_root_raw or not proposed_db_raw:
        raise RuntimeError("No effective SQLite candidate available for apply")

    proposed_root = Path(proposed_root_raw).resolve()
    proposed_db = Path(proposed_db_raw).resolve()
    if report["risk_level"] == "high":
        raise RuntimeError("Refusing apply because risk_level=high")

    changes, _, unresolved = _artifact_changes(proposed_db, library_root=proposed_root)
    if unresolved:
        raise RuntimeError(f"Refusing apply because {unresolved} artifact paths are unresolved")

    registry_path = _canonical_registry_path()
    backups = _backup_files(registry_path, proposed_db)
    registry_before, registry_after = _write_registry(registry_path, root_path=proposed_root)
    _apply_artifact_changes(proposed_db, changes)

    activation_info = activate_active_library_database()
    after_report = build_report()
    return {
        "apply_executed": True,
        "backups": backups,
        "registry_before": registry_before,
        "registry_after": registry_after,
        "artifact_paths_updated": len(changes),
        "artifact_path_changes_preview": [
            {
                "paper_id": change.paper_id,
                "title": change.title,
                "field": change.field,
                "before": change.before,
                "after": change.after,
            }
            for change in changes[:12]
        ],
        "activation_info": activation_info,
        "post_apply_report": after_report,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D2-5 active library registry path repair / artifact path canonicalization gate.")
    parser.add_argument("--apply", action="store_true", help="Modify the canonical registry and normalize persisted artifact paths.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    args = _parse_args()
    report = build_report()
    output: dict[str, Any] = {"mode": "dry_run", "dry_run": report}
    if args.apply:
        output["mode"] = "apply"
        output["apply"] = apply_repairs()

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
