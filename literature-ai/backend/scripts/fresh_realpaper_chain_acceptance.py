from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select, text
from sqlalchemy.engine import make_url


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.db.session import session_scope
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.mcp.server import get_codex_context as mcp_get_codex_context
from app.mcp.server import get_paper as mcp_get_paper
from app.mcp.server import get_review_coverage as mcp_get_review_coverage
from app.services.discovery_service import DiscoveryService
from app.services.paper_identity import PaperIdentityService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.artifact_status import build_paper_artifact_status


AUDIT_SOURCE = "codex_fresh_chain_acceptance_audit"
AUDIT_SOURCE_LABEL = "Codex fresh real-paper chain acceptance audit"
SCHEMA_VERSION = "fresh_realpaper_chain_acceptance_v1"

ALLOWED_ROOT_CAUSES = {
    "real_pdf_source_unavailable",
    "ingestion_failed",
    "parse_failed",
    "artifact_refs_not_persisted_to_active_postgres",
    "artifact_files_not_present_in_api_storage",
    "workspace_not_created",
    "ai_reading_package_missing",
    "external_audit_import_failed",
    "external_audit_candidate_not_created",
    "coverage_not_visible",
    "review_center_not_visible",
    "api_artifact_status_uses_different_code_path",
    "api_server_not_reloaded_or_running_old_code",
    "storage_root_mismatch_between_cli_and_api",
    "legacy_sqlite_used_as_runtime_source",
    "unknown",
}

DISCOVERY_QUERIES = [
    "oxygen reduction reaction single atom catalyst density functional theory",
    "metal nitrogen carbon oxygen reduction catalyst DFT",
    "electrocatalyst density functional theory oxygen reduction",
    "single atom catalyst oxygen evolution reaction DFT",
]

RELEVANCE_TERMS = (
    "catalyst",
    "catalysts",
    "catalytic",
    "electrocatalyst",
    "electrocatalytic",
    "oxygen reduction",
    "oxygen evolution",
    "single-atom",
    "single atom",
    "m-n-c",
)
COMPUTATIONAL_TERMS = (
    "dft",
    "density functional",
    "first-principles",
    "first principles",
    "computational",
    "calculation",
    "calculations",
)


@dataclass(frozen=True)
class Options:
    library_prefix: str
    api_base: str
    min_real_papers: int
    target_real_papers: int
    output: Path
    markdown: Path


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description="Fresh real-paper chain acceptance gate.")
    parser.add_argument("--library-prefix", required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--min-real-papers", type=int, default=1)
    parser.add_argument("--target-real-papers", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.min_real_papers <= 0:
        parser.error("--min-real-papers must be positive")
    if args.target_real_papers < args.min_real_papers:
        parser.error("--target-real-papers must be >= --min-real-papers")
    return Options(
        library_prefix=args.library_prefix,
        api_base=args.api_base.rstrip("/"),
        min_real_papers=args.min_real_papers,
        target_real_papers=args.target_real_papers,
        output=args.output,
        markdown=args.markdown,
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


def http_result(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = None
    result: dict[str, Any] = {
        "ok": response.is_success,
        "status": response.status_code,
        "url": str(response.url),
    }
    if payload is not None:
        result["json"] = payload
    if not response.is_success:
        result["error"] = response.text[:2000]
    return result


def http_get_json(api_base: str, path: str, *, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            return http_result(client.get(f"{api_base}{path}", params=params))
    except Exception as exc:
        return {"ok": False, "status": None, "url": f"{api_base}{path}", "error": f"{type(exc).__name__}: {exc}"}


def http_post_json(
    api_base: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            return http_result(client.post(f"{api_base}{path}", json=payload))
    except Exception as exc:
        return {"ok": False, "status": None, "url": f"{api_base}{path}", "error": f"{type(exc).__name__}: {exc}"}


def upload_pdf(
    api_base: str,
    pdf_path: Path,
    *,
    library_name: str,
    identifier: str | None,
) -> dict[str, Any]:
    data = {"library_name": library_name}
    if identifier:
        data["identifier"] = identifier
    try:
        with httpx.Client(timeout=httpx.Timeout(900.0), follow_redirects=True) as client:
            with pdf_path.open("rb") as handle:
                files = {"file": (pdf_path.name, handle, "application/pdf")}
                response = client.post(f"{api_base}/api/papers/ingest/upload", data=data, files=files)
        return http_result(response)
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "url": f"{api_base}/api/papers/ingest/upload",
            "error": f"{type(exc).__name__}: {exc}",
        }


def database_guard(settings) -> dict[str, Any]:
    try:
        url = make_url(settings.database_url)
        drivername = url.drivername
    except Exception as exc:
        return {"ok": False, "drivername": None, "error": f"database_url_parse_failed: {type(exc).__name__}: {exc}"}
    if not drivername.startswith("postgresql"):
        return {"ok": False, "drivername": drivername, "error": "active_database_is_not_postgresql"}
    try:
        with session_scope(settings.database_url) as session:
            session.execute(text("select 1")).scalar_one()
    except Exception as exc:
        return {"ok": False, "drivername": drivername, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "drivername": drivername}


def unique_library_name(prefix: str, settings) -> str:
    with session_scope(settings.database_url) as session:
        existing = set(
            session.scalars(
                select(Paper.library_name)
                .where(Paper.library_name.like(f"{prefix}%"))
                .group_by(Paper.library_name)
            ).all()
        )
    if prefix not in existing:
        return prefix
    for index in range(1, 1000):
        candidate = f"{prefix}_{index:03d}"
        if candidate not in existing:
            return candidate
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{timestamp}"


def normalize_title(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def paper_index(settings) -> dict[str, set[str]]:
    dois: set[str] = set()
    titles: set[str] = set()
    source_texts: set[str] = set()
    with session_scope(settings.database_url) as session:
        rows = session.execute(select(Paper.doi, Paper.title, Paper.source_path)).all()
    for doi, title, source_path in rows:
        normalized_doi = PaperIdentityService.normalize_doi(doi)
        if normalized_doi:
            dois.add(normalized_doi)
        normalized_title = normalize_title(title)
        if normalized_title:
            titles.add(normalized_title)
        if source_path:
            source_texts.add(str(source_path).lower())
    return {"dois": dois, "titles": titles, "source_texts": source_texts}


def candidate_key(item: dict[str, Any]) -> str:
    doi = PaperIdentityService.normalize_doi(item.get("doi"))
    arxiv_id = PaperIdentityService.extract_arxiv_id(
        " ".join(str(item.get(key) or "") for key in ("identifier", "url", "pdf_url"))
    )
    if doi:
        return f"doi:{doi}"
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower()}"
    return f"title:{normalize_title(item.get('title'))}"


def candidate_is_existing(item: dict[str, Any], index: dict[str, set[str]]) -> bool:
    doi = PaperIdentityService.normalize_doi(item.get("doi"))
    if doi and doi in index["dois"]:
        return True
    title = normalize_title(item.get("title"))
    if title and title in index["titles"]:
        return True
    arxiv_id = PaperIdentityService.extract_arxiv_id(
        " ".join(str(item.get(key) or "") for key in ("identifier", "url", "pdf_url"))
    )
    if arxiv_id:
        needle = arxiv_id.lower()
        if any(needle in source for source in index["source_texts"]):
            return True
    return False


def candidate_is_relevant(item: dict[str, Any]) -> bool:
    text_blob = " ".join(
        str(item.get(key) or "")
        for key in ("title", "abstract", "journal", "identifier", "url")
    ).lower()
    return any(term in text_blob for term in RELEVANCE_TERMS) and any(
        term in text_blob for term in COMPUTATIONAL_TERMS
    )


def search_discovery_candidates(options: Options, settings) -> dict[str, Any]:
    index = paper_index(settings)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    attempts: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_irrelevant = 0
    for query in DISCOVERY_QUERIES:
        response = http_get_json(
            options.api_base,
            "/api/papers/discovery/search",
            params={"q": query, "providers": ["arxiv"], "limit": max(12, options.target_real_papers * 5)},
            timeout=45,
        )
        rows = []
        if response.get("ok") and isinstance(response.get("json"), dict):
            rows = response["json"].get("items") or []
        attempts.append(
            {
                "query": query,
                "ok": response.get("ok"),
                "status": response.get("status"),
                "error": response.get("error"),
                "returned": len(rows),
            }
        )
        for item in rows:
            if not isinstance(item, dict) or not item.get("pdf_url"):
                continue
            key = candidate_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            if candidate_is_existing(item, index):
                skipped_existing += 1
                continue
            if not candidate_is_relevant(item):
                skipped_irrelevant += 1
                continue
            selected.append({**item, "discovery_query": query})
            if len(selected) >= max(options.target_real_papers * 3, options.min_real_papers):
                return {
                    "attempts": attempts,
                    "candidates": selected,
                    "skipped_existing_count": skipped_existing,
                    "skipped_irrelevant_count": skipped_irrelevant,
                }
    return {
        "attempts": attempts,
        "candidates": selected,
        "skipped_existing_count": skipped_existing,
        "skipped_irrelevant_count": skipped_irrelevant,
    }


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:140] or "downloaded_realpaper"


def download_pdf_url_fallback(pdf_url: str, dest_dir: Path, filename: str) -> Path:
    target = dest_dir / filename
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        response = client.get(pdf_url)
        response.raise_for_status()
        content = response.content
    if not content.startswith(b"%PDF"):
        raise ValueError("downloaded URL did not return a PDF")
    target.write_bytes(content)
    return target


def download_candidate_pdf(service: DiscoveryService | None, candidate: dict[str, Any], dest_dir: Path) -> dict[str, Any]:
    pdf_url = str(candidate.get("pdf_url") or "").strip()
    title = str(candidate.get("title") or candidate.get("identifier") or "realpaper")
    arxiv_id = PaperIdentityService.extract_arxiv_id(
        " ".join(str(candidate.get(key) or "") for key in ("identifier", "url", "pdf_url"))
    )
    stem = arxiv_id or PaperIdentityService.normalize_doi(candidate.get("doi")) or title
    filename = f"{sanitize_filename(stem)}.pdf"
    try:
        if service is not None:
            path = service.download_pdf_url(pdf_url, dest_dir, filename)
            method = "DiscoveryService.download_pdf_url"
        else:
            path = download_pdf_url_fallback(pdf_url, dest_dir, filename)
            method = "httpx_direct_pdf_url_fallback"
        return {
            "ok": True,
            "method": method,
            "pdf_url": pdf_url,
            "path": str(path),
            "filename": path.name,
            "size": path.stat().st_size,
        }
    except Exception as exc:
        return {
            "ok": False,
            "pdf_url": pdf_url,
            "filename": filename,
            "error": f"{type(exc).__name__}: {exc}",
        }


def existing_unclaimed_real_pdfs(settings, limit: int) -> list[Path]:
    candidate_dirs = [
        BACKEND_ROOT / "test-artifacts" / "real_pdfs",
        BACKEND_ROOT / "test-artifacts" / "real-paper-smoke-20260608" / "pdfs",
    ]
    index = paper_index(settings)
    paths: list[Path] = []
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.pdf")):
            token = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
            doi_guess = token.replace("_", "/")
            item = {
                "title": path.stem,
                "doi": doi_guess if doi_guess.startswith("10.") else None,
                "identifier": doi_guess,
                "url": "",
                "pdf_url": "",
            }
            if candidate_is_existing(item, index):
                continue
            paths.append(path)
            if len(paths) >= limit:
                return paths
    return paths


def deterministic_audit_payload(paper_id: str, title: str | None, local_status: dict[str, Any]) -> dict[str, Any]:
    suspected_missing: list[str] = []
    if not local_status.get("grobid_tei_has_content"):
        suspected_missing.append("grobid_tei")
    warnings = list(local_status.get("warnings") or [])
    return {
        "paper_id": paper_id,
        "source": AUDIT_SOURCE,
        "verdict": "WARN" if suspected_missing or warnings else "PASS",
        "recommended_action": "human_confirm",
        "suspected_missing": suspected_missing,
        "metadata_status": "PASS",
        "section_structure_status": "PASS" if local_status.get("markdown_has_content") else "WARN",
        "table_status": "candidate_unverified",
        "figure_status": "candidate_unverified",
        "dft_status": "candidate_unverified",
        "verification_status": "unverified",
        "confidence": 0.72,
        "evidence_examples": [
            {
                "paper_id": paper_id,
                "title": title,
                "text": "Deterministic acceptance payload verifies import visibility only.",
            }
        ],
        "blocking_errors_at_import": local_status.get("blocking_errors") or [],
    }


def import_external_audit(api_base: str, paper_id: str, title: str | None, local_status: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "paper_id": paper_id,
        "source": AUDIT_SOURCE,
        "source_label": AUDIT_SOURCE_LABEL,
        "raw_text": None,
        "raw_payload": deterministic_audit_payload(paper_id, title, local_status),
    }
    return http_post_json(api_base, "/api/external-analysis/import", payload, timeout=120)


def prepare_workspace(api_base: str, paper_id: str) -> dict[str, Any]:
    return http_post_json(api_base, f"/api/workbench/papers/{paper_id}/prepare", {"render_pages": False}, timeout=180)


def file_size(path: Path | None) -> int | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return int(path.stat().st_size)


def local_paper_payload(settings, paper_id: str) -> dict[str, Any]:
    try:
        pid = UUID(paper_id)
    except ValueError as exc:
        return {"ok": False, "paper_id": paper_id, "error": f"invalid_uuid: {exc}"}
    with session_scope(settings.database_url) as session:
        paper = session.get(Paper, pid)
        if paper is None:
            return {"ok": False, "paper_id": paper_id, "error": "paper_not_found"}
        status = build_paper_artifact_status(paper, settings=settings)
        pdf = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
        markdown = resolve_persisted_artifact_path(paper.markdown_path, category="markdown", settings=settings)
        docling = resolve_persisted_artifact_path(paper.docling_json_path, category="docling_json", settings=settings)
        tei = resolve_persisted_artifact_path(paper.tei_path, category="tei", settings=settings)
        return {
            "ok": True,
            "paper_id": str(paper.id),
            "title": paper.title,
            "doi": paper.doi,
            "library_name": paper.library_name,
            "workflow_status": paper.workflow_status,
            "oa_status": paper.oa_status,
            "raw_artifact_fields": {
                "pdf_path": paper.pdf_path,
                "markdown_path": paper.markdown_path,
                "docling_json_path": paper.docling_json_path,
                "tei_path": paper.tei_path,
                "workspace_path": paper.workspace_path,
            },
            "artifact_status": status,
            "artifact_sizes": {
                "pdf": file_size(pdf),
                "markdown": file_size(markdown),
                "docling_json": file_size(docling),
                "grobid_tei": file_size(tei),
            },
        }


def mcp_auth() -> MCPAuthInfo:
    return MCPAuthInfo(
        source_prefix="codex_fresh_chain_acceptance",
        display_name="Codex Fresh Chain Acceptance",
        capabilities=frozenset({"read_papers", "propose_corrections"}),
        raw_key="codex_fresh_chain_acceptance",
    )


def mcp_payload_for_paper(paper_id: str) -> dict[str, Any]:
    with mcp_auth_context(mcp_auth()):
        try:
            paper_payload = mcp_get_paper(paper_id)
        except Exception as exc:
            paper_payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            context_payload = mcp_get_codex_context(paper_id)
        except Exception as exc:
            context_payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            coverage_payload = mcp_get_review_coverage(paper_id)
        except Exception as exc:
            coverage_payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    paper_status = paper_payload.get("artifact_status") if isinstance(paper_payload, dict) else None
    context = context_payload.get("context") if isinstance(context_payload, dict) else None
    context_status = context.get("artifact_status") if isinstance(context, dict) else None
    coverage_status = coverage_payload.get("artifact_status") if isinstance(coverage_payload, dict) else None
    source_distribution = (
        coverage_payload.get("external_audit_source_distribution")
        if isinstance(coverage_payload, dict)
        else None
    ) or {}
    return {
        "get_paper": paper_payload,
        "get_codex_context": context_payload,
        "get_review_coverage": coverage_payload,
        "mcp_get_paper_ready": bool((paper_status or {}).get("artifact_ready_for_external_audit")),
        "mcp_get_codex_context_ready": bool((context_status or {}).get("artifact_ready_for_external_audit")),
        "mcp_get_review_coverage_ready": bool((coverage_status or {}).get("artifact_ready_for_external_audit")),
        "coverage_visible": int(source_distribution.get(AUDIT_SOURCE, 0) or 0) > 0,
        "coverage_external_audit_count": (
            coverage_payload.get("external_audit_count") if isinstance(coverage_payload, dict) else None
        ),
        "coverage_source_distribution": source_distribution,
    }


def find_review_center_row(review_center_payload: dict[str, Any], paper_id: str) -> dict[str, Any] | None:
    payload = review_center_payload.get("json") if review_center_payload.get("ok") else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("paper_id")) == paper_id:
            return row
    return None


def api_payload_for_paper(api_base: str, paper_id: str, review_center_payload: dict[str, Any]) -> dict[str, Any]:
    detail = http_get_json(api_base, f"/api/papers/{paper_id}", timeout=60)
    codex = http_get_json(api_base, f"/api/papers/{paper_id}/codex-context", timeout=60)
    review_row = find_review_center_row(review_center_payload, paper_id)
    detail_json = detail.get("json") if detail.get("ok") else {}
    codex_json = codex.get("json") if codex.get("ok") else {}
    context = codex_json.get("context") if isinstance(codex_json, dict) else {}
    detail_status = detail_json.get("artifact_status") if isinstance(detail_json, dict) else None
    context_status = context.get("artifact_status") if isinstance(context, dict) else None
    review_status = review_row.get("artifact_status") if isinstance(review_row, dict) else None
    opinions = review_row.get("external_audit_opinions") if isinstance(review_row, dict) else []
    source_visible = any(isinstance(item, dict) and item.get("source") == AUDIT_SOURCE for item in opinions or [])
    return {
        "api_get_paper": detail,
        "api_get_codex_context": codex,
        "api_review_center_row": review_row,
        "api_get_paper_ready": bool((detail_status or {}).get("artifact_ready_for_external_audit")),
        "api_get_codex_context_ready": bool((context_status or {}).get("artifact_ready_for_external_audit")),
        "api_review_center_ready": bool((review_status or {}).get("artifact_ready_for_external_audit")),
        "review_center_visible": review_row is not None and source_visible,
        "review_center_external_audit_count": review_row.get("external_audit_count") if isinstance(review_row, dict) else None,
        "review_center_source_visible": source_visible,
    }


def db_external_audit_payload(settings, paper_ids: list[str]) -> dict[str, Any]:
    if not paper_ids:
        return {
            "ok": True,
            "run_count": 0,
            "candidate_count": 0,
            "verified_count": 0,
            "safe_verified_count": 0,
            "by_paper": {},
        }
    ids = [UUID(paper_id) for paper_id in paper_ids]
    with session_scope(settings.database_url) as session:
        rows = session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id.in_(ids),
                ExternalAnalysisCandidate.candidate_type == "external_audit_opinion",
                ExternalAnalysisRun.source == AUDIT_SOURCE,
            )
            .order_by(ExternalAnalysisCandidate.created_at.desc())
        ).all()
    by_paper: dict[str, dict[str, Any]] = {
        paper_id: {
            "external_audit_candidate_count": 0,
            "candidate_status_distribution": {},
            "verification_status_distribution": {},
            "candidate_ids": [],
            "run_ids": [],
        }
        for paper_id in paper_ids
    }
    run_ids: set[str] = set()
    verified_count = 0
    safe_verified_count = 0
    for candidate, run in rows:
        paper_id = str(candidate.paper_id)
        row = by_paper.setdefault(
            paper_id,
            {
                "external_audit_candidate_count": 0,
                "candidate_status_distribution": {},
                "verification_status_distribution": {},
                "candidate_ids": [],
                "run_ids": [],
            },
        )
        run_ids.add(str(run.id))
        row["candidate_ids"].append(str(candidate.id))
        row["run_ids"].append(str(run.id))
        row["external_audit_candidate_count"] += 1
        status = str(candidate.status or "unknown").lower()
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        evidence = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
        verification_status = str(
            payload.get("verification_status") or evidence.get("verification_status") or "unverified"
        ).lower()
        row["candidate_status_distribution"][status] = row["candidate_status_distribution"].get(status, 0) + 1
        row["verification_status_distribution"][verification_status] = (
            row["verification_status_distribution"].get(verification_status, 0) + 1
        )
        verified_count += int(status == "verified" or verification_status == "verified")
        safe_verified_count += int(status == "safe_verified" or verification_status == "safe_verified")
    return {
        "ok": True,
        "run_count": len(run_ids),
        "candidate_count": len(rows),
        "verified_count": verified_count,
        "safe_verified_count": safe_verified_count,
        "by_paper": by_paper,
    }


def ingest_fresh_papers(options: Options, settings, library_name: str) -> dict[str, Any]:
    discovery = search_discovery_candidates(options, settings)
    ingest_attempts: list[dict[str, Any]] = []
    selected_paper_ids: list[str] = []
    real_pdf_source = "unavailable"
    source_details: dict[str, Any] = {}
    service: DiscoveryService | None = None
    try:
        service = DiscoveryService()
        source_details["discovery_service_available"] = True
    except Exception as exc:
        source_details["discovery_service_available"] = False
        source_details["discovery_service_error"] = f"{type(exc).__name__}: {exc}"

    with TemporaryDirectory(prefix="fresh_realpaper_acceptance_") as tmpdir:
        temp_root = Path(tmpdir)
        for candidate in discovery.get("candidates", []):
            if len(selected_paper_ids) >= options.target_real_papers:
                break
            download = download_candidate_pdf(service, candidate, temp_root)
            attempt: dict[str, Any] = {
                "candidate": {
                    "identifier": candidate.get("identifier"),
                    "title": candidate.get("title"),
                    "doi": candidate.get("doi"),
                    "url": candidate.get("url"),
                    "pdf_url": candidate.get("pdf_url"),
                    "discovery_query": candidate.get("discovery_query"),
                },
                "download": download,
            }
            if not download.get("ok"):
                ingest_attempts.append(attempt)
                continue
            pdf_path = Path(str(download["path"]))
            identifier = candidate.get("identifier") or candidate.get("url")
            upload = upload_pdf(options.api_base, pdf_path, library_name=library_name, identifier=str(identifier or ""))
            attempt["upload"] = upload
            if upload.get("ok"):
                payload = upload.get("json") if isinstance(upload.get("json"), dict) else {}
                paper_id = payload.get("paper_id")
                if paper_id:
                    selected_paper_ids.append(str(paper_id))
                    real_pdf_source = "downloaded_by_pipeline"
            ingest_attempts.append(attempt)

    if len(selected_paper_ids) < options.min_real_papers:
        fallback_paths = existing_unclaimed_real_pdfs(settings, options.target_real_papers)
        source_details["existing_unclaimed_real_pdf_candidates"] = [str(path) for path in fallback_paths]
        for path in fallback_paths:
            if len(selected_paper_ids) >= options.target_real_papers:
                break
            upload = upload_pdf(options.api_base, path, library_name=library_name, identifier=None)
            attempt = {
                "candidate": {"path": str(path), "title": path.stem},
                "download": {"ok": True, "method": "existing_real_pdf_file", "path": str(path), "size": path.stat().st_size},
                "upload": upload,
            }
            if upload.get("ok"):
                payload = upload.get("json") if isinstance(upload.get("json"), dict) else {}
                paper_id = payload.get("paper_id")
                if paper_id:
                    selected_paper_ids.append(str(paper_id))
                    real_pdf_source = "existing_real_pdf_file"
            ingest_attempts.append(attempt)

    return {
        "real_pdf_source": real_pdf_source,
        "source_details": source_details,
        "discovery": discovery,
        "ingest_attempts": ingest_attempts,
        "paper_ids": selected_paper_ids,
    }


def verify_items(options: Options, settings, paper_ids: list[str]) -> dict[str, Any]:
    prepare_results: dict[str, Any] = {}
    local_before_import: dict[str, Any] = {}
    import_results: dict[str, Any] = {}
    for paper_id in paper_ids:
        prepare_results[paper_id] = prepare_workspace(options.api_base, paper_id)
        local_before_import[paper_id] = local_paper_payload(settings, paper_id)
        local_status = (local_before_import[paper_id].get("artifact_status") or {}) if local_before_import[paper_id].get("ok") else {}
        import_results[paper_id] = import_external_audit(
            options.api_base,
            paper_id,
            local_before_import[paper_id].get("title"),
            local_status,
        )

    runtime_debug = http_get_json(options.api_base, "/api/system/runtime-debug", timeout=20)
    review_center = http_get_json(options.api_base, "/api/workbench/review-center", params={"limit": 500}, timeout=60)
    db_audit = db_external_audit_payload(settings, paper_ids)
    items: list[dict[str, Any]] = []
    for paper_id in paper_ids:
        local_after = local_paper_payload(settings, paper_id)
        api_payload = api_payload_for_paper(options.api_base, paper_id, review_center)
        mcp_payload = mcp_payload_for_paper(paper_id)
        local_status = (local_after.get("artifact_status") or {}) if local_after.get("ok") else {}
        db_audit_row = (db_audit.get("by_paper") or {}).get(paper_id) or {}
        checks = {
            "local_ready": bool(local_status.get("artifact_ready_for_external_audit")),
            "api_get_paper_ready": bool(api_payload.get("api_get_paper_ready")),
            "api_get_codex_context_ready": bool(api_payload.get("api_get_codex_context_ready")),
            "api_review_center_ready": bool(api_payload.get("api_review_center_ready")),
            "mcp_get_paper_ready": bool(mcp_payload.get("mcp_get_paper_ready")),
            "mcp_get_codex_context_ready": bool(mcp_payload.get("mcp_get_codex_context_ready")),
            "coverage_visible": bool(mcp_payload.get("coverage_visible")),
            "review_center_visible": bool(api_payload.get("review_center_visible")),
            "external_audit_candidate_count_ok": int(db_audit_row.get("external_audit_candidate_count") or 0) >= 1,
            "verified_count_zero": int(db_audit.get("verified_count") or 0) == 0,
            "safe_verified_count_zero": int(db_audit.get("safe_verified_count") or 0) == 0,
        }
        items.append(
            {
                "paper_id": paper_id,
                "title": local_after.get("title"),
                "library_name": local_after.get("library_name"),
                "prepare_workspace": prepare_results.get(paper_id),
                "external_audit_import": import_results.get(paper_id),
                "local": local_after,
                "api": api_payload,
                "mcp": mcp_payload,
                "postgres_external_audit": db_audit_row,
                "checks": checks,
            }
        )
    return {
        "runtime_debug": runtime_debug,
        "review_center": {
            "ok": review_center.get("ok"),
            "status": review_center.get("status"),
            "error": review_center.get("error"),
            "row_count": len((review_center.get("json") or {}).get("rows") or [])
            if isinstance(review_center.get("json"), dict)
            else None,
        },
        "prepare_workspace": prepare_results,
        "external_audit_import": import_results,
        "postgres_external_audit": db_audit,
        "items": items,
    }


def classify_root_cause(report: dict[str, Any]) -> str | None:
    db_guard = (report.get("runtime") or {}).get("database_guard") or {}
    if not db_guard.get("ok"):
        if not str(db_guard.get("drivername") or "").startswith("postgresql"):
            return "legacy_sqlite_used_as_runtime_source"
        return "unknown"
    runtime_debug = (report.get("verification") or {}).get("runtime_debug") or {}
    if not runtime_debug.get("ok") or runtime_debug.get("status") == 404:
        return "api_server_not_reloaded_or_running_old_code"
    if report.get("real_pdf_source") == "unavailable":
        return "real_pdf_source_unavailable"
    if len(report.get("paper_ids") or []) < int(report.get("min_real_papers") or 1):
        attempts = ((report.get("ingestion") or {}).get("ingest_attempts") or [])
        if any((attempt.get("upload") or {}).get("status") for attempt in attempts):
            return "ingestion_failed"
        return "real_pdf_source_unavailable"

    api_runtime = runtime_debug.get("json") if runtime_debug.get("ok") else {}
    local_storage_root = ((report.get("runtime") or {}).get("local_settings") or {}).get("storage_root")
    api_storage_root = api_runtime.get("storage_root") if isinstance(api_runtime, dict) else None

    for item in (report.get("verification") or {}).get("items") or []:
        local = item.get("local") or {}
        status = local.get("artifact_status") or {}
        raw_fields = local.get("raw_artifact_fields") or {}
        if not raw_fields.get("pdf_path") or not raw_fields.get("markdown_path") or not raw_fields.get("docling_json_path"):
            return "artifact_refs_not_persisted_to_active_postgres"
        if not status.get("pdf_exists") or not status.get("pdf_file_size"):
            return "artifact_files_not_present_in_api_storage"
        if not status.get("markdown_has_content") or not status.get("docling_json_has_content"):
            return "parse_failed"
        if not status.get("workspace_exists"):
            return "workspace_not_created"
        if not status.get("ai_reading_package_exists"):
            return "ai_reading_package_missing"
        if not status.get("artifact_ready_for_external_audit") or status.get("blocking_errors") != []:
            return "parse_failed"
        imported = item.get("external_audit_import") or {}
        if not imported.get("ok"):
            return "external_audit_import_failed"
        checks = item.get("checks") or {}
        if not checks.get("api_get_paper_ready") or not checks.get("api_get_codex_context_ready") or not checks.get("api_review_center_ready"):
            if api_storage_root and local_storage_root and str(api_storage_root) != str(local_storage_root):
                return "storage_root_mismatch_between_cli_and_api"
            return "api_artifact_status_uses_different_code_path"
        if not checks.get("mcp_get_paper_ready") or not checks.get("mcp_get_codex_context_ready"):
            return "api_artifact_status_uses_different_code_path"
        if not checks.get("external_audit_candidate_count_ok"):
            return "external_audit_candidate_not_created"
        if not checks.get("coverage_visible"):
            return "coverage_not_visible"
        if not checks.get("review_center_visible"):
            return "review_center_not_visible"
        if not checks.get("verified_count_zero") or not checks.get("safe_verified_count_zero"):
            return "unknown"
    return None


def build_report(options: Options) -> dict[str, Any]:
    settings = get_settings()
    db_guard = database_guard(settings)
    library_name = options.library_prefix
    ingestion = {
        "real_pdf_source": "unavailable",
        "source_details": {},
        "discovery": {"attempts": [], "candidates": []},
        "ingest_attempts": [],
        "paper_ids": [],
    }
    verification = {
        "runtime_debug": http_get_json(options.api_base, "/api/system/runtime-debug", timeout=20),
        "items": [],
        "postgres_external_audit": {
            "run_count": 0,
            "candidate_count": 0,
            "verified_count": 0,
            "safe_verified_count": 0,
            "by_paper": {},
        },
    }

    if db_guard.get("ok"):
        library_name = unique_library_name(options.library_prefix, settings)
        runtime_probe = http_get_json(options.api_base, "/api/system/runtime-debug", timeout=20)
        if runtime_probe.get("ok"):
            ingestion = ingest_fresh_papers(options, settings, library_name)
            verification = verify_items(options, settings, ingestion["paper_ids"])
        else:
            verification["runtime_debug"] = runtime_probe

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library_prefix": options.library_prefix,
        "library_name": library_name,
        "api_base": options.api_base,
        "min_real_papers": options.min_real_papers,
        "target_real_papers": options.target_real_papers,
        "real_pdf_source": ingestion.get("real_pdf_source"),
        "paper_ids": ingestion.get("paper_ids") or [],
        "runtime": {
            "database_guard": db_guard,
            "local_settings": {
                "database_dialect": make_url(settings.database_url).drivername
                if db_guard.get("drivername")
                else None,
                "storage_root": str(settings.storage_root),
            },
        },
        "ingestion": ingestion,
        "verification": verification,
    }
    root_cause = classify_root_cause(report)
    if root_cause not in ALLOWED_ROOT_CAUSES and root_cause is not None:
        root_cause = "unknown"
    report["fresh_realpaper_chain_acceptance"] = "FAIL" if root_cause else "PASS"
    report["root_cause"] = root_cause
    return json_safe(report)


def escape_md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_markdown(report: dict[str, Any], path: Path) -> None:
    status = report["fresh_realpaper_chain_acceptance"]
    root_cause = report.get("root_cause")
    verification = report.get("verification") or {}
    db_audit = verification.get("postgres_external_audit") or {}
    lines = [
        "# Fresh Real Paper Chain Acceptance",
        "",
        f"FRESH_REALPAPER_CHAIN_ACCEPTANCE={status}",
    ]
    if root_cause:
        lines.append(f"root_cause={root_cause}")
    lines.extend(
        [
            "",
            f"- Created at: `{report.get('created_at')}`",
            f"- Library: `{report.get('library_name')}`",
            f"- API base: `{report.get('api_base')}`",
            f"- real_pdf_source: `{report.get('real_pdf_source')}`",
            f"- paper_ids: `{', '.join(report.get('paper_ids') or [])}`",
            f"- External audit source: `{AUDIT_SOURCE}`",
            f"- ExternalAnalysisRun count: `{db_audit.get('run_count')}`",
            f"- ExternalAnalysisCandidate count: `{db_audit.get('candidate_count')}`",
            f"- verified_count: `{db_audit.get('verified_count')}`",
            f"- safe_verified_count: `{db_audit.get('safe_verified_count')}`",
            "",
            "## Runtime",
            "",
            f"- Database guard: `{(report.get('runtime') or {}).get('database_guard')}`",
            f"- API runtime-debug: `{(verification.get('runtime_debug') or {}).get('status')}` ok=`{(verification.get('runtime_debug') or {}).get('ok')}`",
            f"- Review-center: `{(verification.get('review_center') or {}).get('status')}` ok=`{(verification.get('review_center') or {}).get('ok')}`",
            "",
            "## Papers",
            "",
            "| Paper ID | Title | PDF | PDF Size | Markdown | Docling JSON | TEI | Workspace | AI Package | Local Ready | API Detail | API Codex | MCP Paper | MCP Codex | Coverage | Review Center | Candidates | Blocking Errors | Warnings |",
            "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for item in verification.get("items") or []:
        local_status = ((item.get("local") or {}).get("artifact_status") or {})
        checks = item.get("checks") or {}
        db_row = item.get("postgres_external_audit") or {}
        lines.append(
            "| {paper_id} | {title} | {pdf} | {pdf_size} | {markdown} | {docling} | {tei} | {workspace} | {ai_pkg} | {local_ready} | {api_detail} | {api_codex} | {mcp_paper} | {mcp_codex} | {coverage} | {review_center} | {candidates} | {errors} | {warnings} |".format(
                paper_id=escape_md(item.get("paper_id")),
                title=escape_md(item.get("title")),
                pdf=escape_md(local_status.get("pdf_exists")),
                pdf_size=escape_md(local_status.get("pdf_file_size")),
                markdown=escape_md(local_status.get("markdown_has_content")),
                docling=escape_md(local_status.get("docling_json_has_content")),
                tei=escape_md(local_status.get("grobid_tei_has_content")),
                workspace=escape_md(local_status.get("workspace_exists")),
                ai_pkg=escape_md(local_status.get("ai_reading_package_exists")),
                local_ready=escape_md(checks.get("local_ready")),
                api_detail=escape_md(checks.get("api_get_paper_ready")),
                api_codex=escape_md(checks.get("api_get_codex_context_ready")),
                mcp_paper=escape_md(checks.get("mcp_get_paper_ready")),
                mcp_codex=escape_md(checks.get("mcp_get_codex_context_ready")),
                coverage=escape_md(checks.get("coverage_visible")),
                review_center=escape_md(checks.get("review_center_visible")),
                candidates=escape_md(db_row.get("external_audit_candidate_count")),
                errors=escape_md(", ".join(local_status.get("blocking_errors") or [])),
                warnings=escape_md(", ".join(local_status.get("warnings") or [])),
            )
        )
    lines.extend(
        [
            "",
            "## Discovery And Ingest",
            "",
            f"- Discovery candidates kept: `{len(((report.get('ingestion') or {}).get('discovery') or {}).get('candidates') or [])}`",
            f"- Existing candidates skipped: `{(((report.get('ingestion') or {}).get('discovery') or {}).get('skipped_existing_count'))}`",
            f"- Irrelevant candidates skipped: `{(((report.get('ingestion') or {}).get('discovery') or {}).get('skipped_irrelevant_count'))}`",
            f"- Ingest attempts: `{len((report.get('ingestion') or {}).get('ingest_attempts') or [])}`",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    report = build_report(options)
    write_json(options.output, report)
    write_markdown(report, options.markdown)
    print(
        json.dumps(
            {
                "status": report["fresh_realpaper_chain_acceptance"],
                "root_cause": report.get("root_cause"),
                "real_pdf_source": report.get("real_pdf_source"),
                "library_name": report.get("library_name"),
                "paper_ids": report.get("paper_ids"),
                "output": str(options.output),
                "markdown": str(options.markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["fresh_realpaper_chain_acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
