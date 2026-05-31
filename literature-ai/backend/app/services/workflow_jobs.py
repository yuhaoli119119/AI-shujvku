from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper, PaperSection, WorkflowJob
from app.db.session import session_scope
from app.schemas.api import (
    AIWorkflowFailedItemResponse,
    AIWorkflowIngestedPaperResponse,
    AIWorkflowPayload,
    AIWorkflowResponse,
    ClassifyBatchPayload,
)
from app.services.discovery_service import DiscoveryService
from app.services.paper_identity import PaperIdentityService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.library_names import (
    DEFAULT_LIBRARY_ALIASES,
    DEFAULT_LIBRARY_NAME,
    build_library_name_clause,
    library_name_variants,
    normalize_library_name,
)


logger = logging.getLogger(__name__)
JOB_TYPE_AI_WORKFLOW = "ai_workflow"
JOB_TYPE_CLASSIFY_BATCH = "classify_batch"
JOB_TYPE_EXTRACTION = "extraction"
JOB_STATUSES = {"queued", "running", "completed", "failed", "cancelled"}
ACTIVE_JOB_STATUSES = {"queued", "running"}

FAILURE_MESSAGES: dict[str, tuple[str, str]] = {
    "missing_identifier": ("缺少可检索标识", "请补充 DOI、URL、arXiv ID 或更完整的题名后再试。"),
    "missing_doi": ("未识别到 DOI", "可先按标题/作者保留元数据，之后手动补 DOI 或上传正确 PDF。"),
    "multiple_doi": ("检测到多个 DOI", "请打开论文详情核对 DOI，必要时手动修正后再解析。"),
    "doi_conflict": ("DOI 与已有文献冲突", "请检查是否选错 PDF，或打开已存在文献继续补全。"),
    "pdf_missing": ("PDF 文件不存在", "请重新上传 PDF，或先保留元数据并稍后补全文献文件。"),
    "pdf_unavailable": ("当前论文没有可解析 PDF", "请先上传 PDF；如果只有元数据，解析任务会等待文件补全。"),
    "pdf_preview_failed": ("PDF 无法预览或渲染", "请确认 PDF 文件完整、未加密，必要时重新下载后重试。"),
    "parsed_text_missing": ("缺少可解析正文", "系统没有找到 TEI、Docling 或正文分段，请重新上传/解析 PDF 后再重试。"),
    "tei_parse_failed": ("TEI/GROBID 解析失败", "可稍后重试；如果反复失败，建议检查 GROBID 服务或改用 Docling 解析结果。"),
    "docling_parse_failed": ("Docling 解析失败", "可重试解析；若仍失败，优先检查 PDF 是否为扫描件、加密文件或损坏文件。"),
    "download_failed": ("下载或开放获取失败", "可换 DOI/URL 重试，或先导入元数据后手动上传 PDF。"),
    "paper_not_found": ("文献记录不存在", "请刷新文献列表，确认该论文仍在当前文献库中。"),
    "llm_failed": ("LLM 解析失败", "请检查模型配置或稍后重试；已保存的原文与证据不会被删除。"),
    "schema_invalid": ("模型输出不符合 schema", "请重试解析；如持续失败，可缩小解析 schema 范围。"),
    "job_error": ("任务执行失败", "请查看原始错误信息后重试。"),
}


class JobCancelledError(RuntimeError):
    pass


class JobPreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_job_status(status: str) -> str:
    if status not in JOB_STATUSES:
        raise ValueError(f"Unsupported workflow job status: {status}")
    return status


def build_job_runtime_context(settings: Settings) -> dict[str, Any]:
    return {
        "database_url": settings.database_url,
        "storage_root": str(settings.storage_root),
    }


def build_runtime_settings(base_settings: Settings, runtime_context: dict[str, Any] | None) -> Settings:
    context = runtime_context or {}
    return base_settings.model_copy(
        update={
            "database_url": context.get("database_url", base_settings.database_url),
            "storage_root": Path(context["storage_root"]) if context.get("storage_root") else base_settings.storage_root,
        }
    )


def serialize_job(job: WorkflowJob) -> dict[str, Any]:
    summary = build_job_summary(job)
    return {
        "job_id": job.job_id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress or {},
        "result": job.result,
        "error": job.error,
        "summary": summary,
        "failure_explanation": build_job_failure_explanation(job, summary),
        "created_at": job.created_at.replace(tzinfo=timezone.utc).isoformat() if job.created_at else None,
        "updated_at": job.updated_at.replace(tzinfo=timezone.utc).isoformat() if job.updated_at else None,
        "library_name": normalize_library_name(job.library_name),
    }


def _safe_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _first_int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _schema_total(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    return sum(int(count or 0) for count in value.values() if isinstance(count, int | float))


def _count_statuses(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _failure_items(job: WorkflowJob) -> list[dict[str, Any]]:
    result = job.result if isinstance(job.result, dict) else {}
    failed = result.get("failed")
    if isinstance(failed, list):
        return [item for item in failed if isinstance(item, dict)]
    failed_items = result.get("failed_items")
    if isinstance(failed_items, list):
        return [item for item in failed_items if isinstance(item, dict)]
    return []


def classify_failure_code(code: str | None = None, reason: str | None = None) -> str:
    raw_code = (code or "").strip().lower()
    text = f"{raw_code} {reason or ''}".lower()
    if raw_code in FAILURE_MESSAGES and raw_code not in {"job_error", "unknown"}:
        return raw_code
    if "paper not found" in text:
        return "paper_not_found"
    if "doi" in text and "conflict" in text:
        return "doi_conflict"
    if "doi" in text and ("multiple" in text or "more than one" in text):
        return "multiple_doi"
    if "doi" in text and any(keyword in text for keyword in ("missing", "none", "empty", "no ")):
        return "missing_doi"
    if "pdf" in text and any(keyword in text for keyword in ("missing", "not found", "does not exist")):
        return "pdf_missing"
    if "pdf" in text and any(keyword in text for keyword in ("preview", "render", "image", "open", "encrypted")):
        return "pdf_preview_failed"
    if "metadata_only" in text or "needs_upload" in text:
        return "pdf_unavailable"
    if "tei" in text or "grobid" in text:
        return "tei_parse_failed"
    if "docling" in text:
        return "docling_parse_failed"
    if "download" in text or "open access" in text or "oa " in text:
        return "download_failed"
    if "schema" in text or "validation" in text or "json" in text:
        return "schema_invalid"
    if "llm" in text or "model" in text or "api key" in text:
        return "llm_failed"
    if "section" in text or "parsed text" in text or "no text" in text:
        return "parsed_text_missing"
    if raw_code in FAILURE_MESSAGES:
        return raw_code
    return raw_code or "job_error"


def _human_failure_reason(code: str, reason: str) -> tuple[str, str]:
    normalized_code = classify_failure_code(code, reason)
    return FAILURE_MESSAGES.get(normalized_code, FAILURE_MESSAGES["job_error"])


def build_job_summary(job: WorkflowJob) -> dict[str, Any]:
    progress = job.progress if isinstance(job.progress, dict) else {}
    payload = job.payload if isinstance(job.payload, dict) else {}
    result = job.result if isinstance(job.result, dict) else {}
    failed_items = _failure_items(job)

    summary: dict[str, Any] = {
        "type": job.type,
        "status": job.status,
        "phase": progress.get("phase"),
        "message": progress.get("message"),
        "query": payload.get("query"),
        "source": payload.get("source") or job.type,
        "library_name": job.library_name,
        "created_at": job.created_at.replace(tzinfo=timezone.utc).isoformat() if job.created_at else None,
        "updated_at": job.updated_at.replace(tzinfo=timezone.utc).isoformat() if job.updated_at else None,
        "retried_from_job_id": progress.get("retried_from_job_id"),
    }
    if job.created_at and job.updated_at:
        summary["duration_seconds"] = max(0, int((job.updated_at - job.created_at).total_seconds()))

    if job.type == JOB_TYPE_AI_WORKFLOW:
        ingested = result.get("ingested") if isinstance(result.get("ingested"), list) else []
        status_counts = _count_statuses(ingested)
        summary.update(
            {
                "source_label": "AI 文献检索与入库",
                "searched_total": _first_int(progress.get("searched_total"), result.get("searched_total")),
                "attempted_downloads": _first_int(
                    progress.get("attempted_downloads"), result.get("attempted_downloads")
                ),
                "success_count": _first_int(progress.get("ingested"), _safe_len(ingested)),
                "failure_count": _first_int(progress.get("failed"), _safe_len(failed_items)),
                "already_exists_count": status_counts.get("already_exists", 0),
                "metadata_only_count": status_counts.get("metadata_only", 0),
                "merged_count": status_counts.get("merged", 0),
                "completed_count": status_counts.get("completed", 0),
                "skipped_count": status_counts.get("skipped", 0),
                "max_results": progress.get("max_results") or payload.get("max_results"),
                "max_downloads": progress.get("max_downloads") or payload.get("max_downloads"),
            }
        )
    elif job.type == JOB_TYPE_EXTRACTION:
        extraction_summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        if not extraction_summary:
            extraction_summary = {key: value for key, value in progress.items() if isinstance(value, int | float)}
        summary.update(
            {
                "source_label": "论文结构化解析",
                "paper_id": payload.get("paper_id") or progress.get("paper_id"),
                "schemas": payload.get("schemas") or progress.get("schemas") or result.get("schemas"),
                "success_count": _schema_total(extraction_summary),
                "failure_count": 1 if job.status == "failed" else 0,
                "extracted_counts": extraction_summary,
                "force": payload.get("force"),
            }
        )
    elif job.type == JOB_TYPE_CLASSIFY_BATCH:
        summary.update(
            {
                "source_label": "批量文献分类",
                "total": _first_int(progress.get("total"), result.get("total")),
                "success_count": _first_int(progress.get("completed"), result.get("classified")),
                "failure_count": _first_int(progress.get("failed"), result.get("failed_count"), _safe_len(failed_items)),
            }
        )
    else:
        summary.update({"source_label": job.type, "failure_count": 1 if job.status == "failed" else 0})

    return summary


def build_job_failure_explanation(job: WorkflowJob, summary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    failed_items = _failure_items(job)
    reason_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in failed_items:
        code = classify_failure_code(str(item.get("code") or item.get("status") or ""), str(item.get("reason") or item.get("error") or ""))
        reason = str(item.get("reason") or item.get("error") or "")
        label, suggestion = _human_failure_reason(code, reason)
        key = (code, label)
        if key not in reason_groups:
            reason_groups[key] = {
                "code": code,
                "label": label,
                "suggestion": suggestion,
                "count": 0,
                "examples": [],
            }
        reason_groups[key]["count"] += 1
        title = item.get("title") or item.get("identifier") or item.get("paper_id")
        if title and len(reason_groups[key]["examples"]) < 3:
            reason_groups[key]["examples"].append(str(title))

    if job.error:
        progress = job.progress if isinstance(job.progress, dict) else {}
        code = classify_failure_code(str(progress.get("failure_code") or "job_error"), job.error)
        label, suggestion = _human_failure_reason(code, job.error)
        reason_groups[(code, label)] = {
            "code": code,
            "label": label,
            "suggestion": suggestion,
            "count": 1,
            "examples": [job.error],
        }

    if not reason_groups:
        return None

    reasons = sorted(reason_groups.values(), key=lambda item: item["count"], reverse=True)
    failure_count = (summary or {}).get("failure_count") or sum(item["count"] for item in reasons)
    return {
        "summary": f"{failure_count} 个失败项，主要原因：{reasons[0]['label']}",
        "reasons": reasons,
        "can_retry": job.status in {"failed", "cancelled"},
    }


def _merge_progress(existing: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    merged.update(updates)
    return merged


def create_job(
    session: Session,
    *,
    job_type: str,
    library_name: str | None,
    payload: dict[str, Any],
    runtime_context: dict[str, Any],
    progress: dict[str, Any],
) -> WorkflowJob:
    job = WorkflowJob(
        job_id=str(uuid4()),
        type=job_type,
        status="queued",
        progress=progress,
        library_name=normalize_library_name(library_name),
        payload=payload,
        runtime_context=runtime_context,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _normalized_job_key(job_type: str, library_name: str | None, payload: dict[str, Any] | None) -> tuple[Any, ...]:
    data = payload or {}
    library = normalize_library_name(library_name)
    if job_type == JOB_TYPE_AI_WORKFLOW:
        query = str(data.get("query") or "").strip().lower()
        return (job_type, library, query)
    if job_type == JOB_TYPE_EXTRACTION:
        return (job_type, library, str(data.get("paper_id") or ""))
    if job_type == JOB_TYPE_CLASSIFY_BATCH:
        return (job_type, library, bool(data.get("overwrite")))
    return (job_type, library)


def find_active_equivalent_job(
    session: Session,
    *,
    job_type: str,
    library_name: str | None,
    payload: dict[str, Any] | None,
    exclude_job_id: str | None = None,
) -> WorkflowJob | None:
    target_key = _normalized_job_key(job_type, library_name, payload)
    stmt = (
        select(WorkflowJob)
        .where(WorkflowJob.type == job_type)
        .where(build_library_name_clause(WorkflowJob.library_name, library_name))
        .where(WorkflowJob.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(desc(WorkflowJob.created_at))
        .limit(100)
    )
    for job in session.scalars(stmt).all():
        if exclude_job_id and job.job_id == exclude_job_id:
            continue
        if _normalized_job_key(job.type, job.library_name, job.payload if isinstance(job.payload, dict) else {}) == target_key:
            return job
    return None


def create_job_or_reuse_active(
    session: Session,
    *,
    job_type: str,
    library_name: str | None,
    payload: dict[str, Any],
    runtime_context: dict[str, Any],
    progress: dict[str, Any],
) -> tuple[WorkflowJob, bool]:
    active = find_active_equivalent_job(
        session,
        job_type=job_type,
        library_name=library_name,
        payload=payload,
    )
    if active is not None:
        return active, True
    return (
        create_job(
            session,
            job_type=job_type,
            library_name=library_name,
            payload=payload,
            runtime_context=runtime_context,
            progress=progress,
        ),
        False,
    )


def list_jobs(
    session: Session,
    *,
    job_type: str | None = None,
    library_name: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[WorkflowJob]:
    stmt = select(WorkflowJob)
    if job_type:
        stmt = stmt.where(WorkflowJob.type == job_type)
    if library_name:
        stmt = stmt.where(build_library_name_clause(WorkflowJob.library_name, library_name))
    if status:
        stmt = stmt.where(WorkflowJob.status == validate_job_status(status))
    stmt = stmt.order_by(desc(WorkflowJob.created_at)).limit(limit)
    return list(session.scalars(stmt).all())


def get_job(session: Session, job_id: str) -> WorkflowJob | None:
    return session.get(WorkflowJob, job_id)


def get_job_or_raise(session: Session, job_id: str) -> WorkflowJob:
    job = get_job(session, job_id)
    if job is None:
        raise ValueError(f"Workflow job not found: {job_id}")
    return job


def update_job(
    session: Session,
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: Any = None,
    error: str | None = None,
) -> WorkflowJob:
    job = get_job_or_raise(session, job_id)
    if status is not None:
        job.status = validate_job_status(status)
    if progress is not None:
        job.progress = progress
    if result is not None or status == "completed":
        job.result = result
    if error is not None or status == "completed":
        job.error = error
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def is_job_cancelled(session: Session, job_id: str) -> bool:
    job = get_job(session, job_id)
    return bool(job and job.status == "cancelled")


def assert_job_not_cancelled(session: Session, job_id: str) -> None:
    if is_job_cancelled(session, job_id):
        raise JobCancelledError(f"Workflow job cancelled: {job_id}")


def cancel_job(session: Session, job_id: str) -> WorkflowJob:
    job = get_job_or_raise(session, job_id)
    if job.status not in {"queued", "running"}:
        raise ValueError(f"Only queued or running jobs can be cancelled: {job.status}")

    message = "Cancellation requested."
    if job.status == "running":
        message = "Soft cancel requested while task is running."

    job.status = "cancelled"
    job.progress = _merge_progress(
        job.progress,
        {
            "phase": "cancelled",
            "message": message,
            "cancel_mode": "soft",
        },
    )
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def clone_job_for_retry_with_status(session: Session, job_id: str) -> tuple[WorkflowJob, bool]:
    source = get_job_or_raise(session, job_id)
    if source.status not in {"failed", "cancelled"}:
        raise ValueError(f"Only failed or cancelled jobs can be retried: {source.status}")

    retry_payload = dict(source.payload or {})
    active = find_active_equivalent_job(
        session,
        job_type=source.type,
        library_name=source.library_name,
        payload=retry_payload,
        exclude_job_id=source.job_id,
    )
    if active is not None:
        return active, True

    retry_progress = {
        "phase": "queued",
        "message": f"Retry queued from job {source.job_id}.",
        "retried_from_job_id": source.job_id,
    }
    if source.type == JOB_TYPE_AI_WORKFLOW:
        retry_payload["skip_existing"] = True
        retry_progress.update(
            {
                "max_results": retry_payload.get("max_results"),
                "max_downloads": retry_payload.get("max_downloads"),
            }
        )
    if source.type == JOB_TYPE_EXTRACTION:
        retry_progress.update({"paper_id": retry_payload.get("paper_id"), "schemas": retry_payload.get("schemas")})

    retry = create_job(
        session,
        job_type=source.type,
        library_name=source.library_name,
        payload=retry_payload,
        runtime_context=dict(source.runtime_context or {}),
        progress=retry_progress,
    )
    return retry, False


def clone_job_for_retry(session: Session, job_id: str) -> WorkflowJob:
    retry, _ = clone_job_for_retry_with_status(session, job_id)
    return retry


def _find_existing_paper(
    session: Session,
    doi: str | None,
    title: str | None,
    year: int | None = None,
    arxiv_id: str | None = None,
    library_name: str | None = None,
) -> Paper | None:
    identity = PaperIdentityService()
    existing = identity.find_existing_paper(
        session,
        doi=identity.normalize_doi(doi),
        title=title,
        year=year,
        arxiv_id=arxiv_id,
        library_name=library_name,
    )
    if existing is not None:
        return existing
    if doi:
        return identity.find_existing_paper(
            session,
            doi=identity.normalize_doi(doi),
            title=title,
            year=year,
            arxiv_id=arxiv_id,
            library_name=None,
        )
    return None


def validate_extraction_preflight(
    session: Session,
    *,
    paper_id: UUID,
    settings: Settings,
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise JobPreflightError("paper_not_found", "Paper not found")

    pdf_reference = (paper.pdf_path or "").strip()
    pdf_path = (
        resolve_persisted_artifact_path(pdf_reference, category="pdf", settings=settings, must_exist=True)
        if pdf_reference
        else None
    )
    has_pdf = pdf_path is not None and paper.oa_status not in {"metadata_only", "needs_upload"}

    if not pdf_reference or paper.oa_status in {"metadata_only", "needs_upload"}:
        raise JobPreflightError(
            "pdf_unavailable",
            "Current paper only has metadata. Upload a PDF before running extraction.",
        )
    if pdf_path is None:
        raise JobPreflightError(
            "pdf_missing",
            f"Stored PDF reference cannot be resolved: {pdf_reference}",
        )

    has_text = (
        session.scalar(
            select(PaperSection.id)
            .where(PaperSection.paper_id == paper_id)
            .where(PaperSection.section_type.not_in(["table", "figure_caption"]))
            .limit(1)
        )
        is not None
    )

    if not has_text:
        raise JobPreflightError(
            "parsed_text_missing",
            "No parsed TEI/Docling/body sections are available for schema extraction.",
        )

    return {
        "paper_id": str(paper_id),
        "has_parsed_text": has_text,
        "pdf_available": has_pdf,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "oa_status": paper.oa_status,
    }


async def download_discovery_candidate(
    service: DiscoveryService,
    raw_paper: Any,
    metadata: dict[str, object],
    dest_dir: Path,
) -> Path:
    try:
        return await run_in_threadpool(service.download_pdf, raw_paper, dest_dir)
    except Exception as primary_exc:
        pdf_url = metadata.get("pdf_url") or metadata.get("oa_url") or metadata.get("url")
        if not pdf_url:
            raise primary_exc
        filename = f"{uuid4()}.pdf"
        try:
            return await run_in_threadpool(service.download_pdf_url, str(pdf_url), dest_dir, filename)
        except Exception:
            raise primary_exc


async def execute_ai_workflow(
    payload: AIWorkflowPayload,
    *,
    session: Session,
    settings: Settings,
    job_id: str | None = None,
) -> AIWorkflowResponse:
    from app.api.papers.common import rewrite_ai_search_query

    prompt_used, llm_status, llm_error, llm_diagnostics = rewrite_ai_search_query(
        payload.query,
        payload.model,
        settings,
    )

    service = DiscoveryService()
    active_providers = payload.providers or service.DEFAULT_SEARCH_PROVIDERS
    raw_results = await run_in_threadpool(
        service.search,
        prompt_used,
        active_providers,
        payload.max_results,
    )

    ingestion = PaperIngestionService(session=session, settings=settings)
    target_library = normalize_library_name(payload.library_name)
    ingested: list[AIWorkflowIngestedPaperResponse] = []
    failed: list[AIWorkflowFailedItemResponse] = []
    attempted_downloads = 0

    for item in raw_results:
        if job_id:
            assert_job_not_cancelled(session, job_id)
        if attempted_downloads >= payload.max_downloads:
            break

        identifier = item.get("doi") or item.get("url") or item.get("identifier") or item.get("title") or ""
        if not identifier:
            failed.append(
                AIWorkflowFailedItemResponse(
                    identifier="",
                    title=item.get("title"),
                    code="missing_identifier",
                    reason="missing_identifier",
                )
            )
            continue

        doi = item.get("doi")
        existing = _find_existing_paper(
            session,
            doi=doi if payload.skip_existing else None,
            title=item.get("title") if payload.skip_existing else None,
            year=item.get("year") if payload.skip_existing else None,
            arxiv_id=PaperIdentityService.extract_arxiv_id(str(identifier)) if payload.skip_existing else None,
            library_name=target_library,
        )
        if payload.skip_existing and existing:
            if existing.library_name != target_library:
                existing.library_name = target_library
                session.add(existing)
                session.commit()
                session.refresh(existing)
            ingested.append(
                AIWorkflowIngestedPaperResponse(
                    paper_id=existing.id,
                    title=existing.title,
                    status="already_exists",
                    identifier=identifier,
                    doi=doi,
                )
            )
            continue

        attempted_downloads += 1
        try:
            raw_paper, metadata = await run_in_threadpool(
                service.fetch_metadata, identifier, active_providers
            )
            existing = (
                _find_existing_paper(
                    session,
                    doi=metadata.get("doi"),
                    title=metadata.get("title"),
                    year=metadata.get("year"),
                    arxiv_id=PaperIdentityService.extract_arxiv_id(
                        str(metadata.get("arxiv_id") or metadata.get("identifier") or metadata.get("url") or identifier)
                    ),
                    library_name=target_library,
                )
                if payload.skip_existing
                else None
            )
            if existing:
                if existing.library_name != target_library:
                    existing.library_name = target_library
                    session.add(existing)
                    session.commit()
                    session.refresh(existing)
                ingested.append(
                    AIWorkflowIngestedPaperResponse(
                        paper_id=existing.id,
                        title=existing.title,
                        status="already_exists",
                        identifier=identifier,
                        doi=metadata.get("doi"),
                    )
                )
                continue

            item_status = "completed"
            try:
                with TemporaryDirectory() as tmpdir:
                    pdf_path = await download_discovery_candidate(
                        service,
                        raw_paper,
                        metadata,
                        Path(tmpdir),
                    )
                    paper = await ingestion.ingest_pdf(
                        source_path=pdf_path,
                        original_filename=pdf_path.name,
                        copy_pdf=True,
                        external_metadata=metadata,
                        source_reference=None,
                        library_name=target_library,
                    )
            except Exception:
                paper = ingestion.ingest_metadata_only(
                    external_metadata=metadata,
                    identifier=identifier,
                    library_name=target_library,
                    source_reference=metadata.get("url") or identifier,
                )
                item_status = "metadata_only"

            ingested.append(
                AIWorkflowIngestedPaperResponse(
                    paper_id=paper.id,
                    title=paper.title,
                    status=item_status,
                    identifier=identifier,
                    doi=paper.doi,
                )
            )
        except Exception as exc:
            failure_code = classify_failure_code("download_or_ingest_failed", str(exc))
            failed.append(
                AIWorkflowFailedItemResponse(
                    identifier=identifier,
                    title=item.get("title"),
                    code=failure_code,
                    reason=str(exc),
                )
            )

    return AIWorkflowResponse(
        query=payload.query,
        prompt_used=prompt_used,
        providers=active_providers,
        searched_total=len(raw_results),
        attempted_downloads=attempted_downloads,
        ingested=ingested,
        failed=failed,
        llm_status=llm_status,
        llm_error=llm_error,
        llm_diagnostics=llm_diagnostics,
    )


def run_ai_workflow_job(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = AIWorkflowPayload.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "search_and_ingest",
                "message": "AI workflow is searching, deduplicating, downloading, and metadata-ingesting failures.",
                "max_results": payload.max_results,
                "max_downloads": payload.max_downloads,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            result = asyncio.run(
                execute_ai_workflow(
                    payload,
                    session=job_session,
                    settings=runtime_settings,
                    job_id=job_id,
                )
            )
            assert_job_not_cancelled(job_session, job_id)

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "searched_total": result.searched_total,
                    "attempted_downloads": result.attempted_downloads,
                    "ingested": len(result.ingested),
                    "failed": len(result.failed),
                },
                result=result.model_dump(mode="json"),
                error=None,
            )
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except Exception as exc:
        logger.exception("AI workflow job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={"phase": "failed", "failure_code": classify_failure_code(reason=str(exc))},
                error=f"{type(exc).__name__}: {exc}",
            )


def run_classify_batch_sync(
    payload: ClassifyBatchPayload,
    *,
    session: Session,
    settings: Settings,
) -> dict[str, Any]:
    target_library = normalize_library_name(payload.library_name)
    stmt = select(Paper).where(build_library_name_clause(Paper.library_name, target_library))
    if not payload.overwrite:
        stmt = stmt.where((Paper.paper_type.is_(None)) | (Paper.paper_type == "Unknown"))

    papers = list(session.scalars(stmt).all())
    total = len(papers)
    failed_items = []
    classified_count = 0
    reprocess = PaperReprocessingService(session=session, settings=settings)

    for paper in papers:
        try:
            reprocess.classify_single_paper(paper.id, payload.overwrite)
            classified_count += 1
        except Exception as exc:
            failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(exc)})

    return {
        "status": "completed",
        "total": total,
        "classified": classified_count,
        "failed_count": len(failed_items),
        "failed_items": failed_items,
    }


def run_classify_batch_job(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = ClassifyBatchPayload.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "classify_batch",
                "message": "Initializing batch classification job.",
                "completed": 0,
                "total": 0,
                "failed": 0,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            target_library = normalize_library_name(payload.library_name)
            stmt = select(Paper).where(build_library_name_clause(Paper.library_name, target_library))
            if not payload.overwrite:
                stmt = stmt.where((Paper.paper_type.is_(None)) | (Paper.paper_type == "Unknown"))

            papers = list(job_session.scalars(stmt).all())
            total = len(papers)
            failed_items = []
            classified_count = 0
            reprocess = PaperReprocessingService(session=job_session, settings=runtime_settings)

            with session_scope(control_db_url) as control_session:
                update_job(
                    control_session,
                    job_id,
                    progress={
                        "phase": "classify_batch",
                        "message": f"Found {total} papers to classify.",
                        "completed": 0,
                        "total": total,
                        "failed": 0,
                    },
                )

            for index, paper in enumerate(papers, start=1):
                assert_job_not_cancelled(job_session, job_id)
                try:
                    reprocess.classify_single_paper(paper.id, payload.overwrite)
                    classified_count += 1
                except Exception as exc:
                    logger.warning("Failed to classify paper %s: %s", paper.id, exc)
                    failed_items.append({"paper_id": str(paper.id), "title": paper.title, "error": str(exc)})

                with session_scope(control_db_url) as control_session:
                    update_job(
                        control_session,
                        job_id,
                        progress={
                            "phase": "classify_batch",
                            "message": f"Classified {index}/{total} papers.",
                            "completed": index,
                            "total": total,
                            "failed": len(failed_items),
                        },
                    )

                if index < total and payload.interval > 0:
                    asyncio.run(asyncio.sleep(payload.interval))

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "message": "Successfully completed batch classification.",
                    "completed": total,
                    "total": total,
                    "failed": len(failed_items),
                },
                result={
                    "total": total,
                    "classified": classified_count,
                    "failed_count": len(failed_items),
                    "failed_items": failed_items,
                },
                error=None,
            )
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except Exception as exc:
        logger.exception("Batch classification job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={"phase": "failed", "failure_code": classify_failure_code(reason=str(exc))},
                error=f"{type(exc).__name__}: {exc}",
            )


def run_extraction_job(job_id: str, control_database_url: str | None = None) -> None:
    from app.schemas.extraction import ExtractionJobRequest

    base_settings = get_settings()
    control_db_url = control_database_url or base_settings.database_url
    with session_scope(control_db_url) as control_session:
        job = get_job_or_raise(control_session, job_id)
        if job.status == "cancelled":
            return
        runtime_settings = build_runtime_settings(base_settings, job.runtime_context)
        payload = ExtractionJobRequest.model_validate(job.payload or {})
        update_job(
            control_session,
            job_id,
            status="running",
            progress={
                "phase": "preflight",
                "message": "Checking paper text/PDF readiness before extraction.",
                "paper_id": str(payload.paper_id),
                "schemas": payload.schemas,
            },
            error=None,
        )

    try:
        with session_scope(runtime_settings.database_url) as job_session:
            assert_job_not_cancelled(job_session, job_id)
            preflight = validate_extraction_preflight(
                job_session,
                paper_id=payload.paper_id,
                settings=runtime_settings,
            )
            with session_scope(control_db_url) as control_session:
                update_job(
                    control_session,
                    job_id,
                    progress={
                        "phase": "extraction",
                        "message": "Running schema-driven scientific extraction.",
                        "paper_id": str(payload.paper_id),
                        "schemas": payload.schemas,
                        "preflight": preflight,
                    },
                )
            reprocess = PaperReprocessingService(session=job_session, settings=runtime_settings)
            summary = reprocess.rerun_stage2(payload.paper_id)
            assert_job_not_cancelled(job_session, job_id)

        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="completed",
                progress={
                    "phase": "completed",
                    "message": "Extraction completed.",
                    "paper_id": str(payload.paper_id),
                    "preflight": preflight,
                    **summary,
                },
                result={"paper_id": str(payload.paper_id), "summary": summary, "schemas": payload.schemas},
                error=None,
            )
    except JobCancelledError:
        with session_scope(control_db_url) as control_session:
            job = get_job(control_session, job_id)
            if job and job.status != "cancelled":
                update_job(
                    control_session,
                    job_id,
                    status="cancelled",
                    progress=_merge_progress(job.progress, {"phase": "cancelled", "cancel_mode": "soft"}),
                    error=None,
                )
    except JobPreflightError as exc:
        logger.warning("Extraction job preflight failed for %s: %s", job_id, exc)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={
                    "phase": "preflight_failed",
                    "message": str(exc),
                    "paper_id": str(payload.paper_id),
                    "schemas": payload.schemas,
                    "failure_code": exc.code,
                },
                error=f"{exc.code}: {exc}",
            )
    except Exception as exc:
        logger.exception("Extraction job failed: %s", job_id)
        with session_scope(control_db_url) as control_session:
            update_job(
                control_session,
                job_id,
                status="failed",
                progress={
                    "phase": "failed",
                    "paper_id": str(payload.paper_id),
                    "schemas": payload.schemas,
                    "failure_code": classify_failure_code(reason=str(exc)),
                },
                error=f"{type(exc).__name__}: {exc}",
            )


def dispatch_job(
    job_id: str,
    background_tasks: BackgroundTasks | None = None,
    *,
    control_database_url: str | None = None,
) -> str:
    from kombu import Connection

    from app.workers.tasks import run_workflow_job_task

    try:
        with Connection(get_settings().celery_broker_url, connect_timeout=1) as connection:
            connection.ensure_connection(max_retries=0)
        run_workflow_job_task.delay(job_id)
        return "celery"
    except Exception as exc:
        logger.warning("Celery dispatch failed for job %s, falling back to in-process background task: %s", job_id, exc)
        if background_tasks is None:
            raise
        background_tasks.add_task(run_workflow_job_by_id, job_id, control_database_url)
        return "background_tasks"


def run_workflow_job_by_id(job_id: str, control_database_url: str | None = None) -> None:
    base_settings = get_settings()
    job_database_url = control_database_url or base_settings.database_url
    with session_scope(job_database_url) as session:
        job = get_job_or_raise(session, job_id)
        if job.status == "cancelled":
            return
        job_type = job.type

    if job_type == JOB_TYPE_AI_WORKFLOW:
        run_ai_workflow_job(job_id, control_database_url)
        return
    if job_type == JOB_TYPE_CLASSIFY_BATCH:
        run_classify_batch_job(job_id, control_database_url)
        return
    if job_type == JOB_TYPE_EXTRACTION:
        run_extraction_job(job_id, control_database_url)
        return
    raise ValueError(f"Unsupported workflow job type: {job_type}")
