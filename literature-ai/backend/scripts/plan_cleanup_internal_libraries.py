from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.db.models import Base, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper  # noqa: E402
from app.db.session import get_engine, session_scope  # noqa: E402
from app.utils.artifact_paths import resolve_persisted_artifact_path  # noqa: E402
from app.utils.project_paths import resolve_data_mount_path  # noqa: E402


STATUS_READY = "READY_FOR_USER_CONFIRMATION"
STATUS_FAIL = "FAIL"

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
EXPLICIT_INTERNAL_LIBRARY_PREFIXES = (
    "chain_failure_recovery_acceptance_20260608",
)
SUSPICIOUS_REVIEW_TOKENS = (
    "acceptance",
    "repeatability",
    "smoke",
    "realpaper",
    "real_paper",
    "codex",
    "gate",
    "probe",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan a dry-run cleanup of internal acceptance/test libraries."
    )
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--backup-manifest", required=True)
    parser.add_argument("--paper-ids-output", required=True)
    parser.add_argument("--artifact-paths-output", required=True)
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def mask_database_url(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.split("@", 1)
    if ":" in credentials:
        user, _password = credentials.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"


def fetch_api_db_info(api_base: str) -> tuple[dict[str, Any] | None, str | None]:
    base = api_base.rstrip("/")
    for suffix in ("/api/system/db-info", "/api/db-info"):
        url = f"{base}{suffix}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                payload["_source_url"] = url
                return payload, None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


def apply_runtime_storage_root(settings: Settings, api_info: dict[str, Any] | None) -> str | None:
    if not api_info:
        return None
    raw_root = str(api_info.get("effective_storage_root") or "").strip()
    if not raw_root:
        return None
    try:
        host_root = resolve_data_mount_path(raw_root)
    except Exception:
        host_root = Path(raw_root).resolve()
    object.__setattr__(settings, "storage_root", host_root)
    return str(host_root)


def classify_library(library_name: str | None) -> tuple[str, str]:
    name = (library_name or "").strip()
    lowered = name.lower()
    if name in PROTECTED_LIBRARY_NAMES:
        return "protected", "protected_library_name"
    if lowered.startswith(DELETE_PREFIXES):
        matching = next(prefix for prefix in DELETE_PREFIXES if lowered.startswith(prefix))
        return "candidate_delete", f"library_prefix_{matching.rstrip('_')}"
    if name in EXPLICIT_INTERNAL_LIBRARY_NAMES:
        return "candidate_delete", "explicit_internal_acceptance_library"
    if any(lowered.startswith(prefix) for prefix in EXPLICIT_INTERNAL_LIBRARY_PREFIXES):
        return "candidate_delete", "explicit_internal_acceptance_library_prefix"
    if any(token in lowered for token in SUSPICIOUS_REVIEW_TOKENS):
        return "manual_review", "suspicious_acceptance_like_name_not_in_delete_rule"
    return "protected", "formal_or_unmatched_library"


def path_text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def directory_size_and_files(path: Path) -> tuple[int, int, list[Path]]:
    total = 0
    count = 0
    files: list[Path] = []
    try:
        iterator = path.rglob("*")
    except OSError:
        return 0, 0, []
    for item in iterator:
        try:
            if not item.is_file():
                continue
            count += 1
            size = file_size(item)
            total += size
            files.append(item.resolve())
        except OSError:
            continue
    return total, count, files


def resolve_artifact_entry(
    raw_value: str | None,
    *,
    label: str,
    category: str | None,
    settings: Settings,
) -> tuple[dict[str, Any], list[Path]]:
    resolved = resolve_persisted_artifact_path(
        raw_value,
        category=category,
        settings=settings,
        must_exist=True,
    )
    planned = resolved
    if planned is None:
        planned = resolve_persisted_artifact_path(
            raw_value,
            category=category,
            settings=settings,
            must_exist=False,
        )
    existing_files: list[Path] = []
    exists = bool(resolved and resolved.exists())
    kind = "missing"
    bytes_total = 0
    file_count = 0
    if exists and resolved is not None:
        if resolved.is_file():
            kind = "file"
            bytes_total = file_size(resolved)
            file_count = 1
            existing_files.append(resolved.resolve())
        elif resolved.is_dir():
            kind = "directory"
            bytes_total, file_count, existing_files = directory_size_and_files(resolved)
    elif raw_value:
        kind = "unresolved"
    return (
        {
            "label": label,
            "raw_path": raw_value,
            "resolved_path": path_text(resolved),
            "planned_path": path_text(planned),
            "exists": exists,
            "kind": kind,
            "file_count": file_count,
            "bytes": bytes_total,
        },
        existing_files,
    )


def workspace_candidates(paper: Paper, settings: Settings) -> list[Path]:
    raw_value = str(getattr(paper, "workspace_path", "") or "").strip()
    candidates: list[Path] = []
    if raw_value:
        raw_path = Path(raw_value)
        if raw_path.is_absolute() or (len(raw_value) > 2 and raw_value[1:3] in (":\\", ":/")):
            candidates.append(raw_path)
        else:
            candidates.append(settings.storage_root / raw_path)
            candidates.append(settings.storage_root / "by_id" / str(paper.id))
    else:
        candidates.append(settings.storage_root / "by_id" / str(paper.id))

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


def resolve_workspace_entry(paper: Paper, settings: Settings) -> tuple[dict[str, Any], list[Path]]:
    candidates = workspace_candidates(paper, settings)
    existing = next((path for path in candidates if path.exists()), None)
    planned = existing or candidates[0]
    existing_files: list[Path] = []
    bytes_total = 0
    file_count = 0
    if existing and existing.is_file():
        bytes_total = file_size(existing)
        file_count = 1
        existing_files = [existing.resolve()]
    elif existing and existing.is_dir():
        bytes_total, file_count, existing_files = directory_size_and_files(existing)
    return (
        {
            "label": "workspace_path",
            "raw_path": paper.workspace_path,
            "resolved_path": path_text(existing),
            "planned_path": path_text(planned),
            "exists": bool(existing and existing.exists()),
            "kind": "directory" if existing and existing.is_dir() else "missing",
            "file_count": file_count,
            "bytes": bytes_total,
        },
        existing_files,
    )


def resolve_ai_package_entry(paper: Paper, settings: Settings) -> tuple[dict[str, Any], list[Path]]:
    workspaces = workspace_candidates(paper, settings)
    candidates = [workspace / "extraction" / "ai_reading_package.json" for workspace in workspaces]
    existing = next((path for path in candidates if path.exists() and path.is_file()), None)
    planned = existing or candidates[0]
    files = [existing.resolve()] if existing else []
    return (
        {
            "label": "ai_reading_package",
            "raw_path": None,
            "resolved_path": path_text(existing),
            "planned_path": path_text(planned),
            "exists": bool(existing),
            "kind": "file" if existing else "missing",
            "file_count": 1 if existing else 0,
            "bytes": file_size(existing) if existing else 0,
        },
        files,
    )


def collect_paper_artifacts(
    session: Any,
    paper: Paper,
    settings: Settings,
) -> tuple[dict[str, str | None], list[dict[str, Any]], list[Path]]:
    entries: list[dict[str, Any]] = []
    files: list[Path] = []
    direct_paths: dict[str, str | None] = {}
    fields = [
        ("pdf_path", paper.pdf_path, "pdf"),
        ("markdown_path", paper.markdown_path, "markdown"),
        ("docling_json_path", paper.docling_json_path, "docling_json"),
        ("grobid_tei_path", paper.tei_path, "tei"),
    ]
    for label, raw_value, category in fields:
        entry, existing_files = resolve_artifact_entry(raw_value, label=label, category=category, settings=settings)
        entries.append(entry)
        files.extend(existing_files)
        direct_paths[label] = entry["resolved_path"] or entry["planned_path"]

    workspace_entry, workspace_files = resolve_workspace_entry(paper, settings)
    entries.append(workspace_entry)
    files.extend(workspace_files)
    direct_paths["workspace_path"] = workspace_entry["resolved_path"] or workspace_entry["planned_path"]

    ai_entry, ai_files = resolve_ai_package_entry(paper, settings)
    entries.append(ai_entry)
    files.extend(ai_files)

    paper_figures = Base.metadata.tables.get("paper_figures")
    if paper_figures is not None:
        figure_rows = session.execute(
            sa.select(paper_figures.c.image_path).where(paper_figures.c.paper_id == paper.id)
        ).all()
        for index, row in enumerate(figure_rows, start=1):
            raw_path = row[0]
            if not raw_path:
                continue
            entry, existing_files = resolve_artifact_entry(
                raw_path,
                label=f"figure_image_{index}",
                category="figures",
                settings=settings,
            )
            entries.append(entry)
            files.extend(existing_files)

    unique_files: list[Path] = []
    seen: set[Path] = set()
    for file_path in files:
        try:
            resolved = file_path.resolve()
        except OSError:
            resolved = file_path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_files.append(resolved)
    return direct_paths, entries, unique_files


def count_table_for_paper(session: Any, table: sa.Table, paper_id: Any) -> int:
    try:
        value = session.execute(
            sa.select(sa.func.count()).select_from(table).where(table.c.paper_id == paper_id)
        ).scalar()
        return int(value or 0)
    except SQLAlchemyError:
        return 0


def count_optional_column(session: Any, table: sa.Table, column_name: str, paper_id: Any) -> int:
    if column_name not in table.c:
        return 0
    try:
        value = session.execute(
            sa.select(sa.func.count()).select_from(table).where(table.c[column_name] == paper_id)
        ).scalar()
        return int(value or 0)
    except SQLAlchemyError:
        return 0


def count_db_records_for_paper(
    session: Any,
    existing_table_names: set[str],
    paper: Paper,
) -> dict[str, int]:
    counts: dict[str, int] = {"papers": 1}
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_table_names:
            continue
        if table.name == "papers":
            continue
        if "paper_id" in table.c:
            counts[table.name] = count_table_for_paper(session, table, paper.id)

    optional_targets = {
        "paper_relationships.source_paper_id": ("paper_relationships", "source_paper_id"),
        "paper_relationships.target_paper_id": ("paper_relationships", "target_paper_id"),
        "reference_entries.linked_paper_id": ("reference_entries", "linked_paper_id"),
        "audit_logs.paper_id_set_null": ("audit_logs", "paper_id"),
        "parse_jobs.paper_id_set_null": ("parse_jobs", "paper_id"),
    }
    for key, (table_name, column_name) in optional_targets.items():
        table = Base.metadata.tables.get(table_name)
        if table is not None and table_name in existing_table_names:
            counts[key] = count_optional_column(session, table, column_name, paper.id)
    return counts


def external_counts_for_paper(session: Any, paper_id: Any) -> tuple[int, int]:
    run_count = int(
        session.execute(
            sa.select(sa.func.count()).select_from(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper_id)
        ).scalar()
        or 0
    )
    candidate_count = int(
        session.execute(
            sa.select(sa.func.count())
            .select_from(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
        ).scalar()
        or 0
    )
    return run_count, candidate_count


def review_count_for_paper(session: Any, existing_table_names: set[str], paper_id: Any) -> int:
    table = Base.metadata.tables.get("extraction_field_reviews")
    if table is None or table.name not in existing_table_names:
        return 0
    return count_table_for_paper(session, table, paper_id)


def risk_level(
    classification: str,
    external_run_count: int,
    external_candidate_count: int,
    review_record_count: int,
    existing_file_count: int,
) -> str:
    if classification == "protected":
        return "protected"
    if classification == "manual_review":
        return "manual_review"
    if review_record_count:
        return "high"
    if external_run_count or external_candidate_count:
        return "medium"
    if existing_file_count == 0:
        return "medium"
    return "low"


def paper_summary(
    session: Any,
    existing_table_names: set[str],
    paper: Paper,
    settings: Settings,
) -> tuple[dict[str, Any], list[Path]]:
    classification, reason = classify_library(paper.library_name)
    direct_paths, artifact_entries, existing_files = collect_paper_artifacts(session, paper, settings)
    external_run_count, external_candidate_count = external_counts_for_paper(session, paper.id)
    review_record_count = review_count_for_paper(session, existing_table_names, paper.id)
    db_counts = count_db_records_for_paper(session, existing_table_names, paper)
    will_delete = classification == "candidate_delete"
    summary = {
        "paper_id": str(paper.id),
        "title": paper.title,
        "doi": paper.doi,
        "library_name": paper.library_name,
        "created_at": paper.created_at.isoformat() if paper.created_at else None,
        "pdf_path": direct_paths.get("pdf_path"),
        "markdown_path": direct_paths.get("markdown_path"),
        "docling_json_path": direct_paths.get("docling_json_path"),
        "grobid_tei_path": direct_paths.get("grobid_tei_path"),
        "workspace_path": direct_paths.get("workspace_path"),
        "artifact_paths": artifact_entries,
        "external_analysis_run_count": external_run_count,
        "external_analysis_candidate_count": external_candidate_count,
        "review_record_count": review_record_count,
        "will_delete_db_records": db_counts if will_delete else {},
        "will_delete_storage_files": [str(path) for path in existing_files] if will_delete else [],
        "risk_level": risk_level(
            classification,
            external_run_count,
            external_candidate_count,
            review_record_count,
            len(existing_files),
        ),
        "reason": reason,
        "classification": classification,
    }
    return summary, existing_files


def group_by_classification(papers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "candidate_delete": [],
        "protected": [],
        "manual_review": [],
    }
    for paper in papers:
        grouped.setdefault(str(paper["classification"]), []).append(paper)
    return grouped


def library_names(items: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("library_name") or "") for item in items})


def table_rows_for_libraries(libraries: list[str], counts: Counter[str]) -> str:
    if not libraries:
        return "_None._\n"
    lines = ["| library_name | papers |", "|---|---:|"]
    for library_name in libraries:
        lines.append(f"| {library_name} | {counts.get(library_name, 0)} |")
    return "\n".join(lines) + "\n"


def render_markdown(plan: dict[str, Any]) -> str:
    if plan["status"] == STATUS_FAIL:
        return (
            "# Internal Library Cleanup Plan\n\n"
            "INTERNAL_LIBRARY_CLEANUP_PLAN=FAIL\n"
            f"root_cause={plan.get('root_cause', 'unknown')}\n"
        )

    stats = plan["stats"]
    candidate_libraries = plan["candidate_delete_libraries"]
    protected_libraries = plan["protected_libraries"]
    manual_review_libraries = plan["manual_review_libraries"]
    library_counts = Counter(plan.get("library_counts", {}))
    lines = [
        "# Internal Library Cleanup Plan",
        "",
        "INTERNAL_LIBRARY_CLEANUP_PLAN=READY_FOR_USER_CONFIRMATION",
        "",
        "## Summary",
        "",
        f"- total_papers: {stats['total_papers']}",
        f"- candidate_delete_papers: {stats['candidate_delete_papers']}",
        f"- candidate_delete_libraries: {stats['candidate_delete_libraries']}",
        f"- manual_review_papers: {stats['manual_review_papers']}",
        f"- protected_papers: {stats['protected_papers']}",
        f"- protected_libraries: {stats['protected_libraries']}",
        f"- storage_files_to_delete_count: {stats['storage_files_to_delete_count']}",
        f"- storage_bytes_to_delete: {stats['storage_bytes_to_delete']}",
        f"- related_external_analysis_records: {stats['related_external_analysis_records']}",
        f"- related_review_records: {stats['related_review_records']}",
        "",
        "## Backup Required Before Delete",
        "",
        "1. Export target DB records to JSON.",
        "2. Export target paper_id list.",
        "3. Export target artifact path list.",
        "4. Optionally run pg_dump before any delete operation.",
        "",
        f"- backup_manifest: {plan['outputs']['backup_manifest']}",
        f"- paper_ids_output: {plan['outputs']['paper_ids_output']}",
        f"- artifact_paths_output: {plan['outputs']['artifact_paths_output']}",
        "",
        "## Candidate Delete Libraries",
        "",
        table_rows_for_libraries(candidate_libraries, library_counts),
        "## Protected Libraries",
        "",
        table_rows_for_libraries(protected_libraries, library_counts),
        "## Manual Review Libraries",
        "",
        table_rows_for_libraries(manual_review_libraries, library_counts),
        "## Candidate Papers",
        "",
    ]
    candidate_papers = plan["candidate_delete_papers"]
    if not candidate_papers:
        lines.append("_None._")
    else:
        lines.extend(
            [
                "| paper_id | library_name | external_runs | candidates | reviews | artifact_files | risk | reason |",
                "|---|---|---:|---:|---:|---:|---|---|",
            ]
        )
        for item in candidate_papers:
            lines.append(
                "| {paper_id} | {library_name} | {runs} | {candidates} | {reviews} | {files} | {risk} | {reason} |".format(
                    paper_id=item["paper_id"],
                    library_name=item["library_name"],
                    runs=item["external_analysis_run_count"],
                    candidates=item["external_analysis_candidate_count"],
                    reviews=item["review_record_count"],
                    files=len(item["will_delete_storage_files"]),
                    risk=item["risk_level"],
                    reason=item["reason"],
                )
            )
    lines.extend(
        [
            "",
            "## Safety Notes",
            "",
            "- This report is a dry-run plan only.",
            "- No DELETE SQL was executed.",
            "- No storage artifact files were removed.",
            "- No git staging or commit was performed by this script.",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def write_failure_outputs(args: argparse.Namespace, root_cause: str, detail: str) -> None:
    output_path = Path(args.output)
    markdown_path = Path(args.markdown)
    payload = {
        "status": STATUS_FAIL,
        "root_cause": root_cause,
        "detail": detail,
        "generated_at": now_iso(),
    }
    write_json(output_path, payload)
    write_text(markdown_path, render_markdown(payload))


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    api_info, api_error = fetch_api_db_info(args.api_base)
    runtime_storage_root = apply_runtime_storage_root(settings, api_info)
    engine = get_engine(settings.database_url)
    inspector = inspect(engine)
    existing_table_names = set(inspector.get_table_names())
    dialect = engine.dialect.name

    with session_scope(settings.database_url) as session:
        papers = list(
            session.execute(
                sa.select(Paper).order_by(Paper.library_name.asc(), Paper.created_at.asc(), Paper.id.asc())
            ).scalars()
        )
        all_papers: list[dict[str, Any]] = []
        candidate_existing_files: dict[str, Path] = {}
        for paper in papers:
            summary, existing_files = paper_summary(session, existing_table_names, paper, settings)
            all_papers.append(summary)
            if summary["classification"] == "candidate_delete":
                for file_path in existing_files:
                    candidate_existing_files[str(file_path)] = file_path

        grouped = group_by_classification(all_papers)
        candidate_papers = grouped.get("candidate_delete", [])
        protected_papers = grouped.get("protected", [])
        manual_review_papers = grouped.get("manual_review", [])
        library_counts = Counter(str(item.get("library_name") or "") for item in all_papers)
        candidate_libraries = library_names(candidate_papers)
        protected_libraries = library_names(protected_papers)
        manual_review_libraries = library_names(manual_review_papers)
        storage_bytes = sum(file_size(path) for path in candidate_existing_files.values())
        external_runs = sum(int(item["external_analysis_run_count"]) for item in candidate_papers)
        external_candidates = sum(int(item["external_analysis_candidate_count"]) for item in candidate_papers)
        review_records = sum(int(item["review_record_count"]) for item in candidate_papers)

        workflow_jobs_table = Base.metadata.tables.get("workflow_jobs")
        workflow_jobs_by_candidate_library: dict[str, int] = {}
        if workflow_jobs_table is not None and workflow_jobs_table.name in existing_table_names:
            for library_name in candidate_libraries:
                count = session.execute(
                    sa.select(sa.func.count())
                    .select_from(workflow_jobs_table)
                    .where(workflow_jobs_table.c.library_name == library_name)
                ).scalar()
                workflow_jobs_by_candidate_library[library_name] = int(count or 0)

    stats = {
        "total_papers": len(all_papers),
        "candidate_delete_papers": len(candidate_papers),
        "candidate_delete_libraries": len(candidate_libraries),
        "manual_review_papers": len(manual_review_papers),
        "protected_papers": len(protected_papers),
        "protected_libraries": len(protected_libraries),
        "storage_files_to_delete_count": len(candidate_existing_files),
        "storage_bytes_to_delete": storage_bytes,
        "related_external_analysis_records": external_runs + external_candidates,
        "related_external_analysis_runs": external_runs,
        "related_external_analysis_candidates": external_candidates,
        "related_review_records": review_records,
    }
    plan = {
        "status": STATUS_READY,
        "generated_at": now_iso(),
        "api_base": args.api_base,
        "api_db_info": api_info,
        "api_db_info_error": api_error,
        "database": {
            "dialect": dialect,
            "database_url_masked": mask_database_url(settings.database_url),
            "configured_storage_root": str(get_settings().storage_root),
            "runtime_storage_root": runtime_storage_root or str(settings.storage_root),
        },
        "rules": {
            "delete_prefixes": list(DELETE_PREFIXES),
            "explicit_internal_library_names": sorted(EXPLICIT_INTERNAL_LIBRARY_NAMES),
            "explicit_internal_library_prefixes": list(EXPLICIT_INTERNAL_LIBRARY_PREFIXES),
            "protected_library_names": sorted(PROTECTED_LIBRARY_NAMES),
            "manual_review_tokens": list(SUSPICIOUS_REVIEW_TOKENS),
        },
        "backup_plan": {
            "required_before_delete": True,
            "steps": [
                "Export target DB records to JSON.",
                "Export target paper_id list.",
                "Export target artifact path list.",
                "Optionally run pg_dump before any delete operation.",
            ],
            "note": "This script writes dry-run backup inputs only; it does not delete DB records or files.",
        },
        "outputs": {
            "plan_json": str(Path(args.output)),
            "plan_markdown": str(Path(args.markdown)),
            "backup_manifest": str(Path(args.backup_manifest)),
            "paper_ids_output": str(Path(args.paper_ids_output)),
            "artifact_paths_output": str(Path(args.artifact_paths_output)),
        },
        "stats": stats,
        "library_counts": dict(sorted(library_counts.items())),
        "candidate_delete_libraries": candidate_libraries,
        "protected_libraries": protected_libraries,
        "manual_review_libraries": manual_review_libraries,
        "candidate_delete_papers": candidate_papers,
        "manual_review_papers": manual_review_papers,
        "protected_papers": protected_papers,
        "workflow_jobs_by_candidate_library": workflow_jobs_by_candidate_library,
        "artifact_files_to_delete": sorted(candidate_existing_files.keys()),
        "dry_run_safety": {
            "db_modified": False,
            "files_deleted": False,
            "delete_sql_executed": False,
        },
    }
    return plan


def build_backup_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "DRY_RUN_BACKUP_MANIFEST",
        "generated_at": now_iso(),
        "must_backup_before_delete": True,
        "paper_ids": [item["paper_id"] for item in plan["candidate_delete_papers"]],
        "candidate_delete_libraries": plan["candidate_delete_libraries"],
        "artifact_paths": plan["artifact_files_to_delete"],
        "db_target_records": [
            {
                "paper_id": item["paper_id"],
                "library_name": item["library_name"],
                "title": item["title"],
                "doi": item["doi"],
                "created_at": item["created_at"],
                "will_delete_db_records": item["will_delete_db_records"],
                "external_analysis_run_count": item["external_analysis_run_count"],
                "external_analysis_candidate_count": item["external_analysis_candidate_count"],
                "review_record_count": item["review_record_count"],
            }
            for item in plan["candidate_delete_papers"]
        ],
        "optional_pg_dump_note": (
            "Before confirmed cleanup, run pg_dump against the active PostgreSQL database "
            "or export the target tables filtered by paper_id/library_name."
        ),
    }


def main() -> int:
    args = parse_args()
    try:
        plan = build_plan(args)
    except SQLAlchemyError as exc:
        write_failure_outputs(args, "database_unreachable", f"{type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        write_failure_outputs(args, "unknown", f"{type(exc).__name__}: {exc}")
        return 1

    output_path = Path(args.output)
    markdown_path = Path(args.markdown)
    backup_manifest_path = Path(args.backup_manifest)
    paper_ids_path = Path(args.paper_ids_output)
    artifact_paths_path = Path(args.artifact_paths_output)

    write_json(output_path, plan)
    write_text(markdown_path, render_markdown(plan))
    write_json(backup_manifest_path, build_backup_manifest(plan))
    write_text(paper_ids_path, "\n".join(item["paper_id"] for item in plan["candidate_delete_papers"]) + "\n")
    write_text(artifact_paths_path, "\n".join(plan["artifact_files_to_delete"]) + "\n")

    print(f"INTERNAL_LIBRARY_CLEANUP_PLAN={plan['status']}")
    print(f"candidate_delete_papers={plan['stats']['candidate_delete_papers']}")
    print(f"candidate_delete_libraries={plan['stats']['candidate_delete_libraries']}")
    print(f"protected_papers={plan['stats']['protected_papers']}")
    print(f"manual_review_papers={plan['stats']['manual_review_papers']}")
    print(f"storage_files_to_delete_count={plan['stats']['storage_files_to_delete_count']}")
    print(f"storage_bytes_to_delete={plan['stats']['storage_bytes_to_delete']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
