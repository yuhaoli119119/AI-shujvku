from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.settings import sync_writer_settings_from_session
from app.config import Settings, get_settings
from app.db.models import (
    AuditLog,
    Base,
    CatalystSample,
    DFTResult,
    ExternalAnalysisCandidate,
    ExtractionFieldReview,
    Paper,
    PaperFigure,
    WorkflowJob,
)
from app.db.session import get_db_session
from app.schemas.api import (
    CodexContextResponse,
    CodexItemContextResponse,
    DFTResultCorrectionProposalRequest,
    DFTResultCorrectionProposalResponse,
    DFTResultManualUpdateRequest,
    DFTResultManualUpdateResponse,
    DFTResultRejectRequest,
    DFTResultRejectResponse,
    DFTResultVerifyRequest,
    DFTResultVerifyResponse,
    ExtractionRunResponse,
    PaperDetailResponse,
    PaperKnowledgeContextResponse,
    PaperTranslationItemResponse,
    PaperTranslationPreviewRequest,
    PaperTranslationPreviewResponse,
)
from app.services.paper_codes import next_supplementary_paper_code, supplementary_base_code
from pydantic import BaseModel, Field

class RelationshipCreateRequest(BaseModel):
    target_paper_id: str
    relationship_type: str
    note: str | None = None

class RelationshipCreateResponse(BaseModel):
    status: str
    id: UUID


class DFTImportedOpinionApplyRequest(BaseModel):
    opinion: dict[str, Any]
    reviewer: str | None = None
    expected_row_state: dict[str, Any] | None = None
    expected_write_versions: dict[str, int] = Field(default_factory=dict)


class FigureDeleteProposalRequest(BaseModel):
    confirm_delete_proposal: bool
    reason: str
    reviewer: str | None = None
    evidence_payload: dict[str, Any] | list[Any] | None = None


class FigureDirectDeleteRequest(BaseModel):
    confirm_direct_delete: bool
    reason: str
    reviewer: str | None = None
    evidence_payload: dict[str, Any] | list[Any] | None = None
    delete_image_file: bool = True


class ManualReviewProgressRequest(BaseModel):
    module: str
    completed: bool
    reviewer: str | None = None


class DFTAIReviewResetRequest(BaseModel):
    confirm_reset_dft_ai_reviews: bool
    reviewer: str | None = None
    keep_dft_candidates: bool = True


class CatalystBasicInfoUpdateRequest(BaseModel):
    name: str | None = None
    catalyst_type: str | None = None
    metal_centers: list[str] | None = None
    coordination: str | None = None
    support: str | None = None
    synthesis_method: str | None = None
    evidence_strength: str | None = None
    source: str | None = None
    reviewer: str | None = None
    evidence_payload: dict[str, Any] | None = None
    note: str | None = None


class CatalystBasicInfoCreateFromDFTRequest(CatalystBasicInfoUpdateRequest):
    dft_result_ids: list[UUID] = Field(min_length=1)

from app.services.codex_context_service import CodexContextService
from app.services.dft_review_service import DFTResultReviewService
from app.schemas.evidence import EvidenceLocatorResponse
from app.services.paper_query import PaperQueryService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.llm_service import LLMService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.paper_knowledge_service import PaperKnowledgeService
from app.services.pdf_image_extractor import PdfImageExtractor
from app.services.review_service import ReviewService
from app.services.verification_session_service import VerificationSessionService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.domain.catalyst_basic_info import catalyst_basic_info_payload

router = APIRouter()


def _copy_paper_detail(detail: PaperDetailResponse, updates: dict) -> PaperDetailResponse:
    if hasattr(detail, "model_copy"):
        return detail.model_copy(update=updates)
    return detail.copy(update=updates)


def _lightweight_paper_detail(detail: PaperDetailResponse) -> PaperDetailResponse:
    """Keep the default detail payload small; heavy text is loaded only on demand."""
    return _copy_paper_detail(
        detail,
        {
            "sections": [],
            "paper_notes": [],
            "outgoing_relationships": [],
            "incoming_relationships": [],
            "references": [],
            "full_translation_zh": None,
        },
    )


TRANSLATION_SYSTEM_PROMPT = (
    "你是严谨的科研论文中文翻译助手。请将用户提供的英文论文片段翻译为简体中文。"
    "要求：保留化学式、材料名称、缩写、单位、数值、引用编号和专有名词；"
    "不要新增原文没有的结论；不要生成参考文献；只输出译文。"
)


def _trim_translation_source(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[原文过长，已截取前段用于预览]"


def _build_translation_prompt(title: str, text: str) -> str:
    return (
        f"片段标题：{title}\n\n"
        "请翻译下面的论文片段为简体中文，保持科研表达准确、克制：\n\n"
        f"{text}"
    )


def _source_only_translation_notice(text: str) -> str:
    return (
        "【网页端翻译 LLM 已停用，当前显示原文占位而非正式译文。"
        "如需中文译文，请在 IDE AI 中基于原文和证据链整理。】\n\n"
        + text
    )


def _manual_review_progress(data: dict[str, Any] | None) -> dict[str, Any]:
    source = data if isinstance(data, dict) else {}
    progress = source.get("manual_review_progress") if isinstance(source.get("manual_review_progress"), dict) else {}

    def normalize_entry(module: str) -> dict[str, Any]:
        raw = progress.get(module)
        if isinstance(raw, dict):
            return {
                "completed": bool(raw.get("completed")),
                "updated_at": raw.get("updated_at"),
                "updated_by": raw.get("updated_by"),
            }
        return {
            "completed": bool(raw),
            "updated_at": None,
            "updated_by": None,
        }

    return {
        "content": normalize_entry("content"),
        "figures": normalize_entry("figures"),
        "dft": normalize_entry("dft"),
    }


def _collect_translation_sources(
    detail: PaperDetailResponse,
    payload: PaperTranslationPreviewRequest,
) -> list[dict]:
    selected_section_ids = {str(item) for item in payload.section_ids}
    items: list[dict] = []
    if payload.include_abstract and detail.abstract:
        items.append(
            {
                "source_type": "abstract",
                "section_id": None,
                "title": "摘要",
                "page_start": None,
                "page_end": None,
                "text": _trim_translation_source(detail.abstract, payload.max_chars_per_item),
            }
        )

    sections = detail.sections or []
    if selected_section_ids:
        sections = [section for section in sections if str(section.id) in selected_section_ids]
    else:
        sections = sections[: payload.max_sections]

    for section in sections:
        text = _trim_translation_source(section.text, payload.max_chars_per_item)
        if not text:
            continue
        items.append(
            {
                "source_type": "section",
                "section_id": section.id,
                "title": section.section_title or section.section_type or "未命名章节",
                "page_start": section.page_start,
                "page_end": section.page_end,
                "text": text,
            }
        )
    return items


def _safe_unlink(base_dir: Path, stored_path: str | None, *, category: str, settings: Settings) -> str | None:
    if not stored_path:
        return None
    base = base_dir.resolve()
    target = resolve_persisted_artifact_path(
        stored_path,
        category=category,
        settings=settings,
        must_exist=False,
        trusted_persisted_reference=True,
    )
    if target is None:
        return None
    target = target.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if target.exists() and target.is_file():
        try:
            target.unlink()
            return str(target)
        except OSError:
            return None
    return None


@router.get("/{paper_id}", response_model=PaperDetailResponse)
def get_paper(
    paper_id: UUID,
    mode: str = Query("full", pattern="^(light|full)$"),
    session: Session = Depends(get_db_session),
) -> PaperDetailResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id, compact=(mode == "light"))
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    if mode == "light":
        return _lightweight_paper_detail(detail)
    return detail


@router.get("/{paper_id}/dft-results")
def get_paper_dft_results(
    paper_id: UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
    result_id: UUID | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    payload = PaperQueryService(session).get_dft_results_page(
        paper_id,
        offset=offset,
        limit=limit,
        result_id=result_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return payload


@router.post("/{paper_id}/figures/{figure_id}/delete-proposal")
async def propose_figure_delete(
    paper_id: UUID,
    figure_id: UUID,
    payload: FigureDeleteProposalRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if not payload.confirm_delete_proposal:
        raise HTTPException(status_code=400, detail="Explicit figure deletion proposal confirmation is required.")
    try:
        correction = ReviewService(session).propose_figure_deletion(
            paper_id=paper_id,
            figure_id=figure_id,
            reason=payload.reason,
            reviewer=payload.reviewer or "literature_library_user",
            evidence_payload=payload.evidence_payload,
        )
        session.commit()
        return {
            "status": correction.status,
            "correction_id": str(correction.id),
            "paper_id": str(correction.paper_id),
            "target_path": correction.target_path,
            "operation": correction.operation,
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{paper_id}/figures/{figure_id}/delete")
async def direct_delete_figure(
    paper_id: UUID,
    figure_id: UUID,
    payload: FigureDirectDeleteRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if not payload.confirm_direct_delete:
        raise HTTPException(status_code=400, detail="Explicit direct figure deletion confirmation is required.")
    settings = get_settings()
    try:
        correction, image_path, retired_ids = ReviewService(session).direct_delete_figure(
            paper_id=paper_id,
            figure_id=figure_id,
            reason=payload.reason,
            reviewer=payload.reviewer or "literature_library_user",
            evidence_payload=payload.evidence_payload,
        )
        session.commit()
    except ValueError as exc:
        detail = str(exc)
        if detail == "Figure not found for this paper.":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail.startswith("direct_delete_not_allowed:"):
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc

    deleted_files: list[str] = []
    if payload.delete_image_file:
        deleted = _safe_unlink(settings.storage_paths["figures"], image_path, category="figures", settings=settings)
        if deleted:
            deleted_files.append(deleted)
    return {
        "status": "deleted",
        "paper_id": str(paper_id),
        "figure_id": str(figure_id),
        "correction_id": str(correction.id),
        "retired_correction_ids": retired_ids,
        "deleted_files": deleted_files,
    }


@router.post("/{paper_id}/settle-ai-dft-reviews")
async def settle_ai_dft_reviews(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    summary = VerificationSessionService(session, settings).settle_ai_dft_reviews_for_paper(
        paper_id=paper_id,
        reviewer="literature_library_dft",
    )
    if (
        summary.get("waiting_second_ai_count", 0) == 0
        and summary.get("need_third_ai_count", 0) == 0
        and summary.get("need_repair_count", 0) == 0
    ):
        analysis = dict(paper.comprehensive_analysis or {})
        progress = _manual_review_progress(analysis)
        progress["dft"] = {
            "completed": True,
            "updated_at": datetime.now(UTC).isoformat(),
            "updated_by": "literature_library_dft",
        }
        analysis["manual_review_progress"] = progress
        paper.comprehensive_analysis = analysis
        session.add(paper)
    session.commit()
    return summary


@router.post("/{paper_id}/dft-ai-reviews/reset")
async def reset_dft_ai_reviews(
    paper_id: UUID,
    payload: DFTAIReviewResetRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    if not payload.confirm_reset_dft_ai_reviews:
        raise HTTPException(status_code=400, detail="Explicit DFT AI review reset confirmation is required.")
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    dft_result_ids = [
        str(row_id)
        for row_id in session.scalars(
            select(DFTResult.id).where(DFTResult.paper_id == paper_id)
        ).all()
    ]

    deleted_field_reviews = 0
    if dft_result_ids:
        result = session.execute(
            delete(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id.in_(dft_result_ids),
            )
        )
        deleted_field_reviews = int(result.rowcount or 0)

    object_review_rows = session.scalars(
        select(ExternalAnalysisCandidate).where(
            ExternalAnalysisCandidate.paper_id == paper_id,
            ExternalAnalysisCandidate.candidate_type.in_(("object_review_audit", "external_audit_opinion")),
        )
    ).all()
    dft_object_review_ids = []
    for candidate in object_review_rows:
        normalized = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        target_type = str(normalized.get("target_type") or "").strip().lower()
        materialized_type = str(candidate.materialized_target_type or "").strip().lower()
        target_id = str(normalized.get("target_id") or "").strip()
        if (
            target_type in {"dft_results", "dft_result"}
            or materialized_type == "dft_results"
            or target_id in dft_result_ids
        ):
            dft_object_review_ids.append(candidate.id)

    deleted_object_review_candidates = 0
    if dft_object_review_ids:
        result = session.execute(
            delete(ExternalAnalysisCandidate).where(
                ExternalAnalysisCandidate.id.in_(dft_object_review_ids)
            )
        )
        deleted_object_review_candidates = int(result.rowcount or 0)

    reset_dft_results = 0
    if dft_result_ids:
        result = session.execute(
            update(DFTResult)
            .where(DFTResult.paper_id == paper_id)
            .values(candidate_status="system_candidate")
        )
        reset_dft_results = int(result.rowcount or 0)

    reviewer = str(payload.reviewer or "literature_library_dft").strip() or "literature_library_dft"
    summary = {
        "paper_id": str(paper_id),
        "deleted_object_review_candidates": deleted_object_review_candidates,
        "deleted_field_reviews": deleted_field_reviews,
        "reset_dft_results": reset_dft_results,
        "kept_dft_candidates": bool(payload.keep_dft_candidates),
    }
    session.add(
        AuditLog(
            paper_id=paper_id,
            action="reset_dft_ai_reviews",
            source=reviewer,
            target_type="paper",
            target_id=str(paper_id),
            payload=summary,
        )
    )
    session.commit()
    return summary


@router.post("/{paper_id}/manual-review-progress")
async def set_manual_review_progress(
    paper_id: UUID,
    payload: ManualReviewProgressRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    module = str(payload.module or "").strip().lower()
    if module not in {"content", "figures", "dft"}:
        raise HTTPException(status_code=400, detail="module must be one of: content, figures, dft")
    analysis = dict(paper.comprehensive_analysis or {})
    progress = _manual_review_progress(analysis)
    progress[module] = {
        "completed": bool(payload.completed),
        "updated_at": datetime.now(UTC).isoformat(),
        "updated_by": str(payload.reviewer or "literature_library").strip() or "literature_library",
    }
    analysis["manual_review_progress"] = progress
    paper.comprehensive_analysis = analysis
    session.add(paper)
    session.add(
        AuditLog(
            paper_id=paper_id,
            action="set_manual_review_progress",
            source=progress[module]["updated_by"],
            target_type="paper",
            target_id=str(paper_id),
            payload={
                "module": module,
                "completed": bool(payload.completed),
            },
        )
    )
    session.commit()
    return {
        "paper_id": str(paper_id),
        "manual_review_progress": progress,
    }


@router.post("/{paper_id}/catalyst-samples/{sample_id}/basic-info")
async def update_catalyst_sample_basic_info(
    paper_id: UUID,
    sample_id: UUID,
    payload: CatalystBasicInfoUpdateRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    sample = session.get(CatalystSample, sample_id)
    if sample is None or sample.paper_id != paper_id:
        raise HTTPException(status_code=404, detail="Catalyst sample not found for this paper")

    provided = set(getattr(payload, "model_fields_set", set()) or set())
    merged = {
        "name": payload.name if "name" in provided else sample.name,
        "catalyst_type": payload.catalyst_type if "catalyst_type" in provided else sample.catalyst_type,
        "metal_centers": payload.metal_centers if "metal_centers" in provided else (sample.metal_centers or []),
        "coordination": payload.coordination if "coordination" in provided else sample.coordination,
        "support": payload.support if "support" in provided else sample.support,
        "synthesis_method": payload.synthesis_method if "synthesis_method" in provided else sample.synthesis_method,
        "evidence_strength": payload.evidence_strength if "evidence_strength" in provided else sample.evidence_strength,
    }
    normalized = catalyst_basic_info_payload(**merged)
    fields = normalized["fields"]
    before = {
        "name": sample.name,
        "catalyst_type": sample.catalyst_type,
        "metal_centers": sample.metal_centers or [],
        "coordination": sample.coordination,
        "support": sample.support,
        "synthesis_method": sample.synthesis_method,
        "evidence_strength": sample.evidence_strength,
    }

    if "name" in provided:
        sample.name = fields["name"]
    if "catalyst_type" in provided:
        sample.catalyst_type = fields["catalyst_type"]
    if "metal_centers" in provided:
        sample.metal_centers = fields["metal_centers"]
    if "coordination" in provided:
        sample.coordination = fields["coordination"]
    if "support" in provided:
        sample.support = fields["support"]
    if "synthesis_method" in provided:
        sample.synthesis_method = fields["synthesis_method"]
    if "evidence_strength" in provided:
        sample.evidence_strength = fields["evidence_strength"]

    source = str(payload.source or payload.reviewer or "literature_library_basic_info").strip() or "literature_library_basic_info"
    after = {
        "name": sample.name,
        "catalyst_type": sample.catalyst_type,
        "metal_centers": sample.metal_centers or [],
        "coordination": sample.coordination,
        "support": sample.support,
        "synthesis_method": sample.synthesis_method,
        "evidence_strength": sample.evidence_strength,
    }
    session.add(sample)
    session.add(
        AuditLog(
            paper_id=paper_id,
            action="update_catalyst_basic_info",
            source=source,
            target_type="catalyst_samples",
            target_id=str(sample_id),
            payload={
                "schema_version": normalized["schema_version"],
                "before": before,
                "after": after,
                "provided_fields": sorted(provided),
                "normalization": {
                    "raw": normalized["raw"],
                    "allowed_values": normalized["allowed_values"],
                    "normalization_source": normalized["normalization_source"],
                },
                "metal_descriptors": normalized["metal_descriptors"],
                "evidence_payload": payload.evidence_payload or {},
                "note": payload.note,
            },
        )
    )
    session.commit()
    session.refresh(sample)
    detail_payload = catalyst_basic_info_payload(
        name=sample.name,
        catalyst_type=sample.catalyst_type,
        metal_centers=sample.metal_centers or [],
        coordination=sample.coordination,
        support=sample.support,
        synthesis_method=sample.synthesis_method,
        evidence_strength=sample.evidence_strength,
    )
    return {
        "status": "updated",
        "paper_id": str(paper_id),
        "catalyst_sample_id": str(sample_id),
        "catalyst_sample": {
            "id": str(sample.id),
            **detail_payload["fields"],
            "support_raw": detail_payload["raw"]["support"],
            "support_normalized": detail_payload["fields"]["support"],
            "catalyst_type_raw": detail_payload["raw"]["catalyst_type"],
            "normalization_source": detail_payload["normalization_source"],
            **detail_payload["metal_descriptors"],
        },
        "allowed_values": detail_payload["allowed_values"],
    }


@router.post("/{paper_id}/catalyst-samples/from-dft-group")
async def create_or_bind_catalyst_sample_from_dft_group(
    paper_id: UUID,
    payload: CatalystBasicInfoCreateFromDFTRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    requested_ids = list(dict.fromkeys(payload.dft_result_ids))
    rows = session.scalars(
        select(DFTResult).where(
            DFTResult.paper_id == paper_id,
            DFTResult.id.in_(requested_ids),
        )
    ).all()
    rows_by_id = {row.id: row for row in rows}
    missing_ids = [str(row_id) for row_id in requested_ids if row_id not in rows_by_id]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={"message": "DFT results not found for this paper", "dft_result_ids": missing_ids},
        )
    already_bound = [str(row.id) for row in rows if row.catalyst_sample_id is not None]
    if already_bound:
        raise HTTPException(
            status_code=409,
            detail={"message": "Some DFT results are already bound", "dft_result_ids": already_bound},
        )

    normalized = catalyst_basic_info_payload(
        name=payload.name,
        catalyst_type=payload.catalyst_type,
        metal_centers=payload.metal_centers or [],
        coordination=payload.coordination,
        support=payload.support,
        synthesis_method=payload.synthesis_method,
        evidence_strength=payload.evidence_strength,
    )
    fields = normalized["fields"]
    sample_name = str(fields.get("name") or "").strip()
    if not sample_name:
        raise HTTPException(status_code=422, detail="Catalyst name is required")

    exact_matches = [
        item
        for item in session.scalars(
            select(CatalystSample).where(CatalystSample.paper_id == paper_id)
        ).all()
        if str(item.name or "").strip().casefold() == sample_name.casefold()
    ]
    if len(exact_matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple catalyst samples have the same name; edit the duplicate samples first",
        )

    created = not exact_matches
    sample = exact_matches[0] if exact_matches else CatalystSample(paper_id=paper_id)
    before = None if created else {
        "name": sample.name,
        "catalyst_type": sample.catalyst_type,
        "metal_centers": sample.metal_centers or [],
        "coordination": sample.coordination,
        "support": sample.support,
        "synthesis_method": sample.synthesis_method,
        "evidence_strength": sample.evidence_strength,
    }
    sample.name = fields["name"]
    sample.catalyst_type = fields["catalyst_type"]
    sample.metal_centers = fields["metal_centers"]
    sample.coordination = fields["coordination"]
    sample.support = fields["support"]
    sample.synthesis_method = fields["synthesis_method"]
    sample.evidence_strength = fields["evidence_strength"]
    session.add(sample)
    session.flush()

    for row in rows:
        row.catalyst_sample_id = sample.id
        session.add(row)

    source = str(payload.source or payload.reviewer or "literature_library_basic_info").strip() or "literature_library_basic_info"
    after = {
        "name": sample.name,
        "catalyst_type": sample.catalyst_type,
        "metal_centers": sample.metal_centers or [],
        "coordination": sample.coordination,
        "support": sample.support,
        "synthesis_method": sample.synthesis_method,
        "evidence_strength": sample.evidence_strength,
    }
    session.add(
        AuditLog(
            paper_id=paper_id,
            action="create_or_bind_catalyst_sample",
            source=source,
            target_type="catalyst_samples",
            target_id=str(sample.id),
            payload={
                "schema_version": normalized["schema_version"],
                "created": created,
                "before": before,
                "after": after,
                "bound_dft_result_ids": [str(row.id) for row in rows],
                "normalization": {
                    "raw": normalized["raw"],
                    "allowed_values": normalized["allowed_values"],
                    "normalization_source": normalized["normalization_source"],
                },
                "metal_descriptors": normalized["metal_descriptors"],
                "evidence_payload": payload.evidence_payload or {},
                "note": payload.note,
            },
        )
    )
    session.commit()
    return {
        "status": "created_and_bound" if created else "bound_existing",
        "paper_id": str(paper_id),
        "catalyst_sample_id": str(sample.id),
        "bound_dft_result_ids": [str(row.id) for row in rows],
        "created": created,
    }


@router.get("/{paper_id}/codex-context", response_model=CodexContextResponse)
async def get_paper_codex_context(
    paper_id: UUID,
    max_sections: int = 8,
    max_chars_per_section: int = 1800,
    max_figures: int = 12,
    max_tables: int = 8,
    max_candidates: int = 20,
    include_supplementary_figures: bool = False,
    session: Session = Depends(get_db_session),
) -> CodexContextResponse:
    context = CodexContextService(session).build_context(
        paper_id,
        max_sections=max(1, min(max_sections, 20)),
        max_chars_per_section=max(300, min(max_chars_per_section, 6000)),
        max_figures=max(0, min(max_figures, 40)),
        max_tables=max(0, min(max_tables, 30)),
        max_candidates=max(1, min(max_candidates, 100)),
        include_supplementary_figures=include_supplementary_figures,
    )
    if context is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return context


@router.get("/{paper_id}/codex-item/{item_type}/{item_id}", response_model=CodexItemContextResponse)
async def get_paper_codex_item(
    paper_id: UUID,
    item_type: str,
    item_id: UUID,
    max_chars_per_section: int = 1600,
    max_related_sections: int = 3,
    max_locators: int = 12,
    session: Session = Depends(get_db_session),
) -> CodexItemContextResponse:
    try:
        context = CodexContextService(session).build_item_context(
            paper_id,
            item_type,
            item_id,
            max_chars_per_section=max(300, min(max_chars_per_section, 6000)),
            max_related_sections=max(0, min(max_related_sections, 8)),
            max_locators=max(0, min(max_locators, 40)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if context is None:
        raise HTTPException(status_code=404, detail="Paper or item not found")
    return context


@router.get("/{paper_id}/knowledge-context", response_model=PaperKnowledgeContextResponse)
async def get_paper_knowledge_context(
    paper_id: UUID,
    max_candidates: int = 60,
    max_chars_per_candidate: int = 1200,
    category: str | None = None,
    session: Session = Depends(get_db_session),
) -> PaperKnowledgeContextResponse:
    context = PaperKnowledgeService(session).build_context(
        paper_id,
        max_candidates=max(1, min(max_candidates, 120)),
        max_chars_per_candidate=max(300, min(max_chars_per_candidate, 4000)),
        category=category,
    )
    if context is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return PaperKnowledgeContextResponse.model_validate(context)


@router.post("/{paper_id}/dft-results/{result_id}/verify", response_model=DFTResultVerifyResponse)
async def verify_dft_result(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTResultVerifyRequest,
    session: Session = Depends(get_db_session),
) -> DFTResultVerifyResponse:
    try:
        result = DFTResultReviewService(session).verify_result(
            paper_id=paper_id,
            result_id=result_id,
            confirm_reviewed_against_pdf=payload.confirm_reviewed_against_pdf,
            reviewer=payload.reviewer,
            reviewer_note=payload.reviewer_note,
            field_names=payload.field_names,
            expected_write_versions=payload.expected_write_versions,
            expected_write_version=payload.expected_write_version,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("write_conflict") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return DFTResultVerifyResponse.model_validate(result)


@router.post("/{paper_id}/dft-results/{result_id}/reject", response_model=DFTResultRejectResponse)
async def reject_dft_result(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTResultRejectRequest,
    session: Session = Depends(get_db_session),
) -> DFTResultRejectResponse:
    try:
        result = DFTResultReviewService(session).reject_result(
            paper_id=paper_id,
            result_id=result_id,
            confirm_reject_candidate=payload.confirm_reject_candidate,
            reviewer=payload.reviewer,
            reviewer_note=payload.reviewer_note,
            field_names=payload.field_names,
            expected_write_versions=payload.expected_write_versions,
            expected_write_version=payload.expected_write_version,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("write_conflict") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return DFTResultRejectResponse.model_validate(result)


@router.post("/{paper_id}/dft-results/{result_id}/corrections", response_model=DFTResultCorrectionProposalResponse)
async def propose_dft_result_correction(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTResultCorrectionProposalRequest,
    session: Session = Depends(get_db_session),
) -> DFTResultCorrectionProposalResponse:
    try:
        correction = DFTResultReviewService(session).propose_correction(
            paper_id=paper_id,
            result_id=result_id,
            confirm_correction_proposal=payload.confirm_correction_proposal,
            field_name=payload.field_name,
            proposed_value=payload.proposed_value,
            reason=payload.reason,
            reviewer=payload.reviewer,
            evidence_payload=payload.evidence_payload,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DFTResultCorrectionProposalResponse(correction=correction)


@router.patch("/{paper_id}/dft-results/{result_id}", response_model=DFTResultManualUpdateResponse)
async def manually_update_dft_result(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTResultManualUpdateRequest,
    session: Session = Depends(get_db_session),
) -> DFTResultManualUpdateResponse:
    try:
        result = DFTResultReviewService(session).manually_update_result(
            paper_id=paper_id,
            result_id=result_id,
            confirm_manual_update=payload.confirm_manual_update,
            updates=payload.updates,
            reason=payload.reason,
            reviewer=payload.reviewer,
            evidence_payload=payload.evidence_payload,
        )
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith("write_conflict") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return DFTResultManualUpdateResponse.model_validate(result)


@router.post("/{paper_id}/dft-results/{result_id}/apply-imported-opinion")
async def apply_imported_dft_opinion(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTImportedOpinionApplyRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return DFTResultReviewService(session).apply_imported_opinion(
            paper_id=paper_id,
            result_id=result_id,
            opinion=payload.opinion,
            reviewer=payload.reviewer,
            expected_row_state=payload.expected_row_state,
            expected_write_versions=payload.expected_write_versions,
        )
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith("write_conflict") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/{paper_id}/dft-results/{result_id}/revoke-review", response_model=DFTResultVerifyResponse)
async def revoke_dft_result_review(
    paper_id: UUID,
    result_id: UUID,
    payload: DFTResultVerifyRequest,
    session: Session = Depends(get_db_session),
) -> DFTResultVerifyResponse:
    try:
        result = DFTResultReviewService(session).revoke_result(
            paper_id=paper_id,
            result_id=result_id,
            reviewer=payload.reviewer,
            reviewer_note=payload.reviewer_note,
            field_names=payload.field_names,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DFTResultVerifyResponse.model_validate(result)


@router.post("/{paper_id}/translation/preview", response_model=PaperTranslationPreviewResponse)
async def preview_paper_translation(
    paper_id: UUID,
    payload: PaperTranslationPreviewRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PaperTranslationPreviewResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")

    sources = _collect_translation_sources(detail, payload)
    if not sources:
        raise HTTPException(status_code=400, detail="暂无可翻译的摘要或章节内容。")

    sync_writer_settings_from_session(session, settings)
    llm = LLMService(settings)
    if not llm.is_configured():
        return PaperTranslationPreviewResponse(
            paper_id=paper_id,
            title=detail.title,
            backend_used="local_fallback",
            llm_status="source_only_writer_llm_not_configured",
            items=[
                PaperTranslationItemResponse(
                    source_type=item["source_type"],
                    section_id=item["section_id"],
                    title=item["title"],
                    page_start=item["page_start"],
                    page_end=item["page_end"],
                    source_text=item["text"],
                    translated_text=_source_only_translation_notice(item["text"]),
                )
                for item in sources
            ],
        )

    translated_items: list[PaperTranslationItemResponse] = []
    used_fallback = False
    for item in sources:
        try:
            translated_text = llm.complete_text(
                TRANSLATION_SYSTEM_PROMPT,
                _build_translation_prompt(item["title"], item["text"]),
            )
        except Exception:
            translated_text = None
        if not translated_text:
            used_fallback = True
            translated_text = _source_only_translation_notice(item["text"])
        translated_items.append(
            PaperTranslationItemResponse(
                source_type=item["source_type"],
                section_id=item["section_id"],
                title=item["title"],
                page_start=item["page_start"],
                page_end=item["page_end"],
                source_text=item["text"],
                translated_text=translated_text,
            )
        )

    return PaperTranslationPreviewResponse(
        paper_id=paper_id,
        title=detail.title,
        backend_used="writer_llm" if not used_fallback else "writer_llm_with_source_fallback",
        llm_status="preview" if not used_fallback else "partial_source_only_fallback",
        items=translated_items,
    )


@router.delete("/{paper_id}")
async def delete_paper(
    paper_id: UUID,
    delete_pdf: bool = False,
    delete_derived: bool = False,
    session: Session = Depends(get_db_session),
) -> dict:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    settings = get_settings()
    files_to_delete: list[tuple[Path, str | None]] = []
    if delete_pdf:
        files_to_delete.append((settings.storage_paths["pdf"], paper.pdf_path))
    if delete_derived:
        files_to_delete.extend(
            [
                (settings.storage_paths["tei"], paper.tei_path),
                (settings.storage_paths["docling_json"], paper.docling_json_path),
                (settings.storage_paths["markdown"], paper.markdown_path),
            ]
        )
        figure_paths = session.scalars(select(PaperFigure.image_path).where(PaperFigure.paper_id == paper_id)).all()
        files_to_delete.extend((settings.storage_paths["figures"], path) for path in figure_paths)

    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "papers":
            continue
        conditions = []
        if "paper_id" in table.c:
            conditions.append(table.c.paper_id == paper_id)
        if "source_paper_id" in table.c:
            conditions.append(table.c.source_paper_id == paper_id)
        if "target_paper_id" in table.c:
            conditions.append(table.c.target_paper_id == paper_id)
        if conditions:
            condition = conditions[0]
            for extra in conditions[1:]:
                condition = condition | extra
            session.execute(table.delete().where(condition))
    session.delete(paper)
    session.commit()

    deleted_files = []
    for base_dir, stored_path in files_to_delete:
        category = base_dir.name
        deleted = _safe_unlink(base_dir, stored_path, category=category, settings=settings)
        if deleted:
            deleted_files.append(deleted)
    return {
        "status": "deleted",
        "paper_id": str(paper_id),
        "delete_pdf": delete_pdf,
        "delete_derived": delete_derived,
        "deleted_files": deleted_files,
    }


@router.post("/{paper_id}/reset-upload")
async def reset_paper_upload(
    paper_id: UUID,
    delete_pdf: bool = True,
    delete_derived: bool = True,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    settings = get_settings()
    files_to_delete: list[tuple[Path, str | None]] = []
    if delete_pdf:
        files_to_delete.append((settings.storage_paths["pdf"], paper.pdf_path))
    if delete_derived:
        files_to_delete.extend(
            [
                (settings.storage_paths["tei"], paper.tei_path),
                (settings.storage_paths["docling_json"], paper.docling_json_path),
                (settings.storage_paths["markdown"], paper.markdown_path),
            ]
        )
        figure_paths = session.scalars(select(PaperFigure.image_path).where(PaperFigure.paper_id == paper_id)).all()
        files_to_delete.extend((settings.storage_paths["figures"], path) for path in figure_paths)

    ingestion = PaperIngestionService(session=session, settings=settings)
    ingestion._clear_document_entities(paper.id)
    ingestion.extraction_pipeline._delete_existing_stage2(paper.id)

    # Keep the paper row reusable for metadata-only re-attach flows while
    # satisfying the non-null pdf_path constraint in the current schema.
    paper.pdf_path = ""
    paper.tei_path = ""
    paper.docling_json_path = ""
    paper.markdown_path = ""
    paper.comprehensive_analysis = None
    paper.oa_status = "metadata_only"
    paper.workflow_status = "metadata_only"
    if hasattr(paper, "pdf_quality_status"):
        paper.pdf_quality_status = None
    if hasattr(paper, "pdf_quality_score"):
        paper.pdf_quality_score = None
    if hasattr(paper, "pdf_quality_report"):
        paper.pdf_quality_report = None
    session.add(paper)
    session.commit()

    deleted_files = []
    for base_dir, stored_path in files_to_delete:
        category = base_dir.name
        deleted = _safe_unlink(base_dir, stored_path, category=category, settings=settings)
        if deleted:
            deleted_files.append(deleted)

    return {
        "status": "reset_to_metadata_only",
        "paper_id": str(paper_id),
        "paper_code": getattr(paper, "paper_code", None),
        "delete_pdf": delete_pdf,
        "delete_derived": delete_derived,
        "deleted_files": deleted_files,
    }


def _build_extraction_run_response(paper_id: UUID, summary: dict) -> ExtractionRunResponse:
    return ExtractionRunResponse(
        paper_id=paper_id,
        status=str(summary.get("status") or "completed"),
        action=summary.get("action"),
        llm_required=bool(summary.get("llm_required", False)),
        material_rebuild_completed=bool(summary.get("material_rebuild_completed", False)),
        external_ai_ready=bool(summary.get("external_ai_ready", False)),
        workflow_status=summary.get("workflow_status"),
        workspace_path=summary.get("workspace_path"),
        refreshed_materials=list(summary.get("refreshed_materials") or []),
        deferred_capabilities=list(summary.get("deferred_capabilities") or []),
        next_actions=list(summary.get("next_actions") or []),
        notes=list(summary.get("notes") or []),
        dft_settings=summary.get("dft_settings", 0),
        catalyst_samples=summary.get("catalyst_samples", 0),
        dft_results=summary.get("dft_results", 0),
        electrochemical_performance=summary.get("electrochemical_performance", 0),
        mechanism_claims=summary.get("mechanism_claims", 0),
        writing_cards=summary.get("writing_cards", 0),
    )


@router.post("/{paper_id}/prepare-ai-context", response_model=ExtractionRunResponse)
def prepare_external_ai_context(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    settings = get_settings()
    try:
        summary = PaperReprocessingService(session=session, settings=settings).rerun_stage2(paper_id)
    except ValueError as exc:
        if str(exc).startswith("paper_operation_conflict"):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    return _build_extraction_run_response(paper_id, summary)


@router.post("/{paper_id}/reparse", response_model=ExtractionRunResponse)
async def reparse_existing_paper(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    settings = get_settings()
    try:
        await PaperIngestionService(session=session, settings=settings).reparse_existing_paper(paper_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Paper not found after reparse")
    summary = {
        "action": "reparse_existing_pdf",
        "status": "completed",
        "llm_required": False,
        "material_rebuild_completed": True,
        "external_ai_ready": bool(getattr(detail.artifact_status, "artifact_ready_for_external_audit", False)),
        "workflow_status": detail.workflow_status,
        "workspace_path": detail.workspace_path,
        "refreshed_materials": [
            "pdf_parse",
            "tei",
            "markdown",
            "docling_json",
            "workspace",
            "stage2_candidates",
        ],
        "next_actions": [
            "Re-open the paper detail page and verify sections, figures, tables, and DFT candidates.",
            "If AI review is needed, continue with prepare-ai-context / codex-item / import_analysis.",
        ],
        "notes": list(getattr(detail.artifact_status, "blocking_errors", []) or []),
        "dft_settings": detail.counts.dft_settings,
        "catalyst_samples": detail.counts.catalyst_samples,
        "dft_results": detail.counts.dft_results,
        "electrochemical_performance": detail.counts.electrochemical_performance,
        "mechanism_claims": detail.counts.mechanism_claims,
        "writing_cards": detail.counts.writing_cards,
    }
    return _build_extraction_run_response(paper_id, summary)


@router.post("/{paper_id}/extract", response_model=ExtractionRunResponse, deprecated=True)
def rerun_stage2_extraction(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    # Compatibility alias: keep old clients working, but the supported meaning is
    # "prepare AI-readable materials for IDE/MCP follow-up", not backend LLM deep extraction.
    return prepare_external_ai_context(paper_id, session)


@router.post("/{paper_id}/figures/recrop")
def recrop_paper_figures(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    pdf_path = _resolve_paper_pdf_path(paper_id, session)
    settings = get_settings()
    figures = session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper_id)).all()
    if not figures:
        raise HTTPException(status_code=400, detail="No figures are available for recropping.")
    for figure in figures:
        figure.crop_status = "needs_recrop"
        session.add(figure)
    PdfImageExtractor.extract_figures(pdf_path, figures, settings.storage_paths["figures"])
    extracted = 0
    for figure in figures:
        if figure.image_path:
            extracted += 1
            figure.crop_status = "candidate_crop"
        session.add(figure)
    session.add(
        AuditLog(
            paper_id=paper_id,
            action="recrop_paper_figures",
            source="review_center",
            target_type="paper",
            target_id=str(paper_id),
            payload={
                "figure_count": len(figures),
                "extracted_count": extracted,
                "policy": "Figure crops are candidate locators and must be checked against the full PDF page.",
            },
        )
    )
    session.add(
        WorkflowJob(
            job_id=str(uuid4()),
            type="figure_evidence_relocation",
            status="completed",
            library_name=paper.library_name or "默认文献库",
            payload={
                "action": "recrop_paper_figures",
                "paper_id": str(paper_id),
                "title": paper.title,
                "figure_count": len(figures),
                "extracted_count": extracted,
            },
            progress={"completed": True},
            result={"status": "recorded"},
        )
    )
    session.commit()
    return {
        "paper_id": str(paper_id),
        "figure_count": len(figures),
        "extracted_count": extracted,
        "status": "recrop_completed",
    }


@router.get("/{paper_id}/evidence/locators", response_model=list[EvidenceLocatorResponse])
def get_paper_evidence_locators(
    paper_id: UUID,
    limit: int = Query(default=40, ge=1, le=200),
    session: Session = Depends(get_db_session),
) -> list[EvidenceLocatorResponse]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return EvidenceLocatorService(session).list_locators_for_paper(paper_id, limit=limit)


def _resolve_paper_pdf_path(paper_id: UUID, session: Session) -> Path:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not paper.pdf_path:
        raise HTTPException(status_code=404, detail="PDF not uploaded or unavailable")
    settings = get_settings()
    file_path = resolve_persisted_artifact_path(
        paper.pdf_path,
        category="pdf",
        settings=settings,
        trusted_persisted_reference=True,
    )
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")
    return file_path


@router.head("/{paper_id}/pdf")
async def head_paper_pdf(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    """Probe PDF availability without downloading it."""
    file_path = _resolve_paper_pdf_path(paper_id, session)
    return Response(
        status_code=200,
        media_type="application/pdf",
        headers={
            "Content-Length": str(file_path.stat().st_size),
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/{paper_id}/pdf")
async def get_paper_pdf(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
):
    """Serve the PDF file for a paper, for in-browser preview."""
    file_path = _resolve_paper_pdf_path(paper_id, session)
    return FileResponse(str(file_path), media_type="application/pdf")


@router.post("/{paper_id}/relationships", response_model=RelationshipCreateResponse)
async def create_paper_relationship(
    paper_id: UUID,
    payload: RelationshipCreateRequest,
    session: Session = Depends(get_db_session),
):
    from app.db.models import PaperRelationship
    source = session.get(Paper, paper_id)
    if not source:
        raise HTTPException(status_code=404, detail="Paper not found")

    target_ref = payload.target_paper_id.strip()
    target = None
    try:
        target = session.get(Paper, UUID(target_ref))
    except ValueError:
        target_codes = list({target_ref, target_ref.upper()})
        target = session.scalar(
            select(Paper).where(
                Paper.paper_code.in_(target_codes),
                Paper.library_name == source.library_name,
            )
        )
        if target is None:
            matches = session.scalars(select(Paper).where(Paper.paper_code.in_(target_codes))).all()
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                raise HTTPException(
                    status_code=409,
                    detail="Multiple papers match this paper_code; open the target paper and copy its UUID.",
                )
        if target is None and target_ref.upper().startswith("U"):
            try:
                migrated_code = supplementary_base_code(
                    main_paper_code=source.paper_code,
                    serial_number=source.serial_number,
                )
            except ValueError:
                migrated_code = ""
            if migrated_code:
                target = session.scalar(
                    select(Paper).where(
                        Paper.paper_code == migrated_code,
                        Paper.library_name == source.library_name,
                    )
                )
    if target is None:
        raise HTTPException(status_code=404, detail="Paper not found")

    if target.id == source.id:
        raise HTTPException(status_code=400, detail="A paper cannot be related to itself")

    relationship_type = payload.relationship_type.strip().lower()
    if not relationship_type:
        raise HTTPException(status_code=422, detail="relationship_type must not be empty")
    supplementary_types = {"supplementary", "supplementary_information", "supporting_information", "si"}
    if relationship_type not in supplementary_types:
        raise HTTPException(status_code=400, detail="Only supplementary relationships are supported")
    relationship_type = "supplementary"

    existing = session.scalar(
        select(PaperRelationship).where(
            PaperRelationship.source_paper_id == paper_id,
            PaperRelationship.target_paper_id == target.id,
            PaperRelationship.relationship_type == relationship_type,
        )
    )

    if target.paper_type != "supplementary":
        target.paper_type = "supplementary"
        session.add(target)
    target_code = str(target.paper_code or "").strip().upper()
    if not target_code.startswith("S"):
        try:
            target.paper_code = next_supplementary_paper_code(
                session,
                main_paper_code=source.paper_code,
                serial_number=source.serial_number,
                exclude_paper_id=target.id,
            )
            session.add(target)
        except ValueError as exc:
            if str(exc) != "supplementary_code_requires_main_code_or_serial":
                raise

    if existing is not None:
        session.commit()
        return RelationshipCreateResponse(status="existing", id=existing.id)

    rel = PaperRelationship(
        source_paper_id=paper_id,
        target_paper_id=target.id,
        relationship_type=relationship_type,
        note=payload.note,
        created_by="user_manual"
    )
    session.add(rel)
    session.commit()
    return RelationshipCreateResponse(status="created", id=rel.id)
