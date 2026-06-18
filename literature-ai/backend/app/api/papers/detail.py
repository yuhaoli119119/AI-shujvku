from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.settings import sync_writer_settings_from_session
from app.config import Settings, get_settings
from app.db.models import AuditLog, Base, Paper, PaperFigure, WorkflowJob
from app.db.session import get_db_session
from app.schemas.api import (
    CodexContextResponse,
    CodexItemContextResponse,
    DFTResultCorrectionProposalRequest,
    DFTResultCorrectionProposalResponse,
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
from pydantic import BaseModel

class RelationshipCreateRequest(BaseModel):
    target_paper_id: UUID
    relationship_type: str
    note: str | None = None

class RelationshipCreateResponse(BaseModel):
    status: str
    id: UUID


class DFTImportedOpinionApplyRequest(BaseModel):
    opinion: dict[str, Any]
    reviewer: str | None = None


class ManualReviewProgressRequest(BaseModel):
    module: str
    completed: bool
    reviewer: str | None = None

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
from app.services.verification_session_service import VerificationSessionService
from app.utils.artifact_paths import resolve_persisted_artifact_path

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
async def get_paper(
    paper_id: UUID,
    mode: str = Query("full", pattern="^(light|full)$"),
    session: Session = Depends(get_db_session),
) -> PaperDetailResponse:
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    if mode == "light":
        return _lightweight_paper_detail(detail)
    return detail


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


@router.get("/{paper_id}/codex-context", response_model=CodexContextResponse)
async def get_paper_codex_context(
    paper_id: UUID,
    max_sections: int = 8,
    max_chars_per_section: int = 1800,
    max_figures: int = 12,
    max_tables: int = 8,
    max_candidates: int = 20,
    session: Session = Depends(get_db_session),
) -> CodexContextResponse:
    context = CodexContextService(session).build_context(
        paper_id,
        max_sections=max(1, min(max_sections, 20)),
        max_chars_per_section=max(300, min(max_chars_per_section, 6000)),
        max_figures=max(0, min(max_figures, 40)),
        max_tables=max(0, min(max_tables, 30)),
        max_candidates=max(1, min(max_candidates, 100)),
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
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        )
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    summary = PaperReprocessingService(session=session, settings=settings).rerun_stage2(paper_id)
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
async def get_paper_evidence_locators(
    paper_id: UUID,
    session: Session = Depends(get_db_session),
) -> list[EvidenceLocatorResponse]:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return EvidenceLocatorService(session).list_locators_for_paper(paper_id)


def _resolve_paper_pdf_path(paper_id: UUID, session: Session) -> Path:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not paper.pdf_path:
        raise HTTPException(status_code=404, detail="PDF not uploaded or unavailable")
    settings = get_settings()
    file_path = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
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
    target = session.get(Paper, payload.target_paper_id)
    if not source or not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    
    rel = PaperRelationship(
        source_paper_id=paper_id,
        target_paper_id=payload.target_paper_id,
        relationship_type=payload.relationship_type,
        note=payload.note,
        created_by="user_manual"
    )
    session.add(rel)
    session.commit()
    return RelationshipCreateResponse(status="created", id=rel.id)
