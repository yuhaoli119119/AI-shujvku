from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings
from app.db.session import get_engine
from app.services.project_library_bundle_service import ProjectLibraryBundleService
from app.utils.active_database import get_registered_active_library_info


DEFAULT_TASKS = (
    "adsorption_energy",
    "li2s_reaction_energy",
    "li2s_barrier",
    "rds_srr_multitask",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only dry-run report for Li-S SAC/DAC project-library v4 exports."
    )
    parser.add_argument("--context-key", default="li_s_sac_dac")
    parser.add_argument("--library-name", default=None)
    parser.add_argument("--task", action="append", choices=DEFAULT_TASKS)
    parser.add_argument("--example-limit", type=int, default=8)
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help=(
            "Backend API base URL. Defaults to the local SSH tunnel for the server backend. "
            "Use --local-db to bypass the API and read LITAI_DATABASE_URL directly."
        ),
    )
    parser.add_argument(
        "--local-db",
        action="store_true",
        help="Read LITAI_DATABASE_URL directly. Use only inside the server/backend container or an explicit test DB.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "exports" / "project_library_v4_dry_run.json",
        help="JSON report path. Defaults to literature-ai/outputs/exports/project_library_v4_dry_run.json.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=10,
        help="Database connection timeout in seconds. Defaults to 10.",
    )
    return parser.parse_args()


def _normalize_api_base_url(value: str) -> str:
    return str(value or "http://127.0.0.1:8000").rstrip("/")


def _api_export_url(
    *,
    api_base_url: str,
    context_key: str,
    library_name: str | None,
    task: str,
    ready_only: bool,
) -> str:
    params = {
        "context_key": context_key,
        "task": task,
        "ready_only": str(bool(ready_only)).lower(),
    }
    if library_name:
        params["library_name"] = library_name
    return (
        f"{_normalize_api_base_url(api_base_url)}/api/dft/project-library-ml-export-v4?"
        + urllib.parse.urlencode(params)
    )


def _fetch_json(url: str, *, timeout: int) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=max(1, int(timeout or 10))) as response:
        text = response.read().decode("utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return payload


def _mask_database_url(url: str) -> str:
    if "@" not in url:
        return url
    prefix, suffix = url.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "postgresql"
    return f"{scheme}://***:***@{suffix}"


def _database_url_with_timeout(url: str, seconds: int) -> str:
    timeout = max(1, int(seconds or 10))
    parsed = make_url(url)
    query = dict(parsed.query)
    query.setdefault("connect_timeout", str(timeout))
    return str(parsed.set(query=query))


def _active_database_metadata(database_url: str) -> dict[str, Any]:
    registered = get_registered_active_library_info()
    db_kind = "postgresql" if database_url.startswith("postgresql") else "unsupported"
    return {
        "db_kind": db_kind,
        "db_url_masked": _mask_database_url(database_url),
        "active_library": registered.get("active_library"),
        "active_library_root": registered.get("active_library_root"),
        "connection_checked_by": "project_library_v4_dry_run",
    }


def _record_example(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record.get("record_id"),
        "paper_id": record.get("paper_id"),
        "title": record.get("title"),
        "task": record.get("task"),
        "label_name": record.get("label_name"),
        "label_value": record.get("label_value"),
        "label_unit": record.get("label_unit"),
        "label_energy_kind": record.get("label_energy_kind"),
        "catalyst_sample_id": record.get("catalyst_sample_id"),
        "catalyst_name": record.get("catalyst_name"),
        "adsorbate": record.get("adsorbate"),
        "reaction_step": record.get("reaction_step"),
        "ml_ready": record.get("ml_ready"),
        "blockers": record.get("blockers") or [],
        "descriptor_blockers": record.get("descriptor_blockers") or [],
        "structure_blockers": record.get("structure_blockers") or [],
        "database_write_authority": record.get("database_write_authority"),
        "ai_consensus_auto_adopt_allowed": record.get("ai_consensus_auto_adopt_allowed"),
    }


def _task_report(
    service: ProjectLibraryBundleService,
    *,
    context_key: str,
    library_name: str | None,
    task: str,
    example_limit: int,
) -> dict[str, Any]:
    ready_payload = service.build_ml_export_v4(
        context_key=context_key,
        task=task,
        library_name=library_name,
        ready_only=True,
    )
    diagnostic_payload = service.build_ml_export_v4(
        context_key=context_key,
        task=task,
        library_name=library_name,
        ready_only=False,
    )
    diagnostic_records = diagnostic_payload.get("records") or []
    blocked_records = [record for record in diagnostic_records if not record.get("ml_ready")]
    blocker_counts = Counter()
    descriptor_blocker_counts = Counter()
    structure_blocker_counts = Counter()
    for record in diagnostic_records:
        blocker_counts.update(record.get("blockers") or [])
        descriptor_blocker_counts.update(record.get("descriptor_blockers") or [])
        structure_blocker_counts.update(record.get("structure_blockers") or [])

    return {
        "task": task,
        "ready_manifest": ready_payload.get("manifest") or {},
        "diagnostic_manifest": diagnostic_payload.get("manifest") or {},
        "ready_record_count": len(ready_payload.get("records") or []),
        "diagnostic_record_count": len(diagnostic_records),
        "blocked_record_count": len(blocked_records),
        "blocker_counts_from_records": dict(sorted(blocker_counts.items())),
        "descriptor_blocker_counts_from_records": dict(sorted(descriptor_blocker_counts.items())),
        "structure_blocker_counts_from_records": dict(sorted(structure_blocker_counts.items())),
        "blocked_examples": [_record_example(record) for record in blocked_records[:example_limit]],
        "ready_examples": [_record_example(record) for record in (ready_payload.get("records") or [])[:example_limit]],
    }


def _task_report_from_api(
    *,
    api_base_url: str,
    context_key: str,
    library_name: str | None,
    task: str,
    example_limit: int,
    connect_timeout: int,
) -> dict[str, Any]:
    ready_payload = _fetch_json(
        _api_export_url(
            api_base_url=api_base_url,
            context_key=context_key,
            library_name=library_name,
            task=task,
            ready_only=True,
        ),
        timeout=connect_timeout,
    )
    diagnostic_payload = _fetch_json(
        _api_export_url(
            api_base_url=api_base_url,
            context_key=context_key,
            library_name=library_name,
            task=task,
            ready_only=False,
        ),
        timeout=connect_timeout,
    )
    diagnostic_records = diagnostic_payload.get("records") or []
    blocked_records = [record for record in diagnostic_records if not record.get("ml_ready")]
    blocker_counts = Counter()
    descriptor_blocker_counts = Counter()
    structure_blocker_counts = Counter()
    for record in diagnostic_records:
        blocker_counts.update(record.get("blockers") or [])
        descriptor_blocker_counts.update(record.get("descriptor_blockers") or [])
        structure_blocker_counts.update(record.get("structure_blockers") or [])

    return {
        "task": task,
        "ready_manifest": ready_payload.get("manifest") or {},
        "diagnostic_manifest": diagnostic_payload.get("manifest") or {},
        "ready_record_count": len(ready_payload.get("records") or []),
        "diagnostic_record_count": len(diagnostic_records),
        "blocked_record_count": len(blocked_records),
        "blocker_counts_from_records": dict(sorted(blocker_counts.items())),
        "descriptor_blocker_counts_from_records": dict(sorted(descriptor_blocker_counts.items())),
        "structure_blocker_counts_from_records": dict(sorted(structure_blocker_counts.items())),
        "blocked_examples": [_record_example(record) for record in blocked_records[:example_limit]],
        "ready_examples": [_record_example(record) for record in (ready_payload.get("records") or [])[:example_limit]],
    }


def build_api_report(
    *,
    api_base_url: str,
    context_key: str,
    library_name: str | None,
    tasks: tuple[str, ...],
    example_limit: int,
    connect_timeout: int = 10,
) -> dict[str, Any]:
    normalized_base_url = _normalize_api_base_url(api_base_url)
    task_reports = [
        _task_report_from_api(
            api_base_url=normalized_base_url,
            context_key=context_key,
            library_name=library_name,
            task=task,
            example_limit=example_limit,
            connect_timeout=connect_timeout,
        )
        for task in tasks
    ]
    return {
        "schema_version": "project_library_v4_dry_run_report_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "database_write_authority": "none",
        "submit_endpoint_called": False,
        "extraction_apply_called": False,
        "execution_mode": "server_api",
        "api_base_url": normalized_base_url,
        "active_database": {
            "db_kind": "postgresql",
            "db_url_masked": "server_backend_api",
            "active_library": None,
            "active_library_root": None,
            "connection_checked_by": "project_library_v4_dry_run_api",
        },
        "context_key": context_key,
        "library_name": library_name,
        "tasks": task_reports,
    }


def build_report(
    *,
    context_key: str,
    library_name: str | None,
    tasks: tuple[str, ...],
    example_limit: int,
    connect_timeout: int = 10,
) -> dict[str, Any]:
    settings = get_settings()
    database_url = _database_url_with_timeout(settings.database_url, connect_timeout)
    engine = get_engine(database_url)
    with Session(engine, future=True) as session:
        service = ProjectLibraryBundleService(session)
        task_reports = [
            _task_report(
                service,
                context_key=context_key,
                library_name=library_name,
                task=task,
                example_limit=example_limit,
            )
            for task in tasks
        ]
        session.rollback()

    return {
        "schema_version": "project_library_v4_dry_run_report_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "database_write_authority": "none",
        "submit_endpoint_called": False,
        "extraction_apply_called": False,
        "execution_mode": "local_db",
        "active_database": _active_database_metadata(settings.database_url),
        "database_url_masked": _mask_database_url(settings.database_url),
        "context_key": context_key,
        "library_name": library_name,
        "tasks": task_reports,
    }


def build_failure_report(
    *,
    context_key: str,
    library_name: str | None,
    tasks: tuple[str, ...],
    connect_timeout: int,
    error: BaseException,
    execution_mode: str = "local_db",
    api_base_url: str | None = None,
) -> dict[str, Any]:
    status = "server_api_unavailable" if execution_mode == "server_api" else "database_unavailable"
    settings = None if execution_mode == "server_api" else get_settings()
    active_database = (
        {
            "db_kind": "postgresql",
            "db_url_masked": "server_backend_api",
            "active_library": None,
            "active_library_root": None,
            "connection_checked_by": "project_library_v4_dry_run_api",
        }
        if execution_mode == "server_api"
        else _active_database_metadata(settings.database_url)
    )
    database_url_masked = (
        "server_backend_api"
        if execution_mode == "server_api"
        else _mask_database_url(settings.database_url)
    )
    return {
        "schema_version": "project_library_v4_dry_run_report_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "database_write_authority": "none",
        "submit_endpoint_called": False,
        "extraction_apply_called": False,
        "status": status,
        "execution_mode": execution_mode,
        "api_base_url": _normalize_api_base_url(api_base_url) if api_base_url else None,
        "active_database": active_database,
        "database_url_masked": database_url_masked,
        "context_key": context_key,
        "library_name": library_name,
        "tasks_requested": list(tasks),
        "tasks": [],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "connect_timeout_seconds": max(1, int(connect_timeout or 10)),
        },
    }


def main() -> int:
    args = parse_args()
    tasks = tuple(args.task or DEFAULT_TASKS)
    exit_code = 0
    try:
        if args.local_db:
            report = build_report(
                context_key=args.context_key,
                library_name=args.library_name,
                tasks=tasks,
                example_limit=max(0, args.example_limit),
                connect_timeout=args.connect_timeout,
            )
        else:
            report = build_api_report(
                api_base_url=args.api_base_url,
                context_key=args.context_key,
                library_name=args.library_name,
                tasks=tasks,
                example_limit=max(0, args.example_limit),
                connect_timeout=args.connect_timeout,
            )
    except (SQLAlchemyError, TimeoutError, OSError, ValueError, urllib.error.URLError) as exc:
        report = build_failure_report(
            context_key=args.context_key,
            library_name=args.library_name,
            tasks=tasks,
            connect_timeout=args.connect_timeout,
            error=exc,
            execution_mode="local_db" if args.local_db else "server_api",
            api_base_url=args.api_base_url if not args.local_db else None,
        )
        exit_code = 2
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(payload)
    print(f"\nWrote read-only dry-run report to {args.output}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
