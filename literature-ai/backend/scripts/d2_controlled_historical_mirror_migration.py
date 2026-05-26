from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.utils.active_database import get_active_database_info
from app.utils.project_paths import canonical_registry_path, default_library_root
from scripts import d2_historical_mirror_migration_readiness as readiness
from scripts import d2_target_conflict_and_artifact_inventory_gate as gate


EXPECTED_ACTIVE_DATABASE_PAPERS_TOTAL = 15
EXPECTED_DB_REFERENCED_ARTIFACT_COUNTS = {
    "pdf": 6,
    "markdown": 6,
    "tei": 6,
    "docling_json": 6,
}
EXPECTED_DB_REFERENCED_ARTIFACT_TOTAL = sum(EXPECTED_DB_REFERENCED_ARTIFACT_COUNTS.values())
REQUIRED_LIBRARY_METADATA_RELATIVE_PATHS = ("library.json", "config/project_config.json")
BACKUP_DIRNAME = "d2_controlled_historical_mirror_migration"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object at {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _registry_entry(payload: dict[str, Any], active_library: str | None) -> dict[str, Any] | None:
    if not active_library:
        return None
    for entry in payload.get("libraries", []):
        if entry.get("name") == active_library:
            return entry
    return None


def _sha256(path: Path) -> str:
    digest = readiness._sha256(path.resolve())
    if digest is None:
        raise RuntimeError(f"Expected file for SHA256: {path}")
    return digest


def _relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _target_backup_root(timestamp: str) -> Path:
    return (WORKSPACE_ROOT / "backups" / BACKUP_DIRNAME / timestamp).resolve()


def _planned_registry_backup_path(timestamp: str) -> Path:
    return _target_backup_root(timestamp) / "library_registry.json.bak"


def _resolved_path_or_none(path_str: str | None) -> Path | None:
    if path_str is None or not str(path_str).strip():
        return None
    return Path(str(path_str)).resolve()


def _required_metadata_files(source_root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for relative_path in REQUIRED_LIBRARY_METADATA_RELATIVE_PATHS:
        candidate = (source_root / relative_path).resolve()
        if not candidate.exists() or not candidate.is_file():
            continue
        files.append(
            {
                "relative_path": relative_path,
                "absolute_path": str(candidate),
                "bytes": int(candidate.stat().st_size),
                "sha256": _sha256(candidate),
                "category": "required_library_metadata",
            }
        )
    return files


def _db_referenced_type_counts(db_referenced_files: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in EXPECTED_DB_REFERENCED_ARTIFACT_COUNTS}
    for item in db_referenced_files:
        item_type = str(item.get("type") or "")
        if item_type in counts:
            counts[item_type] += 1
    return counts


def _source_registry_state(canonical_registry: Path) -> tuple[dict[str, Any], str | None, Path | None]:
    payload = _load_json(canonical_registry)
    active_library = payload.get("active_library")
    entry = _registry_entry(payload, active_library if isinstance(active_library, str) else None)
    root_path = Path(str(entry["root_path"])).resolve() if entry and entry.get("root_path") else None
    return payload, active_library if isinstance(active_library, str) else None, root_path


def _build_copy_plan(
    *,
    source_root: Path,
    target_root: Path,
    metadata_files: list[dict[str, Any]],
    db_referenced_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    seen_relative_paths: set[str] = set()

    def add_entry(*, category: str, relative_path: str, source_path: Path) -> None:
        normalized_relative = Path(relative_path).as_posix()
        if normalized_relative in seen_relative_paths:
            raise RuntimeError(f"Duplicate copy-plan relative path: {normalized_relative}")
        resolved_source = source_path.resolve()
        seen_relative_paths.add(normalized_relative)
        operations.append(
            {
                "category": category,
                "relative_path": normalized_relative,
                "source_path": str(resolved_source),
                "target_path": str((target_root / normalized_relative).resolve()),
                "bytes": int(resolved_source.stat().st_size),
                "source_sha256": _sha256(resolved_source),
            }
        )

    add_entry(
        category="active_database",
        relative_path="database.sqlite",
        source_path=(source_root / "database.sqlite"),
    )

    for item in metadata_files:
        add_entry(
            category="required_library_metadata",
            relative_path=str(item["relative_path"]),
            source_path=Path(str(item["absolute_path"])),
        )

    for item in sorted(
        db_referenced_files,
        key=lambda candidate: (str(candidate.get("type") or ""), str(candidate.get("relative_path") or "")),
    ):
        add_entry(
            category=f"db_referenced_artifact:{item['type']}",
            relative_path=str(item["relative_path"]),
            source_path=Path(str(item["absolute_path"])),
        )

    return operations


def _copy_plan_summary(
    *,
    metadata_files: list[dict[str, Any]],
    db_referenced_files: list[dict[str, Any]],
    copy_plan: list[dict[str, Any]],
    unreferenced_files_count: int,
) -> dict[str, Any]:
    artifact_type_counts = _db_referenced_type_counts(db_referenced_files)
    return {
        "copy_plan_mode": "db_referenced_only_plus_required_library_metadata",
        "active_database_copy_count": 1,
        "required_library_metadata_count": len(metadata_files),
        "db_referenced_artifacts_count": len(db_referenced_files),
        "db_referenced_artifacts_by_type": artifact_type_counts,
        "copy_operations_count": len(copy_plan),
        "copied_files_count_expected": len(copy_plan),
        "skipped_unreferenced_files_count": unreferenced_files_count,
        "includes_unreferenced_files": False,
        "copy_categories": [item["category"] for item in copy_plan],
    }


def _validate_preconditions(
    *,
    canonical_registry: Path,
    readiness_report: dict[str, Any],
    gate_report: dict[str, Any],
    active_db_info: dict[str, Any],
    source_root: Path,
    target_root: Path,
    copy_plan: list[dict[str, Any]],
) -> list[str]:
    failures: list[str] = []
    registry_payload, active_library, registry_root = _source_registry_state(canonical_registry)
    referenced_counts = _db_referenced_type_counts(readiness_report["db_referenced_files"])
    source_db = Path(readiness_report["current_active_database_path"]).resolve()

    if gate_report["target_conflicts_count"] != 0:
        failures.append(f"target_conflicts_count={gate_report['target_conflicts_count']} (expected 0)")
    if active_db_info.get("db_kind") != "sqlite":
        failures.append(f"active db kind is {active_db_info.get('db_kind')} (expected sqlite)")
    if readiness_report["active_db_papers_total"] != EXPECTED_ACTIVE_DATABASE_PAPERS_TOTAL:
        failures.append(
            f"active_database_papers_total={readiness_report['active_db_papers_total']} "
            f"(expected {EXPECTED_ACTIVE_DATABASE_PAPERS_TOTAL})"
        )
    if bool(active_db_info.get("recovered_from_candidate_scan")):
        failures.append("recovered_from_candidate_scan=true (expected false)")
    if _resolved_path_or_none(active_db_info.get("active_library_db_path")) != source_db:
        failures.append("active_library_db_path is not the current historical mirror database.sqlite")
    if _resolved_path_or_none(active_db_info.get("effective_db_path")) != source_db:
        failures.append("effective_db_path is not the current historical mirror database.sqlite")
    if not readiness_report["source_root_is_historical_mirror"]:
        failures.append("source root is not the current historical mirror root")
    if target_root != default_library_root().resolve():
        failures.append(f"target root mismatch: {target_root} != {default_library_root().resolve()}")
    if registry_root != source_root:
        failures.append("canonical registry no longer points at the current source root")
    if registry_payload.get("active_library") != readiness_report["active_library"]:
        failures.append("canonical registry active_library changed unexpectedly")
    if readiness_report["missing_referenced_files_count"] != 0:
        failures.append(
            f"missing referenced artifacts={readiness_report['missing_referenced_files_count']} (expected 0)"
        )
    if readiness_report["duplicate_artifact_paths_count"] != 0:
        failures.append(
            f"duplicate_artifact_paths={readiness_report['duplicate_artifact_paths_count']} (expected 0)"
        )
    if len(readiness_report["db_referenced_files"]) != EXPECTED_DB_REFERENCED_ARTIFACT_TOTAL:
        failures.append(
            f"db_referenced_artifacts_total={len(readiness_report['db_referenced_files'])} "
            f"(expected {EXPECTED_DB_REFERENCED_ARTIFACT_TOTAL})"
        )
    for artifact_type, expected_count in EXPECTED_DB_REFERENCED_ARTIFACT_COUNTS.items():
        actual_count = referenced_counts.get(artifact_type, 0)
        if actual_count != expected_count:
            failures.append(f"{artifact_type} referenced count={actual_count} (expected {expected_count})")
    expected_copy_total = 1 + len(readiness_report["required_library_metadata_files"]) + EXPECTED_DB_REFERENCED_ARTIFACT_TOTAL
    if len(copy_plan) != expected_copy_total:
        failures.append(f"copy plan file count={len(copy_plan)} (expected {expected_copy_total})")
    return failures


def _render_markdown_report(report: dict[str, Any]) -> str:
    summary = report["copy_plan_summary"]
    validation = report["apply_preconditions"]
    lines = [
        "# D2-12 Controlled Historical Mirror Migration Apply Plan",
        "",
        "## Scope",
        "",
        f"- source root: `{report['source_root']}`",
        f"- target root: `{report['target_root']}`",
        f"- apply executed: `{str(report['apply_executed']).lower()}`",
        f"- migration mode: `{summary['copy_plan_mode']}`",
        f"- copied files count planned: `{summary['copied_files_count_expected']}`",
        f"- DB-referenced artifacts count: `{summary['db_referenced_artifacts_count']}`",
        f"- skipped unreferenced files count: `{summary['skipped_unreferenced_files_count']}`",
        f"- registry backup path: `{report['registry_backup_path']}`",
        "",
        "## Preconditions",
        "",
        f"- ready for apply: `{str(validation['ready_for_apply']).lower()}`",
        f"- target conflicts count: `{report['target_conflicts_count']}`",
        f"- missing referenced files count: `{report['missing_referenced_files_count']}`",
        f"- duplicate artifact paths count: `{report['duplicate_artifact_paths_count']}`",
        "",
        "## Rollback",
        "",
    ]
    for item in report["rollback_instructions"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _planned_target_manifest(copy_plan: list[dict[str, Any]]) -> dict[str, str]:
    return {item["relative_path"]: item["target_path"] for item in copy_plan}


def _source_sha_manifest(copy_plan: list[dict[str, Any]]) -> dict[str, str]:
    return {item["relative_path"]: item["source_sha256"] for item in copy_plan}


def _target_sha_manifest(copy_plan: list[dict[str, Any]]) -> dict[str, str]:
    return {item["relative_path"]: _sha256(Path(str(item["target_path"]))) for item in copy_plan}


def _copy_files(copy_plan: list[dict[str, Any]]) -> list[str]:
    copied_relative_paths: list[str] = []
    for item in copy_plan:
        source = Path(str(item["source_path"])).resolve()
        target = Path(str(item["target_path"])).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_relative_paths.append(str(item["relative_path"]))
    return copied_relative_paths


def _verify_hash_match(copy_plan: list[dict[str, Any]]) -> tuple[bool, dict[str, dict[str, str]]]:
    source_hashes = _source_sha_manifest(copy_plan)
    target_hashes = _target_sha_manifest(copy_plan)
    comparison = {
        relative_path: {
            "source_sha256": source_hashes[relative_path],
            "target_sha256": target_hashes[relative_path],
        }
        for relative_path in source_hashes
    }
    return source_hashes == target_hashes, comparison


def _update_canonical_registry_root(
    *,
    canonical_registry: Path,
    active_library: str,
    target_root: Path,
) -> None:
    payload = _load_json(canonical_registry)
    entry = _registry_entry(payload, active_library)
    if entry is None:
        raise RuntimeError(f"Active library missing in canonical registry: {active_library}")
    entry["root_path"] = str(target_root.resolve())
    _write_json(canonical_registry, payload)


def restore_registry_backup(*, registry_backup_path: Path, canonical_registry: Path) -> None:
    canonical_registry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(registry_backup_path.resolve(), canonical_registry.resolve())


def _verify_runtime_binding_after_registry_update(active_library: str) -> dict[str, Any]:
    from app.services.library_manager import LibraryManager

    manager = LibraryManager()
    manager.activate_library(active_library)
    return get_active_database_info()


def _post_registry_validation(
    *,
    active_library: str,
    target_root: Path,
) -> tuple[bool, dict[str, Any], list[str]]:
    info = _verify_runtime_binding_after_registry_update(active_library)
    failures: list[str] = []
    target_db = (target_root / "database.sqlite").resolve()
    if _resolved_path_or_none(info.get("active_library_db_path")) != target_db:
        failures.append("active_library_db_path did not switch to target database.sqlite")
    if _resolved_path_or_none(info.get("effective_db_path")) != target_db:
        failures.append("effective_db_path did not switch to target database.sqlite")
    if int(info.get("effective_db_papers_total") or 0) != EXPECTED_ACTIVE_DATABASE_PAPERS_TOTAL:
        failures.append(
            f"effective_db_papers_total={info.get('effective_db_papers_total')} "
            f"(expected {EXPECTED_ACTIVE_DATABASE_PAPERS_TOTAL})"
        )
    if bool(info.get("recovered_from_candidate_scan")):
        failures.append("recovered_from_candidate_scan=true after registry update")
    return not failures, info, failures


def _write_apply_audit_reports(*, report: dict[str, Any], output_root: Path) -> dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = (output_root / "migration_audit_report.json").resolve()
    markdown_path = (output_root / "migration_audit_report.md").resolve()
    _write_json(json_path, report)
    markdown_path.write_text(_render_markdown_report(report), encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def build_report(*, apply: bool = False) -> dict[str, Any]:
    timestamp = _utc_now()
    canonical_registry = canonical_registry_path().resolve()
    readiness_report = readiness.build_report()
    gate_report = gate.build_report()
    active_db_info = get_active_database_info()

    source_root = Path(readiness_report["current_active_library_root"]).resolve()
    target_root = Path(readiness_report["proposed_canonical_library_root"]).resolve()
    active_library = str(readiness_report["active_library"])
    metadata_files = _required_metadata_files(source_root)
    copy_plan = _build_copy_plan(
        source_root=source_root,
        target_root=target_root,
        metadata_files=metadata_files,
        db_referenced_files=readiness_report["db_referenced_files"],
    )
    precondition_failures = _validate_preconditions(
        canonical_registry=canonical_registry,
        readiness_report=readiness_report,
        gate_report=gate_report,
        active_db_info=active_db_info,
        source_root=source_root,
        target_root=target_root,
        copy_plan=copy_plan,
    )
    registry_backup_path = _planned_registry_backup_path(timestamp)

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "apply_requested": apply,
        "apply_executed": False,
        "apply_supported": True,
        "source_root": str(source_root),
        "target_root": str(target_root),
        "canonical_registry_path": str(canonical_registry),
        "canonical_registry_current_root": readiness_report["current_active_library_root"],
        "active_library": active_library,
        "active_database_path": readiness_report["current_active_database_path"],
        "active_database_kind": active_db_info.get("db_kind"),
        "active_database_papers_total": readiness_report["active_db_papers_total"],
        "recovered_from_candidate_scan": bool(active_db_info.get("recovered_from_candidate_scan")),
        "target_conflicts_count": gate_report["target_conflicts_count"],
        "missing_referenced_files_count": readiness_report["missing_referenced_files_count"],
        "duplicate_artifact_paths_count": readiness_report["duplicate_artifact_paths_count"],
        "registry_backup_path": str(registry_backup_path),
        "copy_plan_summary": _copy_plan_summary(
            metadata_files=metadata_files,
            db_referenced_files=readiness_report["db_referenced_files"],
            copy_plan=copy_plan,
            unreferenced_files_count=readiness_report["unreferenced_files_count"],
        ),
        "copy_plan": copy_plan,
        "required_library_metadata_files": metadata_files,
        "db_referenced_artifacts_count": len(readiness_report["db_referenced_files"]),
        "db_referenced_artifacts_by_type": _db_referenced_type_counts(readiness_report["db_referenced_files"]),
        "skipped_unreferenced_files_count": readiness_report["unreferenced_files_count"],
        "planned_target_manifest": _planned_target_manifest(copy_plan),
        "source_sha256_before": {
            "active_database": _sha256(Path(readiness_report["current_active_database_path"])),
            "copied_files": _source_sha_manifest(copy_plan),
        },
        "target_sha256_after": {},
        "apply_preconditions": {
            "ready_for_apply": not precondition_failures,
            "failures": precondition_failures,
        },
        "registry_update_rule": {
            "only_after_copy_and_hash_verification": True,
            "registry_update_attempted": False,
            "registry_updated": False,
            "registry_restored_from_backup": False,
        },
        "rollback_instructions": [
            f"Restore canonical registry from backup: {registry_backup_path}",
            "If target files were partially written, remove only the files listed in the copy plan manifest under the target root.",
            "If registry was already updated but post-update validation failed, restore the registry backup before reopening runtime traffic.",
            "Re-run the controlled migration dry-run and verify target_conflicts_count=0 before any future apply.",
        ],
        "audit_report_paths": {},
        "post_registry_validation": {},
        "error": None,
    }

    if not apply:
        report["markdown_audit_report"] = _render_markdown_report(report)
        return report

    if precondition_failures:
        report["error"] = "apply_blocked_by_preconditions"
        report["markdown_audit_report"] = _render_markdown_report(report)
        return report

    backup_root = _target_backup_root(timestamp)
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(canonical_registry, registry_backup_path)

    copied_relative_paths: list[str] = []
    try:
        copied_relative_paths = _copy_files(copy_plan)
        hashes_match, target_hashes = _verify_hash_match(copy_plan)
        report["target_sha256_after"] = target_hashes
        if not hashes_match:
            report["error"] = "copied_file_hash_mismatch"
            report["markdown_audit_report"] = _render_markdown_report(report)
            report["audit_report_paths"] = _write_apply_audit_reports(report=report, output_root=backup_root)
            return report

        report["registry_update_rule"]["registry_update_attempted"] = True
        _update_canonical_registry_root(
            canonical_registry=canonical_registry,
            active_library=active_library,
            target_root=target_root,
        )
        report["registry_update_rule"]["registry_updated"] = True

        post_ok, post_info, post_failures = _post_registry_validation(
            active_library=active_library,
            target_root=target_root,
        )
        report["post_registry_validation"] = {
            "ok": post_ok,
            "details": post_info,
            "failures": post_failures,
        }
        if not post_ok:
            restore_registry_backup(
                registry_backup_path=registry_backup_path,
                canonical_registry=canonical_registry,
            )
            report["registry_update_rule"]["registry_restored_from_backup"] = True
            report["error"] = "post_registry_validation_failed"
            report["markdown_audit_report"] = _render_markdown_report(report)
            report["audit_report_paths"] = _write_apply_audit_reports(report=report, output_root=backup_root)
            return report

        report["apply_executed"] = True
        report["copied_files_count"] = len(copied_relative_paths)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["markdown_audit_report"] = _render_markdown_report(report)
        report["audit_report_paths"] = _write_apply_audit_reports(report=report, output_root=backup_root)

    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Controlled historical mirror migration apply plan with strict referenced-only scope."
    )
    parser.add_argument("--apply", action="store_true", help="Execute the controlled migration after strict checks.")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode (default).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--markdown", action="store_true", help="Emit the markdown audit report instead of JSON.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    args = _parse_args()
    apply = bool(args.apply)
    if args.dry_run:
        apply = False

    report = build_report(apply=apply)
    if args.markdown:
        print(report["markdown_audit_report"])
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=args.json))

    if apply and not report["apply_executed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
