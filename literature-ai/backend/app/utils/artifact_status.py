from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.db.models import Paper
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference, resolve_persisted_artifact_path


WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _is_url(value: str | None) -> bool:
    return bool(value and URL_RE.match(value.strip()))


def _is_absolute_like(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return bool(WINDOWS_ABSOLUTE_RE.match(stripped) or stripped.startswith(("/", "\\")) or Path(stripped).is_absolute())


def _file_has_content(path: Path | None, *, json_expected: bool = False) -> bool:
    if path is None or not path.exists() or not path.is_file():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    if not json_expected:
        return True
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return True
    except OSError:
        return False
    if not text:
        return False
    try:
        payload = json.loads(text)
    except Exception:
        return True
    return payload not in ({}, [], None, "")


def _file_size(path: Path | None) -> int | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _path_kind(raw_path: str | None, resolved_path: Path | None) -> str:
    raw = str(raw_path or "").strip()
    if _is_url(raw):
        return "external_source"
    if not raw and resolved_path is None:
        return "missing"
    if _is_absolute_like(raw):
        return "absolute"
    if resolved_path is None:
        return "missing"
    return "storage_relative"


def _public_reference_from_absolute(
    raw: str,
    *,
    category: str | None,
    settings: Settings,
) -> str | None:
    raw_path = Path(raw)
    try:
        resolved = raw_path.resolve(strict=False)
        relative_to_storage = resolved.relative_to(settings.storage_root.resolve(strict=False))
        return Path("storage", relative_to_storage).as_posix()
    except (OSError, ValueError):
        pass

    normalized = raw.replace("\\", "/").lower()
    if normalized.startswith("/app/storage/"):
        canonical = canonicalize_persisted_artifact_reference(raw, category=category, settings=settings)
        if canonical:
            if canonical.startswith("storage/by_id/"):
                return canonical.removeprefix("storage/")
            return canonical
    return None


def _workspace_root(settings: Settings, paper: Paper) -> Path:
    raw_value = str(getattr(paper, "workspace_path", "") or "").strip()
    if raw_value:
        raw_path = Path(raw_value)
        if raw_path.is_absolute() or WINDOWS_ABSOLUTE_RE.match(raw_value):
            return raw_path
        return settings.storage_root / raw_path
    return settings.storage_root / "by_id" / str(paper.id)


def public_artifact_reference(
    stored_path: str | Path | None,
    *,
    category: str | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Return an API/MCP-safe artifact reference without local absolute paths."""
    if stored_path is None:
        return None
    runtime_settings = settings or get_settings()
    raw = str(stored_path).strip()
    if not raw:
        return None
    if _is_url(raw):
        return "external_source"
    if _is_absolute_like(raw):
        return _public_reference_from_absolute(raw, category=category, settings=runtime_settings) or "absolute_path_masked"

    canonical = canonicalize_persisted_artifact_reference(raw, category=category, settings=runtime_settings)
    if canonical:
        if canonical.startswith("storage/by_id/"):
            return canonical.removeprefix("storage/")
        return canonical

    return raw.replace("\\", "/")


def public_workspace_reference(paper: Paper, *, settings: Settings | None = None) -> str | None:
    runtime_settings = settings or get_settings()
    raw = str(getattr(paper, "workspace_path", "") or "").strip()
    workspace_root = _workspace_root(runtime_settings, paper)
    try:
        relative = workspace_root.resolve(strict=False).relative_to(runtime_settings.storage_root.resolve(strict=False))
        return relative.as_posix()
    except ValueError:
        if raw and not _is_absolute_like(raw):
            return raw.replace("\\", "/")
        if workspace_root.exists():
            return "absolute_path_masked"
        return f"by_id/{paper.id}"


def mask_local_absolute_paths(value: Any, *, settings: Settings | None = None) -> Any:
    """Recursively mask local absolute path strings before API/MCP serialization."""
    runtime_settings = settings or get_settings()
    if isinstance(value, dict):
        return {key: mask_local_absolute_paths(item, settings=runtime_settings) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_local_absolute_paths(item, settings=runtime_settings) for item in value]
    if isinstance(value, tuple):
        return [mask_local_absolute_paths(item, settings=runtime_settings) for item in value]
    if isinstance(value, str) and _is_absolute_like(value):
        return _public_reference_from_absolute(value, category=None, settings=runtime_settings) or "absolute_path_masked"
    return value


def build_paper_artifact_status(paper: Paper, *, settings: Settings | None = None) -> dict[str, Any]:
    """Build the external-AI artifact gate using the same persisted-path resolver as smoke checks."""
    runtime_settings = settings or get_settings()
    raw_pdf = paper.pdf_path
    pdf_path = resolve_persisted_artifact_path(raw_pdf, category="pdf", settings=runtime_settings)
    markdown_path = resolve_persisted_artifact_path(
        paper.markdown_path,
        category="markdown",
        settings=runtime_settings,
    )
    docling_path = resolve_persisted_artifact_path(
        paper.docling_json_path,
        category="docling_json",
        settings=runtime_settings,
    )
    grobid_path = resolve_persisted_artifact_path(
        paper.tei_path,
        category="tei",
        settings=runtime_settings,
    )
    workspace_root = _workspace_root(runtime_settings, paper)
    ai_package_path = workspace_root / "extraction" / "ai_reading_package.json"

    pdf_exists = bool(pdf_path and pdf_path.exists() and pdf_path.is_file())
    markdown_has_content = _file_has_content(markdown_path)
    docling_has_content = _file_has_content(docling_path, json_expected=True)
    ai_reading_package_exists = _file_has_content(ai_package_path, json_expected=True)
    quality_report = paper.pdf_quality_report if isinstance(paper.pdf_quality_report, dict) else {}
    quality_status = str(paper.pdf_quality_status or quality_report.get("quality_status") or "").strip()
    quality_reason = str(quality_report.get("reason") or "").strip()
    workflow_status = str(getattr(paper, "workflow_status", "") or "").strip()

    blocking_errors: list[str] = []
    if not pdf_exists:
        blocking_errors.append("missing_pdf")
    if quality_status == "Broken" or quality_reason.startswith("pdf_open_failed"):
        blocking_errors.append("invalid_pdf_content")
    if workflow_status in {"Needs_Human_Confirmation", "parse_failed", "needs_reingest", "metadata_only"}:
        blocking_errors.append("workflow_blocked_for_external_audit")
    if not (markdown_has_content or docling_has_content):
        blocking_errors.append("missing_markdown_and_docling_json")
    if not ai_reading_package_exists:
        blocking_errors.append("missing_ai_reading_package")

    warnings: list[str] = []
    if not _file_has_content(grobid_path):
        warnings.append("missing_or_empty_grobid_tei")
    if not workspace_root.exists():
        warnings.append("workspace_missing")
    if _path_kind(raw_pdf, pdf_path) == "absolute":
        warnings.append("absolute_pdf_path_masked")
    if quality_status:
        warnings.append(f"pdf_quality_status:{quality_status}")
    if quality_reason:
        warnings.append(f"pdf_quality_reason:{quality_reason}")

    return {
        "pdf_exists": pdf_exists,
        "pdf_file_size": _file_size(pdf_path),
        "pdf_path_kind": _path_kind(raw_pdf, pdf_path),
        "markdown_has_content": markdown_has_content,
        "docling_json_has_content": docling_has_content,
        "grobid_tei_has_content": _file_has_content(grobid_path),
        "ai_reading_package_exists": ai_reading_package_exists,
        "workspace_exists": bool(workspace_root.exists()),
        "artifact_ready_for_external_audit": not blocking_errors,
        "blocking_errors": blocking_errors,
        "warnings": warnings,
    }
