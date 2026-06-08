from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from sqlalchemy import select, text


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun, ExtractionFieldReview, Paper
from app.db.session import session_scope
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import get_codex_context as mcp_get_codex_context
from app.mcp.server import get_paper as mcp_get_paper
from app.mcp.server import get_review_coverage as mcp_get_review_coverage
from app.services.paper_ingestion import PaperIngestionService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.review_safety import is_safe_verified_review
from scripts import codex_acceptance_gate as codex_gate
from scripts import fresh_realpaper_chain_acceptance as fresh_gate


SCHEMA_VERSION = "failure_recovery_acceptance_v1"
FAILURE_AUDIT_SOURCE = "codex_failure_recovery_acceptance_audit"
FAILURE_AUDIT_SOURCE_LABEL = "Codex failure recovery acceptance audit"
LEGACY_GATE_PAPER_IDS = (
    "2d977b15-7715-4a27-87e3-985dc77c4da1,"
    "d5d5c467-8a91-4f9a-9c93-4e4c84a30bab,"
    "e636ff33-55fc-436d-b4ec-1b4f064f4050"
)
LEGACY_GATE_LIBRARY_NAME = "chain_realpaper_smoke_20260608"
MISSING_PDF_CODES = {"missing_pdf", "pdf_missing"}
MISSING_TEXT_CODES = {
    "missing_markdown_and_docling_json",
    "markdown_missing",
    "docling_json_missing",
    "markdown_and_docling_json_empty",
}
BLOCKED_CANDIDATE_STATUSES = {
    "blocked",
    "requires_resolution",
    "artifact_precondition_failed",
    "precondition_failed",
}
ALLOWED_ROOT_CAUSES = {
    "api_server_unreachable",
    "database_unreachable",
    "pdf_download_failed",
    "invalid_pdf_source",
    "invalid_pdf_content",
    "parse_failed",
    "artifact_precondition_failed",
    "artifact_files_not_present_in_api_storage",
    "workspace_not_created",
    "external_audit_blocked_by_artifact_precondition",
    "external_audit_candidate_not_created",
    "review_center_incorrectly_visible",
    "verified_pollution",
    "safe_verified_pollution",
    "restore_failed",
    "codex_gate_regressed",
    "unknown",
}


@dataclass(frozen=True)
class Options:
    library_prefix: str
    api_base: str
    output: Path
    markdown: Path
    internal_case: str | None = None


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description="Controlled failure recovery acceptance gate.")
    parser.add_argument("--library-prefix", required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--internal-case", choices=["database_unreachable"], default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return Options(
        library_prefix=args.library_prefix,
        api_base=args.api_base.rstrip("/"),
        output=args.output,
        markdown=args.markdown,
        internal_case=args.internal_case,
    )


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


def has_traceback(*values: Any) -> bool:
    text_blob = "\n".join(str(value or "") for value in values)
    return "Traceback (most recent call last)" in text_blob or "\nTraceback" in text_blob


def case_result(
    name: str,
    status: str,
    root_cause: str,
    *,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "case": name,
        "status": status,
        "root_cause": root_cause if root_cause in ALLOWED_ROOT_CAUSES else "unknown",
        "reason": reason,
        "details": details or {},
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if str(path) == "-":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def escape_md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    if str(path) == "-":
        return
    status = report["failure_recovery_acceptance"]
    lines = [
        "# Failure Recovery Acceptance",
        "",
        f"FAILURE_RECOVERY_ACCEPTANCE={status}",
    ]
    if status != "PASS":
        lines.append(f"root_cause={report.get('root_cause') or 'unknown'}")
        lines.append(f"failed_case={report.get('failed_case') or 'unknown'}")
    lines.extend(
        [
            "",
            f"- Created at: {report['created_at']}",
            f"- Library: `{report.get('library_name')}`",
            f"- API base: `{report.get('api_base')}`",
            f"- Restore attempted: `{(report.get('restore') or {}).get('attempted')}`",
            f"- Restore succeeded: `{(report.get('restore') or {}).get('succeeded')}`",
            f"- Verified pollution: `{report.get('verified_pollution')}`",
            f"- Safe verified pollution: `{report.get('safe_verified_pollution')}`",
            "",
            "## Cases",
            "",
            "| Case | Status | Root Cause | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in report.get("cases", []):
        lines.append(
            "| {case} | {status} | {root} | {reason} |".format(
                case=escape_md(item.get("case")),
                status=escape_md(item.get("status")),
                root=escape_md(item.get("root_cause")),
                reason=escape_md(item.get("reason") or ""),
            )
        )
    legacy = report.get("legacy_codex_gate") or {}
    lines.extend(
        [
            "",
            "## Legacy Gate",
            "",
            f"- ACCEPTANCE_GATE: `{legacy.get('acceptance_gate')}`",
            f"- root_cause: `{legacy.get('root_cause')}`",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def internal_database_unreachable_case() -> dict[str, Any]:
    settings = get_settings()
    guard = codex_gate.database_connectivity_guard(settings)
    root_cause = "database_unreachable" if not guard.get("ok") else "unknown"
    return case_result(
        "case_database_unreachable",
        "PASS" if root_cause == "database_unreachable" else "FAIL",
        root_cause,
        details={
            "expected_root_cause": "database_unreachable",
            "traceback": False,
            "database_guard": guard,
        },
    )


def case_api_unreachable() -> dict[str, Any]:
    probe = fresh_gate.http_get_json("http://localhost:9", "/api/system/runtime-debug", timeout=2)
    trace = has_traceback(probe.get("error"), probe.get("json"))
    root_cause = "api_server_unreachable" if not probe.get("ok") and probe.get("status") is None else "unknown"
    return case_result(
        "case_api_unreachable",
        "PASS" if root_cause == "api_server_unreachable" and not trace else "FAIL",
        root_cause,
        details={
            "expected_root_cause": "api_server_unreachable",
            "traceback": trace,
            "probe": probe,
        },
    )


def case_database_unreachable(options: Options) -> dict[str, Any]:
    env = os.environ.copy()
    env["LITAI_DATABASE_URL"] = "postgresql+psycopg://failure:failure@127.0.0.1:9/failure_recovery"
    env["LITAI_FORCE_CONFIGURED_DATABASE"] = "true"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--internal-case",
        "database_unreachable",
        "--library-prefix",
        options.library_prefix,
        "--api-base",
        "http://localhost:9",
        "--output",
        "-",
        "--markdown",
        "-",
    ]
    completed = subprocess.run(command, cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True, timeout=30)
    trace = has_traceback(completed.stdout, completed.stderr)
    parsed: dict[str, Any] | None = None
    for line in reversed((completed.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
            break
    if parsed and parsed.get("status") == "PASS" and not trace and completed.returncode == 0:
        return parsed
    return case_result(
        "case_database_unreachable",
        "FAIL",
        "database_unreachable" if parsed and parsed.get("root_cause") == "database_unreachable" else "unknown",
        details={
            "expected_root_cause": "database_unreachable",
            "traceback": trace,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[-2000:],
            "stderr": (completed.stderr or "")[-2000:],
            "parsed": parsed,
        },
    )


def database_available(settings) -> dict[str, Any]:
    guard = fresh_gate.database_guard(settings)
    if not guard.get("ok"):
        return guard
    try:
        with session_scope(settings.database_url) as session:
            session.execute(text("select 1")).scalar_one()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", **guard}
    return {**guard, "ok": True}


def unique_library_name(prefix: str, settings) -> str:
    return fresh_gate.unique_library_name(prefix, settings)


def paper_ids_in_library(settings, library_name: str) -> set[str]:
    with session_scope(settings.database_url) as session:
        rows = session.scalars(select(Paper.id).where(Paper.library_name == library_name)).all()
    return {str(item) for item in rows}


def latest_paper_in_library(settings, library_name: str, exclude_ids: set[str]) -> str | None:
    with session_scope(settings.database_url) as session:
        papers = session.scalars(
            select(Paper)
            .where(Paper.library_name == library_name)
            .order_by(Paper.created_at.desc())
        ).all()
    for paper in papers:
        if str(paper.id) not in exclude_ids:
            return str(paper.id)
    return str(papers[0].id) if papers else None


def db_pollution_counts(settings, paper_ids: list[str], *, source: str | None = None) -> dict[str, Any]:
    if not paper_ids:
        return {
            "paper_ids": [],
            "run_count": 0,
            "candidate_count": 0,
            "external_audit_candidate_count": 0,
            "verified_count": 0,
            "safe_verified_count": 0,
            "review_verified_count": 0,
            "review_safe_verified_count": 0,
            "candidate_status_distribution": {},
            "verification_status_distribution": {},
        }
    ids = [UUID(str(item)) for item in paper_ids]
    candidate_status_distribution: dict[str, int] = {}
    verification_status_distribution: dict[str, int] = {}
    run_ids: set[str] = set()
    candidate_count = 0
    external_audit_count = 0
    verified_count = 0
    safe_verified_count = 0
    with session_scope(settings.database_url) as session:
        stmt = (
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id.in_(ids))
        )
        if source is not None:
            stmt = stmt.where(ExternalAnalysisRun.source == source)
        rows = session.execute(stmt).all()
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id.in_(ids))).all()
    for candidate, run in rows:
        run_ids.add(str(run.id))
        candidate_count += 1
        if candidate.candidate_type == "external_audit_opinion":
            external_audit_count += 1
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        evidence = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
        status = str(candidate.status or "unknown").lower()
        verification_status = str(
            payload.get("verification_status")
            or evidence.get("verification_status")
            or "unverified"
        ).lower()
        candidate_status_distribution[status] = candidate_status_distribution.get(status, 0) + 1
        verification_status_distribution[verification_status] = verification_status_distribution.get(verification_status, 0) + 1
        verified_count += int(status == "verified" or verification_status == "verified")
        safe_verified_count += int(status == "safe_verified" or verification_status == "safe_verified")
    review_verified_count = sum(1 for review in reviews if str(review.reviewer_status or "").lower() == "verified")
    review_safe_verified_count = sum(1 for review in reviews if is_safe_verified_review(review))
    return {
        "paper_ids": paper_ids,
        "run_count": len(run_ids),
        "candidate_count": candidate_count,
        "external_audit_candidate_count": external_audit_count,
        "verified_count": verified_count + review_verified_count,
        "safe_verified_count": safe_verified_count + review_safe_verified_count,
        "review_verified_count": review_verified_count,
        "review_safe_verified_count": review_safe_verified_count,
        "candidate_status_distribution": candidate_status_distribution,
        "verification_status_distribution": verification_status_distribution,
    }


def case_invalid_pdf_source(settings, library_name: str) -> dict[str, Any]:
    before = paper_ids_in_library(settings, library_name)
    details: dict[str, Any] = {"library_name": library_name, "before_count": len(before)}
    root_cause = "unknown"
    exception_text = None
    try:
        with session_scope(settings.database_url) as session:
            service = PaperIngestionService(session=session, settings=settings)
            missing_path = BACKEND_ROOT / "test-artifacts" / "failure_recovery_missing_source.pdf"
            asyncio.run(
                service.ingest_pdf(
                    source_path=missing_path,
                    original_filename="failure_recovery_missing_source.pdf",
                    library_name=library_name,
                    ingest_source="failure_recovery_invalid_source",
                )
            )
    except FileNotFoundError as exc:
        root_cause = "invalid_pdf_source"
        exception_text = f"{type(exc).__name__}: {exc}"
    except ValueError as exc:
        root_cause = "invalid_pdf_source"
        exception_text = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        exception_text = f"{type(exc).__name__}: {exc}"
        if "download" in str(exc).lower():
            root_cause = "pdf_download_failed"
    after = paper_ids_in_library(settings, library_name)
    created = sorted(after - before)
    pollution = db_pollution_counts(settings, created)
    ready_created = []
    for paper_id in created:
        local = fresh_gate.local_paper_payload(settings, paper_id)
        if (local.get("artifact_status") or {}).get("artifact_ready_for_external_audit"):
            ready_created.append(paper_id)
    details.update(
        {
            "after_count": len(after),
            "created_paper_ids": created,
            "exception": exception_text,
            "ready_created_paper_ids": ready_created,
            "pollution": pollution,
        }
    )
    ok = root_cause in {"invalid_pdf_source", "pdf_download_failed"} and not ready_created and pollution["verified_count"] == 0 and pollution["safe_verified_count"] == 0
    if pollution["safe_verified_count"]:
        root_cause = "safe_verified_pollution"
    elif pollution["verified_count"]:
        root_cause = "verified_pollution"
    return case_result("case_invalid_pdf_source", "PASS" if ok else "FAIL", root_cause, details=details)


def extract_paper_id_from_upload(upload: dict[str, Any]) -> str | None:
    payload = upload.get("json") if isinstance(upload.get("json"), dict) else {}
    paper_id = payload.get("paper_id")
    if paper_id:
        return str(paper_id)
    serialized = json.dumps(upload, ensure_ascii=False)
    match = re.search(r"docling_parse_failed:([0-9a-fA-F-]{36})", serialized)
    return match.group(1) if match else None


def review_center_row(api_base: str, paper_id: str) -> dict[str, Any] | None:
    payload = fresh_gate.http_get_json(api_base, "/api/workbench/review-center", params={"limit": 500}, timeout=60)
    rows = (payload.get("json") or {}).get("rows") if payload.get("ok") else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("paper_id")) == paper_id:
            return row
    return None


def case_invalid_pdf_content(options: Options, settings, library_name: str) -> dict[str, Any]:
    before = paper_ids_in_library(settings, library_name)
    upload: dict[str, Any] = {}
    with TemporaryDirectory(prefix="failure_recovery_invalid_pdf_") as tmpdir:
        invalid_pdf = Path(tmpdir) / "not_a_pdf.pdf"
        invalid_pdf.write_text("This is plain text, not a PDF.\n", encoding="utf-8")
        upload = fresh_gate.upload_pdf(options.api_base, invalid_pdf, library_name=library_name, identifier=None)
    after = paper_ids_in_library(settings, library_name)
    created = sorted(after - before)
    paper_id = extract_paper_id_from_upload(upload) or (created[0] if created else None)
    if not upload.get("ok") and upload.get("status") is None:
        return case_result(
            "case_invalid_pdf_content",
            "FAIL",
            "api_server_unreachable",
            details={"upload": upload, "created_paper_ids": created},
        )

    local = fresh_gate.local_paper_payload(settings, paper_id) if paper_id else {"ok": False, "error": "paper_not_created"}
    local_status = local.get("artifact_status") or {}
    pollution = db_pollution_counts(settings, [paper_id] if paper_id else created)
    center_row = review_center_row(options.api_base, paper_id) if paper_id else None
    root_cause = "invalid_pdf_content"
    if paper_id and local.get("workflow_status") in {"parse_failed", "needs_reingest", "metadata_only"}:
        root_cause = "parse_failed"
    if paper_id and local_status.get("blocking_errors"):
        root_cause = "artifact_precondition_failed"
    if pollution["safe_verified_count"]:
        root_cause = "safe_verified_pollution"
    elif pollution["verified_count"]:
        root_cause = "verified_pollution"
    ready = bool(local_status.get("artifact_ready_for_external_audit"))
    text_missing_or_warned = (
        not local_status.get("markdown_has_content")
        or not local_status.get("docling_json_has_content")
        or bool(local_status.get("warnings"))
    )
    review_center_ready = bool(((center_row or {}).get("artifact_status") or {}).get("artifact_ready_for_external_audit"))
    ok = (
        not ready
        and text_missing_or_warned
        and pollution["external_audit_candidate_count"] == 0
        and pollution["verified_count"] == 0
        and pollution["safe_verified_count"] == 0
        and not review_center_ready
    )
    return case_result(
        "case_invalid_pdf_content",
        "PASS" if ok else "FAIL",
        root_cause,
        details={
            "upload": upload,
            "paper_id": paper_id,
            "created_paper_ids": created,
            "local": local,
            "review_center_row": center_row,
            "pollution": pollution,
            "checks": {
                "artifact_ready_for_external_audit": ready,
                "markdown_has_content": local_status.get("markdown_has_content"),
                "docling_json_has_content": local_status.get("docling_json_has_content"),
                "external_audit_candidate_count": pollution["external_audit_candidate_count"],
                "verified_count": pollution["verified_count"],
                "safe_verified_count": pollution["safe_verified_count"],
            },
        },
    )


def mcp_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="codex_failure_recovery_acceptance",
        display_name="Codex Failure Recovery Acceptance",
        capabilities=frozenset({"read_papers", "propose_corrections"}),
        raw_key="codex_failure_recovery_acceptance",
    )


def mcp_surfaces(paper_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with mcp_auth_context(mcp_auth()):
        for name, func in (
            ("get_paper", mcp_get_paper),
            ("get_codex_context", mcp_get_codex_context),
            ("get_review_coverage", mcp_get_review_coverage),
        ):
            try:
                result[name] = func(paper_id)
            except Exception as exc:
                result[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


def status_from_mcp_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if name == "get_codex_context":
        context = payload.get("context") if isinstance(payload, dict) else {}
        return context.get("artifact_status") if isinstance(context, dict) else {}
    return payload.get("artifact_status") if isinstance(payload, dict) else {}


def collect_artifact_surfaces(options: Options, settings, paper_id: str) -> dict[str, Any]:
    local = fresh_gate.local_paper_payload(settings, paper_id)
    review_center = fresh_gate.http_get_json(options.api_base, "/api/workbench/review-center", params={"limit": 500}, timeout=60)
    api = fresh_gate.api_payload_for_paper(options.api_base, paper_id, review_center)
    mcp = mcp_surfaces(paper_id)
    return {
        "local": local,
        "api": api,
        "mcp": mcp,
        "review_center": review_center,
    }


def combined_blocking_errors(surfaces: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    local_status = (surfaces.get("local") or {}).get("artifact_status") or {}
    errors.extend(local_status.get("blocking_errors") or [])
    api = surfaces.get("api") or {}
    for key in ("api_get_paper", "api_get_codex_context"):
        payload = (api.get(key) or {}).get("json") if (api.get(key) or {}).get("ok") else {}
        if key == "api_get_codex_context":
            payload = (payload.get("context") or {}) if isinstance(payload, dict) else {}
        status = payload.get("artifact_status") if isinstance(payload, dict) else {}
        errors.extend((status or {}).get("blocking_errors") or [])
    review_status = ((api.get("api_review_center_row") or {}).get("artifact_status") or {})
    errors.extend(review_status.get("blocking_errors") or [])
    for name, payload in (surfaces.get("mcp") or {}).items():
        errors.extend(status_from_mcp_payload(name, payload).get("blocking_errors") or [])
    return sorted(set(str(item) for item in errors))


def surface_ready_flags(surfaces: dict[str, Any]) -> dict[str, bool]:
    local_status = (surfaces.get("local") or {}).get("artifact_status") or {}
    api = surfaces.get("api") or {}
    detail_payload = (api.get("api_get_paper") or {}).get("json") if (api.get("api_get_paper") or {}).get("ok") else {}
    codex_payload = (api.get("api_get_codex_context") or {}).get("json") if (api.get("api_get_codex_context") or {}).get("ok") else {}
    context = (codex_payload.get("context") or {}) if isinstance(codex_payload, dict) else {}
    review_row = api.get("api_review_center_row") or {}
    mcp = surfaces.get("mcp") or {}
    return {
        "local_ready": bool(local_status.get("artifact_ready_for_external_audit")),
        "api_get_paper_ready": bool(((detail_payload or {}).get("artifact_status") or {}).get("artifact_ready_for_external_audit")),
        "api_get_codex_context_ready": bool(((context or {}).get("artifact_status") or {}).get("artifact_ready_for_external_audit")),
        "api_review_center_ready": bool(((review_row or {}).get("artifact_status") or {}).get("artifact_ready_for_external_audit")),
        "mcp_get_paper_ready": bool(status_from_mcp_payload("get_paper", mcp.get("get_paper") or {}).get("artifact_ready_for_external_audit")),
        "mcp_get_codex_context_ready": bool(status_from_mcp_payload("get_codex_context", mcp.get("get_codex_context") or {}).get("artifact_ready_for_external_audit")),
        "mcp_get_review_coverage_ready": bool(status_from_mcp_payload("get_review_coverage", mcp.get("get_review_coverage") or {}).get("artifact_ready_for_external_audit")),
    }


def surface_access_flags(surfaces: dict[str, Any]) -> dict[str, bool]:
    api = surfaces.get("api") or {}
    mcp = surfaces.get("mcp") or {}
    return {
        "api_get_paper_ok": bool((api.get("api_get_paper") or {}).get("ok")),
        "api_get_codex_context_ok": bool((api.get("api_get_codex_context") or {}).get("ok")),
        "api_review_center_ok": bool((surfaces.get("review_center") or {}).get("ok")),
        "mcp_get_paper_ok": not bool((mcp.get("get_paper") or {}).get("error")),
        "mcp_get_codex_context_ok": not bool((mcp.get("get_codex_context") or {}).get("error")),
        "mcp_get_review_coverage_ok": not bool((mcp.get("get_review_coverage") or {}).get("error")),
    }


def create_ready_seed_paper(options: Options, settings, library_name: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for path in local_real_pdf_candidates():
        upload = fresh_gate.upload_pdf(options.api_base, path, library_name=library_name, identifier=None)
        attempt: dict[str, Any] = {
            "source": "local_real_pdf_candidate",
            "path": str(path),
            "size": path.stat().st_size,
            "upload": upload,
        }
        if upload.get("ok"):
            payload = upload.get("json") if isinstance(upload.get("json"), dict) else {}
            paper_id = str(payload.get("paper_id") or "")
            if paper_id:
                prepare = fresh_gate.prepare_workspace(options.api_base, paper_id)
                local = fresh_gate.local_paper_payload(settings, paper_id)
                attempt.update({"paper_id": paper_id, "prepare": prepare, "local": local})
                attempts.append(attempt)
                if (local.get("artifact_status") or {}).get("artifact_ready_for_external_audit"):
                    return {"ok": True, "paper_id": paper_id, "ingestion": {"real_pdf_source": "local_real_pdf_candidate"}, "attempts": attempts}
                continue
        attempts.append(attempt)

    ingestion_options = fresh_gate.Options(
        library_prefix=library_name,
        api_base=options.api_base,
        min_real_papers=1,
        target_real_papers=1,
        output=Path("-"),
        markdown=Path("-"),
    )
    ingestion = fresh_gate.ingest_fresh_papers(ingestion_options, settings, library_name)
    for paper_id in ingestion.get("paper_ids") or []:
        prepare = fresh_gate.prepare_workspace(options.api_base, paper_id)
        local = fresh_gate.local_paper_payload(settings, paper_id)
        attempts.append({"paper_id": paper_id, "prepare": prepare, "local": local})
        if (local.get("artifact_status") or {}).get("artifact_ready_for_external_audit"):
            return {"ok": True, "paper_id": paper_id, "ingestion": ingestion, "attempts": attempts}
    return {"ok": False, "ingestion": ingestion, "attempts": attempts}


def local_real_pdf_candidates() -> list[Path]:
    roots = [
        BACKEND_ROOT.parent.parent / "test-artifacts" / "pdf-eval",
        BACKEND_ROOT / "test-artifacts" / "real_pdfs",
        BACKEND_ROOT / "test-artifacts" / "real-paper-smoke-20260608" / "pdfs",
    ]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.pdf"), key=lambda item: item.stat().st_size):
            resolved = path.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                size = path.stat().st_size
                if size <= 1024 or size > 30 * 1024 * 1024:
                    continue
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        continue
            except OSError:
                continue
            candidates.append(path)
    return candidates[:12]


def resolved_artifact_path(settings, paper_id: str) -> tuple[Path | None, dict[str, Any]]:
    with session_scope(settings.database_url) as session:
        paper = session.get(Paper, UUID(paper_id))
        if paper is None:
            return None, {"error": "paper_not_found"}
        path = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
        return path, {"paper_id": paper_id, "pdf_path": paper.pdf_path}


def external_audit_payload(paper_id: str) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "source": FAILURE_AUDIT_SOURCE,
        "source_label": FAILURE_AUDIT_SOURCE_LABEL,
        "raw_text": None,
        "raw_payload": {
            "paper_id": paper_id,
            "source": FAILURE_AUDIT_SOURCE,
            "verdict": "WARN",
            "recommended_action": "human_confirm_after_artifact_recovery",
            "suspected_missing": ["artifact_precondition_failed"],
            "metadata_status": "unknown",
            "section_structure_status": "blocked",
            "table_status": "blocked",
            "figure_status": "blocked",
            "dft_status": "blocked",
            "verification_status": "unverified",
            "confidence": 0.5,
            "evidence_examples": [
                {
                    "paper_id": paper_id,
                    "text": "Failure recovery acceptance import should be blocked until artifacts are ready.",
                }
            ],
        },
    }


def case_external_audit_import_blocked(options: Options, settings, paper_id: str) -> dict[str, Any]:
    before = db_pollution_counts(settings, [paper_id], source=FAILURE_AUDIT_SOURCE)
    import_result = fresh_gate.http_post_json(
        options.api_base,
        "/api/external-analysis/import",
        external_audit_payload(paper_id),
        timeout=120,
    )
    after = db_pollution_counts(settings, [paper_id], source=FAILURE_AUDIT_SOURCE)
    center_row = review_center_row(options.api_base, paper_id)
    source_counts = (center_row or {}).get("external_audit_source_counts") or {}
    candidate_delta = int(after["external_audit_candidate_count"]) - int(before["external_audit_candidate_count"])
    status_distribution = after.get("candidate_status_distribution") or {}
    blocked_candidates_only = bool(status_distribution) and all(
        str(status).lower() in BLOCKED_CANDIDATE_STATUSES for status in status_distribution
    )
    response_payload = import_result.get("json") if isinstance(import_result.get("json"), dict) else {}
    mapping_status = response_payload.get("mapping_status")
    root_cause = (
        "external_audit_blocked_by_artifact_precondition"
        if mapping_status == "artifact_precondition_failed"
        else "external_audit_candidate_not_created"
    )
    if after["safe_verified_count"]:
        root_cause = "safe_verified_pollution"
    elif after["verified_count"]:
        root_cause = "verified_pollution"
    elif candidate_delta > 0 and not blocked_candidates_only:
        root_cause = "review_center_incorrectly_visible" if int(source_counts.get(FAILURE_AUDIT_SOURCE, 0) or 0) > 0 else "unknown"
    ok = (
        import_result.get("ok") is True
        and after["verified_count"] == 0
        and after["safe_verified_count"] == 0
        and (candidate_delta == 0 or blocked_candidates_only)
        and int(source_counts.get(FAILURE_AUDIT_SOURCE, 0) or 0) == 0
    )
    return case_result(
        "case_external_audit_import_blocked",
        "PASS" if ok else "FAIL",
        root_cause,
        details={
            "paper_id": paper_id,
            "import_result": import_result,
            "before": before,
            "after": after,
            "candidate_delta": candidate_delta,
            "review_center_row": center_row,
            "checks": {
                "candidate_not_created_or_blocked": candidate_delta == 0 or blocked_candidates_only,
                "review_center_not_visible_as_normal_candidate": int(source_counts.get(FAILURE_AUDIT_SOURCE, 0) or 0) == 0,
                "verified_count": after["verified_count"],
                "safe_verified_count": after["safe_verified_count"],
            },
        },
    )


def case_artifact_missing_and_external_import(options: Options, settings, library_name: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    restore = {"attempted": False, "succeeded": None, "path": None, "backup_path": None}
    seed = create_ready_seed_paper(options, settings, library_name)
    if not seed.get("ok"):
        skipped = case_result(
            "case_artifact_file_missing",
            "SKIPPED",
            "workspace_not_created",
            reason="Could not create a ready real-PDF seed paper safely.",
            details={"seed": seed},
        )
        skipped_import = case_result(
            "case_external_audit_import_blocked",
            "SKIPPED",
            "workspace_not_created",
            reason="Skipped because Case 4 did not have a failed artifact paper.",
            details={"seed": seed},
        )
        return skipped, skipped_import, restore

    paper_id = str(seed["paper_id"])
    target, target_details = resolved_artifact_path(settings, paper_id)
    if target is None or not target.exists() or not target.is_file():
        skipped = case_result(
            "case_artifact_file_missing",
            "SKIPPED",
            "artifact_files_not_present_in_api_storage",
            reason="Ready seed paper did not expose a rename-safe PDF artifact.",
            details={"seed": seed, "target": target_details},
        )
        skipped_import = case_result(
            "case_external_audit_import_blocked",
            "SKIPPED",
            "artifact_files_not_present_in_api_storage",
            reason="Skipped because Case 4 could not safely rename an artifact.",
            details={"seed": seed, "target": target_details},
        )
        return skipped, skipped_import, restore

    backup = target.with_name(target.name + ".bak_failure_recovery")
    if backup.exists():
        backup = target.with_name(target.name + f".{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.bak_failure_recovery")
    missing_surfaces: dict[str, Any] = {}
    restored_surfaces: dict[str, Any] = {}
    case5 = case_result(
        "case_external_audit_import_blocked",
        "SKIPPED",
        "unknown",
        reason="External audit import was not reached.",
    )
    case4_status = "FAIL"
    case4_root = "unknown"
    case4_reason = None
    try:
        restore.update({"attempted": True, "path": str(target), "backup_path": str(backup), "succeeded": False})
        target.rename(backup)
        missing_surfaces = collect_artifact_surfaces(options, settings, paper_id)
        missing_flags = surface_ready_flags(missing_surfaces)
        access_flags = surface_access_flags(missing_surfaces)
        errors = combined_blocking_errors(missing_surfaces)
        case5 = case_external_audit_import_blocked(options, settings, paper_id)
        api_ok = access_flags["api_get_paper_ok"] and access_flags["api_get_codex_context_ok"] and access_flags["api_review_center_ok"]
        mcp_ok = (
            access_flags["mcp_get_paper_ok"]
            and access_flags["mcp_get_codex_context_ok"]
            and access_flags["mcp_get_review_coverage_ok"]
        )
        missing_ok = (
            api_ok
            and mcp_ok
            and missing_flags["api_get_paper_ready"] is False
            and missing_flags["api_get_codex_context_ready"] is False
            and missing_flags["mcp_get_paper_ready"] is False
            and missing_flags["mcp_get_review_coverage_ready"] is False
            and bool(MISSING_PDF_CODES & set(errors))
        )
        case4_status = "PASS" if missing_ok else "FAIL"
        if missing_ok:
            case4_root = "artifact_precondition_failed"
        elif not api_ok:
            case4_root = "api_server_unreachable"
        else:
            case4_root = "artifact_files_not_present_in_api_storage"
    except Exception as exc:
        case4_status = "FAIL"
        case4_root = "unknown"
        case4_reason = f"{type(exc).__name__}: {exc}"
    finally:
        if restore["attempted"]:
            try:
                if backup.exists():
                    backup.rename(target)
                restore["succeeded"] = bool(target.exists() and target.is_file())
            except Exception as exc:
                restore["succeeded"] = False
                restore["error"] = f"{type(exc).__name__}: {exc}"

    if restore["attempted"] and not restore["succeeded"]:
        case4_status = "FAIL"
        case4_root = "restore_failed"
    else:
        restored_surfaces = collect_artifact_surfaces(options, settings, paper_id)
        restored_flags = surface_ready_flags(restored_surfaces)
        local_restored_status = (restored_surfaces.get("local") or {}).get("artifact_status") or {}
        local_restored_errors = list(local_restored_status.get("blocking_errors") or [])
        if not (restored_flags["local_ready"] and local_restored_errors == []):
            case4_status = "FAIL"
            case4_root = "restore_failed"
            restore["post_restore_flags"] = restored_flags
            restore["post_restore_blocking_errors"] = local_restored_errors

    case4 = case_result(
        "case_artifact_file_missing",
        case4_status,
        case4_root,
        reason=case4_reason,
        details={
            "paper_id": paper_id,
            "seed": seed,
            "missing_access_flags": surface_access_flags(missing_surfaces) if missing_surfaces else {},
            "missing_ready_flags": surface_ready_flags(missing_surfaces) if missing_surfaces else {},
            "missing_blocking_errors": combined_blocking_errors(missing_surfaces) if missing_surfaces else [],
            "restored_access_flags": surface_access_flags(restored_surfaces) if restored_surfaces else {},
            "restored_ready_flags": surface_ready_flags(restored_surfaces) if restored_surfaces else {},
            "restored_blocking_errors": combined_blocking_errors(restored_surfaces) if restored_surfaces else [],
            "restore": restore,
        },
    )
    return case4, case5, restore


def run_legacy_codex_gate(options: Options) -> dict[str, Any]:
    args = argparse.Namespace(
        paper_ids=LEGACY_GATE_PAPER_IDS,
        library_name=LEGACY_GATE_LIBRARY_NAME,
        api_base=options.api_base,
        output=Path("-"),
        markdown=Path("-"),
    )
    try:
        return codex_gate.build_report(args)
    except Exception as exc:
        return {
            "schema_version": "codex_acceptance_gate_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "library_name": LEGACY_GATE_LIBRARY_NAME,
            "paper_ids": [item.strip() for item in LEGACY_GATE_PAPER_IDS.split(",")],
            "api_base": options.api_base,
            "acceptance_gate": "FAIL",
            "root_cause": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
        }


def classify_report(report: dict[str, Any]) -> tuple[str, str | None, str | None]:
    for item in report.get("cases", []):
        if item.get("status") == "FAIL":
            return "FAIL", item.get("root_cause") or "unknown", item.get("case")
    legacy = report.get("legacy_codex_gate") or {}
    if legacy.get("acceptance_gate") != "PASS":
        return "FAIL", "codex_gate_regressed", "legacy_codex_gate"
    return "PASS", None, None


def build_report(options: Options) -> dict[str, Any]:
    settings = get_settings()
    db_guard = database_available(settings)
    library_name = unique_library_name(options.library_prefix, settings) if db_guard.get("ok") else options.library_prefix
    cases = [case_api_unreachable(), case_database_unreachable(options)]
    restore = {"attempted": False, "succeeded": None}

    if db_guard.get("ok"):
        cases.append(case_invalid_pdf_source(settings, library_name))
        cases.append(case_invalid_pdf_content(options, settings, library_name))
        case4, case5, restore = case_artifact_missing_and_external_import(options, settings, library_name)
        cases.extend([case4, case5])
    else:
        for name in ("case_invalid_pdf_source", "case_invalid_pdf_content", "case_artifact_file_missing", "case_external_audit_import_blocked"):
            cases.append(
                case_result(
                    name,
                    "SKIPPED",
                    "database_unreachable",
                    reason="Active database is unreachable; fault simulation would not be isolated.",
                    details={"database_guard": db_guard},
                )
            )

    legacy = run_legacy_codex_gate(options)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_prefix": options.library_prefix,
        "library_name": library_name,
        "api_base": options.api_base,
        "database_guard": db_guard,
        "cases": cases,
        "restore": restore,
        "legacy_codex_gate": {
            "acceptance_gate": legacy.get("acceptance_gate"),
            "root_cause": legacy.get("root_cause"),
            "paper_ids": legacy.get("paper_ids"),
            "library_name": legacy.get("library_name"),
        },
    }
    all_case_paper_ids: list[str] = []
    for item in cases:
        details = item.get("details") or {}
        paper_id = details.get("paper_id")
        if paper_id:
            all_case_paper_ids.append(str(paper_id))
        all_case_paper_ids.extend(str(pid) for pid in details.get("created_paper_ids") or [])
    pollution = db_pollution_counts(settings, sorted(set(all_case_paper_ids))) if db_guard.get("ok") else {}
    report["pollution"] = pollution
    report["verified_pollution"] = bool((pollution or {}).get("verified_count"))
    report["safe_verified_pollution"] = bool((pollution or {}).get("safe_verified_count"))
    status, root_cause, failed_case = classify_report(report)
    if report["safe_verified_pollution"]:
        status, root_cause, failed_case = "FAIL", "safe_verified_pollution", failed_case or "pollution"
    elif report["verified_pollution"]:
        status, root_cause, failed_case = "FAIL", "verified_pollution", failed_case or "pollution"
    report["failure_recovery_acceptance"] = status
    report["root_cause"] = root_cause
    report["failed_case"] = failed_case
    return json_safe(report)


def unexpected_failure_report(options: Options, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_prefix": options.library_prefix,
        "library_name": options.library_prefix,
        "api_base": options.api_base,
        "failure_recovery_acceptance": "FAIL",
        "root_cause": "unknown",
        "failed_case": "script_unhandled_exception",
        "cases": [
            case_result(
                "script_unhandled_exception",
                "FAIL",
                "unknown",
                details={"error": f"{type(exc).__name__}: {exc}", "traceback": False},
            )
        ],
        "restore": {"attempted": False, "succeeded": None},
        "legacy_codex_gate": {"acceptance_gate": None, "root_cause": None},
        "verified_pollution": None,
        "safe_verified_pollution": None,
    }


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if options.internal_case == "database_unreachable":
        result = internal_database_unreachable_case()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 1
    try:
        report = build_report(options)
    except Exception as exc:
        report = unexpected_failure_report(options, exc)
    write_json(options.output, report)
    write_markdown(options.markdown, report)
    print(
        json.dumps(
            {
                "status": report["failure_recovery_acceptance"],
                "root_cause": report.get("root_cause"),
                "failed_case": report.get("failed_case"),
                "library_name": report.get("library_name"),
                "restore": report.get("restore"),
                "output": str(options.output),
                "markdown": str(options.markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["failure_recovery_acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
