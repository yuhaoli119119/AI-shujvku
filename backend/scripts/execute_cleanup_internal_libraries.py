from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models import Base, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.utils.project_paths import resolve_data_mount_path  # noqa: E402


STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_PARTIAL = "PARTIAL_SUCCESS"
EXPECTED_PAPER_COUNT = 37

DELETE_PREFIXES = ("chain_", "fresh_", "failure_", "core_suite_")
PROTECTED_LIBRARY_NAMES = {
    "\u9ed8\u8ba4\u6587\u732e\u5e93",
    "\u77f3\u58a8\u7094",
    "graphene_dual_atom_li_s_battery",
}
EXPLICIT_INTERNAL_LIBRARY_NAMES = {
    "chain_realpaper_smoke_20260608",
    "chain_fresh_realpaper_acceptance_20260608",
    "chain_fresh_repeatability_20260608_round01",
    "chain_fresh_repeatability_20260608_round02",
    "chain_fresh_repeatability_20260608_round03",
    "fresh_real_paper_smoke_20260608",
    "chain_smoke_20260608",
}
EXPLICIT_INTERNAL_LIBRARY_PREFIXES = ("chain_failure_recovery_acceptance_20260608",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute confirmed internal library cleanup.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--paper-ids", required=True)
    parser.add_argument("--artifact-paths", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-delete-internal-libraries", action="store_true")
    parser.add_argument("--allow-count-mismatch", action="store_true")
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"missing_required_file:{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"missing_required_file:{path}")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def classify_library(library_name: str | None) -> str:
    name = (library_name or "").strip()
    lowered = name.lower()
    if name in PROTECTED_LIBRARY_NAMES:
        return "protected"
    if lowered.startswith(DELETE_PREFIXES):
        return "candidate_delete"
    if name in EXPLICIT_INTERNAL_LIBRARY_NAMES:
        return "candidate_delete"
    if any(lowered.startswith(prefix) for prefix in EXPLICIT_INTERNAL_LIBRARY_PREFIXES):
        return "candidate_delete"
    return "protected"


def table_rows(session: Any, table: sa.Table, where_clause: Any) -> list[dict[str, Any]]:
    rows = session.execute(sa.select(table).where(where_clause)).mappings().all()
    return [dict(row) for row in rows]


def backup_table_rows(
    session: Any,
    existing_table_names: set[str],
    paper_ids: list[uuid.UUID],
    library_names: list[str],
) -> dict[str, list[dict[str, Any]]]:
    related: dict[str, list[dict[str, Any]]] = {}
    excluded = {"papers", "external_analysis_runs", "external_analysis_candidates"}
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_table_names or table.name in excluded:
            continue
        if "paper_id" in table.c:
            rows = table_rows(session, table, table.c.paper_id.in_(paper_ids))
            if rows:
                related[table.name] = rows

    relationship_table = Base.metadata.tables.get("paper_relationships")
    if relationship_table is not None and relationship_table.name in existing_table_names:
        rows = table_rows(
            session,
            relationship_table,
            sa.or_(
                relationship_table.c.source_paper_id.in_(paper_ids),
                relationship_table.c.target_paper_id.in_(paper_ids),
            ),
        )
        if rows:
            related["paper_relationships_by_source_or_target"] = rows

    reference_table = Base.metadata.tables.get("reference_entries")
    if reference_table is not None and reference_table.name in existing_table_names:
        rows = table_rows(session, reference_table, reference_table.c.linked_paper_id.in_(paper_ids))
        if rows:
            related["reference_entries_by_linked_paper_id"] = rows

    workflow_table = Base.metadata.tables.get("workflow_jobs")
    if workflow_table is not None and workflow_table.name in existing_table_names and library_names:
        rows = table_rows(session, workflow_table, workflow_table.c.library_name.in_(library_names))
        if rows:
            related["workflow_jobs_by_library_name"] = rows
    return related


def create_final_backup(
    session: Any,
    existing_table_names: set[str],
    backup_dir: Path,
    paper_ids: list[uuid.UUID],
    paper_id_texts: list[str],
    artifact_paths: list[str],
    library_names: list[str],
) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    paper_rows = table_rows(session, Paper.__table__, Paper.__table__.c.id.in_(paper_ids))
    run_rows = table_rows(
        session,
        ExternalAnalysisRun.__table__,
        ExternalAnalysisRun.__table__.c.paper_id.in_(paper_ids),
    )
    run_ids = [row["id"] for row in run_rows]
    candidate_where = ExternalAnalysisCandidate.__table__.c.paper_id.in_(paper_ids)
    if run_ids:
        candidate_where = sa.or_(candidate_where, ExternalAnalysisCandidate.__table__.c.run_id.in_(run_ids))
    candidate_rows = table_rows(session, ExternalAnalysisCandidate.__table__, candidate_where)
    related_rows = backup_table_rows(session, existing_table_names, paper_ids, library_names)

    write_json(backup_dir / "deleted_papers_backup.json", paper_rows)
    write_json(backup_dir / "deleted_external_analysis_runs_backup.json", run_rows)
    write_json(backup_dir / "deleted_external_analysis_candidates_backup.json", candidate_rows)
    write_json(backup_dir / "deleted_related_records_backup.json", related_rows)
    write_text(backup_dir / "artifact_paths_backup.txt", "\n".join(artifact_paths) + "\n")
    write_text(backup_dir / "paper_ids_backup.txt", "\n".join(paper_id_texts) + "\n")
    write_text(
        backup_dir / "pg_dump_note.txt",
        (
            "pg_dump was not executed by this cleanup script. "
            "The final targeted JSON backups and paper/artifact manifests were written before deletion.\n"
        ),
    )
    return {
        "paper_rows": len(paper_rows),
        "external_analysis_run_rows": len(run_rows),
        "external_analysis_candidate_rows": len(candidate_rows),
        "related_tables": {name: len(rows) for name, rows in related_rows.items()},
        "pg_dump_executed": False,
    }


def delete_where(session: Any, table: sa.Table, where_clause: Any) -> int:
    result = session.execute(table.delete().where(where_clause))
    return int(result.rowcount or 0)


def delete_db_records(
    session: Any,
    existing_table_names: set[str],
    paper_ids: list[uuid.UUID],
    library_names: list[str],
) -> dict[str, Any]:
    deleted: dict[str, Any] = {"tables": {}}

    run_ids = [
        row[0]
        for row in session.execute(
            sa.select(ExternalAnalysisRun.id).where(ExternalAnalysisRun.paper_id.in_(paper_ids))
        ).all()
    ]
    candidate_where = ExternalAnalysisCandidate.paper_id.in_(paper_ids)
    if run_ids:
        candidate_where = sa.or_(candidate_where, ExternalAnalysisCandidate.run_id.in_(run_ids))
    deleted_candidates = delete_where(session, ExternalAnalysisCandidate.__table__, candidate_where)
    deleted_runs = delete_where(session, ExternalAnalysisRun.__table__, ExternalAnalysisRun.paper_id.in_(paper_ids))
    deleted["external_analysis_candidates"] = deleted_candidates
    deleted["external_analysis_runs"] = deleted_runs

    skip_tables = {"papers", "external_analysis_candidates", "external_analysis_runs"}
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in existing_table_names or table.name in skip_tables:
            continue
        if "paper_id" in table.c:
            count = delete_where(session, table, table.c.paper_id.in_(paper_ids))
            if count:
                deleted["tables"][table.name] = count

    relationship_table = Base.metadata.tables.get("paper_relationships")
    if relationship_table is not None and relationship_table.name in existing_table_names:
        count = delete_where(
            session,
            relationship_table,
            sa.or_(
                relationship_table.c.source_paper_id.in_(paper_ids),
                relationship_table.c.target_paper_id.in_(paper_ids),
            ),
        )
        if count:
            deleted["tables"]["paper_relationships_by_source_or_target"] = count

    reference_table = Base.metadata.tables.get("reference_entries")
    if reference_table is not None and reference_table.name in existing_table_names:
        result = session.execute(
            reference_table.update()
            .where(reference_table.c.linked_paper_id.in_(paper_ids))
            .values(linked_paper_id=None)
        )
        if result.rowcount:
            deleted["tables"]["reference_entries_linked_paper_id_set_null"] = int(result.rowcount or 0)

    workflow_table = Base.metadata.tables.get("workflow_jobs")
    if workflow_table is not None and workflow_table.name in existing_table_names and library_names:
        count = delete_where(session, workflow_table, workflow_table.c.library_name.in_(library_names))
        if count:
            deleted["tables"]["workflow_jobs_by_library_name"] = count

    deleted_papers = delete_where(session, Paper.__table__, Paper.__table__.c.id.in_(paper_ids))
    deleted["papers"] = deleted_papers
    deleted["reviews"] = int(deleted["tables"].get("extraction_field_reviews", 0))
    return deleted


def storage_roots() -> list[Path]:
    settings = get_settings()
    candidates = [
        resolve_data_mount_path(settings.storage_root),
        PROJECT_ROOT / "data" / "storage",
        BACKEND_ROOT / "data" / "storage",
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_artifact_paths(artifact_paths: list[str], roots: list[Path]) -> list[Path]:
    resolved_paths: list[Path] = []
    for raw_path in artifact_paths:
        path = Path(raw_path).resolve()
        if not any(is_under(path, root) for root in roots):
            raise RuntimeError(f"artifact_path_outside_storage_root:{path}")
        resolved_paths.append(path)
    return resolved_paths


def delete_artifact_files(artifact_paths: list[Path], roots: list[Path]) -> dict[str, Any]:
    deleted_count = 0
    deleted_bytes = 0
    failed: list[dict[str, str]] = []
    missing: list[str] = []
    deleted_files: list[Path] = []

    for path in artifact_paths:
        if not path.exists():
            missing.append(str(path))
            continue
        if not path.is_file():
            failed.append({"path": str(path), "error": "not_a_file"})
            continue
        try:
            size = int(path.stat().st_size)
            path.unlink()
            deleted_count += 1
            deleted_bytes += size
            deleted_files.append(path)
        except OSError as exc:
            failed.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    removed_dirs: list[str] = []
    for file_path in sorted(deleted_files, key=lambda item: len(item.parts), reverse=True):
        current = file_path.parent
        while any(is_under(current, root) and current != root for root in roots):
            try:
                current.rmdir()
                removed_dirs.append(str(current))
            except OSError:
                break
            current = current.parent
    return {
        "deleted_artifact_file_count": deleted_count,
        "deleted_artifact_bytes": deleted_bytes,
        "failed_artifact_deletes": failed,
        "missing_artifact_paths": missing,
        "removed_empty_dirs": removed_dirs,
    }


def library_counts_after(session: Any) -> dict[str, int]:
    rows = session.execute(
        sa.select(Paper.library_name, sa.func.count()).group_by(Paper.library_name).order_by(Paper.library_name)
    ).all()
    return {str(name): int(count or 0) for name, count in rows}


def internal_library_counts(counts: dict[str, int]) -> dict[str, int]:
    return {name: count for name, count in counts.items() if classify_library(name) == "candidate_delete"}


def preflight(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    plan: dict[str, Any] | None,
    paper_id_texts: list[str],
) -> None:
    if not args.execute or not args.confirm_delete_internal_libraries:
        raise RuntimeError("execute_flags_missing")
    if not paper_id_texts:
        raise RuntimeError("paper_ids_empty")
    manifest_ids = [str(item).strip() for item in manifest.get("paper_ids", []) if str(item).strip()]
    if manifest_ids and sorted(manifest_ids) != sorted(paper_id_texts):
        raise RuntimeError("manifest_paper_ids_do_not_match_paper_ids_file")
    manual_review_count = 0
    if plan:
        manual_review_count = int(plan.get("stats", {}).get("manual_review_papers", 0) or 0)
    else:
        manual_review_count = int(manifest.get("manual_review_papers", 0) or 0)
    if manual_review_count != 0:
        raise RuntimeError(f"manual_review_not_zero:{manual_review_count}")
    if len(paper_id_texts) != EXPECTED_PAPER_COUNT and not args.allow_count_mismatch:
        raise RuntimeError(f"paper_count_mismatch:{len(paper_id_texts)}")


def validate_db_targets(session: Any, paper_ids: list[uuid.UUID], paper_id_texts: list[str]) -> list[str]:
    rows = session.execute(
        sa.select(Paper.id, Paper.library_name).where(Paper.id.in_(paper_ids)).order_by(Paper.library_name)
    ).all()
    found_ids = {str(row.id) for row in rows}
    missing = sorted(set(paper_id_texts) - found_ids)
    if missing:
        raise RuntimeError(f"target_papers_missing_in_db:{','.join(missing)}")
    libraries: set[str] = set()
    for paper_id, library_name in rows:
        name = str(library_name or "")
        if classify_library(name) != "candidate_delete":
            raise RuntimeError(f"target_paper_not_internal:{paper_id}:{name}")
        if name in PROTECTED_LIBRARY_NAMES:
            raise RuntimeError(f"protected_library_targeted:{paper_id}:{name}")
        libraries.add(name)
    return sorted(libraries)


def render_markdown(report: dict[str, Any]) -> str:
    status = report["status"]
    lines = ["# Internal Library Cleanup Execute", ""]
    if status == STATUS_PASS:
        lines.append("INTERNAL_LIBRARY_CLEANUP_EXECUTE=PASS")
    elif status == STATUS_PARTIAL:
        lines.append("INTERNAL_LIBRARY_CLEANUP_EXECUTE=PARTIAL_SUCCESS")
        lines.append(f"root_cause={report.get('root_cause', 'artifact_delete_partial_failed')}")
    else:
        lines.append("INTERNAL_LIBRARY_CLEANUP_EXECUTE=FAIL")
        lines.append(f"root_cause={report.get('root_cause', 'unknown')}")
    lines.extend(["", "## Summary", ""])
    keys = [
        "deleted_paper_count",
        "deleted_external_analysis_run_count",
        "deleted_external_analysis_candidate_count",
        "deleted_review_record_count",
        "deleted_artifact_file_count",
        "deleted_artifact_bytes",
        "remaining_total_papers_after",
        "\u77f3\u58a8\u7094_count_after",
        "default_library_count_after",
        "backup_dir",
    ]
    for key in keys:
        if key in report:
            lines.append(f"- {key}: {report[key]}")
    lines.append("")
    lines.append("## Deleted Libraries")
    lines.append("")
    if report.get("deleted_libraries"):
        for library_name in report["deleted_libraries"]:
            lines.append(f"- {library_name}")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Remaining Internal Libraries After")
    lines.append("")
    remaining = report.get("remaining_internal_library_counts_after", {})
    if remaining:
        for library_name, count in remaining.items():
            lines.append(f"- {library_name}: {count}")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("## Failed Artifact Deletes")
    lines.append("")
    failed = report.get("failed_artifact_deletes", [])
    if failed:
        for item in failed:
            lines.append(f"- {item['path']}: {item['error']}")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def fail_report(args: argparse.Namespace, root_cause: str, detail: str) -> int:
    report = {
        "status": STATUS_FAIL,
        "root_cause": root_cause,
        "detail": detail,
        "generated_at": now_iso(),
        "db_deleted": False,
        "files_deleted": False,
    }
    write_json(Path(args.output), report)
    write_text(Path(args.markdown), render_markdown(report))
    print(f"INTERNAL_LIBRARY_CLEANUP_EXECUTE=FAIL root_cause={root_cause}")
    return 1


def main() -> int:
    args = parse_args()
    try:
        manifest_path = Path(args.manifest)
        manifest = load_json(manifest_path)
        plan_path = manifest_path.with_name("internal_library_cleanup_plan.json")
        plan = load_json(plan_path) if plan_path.exists() else None
        paper_id_texts = load_lines(Path(args.paper_ids))
        artifact_path_texts = load_lines(Path(args.artifact_paths))
        preflight(args, manifest, plan, paper_id_texts)
        paper_ids = [uuid.UUID(item) for item in paper_id_texts]
        roots = storage_roots()
        artifact_paths = validate_artifact_paths(artifact_path_texts, roots)
    except Exception as exc:
        return fail_report(args, "preflight_failed", f"{type(exc).__name__}: {exc}")

    settings = get_settings()
    engine = get_engine(settings.database_url)
    existing_table_names = set(sa.inspect(engine).get_table_names())
    backup_dir = Path(args.backup_dir)
    try:
        with session_scope(settings.database_url) as session:
            deleted_libraries = validate_db_targets(session, paper_ids, paper_id_texts)
            backup_summary = create_final_backup(
                session,
                existing_table_names,
                backup_dir,
                paper_ids,
                paper_id_texts,
                artifact_path_texts,
                deleted_libraries,
            )
    except (SQLAlchemyError, RuntimeError, OSError) as exc:
        return fail_report(args, "backup_or_validation_failed", f"{type(exc).__name__}: {exc}")

    try:
        with session_scope(settings.database_url) as session:
            db_delete_summary = delete_db_records(session, existing_table_names, paper_ids, deleted_libraries)
    except (SQLAlchemyError, RuntimeError) as exc:
        return fail_report(args, "db_delete_failed", f"{type(exc).__name__}: {exc}")

    artifact_summary = delete_artifact_files(artifact_paths, roots)

    with session_scope(settings.database_url) as session:
        counts_after = library_counts_after(session)
    remaining_internal = internal_library_counts(counts_after)
    failed_artifacts = artifact_summary["failed_artifact_deletes"]
    status = STATUS_PARTIAL if failed_artifacts else STATUS_PASS
    report: dict[str, Any] = {
        "status": status,
        "root_cause": "artifact_delete_partial_failed" if failed_artifacts else None,
        "generated_at": now_iso(),
        "deleted_paper_count": int(db_delete_summary.get("papers", 0)),
        "deleted_libraries": deleted_libraries,
        "deleted_external_analysis_run_count": int(db_delete_summary.get("external_analysis_runs", 0)),
        "deleted_external_analysis_candidate_count": int(db_delete_summary.get("external_analysis_candidates", 0)),
        "deleted_review_record_count": int(db_delete_summary.get("reviews", 0)),
        "deleted_artifact_file_count": artifact_summary["deleted_artifact_file_count"],
        "deleted_artifact_bytes": artifact_summary["deleted_artifact_bytes"],
        "protected_library_counts_after": {
            name: int(counts_after.get(name, 0)) for name in sorted(PROTECTED_LIBRARY_NAMES)
        },
        "remaining_internal_library_counts_after": remaining_internal,
        "remaining_total_papers_after": sum(counts_after.values()),
        "\u77f3\u58a8\u7094_count_after": int(counts_after.get("\u77f3\u58a8\u7094", 0)),
        "default_library_count_after": int(counts_after.get("\u9ed8\u8ba4\u6587\u732e\u5e93", 0)),
        "backup_dir": str(backup_dir),
        "failed_artifact_deletes": failed_artifacts,
        "missing_artifact_paths": artifact_summary["missing_artifact_paths"],
        "removed_empty_dirs": artifact_summary["removed_empty_dirs"],
        "backup_summary": backup_summary,
        "db_delete_summary": db_delete_summary,
        "db_modified": True,
        "files_deleted": artifact_summary["deleted_artifact_file_count"] > 0,
        "pg_dump_executed": False,
    }
    write_json(Path(args.output), report)
    write_text(Path(args.markdown), render_markdown(report))
    if status == STATUS_PARTIAL:
        print("INTERNAL_LIBRARY_CLEANUP_EXECUTE=PARTIAL_SUCCESS")
    else:
        print("INTERNAL_LIBRARY_CLEANUP_EXECUTE=PASS")
    print(f"deleted_paper_count={report['deleted_paper_count']}")
    print(f"deleted_artifact_file_count={report['deleted_artifact_file_count']}")
    print(f"remaining_internal_library_count={len(remaining_internal)}")
    return 0 if status == STATUS_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
