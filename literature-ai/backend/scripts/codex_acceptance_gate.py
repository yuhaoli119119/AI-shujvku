from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import make_url


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.db.session import session_scope
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import get_review_coverage
from scripts import diagnose_live_artifact_mismatch as live_diag


WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
ALLOWED_ROOT_CAUSES = {
    "api_server_not_reloaded_or_running_old_code",
    "storage_root_mismatch_between_smoke_and_api",
    "paper_id_mismatch",
    "artifact_refs_not_persisted_to_active_postgres",
    "artifact_files_not_present_in_api_storage",
    "api_artifact_status_uses_different_code_path",
    "external_audit_not_visible_in_review_center",
    "legacy_sqlite_used_as_runtime_source",
    "unknown",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex-only read-only acceptance gate for real-paper artifact parity.")
    parser.add_argument("--paper-ids", required=True, help="Comma-separated paper IDs to check.")
    parser.add_argument("--library-name", required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    return parser.parse_args()


def split_paper_ids(raw: str) -> list[str]:
    return [str(UUID(item.strip())) for item in raw.split(",") if item.strip()]


def json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    return value


def first_existing_smoke_report() -> Path | None:
    candidates = [
        BACKEND_ROOT / "reports" / "realpaper_chain_smoke.json",
        BACKEND_ROOT / "reports" / "fresh_real_paper_smoke_20260608.json",
        BACKEND_ROOT.parent.parent / "reports" / "realpaper_chain_smoke.json",
    ]
    for path in candidates:
        if path.exists():
            return path

    report_dirs = [BACKEND_ROOT / "reports", BACKEND_ROOT.parent.parent / "reports"]
    matches: list[Path] = []
    for directory in report_dirs:
        if directory.exists():
            matches.extend(directory.glob("*real*paper*smoke*.json"))
            matches.extend(directory.glob("*realpaper*smoke*.json"))
    return max(matches, key=lambda item: item.stat().st_mtime) if matches else None


def smoke_report_payload(expected_ids: list[str]) -> dict[str, Any]:
    path = first_existing_smoke_report()
    if path is None:
        return {
            "ok": False,
            "path": None,
            "selected_paper_ids": [],
            "expected_paper_ids": expected_ids,
            "paper_ids_match": False,
            "error": "real_pdf_smoke_report_not_found",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "path": str(path),
            "selected_paper_ids": [],
            "expected_paper_ids": expected_ids,
            "paper_ids_match": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    selected = payload.get("selected_paper_ids")
    if not isinstance(selected, list):
        items = payload.get("items")
        selected = [item.get("paper_id") for item in items if isinstance(item, dict) and item.get("paper_id")] if isinstance(items, list) else []
    selected_ids = [str(item) for item in selected]
    return {
        "ok": True,
        "path": str(path),
        "selected_paper_ids": selected_ids,
        "expected_paper_ids": expected_ids,
        "paper_ids_match": selected_ids == expected_ids,
        "status": payload.get("status"),
        "sample_type": payload.get("sample_type"),
        "selected_count": payload.get("selected_count") or len(selected_ids),
    }


def mask_runtime_for_comparison(local: dict[str, Any], api_debug: dict[str, Any]) -> dict[str, Any]:
    api_json = api_debug.get("json") if api_debug.get("ok") else {}
    fields = [
        "git_commit",
        "cwd",
        "database_url_masked",
        "database_dialect",
        "storage_root",
        "storage_root_exists",
        "artifact_status_module_path",
    ]
    return {
        field: {
            "local": local.get(field),
            "api": api_json.get(field) if isinstance(api_json, dict) else None,
            "same": local.get(field) == api_json.get(field) if isinstance(api_json, dict) else False,
        }
        for field in fields
    }


def legacy_sqlite_guard(settings, api_runtime_debug: dict[str, Any]) -> dict[str, Any]:
    try:
        local_dialect = make_url(settings.database_url).drivername
    except Exception:
        local_dialect = settings.database_url.split(":", 1)[0]
    api_json = api_runtime_debug.get("json") if api_runtime_debug.get("ok") else {}
    api_dialect = api_json.get("database_dialect") if isinstance(api_json, dict) else None
    legacy_ignored = bool(api_json.get("legacy_sqlite_ignored")) if isinstance(api_json, dict) else False
    legacy_found = api_json.get("legacy_sqlite_paths_found") if isinstance(api_json, dict) else None
    return {
        "local_database_dialect": local_dialect,
        "api_database_dialect": api_dialect,
        "local_is_postgresql": local_dialect.startswith("postgresql"),
        "api_is_postgresql": str(api_dialect or "").startswith("postgresql"),
        "legacy_sqlite_ignored": legacy_ignored,
        "legacy_sqlite_paths_found": legacy_found or [],
        "legacy_sqlite_used_as_runtime_source": not local_dialect.startswith("postgresql")
        or not str(api_dialect or "").startswith("postgresql")
        or not legacy_ignored,
    }


def database_connectivity_guard(settings) -> dict[str, Any]:
    try:
        url = make_url(settings.database_url)
    except Exception as exc:
        return {"ok": False, "error": f"database_url_parse_failed: {type(exc).__name__}: {exc}"}
    if not url.drivername.startswith("postgresql"):
        return {"ok": False, "drivername": url.drivername, "error": "active_database_is_not_postgresql"}
    host = url.host or "localhost"
    port = int(url.port or 5432)
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        return {
            "ok": False,
            "drivername": url.drivername,
            "host": host,
            "port": port,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"ok": True, "drivername": url.drivername, "host": host, "port": port}


def unavailable_live_report(
    *,
    args: argparse.Namespace,
    paper_ids: list[str],
    settings,
    error: str,
) -> dict[str, Any]:
    try:
        local_runtime = live_diag.runtime_info(settings)
    except Exception as exc:
        local_runtime = {"error": f"{type(exc).__name__}: {exc}"}
    runtime_debug = live_diag.http_get_json(args.api_base, "/api/system/runtime-debug", timeout=10)
    db_info = live_diag.http_get_json(args.api_base, "/api/system/db-info", timeout=10)
    return {
        "schema_version": "live_artifact_mismatch_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_name": args.library_name,
        "paper_ids": paper_ids,
        "api_base": args.api_base,
        "smoke_report_selected_paper_ids": live_diag.selected_ids_from_smoke_report(),
        "local_runtime": local_runtime,
        "api_runtime_debug": runtime_debug,
        "api_db_info": db_info,
        "api_review_center_summary": {"ok": False, "error": error},
        "items": [
            {
                "paper_id": pid,
                "root_cause": "live_diagnosis_unavailable",
                "error": error,
                "parity": {
                    "local_ready": False,
                    "api_get_paper_ready": False,
                    "api_get_codex_context_ready": False,
                    "api_review_center_ready": False,
                },
            }
            for pid in paper_ids
        ],
        "root_cause_counts": {"live_diagnosis_unavailable": len(paper_ids)},
    }


def mcp_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="codex_acceptance_gate",
        display_name="Codex Acceptance Gate",
        capabilities=frozenset({"read_papers"}),
        raw_key="codex_acceptance_gate_readonly",
    )


def db_external_audit_payload(paper_ids: list[str], library_name: str) -> dict[str, Any]:
    settings = get_settings()
    ids = [UUID(pid) for pid in paper_ids]
    by_paper: dict[str, dict[str, Any]] = {
        pid: {
            "paper_exists": False,
            "library_name": None,
            "external_audit_candidate_count": 0,
            "external_audit_source_distribution": {},
            "external_audit_verification_status_distribution": {},
            "verified_like_external_audit_count": 0,
        }
        for pid in paper_ids
    }
    try:
        with session_scope(settings.database_url) as session:
            papers = session.scalars(select(Paper).where(Paper.id.in_(ids))).all()
            for paper in papers:
                row = by_paper[str(paper.id)]
                row["paper_exists"] = True
                row["title"] = paper.title
                row["library_name"] = paper.library_name
                row["library_name_matches"] = paper.library_name == library_name

            rows = session.execute(
                select(ExternalAnalysisCandidate, ExternalAnalysisRun)
                .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
                .where(
                    ExternalAnalysisCandidate.paper_id.in_(ids),
                    ExternalAnalysisCandidate.candidate_type == "external_audit_opinion",
                )
            ).all()
            source_counts: dict[str, Counter[str]] = {pid: Counter() for pid in paper_ids}
            status_counts: dict[str, Counter[str]] = {pid: Counter() for pid in paper_ids}
            verified_like_counts: Counter[str] = Counter()
            for candidate, run in rows:
                pid = str(candidate.paper_id)
                payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
                evidence = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
                verification_status = str(
                    payload.get("verification_status")
                    or evidence.get("verification_status")
                    or candidate.status
                    or "unverified"
                ).lower()
                source_counts[pid][run.source] += 1
                status_counts[pid][verification_status] += 1
                if verification_status in {"verified", "safe_verified"}:
                    verified_like_counts[pid] += 1
            for pid in paper_ids:
                row = by_paper[pid]
                row["external_audit_source_distribution"] = dict(source_counts[pid])
                row["external_audit_verification_status_distribution"] = dict(status_counts[pid])
                row["external_audit_candidate_count"] = sum(source_counts[pid].values())
                row["verified_like_external_audit_count"] = verified_like_counts[pid]
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "by_paper": by_paper}

    return {"ok": True, "by_paper": by_paper}


def mcp_review_coverage_payload(paper_ids: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "by_paper": {}}
    with mcp_auth_context(mcp_auth()):
        for pid in paper_ids:
            try:
                coverage = get_review_coverage(pid)
            except Exception as exc:
                result["ok"] = False
                result["by_paper"][pid] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                continue
            latest = coverage.get("latest_external_audits") or []
            result["by_paper"][pid] = {
                "ok": True,
                "artifact_status": coverage.get("artifact_status"),
                "external_audit_count": coverage.get("external_audit_count"),
                "external_audit_source_distribution": coverage.get("external_audit_source_distribution"),
                "latest_external_audits": latest,
                "all_external_audits_unverified": all(
                    str(item.get("verification_status") or "unverified").lower() not in {"verified", "safe_verified"}
                    and item.get("writes_final_truth") is False
                    and item.get("requires_human_confirmation") is True
                    for item in latest
                ),
            }
    return result


def contains_windows_absolute(value: Any) -> bool:
    if isinstance(value, str):
        return bool(WINDOWS_ABSOLUTE_RE.search(value))
    if isinstance(value, dict):
        return any(contains_windows_absolute(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return any(contains_windows_absolute(item) for item in value)
    return False


def public_path_payloads_are_safe(live_report: dict[str, Any], mcp_payload: dict[str, Any]) -> dict[str, Any]:
    unsafe: dict[str, list[str]] = {}
    for item in live_report.get("items", []):
        pid = item.get("paper_id")
        public_payload = {
            "api_get_paper": (item.get("api") or {}).get("api_get_paper"),
            "api_get_codex_context": (item.get("api") or {}).get("api_get_codex_context"),
            "api_review_center": (item.get("api") or {}).get("api_review_center"),
            "mcp_review_coverage": (mcp_payload.get("by_paper") or {}).get(pid),
        }
        for surface, payload in public_payload.items():
            if contains_windows_absolute(payload):
                unsafe.setdefault(pid, []).append(surface)
    return {
        "windows_absolute_paths_exposed": unsafe,
        "no_windows_absolute_paths_exposed": not unsafe,
        "external_ai_not_required_to_resolve_storage_paths": all(
            ((item.get("api") or {}).get("api_get_paper") or {}).get("artifact_status", {}).get("pdf_path_kind")
            in {"storage_relative", "external_source"}
            for item in live_report.get("items", [])
        ),
    }


def real_paper_parity_payload(live_report: dict[str, Any]) -> dict[str, Any]:
    by_paper: dict[str, Any] = {}
    all_ready = True
    for item in live_report.get("items", []):
        pid = item.get("paper_id")
        local = item.get("local_resolver") or {}
        api = item.get("api") or {}
        detail_status = (api.get("api_get_paper") or {}).get("artifact_status") or {}
        context_status = (api.get("api_get_codex_context") or {}).get("artifact_status") or {}
        review_status = (api.get("api_review_center") or {}).get("artifact_status") or {}
        checks = {
            "local_ready": local.get("artifact_ready_for_external_audit") is True,
            "api_get_paper_ready": detail_status.get("artifact_ready_for_external_audit") is True,
            "api_get_codex_context_ready": context_status.get("artifact_ready_for_external_audit") is True,
            "api_review_center_ready": review_status.get("artifact_ready_for_external_audit") is True,
            "artifact_ready_for_external_audit": detail_status.get("artifact_ready_for_external_audit") is True,
            "blocking_errors_empty": detail_status.get("blocking_errors") == []
            and context_status.get("blocking_errors") == []
            and review_status.get("blocking_errors") == [],
        }
        ready = all(checks.values())
        all_ready = all_ready and ready
        by_paper[pid] = {
            "ready": ready,
            **checks,
            "blocking_errors": detail_status.get("blocking_errors"),
            "root_cause": item.get("root_cause"),
        }
    return {"all_ready": all_ready, "by_paper": by_paper}


def external_audit_visibility_payload(
    live_report: dict[str, Any],
    mcp_payload: dict[str, Any],
    db_payload: dict[str, Any],
) -> dict[str, Any]:
    by_paper: dict[str, Any] = {}
    all_visible = True
    for item in live_report.get("items", []):
        pid = item.get("paper_id")
        review_center = (item.get("api") or {}).get("api_review_center") or {}
        mcp_row = (mcp_payload.get("by_paper") or {}).get(pid) or {}
        db_row = (db_payload.get("by_paper") or {}).get(pid) or {}
        mcp_visible = bool(mcp_row.get("ok")) and int(mcp_row.get("external_audit_count") or 0) > 0
        review_center_visible = bool(review_center.get("contains_paper")) and int(review_center.get("external_audit_count") or 0) > 0
        db_visible = int(db_row.get("external_audit_candidate_count") or 0) > 0
        no_verified_write = bool(mcp_row.get("all_external_audits_unverified")) and int(
            db_row.get("verified_like_external_audit_count") or 0
        ) == 0
        visible = mcp_visible and review_center_visible and db_visible and no_verified_write
        all_visible = all_visible and visible
        by_paper[pid] = {
            "visible": visible,
            "mcp_get_review_coverage_visible": mcp_visible,
            "review_center_visible": review_center_visible,
            "postgres_external_audit_visible": db_visible,
            "does_not_auto_write_verified_or_safe_verified": no_verified_write,
            "mcp_external_audit_count": mcp_row.get("external_audit_count"),
            "review_center_external_audit_count": review_center.get("external_audit_count"),
            "postgres_external_audit_candidate_count": db_row.get("external_audit_candidate_count"),
            "external_audit_source_distribution": db_row.get("external_audit_source_distribution"),
            "external_audit_verification_status_distribution": db_row.get(
                "external_audit_verification_status_distribution"
            ),
        }
    return {"all_visible": all_visible, "by_paper": by_paper}


def map_live_root_cause(cause: str | None) -> str:
    mapping = {
        "api_service_stale_or_code_mismatch": "api_server_not_reloaded_or_running_old_code",
        "artifact_status_not_exposed_to_api": "api_server_not_reloaded_or_running_old_code",
        "api_unreachable_or_paper_detail_failed": "api_server_not_reloaded_or_running_old_code",
        "storage_root_mismatch_between_smoke_and_api": "storage_root_mismatch_between_smoke_and_api",
        "paper_id_mismatch": "paper_id_mismatch",
        "artifact_refs_not_persisted_to_active_postgres": "artifact_refs_not_persisted_to_active_postgres",
        "artifact_files_not_present_in_api_storage": "artifact_files_not_present_in_api_storage",
        "api_artifact_status_uses_different_code_path": "api_artifact_status_uses_different_code_path",
    }
    return mapping.get(cause or "", "unknown")


def classify_gate_root_cause(report: dict[str, Any]) -> str | None:
    runtime = report["runtime_guard"]
    if not runtime["database_connectivity_guard"]["ok"]:
        return "unknown"
    if runtime["legacy_sqlite_guard"]["legacy_sqlite_used_as_runtime_source"]:
        return "legacy_sqlite_used_as_runtime_source"
    if not runtime["api_runtime_debug"].get("ok") or not runtime["api_runtime_debug"].get("json", {}).get(
        "artifact_status_module_path"
    ):
        return "api_server_not_reloaded_or_running_old_code"
    if not report["smoke_report"]["paper_ids_match"]:
        return "paper_id_mismatch"
    if not report["real_paper_artifact_parity"]["all_ready"]:
        for item in report["live_diagnosis"].get("items", []):
            mapped = map_live_root_cause(item.get("root_cause"))
            if mapped != "unknown":
                return mapped
        return "unknown"
    if not report["external_audit_visibility"]["all_visible"]:
        return "external_audit_not_visible_in_review_center"
    if not report["path_safety"]["no_windows_absolute_paths_exposed"]:
        return "unknown"
    if not report["path_safety"]["external_ai_not_required_to_resolve_storage_paths"]:
        return "api_artifact_status_uses_different_code_path"
    return None


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    paper_ids = split_paper_ids(args.paper_ids)
    database_guard = database_connectivity_guard(settings)
    if database_guard["ok"]:
        try:
            live_report = live_diag.build_report(args)
        except Exception as exc:
            live_report = unavailable_live_report(
                args=args,
                paper_ids=paper_ids,
                settings=settings,
                error=f"{type(exc).__name__}: {exc}",
            )
    else:
        live_report = unavailable_live_report(
            args=args,
            paper_ids=paper_ids,
            settings=settings,
            error=database_guard.get("error") or "database_unreachable",
        )
    smoke = smoke_report_payload(paper_ids)
    if database_guard["ok"]:
        mcp_payload = mcp_review_coverage_payload(paper_ids)
        db_audit_payload = db_external_audit_payload(paper_ids, args.library_name)
    else:
        mcp_payload = {
            "ok": False,
            "error": database_guard.get("error") or "database_unreachable",
            "by_paper": {pid: {"ok": False, "error": "database_unreachable"} for pid in paper_ids},
        }
        db_audit_payload = {
            "ok": False,
            "error": database_guard.get("error") or "database_unreachable",
            "by_paper": {
                pid: {
                    "paper_exists": False,
                    "library_name": None,
                    "external_audit_candidate_count": 0,
                    "external_audit_source_distribution": {},
                    "external_audit_verification_status_distribution": {},
                    "verified_like_external_audit_count": 0,
                }
                for pid in paper_ids
            },
        }
    parity = real_paper_parity_payload(live_report)
    visibility = external_audit_visibility_payload(live_report, mcp_payload, db_audit_payload)
    path_safety = public_path_payloads_are_safe(live_report, mcp_payload)
    runtime_guard = {
        "local_runtime": live_report.get("local_runtime"),
        "api_runtime_debug": live_report.get("api_runtime_debug"),
        "api_db_info": live_report.get("api_db_info"),
        "runtime_comparison": mask_runtime_for_comparison(
            live_report.get("local_runtime") or {},
            live_report.get("api_runtime_debug") or {},
        ),
        "database_connectivity_guard": database_guard,
        "legacy_sqlite_guard": legacy_sqlite_guard(settings, live_report.get("api_runtime_debug") or {}),
    }

    report: dict[str, Any] = {
        "schema_version": "codex_acceptance_gate_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_name": args.library_name,
        "paper_ids": paper_ids,
        "api_base": args.api_base,
        "runtime_guard": runtime_guard,
        "smoke_report": smoke,
        "real_paper_artifact_parity": parity,
        "external_audit_visibility": visibility,
        "path_safety": path_safety,
        "postgres_external_audit": db_audit_payload,
        "mcp_get_review_coverage": mcp_payload,
        "live_diagnosis": live_report,
    }
    root_cause = classify_gate_root_cause(report)
    report["acceptance_gate"] = "FAIL" if root_cause else "PASS"
    report["root_cause"] = root_cause
    if root_cause and root_cause not in ALLOWED_ROOT_CAUSES:
        report["root_cause"] = "unknown"
    return json_safe(report)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    status = report["acceptance_gate"]
    root_cause = report.get("root_cause")
    lines = [
        "# Codex Acceptance Gate",
        "",
        f"ACCEPTANCE_GATE={status}",
    ]
    if root_cause:
        lines.append(f"root_cause={root_cause}")
    lines.extend(
        [
            "",
            f"- Created at: {report['created_at']}",
            f"- Library: `{report['library_name']}`",
            f"- API base: `{report['api_base']}`",
            f"- Paper IDs: `{', '.join(report['paper_ids'])}`",
            "",
            "## Runtime Guard",
            "",
            f"- Active database PostgreSQL: `{report['runtime_guard']['legacy_sqlite_guard']['api_is_postgresql']}`",
            f"- Legacy SQLite ignored: `{report['runtime_guard']['legacy_sqlite_guard']['legacy_sqlite_ignored']}`",
            f"- API runtime-debug status: `{report['runtime_guard']['api_runtime_debug'].get('status')}`",
            f"- API storage root exists: `{(report['runtime_guard']['api_runtime_debug'].get('json') or {}).get('storage_root_exists')}`",
            "",
            "## Live Artifact Parity",
            "",
            f"- Live diagnosis root causes: `{report['live_diagnosis'].get('root_cause_counts')}`",
            f"- All real-paper artifacts ready: `{report['real_paper_artifact_parity']['all_ready']}`",
            "",
        ]
    )
    for pid, row in report["real_paper_artifact_parity"]["by_paper"].items():
        lines.extend(
            [
                f"### {pid}",
                "",
                f"- local_ready: `{row['local_ready']}`",
                f"- api_get_paper_ready: `{row['api_get_paper_ready']}`",
                f"- api_get_codex_context_ready: `{row['api_get_codex_context_ready']}`",
                f"- api_review_center_ready: `{row['api_review_center_ready']}`",
                f"- artifact_ready_for_external_audit: `{row['artifact_ready_for_external_audit']}`",
                f"- blocking_errors: `{row['blocking_errors']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## External Audit Visibility",
            "",
            f"- All visible: `{report['external_audit_visibility']['all_visible']}`",
            f"- No Windows absolute paths exposed: `{report['path_safety']['no_windows_absolute_paths_exposed']}`",
            f"- External AI not required to resolve storage paths: `{report['path_safety']['external_ai_not_required_to_resolve_storage_paths']}`",
            "",
        ]
    )
    for pid, row in report["external_audit_visibility"]["by_paper"].items():
        lines.extend(
            [
                f"### {pid}",
                "",
                f"- MCP get_review_coverage visible: `{row['mcp_get_review_coverage_visible']}`",
                f"- Review-center visible: `{row['review_center_visible']}`",
                f"- PostgreSQL candidate visible: `{row['postgres_external_audit_visible']}`",
                f"- Does not auto-write verified/safe_verified: `{row['does_not_auto_write_verified_or_safe_verified']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Smoke Report",
            "",
            f"- Path: `{report['smoke_report'].get('path')}`",
            f"- Selected IDs match live IDs: `{report['smoke_report']['paper_ids_match']}`",
            f"- Selected IDs: `{report['smoke_report']['selected_paper_ids']}`",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.markdown)
    print(
        json.dumps(
            {
                "status": "ok",
                "acceptance_gate": report["acceptance_gate"],
                "root_cause": report.get("root_cause"),
                "output": str(args.output),
                "markdown": str(args.markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["acceptance_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
