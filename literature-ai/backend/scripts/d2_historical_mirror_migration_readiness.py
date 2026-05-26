from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.utils.active_database import WINDOWS_MIRROR_COLON, WINDOWS_MIRROR_SEP
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference, resolve_persisted_artifact_path
from app.utils.project_paths import canonical_registry_path, default_library_root, shadow_registry_paths


ARTIFACT_FIELDS: dict[str, str] = {
    "pdf_path": "pdf",
    "tei_path": "tei",
    "docling_json_path": "docling_json",
    "markdown_path": "markdown",
}
FILESYSTEM_ARTIFACT_DIRS = ("pdf", "markdown", "tei", "docling_json", "figures", "tables", "images")
UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:_|$)",
    re.IGNORECASE,
)
UUID_ONLY_STEM_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _path_mtime_utc(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _file_entry(path: Path, *, root: Path | None = None, item_type: str | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    relative_path = path.name
    if root is not None:
        try:
            relative_path = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative_path = path.as_posix()

    return {
        "type": item_type or path.parent.name,
        "relative_path": relative_path,
        "absolute_path": str(resolved),
        "bytes": int(path.stat().st_size),
        "mtime_utc": _path_mtime_utc(path),
    }


def _registry_entry(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    active_library = payload.get("active_library")
    for entry in payload.get("libraries", []):
        if entry.get("name") == active_library:
            return entry
    return None


def _is_mirror_path(path: str | None) -> bool:
    if not path:
        return False
    return WINDOWS_MIRROR_COLON in path or WINDOWS_MIRROR_SEP in path


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _sqlite_integrity(path: Path) -> str:
    connection = _sqlite_connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0] if row else "unknown")
    finally:
        connection.close()


def _papers_total(path: Path) -> int:
    connection = _sqlite_connect(path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM papers").fetchone()
        return int(row[0] or 0)
    finally:
        connection.close()


def _sqlite_tables(path: Path) -> list[str]:
    if not path.exists():
        return []
    connection = _sqlite_connect(path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name ASC").fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def sqlite_file_summary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    exists = resolved.exists()
    summary: dict[str, Any] = {
        "path": str(resolved),
        "exists": exists,
        "size": int(resolved.stat().st_size) if exists else 0,
        "sha256": _sha256(resolved),
        "mtime_utc": _path_mtime_utc(resolved),
        "sqlite_integrity_check": None,
        "tables": [],
        "table_count": 0,
        "papers_total": 0,
    }
    if not exists:
        return summary

    try:
        tables = _sqlite_tables(resolved)
        summary["tables"] = tables
        summary["table_count"] = len(tables)
        summary["sqlite_integrity_check"] = _sqlite_integrity(resolved)
        if "papers" in tables:
            summary["papers_total"] = _papers_total(resolved)
    except sqlite3.Error as exc:
        summary["sqlite_integrity_check"] = f"sqlite_error:{type(exc).__name__}"
    return summary


def json_file_summary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    payload = _load_json(resolved)
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "size": int(resolved.stat().st_size) if resolved.exists() else 0,
        "sha256": _sha256(resolved),
        "mtime_utc": _path_mtime_utc(resolved),
        "is_valid_json_object": payload is not None,
        "active_library": payload.get("active_library") if payload else None,
        "name": payload.get("name") if payload else None,
        "storage_mode": payload.get("storage_mode") if payload else None,
        "project_name": payload.get("project_name") if payload else None,
    }


def _runtime_settings_for(root: Path):
    from app.config import get_settings

    storage_root = (root / "storage").resolve()
    return get_settings().model_copy(update={"storage_root": storage_root})


def _storage_dir_counts(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], int, int]:
    storage_root = root / "storage"
    summary: dict[str, Any] = {
        "library_root": str(root.resolve()),
        "storage_root": str(storage_root.resolve()),
        "storage_root_exists": storage_root.exists(),
        "by_dir": {},
    }
    files: list[dict[str, Any]] = []
    total_count = 0
    total_bytes = 0

    for dirname in FILESYSTEM_ARTIFACT_DIRS:
        directory = storage_root / dirname
        count = 0
        size = 0
        if directory.exists():
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                item_size = int(path.stat().st_size)
                file_entry = _file_entry(path, root=root, item_type=dirname)
                file_entry["relative_path"] = rel
                files.append(file_entry)
                count += 1
                size += item_size
                total_count += 1
                total_bytes += item_size
        summary["by_dir"][dirname] = {
            "exists": directory.exists(),
            "files": count,
            "bytes": size,
        }

    return summary, files, total_count, total_bytes


def _paper_artifact_audit(db_path: Path, source_root: Path, target_root: Path) -> dict[str, Any]:
    settings = _runtime_settings_for(source_root)
    connection = _sqlite_connect(db_path)
    try:
        rows = connection.execute(
            "SELECT id, title, pdf_path, tei_path, docling_json_path, markdown_path FROM papers ORDER BY title ASC"
        ).fetchall()
    finally:
        connection.close()

    missing_files: list[dict[str, Any]] = []
    duplicate_sources: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    duplicate_targets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    path_conflicts: list[dict[str, Any]] = []
    referenced_source_paths: set[Path] = set()
    referenced_source_files: dict[str, dict[str, Any]] = {}
    already_canonical_count = 0
    needing_update_count = 0
    field_value_total = 0
    by_field = {field: {"canonical": 0, "needs_update": 0, "missing": 0, "empty": 0} for field in ARTIFACT_FIELDS}

    for row in rows:
        paper_id = str(row["id"])
        title = str(row["title"] or "")
        for field, category in ARTIFACT_FIELDS.items():
            stored_path = row[field]
            if stored_path is None or not str(stored_path).strip():
                by_field[field]["empty"] += 1
                continue

            stored_str = str(stored_path).strip()
            field_value_total += 1
            canonical = canonicalize_persisted_artifact_reference(stored_str, category=category, settings=settings)
            if canonical == stored_str:
                already_canonical_count += 1
                by_field[field]["canonical"] += 1
            else:
                needing_update_count += 1
                by_field[field]["needs_update"] += 1

            resolved = resolve_persisted_artifact_path(stored_str, category=category, settings=settings, must_exist=True)
            if resolved is None or not resolved.exists():
                by_field[field]["missing"] += 1
                missing_files.append(
                    {
                        "paper_id": paper_id,
                        "title": title,
                        "field": field,
                        "stored_path": stored_str,
                        "expected_target_path": str((target_root / (canonical or stored_str)).resolve()),
                        "reason": "referenced_artifact_missing",
                    }
                )
                continue

            referenced_source_paths.add(resolved.resolve())
            resolved_str = str(resolved.resolve())
            if resolved_str not in referenced_source_files:
                referenced_source_files[resolved_str] = {
                    **_file_entry(resolved, root=source_root, item_type=category),
                    "referenced_by": [],
                }
            referenced_source_files[resolved_str]["referenced_by"].append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "field": field,
                    "stored_path": stored_str,
                    "canonical_target_relative_path": canonical or stored_str,
                }
            )
            duplicate_sources[str(resolved.resolve())].append(
                {"paper_id": paper_id, "title": title, "field": field, "stored_path": stored_str}
            )

            target_relative = canonical or stored_str
            target_path = (target_root / target_relative).resolve()
            duplicate_targets[str(target_path)].append(
                {"paper_id": paper_id, "title": title, "field": field, "stored_path": stored_str}
            )
            if target_path.exists():
                path_conflicts.append(
                    {
                        "type": "artifact_target_exists",
                        "paper_id": paper_id,
                        "title": title,
                        "field": field,
                        "source_path": str(resolved),
                        "target_path": str(target_path),
                        "target_bytes": int(target_path.stat().st_size),
                    }
                )

    duplicate_artifact_paths: list[dict[str, Any]] = []
    for source_path, items in sorted(duplicate_sources.items()):
        if len(items) > 1:
            duplicate_artifact_paths.append(
                {
                    "kind": "source_path",
                    "path": source_path,
                    "references": items,
                }
            )
    for target_path, items in sorted(duplicate_targets.items()):
        if len(items) > 1:
            duplicate_artifact_paths.append(
                {
                    "kind": "target_path",
                    "path": target_path,
                    "references": items,
                }
            )

    return {
        "artifact_field_values_total": field_value_total,
        "artifact_paths_already_canonical_count": already_canonical_count,
        "artifact_paths_needing_update_count": needing_update_count,
        "artifact_field_summary": by_field,
        "missing_files": missing_files,
        "path_conflicts": path_conflicts,
        "duplicate_artifact_paths": duplicate_artifact_paths,
        "referenced_source_path_count": len(referenced_source_paths),
        "referenced_source_files": sorted(
            referenced_source_files.values(),
            key=lambda item: (str(item["type"]), str(item["relative_path"])),
        ),
    }


def _source_inventory_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["type"] for item in files)
    bytes_by_type: defaultdict[str, int] = defaultdict(int)
    for item in files:
        bytes_by_type[str(item["type"])] += int(item["bytes"])
    return {
        "total_files": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "by_type": {
            artifact_type: {
                "files": int(counts.get(artifact_type, 0)),
                "bytes": int(bytes_by_type.get(artifact_type, 0)),
            }
            for artifact_type in FILESYSTEM_ARTIFACT_DIRS
        },
    }


def _unreferenced_pdf_origin(relative_path: str, size_bytes: int) -> str:
    name = Path(relative_path).name
    stem = Path(relative_path).stem
    lowered = name.lower()
    if size_bytes <= 1024:
        return "tiny_placeholder_or_test_residue"
    if lowered.endswith(".tmp.pdf") or lowered.startswith("tmp") or "temp" in lowered:
        return "temporary_file_pattern"
    if UUID_ONLY_STEM_RE.match(stem):
        return "uuid_only_import_or_test_residue"
    if UUID_PREFIX_RE.match(name):
        return "uuid_prefixed_historical_import_residue"
    return "document_like_unreferenced_pdf"


def classify_source_artifact_inventory(source_root: Path, target_root: Path, db_path: Path) -> dict[str, Any]:
    storage_summary, storage_files, _, _ = _storage_dir_counts(source_root)
    artifact_audit = _paper_artifact_audit(db_path, source_root, target_root)

    referenced_files = artifact_audit["referenced_source_files"]
    referenced_paths = {str(Path(item["absolute_path"]).resolve()) for item in referenced_files}
    unreferenced_files = [
        item for item in storage_files if str(Path(item["absolute_path"]).resolve()) not in referenced_paths
    ]
    unreferenced_pdf_files = [item for item in unreferenced_files if item["type"] == "pdf"]
    unreferenced_non_pdf_files = [item for item in unreferenced_files if item["type"] != "pdf"]

    pdf_origin_counts = Counter(
        _unreferenced_pdf_origin(str(item["relative_path"]), int(item["bytes"])) for item in unreferenced_pdf_files
    )
    mtime_values = [item["mtime_utc"] for item in unreferenced_pdf_files if item.get("mtime_utc")]

    duplicate_or_suspect_files = [
        {
            "kind": "duplicate_artifact_path",
            "path": item["path"],
            "references": item["references"],
        }
        for item in artifact_audit["duplicate_artifact_paths"]
    ]
    duplicate_or_suspect_files.extend(
        {
            "kind": "unreferenced_pdf_suspect",
            "path": item["relative_path"],
            "bytes": item["bytes"],
            "origin_hint": _unreferenced_pdf_origin(str(item["relative_path"]), int(item["bytes"])),
        }
        for item in unreferenced_pdf_files
        if _unreferenced_pdf_origin(str(item["relative_path"]), int(item["bytes"])) != "document_like_unreferenced_pdf"
    )

    return {
        "source_file_inventory_all": {
            **storage_summary,
            **_source_inventory_summary(storage_files),
        },
        "source_file_inventory_db_referenced": _source_inventory_summary(referenced_files),
        "source_file_inventory_unreferenced": _source_inventory_summary(unreferenced_files),
        "db_referenced_files": referenced_files,
        "db_referenced_files_to_copy_count": len(referenced_files),
        "unreferenced_files_count": len(unreferenced_files),
        "unreferenced_pdf_files": unreferenced_pdf_files,
        "unreferenced_pdf_count": len(unreferenced_pdf_files),
        "unreferenced_pdf_total_bytes": sum(int(item["bytes"]) for item in unreferenced_pdf_files),
        "unreferenced_pdf_examples": [str(item["relative_path"]) for item in unreferenced_pdf_files[:20]],
        "unreferenced_pdf_mtime_range": {
            "min": min(mtime_values) if mtime_values else None,
            "max": max(mtime_values) if mtime_values else None,
        },
        "unreferenced_pdf_origin_hints": dict(sorted(pdf_origin_counts.items())),
        "unreferenced_non_pdf_files": unreferenced_non_pdf_files,
        "unreferenced_non_pdf_count": len(unreferenced_non_pdf_files),
        "missing_referenced_files_count": len(artifact_audit["missing_files"]),
        "missing_referenced_files": artifact_audit["missing_files"],
        "duplicate_or_suspect_files": duplicate_or_suspect_files,
        "artifact_audit": artifact_audit,
    }


def _existing_file_conflicts(source_root: Path, target_root: Path) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for source in sorted(source_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        if target.exists():
            conflicts.append(
                {
                    "type": "target_exists",
                    "relative_path": relative.as_posix(),
                    "source_path": str(source.resolve()),
                    "source_bytes": int(source.stat().st_size),
                    "target_path": str(target.resolve()),
                    "target_bytes": int(target.stat().st_size),
                }
            )
    return conflicts


def _registry_update_plan(
    canonical_registry: Path,
    shadow_paths: list[Path],
    *,
    active_library: str | None,
    current_root: Path,
    proposed_root: Path,
    current_db: Path,
    proposed_db: Path,
) -> dict[str, Any]:
    return {
        "dry_run_only": True,
        "apply_in_this_round": False,
        "canonical_registry_path": str(canonical_registry.resolve()),
        "active_library": active_library,
        "current_root_path": str(current_root.resolve()),
        "proposed_root_path": str(proposed_root.resolve()),
        "current_database_path": str(current_db.resolve()),
        "proposed_database_path": str(proposed_db.resolve()),
        "future_canonical_registry_change": {
            "field": "libraries[].root_path for active_library",
            "from": str(current_root.resolve()),
            "to": str(proposed_root.resolve()),
        },
        "shadow_registry_plan": [
            {
                "path": str(path.resolve()),
                "action": "no_change_in_readiness_audit",
                "note": "keep existing deprecated shadow-report status until a separate controlled migration apply",
            }
            for path in shadow_paths
        ],
    }


def _backup_plan(canonical_registry: Path, active_db: Path, source_root: Path, target_root: Path) -> list[str]:
    return [
        f"Backup canonical registry: {canonical_registry.resolve()} -> timestamped .bak before any apply.",
        f"Backup active database: {active_db.resolve()} -> timestamped database.sqlite.bak before any copy or registry change.",
        f"Backup source artifact/library tree: {source_root.resolve()} -> immutable snapshot or filesystem-level backup manifest before apply.",
        f"Capture target preflight manifest for {target_root.resolve()} including existing files, SHA256, and timestamps before any write.",
    ]


def _rollback_plan(canonical_registry: Path, active_db: Path, source_root: Path, target_root: Path) -> list[str]:
    return [
        f"Restore canonical registry backup to {canonical_registry.resolve()}.",
        f"Restore database.sqlite backup to {active_db.resolve()} if any post-copy DB mutation occurred.",
        f"Delete copied target files under {target_root.resolve()} that were created during migration, or restore the target artifact directory backup if partial writes happened.",
        f"Re-verify source library root {source_root.resolve()} remains intact and rerun integrity_check plus paper-count audit before reopening runtime traffic.",
    ]


def _risk_level(*, source_is_mirror: bool, missing_files: int, path_conflicts: int, integrity_result: str, papers_total: int) -> str:
    if integrity_result.lower() != "ok" or papers_total != 15:
        return "high"
    if path_conflicts > 0:
        return "high"
    if missing_files > 0:
        return "medium"
    if source_is_mirror:
        return "medium"
    return "low"


def _recommended_next_gate(risk_level: str, *, path_conflicts: int, missing_files: int) -> str:
    if risk_level == "high" and path_conflicts > 0:
        return "block_controlled_migration_apply_until_target_conflicts_are_resolved_and_a_clean_target_root_is_prepared"
    if risk_level in {"high", "medium"} and missing_files > 0:
        return "resolve_missing_artifact_references_then_repeat_readiness_audit_before_any_apply"
    if risk_level == "medium":
        return "prepare_clean_canonical_target_backup_set_then_execute_a_separate_controlled_migration_apply_gate"
    return "controlled_migration_apply_can_be_planned_after_backup_and_freeze_confirmation"


def _migration_mode_recommendation(*, missing_files: int, unreferenced_files: int, target_conflicts: int) -> str:
    if missing_files > 0:
        return "blocked_until_missing_referenced_files_are_resolved"
    if unreferenced_files > 0 or target_conflicts > 0:
        return "db_referenced_only_plus_required_library_metadata"
    return "all_source_files"


def _read_sha_stability(
    canonical_registry: Path,
    shadow_paths: list[Path],
    active_db: Path,
    *,
    extra_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    extra_paths = extra_paths or {}

    def snapshot() -> dict[str, Any]:
        return {
            "canonical_registry_sha256": _sha256(canonical_registry),
            "shadow_registry_sha256": {str(path.resolve()): _sha256(path) for path in shadow_paths},
            "active_sqlite_sha256": _sha256(active_db),
            **{label: _sha256(path.resolve()) for label, path in extra_paths.items()},
        }

    before = snapshot()
    after = snapshot()
    return {
        "before": before,
        "after": after,
        "unchanged": before == after,
    }


def build_report() -> dict[str, Any]:
    canonical_registry = canonical_registry_path().resolve()
    canonical_payload = _load_json(canonical_registry)
    active_entry = _registry_entry(canonical_payload)
    if active_entry is None or not active_entry.get("root_path"):
        raise RuntimeError(f"Active library entry missing in canonical registry: {canonical_registry}")

    active_library = canonical_payload.get("active_library")
    current_root = Path(str(active_entry["root_path"])).resolve()
    current_db = (current_root / "database.sqlite").resolve()
    current_library_json = (current_root / "library.json").resolve()
    proposed_root = default_library_root().resolve()
    proposed_db = (proposed_root / "database.sqlite").resolve()
    proposed_library_json = (proposed_root / "library.json").resolve()
    shadows = [path.resolve() for path in shadow_registry_paths() if path.exists()]

    if not current_db.exists():
        raise RuntimeError(f"Active database missing: {current_db}")

    storage_summary, storage_files, storage_file_count, storage_total_bytes = _storage_dir_counts(current_root)
    all_source_files = [
        {
            "type": "database.sqlite",
            "relative_path": "database.sqlite",
            "absolute_path": str(current_db),
            "bytes": int(current_db.stat().st_size),
        },
        *storage_files,
    ]
    all_source_bytes = int(current_db.stat().st_size) + storage_total_bytes

    source_inventory = classify_source_artifact_inventory(current_root, proposed_root, current_db)
    artifact_audit = source_inventory["artifact_audit"]
    base_conflicts = _existing_file_conflicts(current_root, proposed_root)
    merged_conflicts = base_conflicts + [
        item for item in artifact_audit["path_conflicts"] if item not in base_conflicts
    ]
    integrity_result = _sqlite_integrity(current_db)
    papers_total = _papers_total(current_db)

    files_to_copy_by_type = Counter(item["type"] for item in all_source_files)
    sha_stability = _read_sha_stability(
        canonical_registry,
        shadows,
        current_db,
        extra_paths={
            "target_sqlite_sha256": proposed_db,
            "target_library_json_sha256": proposed_library_json,
        },
    )
    risk_level = _risk_level(
        source_is_mirror=_is_mirror_path(str(current_root)),
        missing_files=len(artifact_audit["missing_files"]),
        path_conflicts=len(merged_conflicts),
        integrity_result=integrity_result,
        papers_total=papers_total,
    )

    report = {
        "mode": "dry_run",
        "apply_supported": False,
        "current_active_library_root": str(current_root),
        "current_active_database_path": str(current_db),
        "current_storage_paths_summary": storage_summary,
        "current_root_files": all_source_files,
        "proposed_canonical_library_root": str(proposed_root),
        "proposed_database_path": str(proposed_db),
        "files_to_copy_count": len(all_source_files),
        "all_source_files_to_copy_count": len(all_source_files),
        "files_to_copy_by_type": {
            "database.sqlite": int(files_to_copy_by_type.get("database.sqlite", 0)),
            "pdf": int(files_to_copy_by_type.get("pdf", 0)),
            "markdown": int(files_to_copy_by_type.get("markdown", 0)),
            "tei": int(files_to_copy_by_type.get("tei", 0)),
            "docling_json": int(files_to_copy_by_type.get("docling_json", 0)),
            "figures": int(files_to_copy_by_type.get("figures", 0)),
            "tables": int(files_to_copy_by_type.get("tables", 0)),
            "images": int(files_to_copy_by_type.get("images", 0)),
        },
        "total_bytes_to_copy": all_source_bytes,
        "required_library_metadata_files": [
            json_file_summary(current_library_json),
        ],
        "db_referenced_files_to_copy_count": source_inventory["db_referenced_files_to_copy_count"],
        "unreferenced_files_count": source_inventory["unreferenced_files_count"],
        "source_file_inventory_all": source_inventory["source_file_inventory_all"],
        "source_file_inventory_db_referenced": source_inventory["source_file_inventory_db_referenced"],
        "source_file_inventory_unreferenced": source_inventory["source_file_inventory_unreferenced"],
        "unreferenced_pdf_count": source_inventory["unreferenced_pdf_count"],
        "unreferenced_pdf_total_bytes": source_inventory["unreferenced_pdf_total_bytes"],
        "unreferenced_pdf_examples": source_inventory["unreferenced_pdf_examples"],
        "unreferenced_pdf_mtime_range": source_inventory["unreferenced_pdf_mtime_range"],
        "unreferenced_pdf_origin_hints": source_inventory["unreferenced_pdf_origin_hints"],
        "unreferenced_non_pdf_count": source_inventory["unreferenced_non_pdf_count"],
        "missing_referenced_files_count": source_inventory["missing_referenced_files_count"],
        "duplicate_or_suspect_files": source_inventory["duplicate_or_suspect_files"],
        "migration_mode_recommendation": _migration_mode_recommendation(
            missing_files=source_inventory["missing_referenced_files_count"],
            unreferenced_files=source_inventory["unreferenced_files_count"],
            target_conflicts=len(merged_conflicts),
        ),
        "missing_files": artifact_audit["missing_files"],
        "path_conflicts": merged_conflicts,
        "duplicate_artifact_paths": artifact_audit["duplicate_artifact_paths"],
        "artifact_paths_already_canonical_count": artifact_audit["artifact_paths_already_canonical_count"],
        "artifact_paths_needing_update_count": artifact_audit["artifact_paths_needing_update_count"],
        "artifact_field_summary": artifact_audit["artifact_field_summary"],
        "registry_update_plan": _registry_update_plan(
            canonical_registry,
            shadows,
            active_library=active_library,
            current_root=current_root,
            proposed_root=proposed_root,
            current_db=current_db,
            proposed_db=proposed_db,
        ),
        "sqlite_integrity_check_result": integrity_result,
        "active_db_papers_total": papers_total,
        "backup_plan": _backup_plan(canonical_registry, current_db, current_root, proposed_root),
        "rollback_plan": _rollback_plan(canonical_registry, current_db, current_root, proposed_root),
        "migration_risk_level": risk_level,
        "recommended_next_gate": _recommended_next_gate(
            risk_level,
            path_conflicts=len(merged_conflicts),
            missing_files=len(artifact_audit["missing_files"]),
        ),
        "source_root_is_historical_mirror": _is_mirror_path(str(current_root)),
        "canonical_registry_path": str(canonical_registry),
        "shadow_registry_paths": [str(path) for path in shadows],
        "sha256_stability": sha_stability,
    }
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D2-8 historical mirror root migration readiness audit (dry-run only).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--apply", action="store_true", help="Unsupported. This script is read-only.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    args = _parse_args()
    if args.apply:
        raise SystemExit("apply is not supported; this readiness audit is dry-run only")

    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
